"""Focused unit tests for agit.proxy.CommitEngine (#29, P4).

These tests construct CommitEngine directly — no ProxyRunner.__new__ required —
verifying the core commit pipeline, parse-result consumption, parse-worker
launch, and the simple state helpers.
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from agit.backends.base import TokenUsage
from agit.transcripts.opencode import SessionTurn
from agit.proxy.commit_engine import CommitEngine
from agit.proxy.session import Session
from agit.transcripts import ExportedSession
from agit.config import AgitState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Repo:
    """Minimal GitRepo stand-in."""

    def __init__(self, *, staged: bool = True):
        self._staged = staged
        self.message: str | None = None
        self.staged_paths: list[str] = []
        self.untracked: list[str] = []

    def add_tracked(self) -> None:
        pass

    def has_staged_changes(self) -> bool:
        return self._staged

    def commit(self, message: str) -> str:
        self.message = message
        return "dead1234"

    def untracked_files(self) -> list[str]:
        return list(self.untracked)

    def stage_paths(self, paths: list[str]) -> None:
        self.staged_paths.extend(paths)


def _noop_stage(repo, state) -> None:
    """No-op stage_untracked_fn for tests that don't care about untracked."""


def _engine(tmp_path, *, staged: bool = True) -> tuple[CommitEngine, _Repo, AgitState]:
    repo = _Repo(staged=staged)
    state = AgitState(tmp_path)
    engine = CommitEngine(repo, state)
    return engine, repo, state


def _turn(prompt: str, response: str, *, total: int = 1, output: int = 1) -> SessionTurn:
    return SessionTurn(
        "uid", "aid", prompt, response,
        TokenUsage(total=total, output=output, input=total - output),
        None,
        complete=True,
    )


# ---------------------------------------------------------------------------
# CommitEngine.commit_turns — proxy mode (accumulate_trace_only_on_commit=False)
# ---------------------------------------------------------------------------

def test_commit_turns_returns_false_for_empty_turns(tmp_path):
    engine, repo, state = _engine(tmp_path)
    assert engine.commit_turns(
        turns=[],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    ) is False
    assert repo.message is None


def test_commit_turns_returns_false_when_nothing_staged(tmp_path):
    engine, repo, state = _engine(tmp_path, staged=False)
    assert engine.commit_turns(
        turns=[_turn("fix it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    ) is False


def test_commit_turns_creates_commit_when_staged(tmp_path):
    engine, repo, state = _engine(tmp_path)
    assert engine.commit_turns(
        turns=[_turn("fix the bug", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    ) is True
    assert repo.message is not None
    assert repo.message.startswith("<agent> fix the bug")


def test_commit_turns_token_not_counted_on_failed_attempt(tmp_path):
    engine, repo, state = _engine(tmp_path, staged=False)
    engine.commit_turns(
        turns=[_turn("fix it", "done", total=100, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert state.pending_token_usage()["input"] == 0


def test_commit_turns_calls_pre_commit_fn(tmp_path):
    engine, repo, state = _engine(tmp_path)
    called = []
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        pre_commit_fn=lambda: called.append(True),
    )
    assert called == [True]


def test_commit_turns_calls_on_commit_fn_with_sha(tmp_path):
    engine, repo, state = _engine(tmp_path)
    received = []
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        on_commit_fn=lambda sha: received.append(sha),
    )
    assert received == ["dead1234"]


def test_commit_turns_subject_joins_multiple_prompts(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[
            _turn("add parser", "done1"),
            _turn("add tests", "done2"),
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    subject = repo.message.splitlines()[0]
    assert subject == "<agent> add parser / add tests"


def test_commit_turns_pending_trace_cleared_after_commit(tmp_path):
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "prior prompt")
    engine.commit_turns(
        turns=[_turn("new prompt", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert state.pending_trace() == []


def test_commit_turns_preserves_pending_users_not_in_turns(tmp_path):
    # A user prompt recorded in state that never appears as a turn's user_prompt
    # (e.g. from an earlier incomplete parse) is carried into the commit.
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "initial orphan prompt")
    engine.commit_turns(
        turns=[_turn("second prompt", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert "initial orphan prompt" in repo.message
    assert "second prompt" in repo.message


def test_commit_turns_stage_untracked_fn_receives_repo_and_state(tmp_path):
    engine, repo, state = _engine(tmp_path)
    calls = []
    def capture_stage(r, s):
        calls.append((r, s))
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=capture_stage,
    )
    assert calls == [(repo, state)]


def test_commit_turns_auto_stages_untracked_excluding_declined(tmp_path):
    # When stage_untracked_fn does the auto-staging (non-interactive / proxy
    # exit), it must skip files the user intentionally declined.
    engine, repo, state = _engine(tmp_path)
    repo.untracked = ["new_file.py", "ignored.log"]
    state.add_declined(["ignored.log"])

    def stage_untracked_fn(r, s):
        declined = set(s.declined_untracked())
        r.stage_paths([p for p in r.untracked_files() if p not in declined])

    engine.commit_turns(
        turns=[_turn("add file", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=stage_untracked_fn,
    )
    assert "new_file.py" in repo.staged_paths
    assert "ignored.log" not in repo.staged_paths


# ---------------------------------------------------------------------------
# CommitEngine.commit_turns — actions mode (accumulate_trace_only_on_commit=True)
# ---------------------------------------------------------------------------

def test_commit_turns_actions_mode_no_trace_on_failed_attempt(tmp_path):
    # Bug #14 (d041d10): a failed attempt must leave state pristine.
    engine, repo, state = _engine(tmp_path, staged=False)
    engine.commit_turns(
        turns=[_turn("fix it", "done", total=140, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        accumulate_trace_only_on_commit=True,
    )
    assert state.pending_trace() == []
    assert state.pending_token_usage()["input"] == 0


def test_commit_turns_actions_mode_succeeds_with_clean_state(tmp_path):
    engine, repo, state = _engine(tmp_path)
    result = engine.commit_turns(
        turns=[_turn("fix it", "done", total=50, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        accumulate_trace_only_on_commit=True,
    )
    assert result is True
    assert repo.message is not None
    assert state.pending_trace() == []


# ---------------------------------------------------------------------------
# CommitEngine.record_user_prompt / await_followup
# ---------------------------------------------------------------------------

def test_record_user_prompt_appends_to_trace(tmp_path):
    engine, _, state = _engine(tmp_path)
    engine.record_user_prompt("hello world")
    trace = state.pending_trace()
    assert len(trace) == 1
    assert trace[0]["role"] == "user"
    assert trace[0]["content"] == "hello world"


def test_record_user_prompt_noop_on_empty(tmp_path):
    engine, _, state = _engine(tmp_path)
    engine.record_user_prompt("")
    assert state.pending_trace() == []


def test_await_followup_appends_normalized(tmp_path):
    engine, _, _ = _engine(tmp_path)
    result = engine.await_followup("  fix   the bug  ", [])
    assert result == ["fix the bug"]


def test_await_followup_skips_slash_commands(tmp_path):
    engine, _, _ = _engine(tmp_path)
    result = engine.await_followup("/compact", [])
    assert result == []


def test_await_followup_skips_empty(tmp_path):
    engine, _, _ = _engine(tmp_path)
    assert engine.await_followup("", []) == []


# ---------------------------------------------------------------------------
# CommitEngine.sanitize_state_trace
# ---------------------------------------------------------------------------

def test_sanitize_state_trace_removes_event_blobs(tmp_path):
    engine, _, state = _engine(tmp_path)
    blob = '{"type":"event","payload":"..."}'

    class FakeBackend:
        def is_event_blob(self, content):
            return content.startswith('{"type":"event"')

    state.append_trace("user", "my prompt")
    state.append_trace("agent", blob)
    state.append_trace("agent", "a normal response")

    engine.sanitize_state_trace(FakeBackend())

    items = state.pending_trace()
    assert len(items) == 2
    assert all(item["content"] != blob for item in items)


def test_sanitize_state_trace_noop_when_clean(tmp_path):
    engine, _, state = _engine(tmp_path)

    class FakeBackend:
        def is_event_blob(self, content):
            return False

    state.append_trace("user", "hello")
    state.append_trace("agent", "world")
    engine.sanitize_state_trace(FakeBackend())
    assert len(state.pending_trace()) == 2


# ---------------------------------------------------------------------------
# CommitEngine.start_parse / parse worker
# ---------------------------------------------------------------------------

def test_start_parse_sets_parse_thread_on_session(tmp_path):
    state = AgitState(tmp_path)
    session = Session.bare()
    session.state = state
    session.worktree = None
    session.backend = types.SimpleNamespace(
        name="claude",
        latest_session_id=lambda repo: None,
        export_session=lambda repo, sid: None,
    )
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    started = engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    )
    assert started is True
    assert session.agent_parse_thread is not None
    session.agent_parse_thread.join(timeout=5)


def test_start_parse_returns_false_when_already_running(tmp_path):
    state = AgitState(tmp_path)
    session = Session.bare()
    session.state = state
    session.worktree = None

    ready = threading.Event()

    class SlowBackend:
        name = "claude"
        def latest_session_id(self, repo): return None
        def export_session(self, repo, sid):
            ready.wait(timeout=5)
            return None

    session.backend = SlowBackend()
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    assert engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    ) is True
    # Second launch while thread is alive
    assert engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    ) is False
    ready.set()
    session.agent_parse_thread.join(timeout=5)


def test_start_parse_writes_result_to_owning_session(tmp_path):
    state = AgitState(tmp_path)
    session = Session.bare()
    session.state = state
    # Use a worktree so the worker calls backend.latest_session_id to find the id.
    session.worktree = object()

    exported = ExportedSession("ses-1", "m", None, [])
    class Backend:
        name = "claude"
        def latest_session_id(self, repo): return "ses-1"
        def export_session(self, repo, sid): return exported

    session.backend = Backend()
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    )
    session.agent_parse_thread.join(timeout=5)
    assert session.agent_parse_result is not None
    sid, exp, last_id, owner_state = session.agent_parse_result
    assert sid == "ses-1"
    assert exp is exported
    assert owner_state is state


# ---------------------------------------------------------------------------
# CommitEngine.finish_parse_if_ready
# ---------------------------------------------------------------------------

def _make_finish_helpers(tmp_path, session, exported_session, *, last_message_id=None):
    """Set up a CommitEngine and helper stubs for finish_parse_if_ready tests."""
    state = AgitState(tmp_path)
    session.state = state
    session.backend = types.SimpleNamespace(name="claude")
    session.agent_parse_result = (
        exported_session.session_id, exported_session, last_message_id, state
    )
    session.agent_parse_thread = None

    repo = _Repo(staged=True)
    engine = CommitEngine(repo, state)

    commits = []

    def commit_fn(**kwargs):
        commits.append(kwargs)
        return True

    return engine, state, commits, commit_fn


def test_finish_parse_returns_none_when_no_result(tmp_path):
    state = AgitState(tmp_path)
    session = Session.bare()
    session.state = state
    session.agent_parse_thread = None
    session.agent_parse_result = None

    engine = CommitEngine(None, state)
    result, awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=lambda **k: True,
    )
    assert result is None


def test_finish_parse_defers_incomplete_turn(tmp_path):
    session = Session.bare()
    exported = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "a1", "do it", "partial", TokenUsage(total=1, output=1), None, complete=False),
    ])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: True,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []


def test_finish_parse_commits_complete_turn(tmp_path):
    session = Session.bare()
    # SessionTurn(user_message_id, assistant_message_id, user_prompt, final_response,
    #             tokens, model, complete)
    exported = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "msg-1", "fix it", "done", TokenUsage(total=1, output=1), None, complete=True),
    ])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert len(commits) == 1
    # Watermark advanced to the assistant_message_id of the last complete turn
    assert state.last_backend_message_id == "msg-1"


def test_finish_parse_defers_awaited_followup_while_active(tmp_path):
    session = Session.bare()
    exported = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "a1", "prompt one", "done", TokenUsage(total=1, output=1), None, complete=True),
    ])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, new_awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=["queued follow-up"],
        agent_is_active_fn=lambda: True,  # agent still running
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []
    assert "queued follow-up" in new_awaited


def test_finish_parse_clears_awaited_on_interrupt(tmp_path):
    # An Esc interrupt clears the awaited list so commits are not deferred forever.
    session = Session.bare()
    exported = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "a1", "fix", "partial", TokenUsage(total=1, output=1), None,
                    complete=True, interrupted=True),
    ])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, new_awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=["a prompt that was discarded"],
        agent_is_active_fn=lambda: True,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert new_awaited == []


def test_finish_parse_discards_stale_session_result(tmp_path):
    # A result whose owner_state differs from self.state is silently discarded.
    state_a = AgitState(tmp_path / "a")
    state_b = AgitState(tmp_path / "b")

    session = Session.bare()
    session.state = state_a
    # Result is tagged with state_b (a stale worker from before a session switch)
    session.agent_parse_result = ("ses-b", None, None, state_b)
    session.agent_parse_thread = None

    engine = CommitEngine(None, state_a)
    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=lambda **k: True,
    )
    assert result is None  # discarded, not committed


# ---------------------------------------------------------------------------
# CommitEngine.recover_nonempty_session / initialize_session_baseline
# ---------------------------------------------------------------------------

def test_recover_nonempty_session_returns_none_on_exception(tmp_path):
    state = AgitState(tmp_path)
    engine = CommitEngine(None, state)

    class FailBackend:
        def latest_session_id(self, repo): raise RuntimeError("network error")

    result = engine.recover_nonempty_session(
        FailBackend(),
        types.SimpleNamespace(repo=tmp_path),
        lambda sid: None,
    )
    assert result is None


def test_recover_nonempty_session_returns_none_when_same_id(tmp_path):
    state = AgitState(tmp_path)
    state.backend_session_id = "ses-1"
    engine = CommitEngine(None, state)

    class SameBackend:
        def latest_session_id(self, repo): return "ses-1"

    result = engine.recover_nonempty_session(
        SameBackend(),
        types.SimpleNamespace(repo=tmp_path),
        lambda sid: None,
    )
    assert result is None


def test_initialize_session_baseline_clears_when_not_continue(tmp_path):
    state = AgitState(tmp_path)
    state.backend_session_id = "old-ses"
    state.last_backend_message_id = "old-msg"
    engine = CommitEngine(None, state)

    engine.initialize_session_baseline(
        None,
        None,
        should_continue_fn=lambda: False,
        stage_backend_resume_fn=lambda sid: None,
    )
    assert state.backend_session_id is None
    assert state.last_backend_message_id is None
