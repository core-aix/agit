from __future__ import annotations

import threading

# The per-session runtime state. In the single-window multiplexer the *active*
# session's state lives directly on the ProxyRunner (so every existing method
# keeps using ``self.<field>`` unchanged); background sessions keep their state
# in a Session snapshot and are processed by swapping it onto the runner.
#
# Anything NOT listed here is host-level (terminal size, colour detection, host
# query cache, the management lock, the ProxyInput, signal handlers, render
# throttling) and is shared across sessions rather than swapped.
SESSION_FIELDS: tuple[str, ...] = (
    # backend + git identity
    "repo",
    "state",
    "backend",
    "actions",
    "name",
    "worktree",
    "turn",
    "merge_ctx",
    # child process / screen
    "child_pid",
    "master_fd",
    "screen",
    "stream",
    # file watcher
    "file_change_event",
    "file_observer",
    # commit / parse tracking
    "agent_in_flight",
    "agent_parse_thread",
    "agent_parse_result",
    "agent_parse_active",
    "agent_parse_lock",
    "parse_pending",
    "last_parse_start",
    "last_parse_attempt_status",
    "last_parse_finish",
    "pre_agent_reconciled_status",
    "status_check_pending",
    "last_poll",
    "last_status",
    "last_status_change",
    "last_child_output",
    "last_child_output_sample",
    "_pre_spawn_session_ids",
    # pending passthrough prompt
    "passthrough_prompt",
    "passthrough_escape",
    "pending_forwarded",
    "pending_prompt_text",
    # input buffering + per-session view state
    "_input_tail",
    "child_mouse",
    "scroll_back",
    "sel_active",
    "sel_anchor",
    "sel_point",
)


class Session:
    """A snapshot of one backend session's runtime state.

    Created by :func:`capture_session` from a ProxyRunner and applied with
    :func:`restore_session`. Holds only the fields in ``SESSION_FIELDS``.
    """

    __slots__ = SESSION_FIELDS

    def __init__(self, **fields) -> None:
        for field in SESSION_FIELDS:
            setattr(self, field, fields.get(field))

    @property
    def session_id(self) -> str | None:
        return self.state.backend_session_id if self.state is not None else None


def default_session_fields() -> dict:
    """Fresh per-session runtime values for a newly created session (before its
    backend identity/screen are assigned)."""
    return {
        "child_pid": None,
        "master_fd": None,
        "screen": None,
        "stream": None,
        "file_change_event": threading.Event(),
        "file_observer": None,
        "agent_in_flight": False,
        "agent_parse_thread": None,
        "agent_parse_result": None,
        "agent_parse_active": False,
        "agent_parse_lock": threading.Lock(),
        "parse_pending": False,
        "last_parse_start": 0.0,
        "last_parse_attempt_status": "",
        "last_parse_finish": 0.0,
        "pre_agent_reconciled_status": "",
        "status_check_pending": False,
        "last_poll": 0.0,
        "last_status": "",
        "last_status_change": 0.0,
        "last_child_output": 0.0,
        "last_child_output_sample": b"",
        "_pre_spawn_session_ids": None,
        "passthrough_prompt": bytearray(),
        "passthrough_escape": None,
        "pending_forwarded": None,
        "pending_prompt_text": "",
        "_input_tail": b"",
        "child_mouse": False,
        "scroll_back": 0,
        "sel_active": False,
        "sel_anchor": None,
        "sel_point": None,
        "turn": 0,
        "merge_ctx": None,
    }


def capture_session(runner) -> Session:
    return Session(**{field: getattr(runner, field, None) for field in SESSION_FIELDS})


def restore_session(runner, session: Session) -> None:
    for field in SESSION_FIELDS:
        setattr(runner, field, getattr(session, field, None))
