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


# --- unsuccessful summaries are rejected, not used as commit subjects (#8) ----


def _result(text: str, exit_code: int = 0) -> AgentResult:
    return AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response=text,
        exit_code=exit_code,
        tokens=TokenUsage(),
    )


def _turn() -> SessionTurn:
    return SessionTurn(
        user_message_id="1",
        assistant_message_id="2",
        user_prompt="Add a new feature",
        final_response="I've added the feature.",
        tokens=TokenUsage(),
        model="test-model",
        complete=True,
        interrupted=False,
    )


def test_summarizer_raises_on_session_limit_error_text() -> None:
    import pytest
    from agit.summaries import UnusableSummaryError

    backend = Mock()
    backend.run.return_value = _result("You've hit your session limit. Your limit will reset at 3pm.")
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(turns=[_turn()], diff="+x", session_summary=None)


def test_summarizer_raises_on_nonzero_exit_code() -> None:
    import pytest
    from agit.summaries import UnusableSummaryError

    backend = Mock()
    backend.run.return_value = _result("Looks like a fine summary.", exit_code=1)
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(turns=[_turn()], diff="+x", session_summary=None)


def test_summarizer_raises_on_empty_response() -> None:
    import pytest
    from agit.summaries import UnusableSummaryError

    backend = Mock()
    backend.run.return_value = _result("   \n  ")
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(turns=[_turn()], diff="+x", session_summary=None)


def test_summary_is_usable_detects_error_shapes() -> None:
    from agit.summaries import summary_is_usable

    for error in [
        "You've hit your session limit. It resets at 3pm.",
        "you have hit your usage limit",
        "Claude usage limit reached|1760000000",
        "API Error: 429 rate_limit_error",
        "Credit balance is too low",
        "Please run /login to authenticate",
        "Invalid API key. Please check your configuration.",
    ]:
        assert summary_is_usable(error) is False, error


def test_summary_is_usable_keeps_topical_mentions_of_limits_and_errors() -> None:
    from agit.summaries import summary_is_usable

    # Legitimate summaries that merely talk about limits/errors must pass.
    for summary in [
        "Add rate limiting to the API client",
        "Fix API error handling in the retry loop",
        "Enforce a session limit on concurrent worktrees",
        "Handle the usage limits page in the settings UI",
    ]:
        assert summary_is_usable(summary) is True, summary


def test_summary_is_usable_rejects_echoed_prompt() -> None:
    from agit.summaries import summary_is_usable
    from agit.summaries.prompts import COMMIT_SUMMARY_SYSTEM, PRE_COMPACTION_SYSTEM, SESSION_UPDATE_SYSTEM

    # The exact bug: the backend echoed its own system prompt, which then became
    # the commit subject/body. Every system prompt and the scaffolding must be
    # recognised as not-a-summary.
    assert summary_is_usable(COMMIT_SUMMARY_SYSTEM) is False
    assert summary_is_usable(SESSION_UPDATE_SYSTEM) is False
    assert summary_is_usable(PRE_COMPACTION_SYSTEM) is False
    assert summary_is_usable("Recent conversation turns:\nUser: hi\nAgent: hello") is False
    # A real summary that happens to mention conversations still passes.
    assert summary_is_usable("Refactored the conversation parser and added tests.") is True


def test_summarizer_raises_when_backend_echoes_the_prompt() -> None:
    import pytest
    from agit.summaries import UnusableSummaryError

    # Simulate the backend returning the *entire prompt* it was given (the
    # observed failure mode) — the summarizer must reject it so the commit keeps
    # its prompt-led message rather than a prompt-dump.
    captured: dict[str, str] = {}

    def echo_run(prompt, *, model=None, session_id=None):
        captured["prompt"] = prompt
        return _result(prompt)  # echo the prompt back as the response

    backend = Mock()
    backend.run.side_effect = echo_run
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(turns=[_turn()], diff="+x", session_summary=None)
    assert captured["prompt"].startswith("You are a technical summarizer")


def test_looks_like_prompt_echo_is_marker_independent() -> None:
    from agit.summaries.summarizer import _looks_like_prompt_echo

    # The general (marker-independent) check: a response that restates the prompt
    # from the top is an echo even if no fixed marker matches.
    prompt = "Please produce a one-line summary of the following changes in plain prose."
    assert _looks_like_prompt_echo(prompt, prompt) is True
    assert _looks_like_prompt_echo(prompt, prompt + " extra trailing text") is True
    assert _looks_like_prompt_echo(prompt, "A concise, unrelated summary of the work.") is False
    # Short responses can't be confused with an echo of a long prompt.
    assert _looks_like_prompt_echo(prompt, "Done.") is False


def test_session_update_rejects_echoed_prompt() -> None:
    import pytest
    from agit.summaries import UnusableSummaryError

    # The session-update path goes through the same _run guard; echoing the
    # received prompt must be rejected too.
    def echo_run(prompt, *, model=None, session_id=None):
        return _result(prompt)

    backend = Mock()
    backend.run.side_effect = echo_run
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.update_session_summary(current_summary=None, turns=[_turn()], diff="+x", commit_summary="x")
