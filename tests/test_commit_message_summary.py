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
        summary="This commit adds a new feature that improves performance.",
    )
    assert "# Summary" in message
    assert "This commit adds a new feature that improves performance." in message
    assert "# Interaction Trace" in message
    assert message.index("# Summary") < message.index("# Interaction Trace")


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
    assert "# Summary" not in message
    assert "# Interaction Trace" in message


def test_build_agent_commit_message_summary_ordering() -> None:
    message = build_agent_commit_message(
        latest_prompt="Fix bug",
        trace=[
            {"role": "user", "content": "Fix bug"},
            {"role": "agent", "content": "Bug fixed."},
        ],
        backend="claude",
        backend_session_id="session-789",
        agit_session_id="agit-012",
        model="claude-3",
        summary="Fixed a critical bug in the authentication system.",
    )
    lines = message.split("\n")
    summary_idx = None
    trace_idx = None
    metadata_idx = None
    for i, line in enumerate(lines):
        if line == "# Summary":
            summary_idx = i
        elif line == "# Interaction Trace":
            trace_idx = i
        elif line == "# aGiT Metadata":
            metadata_idx = i
    assert summary_idx is not None
    assert trace_idx is not None
    assert metadata_idx is not None
    assert summary_idx < trace_idx < metadata_idx
