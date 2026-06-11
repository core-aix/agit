import pytest

from agit.commits import build_agent_commit_message, build_user_commit_message


def test_agent_commit_message_contains_trace_and_metadata():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}, {"role": "agent", "content": "fixed"}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 40,
            "input": 130,
            "output": 10,
            "reasoning": 0,
            "cache_read": 20,
            "cache_write": 5,
        },
    )

    assert message.startswith("<agent> fix it")
    assert "# Interaction Trace" in message
    assert "## User\n\nfix it" in message
    assert "## Agent\n\nfixed" in message
    assert "# aGiT Metadata" in message
    assert "backend: opencode" in message
    assert "backend_session_id: ses-1" in message
    assert "context_tokens: 100" in message
    assert "tokens_since_last_commit_input: 130" in message
    assert "tokens_since_last_commit_output: 10" in message
    assert "tokens_since_last_commit_cache_read: 20" in message
    assert "tokens_since_last_commit_cache_write: 5" in message
    assert "tokens_since_last_commit_total" not in message
    assert "tokens_since_last_commit_subagent_input" not in message
    assert "token_note" not in message


def test_commit_message_masks_secrets_in_subject_and_trace():
    message = build_agent_commit_message(
        latest_prompt="use api_key=sk-abc12345678901234567890",
        trace=[{"role": "user", "content": "password=hunter2"}, {"role": "agent", "content": "token: ghp_abcdefghijklmnopqrstuvwxyz"}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    assert "hunter2" not in message
    assert "sk-abc" not in message
    assert "ghp_" not in message
    assert "[REDACTED]" in message


def test_commit_message_removes_mouse_coordinate_reports():
    message = build_agent_commit_message(
        latest_prompt="fix [<35;59;45M[<35;59;44M bug",
        trace=[{"role": "user", "content": "[<35;60;43M[<0;42;39m keep text"}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    assert "[<35;" not in message
    assert "[<0;" not in message
    assert "keep text" in message


def test_agent_commit_message_preserves_user_trace_order():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[
            {"role": "user", "content": "fix it"},
            {"role": "agent", "content": "working"},
            {"role": "user", "content": "also handle errors"},
            {"role": "agent", "content": "done"},
        ],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    assert message.index("## User\n\nfix it") < message.index("## User\n\nalso handle errors")


def test_agent_commit_message_omits_zero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        token_usage={"context": 100, "total": 5, "input": 100, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0},
    )

    assert "tokens_since_last_commit_input: 100" in message
    assert "tokens_since_last_commit_reasoning" not in message


def test_agent_commit_message_includes_nonzero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        token_usage={"context": 100, "total": 11, "input": 100, "output": 5, "reasoning": 6, "cache_read": 0, "cache_write": 0},
    )

    assert "tokens_since_last_commit_reasoning: 6" in message


def test_agent_commit_message_records_subagent_token_categories():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="claude-opus-4-8",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "subagent_input": 40,
            "subagent_output": 60,
            "subagent_reasoning": 0,
            "subagent_cache_read": 700,
            "subagent_cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_subagent_input: 40" in message
    assert "tokens_since_last_commit_subagent_output: 60" in message
    assert "tokens_since_last_commit_subagent_cache_read: 700" in message
    # Zero-valued sub-agent categories stay out of the metadata.
    assert "tokens_since_last_commit_subagent_reasoning" not in message
    assert "tokens_since_last_commit_subagent_cache_write" not in message


def test_user_commit_message_rejects_blank_message():
    with pytest.raises(ValueError, match="required"):
        build_user_commit_message(message="", agit_session_id="agit-1")


def test_user_commit_message_uses_required_message_subject():
    message = build_user_commit_message(message="save work", agit_session_id="agit-1")

    assert message.startswith("save work")
    assert "User Message:" not in message
    assert "# aGiT Metadata" in message


def test_user_commit_message_masks_secret_subject():
    message = build_user_commit_message(message="save password=secret123", agit_session_id="agit-1")

    assert message.startswith("save password=[REDACTED]")
    assert "secret123" not in message


def test_commit_messages_include_current_agit_version_without_created_at():
    message = build_user_commit_message(message="save work", agit_session_id="agit-1")

    assert "agit_version: 0.0.1" in message
    assert "created_at" not in message


def test_agent_commit_subject_is_capped_for_github():
    message = build_agent_commit_message(
        latest_prompt="please " * 40,
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    lines = message.splitlines()
    subject = lines[0]
    assert len(subject) <= 72  # GitHub's subject-line truncation limit
    assert subject.startswith("<agent> ")
    assert subject.endswith("...")
    # The full (untruncated) text follows the subject line directly — no separate
    # "# Full Subject" header, and no blank line between subject and continuation.
    assert "# Full Subject" not in message
    assert lines[1].startswith("please please")


def test_user_commit_subject_is_capped_for_github():
    message = build_user_commit_message(message="save " * 40, agit_session_id="agit-1")

    lines = message.splitlines()
    subject = lines[0]
    assert len(subject) <= 72  # GitHub's subject-line truncation limit
    assert subject.endswith("...")
    # Full text follows the subject directly — no header, no blank line between.
    assert "# Full Subject" not in message
    assert lines[1].startswith("save save")


def test_commit_message_body_lines_are_wrapped_to_72():
    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=[{"role": "agent", "content": "a " * 80}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    body_lines = message.splitlines()[1:]
    assert all(len(line) <= 72 for line in body_lines)


def test_agent_commit_trace_is_limited_by_user_turns():
    trace = []
    for index in range(7):
        trace.extend([
            {"role": "user", "content": f"user {index}"},
            {"role": "agent", "content": f"agent {index}"},
        ])

    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=trace,
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        trace_turn_limit=5,
    )

    assert "## User\n\nuser 0" not in message
    assert "## User\n\nuser 1" not in message
    assert "## User\n\nuser 2" in message
    assert "## Agent\n\nagent 6" in message


def test_subject_strips_terminal_escape_sequences():
    # Arrow-key residue and SGR colour codes must never reach the subject.
    message = build_user_commit_message(message="fix \x1b[Bthe \x1b[31mparser\x1b[0m bug", agit_session_id="agit-1")
    subject = message.splitlines()[0]
    assert subject == "fix the parser bug"
    assert "\x1b" not in message


def test_agent_subject_strips_orphan_mouse_and_control_chars():
    message = build_agent_commit_message(
        latest_prompt="run \x07tests\x1b]0;title\x07 now",
        trace=[{"role": "user", "content": "run \x1b[Atests now"}],
        backend="claude",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="claude-opus-4-8",
    )
    subject = message.splitlines()[0]
    assert subject == "<agent> run tests now"
    assert "\x1b" not in message
    assert "\x07" not in message


def test_legitimate_bracketed_text_is_preserved():
    # Defensive escape stripping must not damage normal bracketed prose.
    message = build_user_commit_message(message="handle [Beta] flag and list[B]", agit_session_id="agit-1")
    assert message.splitlines()[0] == "handle [Beta] flag and list[B]"


def test_agent_merge_message_format():
    from agit.commits import build_agent_merge_message

    message = build_agent_merge_message(
        session_name="feature-x",
        base_branch="main",
        source_branch="agit/feature-x/t2",
        agit_session_id="agit-1",
        backend="claude",
        backend_session_id="ses-9",
        conflicting_commits="abc123 base edit",
    )
    assert message.splitlines()[0].startswith("<agent-merge> ")
    assert "commit_type: agent-merge" in message
    assert "session_name: feature-x" in message
    assert "source_branch: agit/feature-x/t2" in message
    assert "base_branch: main" in message
    assert "backend_session_id: ses-9" in message
    assert "abc123 base edit" in message
