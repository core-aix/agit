"""Background (headless) mode: aGiTrack tracks a user-driven native backend session without the
interactive TUI. Always no-worktree; manual (latent + fold on the user's commit) or auto (aGiTrack
folds the pending turns into a commit itself). Reuses the same CommitEngine + ManualCommitTracker
as the proxy, so token/turn accounting is identical."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import types
from pathlib import Path

import pytest

from agitrack.backends.base import TokenUsage
from agitrack.commits import ManualCommitTracker
from agitrack.config import AgitrackState
from agitrack.config.settings import GlobalConfig
from agitrack.git import GitRepo
from agitrack.proxy.background import (
    BackgroundRunner,
    background_handshake_path,
    background_status,
    stop_background,
)
from agitrack.transcripts.types import ExportedSession, SessionTurn
from agitrack.backends.proxy_agents import available_backends


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _delete_tree(path: Path) -> None:
    """Delete a directory that may contain git objects, on every OS.

    git writes loose objects READ-ONLY and Windows enforces that on delete, so a plain
    `shutil.rmtree` over a real repository raises ``PermissionError: [WinError 5]`` — which is
    exactly how these tests first failed on Windows CI while passing on macOS and Linux. Making
    every FILE writable first is the portable fix; directories are left alone, since stripping
    their execute bit on POSIX would stop rmtree descending into them."""
    for child in path.rglob("*"):
        if child.is_file():
            try:
                child.chmod(stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass  # best-effort: rmtree reports anything that actually blocks the delete
    shutil.rmtree(path)


def _git(repo: GitRepo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo.repo), *args], capture_output=True, text=True, check=True).stdout


class FakeBackend:
    name = "claude"

    def __init__(self, name: str | None = None) -> None:
        self.sessions: dict[str, ExportedSession] = {}
        self.latest: str | None = None
        if name is not None:
            self.name = name
        # What `repo_activity` reports: when this backend last saw a human here. None means
        # "never used in this repo", which is what every backend says in a fresh tmp repo.
        self.activity: float | None = None

    def latest_session_id(self, repo):
        return self.latest

    def repo_activity(self, repo):
        return self.activity

    def export_session(self, repo, session_id):
        return self.sessions.get(session_id)

    def set_session(self, session_id: str, turns: list[SessionTurn], *, model: str = "claude-opus-4-8") -> None:
        self.sessions[session_id] = ExportedSession(session_id, model, None, turns)
        self.latest = session_id


def _turn(uid: str, aid: str, prompt: str, response: str, out: int) -> SessionTurn:
    return SessionTurn(uid, aid, prompt, response, TokenUsage(total=out, output=out), "claude-opus-4-8")


def _runner(tmp_path, *, manual: bool):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    gc = GlobalConfig(path=tmp_path / "gc.json")
    runner = BackgroundRunner(repo, manual_commits=manual, _global_config=gc, _state=state)
    runner.backend = FakeBackend()
    runner._make_summarizer = lambda: None  # never spawn a real summarizer LLM in tests
    runner._summarization_enabled = lambda: False  # ...so auto-fold doesn't wait for a summary
    return runner, repo, state, runner.backend


# --- manual mode ------------------------------------------------------------


def test_a_tracker_stops_itself_once_its_repository_is_deleted(tmp_path):
    # A tracker that outlives its repo has nothing to do and NO WAY to be told so: `-b stop` and
    # `agitrack stop` both work through files under `<repo>/.agitrack/`, which went with the
    # directory. It polls a missing path every few seconds until the machine reboots. Temp-dir
    # repos make this routine — a scratch clone or a test run under /tmp is removed while its
    # tracker keeps going (six such daemons were found alive on a developer's machine).
    proj = tmp_path / "proj"
    proj.mkdir()
    runner, repo, _state, _backend = _runner(proj, manual=False)
    runner._explicit_stop = False
    _delete_tree(repo.repo)

    runner._loop()  # returns rather than polling forever

    assert runner._stop.is_set()
    # `_explicit_stop` so the update-restart path does not resurrect a tracker for a repo that
    # no longer exists.
    assert runner._explicit_stop is True


def test_a_tracker_keeps_running_when_only_git_was_removed(tmp_path):
    # Only the DIRECTORY disappearing is unambiguous. A repo whose `.git` was removed is one the
    # user still has and may be about to re-init, and quitting there would throw away the pending
    # turn this tracker holds.
    proj = tmp_path / "proj"
    proj.mkdir()
    runner, repo, _state, _backend = _runner(proj, manual=False)
    _delete_tree(repo.repo / ".git")

    assert runner._repo_is_gone() is False


def test_a_tracker_never_stops_because_the_check_itself_failed(tmp_path):
    # A permission error or a stalled network mount is not an answer. Treating "cannot tell" as
    # "gone" would stop a tracker on a repo that is merely slow to reach.
    proj = tmp_path / "proj"
    proj.mkdir()
    runner, _repo, _state, _backend = _runner(proj, manual=False)

    class _Unreachable:
        def exists(self):
            raise OSError("mount stalled")

    # Swapped on the runner rather than monkeypatching Path.exists for the whole process: that
    # patch also fires for anything else touching a path while the test runs (pytest's own
    # reporting included), which is a lot of blast radius for one call.
    runner.repo = types.SimpleNamespace(repo=_Unreachable())

    assert runner._repo_is_gone() is False


def test_a_tracker_remembers_its_repo_for_the_dashboard_switcher(tmp_path, monkeypatch):
    # THE BUG: the user-wide repo list was written ONLY by `ensure_hub_for`, i.e. only when a
    # dashboard was opened. `_open_dashboard_on_start` returns early for a scripted run, a
    # non-TTY stdout (the autotrack hook, a script, SSH) or `open_dashboard_on_start: false`, so
    # those trackers ran for days without their repo ever appearing in the switcher. Tracking a
    # repo is what makes it worth listing, so the tracker records it itself.
    from agitrack import repos as repo_registry

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    runner, repo, _state, _backend = _runner(tmp_path, manual=False)
    assert repo_registry.entry_for(repo.repo) is None  # nothing has opened a dashboard here

    runner._remember_repo()

    entry = repo_registry.entry_for(repo.repo)
    assert entry is not None and entry.served is True
    assert [e.path for e in repo_registry.list_repos()] == [str(repo.repo)]


def test_remembering_the_repo_never_breaks_the_tracker(tmp_path, monkeypatch):
    # Best-effort by design: a read-only or full home directory must not stop a repo being
    # tracked, so a registry that raises is swallowed rather than taking the daemon down.
    runner, _repo, _state, _backend = _runner(tmp_path, manual=False)

    def boom(*_args, **_kwargs):
        raise OSError("home is read-only")

    monkeypatch.setattr("agitrack.repos.remember", boom)

    runner._remember_repo()  # must not raise


def test_background_manual_records_latent_and_freezes_head(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    head = repo.rev_parse("HEAD")
    # The user drives the agent: it edits a file and the transcript records a completed turn.
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    assert runner._process_once() is True
    assert repo.rev_parse("HEAD") == head  # HEAD never moves in manual mode
    assert repo.ref_sha(runner._manual.ref()) is not None  # recorded on the latent ref
    # The fold trailer the prepare-commit-msg hook reads was rendered.
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()


def test_background_manual_folds_into_user_commit_via_hook(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    # The user commits their work (the fold + reset hooks are installed).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "my work")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "my work" in msg
    assert msg.count("# aGiTrack Metadata") == 2  # user block + the one folded turn
    assert len(_git(repo, "log", "--format=%H").split()) == 2  # init + one folded commit


# --- auto mode --------------------------------------------------------------


def test_background_auto_folds_pending_into_a_commit_itself(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()  # records the latent turn

    runner._auto_fold_pending()  # aGiTrack commits it itself (no user action)

    log = _git(repo, "log", "--format=%H").split()
    assert len(log) == 2  # init + the auto commit
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    # A CLEAN agent commit: the prompt is the subject (not a generic "commit agent turns"), with a
    # single agent metadata block — NOT the manual squash-into-a-user-commit format.
    assert msg.startswith("<aGiTrack> do x")
    assert "commit agent turns" not in msg
    assert "commit_type: user" not in msg
    assert msg.count("# aGiTrack Metadata") == 1 and "commit_type: agent" in msg
    # Ref reset to HEAD, so nothing is pending after the auto commit.
    assert repo.ref_sha(runner._manual.ref()) == repo.rev_parse("HEAD")
    assert runner._manual.pending_count() == 0


def test_background_auto_fold_waits_for_summary_then_uses_it_as_subject(tmp_path):
    # When summarization is on, the auto-fold must WAIT for the LLM summary and use it as the
    # commit subject/lead (the daemon never amends HEAD, so the summary must be in before commit).
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._summarization_enabled = lambda: True  # summaries on for this test
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    # No summary note yet ⇒ fold is deferred (no commit made).
    runner._auto_fold_pending()
    assert len(_git(repo, "log", "--format=%H").split()) == 1  # still just init

    # The summary lands as a git note on the latent tip; now the fold proceeds and uses it.
    tip = repo.ref_sha(runner._manual.ref())
    repo.notes_add(tip, "Add the greeting helper", namespace="agitrack/commit-summary")
    runner._auto_fold_pending()

    assert len(_git(repo, "log", "--format=%H").split()) == 2
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert msg.startswith("<aGiTrack> Add the greeting helper")  # summary leads the subject
    assert "commit_type: agent" in msg


def test_background_auto_skips_when_agent_committed_its_own_work(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()
    before = repo.rev_parse("HEAD")
    # The agent commits its own work (the fold hook folded tracking into it, resetting the ref).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent commit")
    runner._manual.service()  # react to the commit: drop the now-stale latent chain

    runner._auto_fold_pending()  # clean tree ⇒ nothing more to do

    # Only the agent's own commit was added — aGiTrack did NOT add a second commit on top.
    assert _git(repo, "log", "--format=%H").split()[0] != before
    assert len(_git(repo, "log", "--format=%H").split()) == 2


# --- discovery / accounting -------------------------------------------------


def test_background_follows_latest_session_and_counts_once(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    # First conversation, one turn.
    (tmp_path / "a.txt").write_text("one\nfirst\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "first", "done", 10)])
    assert runner._process_once() is True
    # Re-processing the SAME transcript records nothing new (watermark already past it).
    assert runner._process_once() is False

    # The user switches to a new conversation inside the backend; aGiTrack follows the latest.
    (tmp_path / "a.txt").write_text("one\nfirst\nsecond\n", encoding="utf-8")
    backend.set_session("s2", [_turn("u2", "m2", "second", "done", 15)])
    assert runner._process_once() is True
    assert state.backend_session_id == "s2"  # followed the switch
    # Two latent turns pending (one per conversation), each counted once.
    assert runner._manual.pending_count() == 2


def _in_flight_turn(uid: str, aid: str, prompt: str) -> SessionTurn:
    """A turn the agent is still working on: prompt received, no final response yet."""
    return SessionTurn(uid, aid, prompt, "", TokenUsage(total=0, output=0), "claude-opus-4-8", complete=False)


def test_agent_commit_mid_turn_is_attributed_by_the_fold_hook(tmp_path):
    # THE regression, end to end with the real hooks: the agent runs `git commit` ITSELF
    # partway through a turn. The turn is not complete, so there is no pending latent turn to
    # fold, and the commit used to land with no aGiTrack metadata at all — permanently
    # untracked, because the daemon's cover fallback also bails when the tree is dirty.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()  # installs prepare-commit-msg / post-commit
    assert (repo.repo / ".git" / "hooks" / "prepare-commit-msg").exists()

    # The agent is mid-turn and has edited the tree.
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    backend.set_session("s1", [_in_flight_turn("u1", "m1", "add the thing")])
    assert runner._process_once() is False  # nothing to record: the turn has not finished
    runner._manual.render_trailer()  # what the pre-commit flush does

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Add the thing")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "Add the thing" in msg  # the agent's own subject survives
    assert "# aGiTrack Metadata" in msg
    assert "commit_type: agent" in msg
    assert "in_flight: true" in msg  # marked partial: the full record follows later
    assert "add the thing" in msg  # the prompt it was working on
    assert "tokens_since_last_commit" not in msg  # counted once, on the completed turn


def test_completed_turn_still_covers_the_commit_it_stamped_mid_flight(tmp_path):
    # The hand-off. An in-flight block is attribution only — no trace, no tokens — so the
    # commit carrying it must NOT read as already-accounted-for, or the completed turn would
    # find nothing to attach to and its whole record (and token counts) would be lost.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    backend.set_session("s1", [_in_flight_turn("u1", "m1", "add the thing")])
    runner._process_once()
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own commit")
    agent_commit = repo.rev_parse("HEAD")
    assert "in_flight: true" in _git(repo, "log", "-1", "--format=%B", agent_commit)
    assert runner._is_agitrack_tracked(agent_commit) is False  # partial ⇒ still owed a record

    # The turn finishes with the tree clean (the agent committed everything it did).
    backend.set_session("s1", [_turn("u1", "m1", "add the thing", "done", 120)])
    assert runner._process_once() is True

    head = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "covered_commits:" in head and agent_commit[:7] in head  # its lines are attributed to AI
    assert "tokens_since_last_commit_output: 120" in head  # the usage lands exactly once
    assert runner._is_agitrack_tracked(repo.rev_parse("HEAD")) is True


@pytest.mark.parametrize("backend_name", available_backends())
def test_latent_turn_covers_a_mid_turn_commit_even_with_more_uncommitted_work(tmp_path, backend_name):
    # The dirty-tree hand-off. The agent commits ONCE mid-turn, then keeps editing, so when the
    # turn finishes the tree is still dirty and the clean-tree cover path bails — the turn records
    # LATENTLY. Its token count still spans the mid-turn commit's work, so the recorded body must
    # list that commit in covered_commits (and lead with the explanatory note) rather than leave it
    # attributed to nobody. Backend-agnostic: the metadata format and git-history detection are the
    # same for Claude and OpenCode, so it must hold for both.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    backend.name = backend_name
    state.backend = backend_name
    runner._manual.setup()
    runner._load_tracked_head()  # watermark starts at init (pre-existing history is never covered)

    # Mid-turn: the agent edits and commits its own work.
    (tmp_path / "a.txt").write_text("one\nmid-turn work\n", encoding="utf-8")
    backend.set_session("s1", [_in_flight_turn("u1", "m1", "add the thing")])
    runner._process_once()  # turn not complete: nothing recorded, in-flight noted
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own mid-turn commit")
    agent_commit = repo.rev_parse("HEAD")
    runner._manual.service()  # react to the agent's commit (drop the stale latent chain)
    assert runner._is_agitrack_tracked(agent_commit) is False  # in-flight only ⇒ still owed a record

    # The agent keeps working — MORE uncommitted edits — then the turn finishes with a dirty tree.
    (tmp_path / "a.txt").write_text("one\nmid-turn work\nstill more uncommitted\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "add the thing", "done", 120)])
    assert runner._process_once() is True  # records latently
    assert repo.rev_parse("HEAD") == agent_commit  # the latent record never moved HEAD

    body = runner._manual.pending_bodies()[-1]
    assert "covered_commits:" in body and agent_commit[:7] in body  # its lines are attributed to AI
    assert "tokens_since_last_commit_output: 120" in body  # the usage lands exactly once, here
    assert "This commit accounts" in body  # the explanatory note leads the trace
    assert f"backend: {backend_name}" in body  # recorded for whichever backend is driving

    # Attributed EXACTLY once: a further turn (before this one folds) must not re-list the hash.
    (tmp_path / "a.txt").write_text("one\nmid-turn work\nstill more uncommitted\neven more\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u2", "m2", "and more", "done", 5)])
    assert runner._process_once() is True
    second = runner._manual.pending_bodies()[-1]
    assert agent_commit[:7] not in second  # already accounted for by the first turn's body


@pytest.mark.parametrize("backend_name", available_backends())
def test_stop_finalize_never_covers_a_mid_turn_commit_before_the_final_message(tmp_path, backend_name):
    # The constraint: a cover (which attributes the agent's own commit to AI) is made ONLY after the
    # turn's final message. The stop finalize keeps a still-running turn so in-flight work is captured
    # on the way out — but it must NOT cover the agent's mid-turn commit, or the turn would be
    # attributed before it finished. Backend-agnostic: the completeness signal and cover path are the
    # same for Claude and OpenCode.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    backend.name = backend_name
    state.backend = backend_name
    runner._manual.setup()
    runner._load_tracked_head()
    base = repo.rev_parse("HEAD")

    # The agent commits its own work mid-turn; the turn is STILL running (no final message yet).
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    backend.set_session("s1", [_in_flight_turn("u1", "m1", "add the thing")])
    runner._process_once()  # in-flight noted, nothing recorded
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own mid-turn commit")
    agent_commit = repo.rev_parse("HEAD")
    runner._manual.service()
    assert runner._is_agitrack_tracked(agent_commit) is False  # in-flight only ⇒ still owed a record

    # Stop finalize (require_complete=False) while the turn is unfinished: no cover, no attribution,
    # and the watermark stays put so the completed turn still covers this commit later.
    assert runner._process_once(require_complete=False) is False
    assert repo.rev_parse("HEAD") == agent_commit  # no cover commit was placed on top
    assert runner._is_agitrack_tracked(agent_commit) is False  # not prematurely attributed
    assert runner._tracked_head == base  # watermark never advanced past the unfinished turn

    # Once the final message lands the cover is made normally — attributing the commit exactly once.
    backend.set_session("s1", [_turn("u1", "m1", "add the thing", "done", 120)])
    assert runner._process_once() is True
    head = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "covered_commits:" in head and agent_commit[:7] in head
    assert "tokens_since_last_commit_output: 120" in head  # usage lands exactly once, on completion


def test_no_trailer_for_a_human_commit_while_the_agent_is_idle(tmp_path):
    # The in-flight fallback must not turn "aGiTrack is running" into "every commit is
    # attributed to the AI". With no running turn there is no footprint at all.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    backend.set_session("s1", [_turn("u1", "m1", "done earlier", "ok", 10)])
    runner._process_once()  # records + clears: no turn is running now
    _git(repo, "commit", "-qm", "fold the pending turn", "--allow-empty")

    (tmp_path / "a.txt").write_text("one\nmy own edit\n", encoding="utf-8")
    runner._manual.render_trailer()
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My hand-written change")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "My hand-written change" in msg
    assert "# aGiTrack Metadata" not in msg


def test_in_flight_note_clears_once_the_turn_completes(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")

    backend.set_session("s1", [_in_flight_turn("u1", "m1", "add the thing")])
    runner._process_once()
    assert runner._in_flight is not None
    assert runner._in_flight["prompt"] == "add the thing"

    # The same turn finishes: the completed record takes over and the fallback stands down.
    backend.set_session("s1", [_turn("u1", "m1", "add the thing", "done", 12)])
    runner._process_once()
    assert runner._in_flight is None
    assert runner._manual.in_flight_attribution() is None


# --- parity with the interactive TUI's auto-commit rule ---------------------


def test_fold_waits_for_the_worktree_to_settle_like_the_tui(tmp_path):
    # `file_stable_seconds` is a documented setting the daemon used to ignore entirely: the TUI
    # waits for the tree to go quiet before auto-committing, the daemon folded immediately.
    import agitrack.proxy.background as B

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner.global_config.timings["file_stable_seconds"] = 8.0
    runner.global_config.timings["summary_wait_seconds"] = 45.0

    clock = [1000.0]
    monkey = B.time.monotonic
    B.time.monotonic = lambda: clock[0]  # type: ignore[assignment]
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        runner._sample_worktree()  # first observation: nothing to compare against yet
        backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
        runner._process_once()

        (tmp_path / "a.txt").write_text("one\nagent edit\nstill writing\n", encoding="utf-8")
        runner._sample_worktree()  # a real change lands
        head = repo.rev_parse("HEAD")
        runner._auto_fold_pending()
        assert repo.rev_parse("HEAD") == head  # deferred: the tree is still moving

        clock[0] += 9.0  # quiet for longer than file_stable_seconds
        runner._auto_fold_pending()
        assert repo.rev_parse("HEAD") != head  # committed once things settled
    finally:
        B.time.monotonic = monkey  # type: ignore[assignment]


def test_settle_wait_is_bounded_so_a_busy_repo_still_commits(tmp_path):
    # The TUI's settle gate is unbounded, which is survivable with a human watching. A daemon
    # commonly runs where something writes continuously (the log of a job the agent started), and
    # there the tree NEVER goes quiet — an unbounded wait would stop committing while looking
    # perfectly healthy. Past summary_wait_seconds on the same turn, fold anyway.
    import agitrack.proxy.background as B

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner.global_config.timings["file_stable_seconds"] = 8.0
    runner.global_config.timings["summary_wait_seconds"] = 45.0

    clock = [1000.0]
    monkey = B.time.monotonic
    B.time.monotonic = lambda: clock[0]  # type: ignore[assignment]
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        runner._sample_worktree()
        backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
        runner._process_once()
        head = repo.rev_parse("HEAD")

        # Something writes on every single cycle: the quiet period never elapses.
        for tick in range(12):  # 12 * 5s spans the 45s cap
            (tmp_path / "log.txt").write_text(f"line {tick}\n", encoding="utf-8")
            runner._sample_worktree()
            runner._auto_fold_pending()
            clock[0] += 5.0
        assert repo.rev_parse("HEAD") != head  # the cap fired instead of waiting forever
    finally:
        B.time.monotonic = monkey  # type: ignore[assignment]


def test_live_background_task_suspends_the_settle_cap(tmp_path):
    # A long task the agent started streams output for an hour, so the tree NEVER goes quiet.
    # With the cap applying, that produced a commit every few minutes whose whole diff was the
    # task's own log churn and whose turn was the agent saying "still running, nothing to
    # report" — the reported flood of minor "(background task completed)" commits. While the
    # task is live the wait is not capped, matching the TUI's unbounded gate, so the whole run
    # collapses into ONE commit carrying every turn's trace.
    import agitrack.proxy.background as B

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner.global_config.timings["file_stable_seconds"] = 8.0
    runner.global_config.timings["summary_wait_seconds"] = 45.0

    clock = [1000.0]
    monkey = B.time.monotonic
    B.time.monotonic = lambda: clock[0]  # type: ignore[assignment]
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        runner._sample_worktree()
        backend.set_session("s1", [_turn("u1", "m1", "(background task completed)", "still running", 900)])
        backend.sessions["s1"].live_background_task_ids = ["task-1"]  # the task is still streaming
        runner._process_once()
        assert runner._live_background_tasks == ["task-1"]  # carried off the export onto the runner
        head = repo.rev_parse("HEAD")

        for tick in range(12):  # far past the 45s cap
            (tmp_path / "run.log").write_text(f"line {tick}\n", encoding="utf-8")
            runner._sample_worktree()
            runner._auto_fold_pending()
            clock[0] += 5.0
        assert repo.rev_parse("HEAD") == head  # no commit while the task churns the tree

        # The task ends. The cap applies again, so the accumulated work commits once.
        runner._live_background_tasks = []
        runner._auto_fold_pending()
        assert repo.rev_parse("HEAD") != head
    finally:
        B.time.monotonic = monkey  # type: ignore[assignment]


def test_stop_finalize_captures_a_turn_that_never_finished(tmp_path):
    # The TUI's exit finalize passes require_complete=False so work in progress is still
    # recorded on the way out. The daemon's stop path used to keep require_complete=True, so
    # `agitrack -b stop` mid-turn silently dropped that turn's record.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nwork in progress\n", encoding="utf-8")
    backend.set_session("s1", [_in_flight_turn("u1", "m1", "half-done work")])

    assert runner._process_once() is False  # a normal poll leaves an unfinished turn alone
    runner._teardown()

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "half-done work" in msg and "# aGiTrack Metadata" in msg


def test_background_skips_cycle_when_no_human_driven_session(tmp_path):
    # The daemon must never fall back to a pinned id when the backend reports no
    # human-driven conversation. `latest_session_id` excludes programmatic (SDK) sessions,
    # so "None" here means the only transcripts in the repo dir are worker runs an agent
    # fanned out — and a still-running one would otherwise keep feeding the daemon turns.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nfirst\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "first", "done", 10)])
    assert runner._process_once() is True
    assert state.backend_session_id == "s1"

    # A worker run appends turns to a session aGiTrack no longer considers adoptable.
    backend.sessions["s1"] = ExportedSession(
        "s1", "claude-opus-4-8", None, [_turn("u1", "m1", "first", "done", 10), _turn("u2", "m2", "worker", "done", 99)]
    )
    (tmp_path / "a.txt").write_text("one\nfirst\nworker\n", encoding="utf-8")
    backend.latest = None
    assert runner._process_once() is False
    assert runner._manual.pending_count() == 1  # still just the human turn


# --- regression: a completed turn with changes is always committed ----------


def test_auto_pipeline_commits_completed_turn_on_dirty_tree(tmp_path):
    # Regression guard for "files left uncommitted": a completed agent turn whose edits sit in
    # the working tree MUST be committed by the auto (no-worktree, direct-commit) pipeline — HEAD
    # advances and the tree goes clean, never silently left for the user to commit by hand.
    import types

    from agitrack.proxy.commit_engine import CommitEngine
    from agitrack.proxy.session import Session

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")  # agent's uncommitted work

    session = Session.bare()
    session.state = state
    session.backend = types.SimpleNamespace(name="claude")
    exported = ExportedSession("s1", "m", None, [_turn("u1", "m1", "do it", "done", 7)])
    session.agent_parse_result = ("s1", exported, None, state)
    session.agent_parse_thread = None

    engine = CommitEngine(repo, state)
    head_before = repo.rev_parse("HEAD")

    def commit_fn(*, turns, backend, backend_session_id, model, quiet, prompt_untracked=True):
        def stage_untracked_fn(r, s):
            r.stage_paths(r.untracked_entries())

        return engine.commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=stage_untracked_fn,
        )

    committed, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=True,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda _s: None,
        mirror_fn=lambda _s: None,
        commit_fn=commit_fn,
    )

    assert committed is True
    assert repo.rev_parse("HEAD") != head_before  # a real commit was made, not left uncommitted
    assert "# aGiTrack Metadata" in repo.commit_message("HEAD")
    assert _git(repo, "status", "--short").strip() == ""  # working tree is now clean


# --- ManualCommitTracker direct --------------------------------------------


def test_manual_tracker_gate_and_record(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    head = repo.rev_parse("HEAD")

    assert tracker.gate() is False  # clean tree ⇒ nothing to record
    (tmp_path / "a.txt").write_text("one\nx\n", encoding="utf-8")
    assert tracker.gate() is True
    sha = tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")

    assert sha is not None
    assert repo.rev_parse("HEAD") == head  # HEAD frozen
    assert repo.ref_sha(tracker.ref()) is not None
    assert tracker.pending_count() == 1


# --- stop / status handshake ------------------------------------------------


def test_background_status_reports_none_when_not_running(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    assert background_status(repo) == 0
    assert "No aGiTrack background tracker" in capsys.readouterr().out


def test_background_status_reports_running_for_live_pid(tmp_path, capsys):
    import json
    import os

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "mode": "auto commits"}), encoding="utf-8")

    assert background_status(repo) == 0
    out = capsys.readouterr().out
    assert "is running" in out and "auto commits" in out


def test_background_stop_cleans_stale_handshake(tmp_path, capsys):
    import json

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A dead pid (very high, not alive) ⇒ treated as not running and the stale file is removed.
    path.write_text(json.dumps({"pid": 2_000_000_000, "mode": "auto commits"}), encoding="utf-8")

    assert stop_background(repo) == 0
    assert "No aGiTrack background tracker" in capsys.readouterr().out
    assert not path.exists()  # stale handshake cleaned up


def test_background_run_writes_and_removes_handshake(tmp_path, monkeypatch):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)
    # Stop immediately after the first loop iteration so run() returns.
    runner._stop.set()
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)

    assert runner.run() == 0
    # After a clean run the handshake is removed again.
    assert not background_handshake_path(repo).exists()


def test_start_background_daemon_restarts_running(tmp_path, capsys, monkeypatch):
    # Like `-d` and `--backtrace`: re-running `agitrack -b` replaces a live tracker so an
    # updated aGiTrack takes effect, after the old one's clean SIGTERM shutdown.
    import json

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4241, "mode": "auto commits"}), encoding="utf-8")

    alive = {4241, 4242}
    terminated = []
    monkeypatch.setattr(bg, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(bg, "terminate_pid", lambda pid: (terminated.append(pid), alive.discard(pid)))

    class _FakeProc:
        pid = 4242

    def fake_spawn(r, *, extra_args):
        p = background_handshake_path(r)
        p.write_text(json.dumps({"pid": 4242, "mode": "auto commits"}), encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(bg, "spawn_background_daemon", fake_spawn)

    assert bg.start_background_daemon(repo, extra_args=[]) == 0
    out = capsys.readouterr().out
    assert "Restarting the aGiTrack background tracker (was PID 4241" in out
    assert terminated == [4241]  # the old tracker got its clean shutdown signal
    assert "daemon live" in out and "4242" in out


def test_start_background_daemon_leaves_a_current_version_daemon_in_place(tmp_path, capsys, monkeypatch):
    # Re-running `agitrack -b` over a daemon ALREADY on the current version must NOT tear it down
    # and respawn it — that needless churn (stop→start on every unrelated invocation) is what made
    # the tracker look like it "quit" whenever aGiTrack was touched. Only a stale daemon restarts.
    import json

    from agitrack import __version__
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4241, "mode": "auto commits", "version": __version__}), encoding="utf-8")

    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)
    terminated, spawned = [], []
    monkeypatch.setattr(bg, "terminate_pid", lambda pid: terminated.append(pid))
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda *a, **k: spawned.append(1) or None)

    assert bg.start_background_daemon(repo, extra_args=[]) == 0
    assert terminated == []  # the live current-version daemon was left running
    assert spawned == []  # nothing new was spawned
    assert "left in place" in capsys.readouterr().out


def test_running_tracker_is_current_matches_only_the_live_current_version_tracker(tmp_path, monkeypatch):
    import json

    from agitrack import __version__
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)

    # A live daemon on the current version, with a matching lock owner ⇒ current (leave it).
    path.write_text(json.dumps({"pid": 4241, "version": __version__}), encoding="utf-8")
    assert bg._running_tracker_is_current(repo) is True
    assert bg._running_tracker_is_current(repo, owner_pid=4241) is True
    # A different lock owner (e.g. an interactive session) ⇒ not "the tracker", never skip.
    assert bg._running_tracker_is_current(repo, owner_pid=9999) is False
    # An older version ⇒ stale: restart is warranted to load the new code.
    path.write_text(json.dumps({"pid": 4241, "version": "0.0.1"}), encoding="utf-8")
    assert bg._running_tracker_is_current(repo) is False
    # A pre-version handshake (daemon from before this stamp existed) reads as stale too.
    path.write_text(json.dumps({"pid": 4241}), encoding="utf-8")
    assert bg._running_tracker_is_current(repo) is False


def test_running_tracker_is_not_current_when_a_different_commit_mode_is_requested(tmp_path, monkeypatch):
    # A mode switch must replace the daemon, not be answered with "already running". `agitrack -b -m`
    # over an auto-commit daemon (or `-b --auto-commit` over a manual one) used to hit the
    # leave-it-in-place shortcut, so the requested commit mode was silently ignored.
    import json

    from agitrack import __version__
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)
    path.write_text(json.dumps({"pid": 4241, "mode": "auto commits", "version": __version__}), encoding="utf-8")

    assert bg._running_tracker_is_current(repo, manual=False) is True  # same mode ⇒ leave it
    assert bg._running_tracker_is_current(repo, manual=True) is False  # switch to manual ⇒ replace
    assert bg._running_tracker_is_current(repo) is True  # no mode asked for ⇒ version check only

    path.write_text(json.dumps({"pid": 4241, "mode": "manual commits", "version": __version__}), encoding="utf-8")
    assert bg._running_tracker_is_current(repo, manual=True) is True
    assert bg._running_tracker_is_current(repo, manual=False) is False  # switch to auto ⇒ replace


def test_start_background_daemon_restarts_a_daemon_running_the_other_commit_mode(tmp_path, capsys, monkeypatch):
    # Same rule through the launcher: the forwarded --manual-commits/--auto-commit flag decides
    # whether the live daemon can be reused.
    import json

    from agitrack import __version__
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4241, "mode": "auto commits", "version": __version__}), encoding="utf-8")

    alive = {4241, 4242}
    terminated = []
    monkeypatch.setattr(bg, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(bg, "terminate_pid", lambda pid: (terminated.append(pid), alive.discard(pid)))

    class _FakeProc:
        pid = 4242

    def fake_spawn(r, *, extra_args):
        background_handshake_path(r).write_text(
            json.dumps({"pid": 4242, "mode": "manual commits", "version": __version__}), encoding="utf-8"
        )
        return _FakeProc()

    monkeypatch.setattr(bg, "spawn_background_daemon", fake_spawn)

    assert bg.start_background_daemon(repo, extra_args=["--manual-commits"]) == 0
    out = capsys.readouterr().out
    assert terminated == [4241]  # the auto-commit daemon was replaced, not left in place
    assert "manual commits" in out and "4242" in out


def test_handshake_records_the_running_version(tmp_path, monkeypatch):
    import json

    from agitrack import __version__

    monkeypatch.setattr("agitrack.daemons.register", lambda *a, **k: None)  # don't touch the real registry
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._write_handshake()
    info = json.loads(background_handshake_path(repo).read_text(encoding="utf-8"))
    assert info["version"] == __version__  # so a later rerun can tell current from stale


def test_start_background_daemon_fails_when_old_tracker_will_not_stop(tmp_path, capsys, monkeypatch):
    import json

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4241, "mode": "auto commits"}), encoding="utf-8")

    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)
    monkeypatch.setattr(bg, "terminate_pid", lambda pid: None)
    monkeypatch.setattr(bg.time, "sleep", lambda s: None)
    monkeypatch.setattr(bg, "_terminate_and_wait", lambda pid, **k: False)
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda *a, **k: spawned.append(1) or None)

    assert bg.start_background_daemon(repo, extra_args=[]) == 1
    assert "did not stop in time" in capsys.readouterr().out
    assert spawned == []  # never spawns beside a still-live tracker


def test_replace_running_tracker_only_replaces_a_background_tracker(tmp_path, capsys, monkeypatch):
    import json

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4241, "mode": "auto commits"}), encoding="utf-8")

    alive = {4241}
    monkeypatch.setattr(bg, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(bg, "terminate_pid", lambda pid: alive.discard(pid))

    # The lock holder is NOT the tracker (an interactive session): refuse to replace.
    assert bg.replace_running_tracker(repo, owner_pid=7777) is False
    assert 4241 in alive  # nothing was terminated

    # The lock holder IS the live tracker: stop it and allow the rerun to take over.
    assert bg.replace_running_tracker(repo, owner_pid=4241) is True
    assert 4241 not in alive
    assert "Restarting the aGiTrack background tracker (was PID 4241)" in capsys.readouterr().out


def test_start_background_daemon_spawns_and_reports(tmp_path, capsys, monkeypatch):
    import json

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)

    class _FakeProc:
        pid = 4242

    def fake_spawn(r, *, extra_args):
        # Simulate the detached child publishing its handshake right after launch.
        p = background_handshake_path(r)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": 4242, "mode": "auto commits"}), encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(bg, "spawn_background_daemon", fake_spawn)
    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)

    assert bg.start_background_daemon(repo, extra_args=["--auto-commit"]) == 0
    out = capsys.readouterr().out
    assert "daemon live" in out and "4242" in out and "agitrack -b stop" in out


def test_start_background_daemon_reports_failure_when_child_dies(tmp_path, capsys, monkeypatch):
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)

    class _FakeProc:
        pid = 4243

    # Child never writes a handshake and is not alive ⇒ wait_for_handshake gives up fast.
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda r, *, extra_args: _FakeProc())
    monkeypatch.setattr(bg, "pid_alive", lambda pid: False)

    assert bg.start_background_daemon(repo, extra_args=[]) == 1
    assert "did not start" in capsys.readouterr().out


def test_a_slow_start_is_not_reported_as_a_failed_one(tmp_path, capsys, monkeypatch):
    """The daemon publishes its handshake LAST, after the git work that arms the repo, and on a
    large repository that work outran the launcher's wait: a tracker that was coming up perfectly
    was announced as one that "did not start", and — because that answer was an error — the
    dashboard was not opened for it either. A child that is still alive is still starting."""
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)

    class _FakeProc:
        pid = 4244

    # Alive, but slower to arm than the wait: no handshake by the deadline.
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda r, *, extra_args: _FakeProc())
    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)

    assert bg.start_background_daemon(repo, extra_args=[], timeout=0.2) == 0
    out = capsys.readouterr().out
    assert "still starting" in out and "4244" in out
    assert "did not start" not in out


def test_startup_takes_one_worktree_snapshot_for_both_recovery_steps(tmp_path, monkeypatch):
    """Snapshotting the working tree stages it into a throwaway index with no stat cache, so it
    re-hashes every tracked file — 4.4s on one 14 GB repo, taken TWICE by startup for the same
    question, which is most of why arming outran the launcher's handshake wait."""
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    (tmp_path / "a.txt").write_text("uncommitted\n", encoding="utf-8")
    runner._manual.record("turn one")  # a latent tip, so reset_stale_ref does not return early

    real = repo.snapshot_worktree_tree
    calls: list[str] = []

    def counted():
        calls.append("snapshot")
        return real()

    monkeypatch.setattr(repo, "snapshot_worktree_tree", counted)
    runner._manual.setup()

    assert len(calls) == 1, f"startup re-snapshotted the working tree: {len(calls)} snapshots"


def test_daemon_installs_autotrack_hook_by_default_and_skips_when_off(tmp_path):
    from agitrack.git import hooks as git_hooks

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    hook = repo.repo / ".git" / "hooks" / "pre-commit"

    # Default (keep): the persistent hook is installed.
    runner._install_autotrack_hook()
    assert hook.exists() and git_hooks.is_autotrack_hook(hook)

    # autotrack_hook=off: installing removes it (the user opted out via the -b prompt).
    runner.global_config.autotrack_hook = "off"
    runner._install_autotrack_hook()
    assert not hook.exists()


def test_background_writes_event_log(tmp_path):
    # The event log records notable events in background mode too: an AI change detected and
    # the commit aGiTrack makes for it. Works with --log-file / config in every mode.
    from agitrack.events import EventLog

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    log = tmp_path / "events.log"
    runner.events = EventLog(log)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    assert runner._process_once() is True  # records the latent turn → ai-change-detected
    runner._auto_fold_pending()  # folds into a real commit → commit event

    text = log.read_text(encoding="utf-8")
    assert "ai-change-detected" in text and "backend=claude" in text
    assert "commit " in text and "type=agent" in text


def _precommit_env(tmp_path, monkeypatch, backend: "FakeBackend", *, autostart: bool = False):
    """Set up a repo + injected fake backend so precommit_sync (which builds its own
    BackgroundRunner via make_proxy_agent) runs in-process against the fake. ``autostart=False``
    sets ``autotrack_hook: off`` so the fold happens WITHOUT spawning a real daemon subprocess."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    hook = "auto" if autostart else "off"
    (cfg / "config.json").write_text(f'{{"default_backend": "claude", "autotrack_hook": "{hook}"}}', encoding="utf-8")
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda name: backend)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)
    # Stub the spawn by DEFAULT. Auto-start is no longer gated on "did this commit carry AI
    # work" (a dead tracker could never be revived otherwise), so every autostart=True test now
    # reaches it — and one that had not thought about the daemon started a real detached
    # `python -m agitrack --background-serve` that outlived the test. The tests that are ABOUT
    # the spawn re-patch this with their own recorder, which wins.
    monkeypatch.setattr("agitrack.proxy.background.spawn_background_daemon", lambda repo, *, extra_args: None)


def test_precommit_sync_folds_ai_work_into_the_commit(tmp_path, monkeypatch):
    # The persistent auto-track pre-commit hook path: with AI work pending and no tracker running,
    # precommit_sync records the turn + renders the trailer + installs the fold hooks, so the
    # user's in-progress commit carries the full trace/metadata (the user's requirement).
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")  # AI-made change in the tree

    assert precommit_sync(repo) == 0
    # The trailer was rendered for the prepare-commit-msg hook, and the fold hooks are installed.
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()

    # The user now commits; the installed fold hook folds the trace into that ONE commit.
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My change")
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "My change" in msg and "# aGiTrack Metadata" in msg  # single, fully-tracked commit


def test_precommit_sync_git_ignores_agitrack_dir(tmp_path, monkeypatch):
    # aGiTrack's own state files (the trailer, ref, …) must never leak into a user commit: the
    # sync must git-ignore .agitrack/ before writing any of them.
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert precommit_sync(repo) == 0
    exclude = (repo.repo / ".git" / "info" / "exclude").read_text()
    assert ".agitrack/" in exclude.splitlines()
    # A blanket `git add -A` must not stage any .agitrack file.
    _git(repo, "add", "-A")
    staged = _git(repo, "diff", "--cached", "--name-only")
    assert not any(line.startswith(".agitrack/") for line in staged.splitlines())


def test_precommit_sync_no_ai_work_is_a_noop(tmp_path, monkeypatch):
    # No pending AI turns ⇒ precommit_sync leaves no footprint: no trailer content, no reminder.
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()  # no session set ⇒ nothing to track
    _precommit_env(tmp_path, monkeypatch, backend)
    (tmp_path / "a.txt").write_text("one\njust me\n", encoding="utf-8")  # human-only edit

    assert precommit_sync(repo) == 0
    trailer_file = repo.repo / ".agitrack" / "manual-pending-trailer"
    # Either no trailer file, or an empty one — a human commit is left untouched.
    assert not trailer_file.exists() or trailer_file.read_text().strip() == ""


def test_a_background_run_records_the_backend_it_was_given(tmp_path, monkeypatch):
    # `agitrack -b --backend claude` used to record the choice NOWHERE: the state was seeded with
    # that same value as an in-memory default, so the "differs from what the state resolves to?"
    # test was always false. Everything downstream then saw a repo with no backend at all.
    from agitrack.config import GlobalConfig
    from agitrack.proxy.background import BackgroundRunner

    repo = _init_repo(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda name: FakeBackend())
    config = GlobalConfig(path=cfg / "config.json")
    assert config.default_backend is None  # a machine that has never chosen one

    runner = BackgroundRunner(repo, backend="claude", _global_config=config)

    assert runner.state.data.get("backend") == "claude"  # written to the repo's state FILE
    assert GlobalConfig(path=cfg / "config.json").default_backend == "claude"


def test_the_commit_hook_installs_as_soon_as_the_repo_state_names_a_backend(tmp_path, monkeypatch):
    # The hook's "is a backend configured?" question used to be asked as `runner.state.backend`,
    # which RAISES when none is, straight into a blanket `except Exception: return 0` — so with
    # nothing recorded the hook was a silent no-op (no fold hooks, no trace, the commit's AI work
    # lost) while `agitrack -s` still said "Auto-start on commit: on". The unanswerable case must
    # still never fail the user's commit; the answerable one must work, and it is the one the
    # backend-recording fix above now produces on every `-b` run.
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    (cfg / "config.json").write_text('{"privacy_ack": true}', encoding="utf-8")  # no default_backend
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda name: FakeBackend())
    # autotrack_hook defaults to "auto" here, and auto-start is no longer gated on the commit
    # carrying AI work, so this reaches the spawn. This test is about the HOOKS; a real detached
    # daemon would outlive it.
    monkeypatch.setattr("agitrack.proxy.background.spawn_background_daemon", lambda repo, *, extra_args: None)

    assert precommit_sync(repo) == 0  # never fails the user's commit
    assert not (repo.repo / ".git" / "hooks" / "prepare-commit-msg").exists()

    # With a backend recorded in the repo's own state, the very same call DOES install them.
    (repo.repo / ".agitrack").mkdir(exist_ok=True)
    (repo.repo / ".agitrack" / "state.json").write_text('{"backend": "claude"}', encoding="utf-8")
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)

    assert precommit_sync(repo) == 0
    assert (repo.repo / ".git" / "hooks" / "prepare-commit-msg").exists()


def test_the_hook_names_the_backend_when_it_auto_starts_the_daemon(tmp_path, monkeypatch):
    # The spawned daemon resolves `--backend or the GLOBAL default` and never reads this repo's
    # state, so without an explicit --backend it died on startup ("No coding agent backend is
    # configured.") seconds after the commit told the user tracking had started.
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    (cfg / "config.json").write_text('{"autotrack_hook": "auto"}', encoding="utf-8")  # no default
    (repo.repo / ".agitrack").mkdir(exist_ok=True)
    (repo.repo / ".agitrack" / "state.json").write_text('{"backend": "claude"}', encoding="utf-8")
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda name: backend)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "agitrack.proxy.background.spawn_background_daemon",
        lambda _repo, *, extra_args: spawned.append(list(extra_args)),
    )

    precommit_sync(repo)

    assert spawned and "--backend" in spawned[0]
    assert spawned[0][spawned[0].index("--backend") + 1] == "claude"


def test_precommit_sync_defers_to_a_running_tracker(tmp_path, monkeypatch):
    # When a live tracker holds the repo lock, precommit_sync does nothing (that tracker's own
    # fold hooks handle this commit) — it must never double-track.
    from agitrack.git import RepoLock
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    held = RepoLock(repo.repo / ".agitrack" / "lock")
    assert held.acquire()  # simulate a running tracker
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        assert precommit_sync(repo) == 0
        # Deferred: no latent ref recorded by the sync (the running tracker owns that).
        assert (
            repo.ref_sha("refs/agitrack/manual/" + AgitrackState(tmp_path, default_backend="claude").session_id) is None
        )
    finally:
        held.release()


def test_precommit_sync_autostart_spawns_daemon(tmp_path, monkeypatch):
    # By default (autotrack_hook=auto) the sync also auto-starts the daemon for future commits,
    # in the SAME commit mode as the last run (persisted via write_background_mode).
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend, autostart=True)
    bg.write_background_mode(repo, manual=True)  # last run was manual-commit mode
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args))
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0
    assert spawned and "--manual-commits" in spawned[0]  # resumes the last run's mode


def test_precommit_sync_off_does_not_spawn_daemon(tmp_path, monkeypatch):
    # autotrack_hook=off: the sync still folds the AI work into the commit but never auto-starts.
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend, autostart=False)  # -> autotrack_hook: off
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda *a, **k: spawned.append(k))
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0
    assert spawned == []  # folded, but no daemon started
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()


# --- pre-commit flush: keep the trailer fresh when a daemon is running -------


def test_daemon_flush_request_records_and_acks(tmp_path):
    # The daemon services a pre-commit flush request by recording pending completed turns and
    # re-rendering the fold trailer synchronously, then echoing the nonce back. This is what keeps
    # a commit that races ahead of the daemon's poll from folding a stale/empty trailer.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")  # AI edit still in the tree
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    (repo.repo / ".agitrack" / "flush-request").write_text("nonce-1", encoding="utf-8")
    runner._service_flush_requests()

    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()
    assert (repo.repo / ".agitrack" / "flush-done").read_text().strip() == "nonce-1"
    # A repeated identical nonce is a no-op (dedup) — it must not re-service.
    (repo.repo / ".agitrack" / "flush-done").write_text("stale", encoding="utf-8")
    runner._service_flush_requests()
    assert (repo.repo / ".agitrack" / "flush-done").read_text().strip() == "stale"


def test_background_commit_folds_fresh_turn_via_flush(tmp_path):
    # End-to-end: a turn completed but the daemon hasn't polled yet, so the trailer is empty and a
    # naive commit would fold NOTHING (the reported bug). The pre-commit flush records the turn +
    # renders the trailer, so the very next commit carries the full trace/metadata.
    runner, repo, state, backend = _runner(tmp_path, manual=False)  # auto mode; the fold hook is installed too
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    # Before the flush the trailer holds no turn (daemon hasn't recorded it yet).
    trailer = repo.repo / ".agitrack" / "manual-pending-trailer"
    assert "# aGiTrack Metadata" not in (trailer.read_text() if trailer.exists() else "")

    (repo.repo / ".agitrack" / "flush-request").write_text("n1", encoding="utf-8")
    runner._service_flush_requests()  # the daemon flushes on the pre-commit hook's request

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "my work")
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "my work" in msg
    assert "# aGiTrack Metadata" in msg  # the turn's trace/metadata folded into the commit


def test_precommit_sync_nudges_a_running_daemon_to_flush(tmp_path, monkeypatch):
    # When a LIVE background daemon holds the lock, precommit_sync no longer just bails: it asks the
    # daemon to flush so this commit folds a fresh trailer (it still records nothing itself — the
    # daemon is the single writer).
    import json
    import os

    from agitrack.git import RepoLock
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    held = RepoLock(repo.repo / ".agitrack" / "lock")
    assert held.acquire()  # simulate the daemon holding the repo lock
    bg.background_handshake_path(repo).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(bg, "request_daemon_flush", lambda r, **k: calls.append(r) or True)
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        assert bg.precommit_sync(repo) == 0
        assert calls  # it nudged the running daemon to flush
        # Still never records the turn itself (single-writer): no latent ref from the sync.
        assert repo.ref_sha("refs/agitrack/manual/" + state_session_id(tmp_path)) is None
    finally:
        held.release()


def state_session_id(tmp_path) -> str:
    return AgitrackState(tmp_path, default_backend="claude").session_id


# --- agent commits its own work mid-turn (final message comes after the commit) ---


def test_daemon_covers_agent_commit_made_during_an_unfinished_turn(tmp_path):
    # The hard case: the agent `git commit`s its own work (mid-turn, so its final message comes
    # after the commit), and by the time the turn finishes the tree is clean — the tree-gated
    # recorder would drop it. The daemon reconciles from its persistent watermark: HEAD has advanced
    # past it with an untracked commit, so it covers that commit with the turn's trace/metadata.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()
    init_head = repo.rev_parse("HEAD")
    assert runner._tracked_head == init_head  # watermark starts at HEAD

    # The agent commits its OWN work — HEAD moves, the working tree ends clean.
    (tmp_path / "a.txt").write_text("one\nagent code\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    agent_head = repo.rev_parse("HEAD")

    # The turn completes ⇒ the daemon covers the untracked commit since the watermark.
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    head = repo.rev_parse("HEAD")
    assert head != agent_head  # a cover was added on top
    # ONE cover commit, merge-shaped: first parent = the watermark, second = the agent's commit.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [init_head, agent_head]
    cover_msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert cover_msg.count("# aGiTrack Metadata") == 1  # metadata EXACTLY once — never duplicated
    assert "do x" in cover_msg  # the turn's trace
    assert "covered_commits:" in cover_msg and agent_head[:7] in cover_msg  # attributes its lines to AI
    assert repo.rev_parse("HEAD^{tree}") == repo.rev_parse(agent_head + "^{tree}")  # no diff
    assert _git(repo, "cat-file", "-t", agent_head).strip() == "commit"  # agent's commit keeps its hash
    assert runner._tracked_head == head  # watermark advanced to the cover


def test_cover_commit_message_leads_with_the_summary(tmp_path):
    # A cover commit must be summarized like every other aGiTrack commit: because the daemon can't
    # amend HEAD, it summarizes SYNCHRONOUSLY before committing so the message leads with the summary
    # (not the raw prompt), and records the summary cost + a git note.
    class FakeSummarizer:
        model = "sum-model"
        tokens_input = 11
        tokens_output = 7
        tokens_cache_read = 0

        def summarize_commit(self, *, trace):
            return "Add a greeting helper and its test"

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._summarization_enabled = lambda: True
    runner._make_summarizer = lambda: FakeSummarizer()
    runner._manual.setup()
    runner._load_tracked_head()

    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert msg.startswith("<aGiTrack> Add a greeting helper and its test")  # summary leads, not "do x"
    assert "summary_model: sum-model" in msg and "summary_tokens_input: 11" in msg  # cost recorded
    assert "covered_commits:" in msg  # still a proper cover
    note = _git(repo, "notes", "--ref", "agitrack/commit-summary", "show", "HEAD")
    assert "Add a greeting helper" in note  # summary also stored as a note


def test_watermark_persists_and_covers_a_commit_made_while_the_daemon_was_down(tmp_path):
    # The watermark is what makes coverage survive a restart / the daemon being down at commit time:
    # a commit made with no daemon running is still covered when the next turn completes, because the
    # PERSISTED watermark predates it (the old in-flight snapshot was lost on every restart).
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()  # persists watermark = init HEAD to .agitrack/tracked-head
    init_head = repo.rev_parse("HEAD")

    # "Daemon down": a commit is made with nothing observing the turn in flight.
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent commit while daemon down")
    agent_head = repo.rev_parse("HEAD")

    # A fresh runner (a restart) reloads the PERSISTED watermark — init_head, not the current HEAD.
    restarted = BackgroundRunner(
        repo, manual_commits=False, _global_config=GlobalConfig(path=tmp_path / "gc2.json"), _state=state
    )
    restarted.backend = backend
    restarted._make_summarizer = lambda: None
    restarted._summarization_enabled = lambda: False
    restarted._manual.setup()
    restarted._load_tracked_head()
    assert restarted._tracked_head == init_head  # survived the restart (not agent_head)

    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    restarted._process_once()

    assert repo.rev_parse("HEAD") != agent_head  # the commit made while down is now covered
    assert "covered_commits:" in _git(repo, "log", "-1", "--format=%B", "HEAD")


def test_daemon_does_not_cover_a_pure_qa_turn(tmp_path):
    # A completed turn that made NO commit (pure Q&A, no file change) must not create a cover:
    # HEAD == watermark, so there is nothing new to attribute.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()
    head = repo.rev_parse("HEAD")

    backend.set_session("s1", [_turn("u1", "m1", "explain this", "here is why", 15)])  # pure Q&A
    runner._process_once()

    assert repo.rev_parse("HEAD") == head  # nothing committed ⇒ no cover


def test_daemon_never_retroactively_covers_preexisting_history(tmp_path):
    # On a repo it has never tracked, the watermark initializes to the CURRENT HEAD, so human commits
    # made BEFORE the daemon are never attributed to AI — only commits after the watermark are.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    (tmp_path / "h.txt").write_text("human\n", encoding="utf-8")  # a human commit made before tracking
    _git(repo, "add", "h.txt")
    _git(repo, "commit", "-m", "human work")
    human_head = repo.rev_parse("HEAD")
    runner._manual.setup()
    runner._load_tracked_head()
    assert runner._tracked_head == human_head  # watermark = current HEAD, not the repo root

    backend.set_session("s1", [_turn("u1", "m1", "hi", "hello", 15)])  # a turn that makes no commit
    runner._process_once()

    assert repo.rev_parse("HEAD") == human_head  # the pre-existing human commit is NOT covered


# --- mode switching: a stale watermark must not claim another mode's commits ---


def _timed_turn(uid: str, aid: str, prompt: str, response: str, out: int, *, started_at: int) -> SessionTurn:
    """A completed turn stamped with when its prompt arrived — what the turn-window gate reads."""
    turn = _turn(uid, aid, prompt, response, out)
    turn.started_at = started_at
    turn.ended_at = started_at + 5
    return turn


def _commit_at(repo: GitRepo, path, name: str, message: str, *, when: int) -> str:
    """Commit *name* with a fixed committer date so the turn-window gate can be exercised."""
    path.write_text(f"{name} content\n", encoding="utf-8")
    _git(repo, "add", name)
    stamp = f"{when} +0000"
    subprocess.run(
        ["git", "-C", str(repo.repo), "commit", "-m", message],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
    )
    return repo.rev_parse("HEAD")


@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("backend_name", available_backends())
def test_stale_watermark_never_claims_a_human_commit_from_another_mode(tmp_path, manual, backend_name):
    # THE mode-switch regression. The coverage watermark is PERSISTENT (it must survive the daemon
    # being down when a commit is made), so after a crash — or a switch to interactive/manual mode,
    # or a plain `git commit` with no aGiTrack running — a purely human commit can sit between the
    # stale watermark and HEAD. It predates the next turn's prompt, so it is NOT that turn's work
    # and must never appear in covered_commits. Before the fix, a human commit made in another mode
    # was attributed to the next AI turn on both the cover path (clean tree) and the latent path.
    runner, repo, state, backend = _runner(tmp_path, manual=manual)
    backend.name = backend_name  # both backends stamp turns, so the gate must hold for both
    state.backend = backend_name
    runner._manual.setup()
    runner._load_tracked_head()
    stale = repo.rev_parse("HEAD")  # watermark: where the crashed daemon last accounted

    # The user commits their own work while nothing is tracking — LONG before the next prompt.
    human = _commit_at(repo, tmp_path / "notes.md", "notes.md", "Human: document the project", when=1_700_000_000)
    assert runner._tracked_head == stale  # still behind the human commit

    # A new turn arrives and leaves its edits uncommitted (the latent path).
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    backend.set_session("s1", [_timed_turn("u1", "m1", "do x", "done", 42, started_at=1_700_100_000)])

    assert runner._since_turn_start([human], [backend.sessions["s1"].turns[0]]) == []
    assert runner._uncovered_agent_commits(backend.sessions["s1"].turns) == []
    assert runner._agent_committed_own_work(backend.sessions["s1"].turns) == []

    assert runner._process_once() is True
    body = (runner._manual.pending_bodies() or [_git(repo, "log", "-1", "--format=%B", "HEAD")])[-1]
    assert "covered_commits" not in body  # the human's commit is not claimed
    assert human[:7] not in body


def test_a_commit_made_after_the_prompt_is_still_covered_when_the_daemon_was_down(tmp_path):
    # The other half of the gate: coverage of a commit made while the daemon wasn't watching is the
    # whole reason the watermark persists, so a commit made AFTER the turn's prompt must still be
    # claimed. Only commits predating the prompt are excluded.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()

    # The turn's prompt arrives, then its work is committed (by the agent or the user) — the daemon
    # only sees both afterwards.
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    own = _commit_at(repo, tmp_path / "a.txt", "a.txt", "the turn's work", when=1_700_100_030)
    backend.set_session("s1", [_timed_turn("u1", "m1", "do x", "done", 42, started_at=1_700_100_000)])

    assert runner._agent_committed_own_work(backend.sessions["s1"].turns) == [own]
    assert runner._process_once() is True
    head = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "covered_commits:" in head and own[:7] in head


def test_turn_window_gate_is_skipped_when_the_transcript_has_no_timestamps(tmp_path):
    # Backends/turns without timestamps must keep the old behavior rather than lose coverage:
    # with nothing to compare against, every untracked commit past the watermark stays claimable.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()
    sha = _commit_at(repo, tmp_path / "a.txt", "a.txt", "some commit", when=1_700_000_000)

    untimed = _turn("u1", "m1", "do x", "done", 42)
    assert untimed.started_at is None
    assert runner._since_turn_start([sha], [untimed]) == [sha]


def test_repo_status_reports_each_mode(tmp_path, capsys):
    import json
    import os

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    agit = repo.repo / ".agitrack"
    agit.mkdir(exist_ok=True)

    # Not running — and it reports the auto-start (autotrack_hook) state. Not "on commit":
    # auto-start also fires when an agent turn leaves changes behind, so a status line that
    # says "on commit" tells the user tracking will wait for something it will not wait for.
    assert bg.repo_status(repo) == 0
    out = capsys.readouterr().out.lower()
    assert "not running" in out and "auto-start:" in out

    # Background daemon (live pid via our own pid) with a manual-commit handshake.
    bg.background_handshake_path(repo).write_text(
        json.dumps({"pid": os.getpid(), "mode": "manual commits", "backend": "opencode"}), encoding="utf-8"
    )
    assert bg.repo_status(repo) == 0
    out = capsys.readouterr().out
    assert "BACKGROUND" in out and "manual-commit" in out and "no worktree" in out and "opencode" in out
    bg.background_handshake_path(repo).unlink()

    # Interactive proxy (live pid) with worktree + auto commit.
    bg.proxy_status_path(repo).write_text(
        json.dumps({"pid": os.getpid(), "mode": "interactive", "commits": "auto", "worktree": True}), encoding="utf-8"
    )
    assert bg.repo_status(repo) == 0
    out = capsys.readouterr().out
    assert "INTERACTIVE" in out and "auto-commit" in out and "worktree" in out and "no worktree" not in out


def test_proxy_status_write_and_clear(tmp_path):
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    (repo.repo / ".agitrack").mkdir(exist_ok=True)
    bg.write_proxy_status(repo, commits="manual", worktree=False)
    info = bg._read_proxy_status(repo)
    assert info["commits"] == "manual" and info["worktree"] is False
    bg.clear_proxy_status(repo)
    assert bg._read_proxy_status(repo) is None


def test_global_summarization_disabled_is_not_shadowed_by_state_default(tmp_path):
    # A global `summarization_enabled: false` must win — the per-repo AgitrackState defaults it to
    # True, and that default must NOT shadow the global toggle (else you can't turn it off in bg).
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    del runner._summarization_enabled  # drop the test stub to exercise the real method
    runner.global_config.summarization_enabled = False
    assert state.summarization_enabled is True  # state still defaults on
    assert runner._summarization_enabled() is False  # ...but the global 'off' wins


def test_fold_summary_ready_bails_when_worker_finished_without_note(tmp_path):
    # When the summary worker has finished but produced NO note (the summarizer errored), the fold
    # must proceed immediately rather than waiting out the full summary_wait_seconds.
    import threading

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._summarization_enabled = lambda: True
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()
    tip = repo.ref_sha(runner._manual.ref())

    # No note, and a still-running worker ⇒ keep waiting.
    alive = threading.Event()
    t = threading.Thread(target=alive.wait)
    t.start()
    runner._summary_threads[tip] = t
    assert runner._fold_summary_ready(tip) is False
    alive.set()
    t.join()

    # Worker finished with no note ⇒ fold now (don't wait out the deadline).
    assert runner._fold_summary_ready(tip) is True


def test_background_mode_persist_roundtrip(tmp_path):
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    assert bg.read_background_mode(repo) is None  # nothing recorded yet
    bg.write_background_mode(repo, manual=True)
    assert bg.read_background_mode(repo) is True
    bg.write_background_mode(repo, manual=False)
    assert bg.read_background_mode(repo) is False


def test_daemon_update_check_writes_marker_and_clears(tmp_path, monkeypatch):
    # The daemon's periodic pass now SELF-UPDATES (behind the cross-process lock) and still
    # records what is left for the user, clearing a stale marker once up to date. The
    # attempt is stubbed here — no test may run a real update, which fetches and merges the
    # checkout aGiTrack runs from (see the conftest guard); what is asserted is what the
    # daemon does with the status the attempt reports.
    from agitrack.events import EventLog
    from agitrack.update.marker import read_update_marker

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    log = tmp_path / "events.log"
    runner.events = EventLog(log)

    class _Status:
        ok = True
        available = True
        current = "0.1.16"
        latest = "0.2.0"
        message = "aGiTrack update available: 0.1.16 → 0.2.0."

    def _attempt(status_cls):
        def _run(*, debug=None, timeout=None, on_status=None):
            if on_status is not None:
                on_status(status_cls())
            from agitrack.update.selfupdate import SelfUpdateRecord

            return SelfUpdateRecord()

        return _run

    monkeypatch.setattr("agitrack.update.selfupdate.attempt_self_update", _attempt(_Status))
    runner._run_update_check()
    info = read_update_marker(repo.repo)
    assert info and info["latest"] == "0.2.0"
    assert "update-available" in log.read_text() and "0.2.0" in log.read_text()

    # Now up to date ⇒ the stale marker is cleared.
    class _None(_Status):
        available = False

    monkeypatch.setattr("agitrack.update.selfupdate.attempt_self_update", _attempt(_None))
    runner._run_update_check()
    assert read_update_marker(repo.repo) is None


def test_background_status_shows_available_update(tmp_path, capsys):
    from agitrack.update.marker import write_update_marker

    repo = _init_repo(tmp_path)
    # The check's own sentence is what every surface repeats: see `update_reminder_line`.
    write_update_marker(
        repo.repo, current="0.1.16", latest="0.2.0", message="aGiTrack update available: 0.1.16 → 0.2.0."
    )
    assert background_status(repo) == 0
    out = capsys.readouterr().out
    assert "update available" in out.lower() and "0.2.0" in out


def test_precommit_sync_reminds_about_update_on_every_commit(tmp_path, monkeypatch, capsys):
    from agitrack.proxy.background import precommit_sync
    from agitrack.update.marker import write_update_marker

    repo = _init_repo(tmp_path)
    backend = FakeBackend()  # no AI work — the reminder must still show
    _precommit_env(tmp_path, monkeypatch, backend)
    write_update_marker(
        repo.repo, current="0.1.16", latest="0.2.0", message="aGiTrack update available: 0.1.16 → 0.2.0."
    )

    assert precommit_sync(repo) == 0
    err = capsys.readouterr().err
    assert "update available" in err.lower() and "0.2.0" in err


def test_manual_tracker_reconcile_covers_external_commit(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    tracker.hooks_installed = False  # force the cover fallback
    tracker.last_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    tracker.gate()
    tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external")
    user_head = repo.rev_parse("HEAD")
    user_tree = repo.rev_parse("HEAD^{tree}")

    tracker.reconcile_external_commit()

    cover = repo.rev_parse("HEAD")
    assert cover != user_head  # a cover commit was added
    assert repo.parents(cover)[0] == user_head  # first-parent = the user's own commit
    assert repo.rev_parse("HEAD^{tree}") == user_tree  # cover introduced no diff
    assert "# aGiTrack Metadata" in repo.commit_message(cover)
    assert repo.ref_sha(tracker.ref()) == cover  # ref reset


def test_daemon_manual_mode_never_commits_for_the_user(tmp_path):
    """`agitrack -b --manual-commits`: the user decides when to commit, full stop.

    The daemon's cover path — which exists so an agent commit that leaves a clean tree still
    gets its trace and tokens — was never gated on manual mode, so it added an unrequested
    commit to the user's branch. That is the one thing manual mode promises will not happen.
    The interactive proxy already refuses this; the daemon simply never checked.

    Nothing is lost by refusing: the latent path records the turn instead, and the fold hook
    combines it into the user's next commit.
    """
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    runner._load_tracked_head()

    # The agent commits its OWN work, leaving the tree clean — the case that triggers a cover.
    (tmp_path / "a.txt").write_text("one\nagent code\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    agent_head = repo.rev_parse("HEAD")

    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    assert repo.rev_parse("HEAD") == agent_head, "manual mode must not commit on the user's behalf"


def test_daemon_auto_mode_still_covers_an_agent_commit(tmp_path):
    # The other side, so the manual gate stays narrow: AUTO mode must keep covering, or the
    # fix would trade an unwanted commit for lost token accounting.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()

    (tmp_path / "a.txt").write_text("one\nagent code\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    agent_head = repo.rev_parse("HEAD")

    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    assert repo.rev_parse("HEAD") != agent_head
    assert "do x" in _git(repo, "log", "-1", "--format=%B", "HEAD")


def test_manual_daemon_records_a_turn_whose_only_action_was_its_own_midturn_commit(tmp_path):
    """`-b --manual-commits`, agent self-commits mid-turn, turn ends with a clean tree.

    The daemon's half of the same loss the interactive proxy had: the tracker's `gate()` and
    `record()` both read an unchanged tree as "nothing happened", so a turn whose work the agent
    had already committed was dropped — no trace, no tokens, `commit_turns` returning False and
    nothing said. Manual mode makes it worse: aGiTrack must not commit for the user, so the
    latent chain is the ONLY place the accounting can live until their next commit folds it in.
    """
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    runner._load_tracked_head()

    # Mid-turn: the agent commits its own work. The tree is clean from here on.
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    backend.set_session("s1", [_in_flight_turn("u1", "m1", "add the thing")])
    runner._process_once()
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    agent_head = repo.rev_parse("HEAD")

    # The turn finishes having edited nothing further.
    backend.set_session("s1", [_turn("u1", "m1", "add the thing", "done", 120)])
    runner._process_once()

    assert repo.rev_parse("HEAD") == agent_head, "manual mode must not commit on the user's behalf"
    bodies = runner._manual.pending_bodies()
    assert bodies, "the turn's trace and tokens were dropped instead of held for the user's commit"
    assert "tokens_since_last_commit_output: 120" in bodies[-1]
    assert agent_head[:7] in bodies[-1], "the agent's own commit must be attributed in covered_commits"


def test_the_daemons_record_guard_still_refuses_a_turn_that_changed_nothing(tmp_path):
    # The guard is loosened, not removed: with no commit owed a record an unchanged tree really
    # does mean nothing happened, and recording would chain an empty latent commit every poll.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    assert runner._manual.gate() is True
    assert runner._manual.record("<aGiTrack> first\n") is not None

    runner._manual.owed_record = False
    assert runner._manual.gate() is False
    assert runner._manual.record("<aGiTrack> nothing\n") is None


def test_precommit_sync_autostart_resumes_auto_mode(tmp_path, monkeypatch):
    # The mirror of the manual-mode autostart test. A regression here silently flips the user's
    # chosen commit mode after a reboot or a pre-commit-triggered restart: they get aGiTrack
    # committing for them when they had chosen to commit themselves, or the reverse.
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend, autostart=True)
    bg.write_background_mode(repo, manual=False)  # the last run was AUTO
    spawned: list = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args))
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0

    assert spawned, "the daemon was never autostarted"
    assert "--auto-commit" in spawned[0]
    assert "--manual-commits" not in spawned[0]


# --- a repo that TRACKS the agent scaffolding dirs ---------------------------
#
# `snapshot_worktree_tree()` strips every agent-config prefix (`.agitrack/`, `.claude/`, `.codex/`,
# `.opencode/` — see git/repo.py `_NEVER_STAGE_PREFIXES`); a raw `^{tree}` does
# not. So in a repo that COMMITS its `.claude/` config — ordinary practice, a team shares its
# agent setup like an editorconfig — "is the working tree clean?" answered "dirty" forever, and
# `_agent_committed_own_work` bails on a dirty tree. The daemon therefore covered NO agent
# self-commit at all: exactly the loss the persistent watermark exists to prevent, reintroduced
# by nothing more than a file the user chose to track. `GitRepo.comparable_tree` strips the same
# paths from the commit side so the comparison means what it says.


def _track_the_agent_config(repo: GitRepo, path: Path) -> None:
    (path / ".claude").mkdir(exist_ok=True)
    (path / ".claude" / "settings.json").write_text('{"model": "opus"}\n', encoding="utf-8")
    _git(repo, "add", "-f", ".claude/settings.json")
    _git(repo, "commit", "-qm", "share the agent config, as teams do")
    assert repo.rev_parse("HEAD^{tree}") != repo.snapshot_worktree_tree(), "precondition"


def test_daemon_covers_an_agent_self_commit_in_a_repo_that_tracks_claude_config(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    _track_the_agent_config(repo, tmp_path)
    runner._manual.setup()
    runner._load_tracked_head()
    watermark = repo.rev_parse("HEAD")
    assert runner._tracked_head == watermark

    # The agent commits its OWN work; the tree ends clean apart from the tracked `.claude/` file
    # the snapshot always strips.
    (tmp_path / "a.txt").write_text("one\nagent code\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    agent_head = repo.rev_parse("HEAD")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    runner._process_once()

    head = repo.rev_parse("HEAD")
    assert head != agent_head, "the agent's own commit went uncovered"
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [watermark, agent_head]  # one merge-shaped cover, as in any other repo
    cover = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert cover.count("# aGiTrack Metadata") == 1  # metadata exactly once
    assert "covered_commits:" in cover and agent_head[:7] in cover
    assert repo.rev_parse("HEAD^{tree}") == repo.rev_parse(agent_head + "^{tree}")  # no diff
    assert runner._tracked_head == head


def test_daemon_still_records_latently_while_the_agents_edits_are_uncommitted(tmp_path):
    # The other direction: the strip must not make a genuinely DIRTY tree read as clean, or the
    # daemon would cover a commit while the work it claims sits uncommitted in the tree.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    _track_the_agent_config(repo, tmp_path)
    runner._manual.setup()
    head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nuncommitted agent edit\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    assert runner._process_once() is True

    assert repo.rev_parse("HEAD") == head  # manual mode never moves HEAD
    assert repo.ref_sha(runner._manual.ref()) is not None  # …the turn is on the latent ref


def test_daemon_does_not_cover_a_pure_qa_turn_in_a_scaffolded_repo(tmp_path):
    # A turn that changed nothing must still leave no footprint. Reading the tree as permanently
    # dirty broke this in the other direction from the cover bug: the daemon's manual path
    # recorded a latent turn against a tree identical to HEAD's.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    _track_the_agent_config(repo, tmp_path)
    runner._manual.setup()
    backend.set_session("s1", [_turn("u1", "m1", "what does this do?", "it does x", 20)])

    runner._process_once()

    assert repo.ref_sha(runner._manual.ref()) in (None, repo.rev_parse("HEAD"))
    assert runner._manual.pending_count() == 0


# --- commit guidance in background mode -------------------------------------


def _guidance_runner(tmp_path, *, backend="claude", guidance=True, installed=None):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend=backend)
    gc = GlobalConfig(path=tmp_path / "gc.json")
    runner = BackgroundRunner(repo, commit_guidance=guidance, _global_config=gc, _state=state)
    runner.backend = FakeBackend()
    # Which agents this machine has decides which hooks get written, so the test says so instead
    # of inheriting whatever the developer happens to have installed.
    runner._installed_backends = lambda: list(installed if installed is not None else [backend])
    return runner, repo


def test_the_daemon_installs_and_removes_the_commit_guidance_hook(tmp_path):
    """The -b answer to "why did the agent commit that itself?".

    Proxy mode tells the agent aGiTrack is committing for it with
    --append-system-prompt. Background mode never spawns the agent, so the note has to
    reach it another way; Claude Code's SessionStart hook is that way. It is REMOVED on
    teardown, unlike the auto-track hook: with no tracker running, nothing is committing
    for the agent and the note would be false.
    """
    from agitrack.backends import claude_settings

    runner, repo = _guidance_runner(tmp_path)

    runner._install_commit_guidance()
    assert claude_settings.hook_is_installed(repo.repo) is True

    runner._remove_commit_guidance()
    assert claude_settings.hook_is_installed(repo.repo) is False


def test_no_commit_guidance_installs_nothing(tmp_path):
    # The flag means "do not tell the agent what to do about commits" — which mechanism
    # carries the note is an implementation detail it should not have to know about.
    from agitrack.backends import claude_settings

    runner, repo = _guidance_runner(tmp_path, guidance=False)

    runner._install_commit_guidance()

    assert claude_settings.hook_is_installed(repo.repo) is False


def test_a_backend_with_nowhere_to_put_the_note_is_left_alone(tmp_path):
    # OpenCode's only place to add text to a turn is the user's own message parts, so it gets no
    # half-measure — and no stray .claude/ or .opencode/ config for a note it cannot carry.
    # (Codex is no longer in this category: since 0.147 its SessionStart hook carries the note,
    # see test_codex_gets_the_note_through_its_own_hooks_file.)
    runner, repo = _guidance_runner(tmp_path, backend="opencode", installed=["opencode"])
    runner.state.backend = "opencode"

    runner._install_commit_guidance()

    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".opencode").exists()


def test_codex_gets_the_note_through_its_own_hooks_file(tmp_path):
    """The capability that arrived with Codex 0.147 and that background mode never had: its
    SessionStart hook takes the same payload Claude Code's does, so a Codex agent can finally be
    told that aGiTrack is committing for it instead of committing its own work."""
    from agitrack.backends import codex_settings

    runner, repo = _guidance_runner(tmp_path, backend="codex", installed=["codex"])
    runner.state.backend = "codex"

    runner._install_commit_guidance()

    assert codex_settings.hook_is_installed(tmp_path, codex_settings.SESSION_NOTE_HOOK)
    assert not (tmp_path / ".claude").exists()


def test_the_note_reaches_every_agent_the_machine_can_open(tmp_path):
    """The note is a promise about THIS REPOSITORY — "aGiTrack commits for you here" — and the
    tracker keeps that promise for whichever agent is opened (see
    `test_the_tracker_follows_the_backend_the_user_actually_opened`). Telling only the backend
    that happens to be tracked at install time left the agent the person actually opened
    uninformed, so it committed its own work: the duplicate-commit mess the note exists to
    prevent, in the one situation nobody was watching for."""
    from agitrack.backends import claude_settings, codex_settings

    runner, repo = _guidance_runner(tmp_path, backend="opencode", installed=["opencode", "claude", "codex"])
    runner.state.backend = "opencode"

    runner._install_commit_guidance()

    assert claude_settings.hook_is_installed(repo.repo) is True
    assert codex_settings.hook_is_installed(tmp_path, codex_settings.SESSION_NOTE_HOOK)
    assert not (tmp_path / ".opencode").exists()  # still no half-measure where there is no channel


def test_auto_start_is_armed_for_every_agent_the_machine_can_open(tmp_path):
    # Auto-start has to fire for whichever agent is opened NEXT, which is not necessarily the one
    # opened last. Arming only the tracked backend meant opening a different agent started no
    # tracker at all, and that session ran unrecorded until someone committed by hand.
    from agitrack.backends import agent_hooks

    runner, repo = _guidance_runner(tmp_path, backend="claude", installed=["claude", "codex", "opencode"])

    runner._install_change_autostart_hook()

    assert sorted(agent_hooks.installed_autostart_backends(repo.repo)) == ["claude", "codex", "opencode"]


# --- following the backend the user actually opened -------------------------


def _following_runner(tmp_path, monkeypatch, *, recorded="opencode", stub_hooks=True):
    """A tracker whose RECORDED backend is not the one being driven — the shape of the bug."""
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend=recorded)
    gc = GlobalConfig(path=tmp_path / "gc.json")
    runner = BackgroundRunner(repo, manual_commits=False, _global_config=gc, _state=state)
    runner._make_summarizer = lambda: None
    runner._summarization_enabled = lambda: False
    quiet = FakeBackend(recorded)  # the backend the repo is recorded against: nothing happens here
    driven = FakeBackend("claude")  # the agent the person actually opened
    runner.backend = quiet
    runner._installed_backends = lambda: [recorded, "claude"]
    if stub_hooks:  # hook writing has its own tests; most of these are about what gets tracked
        runner._install_commit_guidance = lambda: None
        runner._install_change_autostart_hook = lambda: None
    monkeypatch.setattr(background_module, "make_proxy_agent", lambda name: driven if name == "claude" else quiet)
    return runner, repo, state, quiet, driven


def test_the_tracker_follows_the_backend_the_user_actually_opened(tmp_path, monkeypatch):
    """THE BUG, and the reason this whole mechanism exists.

    A background tracker observes work it did not start, so the backend recorded for a repo is
    only ever a GUESS about which agent the person will open — seeded, the first time anything
    tracked here, from the GLOBAL default. Measured live: a repo recorded against OpenCode
    (the global default) while the entire session ran on Claude Code. The tracker polled
    OpenCode's store for 90 minutes, found nothing because there was nothing there, recorded no
    turn, and the pre-commit flush had nothing to fold — so the user's own commit carried no
    trace, no tokens and no model, and ~148k output tokens of work were tracked nowhere. The
    person is free to choose their agent per session; the tracker follows them."""
    runner, repo, state, _quiet, driven = _following_runner(tmp_path, monkeypatch)
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    driven.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    driven.activity = 1_000.0  # the only backend with any activity in this repo

    assert runner._process_once() is True

    assert state.backend == "claude"  # ...and it stays there for the turns that follow
    assert "backend: claude" in _git(repo, "log", "-1", "--format=%B", runner._manual.ref())


def test_a_backend_used_once_last_month_never_steals_the_live_session(tmp_path, monkeypatch):
    # The comparison has to be between CONVERSATION TIMES, not "does this backend have a session
    # here at all". A repo where Claude was opened once, long ago, must not pull the tracker off
    # the session running right now on the backend it is already following.
    runner, repo, state, quiet, driven = _following_runner(tmp_path, monkeypatch)
    quiet.activity = 2_000.0  # the live session
    driven.activity = 1_000.0  # last month
    quiet.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")

    assert runner._process_once() is True

    assert state.backend == "opencode"


def test_a_turn_in_flight_is_never_abandoned_for_another_backend(tmp_path, monkeypatch):
    # A turn in flight is one this tracker still owes a commit. Going to look at another store
    # mid-turn would drop its trace and tokens on the floor, so the switch waits.
    runner, _repo, state, _quiet, driven = _following_runner(tmp_path, monkeypatch)
    driven.activity = 5_000.0
    runner._in_flight = {"backend": "opencode", "prompt": "still working"}

    runner._follow_the_driven_backend()

    assert state.backend == "opencode"


def test_switching_backends_keeps_the_old_conversation_to_come_back_to(tmp_path, monkeypatch):
    # Switching is not forgetting: the person moving back to their other agent should land on the
    # conversation they left, not on whatever happens to be newest in that store.
    runner, _repo, state, _quiet, driven = _following_runner(tmp_path, monkeypatch)
    state.backend_session_id = "opencode-chat"
    driven.activity = 5_000.0

    runner._follow_the_driven_backend()

    assert state.backend == "claude"
    assert state.stored_backend_session("opencode") == "opencode-chat"


def test_a_backend_whose_store_cannot_be_read_never_takes_over(tmp_path, monkeypatch):
    # A probe that raises means "no answer", not "now". An unreadable or half-migrated store must
    # never be able to pull the tracker off the backend that is actually working.
    runner, _repo, state, _quiet, driven = _following_runner(tmp_path, monkeypatch)

    def boom(_repo):
        raise OSError("store is unreadable")

    driven.repo_activity = boom

    runner._follow_the_driven_backend()

    assert state.backend == "opencode"


def test_the_commit_hooks_own_tracker_follows_without_arming_anything(tmp_path, monkeypatch):
    """`precommit_sync` builds a runner of its own during `git commit`, and it follows the driven
    backend too — that is what puts the session's trace into a commit made while no daemon is
    running. But it is a hook, not a daemon: publishing a handshake would advertise a tracker
    whose pid dies with the commit, and installing the commit-guidance note would promise "aGiTrack
    commits for you" in a repo where nothing is about to run and then never take it back."""
    runner, repo, state, _quiet, driven = _following_runner(tmp_path, monkeypatch, stub_hooks=False)
    driven.activity = 5_000.0

    runner._follow_the_driven_backend()

    assert state.backend == "claude"  # it still tracks the right conversation
    assert not background_handshake_path(repo).exists()
    assert not (tmp_path / ".claude").exists()


# --- auto-start on new changes ----------------------------------------------


def test_a_finished_turn_that_changed_code_starts_the_tracker(tmp_path, monkeypatch):
    """Auto-start used to be reachable only through the git pre-commit hook, so tracking
    resumed when the user COMMITTED — and an agent that edits for an hour without committing
    is exactly the work aGiTrack exists to record. No git hook can see a plain file edit
    (they fire on git operations, not on writes), so the trigger is the agent's own turn-end
    hook: a turn finished and the tree is not what HEAD says it is.
    """
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    (tmp_path / "agent-wrote-this.txt").write_text("work\n", encoding="utf-8")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    assert background_module.autostart_on_change(repo) == 0

    assert spawned and "--backend" in spawned[0]


def test_auto_start_arms_on_the_recorded_backend_even_when_its_binary_is_not_found(tmp_path, monkeypatch):
    """A hook that fires BECAUSE an agent session opened must not then conclude there is no agent.

    Auto-start briefly required the recorded backend to pass an install probe, and every machine
    where that probe answers "no" — CI runners, a backend behind a wrapper or on a PATH the
    search does not know — stopped auto-starting entirely: two of this suite's own auto-start
    tests went red the moment they ran somewhere without the CLIs. The recorded backend is taken
    as recorded; the daemon it spawns is the thing that checks what it can actually run, and
    moves itself onto another installed agent if it must."""
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    (tmp_path / "agent-wrote-this.txt").write_text("work\n", encoding="utf-8")
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: False)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    background_module.autostart_on_change(repo)

    assert spawned and spawned[0][-2:] == ["--backend", "claude"]


def test_auto_start_picks_an_installed_agent_when_the_repo_has_nothing_recorded(tmp_path, monkeypatch):
    # The hole on the other side: a repo whose FIRST agent session is the one firing this hook has
    # no recorded backend at all, and that used to mean no tracker — the session ran unrecorded
    # until someone committed. Any agent the machine has is a fine starting point, because the
    # tracker follows whichever one is actually driven.
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)  # no state.json: nothing has ever been recorded here
    (tmp_path / "agent-wrote-this.txt").write_text("work\n", encoding="utf-8")
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: name == "codex")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    background_module.autostart_on_change(repo)

    assert spawned and spawned[0][-2:] == ["--backend", "codex"]


def test_nothing_starts_where_there_is_no_record_and_no_agent_at_all(tmp_path, monkeypatch):
    # With nothing recorded and no coding agent on the machine, there is nothing a tracker could
    # follow; spawning one would only add a daemon that exits with "not installed".
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    (tmp_path / "agent-wrote-this.txt").write_text("work\n", encoding="utf-8")
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: False)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    background_module.autostart_on_change(repo)

    assert spawned == []


def test_a_turn_that_changed_nothing_starts_nothing(tmp_path, monkeypatch):
    # A question-and-answer turn ends like any other. Starting a daemon for it would mean a
    # tracker (and its commit hooks) appearing because someone asked what a function does.
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    background_module.autostart_on_change(repo)

    assert spawned == []


def test_auto_start_never_creates_a_second_writer(tmp_path, monkeypatch):
    """The rule the whole design rests on: one git-editing aGiTrack per repo.

    This hook fires on every turn end, including turns of a session an interactive aGiTrack is
    already driving. That process holds the repo's single-writer lock, so a daemon started
    beside it would be a second writer committing into the same branch.
    """
    from agitrack.git import RepoLock
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    (tmp_path / "agent-wrote-this.txt").write_text("work\n", encoding="utf-8")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )
    holder = RepoLock(tmp_path / ".agitrack" / "lock")
    assert holder.acquire()
    try:
        background_module.autostart_on_change(repo)
    finally:
        holder.release()

    assert spawned == []


def test_auto_start_respects_the_opt_out(tmp_path, monkeypatch):
    from agitrack.config import GlobalConfig
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    (tmp_path / "agent-wrote-this.txt").write_text("work\n", encoding="utf-8")
    config = GlobalConfig()
    config.load_repo_overlay(tmp_path)
    config.set("autotrack_hook", "off", scope="repo")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    background_module.autostart_on_change(repo)

    assert spawned == []


def test_stop_disarms_every_way_tracking_could_restart(tmp_path):
    """`-b stop` means stop.

    Two things would otherwise keep acting after the user asked aGiTrack to stop: the
    auto-start hooks would bring the tracker back on the next commit or the next agent turn,
    and the session note would keep telling the agent that aGiTrack is committing for it —
    false with no tracker running, and it stops the agent committing at all.
    """
    from agitrack.backends import claude_settings
    from agitrack.git import hooks as git_hooks
    from agitrack.proc import agitrack_invocation
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    git_hooks.install_autotrack_precommit_hook(
        repo.hooks_dir(), invoke=agitrack_invocation(), repo_root=str(repo.repo), version="9.9.9"
    )
    claude_settings.install_autostart_hook(repo.repo)
    claude_settings.install_commit_guidance_hook(repo.repo)

    assert background_module.stop_background(repo) == 0

    assert not git_hooks.is_autotrack_hook(repo.hooks_dir() / "pre-commit")
    assert claude_settings.hook_is_installed(repo.repo, claude_settings.AUTOSTART_HOOK) is False
    assert claude_settings.hook_is_installed(repo.repo, claude_settings.SESSION_NOTE_HOOK) is False


def test_stop_does_not_revoke_the_users_standing_auto_start_choice(tmp_path):
    # Disarming now is not the same as opting out forever: the next `agitrack -b` re-arms it.
    # Only `--remove-hooks` turns the preference itself off.
    from agitrack.config import GlobalConfig
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)

    background_module.stop_background(repo)

    config = GlobalConfig()
    config.load_repo_overlay(tmp_path)
    assert config.autotrack_hook == "auto"


def test_autostart_status_is_read_from_the_hooks_not_the_preference(tmp_path, monkeypatch):
    """B7 / C32: `Auto-start: on` was derived from the config VALUE and never from the hook file,
    so it lied in four separate ways, all confirmed live:

    * a virgin repo reported `on` before any hook existed — the first commit after agent work
      was silently lost and no tracker ever started;
    * `-b stop` printed "Auto-start is off for this repo", really did remove the hooks, and `-s`
      a second later still said `on`;
    * with core.hooksPath set aGiTrack installs nothing at all, and `-s` asserted the opposite —
      anyone on Husky / pre-commit / lefthook was told their commits were tracked when nothing
      was, disproved by a real commit;
    * every backend was told "…or when an agent turn leaves changes", which only Claude Code
      does.
    """
    from agitrack.proxy.background import autostart_status_line

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    repo = _init_repo(tmp_path / "virgin")

    line = autostart_status_line(repo)
    assert "not armed" in line
    assert "on (" not in line

    # core.hooksPath: aGiTrack installs nothing and must say so, naming the setting.
    other = _init_repo(tmp_path / "hookspath")
    subprocess.run(["git", "-C", str(other.repo), "config", "core.hooksPath", "myhooks"], check=True)
    line = autostart_status_line(other)
    assert "core.hooksPath" in line
    assert "NOT tracked" in line


def test_autostart_status_says_on_once_the_hook_is_really_installed(tmp_path, monkeypatch):
    from agitrack import __version__
    from agitrack.git import hooks as git_hooks
    from agitrack.proxy.background import autostart_status_line

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    repo = _init_repo(tmp_path / "armed")
    git_hooks.install_autotrack_precommit_hook(
        repo.hooks_dir(), invoke=["python", "-m", "agitrack"], repo_root=str(repo.repo), version=__version__
    )

    line = autostart_status_line(repo)
    assert line.startswith("Auto-start: on (")
    assert "on a commit" in line
    # No Claude turn-end hook here, so the clause that only Claude Code honours must be absent.
    assert "agent turn leaves changes" not in line
    assert "last-run" not in line  # the placeholder that used to leak onto a fresh repo


def test_autostart_status_reports_the_opt_out(tmp_path, monkeypatch):
    from agitrack.config import GlobalConfig
    from agitrack.proxy.background import autostart_status_line

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    repo = _init_repo(tmp_path / "optout")
    config = GlobalConfig()
    config.load_repo_overlay(repo.repo)
    config.set("autotrack_hook", "off", scope="repo")

    assert autostart_status_line(repo).startswith("Auto-start: off")


def test_status_names_the_repository(tmp_path, monkeypatch, capsys):
    """`-s`, `-b status` and the `-b` banner never named the repo — exactly wrong in a submodule
    or a multi-repo terminal, which is the one place the answer needs disambiguating."""
    from agitrack.proxy.background import background_status, repo_status

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    repo = _init_repo(tmp_path / "named-repo")

    repo_status(repo)
    background_status(repo)

    out = capsys.readouterr().out
    assert out.count("named-repo") >= 2


def test_core_hookspath_is_announced_not_only_debug_logged(tmp_path, monkeypatch, capsys):
    """B7: with core.hooksPath set, aGiTrack installs nothing and the string `core.hooksPath`
    never reached the user — both skip messages were `_debug`-only, invisible even with
    --verbose (those diagnostics go to a log file whose path is never printed). The spec says it
    must SAY it cannot install hooks and continue."""
    from agitrack.proxy.background import BackgroundRunner

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    repo = _init_repo(tmp_path / "husky")
    subprocess.run(["git", "-C", str(repo.repo), "config", "core.hooksPath", "myhooks"], check=True)
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda name: FakeBackend())

    runner = BackgroundRunner(repo, backend="claude")
    capsys.readouterr()  # drop construction output
    runner._install_autotrack_hook()

    out = capsys.readouterr().out
    assert "core.hooksPath" in out
    assert "myhooks" in out
    assert "NOT tracked" in out


# --- what `-b` publishes, and when ------------------------------------------


def test_the_handshake_is_written_only_once_the_hooks_are_armed(tmp_path, monkeypatch):
    """The handshake is what `agitrack -b` waits on before printing "daemon live" and handing
    the shell back, so anything the daemon does AFTER writing it is a race the user can lose.
    Arming came after, and it showed: ~0.6s after a successful `agitrack -b`, `agitrack -s`
    reported "Auto-start: not armed — no aGiTrack hook is installed in this repo yet. Run
    `agitrack` or `agitrack -b` once" — the exact command that had just been run. A script
    doing `agitrack -b run` then `agitrack -b stop` armed nothing at all.

    Measured on Windows, where the launcher returns fastest, but the ordering was wrong on
    every platform."""
    from agitrack.git import hooks as git_hooks

    order: list[str] = []
    monkeypatch.setattr("agitrack.daemons.register", lambda *a, **k: None)

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    real_handshake = runner._write_handshake
    real_hook = runner._install_autotrack_hook

    def _handshake():
        # The hook has to be on disk BEFORE the record that says the daemon is up.
        order.append("handshake")
        assert git_hooks.is_autotrack_hook(repo.hooks_dir() / "pre-commit"), (
            "the handshake went out while the repo was still unarmed"
        )
        real_handshake()

    monkeypatch.setattr(runner, "_write_handshake", _handshake)
    monkeypatch.setattr(runner, "_install_autotrack_hook", lambda: (order.append("hook"), real_hook())[1])
    monkeypatch.setattr(runner, "_loop", lambda: None)
    monkeypatch.setattr(runner, "_teardown", lambda **kw: None)
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)

    runner.run()

    assert order == ["hook", "handshake"]


def test_stop_logs_daemon_stop_to_the_log_file_the_daemon_was_started_with(tmp_path, monkeypatch):
    """`--log-file` is a command-line flag and is never persisted to config, but `-b stop` read
    the log path from config alone — so a tracker started as `agitrack -b --log-file events.log`
    got `daemon-start` and nothing else, ever. It matters most on Windows, where `-b stop` is a
    TerminateProcess and the daemon's own teardown (which emits the event) never runs at all."""
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    log = tmp_path / "events.log"
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": 4242, "mode": "auto commits", "backend": "claude", "log_file": str(log)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(background_module, "_live_background_pid", lambda r: 4242)
    monkeypatch.setattr(background_module, "_terminate_and_wait", lambda pid: True)

    assert background_module.stop_background(repo) == 0

    assert "daemon-stop" in log.read_text(encoding="utf-8")


def test_stop_without_a_log_file_writes_nothing_anywhere(tmp_path, monkeypatch):
    """A tracker with no event log configured must not have one invented for it."""
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 4242, "mode": "auto commits", "backend": "claude"}), encoding="utf-8")
    monkeypatch.setattr(background_module, "_live_background_pid", lambda r: 4242)
    monkeypatch.setattr(background_module, "_terminate_and_wait", lambda pid: True)

    assert background_module.stop_background(repo) == 0
    assert not list(tmp_path.glob("*.log"))


# --- a dead tracker must not stay dead -------------------------------------


def test_a_commit_with_no_ai_work_still_revives_a_dead_tracker(tmp_path, monkeypatch):
    """Auto-start used to sit behind `if not synced: return 0` — "no AI work in this commit, no
    footprint, no nag". That made a stopped tracker PERMANENT: with nothing running, no agent
    turn can be recorded, so `synced` is False on the next commit too, and on every commit after
    it, so the one hook that could bring the tracker back never even looked. Observed in the
    wild — the tracker stopped during a failed restart and none of the commits that followed
    restarted it, because none of them had pending AI work to fold.

    `agitrack -s` has always advertised this as "on a commit", unqualified. Now it is."""
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()  # no session ⇒ no pending AI work, the whole point
    _precommit_env(tmp_path, monkeypatch, backend, autostart=True)
    spawned: list = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args))
    (tmp_path / "a.txt").write_text("one\njust me\n", encoding="utf-8")  # a purely human commit

    assert bg.precommit_sync(repo) == 0

    assert spawned, "a commit made with no tracker running must start one"


def test_a_commit_does_not_start_a_second_tracker(tmp_path, monkeypatch):
    """The other half: reviving a DEAD tracker must not mean racing a LIVE one."""
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    _precommit_env(tmp_path, monkeypatch, backend, autostart=True)
    monkeypatch.setattr(bg, "_live_background_pid", lambda r: 4242)  # one is already running
    spawned: list = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args))
    (tmp_path / "a.txt").write_text("one\njust me\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0

    assert spawned == []


# --- the restart handoff: a successor must win the lock, and a loss must be loud ---------


def test_a_serve_child_waits_for_the_lock_instead_of_declaring_success(tmp_path, monkeypatch, capsys):
    """A `--background-serve` child is a DAEMON TAKING OVER, not a second writer asking
    permission. Its predecessor may still be dying with the repo lock held — on Windows a
    byte-range lock outlives the process while the kernel tears its handles down — and the
    child's first `acquire()` had no retry.

    So it lost the race, took the LAUNCHER's branch ("already running (PID N, current version)
    — left in place"), and exited **0**. The predecessor then exited too. Nothing tracked the
    repo, and the exit code said everything was fine. Reproduced live before this fix."""
    from agitrack import cli
    from agitrack.git import RepoLock

    repo = _init_repo(tmp_path)
    monkeypatch.setattr(cli, "backend_installed", lambda name: True)
    holder = RepoLock(repo.repo / ".agitrack" / "lock")
    assert holder.acquire() is True
    try:
        code = cli.main(
            ["--repo", str(tmp_path), "--background", "--background-serve", "--skip-privacy-ack", "--backend", "claude"]
        )
    finally:
        holder.release()

    out = capsys.readouterr().out
    assert code == 1, "a serve child that cannot take the lock must FAIL, not report success"
    assert "could not take this repo's single-writer lock" in out
    assert "left in place" not in out  # never the launcher's message


def test_a_failed_serve_start_records_a_traceback(tmp_path, monkeypatch, capsys):
    """A daemon has no terminal — its stdout IS .agitrack/background.log. One that died on the
    way up left that log holding a bare "aGiTrack is starting..." and nothing else: no reason, no
    traceback, no exit code, which is why the original failure could not be diagnosed after the
    fact."""
    from agitrack import cli
    from agitrack.proxy import background as bg

    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "backend_installed", lambda name: True)

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("the new code does not import")

    monkeypatch.setattr(cli, "BackgroundRunner", _Boom)

    code = cli.main(
        ["--repo", str(tmp_path), "--background", "--background-serve", "--skip-privacy-ack", "--backend", "claude"]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED to start" in out
    assert "the new code does not import" in out
    assert "Traceback (most recent call last)" in out  # the actual stack, not just the message
    assert "cli.py" in out and "test_background.py" in out  # ...naming both frames
    assert bg is not None


def test_a_daemon_is_registered_before_it_finishes_starting(tmp_path, monkeypatch):
    """The registry is what `--daemons`, a self-update restart, and the test suite's own sweep
    all read. It used to be written by `_write_handshake`, which runs LAST — so for the whole of
    startup (hooks, git work: seconds) the process existed and nothing could see it. Any sweep in
    that window missed it, and the daemon outlived the run.

    Findability and readiness are different claims: the registry says "this process exists, here
    is how to stop it" and must be true immediately; the handshake says "up and armed" and must
    not be published early."""
    from agitrack.git import hooks as git_hooks

    seen: list[str] = []
    monkeypatch.setattr("agitrack.daemons.register", lambda kind, path, **kw: seen.append(f"register:{kind}"))
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    monkeypatch.setattr(runner, "_loop", lambda: None)
    monkeypatch.setattr(runner, "_teardown", lambda **kw: None)
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)
    real_hook = runner._install_autotrack_hook
    monkeypatch.setattr(runner, "_install_autotrack_hook", lambda: (seen.append("hooks"), real_hook())[1])

    runner.run()

    assert seen[0] == "register:background", f"registered too late: {seen}"
    assert "hooks" in seen  # ...and the slow startup work really did come after it
    assert git_hooks.is_autotrack_hook(repo.hooks_dir() / "pre-commit")
