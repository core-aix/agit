from types import SimpleNamespace

from agit.session_runtime import (
    SESSION_FIELDS,
    Session,
    capture_session,
    default_session_fields,
    restore_session,
)


def _runner_with_distinct_values(tag):
    values = {field: f"{field}-{tag}" for field in SESSION_FIELDS}
    return SimpleNamespace(**values)


def test_capture_then_restore_round_trips():
    runner = _runner_with_distinct_values("a")
    snapshot = capture_session(runner)
    for field in SESSION_FIELDS:
        assert getattr(snapshot, field) == f"{field}-a"

    # Mutate the runner, then restore the snapshot and confirm every field returns.
    for field in SESSION_FIELDS:
        setattr(runner, field, "mutated")
    restore_session(runner, snapshot)
    for field in SESSION_FIELDS:
        assert getattr(runner, field) == f"{field}-a"


def test_swap_between_two_sessions_keeps_state_isolated():
    runner = _runner_with_distinct_values("a")
    session_a = capture_session(runner)

    # Switch to a fresh "b" session.
    for field in SESSION_FIELDS:
        setattr(runner, field, f"{field}-b")
    session_b = capture_session(runner)

    # Back to a: runner reflects a's state, b's snapshot is untouched.
    restore_session(runner, session_a)
    assert getattr(runner, SESSION_FIELDS[0]) == f"{SESSION_FIELDS[0]}-a"
    assert getattr(session_b, SESSION_FIELDS[0]) == f"{SESSION_FIELDS[0]}-b"


def test_default_session_fields_are_independent_objects():
    one = default_session_fields()
    two = default_session_fields()
    # Mutable defaults must not be shared between sessions.
    assert one["file_change_event"] is not two["file_change_event"]
    assert one["agent_parse_lock"] is not two["agent_parse_lock"]
    assert one["passthrough_prompt"] is not two["passthrough_prompt"]


def test_session_exposes_session_id_from_state():
    session = Session(state=SimpleNamespace(backend_session_id="ses-123"))
    assert session.session_id == "ses-123"
    assert Session(state=None).session_id is None
