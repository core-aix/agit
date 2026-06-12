"""Summary placement in commit messages (#8).

The summary leads the message like a regular subject does: its first line is
the subject, the rest of it is the first paragraph of the body — there is no
separate ``# Summary`` section. The prompts that would otherwise head the
message move to ``# Prompts``.
"""

from agit.commits.message import build_agent_commit_message


def test_build_agent_commit_message_with_summary() -> None:
    message = build_agent_commit_message(
        latest_prompt="Add new feature",
        trace=[
            {"role": "user", "content": "Add new feature"},
            {"role": "agent", "content": "Feature added."},
        ],
        backend="opencode",
        backend_session_id="session-123",
        agit_session_id="agit-456",
        model="gpt-4",
        summary="Add a faster feature pipeline.\n\nThe new path caches lookups, improving performance.",
    )
    # First line of the summary is the subject; the rest is the body's first
    # paragraph, with no # Summary section.
    assert message.startswith("<aGiT> Add a faster feature pipeline.\n")
    assert "# Summary" not in message
    body = message.split("\n", 1)[1]
    assert body.lstrip("\n").startswith("The new path caches lookups, improving performance.")
    assert "# Interaction Trace" in message
    assert body.index("The new path caches lookups") < body.index("# Prompts") < body.index("# Interaction Trace")
    # The prompts that used to head the message are preserved under # Prompts.
    assert "Add new feature" in body.split("# Prompts")[1]


def test_build_agent_commit_message_without_summary() -> None:
    message = build_agent_commit_message(
        latest_prompt="Add new feature",
        trace=[
            {"role": "user", "content": "Add new feature"},
            {"role": "agent", "content": "Feature added."},
        ],
        backend="opencode",
        backend_session_id="session-123",
        agit_session_id="agit-456",
        model="gpt-4",
        summary=None,
    )
    assert message.startswith("<aGiT> Add new feature")
    assert "# Summary" not in message
    assert "# Prompts" not in message
    assert "# Interaction Trace" in message


def test_single_line_summary_has_no_dangling_body_paragraph() -> None:
    message = build_agent_commit_message(
        latest_prompt="Fix bug",
        trace=[{"role": "user", "content": "Fix bug"}],
        backend="claude",
        backend_session_id="session-789",
        agit_session_id="agit-012",
        model="claude-3",
        summary="Fixed a critical bug in the authentication system.",
    )
    lines = message.split("\n")
    assert lines[0] == "<aGiT> Fixed a critical bug in the authentication system."
    # No leftover summary text floating before # Prompts: the body goes
    # straight to the sections.
    assert lines[1] == ""
    assert lines[2] == "# Prompts"
    assert message.index("# Prompts") < message.index("# Interaction Trace") < message.index("# aGiT Metadata")
