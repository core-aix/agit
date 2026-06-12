"""ProxyRunner._handle_pre_compaction: the /compact capture path.

The Summarizer class itself is covered in test_summarizer.py; these tests
exercise the proxy runner integration around it, which a try/except guards —
a bug there fails silently in production (the path once shipped constructing
the backend without its required repo argument and exporting via a
nonexistent attribute, so the capture never ran). The fake summarizer keeps
the real backend construction and real git notes in the loop so regressions
in either surface as assertion failures, not swallowed exceptions.

Since #8 the summarization runs on a worker thread so the UI never blocks:
_handle_pre_compaction only exports and spawns, and the result is applied by
_service_precompact_summary on the main loop.
"""

from types import SimpleNamespace

from agit.backends.base import TokenUsage
from agit.config import AgitState
from agit.git import GitRepo
from agit.transcripts.types import ExportedSession, SessionTurn

from proxy_helpers import make_runner


class FakeSummarizer:
    """Stands in for agit.summaries.Summarizer; records what it was built with."""

    last = None

    def __init__(self, backend, *, model=None):
        self.backend = backend
        self.model = model
        self.exported_session = None
        self.current_summary = None
        FakeSummarizer.last = self

    def summarize_pre_compaction(self, *, exported_session, current_summary=None):
        self.exported_session = exported_session
        self.current_summary = current_summary
        return "captured design context"


def _turn() -> SessionTurn:
    return SessionTurn(
        user_message_id="u1",
        assistant_message_id="a1",
        user_prompt="build the feature",
        final_response="done",
        tokens=TokenUsage(),
        model="m",
        complete=True,
        interrupted=False,
    )


def _pre_compaction_runner(tmp_path, monkeypatch, *, turns):
    monkeypatch.setattr("agit.summaries.Summarizer", FakeSummarizer)
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path / "agit-config"))
    FakeSummarizer.last = None
    repo = GitRepo.init(tmp_path)
    runner = make_runner(repo=repo, state=AgitState(tmp_path))
    runner.global_config = None
    runner._render = lambda *a, **k: None
    runner.backend = SimpleNamespace(
        export_session=lambda repo_path, session_id: ExportedSession(session_id, None, None, turns)
    )
    return runner, repo


def test_pre_compaction_captures_summary_to_state_and_notes(tmp_path, monkeypatch):
    runner, repo = _pre_compaction_runner(tmp_path, monkeypatch, turns=[_turn()])
    runner.state.backend_session_id = "ses-1"
    runner.state.summarization_model = "cheap-model"
    runner.state.session_summary = "previous narrative"

    runner._handle_pre_compaction()
    # The export+spawn must not touch state synchronously (the LLM call runs
    # on a worker thread; the UI thread stays free).
    assert runner.state.session_summary == "previous narrative"
    assert runner._precompact_thread is not None
    runner._precompact_thread.join(timeout=10)
    runner._service_precompact_summary()

    # The summary landed in state and as a git note on HEAD.
    assert runner.state.session_summary == "captured design context"
    head = repo.rev_parse("HEAD")
    assert runner.state.session_summary_commit == head
    note = repo.notes_show(head, namespace="agit/session-summary")
    assert note is not None and "captured design context" in note
    # Success, not the swallowed-exception failure message.
    assert "failed" not in (runner.message or "").lower()

    summarizer = FakeSummarizer.last
    assert summarizer is not None
    # Regression (#47 merge): the summarization backend must be constructed
    # with a directory argument — backend_class() raised TypeError and the
    # whole capture silently no-opped. Since #56 that directory is the scratch
    # dir, never the session worktree/repo (its headless runs would otherwise
    # be recorded as this repo's newest session and get resumed on restart).
    from agit.summaries import summary_scratch_dir

    assert summarizer.backend.repo == summary_scratch_dir()
    assert summarizer.backend.repo != tmp_path
    assert summarizer.model == "cheap-model"
    # The previous rolling summary is passed along so the narrative evolves.
    assert summarizer.current_summary == "previous narrative"
    assert [t.user_prompt for t in summarizer.exported_session.turns] == ["build the feature"]


def test_pre_compaction_without_tracked_session_is_a_noop(tmp_path, monkeypatch):
    runner, repo = _pre_compaction_runner(tmp_path, monkeypatch, turns=[_turn()])
    runner.state.backend_session_id = None

    runner._handle_pre_compaction()
    if runner._precompact_thread is not None:
        runner._precompact_thread.join(timeout=10)
    runner._service_precompact_summary()

    assert runner.state.session_summary is None
    assert repo.notes_show(repo.rev_parse("HEAD"), namespace="agit/session-summary") is None


def test_pre_compaction_with_empty_session_writes_nothing(tmp_path, monkeypatch):
    runner, repo = _pre_compaction_runner(tmp_path, monkeypatch, turns=[])
    runner.state.backend_session_id = "ses-1"

    runner._handle_pre_compaction()
    if runner._precompact_thread is not None:
        runner._precompact_thread.join(timeout=10)
    runner._service_precompact_summary()

    assert runner.state.session_summary is None
    assert repo.notes_show(repo.rev_parse("HEAD"), namespace="agit/session-summary") is None
