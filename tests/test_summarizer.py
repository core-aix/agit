from unittest.mock import Mock
from agit.summaries import Summarizer
from agit.backends.base import AgentResult, TokenUsage
from agit.transcripts.types import SessionTurn, ExportedSession


def test_summarize_commit() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="This is a commit summary.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    turns = [
        SessionTurn(
            user_message_id="1",
            assistant_message_id="2",
            user_prompt="Add a new feature",
            final_response="I've added the feature.",
            tokens=TokenUsage(),
            model="test-model",
            complete=True,
            interrupted=False,
        )
    ]
    summary = summarizer.summarize_commit(
        turns=turns,
        diff="diff --git a/file.py b/file.py\n+new code",
        session_summary=None,
    )
    assert summary == "This is a commit summary."
    backend.run.assert_called_once()
    call_args = backend.run.call_args
    assert call_args[1]["model"] == "test-model"
    assert call_args[1]["session_id"] is None


def test_summarize_commit_with_session_context() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Updated summary with new changes.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    turns = [
        SessionTurn(
            user_message_id="1",
            assistant_message_id="2",
            user_prompt="Fix a bug",
            final_response="Bug fixed.",
            tokens=TokenUsage(),
            model="test-model",
            complete=True,
            interrupted=False,
        )
    ]
    summary = summarizer.summarize_commit(
        turns=turns,
        diff="diff --git a/file.py b/file.py\n-buggy code\n+fixed code",
        session_summary="Previous session: Added new feature.",
    )
    assert summary == "Updated summary with new changes."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "Previous session: Added new feature." in prompt


def test_update_session_summary() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Updated session summary.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    turns = [
        SessionTurn(
            user_message_id="1",
            assistant_message_id="2",
            user_prompt="Add tests",
            final_response="Tests added.",
            tokens=TokenUsage(),
            model="test-model",
            complete=True,
            interrupted=False,
        )
    ]
    summary = summarizer.update_session_summary(
        current_summary="Initial summary.",
        turns=turns,
        diff="diff --git a/test.py b/test.py\n+new tests",
        commit_summary="Added tests for new feature.",
    )
    assert summary == "Updated session summary."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "Initial summary." in prompt
    assert "Added tests for new feature." in prompt


def test_update_session_summary_initial() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Initial session summary.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    turns = [
        SessionTurn(
            user_message_id="1",
            assistant_message_id="2",
            user_prompt="Start project",
            final_response="Project started.",
            tokens=TokenUsage(),
            model="test-model",
            complete=True,
            interrupted=False,
        )
    ]
    summary = summarizer.update_session_summary(
        current_summary=None,
        turns=turns,
        diff="diff --git a/main.py b/main.py\n+initial code",
        commit_summary="Started the project.",
    )
    assert summary == "Initial session summary."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "No previous session summary exists" in prompt


def test_summarize_pre_compaction() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Comprehensive session summary before compaction.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    exported = ExportedSession(
        session_id="test-session",
        model="test-model",
        updated=0,
        turns=[
            SessionTurn(
                user_message_id="1",
                assistant_message_id="2",
                user_prompt="First task",
                final_response="Done.",
                tokens=TokenUsage(),
                model="test-model",
                complete=True,
                interrupted=False,
            ),
            SessionTurn(
                user_message_id="3",
                assistant_message_id="4",
                user_prompt="Second task",
                final_response="Also done.",
                tokens=TokenUsage(),
                model="test-model",
                complete=True,
                interrupted=False,
            ),
        ],
    )
    summary = summarizer.summarize_pre_compaction(
        exported_session=exported,
        current_summary="Current summary.",
    )
    assert summary == "Comprehensive session summary before compaction."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "First task" in prompt
    assert "Second task" in prompt
    assert "Current summary." in prompt
