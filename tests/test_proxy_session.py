"""Session-as-a-real-object tests (#29 P3).

Replaces tests/test_session_runtime.py: the capture/restore round-trip tests
become pointer-switch tests (state lives on the Session object and is never
copied), and default_session_fields() coverage moves to Session.bare().
"""

from types import SimpleNamespace

from agit.proxy.process import BackendProcess
from agit.proxy.session import Session
from proxy_helpers import make_runner


def _session_with_distinct_values(tag):
    return Session(**{field: f"{field}-{tag}" for field in Session.FIELDS})


# --- construction -----------------------------------------------------------


def test_session_construction_defaults_every_field_to_none():
    session = Session()
    for field in Session.FIELDS:
        assert getattr(session, field) is None, field


def test_session_construction_accepts_field_kwargs():
    session = _session_with_distinct_values("a")
    for field in Session.FIELDS:
        assert getattr(session, field) == f"{field}-a"


def test_bare_runtime_defaults_are_independent_objects():
    # Mutable defaults must not be shared between sessions (ported from
    # test_session_runtime.test_default_session_fields_are_independent_objects).
    one = Session.bare()
    two = Session.bare()
    assert one.file_change_event is not two.file_change_event
    assert one.agent_parse_lock is not two.agent_parse_lock
    assert one.passthrough_prompt is not two.passthrough_prompt
    assert one.agent_in_flight is False and one.scroll_back == 0
    assert one.turn == 0 and one.merge_ctx is None


def test_session_exposes_session_id_from_state():
    session = Session(state=SimpleNamespace(backend_session_id="ses-123"))
    assert session.session_id == "ses-123"
    assert Session(state=None).session_id is None


# --- BackendProcess ownership ------------------------------------------------


def test_session_owns_a_backend_process():
    session = Session(child_pid=42, master_fd=7)
    assert isinstance(session.process, BackendProcess)
    assert (session.process.child_pid, session.process.master_fd) == (42, 7)
    # child_pid / master_fd remain addressable as plain session fields.
    session.master_fd = None
    assert session.process.master_fd is None
    session.process.child_pid = 43
    assert session.child_pid == 43


# --- runner delegation (P3 compat layer) -------------------------------------


def test_runner_session_fields_delegate_to_active_session():
    runner = make_runner()
    # Fresh session: reads see runtime defaults...
    assert runner.agent_in_flight is False
    assert runner.scroll_back == 0
    # ...and writes land on the owning Session object.
    runner.agent_in_flight = True
    runner.name = "session-1"
    runner.master_fd = 9
    assert runner.active.agent_in_flight is True
    assert runner.active.name == "session-1"
    assert runner.active.process.master_fd == 9


def test_switching_sessions_is_pointer_assignment_not_copying():
    runner = make_runner()
    a = _session_with_distinct_values("a")
    b = _session_with_distinct_values("b")
    runner.sessions = [a, b]
    runner.active = a
    assert runner.repo == "repo-a" and runner.active_index == 0

    runner.active = b
    assert runner.repo == "repo-b" and runner.active_index == 1
    # Mutations while b is active stay on b; a is untouched.
    runner.last_status = "touched"
    assert b.last_status == "touched"
    assert a.last_status == "last_status-a"

    runner.active = a
    assert runner.repo == "repo-a"
    assert runner.last_status == "last_status-a"


def test_active_index_setter_repoints_the_active_session():
    runner = make_runner()
    a, b = Session.bare(), Session.bare()
    runner.sessions = [a, b]
    runner.active_index = 1
    assert runner.active is b
    # Setting active_index again to 0 switches back to a.
    runner.active_index = 0
    assert runner.active is a
    assert runner.active_index == 0
