"""Backend-made commits get aGiT's trace/metadata attached (issue #35).

Some backends commit on their own (a Claude Code hook, or an agent told to run
`git commit`). Those commits leave the worktree clean, so aGiT's normal
stage-and-commit path never runs and the turn's provenance was lost. Option A
(decided on the issue): amend the backend's latest commit with the interaction
trace and metadata; the metadata's ``covered_commits`` line records the
pre-amend hashes of every commit it accounts for.
"""

from pathlib import Path
from types import SimpleNamespace

from agit.backends.base import TokenUsage
from agit.commits import build_agent_commit_message, build_backend_amend_message
from agit.config import AgitState
from agit.git import GitRepo
from agit.proxy.commit_engine import CommitEngine
from agit.transcripts.types import SessionTurn

from proxy_helpers import make_runner

AGIT_BODY = "internal change\n\n# aGiT Metadata\ncommit_type: agent\n"


def _turn(prompt: str = "add the feature", response: str = "done") -> SessionTurn:
    return SessionTurn(
        user_message_id="u1",
        assistant_message_id="a1",
        user_prompt=prompt,
        final_response=response,
        tokens=TokenUsage(input=10, output=5),
        model="m1",
        complete=True,
        interrupted=False,
    )


def _repo_on_turn_branch(tmp_path: Path) -> tuple[GitRepo, str]:
    repo = GitRepo.init(tmp_path)
    base = repo.current_branch()
    repo.switch("agit/test/s1/t1", create=True)
    return repo, base


def _backend_commit(repo: GitRepo, name: str, message: str) -> str:
    (repo.repo / name).write_text(f"{name} content\n", encoding="utf-8")
    repo.stage_paths([name])
    repo.commit(message)
    return repo.rev_parse("HEAD")


def _commit_turns(repo: GitRepo, state: AgitState, backend_commits: list[str], **kwargs) -> bool:
    return CommitEngine(repo, state).commit_turns(
        turns=[_turn()],
        backend="claude",
        backend_session_id="ses-1",
        model="m1",
        stage_untracked_fn=lambda repo, state: None,
        session_name="s1",
        backend_commits=backend_commits,
        **kwargs,
    )


# --- message builders --------------------------------------------------------


def test_amend_message_keeps_original_and_appends_trace_and_metadata():
    message = build_backend_amend_message(
        original_message="fix the parser\n\nDetails the agent wrote itself.",
        trace=[{"role": "user", "content": "fix it"}, {"role": "agent", "content": "fixed"}],
        backend="claude",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="m1",
        session_name="s1",
        covered_commits=["abc123", "def456"],
    )
    assert message.startswith("<aGiT> fix the parser\n")
    assert "Details the agent wrote itself." in message
    assert "# Interaction Trace" in message
    assert "## User" in message and "fix it" in message
    assert "# aGiT Metadata" in message
    assert "commit_type: agent" in message
    assert "covered_commits: abc123 def456" in message


def test_amend_message_does_not_double_prefix_subject():
    message = build_backend_amend_message(
        original_message="<aGiT> already prefixed",
        trace=[],
        backend="claude",
        backend_session_id=None,
        agit_session_id="agit-1",
        model=None,
        covered_commits=["abc123"],
    )
    assert message.startswith("<aGiT> already prefixed\n")
    assert "<aGiT> <aGiT>" not in message


def test_amend_message_tag_can_be_disabled():
    # The tag_backend_commits config option (README: default true) removes the
    # <aGiT> subject tag while keeping the trace/metadata in the body.
    message = build_backend_amend_message(
        original_message="fix the parser",
        trace=[{"role": "user", "content": "fix it"}],
        backend="claude",
        backend_session_id=None,
        agit_session_id="agit-1",
        model=None,
        covered_commits=["abc123"],
        tag=False,
    )
    assert message.startswith("fix the parser\n")
    assert "<aGiT>" not in message
    assert "# aGiT Metadata" in message
    assert "covered_commits: abc123" in message


def test_agent_commit_message_covered_commits_line():
    kwargs = dict(
        latest_prompt="do things",
        trace=[],
        backend="claude",
        backend_session_id=None,
        agit_session_id="agit-1",
        model=None,
    )
    with_covers = build_agent_commit_message(**kwargs, covered_commits=["abc123"])
    without = build_agent_commit_message(**kwargs)
    assert "covered_commits: abc123" in with_covers
    assert "covered_commits" not in without


# --- commit_turns amend path -------------------------------------------------


def test_clean_tree_amends_latest_backend_commit(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")

    committed = _commit_turns(repo, state, [first, last])

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert head_message.startswith("<aGiT> backend commit two")
    assert "# Interaction Trace" in head_message
    assert "add the feature" in head_message
    # Covered hashes are recorded in SHORT form, the amended commit included.
    assert f"covered_commits: {repo.short_sha(first)} {repo.short_sha(last)}" in head_message
    assert repo.rev_parse("HEAD") != last  # amend rewrote the hash
    # The earlier backend commit is untouched (only HEAD may be amended).
    assert repo.commit_message("HEAD^").startswith("backend commit one")
    assert state.pending_trace() == []  # trace consumed by the amend


def test_clean_tree_without_backend_commits_does_not_commit(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    head = repo.rev_parse("HEAD")

    assert _commit_turns(repo, state, []) is False
    assert repo.rev_parse("HEAD") == head


def test_amend_refused_when_head_is_not_the_latest_backend_commit(tmp_path):
    # An aGiT commit sits on top of the backend's: amending would rewrite
    # aGiT's own commit, so the engine must refuse.
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    _backend_commit(repo, "b.txt", AGIT_BODY)
    head = repo.rev_parse("HEAD")

    assert _commit_turns(repo, state, [backend_sha]) is False
    assert repo.rev_parse("HEAD") == head
    assert repo.commit_message("HEAD").startswith("internal change")


def test_staged_changes_commit_lists_backend_commits_as_covered(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    (repo.repo / "a.txt").write_text("further uncommitted work\n", encoding="utf-8")

    committed = _commit_turns(repo, state, [backend_sha])

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert head_message.startswith("<aGiT> add the feature")
    assert f"covered_commits: {repo.short_sha(backend_sha)}" in head_message
    # The backend's own commit is preserved below, message intact.
    assert repo.rev_parse("HEAD^") == backend_sha
    assert repo.commit_message("HEAD^").startswith("backend commit")


def test_commit_turns_honors_tag_backend_commits_off(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    sha = _backend_commit(repo, "a.txt", "backend commit")

    committed = _commit_turns(repo, state, [sha], tag_backend_commits=False)

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert head_message.startswith("backend commit\n")
    assert "<aGiT>" not in head_message
    assert f"covered_commits: {repo.short_sha(sha)}" in head_message  # metadata still attached


def test_summary_amend_preserves_agit_tag():
    # When a summary is later amended into an amended backend commit, the
    # subject keeps its <aGiT> tag instead of being relabeled <aGiT>.
    from agit.commits import apply_summary_to_message

    message = build_backend_amend_message(
        original_message="fix the parser",
        trace=[{"role": "user", "content": "fix it"}],
        backend="claude",
        backend_session_id=None,
        agit_session_id="agit-1",
        model=None,
        covered_commits=["abc123"],
    )
    summarized = apply_summary_to_message(message, "Rework the parser error paths")
    assert summarized.startswith("<aGiT> Rework the parser error paths\n")
    # The original subject is preserved under # Prompts, without its tag.
    assert "fix the parser" in summarized.split("# Prompts")[1]


def test_amend_in_actions_mode_accumulates_trace_first(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    sha = _backend_commit(repo, "a.txt", "backend commit")

    committed = _commit_turns(repo, state, [sha], accumulate_trace_only_on_commit=True)

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert "add the feature" in head_message
    assert f"covered_commits: {repo.short_sha(sha)}" in head_message


# --- runner detection and attach flow ----------------------------------------


def _detection_runner(tmp_path):
    repo = GitRepo.init(tmp_path)
    base = repo.current_branch()
    repo.switch("agit/test/s1/t1", create=True)
    runner = make_runner(
        repo=repo,
        state=AgitState(tmp_path),
        worktree=object(),
        _base_branch=base,
    )
    return runner, repo


def test_uncovered_backend_commits_detects_only_unmarked_commits(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    plain = _backend_commit(repo, "a.txt", "backend's own commit")
    _backend_commit(repo, "b.txt", AGIT_BODY)  # has metadata: covered

    assert runner._uncovered_backend_commits() == [plain]


def test_uncovered_backend_commits_empty_off_turn_branch(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "backend's own commit")
    repo.switch(runner._base_branch)

    assert runner._uncovered_backend_commits() == []


def test_uncovered_backend_commits_empty_without_worktree(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "backend's own commit")
    runner.worktree = None  # --no-worktree mode: never amend the user's branch

    assert runner._uncovered_backend_commits() == []


def _attach_runner(uncovered):
    runner = make_runner()
    runner._uncovered_backend_commits = lambda: list(uncovered)
    runner.PARSE_COOLDOWN_SECONDS = 10.0
    runner._attach_uncovered_until = 0.0
    runner.calls = SimpleNamespace(started=0)

    def start_parse():
        runner.calls.started += 1
        return True

    runner._start_agent_parse = start_parse
    return runner


def test_attach_proceeds_immediately_when_nothing_uncovered():
    runner = _attach_runner([])
    assert runner._attach_trace_to_backend_commits(100.0) is True


def test_attach_defers_integration_while_parse_pending():
    runner = _attach_runner(["abc"])
    runner._finish_agent_parse_if_ready = lambda **kw: None
    assert runner._attach_trace_to_backend_commits(100.0) is False
    assert runner.calls.started == 1  # kicked off the parse that will attach


def test_attach_proceeds_after_amend():
    runner = _attach_runner(["abc"])
    runner._finish_agent_parse_if_ready = lambda **kw: True
    assert runner._attach_trace_to_backend_commits(100.0) is True
    assert runner.calls.started == 0


def test_attach_gives_up_after_deadline():
    runner = _attach_runner(["abc"])
    runner._finish_agent_parse_if_ready = lambda **kw: None
    assert runner._attach_trace_to_backend_commits(100.0) is False
    # Well past the 3x parse-cooldown deadline: integrate as-is, don't stall.
    assert runner._attach_trace_to_backend_commits(200.0) is True
