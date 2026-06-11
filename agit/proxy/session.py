"""Session: one backend session's runtime state as a real object (#29, P3).

Replaces ``agit/session_runtime.py``'s field-swapping multiplexer (where the
active session's state lived directly on the ProxyRunner and background
sessions were snapshots swapped on/off the runner).  A :class:`Session` now
*owns* its state for its whole lifetime; the runner keeps ``self.sessions``
(a list of Session objects) and ``self.active`` (a pointer into that list),
and switching sessions is a pointer assignment — never an attribute copy.

The runner still exposes every per-session field as an attribute on itself
(``runner.agent_in_flight`` etc.) through a backward-compat property layer
that delegates to ``runner.active`` — see the bottom of ``runner.py``.  P7
removes that layer and moves call sites to ``runner.active.<field>``.
"""

from __future__ import annotations

import threading

from agit.proxy.process import BackendProcess


class Session:
    """All runtime state belonging to one backend session.

    Anything NOT listed in :data:`FIELDS` is host-level (terminal size, colour
    detection, host query cache, the management lock, the ProxyInput, signal
    handlers, render throttling) and lives on the ProxyRunner, shared across
    sessions.
    """

    # The per-session runtime state (former session_runtime.SESSION_FIELDS).
    FIELDS: tuple[str, ...] = (
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

    # ``child_pid`` / ``master_fd`` are owned by the session's BackendProcess
    # (P2) and exposed as properties below, so they are excluded from slots.
    __slots__ = tuple(f for f in FIELDS if f not in ("child_pid", "master_fd")) + ("process",)

    def __init__(self, **fields) -> None:
        self.process = BackendProcess(
            master_fd=fields.get("master_fd"),
            child_pid=fields.get("child_pid"),
        )
        for field in self.FIELDS:
            if field in ("child_pid", "master_fd"):
                continue
            setattr(self, field, fields.get(field))

    # ------------------------------------------------------------------
    # Process ownership: the Session owns a BackendProcess; child_pid and
    # master_fd remain addressable as plain session fields over it.
    # ------------------------------------------------------------------

    @property
    def child_pid(self) -> int | None:
        return self.process.child_pid

    @child_pid.setter
    def child_pid(self, value: int | None) -> None:
        self.process.child_pid = value

    @property
    def master_fd(self) -> int | None:
        return self.process.master_fd

    @master_fd.setter
    def master_fd(self, value: int | None) -> None:
        self.process.master_fd = value

    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self.state.backend_session_id if self.state is not None else None

    @classmethod
    def bare(cls) -> "Session":
        """A session with fresh per-session runtime defaults (before its
        backend identity/screen are assigned)."""
        return cls(**cls.runtime_defaults())

    @staticmethod
    def runtime_defaults() -> dict:
        """Fresh per-session runtime values for a newly created session
        (former ``session_runtime.default_session_fields``)."""
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
