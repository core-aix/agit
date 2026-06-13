from __future__ import annotations

import base64
import os
import re
import select
import shutil
import signal
import subprocess
import sys
from types import FrameType
from typing import Any, Callable, cast
import termios
import threading
import time

import pyte

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised only without optional dependency
    FileSystemEvent = None  # type: ignore[misc, assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]
    Observer = None  # type: ignore[misc, assignment]

from agit.commits import AgitActions
from agit.backends.setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agit.backends.proxy_agents import available_backends, make_proxy_agent
from agit.commits import (
    METADATA_HEADER,
    apply_summary_to_message,
    build_user_commit_message,
    summary_metadata_lines,
)
from agit.git import GitRepo
from agit.config import GlobalConfig
from agit.git import RepoLock, already_running_message
from agit.proxy import sandbox
from agit.config import AgitState
from agit.git import WorktreeInfo, WorktreeManager, _sanitize_name
from agit.proxy.commit_engine import CommitEngine
from agit.proxy.integration import IntegrationService, MergeContext, MergePhase
from agit.proxy.process import BackendProcess
from agit.proxy.session import Session


# Palette helpers, _BackgroundColorEraseScreen, and detect_color_mode live in
# renderer.py (P1). Re-import them here so call sites in this file are unchanged
# and the agit.proxy shim can export them under their original names.
from agit.proxy.renderer import (
    detect_color_mode,
    _BackgroundColorEraseScreen,
    ScreenRenderer,
)

# TerminalHost lives in terminal.py (P1).
from agit.proxy.terminal import TerminalHost
from agit.update import Updater, UpdateStatus, restart_agit

# Modal state-machines (P6 Stage 2): PromptModal and SelectModal encode the
# byte-handling logic for free-text and selection popups.
from agit.proxy.modal import PromptModal, SelectModal, _escape_sequence_complete

_SGR_MOUSE_RE = re.compile(rb"\x1b\[<\d+;\d+;\d+[Mm]")
_SGR_MOUSE_EVENT_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_PAGE_KEY_RE = re.compile(rb"\x1b\[(5|6)(?:;\d+)?~")  # PageUp / PageDown (with optional modifiers)
# A trailing, not-yet-complete CSI sequence (e.g. a mouse report split across
# reads). Held back so it is not forwarded as stray bytes. A lone trailing ESC
# is deliberately NOT matched so the Escape key is never delayed.
_INCOMPLETE_TAIL_RE = re.compile(rb"\x1b\[[<0-9;]*$")
# Private-marker CSI sequences (parameter prefix ``>``, ``<`` or ``=``) — xterm
# keyboard/feature negotiation such as XTMODKEYS (``CSI > Ps m``), XTVERSION
# (``CSI > Ps q``) and the kitty keyboard protocol (``CSI < Ps u``). pyte cannot
# model these and, worse, mis-tokenises ``\x1b[>4m`` as the SGR ``\x1b[4m``
# (underline on), which then sticks to everything drawn afterwards. None of them
# affect the visible grid, so they are stripped from the copy fed to pyte. The
# ``?`` (DEC private) forms are deliberately NOT matched — pyte models several of
# them and aGiT syncs the rest from the raw output separately.
_PYTE_HOSTILE_CSI_RE = re.compile(rb"\x1b\[[<>=][0-9;:]*[ -/]*[@-~]")
# Keyboard-protocol negotiation the backend sends to its (virtual) terminal:
# kitty keyboard protocol push/set/pop/query (``CSI > flags u`` / ``CSI = ... u``
# / ``CSI < ... u`` / ``CSI ? u``) and xterm modifyOtherKeys (``CSI > 4 ; N m``).
# aGiT renders from a pyte model, so the HOST terminal never sees these unless
# they are mirrored — and without them the host keeps sending plain ``\r`` for
# Shift+Enter instead of the disambiguated encoding the backend's keybindings
# (e.g. Claude's newline-in-input) expect.
_KEYBOARD_PROTO_RE = re.compile(rb"\x1b\[(?:[><=][0-9;]*u|\?u|>4(?:;[0-9]+)?m)")
# A bracketed paste (CSI 200~ ... CSI 201~, possibly split across reads, hence
# the `|$`): newlines inside it are pasted CONTENT, not prompt submissions.
_BRACKETED_PASTE_RE = re.compile(rb"\x1b\[200~.*?(?:\x1b\[201~|$)", re.S)


def _short_session(session_id: str | None) -> str:
    if not session_id:
        return "(none)"
    return session_id[:8]


class RepoChangeHandler(FileSystemEventHandler):
    IGNORED_PARTS = {".agit", ".git", ".pytest_cache", ".venv", "__pycache__"}

    def __init__(self, repo_path, changed: threading.Event) -> None:
        self.repo_path = repo_path
        self.changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        # watchdog reports src_path as str or bytes depending on how the watch was
        # set up; normalise to str so the IGNORED_PARTS check is uniform.
        src_path = os.fsdecode(event.src_path)
        try:
            relative = os.path.relpath(src_path, self.repo_path)
        except ValueError:
            relative = src_path
        if any(part in self.IGNORED_PARTS for part in relative.split(os.sep)):
            return
        self.changed.set()


class ProxyInput:
    # Order matters (shown in the palette). Only "session" starts with "s" so
    # that pressing s+Enter jumps straight to the session picker. Git-specific
    # commands are grouped under a "git-" prefix.
    COMMANDS = [
        "session",
        "agent-backend",
        "summarizer",
        "git-base-branch",
        "git-status",
        "git-stage",
        "git-unstaged",
        "git-user-commit",
        "update",
        "exit",
    ]

    def __init__(self, menu_key: bytes = b"\x07") -> None:
        self.capturing = False
        self.buffer = bytearray()
        self.selected_index = 0
        self.escape_buffer: bytearray | None = None
        # The control byte that opens the command menu (default Ctrl-G;
        # configurable via "menu_key" in ~/.agit/config.json).
        self.menu_key = menu_key

    def feed(self, data: bytes) -> tuple[list[bytes], bytes, str | None, bool]:
        forwarded: list[bytes] = []
        command = None
        should_exit = False
        for byte in data:
            char = bytes([byte])
            if char == b"\x03":
                if self.capturing:
                    # Inside aGiT's own command palette, Ctrl-C cancels it
                    # (like Esc) rather than starting the exit flow.
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                    self.escape_buffer = None
                    continue
                # Ctrl-C starts aGiT's exit flow: the first press opens the
                # confirmation popup, and a second press while it is open exits
                # immediately but still gracefully (see _run_exit_flow).
                should_exit = True
                break
            if self.capturing:
                if self.escape_buffer is not None:
                    self.escape_buffer.extend(char)
                    sequence = bytes(self.escape_buffer)
                    if sequence in {b"\x1b[A", b"\x1b[B"}:
                        self._move_selection(-1 if sequence == b"\x1b[A" else 1)
                        self.escape_buffer = None
                    elif _escape_sequence_complete(sequence):
                        self.escape_buffer = None
                    continue
                if char == b"\x1b":
                    if byte == data[-1]:
                        self.buffer.clear()
                        self.capturing = False
                        self.selected_index = 0
                        self.escape_buffer = None
                        continue
                    self.escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    typed = self.buffer.decode(errors="ignore").strip()
                    command = self.selected() or typed
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                elif char in {b"\x7f", b"\b"}:
                    if self.buffer:
                        self.buffer.pop()
                        self.selected_index = 0
                elif char == b"\t":
                    match = self.selected()
                    if match:
                        self.buffer = bytearray(match.encode())
                        self.selected_index = 0
                else:
                    self.buffer.extend(char)
                    self.selected_index = 0
                continue

            if char == self.menu_key:
                self.capturing = True
                self.selected_index = 0
                self.escape_buffer = None
                continue

            forwarded.append(char)
        return forwarded, b"", command, should_exit

    def text(self) -> str:
        return self.buffer.decode(errors="ignore")

    def matches(self) -> list[str]:
        text = self.text()
        if not text:
            return self.COMMANDS
        return [command for command in self.COMMANDS if command.startswith(text)] or self.COMMANDS

    def selected(self) -> str | None:
        matches = self.matches()
        if not matches:
            return None
        self.selected_index = min(self.selected_index, len(matches) - 1)
        return matches[self.selected_index]

    def _move_selection(self, delta: int) -> None:
        matches = self.matches()
        if matches:
            self.selected_index = (self.selected_index + delta) % len(matches)


class ProxyRunner:
    # Defaults for the tunable timings; overridden per-instance from the global
    # config in __init__ (see GlobalConfig.timings). Kept as class constants so
    # `self.X` still resolves for runners built via __new__ (tests).
    FILE_STABLE_SECONDS = 8.0
    CHILD_IDLE_SECONDS = 4.0
    POLL_SECONDS = 2.0
    PARSE_COOLDOWN_SECONDS = 10.0
    BASE_POLL_SECONDS = 3.0
    BASE_EDIT_CHECK_SECONDS = 3.0
    CWD_CHECK_SECONDS = 3.0
    BASE_DRIFT_CHECK_SECONDS = 2.0
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps
    SYNC_MAX_HOLD = 0.05  # cap how long a backend synchronized-update may defer a paint
    SUMMARY_WAIT_SECONDS = 45.0  # how long integration waits for a background commit summary (#8)

    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        use_worktrees: bool = True,
        backend_args: list[str] | None = None,
        # Optional injected collaborators (default to production construction).
        # These keyword arguments are for testing and advanced use; the CLI call
        # site passes only the first five parameters and is unaffected.
        _global_config: "GlobalConfig | None" = None,
        _state: "AgitState | None" = None,
        _integration: "IntegrationService | None" = None,
        _lock: "RepoLock | None" = None,
    ) -> None:
        # Attach the initial session; per-session state lives on it.
        self.active = Session.bare()
        self.repo = repo
        self._use_worktrees = use_worktrees  # #9: when False, run on the current branch directly
        # Extra CLI args forwarded verbatim to every backend spawn (#32).
        self._backend_args = list(backend_args or [])
        self._force_new_session = new_session  # start a fresh conversation, do not resume
        self.name = "main"  # session label (multiplexer assigns names to others)
        self._primary_worktree_name: str | None = None  # session kept across exits for auto-resume
        self.worktree: WorktreeInfo | None = None  # set when this session runs in a git worktree
        self.global_config = _global_config if _global_config is not None else GlobalConfig()
        self._apply_timings(self.global_config.timings)
        self.state = (
            _state if _state is not None else AgitState(repo.repo, default_backend=self.global_config.default_backend)
        )
        if backend and backend != self.state.backend:
            self.state.remember_backend_session()
            self.state.backend = backend
            self.global_config.default_backend = backend
            self.state.backend_session_id = self.state.stored_backend_session(backend)
            self.state.last_backend_message_id = None
        self.backend = make_proxy_agent(self.state.backend)
        self.actions = AgitActions(repo, self.state, verbose=verbose)
        self.verbose = verbose
        self.input = ProxyInput(menu_key=self.global_config.menu_key_byte)
        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.last_poll = 0.0
        self.status_check_pending = False
        self.file_change_event = threading.Event()
        self.file_observer: Any = None
        self.parse_pending = False
        self.last_parse_start = 0.0
        self.running = True
        self.old_attrs: Any = None
        self.original_sigwinch: Callable[[int, FrameType | None], Any] | int | None = None
        self.original_signal_handlers: dict = {}
        self.rows = 24
        self.cols = 80
        self.screen: pyte.HistoryScreen | None = None
        self.stream: pyte.ByteStream | None = None
        # Scrollback: whether the backend manages the mouse itself (OpenCode) or
        # aGiT must provide wheel-driven scrollback (Claude streams to the normal
        # screen and relies on native scrollback, which aGiT's render replaces).
        self.child_mouse = False
        self.scroll_back = 0
        self._last_render = 0.0
        self._render_pending = False
        # Synchronized output (DECSET 2026): while the backend is mid-update we
        # hold the repaint so a half-drawn frame is never shown (tearing), and
        # each repaint aGiT emits is itself wrapped in a 2026 update so the host
        # applies the whole frame atomically (the flicker fix).
        self._in_sync_update = False
        self._sync_since = 0.0
        # Mouse drag selection -> clipboard (for backends aGiT renders itself).
        self.sel_active = False
        self.sel_anchor: tuple[int, int] | None = None
        self.sel_point: tuple[int, int] | None = None
        self._input_tail = b""
        self.last_child_output = 0.0
        self.last_child_output_sample = b""
        self.last_status = ""
        self.last_status_change = 0.0
        self.message: str | None = None
        self.message_until = 0.0
        # A sticky message stays up until the user's next keypress instead of
        # timing out — used for the auto-commit confirmation so the user actually
        # sees that aGiT committed (and isn't misled by the backend asking to
        # commit work aGiT has already captured).
        self._message_sticky = False
        self._last_agent_commit_id: str | None = None
        # Prompts the user queued while the agent was busy; the next commit waits
        # for each to appear as a turn so a queued follow-up shares one commit with
        # the turn it follows instead of producing a second commit.
        self._awaited_followups: list[str] = []
        self.agent_parse_thread: threading.Thread | None = None
        self.agent_parse_result = None
        self.agent_parse_active = False
        self.agent_parse_lock = threading.Lock()
        self.agent_in_flight = False
        self.pre_agent_reconciled_status = ""
        self.last_parse_attempt_status = ""
        self.last_parse_finish = 0.0
        self.passthrough_prompt = bytearray()
        self.passthrough_escape: bytearray | None = None
        self.pending_forwarded: list[bytes] | None = None
        self.pending_prompt_text = ""
        # Session ids that existed before aGiT launched a fresh backend session,
        # used to identify (and then pin to) the session aGiT actually spawned
        # rather than chasing whichever session is globally newest.
        self._pre_spawn_session_ids: set[str] | None = None
        # Raw responses captured from the host terminal so we can answer the
        # same queries OpenCode makes (foreground/background/palette colors and
        # device attributes). Without these, OpenCode cannot detect the real
        # terminal theme and its colors do not match a native session.
        self.host_fg_value: bytes | None = None
        self.host_bg_value: bytes | None = None
        self.host_palette: dict[bytes, bytes] = {}
        self.host_da: bytes | None = None
        self.color_mode = detect_color_mode()
        # Single-writer management: only one aGiT may auto-commit/merge in a
        # working tree. A second instance is refused at startup (see `run`).
        self.management_lock = _lock if _lock is not None else RepoLock(repo.repo / ".agit" / "lock")
        # Multiplexer: every session (active included) is a Session object in
        # `self.sessions`; `self.active` points at the one being serviced and
        # switching sessions is a pointer assignment. With a single session
        # this stays empty/identity and the loop is unchanged. `base_repo` is
        # the main working tree (worktrees branch off it).
        self.base_repo = repo
        # IntegrationService: encapsulates all branch/merge/integration policy.
        # base_branch is set at startup (run()) and updated by _perform_base_switch.
        self._integration: IntegrationService = (
            _integration if _integration is not None else IntegrationService(repo, None, menu_label=self._menu_label())
        )
        self._base_branch: str | None = None  # integration target branch (set at startup)
        self._integration_paused = False  # set when the base repo is switched off _base_branch out-of-band
        self._base_drift_check_at = 0.0
        self.turn = 0  # per-session transient-branch counter
        self.merge_ctx: MergeContext | None = None  # in-progress agent merge resolution
        self._pending_enter_at: float | None = None  # deferred submit of an injected prompt
        self._pending_enter_fd: int | None = None  # the PTY that injected prompt's Enter must go to
        self._base_advanced = False  # base moved; sync idle sessions onto it on the next loop pass
        self._last_base_head: str | None = None  # last-polled base HEAD, to catch out-of-band commits
        self._base_edits_declined_status: str | None = None  # base status the user declined to commit
        self._popup_exit_pending = False  # a popup Ctrl-C exit flow is running
        self._popup_exit_force = False  # second Ctrl-C inside the exit confirmation
        self._reap_pids: list[int] = []  # signalled backends awaiting their waitpid
        self._idle_integrate_at = 0.0  # throttle for integrating agent-made commits
        self._attach_uncovered_until = 0.0  # deadline for attaching traces to backend-made commits (#35)
        self._summary_thread: threading.Thread | None = None  # background commit-summary worker (#8)
        self._summary_result: dict | None = None  # finished summary awaiting main-thread application
        self._summary_pending: dict | None = None  # {"sha", "since"} while a summary is being computed
        self._precompact_thread: threading.Thread | None = None  # background pre-compaction summary worker
        self._precompact_result: dict | None = None
        self._base_poll_at = 0.0  # throttle for the base-HEAD poll
        self._warned_backend_session = False  # one-shot "use agit to start sessions" notice
        # Lifecycle flags read before their first conditional assignment. These
        # MUST be initialized here: their getattr() guards were removed in P7,
        # and for_testing() seeding them alone would hide a missing init from
        # the suite (the real __init__ is the production path).
        self._monitor_base_edits = False
        self._base_check_at = 0.0
        self._cwd_drift_checked = False
        self._cwd_check_at = 0.0
        self._relaunch_times: list[float] = []
        self._exiting = False
        self._finalized_on_exit = False
        # The user's intentionally-unstaged files belong to the base working tree
        # (their repo), not the ephemeral session worktree; cache the list so the
        # status line can show its count without a per-frame disk read.
        self._user_declined: list[str] = []
        self.sessions: list[Session] = []
        self.worktree_manager: WorktreeManager | None = None
        # AGIT_DEBUG_RAW records every raw child-output / user-input chunk so an
        # interactive glitch (e.g. Claude's native session picker) can be replayed
        # byte-for-byte; it implies debug logging too.
        self.raw_capture = os.environ.get("AGIT_DEBUG_RAW", "").strip().lower() in {"1", "true", "yes"}
        self.debug_proxy = (
            verbose
            or self.raw_capture
            or os.environ.get("AGIT_DEBUG_PROXY", "").strip().lower() in {"1", "true", "yes"}
        )
        # One diagnostic-log file per run, in the base repo's .agit/ (survives the
        # per-run worktree teardown).
        self._diag_run = time.strftime("%Y%m%d-%H%M%S")
        # Self-update: a background check runs on a throttle; when the user opts
        # in, the update is applied once every session is finished and committed,
        # then aGiT re-execs itself.
        self._updater = Updater()
        self._update_status: UpdateStatus | None = None  # latest completed check result
        self._update_check_at = 0.0  # throttle for the periodic background check
        self._update_check_thread: threading.Thread | None = None
        self._update_worker_result: UpdateStatus | None = None  # worker -> main handoff
        self._update_offered = False  # have we notified the user about the current update?
        self._update_pending = False  # user accepted; apply once sessions finish
        self._update_applying = False  # apply+restart in progress
        self._pending_restart = False  # re-exec aGiT after the loop tears down

    def _apply_timings(self, timings: dict[str, float]) -> None:
        # Override the class-constant timing defaults with the user's configured
        # values (GlobalConfig.timings already validated + filled in the defaults).
        self.FILE_STABLE_SECONDS = timings["file_stable_seconds"]
        self.CHILD_IDLE_SECONDS = timings["child_idle_seconds"]
        self.POLL_SECONDS = timings["background_poll_seconds"]
        self.PARSE_COOLDOWN_SECONDS = timings["parse_cooldown_seconds"]
        self.BASE_POLL_SECONDS = timings["base_poll_seconds"]
        self.BASE_EDIT_CHECK_SECONDS = timings["base_edit_check_seconds"]
        self.CWD_CHECK_SECONDS = timings["cwd_check_seconds"]
        self.BASE_DRIFT_CHECK_SECONDS = timings["base_drift_check_seconds"]
        self.SUMMARY_WAIT_SECONDS = timings["summary_wait_seconds"]
        self.UPDATE_CHECK_SECONDS = timings["update_check_seconds"]

    # --- session pointer -------------------------------------------------

    # NOTE: inside temp-swap helpers (_with_session, _pump_background,
    # _stop_session, _finalize_pending_work) `active` — and therefore
    # `active_index` — refers to the session being SERVICED, not the
    # user-facing foreground one. Do not call UI or session-list helpers
    # (_session_name, _session_status, _background_fds, popups) from code
    # reachable inside those windows; they would classify the real foreground
    # session as background.
    @property
    def active(self) -> Session:
        """The Session whose state the runner currently operates on."""
        return self.__dict__["_active_session"]

    @active.setter
    def active(self, session: Session) -> None:
        self.__dict__["_active_session"] = session

    @property
    def active_index(self) -> int:
        """Derived from the session pointer: position of the active Session in self.sessions."""
        sessions = self.__dict__.get("sessions")
        active = self.__dict__.get("_active_session")
        if sessions and active is not None:
            for index, session in enumerate(sessions):
                if session is active:
                    return index
        return 0

    @active_index.setter
    def active_index(self, index: int) -> None:
        sessions = self.__dict__.get("sessions")
        if sessions and 0 <= index < len(sessions) and isinstance(sessions[index], Session):
            self.active = sessions[index]

    @property
    def _integration(self) -> IntegrationService:
        """The integration service. Lazily constructed only as a safety net for
        partially-constructed runners; production wires it in __init__ and
        tests inject it via for_testing()/_integration kwarg."""
        svc = self.__dict__.get("_integration_svc")
        if svc is None:
            base_repo = self.__dict__.get("base_repo")
            base_branch = self.__dict__.get("_base_branch")
            # Production always wires base_repo before _integration is read; this lazy
            # branch is only a safety net for partially-constructed/test runners, which
            # may build the service before base_repo is set, so pass it through as-is.
            svc = IntegrationService(cast("GitRepo", base_repo), base_branch, menu_label=self._menu_label())
            self.__dict__["_integration_svc"] = svc
        return svc

    @_integration.setter
    def _integration(self, svc: IntegrationService) -> None:
        self.__dict__["_integration_svc"] = svc

    # ------------------------------------------------------------------
    # Test factory: builds a fully-initialised ProxyRunner without the
    # production __init__ path (which requires a real filesystem, a TTY,
    # etc.).  Call sites in tests must migrate from ProxyRunner.__new__
    # to ProxyRunner.for_testing(**overrides).
    # ------------------------------------------------------------------

    @classmethod
    def for_testing(cls, **overrides) -> "ProxyRunner":
        """Return a ProxyRunner suitable for unit tests.

        A real :class:`Session` is attached and all runner-level fields are
        initialised to safe defaults. Any keyword argument whose name matches
        a :data:`~agit.proxy.session.Session.FIELDS` entry is routed to the
        session; all other keyword arguments are set directly on the runner.

        Example::

            runner = ProxyRunner.for_testing(
                repo=fake_repo,
                state=AgitState(tmp_path),
                verbose=False,
            )
        """
        instance = cls.__new__(cls)

        # --- runner-level defaults (fields that live on the runner, not the session) ---
        instance.__dict__.update(
            {
                "verbose": False,
                "input": ProxyInput(),
                "running": True,
                "old_attrs": None,
                "original_sigwinch": None,
                "original_signal_handlers": {},
                "rows": 24,
                "cols": 80,
                "_last_render": 0.0,
                "_render_pending": False,
                "_in_sync_update": False,
                "_sync_since": 0.0,
                "message": None,
                "message_until": 0.0,
                "_message_sticky": False,
                "_last_agent_commit_id": None,
                "_awaited_followups": [],
                "host_fg_value": None,
                "host_bg_value": None,
                "host_palette": {},
                "host_da": None,
                "color_mode": "truecolor",
                "management_lock": None,
                "base_repo": None,
                "_base_branch": None,
                "_integration_paused": False,
                "_base_drift_check_at": 0.0,
                "_pending_enter_at": None,
                "_pending_enter_fd": None,
                "_base_advanced": False,
                "_last_base_head": None,
                "_base_edits_declined_status": None,
                "_popup_exit_pending": False,
                "_popup_exit_force": False,
                "_reap_pids": [],
                "_idle_integrate_at": 0.0,
                "_attach_uncovered_until": 0.0,
                "_summary_thread": None,
                "_summary_result": None,
                "_summary_pending": None,
                "_precompact_thread": None,
                "_precompact_result": None,
                "_base_poll_at": 0.0,
                "_warned_backend_session": False,
                "_user_declined": [],
                "sessions": [],
                "worktree_manager": None,
                "raw_capture": False,
                "debug_proxy": False,
                "_diag_run": "test",
                "_force_new_session": False,
                "_primary_worktree_name": None,
                "global_config": None,
                # Lazily-set fields that getattr() guards in production methods:
                "_monitor_base_edits": False,
                "_base_check_at": 0.0,
                "_cwd_drift_checked": False,
                "_cwd_check_at": 0.0,
                "_relaunch_times": [],
                "_exiting": False,
                "_finalized_on_exit": False,
                # Self-update fields (production sets these in __init__).
                "_updater": None,
                "_update_status": None,
                "_update_check_at": 0.0,
                "_update_check_thread": None,
                "_update_worker_result": None,
                "_update_offered": False,
                "_update_pending": False,
                "_update_applying": False,
                "_pending_restart": False,
                "UPDATE_CHECK_SECONDS": 300.0,
            }
        )
        # Apply timing class-constant defaults (so CHILD_IDLE_SECONDS etc. resolve).
        # These stay as class attributes; no instance-level override needed unless
        # the test provides one via **overrides below.

        # --- Separate session-level overrides from runner-level overrides ---
        session_fields = set(Session.FIELDS)
        session_overrides = {k: v for k, v in overrides.items() if k in session_fields}
        runner_overrides = {k: v for k, v in overrides.items() if k not in session_fields}

        # Build the session with any provided session-level values merged on top of
        # Session.bare() defaults.
        session_kwargs = Session.runtime_defaults()
        session_kwargs.update(session_overrides)
        session = Session(**session_kwargs)
        instance.__dict__["_active_session"] = session

        # Apply runner-level overrides. Names shadowed by a class property are
        # routed through it; a read-only property makes the misuse loud instead
        # of silently leaving the kwarg inert in __dict__.
        for key, value in runner_overrides.items():
            descriptor = getattr(cls, key, None)
            if isinstance(descriptor, property):
                if descriptor.fset is None:
                    raise TypeError(
                        f"for_testing() cannot set {key!r}: it is a read-only property derived from runner state"
                    )
                setattr(instance, key, value)
            else:
                instance.__dict__[key] = value

        # base_repo defaults to repo if not explicitly overridden.
        if instance.__dict__.get("base_repo") is None:
            repo = getattr(instance.active, "repo", None)
            if repo is not None:
                instance.__dict__["base_repo"] = repo

        return instance

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Proxy mode requires an interactive terminal. Use --mode json for non-TTY use.")
        if not self._ensure_backend_available():
            return 1
        if not self.management_lock.acquire():
            print(already_running_message(self.management_lock.owner_pid()))
            return 1
        self.state.save()
        if self.actions.has_pre_agent_user_changes():
            print("User changes detected before the agent starts.")
            self.actions.create_user_commit()
        # Base-merge-only: run even the first session in a worktree so the base
        # branch is only advanced by integration, never edited by a live agent.
        self._base_branch = self.base_repo.current_branch()
        self._integration.base_branch = self._base_branch
        self._cleanup_stale_state_on_startup()
        self._reload_user_declined()  # files the pre-agent flow left intentionally unstaged
        self._setup_base_merge_only_session()
        self._apply_new_session_if_requested()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        # Register the initial session as the sole (active) entry in the
        # multiplexer. Additional sessions are appended by `_new_session`.
        self.sessions = [self.active]
        self._reconcile_sessions_on_startup()
        self.old_attrs = termios.tcgetattr(sys.stdin.fileno())
        try:
            self._enter_host_screen()
            self._set_raw()
            self._detect_host_terminal()
            self._resize_child()
            self.original_sigwinch = signal.getsignal(signal.SIGWINCH)
            self.original_signal_handlers = {
                signal.SIGTERM: signal.getsignal(signal.SIGTERM),
                signal.SIGHUP: signal.getsignal(signal.SIGHUP),
            }
            signal.signal(signal.SIGWINCH, lambda _signum, _frame: self._resize_child())
            signal.signal(signal.SIGTERM, self._handle_exit_signal)
            signal.signal(signal.SIGHUP, self._handle_exit_signal)
            self._setup_worktree_confinement_notice()
            exit_code = self._loop()
        finally:
            if self.original_sigwinch is not None:
                signal.signal(signal.SIGWINCH, self.original_sigwinch)
            for signum, handler in self.original_signal_handlers.items():
                signal.signal(signum, handler)
            self._stop_file_watcher()
            self._cleanup_child()
            self._restore_terminal()
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
            self.management_lock.release()
        # A self-update was applied; re-exec aGiT in place now that the terminal
        # is restored and the management lock is released. Does not return.
        if self._pending_restart:
            restart_agit()
        return exit_code

    def _ensure_backend_available(self) -> bool:
        try:
            resolved = ensure_installed_backend(self.state.backend, self.global_config, interactive=True)
        except BackendUnavailable as error:
            print(error)
            return False
        if resolved != self.state.backend:
            self.state.backend = resolved
            self.backend = make_proxy_agent(resolved)
        return True

    def _spawn(self) -> None:
        resume = self._should_continue_session()
        if resume:
            session_id = self.state.backend_session_id
            self._pre_spawn_session_ids = None
        else:
            session_id = self.backend.new_session_id()
            if session_id:
                # The backend lets aGiT choose the id, so it is pinned already.
                self.state.backend_session_id = session_id
                self._pre_spawn_session_ids = None
            else:
                # The backend assigns its own id; snapshot existing sessions so
                # the one it creates can be identified on the first parse.
                self._pre_spawn_session_ids = {ref.id for ref in self.backend.list_sessions(self.repo.repo)}
        command = self.backend.spawn_command(self.repo.repo, session_id=session_id, resume=resume)
        # Forward any backend-specific args the user passed through aGiT (#32),
        # before the sandbox wrapper so they reach the backend, not sandbox-exec.
        command = command + getattr(self, "_backend_args", [])
        command = self._confine_to_worktree(command)
        # Fork/exec mechanics delegated to BackendProcess; policy (command
        # construction, sandbox wrapping) stays here in the runner. The session
        # owns its BackendProcess; child_pid / master_fd remain readable on the
        # runner via the Session-delegating compat properties.
        self.active.process = BackendProcess.spawn(command, str(self.repo.repo))

    def _setup_worktree_confinement_notice(self) -> None:
        # When confinement is requested but the platform can't enforce it (no
        # sandbox), watch the base repo and warn if the agent writes into it, so
        # edits outside the worktree don't silently go untracked.
        self._monitor_base_edits = False
        if self.worktree is None:
            return
        if not (self.global_config.sandbox and sandbox.is_enabled()):
            return  # user disabled confinement
        if sandbox.is_available():
            return  # the sandbox enforces it; no warning needed
        self._monitor_base_edits = True
        self._base_check_at = 0.0
        try:
            self._base_status_baseline: set[str] = set(self.base_repo.status_short().splitlines())
        except Exception:
            self._base_status_baseline = set()
        self._set_message(
            "Agent sandbox unavailable on this platform — edits outside the session\n"
            "worktree can't be prevented; aGiT will warn if the base repo is modified.",
            seconds=8.0,
        )
        self._render()

    def _check_base_branch_drift(self) -> None:
        # The user can `git checkout` another branch in the base repo while aGiT is
        # running. aGiT integrates session work into the branch it launched on
        # (`self._base_branch`) by fast-forwarding the base repo's checkout, so a
        # switch would target the wrong branch. Detect it, PAUSE worktree merging,
        # and tell the user; resume automatically when they switch back. (Sessions
        # keep running and committing to their own branches throughout.)
        if self._base_branch is None:
            return
        paused, new_check_at, message = self._integration.check_base_drift(
            base_branch=self._base_branch,
            integration_paused=self._integration_paused,
            last_check_at=self._base_drift_check_at,
            drift_check_seconds=self.BASE_DRIFT_CHECK_SECONDS,
        )
        self._base_drift_check_at = new_check_at
        if message is not None:
            if paused and not self._integration_paused:
                try:
                    _cur = self.base_repo.current_branch()
                except Exception:
                    _cur = "?"
                self._debug(f"base branch drift: repo on '{_cur}', integration target '{self._base_branch}'")
                self._set_message(message, seconds=30.0)
            elif not paused and self._integration_paused:
                self._debug(f"base branch restored to '{self._base_branch}'; integration resumed")
                self._set_message(message, seconds=8.0)
            self._integration_paused = paused
            self._render()

    def _warn_if_base_edited(self) -> None:
        # Fallback for un-sandboxed platforms: detect the agent editing the base
        # repo (its working tree gaining uncommitted changes beyond the startup
        # baseline) and warn, since those edits bypass aGiT's worktree tracking.
        if not self._monitor_base_edits:
            return
        now = time.monotonic()
        if now - self._base_check_at < self.BASE_EDIT_CHECK_SECONDS:
            return
        self._base_check_at = now
        try:
            current = set(self.base_repo.status_short().splitlines())
        except Exception:
            return
        new = current - self._base_status_baseline
        if new:
            files = ", ".join(sorted(line[3:] for line in list(new) if len(line) > 3)[:5])
            self._set_message(
                f"Agent edited the base repo, outside its worktree ({files}). These "
                "changes are not tracked by aGiT — move them into the worktree.",
                seconds=12.0,
            )
            self._base_status_baseline = current  # don't repeat for the same files
            self._render()

    def _poll_base_advanced(self) -> None:
        # aGiT advances the base itself (integration sets `_base_advanced`), but the
        # base branch can also gain commits out of band — the user commits directly
        # to it, pulls, rebases, etc. Poll its HEAD on a throttle and, when it moves
        # for any reason, flag a sync so idle worktrees pick the new commits up
        # (`_sync_idle_worktrees_to_base`). The first observation only records the
        # baseline; it never triggers on startup.
        if self.worktree is None or self._base_branch is None:
            return
        now = time.monotonic()
        if now - self._base_poll_at < self.BASE_POLL_SECONDS:
            return
        self._base_poll_at = now
        try:
            head = self.base_repo.rev_parse(self._base_branch)
        except Exception as error:
            self._debug(f"base-head poll failed: {error!r}")
            return
        if self._last_base_head is not None and head != self._last_base_head:
            self._base_advanced = True
        self._last_base_head = head
        self._prune_user_declined()  # keep the status-line count current

    def _warn_if_cwd_drifted(self) -> None:
        # `claude --resume` can restore a session's *saved* working directory and
        # ignore the worktree aGiT launched it in (Claude Code issue #58591). When
        # that happens the agent works in the wrong directory: its turns aren't
        # tracked here and writes outside the worktree are sandbox-blocked. Detect
        # it from the cwd the backend records, and warn once with how to recover.
        if self._cwd_drift_checked:
            return
        if self.worktree is None:
            return
        now = time.monotonic()
        if now - self._cwd_check_at < self.CWD_CHECK_SECONDS:
            return
        self._cwd_check_at = now
        fn = getattr(self.backend, "recorded_working_dir", None)
        if fn is None:
            self._cwd_drift_checked = True  # backend doesn't record a cwd
            return
        try:
            recorded = fn(self.state.backend_session_id)
        except Exception as error:
            self._debug(f"cwd drift check failed: {error!r}")
            return
        if not recorded:
            return  # nothing recorded yet — check again next tick
        self._cwd_drift_checked = True
        if os.path.realpath(recorded) == os.path.realpath(str(self.repo.repo)):
            return  # on the worktree, as intended
        self._debug(f"cwd drift: backend recorded {recorded}, worktree is {self.repo.repo}")
        self._set_message(
            f"⚠ The agent is working in:\n    {recorded}\n"
            f"not this session's worktree:\n    {self.repo.repo}\n"
            "This is Claude's resume-cwd bug (#58591): turns made there are NOT committed "
            "by aGiT, and edits outside the worktree are blocked by the sandbox.\n"
            f"To recover: {self._menu_label()} → session → start a NEW session (it launches fresh in the "
            "worktree) and re-send your request there; resuming this conversation will keep "
            "landing in the wrong directory. Any work already done in the other directory "
            "stays there — move it into the worktree by hand if you need it tracked.",
            seconds=30.0,
        )
        self._render()

    # ------------------------------------------------------------------
    # Self-update (#: check periodically, apply once sessions are finished)
    # ------------------------------------------------------------------

    def _update_checks_enabled(self) -> bool:
        gc = getattr(self, "global_config", None)
        return bool(getattr(gc, "check_for_updates", True)) if gc is not None else False

    def _merge_session_active(self) -> bool:
        # True while a merge / conflict resolution is in progress in ANY session
        # (the active one, or a background session integrating its turn). Update
        # prompts and updates are suppressed for the duration so they never
        # interrupt a merge the user is in the middle of.
        if getattr(self, "merge_ctx", None) is not None:
            return True
        return any(getattr(session, "merge_ctx", None) is not None for session in getattr(self, "sessions", []))

    def _maybe_check_for_update(self) -> None:
        # Kick off a background self-update check on a throttle, and surface a
        # finished one. Network I/O (`git fetch`) runs on a worker thread so the
        # terminal never stalls; the result is handed back and consumed here on
        # the main thread.
        if self._merge_session_active():
            # Don't prompt mid-merge; a result that lands now stays pending and
            # is surfaced once the merge is done.
            return
        self._consume_update_check_result()
        if self._updater is None or not self._update_checks_enabled():
            return
        if self._update_pending or self._update_applying:
            return  # already decided / in progress — stop nagging
        if self._update_check_thread is not None and self._update_check_thread.is_alive():
            return
        now = time.monotonic()
        if self._update_check_at and now - self._update_check_at < self.UPDATE_CHECK_SECONDS:
            return
        self._update_check_at = now
        updater = self._updater

        def worker() -> None:
            try:
                self._update_worker_result = updater.check()
            except Exception as error:  # never let a check crash the worker
                self._debug(f"update check failed: {error!r}")
                self._update_worker_result = None

        self._update_worker_result = None
        self._update_check_thread = threading.Thread(target=worker, daemon=True, name="agit-update-check")
        self._update_check_thread.start()

    def _consume_update_check_result(self) -> None:
        thread = self._update_check_thread
        if thread is None or thread.is_alive():
            return
        result = self._update_worker_result
        self._update_check_thread = None
        self._update_worker_result = None
        if result is None or not result.ok:
            return
        self._update_status = result
        if result.available and not self._update_offered:
            # First time we have seen this update: prompt the user (a status-bar
            # notice pointing at the `update` command, so we don't seize the
            # screen mid-keystroke).
            self._update_offered = True
            self._set_message(
                f"{result.message}\n{self._menu_label()} → 'update' to install it when your sessions finish.",
                seconds=12.0,
            )
            self._render()

    def _ready_for_update(self) -> bool:
        # "All sessions finished and commits are in": nothing is mid-turn,
        # mid-parse, mid-merge, or mid-summary anywhere. The actual commit +
        # integration of finished work is flushed by _finalize_pending_work()
        # right before the update is applied.
        if self._merge_session_active():
            return False
        if getattr(self, "agent_in_flight", False):
            return False
        if getattr(self, "agent_parse_active", False):
            return False
        if getattr(self, "pending_forwarded", None) or getattr(self, "pending_prompt_text", ""):
            return False
        if getattr(self, "_summary_pending", None) is not None:
            return False
        summary_thread = getattr(self, "_summary_thread", None)
        if summary_thread is not None and summary_thread.is_alive():
            return False
        if self._running_background_session_names():
            return False
        return True

    def _maybe_apply_pending_update(self) -> None:
        if not self._update_pending or self._update_applying:
            return
        if not self._ready_for_update():
            return
        self._apply_update_and_restart()

    def _apply_update_and_restart(self) -> None:
        # Commit + integrate every session's finished work (same path as exit),
        # install the update, then ask run()'s teardown to re-exec aGiT.
        self._update_applying = True
        self._update_pending = False
        self._set_message("Finishing commits, then updating aGiT…", seconds=30.0)
        self._render()
        try:
            self._finalize_pending_work()
        except Exception as error:  # don't let a commit hiccup strand the update
            self._debug(f"finalize before update failed: {error!r}")
        result = self._updater.apply()
        if not result.ok:
            self._update_applying = False
            self._finalized_on_exit = False  # allow a later clean exit to finalize again
            self._exiting = False
            self._set_message(f"aGiT update failed: {result.error}", seconds=12.0)
            self._render()
            return
        # Success: stop the loop and let run()'s finally restore the terminal and
        # release the lock before _pending_restart triggers the re-exec.
        self._set_message(f"{result.message} Restarting aGiT…", seconds=10.0)
        self._render()
        self._exit_child()
        self._pending_restart = True
        self.running = False

    def _handle_update_command(self) -> None:
        # Ctrl-G → "update": show the current update status and let the user opt
        # in (applied once sessions finish), postpone, or stop update checks.
        if self._update_applying:
            self._set_message("An aGiT update is already in progress.")
            self._render()
            return
        status = self._update_status
        if status is None:
            # No completed check yet — make sure one is running and ask the user
            # to retry, rather than blocking the UI on a network fetch.
            self._update_check_at = 0.0
            self._maybe_check_for_update()
            self._set_message("Checking for aGiT updates… run 'update' again in a moment.")
            self._render()
            return
        if not status.ok:
            self._set_message(f"Update check failed: {status.error}")
            self._render()
            return
        if not status.available:
            self._set_message(f"aGiT is up to date ({status.current or 'current'}).")
            self._render()
            return
        choice = self._select_popup(
            status.message,
            ["Update when sessions finish", "Not now", "Stop checking for updates"],
        )
        if choice == "Stop checking for updates":
            if self.global_config is not None:
                self.global_config.check_for_updates = False
            self._set_message("aGiT will no longer check for updates.")
            self._render()
            return
        if choice != "Update when sessions finish":
            self._set_message("Update postponed.")
            self._render()
            return
        self._update_pending = True
        if self._ready_for_update():
            self._apply_update_and_restart()
        else:
            self._set_message(
                "aGiT will update and restart once all sessions finish and commits are in.",
                seconds=8.0,
            )
            self._render()

    def _confine_to_worktree(self, command: list[str]) -> list[str]:
        # Wrap the backend so it can only write inside its session worktree (plus
        # the repo's .git), not the base repo it lives in. A no-op when there is
        # no worktree, or when confinement is disabled / unavailable (the loop
        # then warns if the base working tree is touched).
        if self.worktree is None:
            return command
        if not self.global_config.sandbox:
            return command
        base = self.base_repo
        if base is None:
            return command
        return sandbox.wrap_command(command, base=str(base.repo), worktree=str(self.repo.repo))

    def _should_continue_session(self) -> bool:
        session_id = self.state.backend_session_id
        if not session_id:
            return False
        if self.state.backend_session_matches_repo():
            return True
        return self.backend.session_belongs_to_repo(self.repo.repo, session_id)

    def _teardown_child(self) -> None:
        # Dispatch through the bound method (not BackendProcess.cleanup directly)
        # so subclass/test overrides of _cleanup_child keep applying to teardown.
        self._cleanup_child()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.child_pid = None

    def _reset_agent_tracking(self) -> None:
        self.agent_in_flight = False
        self.agent_parse_thread = None
        self.agent_parse_result = None
        self.agent_parse_active = False
        self.pending_forwarded = None
        self.pending_prompt_text = ""
        self.passthrough_prompt.clear()
        self.passthrough_escape = None
        self.last_status = ""
        self.parse_pending = False
        self.status_check_pending = False
        # Re-detect mouse ownership for the new backend: OpenCode enables mouse
        # (so wheel events are forwarded to it), Claude does not (so aGiT keeps
        # the wheel for scrollback). Without this reset, switching OpenCode→Claude
        # would leave child_mouse stuck True and break Claude's scrollback.
        self.child_mouse = False
        self.scroll_back = 0

    def _restart_agent(self, message: str) -> None:
        # Tear down the running TUI and relaunch it for the current backend and
        # session state, re-baselining so existing history is not re-committed.
        self._teardown_child()
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._resize_child()
        # Re-assert host mouse reporting for the new backend so wheel scrollback
        # keeps working regardless of what the previous backend left behind.
        self._enable_host_mouse()
        self._set_message(message)
        self._render()

    def _live_session_for_backend(self, name: str) -> int | None:
        # Index of a live session currently running the given backend (the active
        # session's live backend is on self; background ones on their snapshot).
        for index in range(len(self.sessions)):
            backend = self.backend if index == self.active_index else getattr(self.sessions[index], "backend", None)
            if getattr(backend, "name", None) == name:
                return index
        return None

    def _switch_backend(self, name: str) -> None:
        if not backend_installed(name):
            self._set_message(f"'{name}' is not installed.\n{install_hint(name)}", seconds=8.0)
            self._render()
            return
        if name == getattr(self.backend, "name", None):
            self._set_message(f"Already using {name}.")
            self._render()
            return
        self.global_config.default_backend = name
        if self.worktree is None:
            # A non-worktree session has nothing to multiplex; restart the single
            # backend in place (legacy behaviour).
            self.state.remember_backend_session()
            self.state.backend = name
            self.backend = make_proxy_agent(name)
            self.state.backend_session_id = self.state.stored_backend_session(name)
            self.state.last_backend_message_id = None
            self.state.clear_trace()
            self._restart_agent(f"Backend set to {name}")
            return
        # Keep the current backend's session running in the background and switch
        # to this backend's own session.
        index = self._live_session_for_backend(name)
        if index is not None:
            self._switch_active(index)
            return
        # Resume this backend's last conversation if we remember one (recreating
        # its worktree at the same path so the backend finds its transcript).
        record = self._recall_backend_session(name)
        if record and record.get("id"):
            self._new_session(
                record.get("worktree") or self._next_session_name(), backend=name, resume_session_id=record["id"]
            )
            return
        # Otherwise start fresh, confirming the session name first.
        session_name = self._prompt_session_name(f"New {name} session", default=self._next_session_name())
        if session_name is None:
            self._set_message("Cancelled.")
            self._render()
            return
        self._new_session(session_name, backend=name)

    def _persisted_session_names(self) -> set[str]:
        # Sanitized names recorded for past conversations — both the durable
        # repo-root record and the worktree directory names the backend still
        # knows. A new session must not reuse one of these: resuming that past
        # conversation later recreates its worktree at worktree_path(name), so a
        # collision would put two backends in one directory.
        return {_sanitize_name(name) for name in self._agit_named_sessions().values() if name}

    def _taken_session_names(self) -> set[str]:
        # Sanitized names already in use: live sessions, on-disk worktrees, and
        # names already claimed by a past (named/dormant) conversation.
        used: set[str] = set()
        try:
            used.update(_sanitize_name(info.name) for info in self._worktrees().list())
        except Exception:
            pass
        used.update(_sanitize_name(self._session_name(index)) for index in range(len(self.sessions)))
        used.update(self._persisted_session_names())
        return used

    def _next_session_name(self) -> str:
        # The next free ``session-N`` name, avoiding existing worktrees and live
        # sessions (session names are independent of which backend they run).
        used = self._taken_session_names()
        number = 1
        while f"session-{number}" in used:
            number += 1
        return f"session-{number}"

    def _session_name_taken(self, name: str) -> bool:
        return _sanitize_name(name) in self._taken_session_names()

    def _prompt_session_name(self, title: str, *, default: str) -> str | None:
        # Ask for a session name, rejecting duplicates (a session and its worktree
        # are 1:1, so names must be unique). Returns the chosen name, or None on
        # cancel / empty input.
        prompt = "Name for the new session (its own git worktree):"
        while True:
            name = self._prompt_popup(title, prompt, default=default)
            if name is None or not name.strip():
                return None
            name = name.strip()
            if self._session_name_taken(name):
                prompt = f"'{name}' is already in use. Choose a different name:"
                default = self._next_session_name()
                continue
            return name

    def _recall_backend_session(self, backend: str) -> dict | None:
        try:
            root = AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            return root.recall_session(backend)
        except Exception as error:
            self._debug(f"recall backend session failed: {error!r}")
            return None

    def _remember_session_for_backend(self) -> None:
        # Persist the current session's conversation under its backend in the
        # durable repo-root state, so switching back to that backend later resumes
        # it (its worktree is recreated at the same path).
        info = self.worktree
        if info is None:
            return
        try:
            root = AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            root.remember_session(
                self.state.backend,
                session_id=self.state.backend_session_id,
                worktree=info.name,
                message_id=self.state.last_backend_message_id,
                model=self.state.model,
            )
            # Remember a user-given name (not an auto session-N) keyed by the
            # backend conversation id, so resuming it later restores the name.
            if info.name and not self._AUTO_NAME_RE.match(info.name):
                root.name_session(self.state.backend_session_id, info.name)
        except Exception as error:
            self._debug(f"remember backend session failed: {error!r}")

    def _persist_session_name(self, session_id: str | None) -> None:
        # Link this session's user-given name to its backend conversation id in
        # the durable repo-root record as soon as the id is known — and again
        # whenever the backend forks a new id — not only on clean exit. Waiting
        # for exit strands the name under a stale id (or never records it) when
        # the worktree is kept, aGiT crashes, or the conversation id drifts
        # across resumes, leaving the session unnamed in the resume list.
        name = self.name
        if not session_id or not name or self._AUTO_NAME_RE.match(name):
            return
        try:
            root = AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            if root.session_name_for(session_id) != name:
                root.name_session(session_id, name)
        except Exception as error:
            self._debug(f"persist session name failed: {error!r}")

    # --- live-session multiplexer ---

    def _worktrees(self) -> WorktreeManager:
        if self.worktree_manager is None:
            self.worktree_manager = WorktreeManager(self.base_repo)
        return self.worktree_manager

    def _apply_new_session_if_requested(self) -> None:
        # `agit --new-session`: start a fresh backend conversation (don't resume)
        # and mint a new aGiT session id.
        if not self._force_new_session:
            return
        self.state.backend_session_id = None
        self.state.last_backend_message_id = None
        self.state.new_agit_session_id()

    def _turn_from_branch(self, branch: str) -> int:
        return self._integration.turn_from_branch(branch)

    def _setup_base_merge_only_session(self) -> None:
        # Move the initial session into its own worktree so the base tree is only
        # ever advanced by integration. Reuses an existing worktree (resuming a
        # previous run) or creates a fresh one; falls back to running on the base
        # tree (legacy behaviour, no auto-integration) if neither is possible.
        if not self._use_worktrees:
            # #9: opt-out — run on the current branch directly (worktree stays
            # None; all the `worktree is None` paths commit straight to it).
            self._set_message(
                "Running without a worktree: the agent edits this branch directly (visible live), "
                "but there's no isolation or auto-integration. Don't run multiple sessions this way "
                "— they'd all write the same tree.",
                seconds=12.0,
            )
            return
        root_state = self.state  # the durable repo-root "last session" record
        backend_name = root_state.backend
        prior_message_id = root_state.last_backend_message_id
        prior_model = root_state.model
        prior_worktree = (root_state.recall_session(backend_name) or {}).get("worktree")
        # Which conversation to continue at startup: aGiT's own last session if we
        # have one, otherwise the repo's most recent backend conversation (e.g. one
        # you ran with plain claude/opencode before aGiT). Resume is by id, which
        # the backend resolves regardless of which directory it runs in.
        if self._force_new_session:
            resume_id = None
        else:
            resume_id = root_state.backend_session_id or self._repo_latest_session_id()
        name = self._resolve_startup_session_name(root_state, resume_id, prior_worktree)
        try:
            info, repo = self._open_session_worktree(name)
        except Exception as error:
            self._debug(f"base-merge-only setup failed; running on the base tree: {error!r}")
            return
        self.name = info.name
        self._primary_worktree_name = info.name  # the session that auto-resumes across exits
        self.worktree = info
        self.repo = repo
        self._align_session_to_base(repo)
        self.turn = self._turn_from_branch(repo.current_branch())
        self.state = AgitState(info.path, default_backend=self.global_config.default_backend)
        self.state.backend = backend_name
        # The worktree is recreated fresh each run (its working state is not kept),
        # so seed its resume pointer to continue the chosen conversation.
        if not self.state.backend_session_id and resume_id:
            if prior_model:
                self.state.model = prior_model
            self.state.last_backend_message_id = prior_message_id
            self.state.backend_session_id = resume_id  # setter records this worktree as its repo
        self.backend = make_proxy_agent(backend_name)
        self.actions = AgitActions(self.repo, self.state, verbose=self.verbose)

    _AUTO_NAME_RE = re.compile(r"^session-\d+$")

    def _repo_latest_session_id(self) -> str | None:
        # The conversation a bare `claude -c` / `opencode` would continue in the
        # repo aGiT was launched in (its most recent recorded session).
        try:
            return self.backend.latest_session_id(self.base_repo.repo)
        except Exception as error:
            self._debug(f"latest_session_id failed: {error!r}")
            return None

    def _resolve_startup_session_name(self, root_state, resume_id, prior_worktree) -> str:
        # Keep the conversation's existing (user-given) name if it has one; only
        # prompt when there is no real name yet. Auto `session-N` names don't count.
        existing = root_state.session_name_for(resume_id)
        if not existing and prior_worktree and not self._AUTO_NAME_RE.match(prior_worktree):
            # The name only lived in the last-session record; key it by the
            # conversation id too so it stays linked once that record moves on.
            existing = prior_worktree
            if resume_id:
                root_state.name_session(resume_id, existing)
        if existing:
            return existing
        name = self._prompt_startup_name(resume_id is not None)
        if resume_id:
            root_state.name_session(resume_id, name)
        return name

    def _startup_taken_names(self) -> set[str]:
        # Sanitized names already claimed when the primary session is set up
        # (self.sessions is not built yet): on-disk worktrees plus names recorded
        # for past (named/dormant) conversations, so a fresh session can't reuse
        # the name of one the user may resume later.
        try:
            used = {_sanitize_name(info.name) for info in self._worktrees().list()}
        except Exception:
            used = set()
        used |= self._persisted_session_names()
        return used

    def _first_free_session_name(self) -> str:
        # `session-N` not already taken by a worktree or a past conversation.
        used = self._startup_taken_names()
        number = 1
        while f"session-{number}" in used:
            number += 1
        return f"session-{number}"

    def _prompt_startup_name(self, continuing: bool) -> str:
        # Pre-reactor, cooked mode: the alt-screen has not been entered yet so
        # the terminal is still in line-buffered cooked mode.  Using input()
        # here is intentional — the reactor loop is not running, so there are
        # no PTY fds to drain, and the simple cooked readline is the right tool.
        # Do NOT convert this to a modal; modals require the reactor to be live.
        default = self._first_free_session_name()
        print("Continuing a conversation that has no name yet." if continuing else "Starting a new session.")
        taken = self._startup_taken_names()
        while True:
            try:
                raw = input(f"Name this session [{default}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return default
            name = raw or default
            if _sanitize_name(name) in taken:
                print(f"'{name}' is already in use. Choose a different name.")
                continue
            return name

    def _align_session_to_base(self, repo: GitRepo) -> None:
        # Bring a clean, idle session worktree up to date with the base after the
        # base branch gained commits (another session integrated, or a previous
        # run). Two cases, by whether the worktree has its own committed work:
        #   * No unintegrated commits → re-point (detach) it onto the current
        #     base, so it works from, and later integrates into, the branch the
        #     user launched from — not whatever branch an earlier run left current.
        #   * Has its own work → merge the new base commits into its turn branch so
        #     it stays current, but only when that merges cleanly; a conflicting
        #     base is backed out and left for the session's own integration to
        #     surface. (Direction base → worktree; the reverse is integration.)
        # A worktree mid-merge or with uncommitted changes is left untouched.
        if self._base_branch is None:
            return
        try:
            outcome = self._integration.align_session_to_base(repo)
        except Exception as error:
            self._debug(f"align to base failed: {error!r}")
            return
        if outcome.startswith("merged:"):
            branch = outcome[len("merged:") :]
            self._debug(f"merged base '{self._base_branch}' into session branch {branch}")
        elif outcome.startswith("conflict:"):
            branch = outcome[len("conflict:") :]
            self._debug(f"base '{self._base_branch}' conflicts with {branch}; left for integration")
        elif outcome == "repointed":
            self._debug(f"re-pointed session worktree to current base '{self._base_branch}'")

    def _worktree_has_pending_work(self, repo: GitRepo, branch: str) -> bool:
        # Pending = uncommitted changes, or commits on its branch not yet in base.
        return self._integration.worktree_has_pending_work(repo, branch)

    def _cleanup_stale_state_on_startup(self) -> None:
        # A non-graceful exit (the backend dying, SIGKILL, a crash) can leave junk
        # that makes the *next* run misbehave until a second restart — chiefly:
        #   • prunable git worktree registrations whose directories are gone, and
        #   • orphaned worktree *directories* that are no longer valid worktrees
        #     (e.g. only a `.agit/` recreated by a write after teardown).
        # git's own `worktree list` can't see the orphaned dirs, so sweep the
        # filesystem under the worktrees root and delete anything that isn't a real,
        # registered worktree. Valid dormant worktrees (kept for resume) are
        # registered, so they're left untouched. Runs before the primary session is
        # set up so startup is clean from the first launch, not the second.
        try:
            self.base_repo.worktree_prune()
        except Exception as error:
            self._debug(f"startup prune failed: {error!r}")
        try:
            worktrees = self._worktrees()
            root = worktrees.root
            if not root.is_dir():
                return
            registered = set()
            for info in worktrees.list():
                try:
                    registered.add(info.path.resolve())
                except OSError:
                    pass
            removed = 0
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if resolved in registered or self._is_valid_worktree(entry):
                    continue  # a real worktree — keep it
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                self._debug(f"startup cleanup: removed orphaned worktree dir {entry}")
            if removed:
                self.base_repo.worktree_prune()  # removing dirs may strand registrations
        except Exception as error:
            self._debug(f"startup cleanup failed: {error!r}")

    def _reconcile_sessions_on_startup(self) -> None:
        # Clean up worktrees left by previous runs: integrate any pending commits
        # into the base, then delete the worktree. The Claude conversation itself
        # persists (keyed by the worktree path) and stays resumable from the
        # session list, so nothing of value is lost. Worktrees whose work cannot
        # be merged cleanly (a conflict, or uncommitted changes) are kept and
        # flagged for the user to resolve.
        if self.worktree is None:
            return
        active_pending = False
        try:
            active_pending = self._worktree_has_pending_work(self.repo, self.repo.current_branch())
        except Exception:
            pass
        flagged: list[str] = []
        try:
            infos = self._worktrees().list()
        except Exception:
            infos = []
        for info in infos:
            if info.name == self.name:  # the active session, handled below
                continue
            try:
                if not self._cleanup_stale_worktree(info):
                    flagged.append(info.name)
            except Exception as error:
                self._debug(f"reconcile skipped '{info.name}': {error!r}")
        self._delete_orphan_merged_branches()
        notes: list[str] = []
        if active_pending:
            notes.append(f"this session ('{self.name}') has commits to integrate")
        if flagged:
            notes.append(f"{len(flagged)} stale session(s) need attention: {', '.join(flagged)}")
        if notes:
            self._set_message(
                "⚠ " + "; ".join(notes) + f".\nUse {self._menu_label()} → session (s) to handle them.",
                seconds=12.0,
            )

    def _cleanup_stale_worktree(self, info) -> bool:
        # Integrate a dormant worktree's pending commits (if any) and delete it.
        # Returns False (keep + flag) when it has uncommitted changes or its work
        # conflicts with the base and so needs the user/agent to resolve.
        result = self._integration.cleanup_stale_worktree(info, self._worktrees())
        if result:
            self._debug(f"cleaned stale worktree '{info.name}'")
        return result

    def _delete_orphan_merged_branches(self) -> None:
        # Remove agit/* branches that no worktree checks out and that are already
        # contained in the base branch (stale leftovers).
        try:
            for branch in self._integration.delete_orphan_merged_branches():
                self._debug(f"deleted stale merged branch {branch}")
        except Exception as error:
            self._debug(f"orphan branch cleanup failed: {error!r}")

    def _integrate_session_turn(self) -> None:
        # Integrate the current session's just-committed turn branch into the
        # base branch: merge base into the turn branch (in the session's
        # worktree), then fast-forward the base to it and start the next turn
        # branch. When it cannot fast-forward (the base gained conflicting work
        # from another session), surface the resolve options box and let the
        # user choose how to handle it.
        result = self._integrate_turn_or_conflict()
        if result == "conflict" and not self._exiting:
            # On exit there is no UI to drive a resolution; the work stays on its
            # branch for the next startup / session menu to surface.
            self._prompt_resolve_conflict(self.repo.current_branch())

    def _integrate_turn_or_conflict(self) -> str:
        # Try to fast-forward the current session's turn branch into the base.
        # Returns "integrated" (base advanced), "conflict" (the merge was backed
        # out and needs resolution), or "skip" (nothing to integrate).
        if self.worktree is None or self._base_branch is None or self.merge_ctx:
            return "skip"
        if self._integration_paused:
            return "skip"  # base switched out-of-band; merging is paused
        turn_branch = self.repo.current_branch()
        if not turn_branch.startswith("agit/"):
            return "skip"
        try:
            if not self.repo.merge(self._base_branch):
                # Back out of the conflicted merge so the worktree is clean again;
                # the chosen resolution path re-starts the merge from here.
                self.repo.merge_abort()
                return "conflict"
            self._advance_base_to(turn_branch)
            self._debug(f"integrated '{self.name}' {turn_branch} -> {self._base_branch}")
            return "integrated"
        except Exception as error:
            self._debug(f"integration failed for '{self.name}': {error!r}")
            return "skip"

    def _prompt_resolve_conflict(self, source_branch: str) -> None:
        # A finished turn cannot fast-forward into the base because it conflicts
        # with work another session already integrated. Surface the same resolve
        # options the session menu offers for a conflicting session, labelled
        # with this session and its backend, then dispatch the choice.
        backend = getattr(self.backend, "name", "?")
        choice = self._select_popup(
            f"Session '{self.name}' ({backend}) finished, but its changes conflict with '{self._base_branch}'.",
            [
                "Merge automatically (agent resolves conflicts)",
                "Merge manually (you resolve here, then Complete merge)",
                f"Leave for later (resolve via {self._menu_label()} → session)",
            ],
        )
        if not choice or choice.startswith("Leave"):
            self._set_message(
                f"'{self.name}' has unintegrated work that conflicts with '{self._base_branch}'. "
                f"Resolve it any time via {self._menu_label()} → session.",
                seconds=8.0,
            )
            self._render()
            return
        self._start_merge_for_active(auto=choice.startswith("Merge automatically"))

    def _integrate_session_on_exit(self) -> None:
        # Clean up the current session's branch when aGiT exits: integrate any
        # committed-but-unintegrated work into the base, then detach and delete
        # the turn branch (an empty one is dropped too). Conflicts / dirty trees
        # are left intact for the next startup to surface.
        if self.worktree is None or self._base_branch is None:
            return
        try:
            self._integration.integrate_session_on_exit(self.repo, self.merge_ctx)
        except Exception as error:
            self._debug(f"exit integration failed for '{self.name}': {error!r}")

    def _advance_base_to(self, source_branch: str) -> None:
        # The source branch now contains the base plus this session's work; move
        # the base to it, then detach the worktree at the new base and delete the
        # turn branch. A fresh turn branch is created lazily on the next commit,
        # so a fully-merged session leaves no branch behind — only its worktree,
        # whose conversation context can still be resumed.
        # Hard safety: never fast-forward if the base repo was checked out onto a
        # different branch out-of-band — `git merge --ff-only` advances whatever is
        # currently checked out, so this would move the WRONG branch. Callers wrap
        # this in try/except and treat the failure as "not integrated".
        self._integration.advance_base_to(self.repo, source_branch)
        # The base moved; other idle sessions should fast-forward onto it.
        self._base_advanced = True

    def _sync_idle_worktrees_to_base(self) -> None:
        # Keep every idle session's worktree current with the (just-advanced) base:
        # a session with nothing of its own is re-pointed onto the base, and one
        # that has its own committed work has the new base commits merged into its
        # turn branch (cleanly, or skipped on conflict) — see `_align_session_to_base`.
        # Only idle, clean worktrees are touched, so in-flight work is left alone.
        if self._integration_paused:
            return  # base switched out-of-band; don't re-point worktrees meanwhile
        repo: GitRepo | None
        for index in range(len(self.sessions)):
            if index == self.active_index:
                repo, in_flight = self.repo, self.agent_in_flight
            else:
                snapshot = self.sessions[index]
                repo, in_flight = getattr(snapshot, "repo", None), getattr(snapshot, "agent_in_flight", False)
            if repo is None or in_flight:
                continue
            self._align_session_to_base(repo)

    def _ensure_turn_branch(self) -> None:
        # A merged-and-detached session sits at base between turns. Before its
        # next commit, put it on a fresh turn branch (this preserves the working
        # tree, so the agent's changes carry over).
        # Recovery paths can reset the turn counter (e.g. a recreated worktree
        # starts detached at base, so the counter restarts at 0) while earlier
        # turn branches still exist — deliberately kept when they hold
        # unintegrated commits. Never reuse such a name: resetting it would
        # destroy that work. Skip to the next free turn number instead.
        if self.worktree is None or not self.repo.is_detached():
            return
        new_turn = self._integration.ensure_turn_branch(
            repo=self.repo,
            worktree=self.worktree,
            turn=self.turn,
            worktree_manager=self._worktrees(),
            session_name=self.name,
            backend_name=self.backend.name,
        )
        self.turn = new_turn

    def _merge_resolution_prompt(self, files: list[str], context: str) -> str:
        # Delegates to IntegrationService.merge_resolution_prompt indirectly;
        # kept as a runner method because tests mock it directly.
        listing = ", ".join(files) if files else "the conflicted files"
        commits = context.replace("\n", "; ") if context else "(none recorded)"
        return (
            f"[aGiT] Merge conflict: the base branch '{self._base_branch}' gained changes from another "
            f"session that conflict with your work in {listing}. The conflicting base commits are: {commits}. "
            "Please open the conflicted files, resolve every <<<<<<< / ======= / >>>>>>> marker keeping both "
            "changes' intent, and save. Do NOT run git or commit — aGiT will create the merge commit once you are done."
        )

    def _inject_prompt(self, text: str) -> None:
        # Type a synthesized single-line prompt into the active backend, then
        # submit it with a separate Enter a beat later. Backends like Claude Code
        # use bracketed paste, where a trailing "\r" in the same write is treated
        # as a newline inside the box rather than a submit; sending Enter as its
        # own keystroke once the text has settled reliably submits the prompt.
        if self.master_fd is None:
            return
        payload = " ".join(text.split()).encode("utf-8", errors="replace")
        try:
            self.active.process.write(payload)
        except OSError:
            return  # text never reached the backend; don't schedule its Enter
        self._pending_enter_at = time.monotonic() + 0.4
        self._pending_enter_fd = self.master_fd  # submit to THIS backend, not whatever is active later

    def _flush_pending_enter(self) -> None:
        # Submit a previously-injected prompt once its text has settled. The Enter
        # is sent to the PTY the text was typed into, even if the active session
        # changed in the meantime, so it never lands in another backend.
        if self._pending_enter_at is None or time.monotonic() < self._pending_enter_at:
            return
        self._pending_enter_at = None
        fd = self._pending_enter_fd
        self._pending_enter_fd = None
        if fd is None:
            return
        try:
            BackendProcess(fd).write(b"\r")
        except OSError:
            return  # Enter never reached the backend; the merge gate stays closed
        if self.merge_ctx is not None and self.master_fd == fd:
            self.merge_ctx["prompt_sent_at"] = time.monotonic()
            if self.merge_ctx.phase is MergePhase.PENDING:
                self.merge_ctx.phase = MergePhase.RESOLVING

    def _begin_agent_merge(self, source_branch: str) -> None:
        # A merge is in progress (conflicted) in the worktree. Ask the session's
        # agent to resolve it; aGiT finalizes once the conflicts are gone.
        assert self._base_branch is not None  # a merge only starts once the base is established
        files = self.repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self._base_branch, paths=files)
        except Exception:
            context = ""
        self._inject_prompt(self._merge_resolution_prompt(files, context))
        self.merge_ctx = MergeContext(
            source_branch=source_branch,
            context=context,
            phase=MergePhase.PENDING,
            auto_tried=False,
            prompt_sent_at=None,  # set once the submit Enter goes out
        )
        self.agent_in_flight = True
        self._set_message(
            f"Merge conflict in {', '.join(files) or 'this session'} — asking the agent to resolve it… "
            f"aGiT will commit the merge once the agent finishes (or use {self._menu_label()} → session → Complete merge).",
            seconds=12.0,
        )
        self._render()

    def _note_backend_session_change(self, new_session_id: str | None) -> None:
        # Keep the durable name record pointing at the conversation this session
        # is actually running (ids drift when the backend forks on resume).
        self._persist_session_name(new_session_id)
        # If the worktree's active conversation changed to a different backend
        # session that aGiT didn't start, the user likely started it from inside
        # the backend. Warn once that such sessions share this branch.
        previous = self.state.backend_session_id
        if (
            self.worktree is not None
            and previous
            and new_session_id
            and new_session_id != previous
            and not self._warned_backend_session
        ):
            self._warned_backend_session = True
            self._set_message(
                "Detected a new conversation started inside the backend. Its changes are tracked on "
                f"this session's branch. To get a separate branch, start sessions with {self._menu_label()} → session → New.",
                seconds=12.0,
            )

    def _maybe_complete_agent_merge(self) -> None:
        # Auto-finalize a pending agent merge only after the agent has actually
        # engaged with the injected prompt (produced output after we submitted
        # it) and then gone idle — never before the prompt has even been sent.
        # MANUAL contexts (phase == MANUAL, auto_tried == True) are never
        # auto-finalized; should_auto_complete_merge gates them via auto_tried.
        ctx = self.merge_ctx
        if not ctx:
            return
        if not self._integration.should_auto_complete_merge(ctx, self.last_child_output, self.CHILD_IDLE_SECONDS):
            return
        ctx.auto_tried = True  # prevent a second attempt
        self._finalize_agent_merge()

    def _finalize_agent_merge(self) -> bool:
        ctx = self.merge_ctx
        if not ctx:
            return False
        try:
            success, message = self._integration.finalize_agent_merge(
                self.repo,
                ctx,
                session_name=self.name,
                agit_session_id=self.state.session_id,
                backend_name=self.backend.name,
                backend_session_id=self.state.backend_session_id,
            )
        except Exception as error:
            self._debug(f"finalize agent merge failed: {error!r}")
            return False  # merge_ctx intentionally kept — user can retry
        if success is False and message is None:
            # merge not in progress — already resolved/aborted elsewhere
            self.merge_ctx = None
            return False
        if message and not success:
            self._set_message(message, seconds=10.0)
            return False
        if success:
            self.merge_ctx = None
            self.agent_in_flight = False
            self._base_advanced = True  # sync idle worktrees after base advanced
            self._set_message(message, seconds=6.0)
            self._render()
            return True
        return False

    def _session_name(self, index: int) -> str:
        if index == self.active_index:
            return self.name or f"session{index}"
        return getattr(self.sessions[index], "name", None) or f"session{index}"

    def _session_status(self, index: int) -> str:
        # "running" = the backend is actively working (a turn is in flight or it
        # produced output recently); otherwise "idle" (waiting for input).
        if index == self.active_index:
            in_flight, last = self.agent_in_flight, self.last_child_output
        else:
            session = self.sessions[index]
            in_flight = getattr(session, "agent_in_flight", False)
            last = getattr(session, "last_child_output", 0.0)
        working = in_flight or (last and time.monotonic() - last < self.CHILD_IDLE_SECONDS)
        return "running" if working else "idle"

    def _handle_session_command(self, arg: str) -> None:
        # Ctrl-G then "session": manage the live concurrent sessions.
        arg = arg.strip()
        if arg in {"new", "fresh"}:
            self._prompt_new_session()
        elif arg.isdigit():
            self._switch_active(int(arg) - 1)
        else:
            self._session_menu()

    def _handle_summarizer_command(self, arg: str) -> None:
        sub = arg.strip().lower()
        if sub in ("on", "off"):
            enabled = sub == "on"
            self.state.summarization_enabled = enabled
            self._set_message(f"Summarizer {'enabled' if enabled else 'disabled'}.")
            self._render()
            return
        if sub == "model":
            current = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            new_model = self._prompt_popup(
                "Summarizer Model",
                f"Current: {current}\nEnter model (empty to clear):",
                default=self.state.summarization_model or "",
            )
            if new_model is not None:
                self.state.summarization_model = new_model.strip() or None
                self._set_message(f"Summarizer model: {self.state.summarization_model or '(same as session)'}")
            self._render()
            return
        enabled = self._summarization_enabled()
        model = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
        choice = self._select_popup(
            "Summarizer",
            [
                f"Toggle ({'ON' if enabled else 'OFF'})",
                f"Set model (current: {model})",
            ],
        )
        if choice is None:
            self._render()
            return
        if choice.startswith("Toggle"):
            self.state.summarization_enabled = not enabled
            self._set_message(f"Summarizer {'enabled' if not enabled else 'disabled'}.")
        elif choice.startswith("Set model"):
            self._handle_summarizer_command("model")
            return
        self._render()

    # --- switch base branch ---

    def _base_switch_candidates(self) -> list[str]:
        # User branches the base could switch to (never aGiT's transient ones).
        return self._integration.base_switch_candidates()

    def _switch_base_command(self, arg: str = "") -> None:
        if self.worktree is None or self._base_branch is None:
            self._set_message("Base switching is unavailable for this session.")
            self._render()
            return
        candidates = self._base_switch_candidates()
        if not candidates:
            self._set_message("No other branches to switch the base to.")
            self._render()
            return
        target = (
            arg.strip()
            if arg.strip() in candidates
            else self._select_popup(f"Switch base from '{self._base_branch}' to:", candidates)
        )
        if not target:
            self._set_message("Cancelled.")
            self._render()
            return
        confirm = self._select_popup(
            f"Switch base to '{target}'? aGiT integrates every session's work into "
            f"'{self._base_branch}' first, then re-points them at '{target}'.",
            ["No, cancel", "Yes, switch base"],
        )
        if confirm != "Yes, switch base":
            self._set_message("Cancelled.")
            self._render()
            return
        self._perform_base_switch(target)

    def _session_unintegrated(self, repo) -> bool:
        # True if a session still has work that did not make it into the base.
        return self._integration.session_unintegrated(repo)

    def _unintegrated_session_names(self) -> list[str]:
        blocked: list[str] = []
        repo: GitRepo | None
        for index in range(len(self.sessions)):
            if index == self.active_index:
                repo, name = self.repo, self.name
            else:
                session = self.sessions[index]
                repo, name = getattr(session, "repo", None), self._session_name(index)
            if self._session_unintegrated(repo):
                blocked.append(name)
        return blocked

    def _perform_base_switch(self, new_base: str) -> None:
        self._exiting = True
        self._set_message("Integrating session work before switching base…", seconds=30)
        self._render()
        self._finalize_pending_work()  # commit + integrate every session into the current base
        blocked = self._unintegrated_session_names()
        if blocked:
            self._exiting = False
            self._set_message(
                f"Cannot switch base: unresolved work in {', '.join(blocked)}. "
                f"Resolve it ({self._menu_label()} → session), then try again.",
                seconds=14.0,
            )
            self._render()
            return
        try:
            self.base_repo.switch(new_base)
        except Exception as error:
            self._exiting = False
            self._set_message(f"Could not switch base to '{new_base}': {error}", seconds=10.0)
            self._render()
            return
        self._base_branch = new_base
        self._integration.base_branch = new_base
        self._repoint_all_sessions_to_base()
        self._exiting = False
        self._set_message(
            f"Base switched to '{new_base}'. Existing sessions keep running and now merge into it.",
            seconds=8.0,
        )
        self._render()

    def _repoint_current_to_base(self) -> None:
        # Detach the current session's worktree at the new base so its next turn
        # branches from there. The session and its conversation keep running.
        try:
            new_turn = self._integration.repoint_to_base(self.repo, self.worktree)
        except Exception as error:
            self._debug(f"re-point failed for '{self.name}': {error!r}")
            return
        if new_turn is not None:
            self.turn = new_turn

    def _repoint_all_sessions_to_base(self) -> None:
        # Re-point every live session at the new base without stopping any of them.
        self._repoint_current_to_base()  # active, in place
        for session in self.sessions:
            if session is self.active:
                continue
            self._with_session(session, self._repoint_current_to_base)

    def _session_menu(self) -> None:
        options: list[str] = []
        actions: list[tuple[str, object]] = []
        if self.merge_ctx or (self.worktree is not None and self.repo.merge_in_progress()):
            options.append("✓ Complete merge for this session")
            actions.append(("complete-merge", None))
        live_names = set()
        for index, session in enumerate(self.sessions):
            live_names.add(self._session_name(index))
            marker = "* " if index == self.active_index else "  "
            backend = getattr(getattr(session, "backend", None), "name", "?")
            label = f"{marker}{self._session_name(index)} [{self._session_status(index)}] ({backend})"
            if index == self.active_index and not self.merge_ctx and self._active_has_pending():
                label += " — commits to integrate"
            options.append(label)
            actions.append(("switch", index))
        for info in self._dormant_worktrees(live_names):
            if self._dormant_has_pending(info):
                options.append(f"  {info.name} [unmerged changes — resolve]")
                actions.append(("resolve", info.name))
            else:
                options.append(f"  {info.name} [idle — resume]")
                actions.append(("resume", info.name))
        options.append("+ New session (own worktree)")
        actions.append(("new", None))
        if self._resumable_sessions():
            options.append("↻ Resume a past conversation…")
            actions.append(("resume-past", None))
        if len(self.sessions) > 1:
            options.append("- Stop a session")
            actions.append(("stop", None))
        choice = self._select_popup("Sessions", options)
        if choice is None:
            self._set_message("Cancelled.")
            self._render()
            return
        kind, value = actions[options.index(choice)]
        if kind == "switch":
            assert isinstance(value, int)  # "switch" pairs with a session index
            if value == self.active_index:
                self._integrate_active_session()
            else:
                self._switch_active(value)
        elif kind == "resume-past":
            self._resume_session_menu()
        elif kind == "complete-merge":
            self._finalize_agent_merge()
        elif kind == "resolve":
            assert isinstance(value, str)  # "resolve" pairs with a worktree name
            self._resolve_dormant_worktree(value)
        elif kind == "resume":
            assert isinstance(value, str)  # "resume" pairs with a worktree name
            self._new_session(value)
        elif kind == "new":
            self._prompt_new_session()
        else:
            self._stop_session_menu()

    def _active_has_pending(self) -> bool:
        # True if the active session has committed work not yet in the base.
        return self._integration.active_has_pending(self.repo, self.worktree)

    def _integrate_active_session(self) -> None:
        # Selecting the current session offers to integrate its outstanding
        # commits (the "merge box"), since there is nothing to switch to.
        if self.worktree is None or self._base_branch is None:
            self._set_message("This session has no worktree to integrate.")
            self._render()
            return
        if self.repo.has_changes():
            self._set_message(
                "Finish or stop the current turn before integrating — the worktree has uncommitted changes.",
                seconds=8.0,
            )
            self._render()
            return
        if not self._active_has_pending():
            self._set_message(f"'{self.name}' has nothing to integrate.")
            self._render()
            return
        # Try a clean / fast-forward integration first — if the work merges without
        # conflicts there is nothing to resolve, so the agent is never involved and
        # the user is not asked. Only a real conflict surfaces the resolve options.
        result = self._integrate_turn_or_conflict()
        if result == "integrated":
            self._set_message(f"Integrated '{self.name}' into {self._base_branch} (no conflicts).", seconds=6.0)
            self._render()
            return
        if result == "conflict":
            self._prompt_resolve_conflict(self.repo.current_branch())
            return
        self._set_message(f"'{self.name}' has nothing to integrate.")
        self._render()

    def _resolve_dormant_worktree(self, name: str) -> None:
        choice = self._select_popup(
            f"Session '{name}' has unmerged changes",
            [
                "Merge automatically (agent resolves conflicts)",
                "Merge manually (you resolve in the session)",
                "Discard this session's changes",
            ],
        )
        if choice is None:
            self._set_message("Cancelled.")
            self._render()
            return
        if choice.startswith("Discard"):
            confirm = self._select_popup(
                f"Discard ALL un-integrated changes in '{name}'? This cannot be undone.",
                ["No, keep it", "Yes, discard"],
            )
            if confirm == "Yes, discard":
                self._worktrees().remove(name)
                self._set_message(f"Discarded session '{name}'.")
            else:
                self._set_message("Kept.")
            self._render()
            return
        # Both merge paths relaunch the session, then start the merge in it.
        self._new_session(name)
        self._start_merge_for_active(auto=choice.startswith("Merge automatically"))

    def _start_merge_for_active(self, *, auto: bool) -> None:
        # Begin merging base into the (now active) session's branch.
        outcome, ctx, message = self._integration.start_merge(
            repo=self.repo,
            name=self.name,
            worktree=self.worktree,
            auto=auto,
        )
        if outcome == "skip":
            return
        if outcome == "error":
            self._set_message(message, seconds=8.0)
            self._render()
            return
        if outcome == "clean":
            # advance_base_to was already called inside the service; set _base_advanced.
            self._base_advanced = True
            self._set_message(message, seconds=6.0)
            self._render()
            return
        # conflict_auto or conflict_manual: ctx is a MergeContext
        assert ctx is not None
        if outcome == "conflict_auto":
            # Re-enter _begin_agent_merge to inject the prompt (it uses self.repo etc.)
            self._begin_agent_merge(ctx.source_branch)
        else:  # conflict_manual
            self.merge_ctx = ctx
            self._set_message(message, seconds=12.0)
            self._render()

    RESUME_LIST_LIMIT = 20

    def _worktree_sessions(self) -> list:
        # (worktree-name, SessionRef) for every conversation this backend recorded
        # under any of the repo's aGiT worktrees — including ones whose worktree
        # has since been removed (the backend keeps the transcript keyed by path
        # or id). The worktree directory name IS the session's user-given name, so
        # this is how a named session — and its name — survives across runs even
        # after its worktree is gone.
        try:
            return list(self.backend.list_worktree_sessions(self._worktrees().root))
        except Exception as error:
            self._debug(f"list_worktree_sessions failed: {error!r}")
            return []

    def _resumable_sessions(self) -> list:
        # Past conversations to offer for resume: those the backend recorded in
        # the base repo (a plain claude/opencode run, or --no-worktree mode) plus
        # every conversation recorded under an aGiT worktree of this repo. The
        # worktree ones are the named sessions, and they are surfaced here even
        # once their worktree has been emptied on quit — otherwise a named session
        # that ran in a worktree (i.e. every normal session) would vanish from the
        # list on the next run. Resuming is by session id, which the backend
        # resolves regardless of directory.
        try:
            refs = list(self.backend.list_sessions(self.base_repo.repo))
        except Exception as error:
            self._debug(f"list_sessions failed: {error!r}")
            refs = []
        seen = {ref.id for ref in refs}
        for _name, ref in self._worktree_sessions():
            if ref.id not in seen:
                seen.add(ref.id)
                refs.append(ref)
        refs = sorted(refs, key=lambda ref: getattr(ref, "updated", 0) or 0, reverse=True)
        return refs[: self.RESUME_LIST_LIMIT]

    def _agit_named_sessions(self) -> dict:
        # Friendly names for conversations aGiT itself created/named, keyed by
        # backend session id. Used only to label/resume the list that comes from
        # the backend. Two sources, in precedence order:
        #   1. the durable repo-root record (user-given names, which follow id
        #      drift across resumes), then
        #   2. the worktree directory name a conversation ran in — which is the
        #      session's name — recovered for any conversation the record missed
        #      (its id drifted before being linked, or it was never persisted).
        names: dict = {}
        record = None
        try:
            root = AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            names.update({str(k): str(v) for k, v in (root.data.get("session_names") or {}).items() if v})
            record = root.recall_session(self.backend.name)
        except Exception:
            record = None
        if record and record.get("id") and record.get("worktree"):
            names.setdefault(str(record["id"]), str(record["worktree"]))
        for name, ref in self._worktree_sessions():
            if name:
                names.setdefault(str(ref.id), name)
        return names

    def _resume_session_menu(self) -> None:
        sessions = self._resumable_sessions()
        if not sessions:
            self._set_message("No past conversations found to resume.")
            self._render()
            return
        live_ids = {getattr(getattr(s, "state", None), "backend_session_id", None) for s in self.sessions}
        names = self._agit_named_sessions()
        options: list[str] = []
        for ref in sessions:
            mark = "● " if ref.id in live_ids else "  "
            label = (names.get(ref.id) or ref.label or "(no prompt recorded)").strip()[:48]
            options.append(f"{mark}{_short_session(ref.id)}  {self._format_age(ref.updated)}  {label}")
        choice = self._select_popup("Resume a conversation (newest first)", options)
        if choice is None:
            self._set_message("Cancelled.")
            self._render()
            return
        ref = sessions[options.index(choice)]
        self._resume_conversation(names.get(ref.id) or self._next_session_name(), ref.id)

    def _resume_conversation(self, name: str, session_id: str) -> None:
        # If this conversation is already live, just switch to it; otherwise
        # create a worktree for it and resume the backend by id there.
        for index, session in enumerate(self.sessions):
            if getattr(getattr(session, "state", None), "backend_session_id", None) == session_id:
                self._switch_active(index)
                return
        if self._live_session_name_taken(name):
            # The chosen name is occupied by a different live session — resume
            # under a fresh name so the two don't share a worktree (which would
            # run two backends in one directory).
            name = self._next_session_name()
        self._new_session(name, resume_session_id=session_id)

    def _live_session_name_taken(self, name: str) -> bool:
        sanitized = _sanitize_name(name)
        return any(_sanitize_name(self._session_name(index)) == sanitized for index in range(len(self.sessions)))

    def _format_age(self, updated: float) -> str:
        delta = max(0, int(time.time() - (updated or 0)))
        for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
            if delta >= size:
                return f"{delta // size}{unit} ago"
        return "just now"

    def _dormant_worktrees(self, live_names: set) -> list:
        # Worktrees that exist on disk but are not currently live sessions. These
        # are kept across restarts so their conversation context can be resumed,
        # whether or not they still carry unmerged work.
        try:
            return [info for info in self._worktrees().list() if info.name not in live_names]
        except Exception:
            return []

    def _dormant_has_pending(self, info) -> bool:
        # True if a dormant worktree still has work to integrate into the base.
        try:
            repo = GitRepo(info.path)
            return self._worktree_has_pending_work(repo, repo.current_branch())
        except Exception:
            return True  # err toward offering the resolve flow

    def _prompt_new_session(self) -> None:
        name = self._prompt_session_name("New Session", default=self._next_session_name())
        if name is None:
            self._set_message("Cancelled.")
            self._render()
            return
        self._new_session(name)

    def _stop_session_menu(self) -> None:
        options = [self._session_name(index) for index in range(len(self.sessions))]
        choice = self._select_popup("Stop which session?", options)
        if choice is None:
            self._set_message("Cancelled.")
            self._render()
            return
        self._stop_session(options.index(choice))

    def _switch_active(self, index: int) -> None:
        if not (0 <= index < len(self.sessions)) or index == self.active_index:
            return
        self._join_parse_worker_before_swap()
        # Swap under the outgoing session's parse lock: if the join above timed
        # out, the still-running worker writes its result to its owning Session
        # under this same lock, so it sees either fully-before or fully-after.
        lock = self.agent_parse_lock or threading.Lock()
        with lock:
            self.active = self.sessions[index]
        self.scroll_back = 0
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Switched to session '{self._session_name(index)}'")
        self._render()

    def _join_parse_worker_before_swap(self) -> None:
        # A parse worker started for the active session reads that session's
        # backend/repo and must not straddle a session swap. The export normally
        # takes well under a second; wait for it rather than racing it.
        thread = self.agent_parse_thread
        if thread is not None and thread.is_alive():
            self._set_message("Finishing this session's transcript export...")
            self._render()
            thread.join(timeout=10)

    def _pump_background(self, session: Session) -> None:
        # Keep a background session's screen current by draining + feeding its
        # output. No render and no commit here — committing happens separately in
        # _service_background_sessions (synchronously, via _with_session), since
        # the async parse worker is not safe to run on a background session.
        if session not in self.sessions:
            return
        saved = self.active
        self.active = session
        died = False
        try:
            output = self._drain_child_output()
            if output is None:
                died = True
            elif output:
                session.last_child_output = time.monotonic()
                self._answer_terminal_queries(output)
                self._feed_child_output(output)
        finally:
            self.active = saved
        if died:
            self._stop_session(self.sessions.index(session), commit=False)

    def _ensure_worktree_alive(self) -> None:
        if self.worktree is None:
            return
        if self.worktree.path.exists():
            return
        self._debug(f"Worktree '{self.name}' directory gone; recovering...")
        self._teardown_child()
        self._stop_file_watcher()
        try:
            self.base_repo._run(["git", "worktree", "prune"], check=False)
        except Exception:
            pass
        backend_name = self.state.backend
        try:
            info, repo = self._open_session_worktree(self.name)
        except Exception as error:
            self._debug(f"worktree recovery failed: {error!r}; falling back to base tree")
            self.worktree = None
            self.name = "main"
            self.repo = self.base_repo
            self.state = AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            self.state.backend = backend_name
            self.backend = make_proxy_agent(backend_name)
            self.actions = AgitActions(self.repo, self.state, verbose=self.verbose)
            self._reset_agent_tracking()
            self._sanitize_state_trace()
            self._initialize_session_baseline()
            self._init_screen()
            self._spawn()
            self._start_file_watcher()
            self._resize_child()
            self._enable_host_mouse()
            self._set_message("Worktree was deleted externally; now tracking base repo.", seconds=8.0)
            self._render()
            return
        self.name = info.name
        self.worktree = info
        self.repo = repo
        self.turn = self._turn_from_branch(repo.current_branch())
        self.state = AgitState(info.path, default_backend=self.global_config.default_backend)
        self.state.backend = backend_name
        self.backend = make_proxy_agent(backend_name)
        self.actions = AgitActions(self.repo, self.state, verbose=self.verbose)
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Worktree was deleted externally; recreated '{info.name}'.", seconds=8.0)
        self._render()

    def _with_session(self, session: Session, fn):
        # Run fn with `session` as the runner's active session: runner methods
        # (and the compat attribute layer) then read/write that session's state
        # directly on its own Session object. Pointer re-assignment only — no
        # field copying — so mutations made by fn persist on the session.
        if session not in self.sessions:
            return None
        saved = self.active
        self.active = session
        try:
            return fn()
        finally:
            self.active = saved

    def _commit_and_integrate_background(self) -> str:
        # A clean tree can still need the commit pipeline: the backend may have
        # committed its own work, which the parse then amends with the
        # trace/metadata before integration (#35).
        if self.repo.has_changes() or self._uncovered_backend_commits():
            self._commit_latest_turn_sync()
        self._clear_agent_in_flight_if_idle()
        return self._integrate_turn_or_conflict()

    def _service_background_sessions(self) -> None:
        # Integrate background sessions as soon as they go idle, so finished work
        # lands in the base without waiting to be switched to. A background
        # session whose finished turn cannot fast-forward is brought to the
        # foreground and its resolve options box is surfaced (session + backend).
        if self.merge_ctx is not None:
            return
        now = time.monotonic()
        for index in range(len(self.sessions)):
            if index == self.active_index:
                continue
            session = self.sessions[index]
            if getattr(session, "merge_ctx", None) is not None:
                self._with_session(session, self._maybe_complete_agent_merge)
                continue
            if now - getattr(session, "last_child_output", 0.0) < self.CHILD_IDLE_SECONDS:
                continue
            if now - getattr(session, "last_poll", 0.0) < self.POLL_SECONDS:
                continue
            session.last_poll = now
            if self._with_session(session, self._commit_and_integrate_background) == "conflict":
                self._switch_active(index)
                self._prompt_resolve_conflict(self.repo.current_branch())
                return

    def _open_session_worktree(self, name: str) -> tuple[WorktreeInfo, GitRepo]:
        worktrees = self._worktrees()
        path = worktrees.worktree_path(name)
        if self._is_valid_worktree(path):
            repo = GitRepo(path)
            return WorktreeInfo(name=path.name, path=path, branch=repo.current_branch()), repo
        # A leftover, *invalid* dir (e.g. only `.agit/` recreated by a write after
        # the previous run tore the worktree down) would otherwise make GitRepo
        # fail and drop us into a fresh session — the "first restart starts new,
        # second restart resumes" off-by-one. Clear it so `create` (which prunes
        # first) can re-add the worktree cleanly.
        if path.exists():
            self._debug(f"removing invalid leftover worktree dir {path}")
            shutil.rmtree(path, ignore_errors=True)
        info = worktrees.create(name, base=self._base_branch or self.base_repo.current_branch())
        return info, GitRepo(info.path)

    def _is_valid_worktree(self, path) -> bool:
        # A real linked worktree has a `.git` file pointing at its gitdir and a
        # resolvable current branch; a torn-down leftover (only `.agit/`) has not.
        if not (path / ".git").exists():
            return False
        try:
            GitRepo(path).current_branch()
            return True
        except Exception:
            return False

    def _new_session(self, name: str, *, resume_session_id: str | None = None, backend: str | None = None) -> None:
        if not self._use_worktrees:
            # #9: concurrent sessions need worktrees; in no-worktree mode they'd
            # all write the same tree. Refuse rather than corrupt the checkout.
            self._set_message(
                "New sessions need worktrees, which are off (--no-worktree). "
                "Restart without --no-worktree to run multiple sessions.",
                seconds=8.0,
            )
            self._render()
            return
        try:
            info, repo = self._open_session_worktree(name)
        except Exception as error:
            self._set_message(f"Could not create worktree: {error}", seconds=8.0)
            self._render()
            return
        # Fresh per-session runtime state; the outgoing active session keeps
        # its state on its own Session object in self.sessions.
        self.active = Session.bare()
        self.name = info.name
        self.worktree = info
        self.repo = repo
        self.turn = self._turn_from_branch(repo.current_branch())
        self.state = AgitState(info.path, default_backend=self.global_config.default_backend)
        if backend:
            # Pin this session to a specific backend (e.g. created by switching
            # backends), independent of the global default.
            self.state.backend = backend
        if resume_session_id:
            # Resume this exact backend conversation (its transcript lives under
            # the worktree path, which we have just recreated/reused).
            self.state.backend_session_id = resume_session_id
            self._persist_session_name(resume_session_id)
        self.backend = make_proxy_agent(self.state.backend)
        self.actions = AgitActions(self.repo, self.state, verbose=self.verbose)
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        self.sessions.append(self.active)
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Started session '{info.name}' in .agit/worktrees/{info.path.name}")
        self._render()

    def _stop_session(self, index: int, *, commit: bool = True) -> None:
        if not (0 <= index < len(self.sessions)):
            return
        if len(self.sessions) <= 1:
            self._set_message("Cannot stop the only session.")
            self._render()
            return
        session = self.sessions[index]
        active = session is self.active
        if active:
            # Don't let an in-flight parse worker outlive its session's runtime
            # and write into whichever session becomes active next.
            self._join_parse_worker_before_swap()
        saved = self.active
        self.active = session
        try:
            if commit:
                self._commit_latest_turn_sync()
            self._stop_file_watcher()
            if self.child_pid:
                try:
                    os.kill(self.child_pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                self._note_pid_for_reaping(self.child_pid)
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
            worktree = self.worktree
        finally:
            self.active = saved
        # Leave the worktree's branch on disk (recoverable); just drop the live session.
        self._debug(f"stopped session index={index} worktree={getattr(worktree, 'path', None)}")
        self.sessions.pop(index)
        if active:
            self.active = self.sessions[min(index, len(self.sessions) - 1)]
            self.scroll_back = 0
            self._resize_child()
            self._enable_host_mouse()
        self._render()

    def _handle_active_session_exit(self) -> bool:
        # The active backend's PTY closed. If other sessions remain, drop this
        # one and switch to another; otherwise tell the loop to stop.
        if len(self.sessions) <= 1:
            return False
        self._stop_file_watcher()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.master_fd = None
        self.child_pid = None
        index = self.active_index
        self.sessions.pop(index)
        self.active = self.sessions[min(index, len(self.sessions) - 1)]
        self.scroll_back = 0
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Session ended; switched to '{self._session_name(self.active_index)}'")
        self._render()
        return True

    def _relaunch_backend_or_exit(self) -> bool:
        # The only session's backend process exited on its own — most often Claude
        # quitting when you Esc its native session picker, where Claude restores
        # the terminal and terminates (its shutdown sequence is in the debug log).
        # aGiT should stay up and relaunch+resume the conversation rather than
        # quitting with it. Guard against a crash loop: if the backend keeps dying
        # quickly, stop relaunching and exit normally.
        now = time.monotonic()
        recent = [t for t in self._relaunch_times if now - t < 12.0]
        if len(recent) >= 3:
            self._debug("backend exited 3x within 12s; quitting instead of relaunching")
            self._finalize_on_backend_exit()
            return False
        recent.append(now)
        self._relaunch_times = recent
        self.child_pid = None  # already gone
        try:
            # _restart_agent tears down the dead PTY, re-baselines (so existing
            # history is not re-committed) and respawns; _spawn resumes the same
            # conversation via _should_continue_session.
            self._restart_agent("Backend exited — relaunched and resumed (Ctrl-C to quit aGiT).")
        except Exception as error:
            self._debug(f"relaunch failed, exiting: {error!r}")
            self._finalize_on_backend_exit()
            return False
        return True

    def _finalize_on_backend_exit(self) -> None:
        # The only session's backend process is gone and we are NOT relaunching
        # (e.g. a real exit, or a crash loop). Commit/integrate its last turn and
        # persist the resume pointer before aGiT leaves, instead of just dropping
        # out of the loop (which would lose the last commit and leave resume
        # pointing at a stale session).
        self.child_pid = None  # already gone; don't signal a dead process
        try:
            self._finalize_pending_work()
        except Exception as error:
            self._debug(f"finalize on backend exit failed: {error!r}")

    def _start_new_session(self) -> None:
        self.state.backend_session_id = None
        self.state.last_backend_message_id = None
        self.state.clear_trace()
        self._restart_agent("Started a new session.")

    def _switch_to_session(self, session_id: str) -> None:
        if session_id == self.state.backend_session_id:
            self._set_message(f"Already tracking session {_short_session(session_id)}.")
            self._render()
            return
        self.state.backend_session_id = session_id
        self.state.last_backend_message_id = None
        self.state.clear_trace()
        self._restart_agent(f"Now tracking session {_short_session(session_id)}")

    def _sync_tracked_session(self) -> None:
        # Re-point tracking at the most recently active session without
        # relaunching the TUI, e.g. after the user started a new session inside
        # the backend itself.
        refs = self.backend.list_sessions(self.repo.repo)
        if not refs:
            self._set_message("No sessions found to sync.")
            self._render()
            return
        # Prefer sessions with content (a label is the first real user prompt):
        # Claude mints a fresh EMPTY session id on resume/picker actions, which
        # is newest by mtime but has nothing to track — adopting it blanks the
        # next restart (same trap claude_session.latest_session_id avoids).
        resumable = [ref for ref in refs if ref.label]
        newest = max(resumable or refs, key=lambda ref: ref.updated)
        if newest.id == self.state.backend_session_id:
            self._set_message(f"Already tracking the most recent session ({_short_session(newest.id)}).")
            self._render()
            return
        self.state.backend_session_id = newest.id
        self.state.last_backend_message_id = None
        self._initialize_session_baseline()
        self._set_message(f"Now tracking session {_short_session(newest.id)}")
        self._render()

    def _start_file_watcher(self) -> None:
        if Observer is None:
            return
        observer = Observer()
        observer.schedule(
            RepoChangeHandler(self.repo.repo, self.file_change_event), str(self.repo.repo), recursive=True
        )
        observer.start()
        self.file_observer = observer

    def _stop_file_watcher(self) -> None:
        observer = self.file_observer
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=2.0)
        self.file_observer = None

    def _background_fds(self) -> dict:
        # master_fd -> session object, for every session that is not the active
        # one. Keyed by object (not index) so that removing a session that has
        # died does not invalidate the other entries.
        mapping = {}
        for index, session in enumerate(self.sessions):
            if index == self.active_index:
                continue
            fd = getattr(session, "master_fd", None)
            if fd is not None:
                mapping[fd] = session
        return mapping

    def _loop(self) -> int:
        # Main event loop.  Each iteration is decomposed into five named reactor
        # phases; the phases communicate via a simple sentinel convention:
        #   "continue"  → restart the while loop immediately (skip later phases)
        #   "break"     → leave the while loop (clean exit)
        #   int         → leave the while loop and return that exit code
        #   None        → proceed to the next phase as usual
        assert self.master_fd is not None
        while self.running:
            # --- phase 1: select ------------------------------------------
            background, readable = self._reactor_select_phase()
            # --- phase 2: pty-output --------------------------------------
            sentinel: str | int | None = self._reactor_pty_output_phase(readable)
            if sentinel == "continue":
                continue
            if sentinel == "break":
                break
            # --- phase 3: stdin -------------------------------------------
            sentinel = self._reactor_stdin_phase(readable)
            if sentinel == "continue":
                continue
            if sentinel == "break":
                break
            # --- phase 4: timers / background tasks -----------------------
            self._reactor_timers_phase()
            # --- phase 5: child-exit --------------------------------------
            sentinel = self._reactor_child_exit_phase()
            if sentinel == "continue":
                continue
            if isinstance(sentinel, int):
                return sentinel
        return 0

    # ------------------------------------------------------------------
    # Reactor phases (called exclusively from _loop)
    # ------------------------------------------------------------------

    def _reactor_select_phase(self) -> tuple[dict, list]:
        """Phase 1 — compute the fd set, block in select, drain background PTYs.

        Returns (background_fds_map, readable_list).  Background sessions are
        drained here so their PTY buffers never fill up regardless of which
        phase the main loop is in.
        """
        timeout = 0.016 if self._render_pending else 0.2
        background = self._background_fds()
        readable, _, _ = select.select([sys.stdin.fileno(), self.master_fd, *background], [], [], timeout)
        for fd in readable:
            if fd in background:
                self._pump_background(background[fd])
        return background, readable

    def _reactor_pty_output_phase(self, readable: list) -> "str | None":
        """Phase 2 — drain and process the active session's PTY output.

        Returns a loop-control sentinel or None to continue normally.
        """
        if self.master_fd not in readable:
            return None
        output = self._drain_child_output()
        if output is None:
            sample = self.last_child_output_sample[-2048:].decode(errors="replace").replace("\x1b", "\\x1b")
            self._debug(f"master_fd closed (backend gone); last_output={sample!r}")
            self._raw_capture("EOF", b"")
            if self._handle_active_session_exit():
                return "continue"
            if self._relaunch_backend_or_exit():
                return "continue"
            return "break"
        if output:
            self._raw_capture("<", output)
            self.last_child_output = time.monotonic()
            self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
            self._answer_terminal_queries(output)
            self._sync_terminal_modes(output)
            self._track_sync_update(output)
            self._feed_child_output(output)
            self._render_output()
        return None

    def _reactor_stdin_phase(self, readable: list) -> "str | None":
        """Phase 3 — read stdin and route bytes to the active session or handler.

        Returns a loop-control sentinel or None to continue normally.
        """
        if sys.stdin.fileno() not in readable:
            return None
        data = os.read(sys.stdin.fileno(), 4096)
        self._raw_capture(">", data)
        # Any keypress dismisses a sticky message (e.g. the auto-commit
        # confirmation) so it no longer overlays the live view; repaint to
        # remove the popup even if the key produces no child echo.
        if self._clear_sticky_message_on_input():
            self._render_pending = True
        data = self._input_tail + data
        data, self._input_tail = self._hold_incomplete_tail(data)
        data = self._intercept_scroll(data)
        was_capturing = self.input.capturing
        forwarded, local_echo, command, should_exit = self.input.feed(data)
        if should_exit:
            # One shared flow (also reachable from inside popups): a
            # second Ctrl-C during the confirmation popup exits
            # immediately but still finalizes pending work first.
            if self._run_exit_flow():
                return "break"
            self._render()
            return "continue"
        if local_echo:
            self._render_status(local_echo.decode(errors="ignore"))
        if self.input.capturing:
            self._render()
        elif was_capturing and command is None:
            self._render()
        if forwarded:
            self.scroll_back = 0  # interacting snaps back to the live view
            submit = self._forwarded_submits(forwarded)
            self._update_passthrough_prompt(forwarded)
            submitted_prompt = ""
            if submit:
                submitted_prompt = self.passthrough_prompt.decode(errors="ignore").strip()
                if submitted_prompt.startswith("/compact"):
                    self._handle_pre_compaction()
                if not self._pre_agent_commit_if_needed(submitted_prompt):
                    self.pending_forwarded = [chunk for chunk in forwarded if chunk in {b"\r", b"\n"}]
                    self.pending_prompt_text = submitted_prompt
                    forwarded = [chunk for chunk in forwarded if chunk not in {b"\r", b"\n"}]
                    submit = False
            if submit:
                self.passthrough_prompt.clear()
                self.passthrough_escape = None
            if forwarded:
                if submit:
                    self.agent_in_flight = True
                    if submitted_prompt:
                        # A new prompt starts a turn on its own branch.
                        self._ensure_turn_branch()
                self.active.process.write(b"".join(forwarded))
        if command:
            self._run_command(command)
        return None

    def _reactor_timers_phase(self) -> None:
        """Phase 4 — flush pending renders, deferred enters, and all background tasks."""
        self._flush_pending_render()
        self._flush_pending_enter()
        if self.merge_ctx:
            # A merge is being resolved; don't make normal commits meanwhile.
            self._maybe_complete_agent_merge()
        else:
            self._check_base_branch_drift()  # pause merging before any integration runs
            self._resume_pending_prompt_if_ready()
            self._ensure_worktree_alive()
            self._service_commit_summary()  # apply finished background summaries (#8)
            self._service_precompact_summary()
            self._maybe_agent_commit()
            self._service_background_sessions()
            self._poll_base_advanced()
            self._warn_if_base_edited()
            self._warn_if_cwd_drifted()
            self._maybe_check_for_update()
            self._maybe_apply_pending_update()
        if self._base_advanced:
            self._base_advanced = False
            self._sync_idle_worktrees_to_base()

    def _reactor_child_exit_phase(self) -> "str | int | None":
        """Phase 5 — reap stopped children and check whether the active child exited.

        Returns a loop-control sentinel (``"continue"``, an ``int`` exit code),
        or ``None`` to proceed normally.
        """
        self._reap_stopped_children()  # collect SIGINT'd backends as they exit
        if self.child_pid is not None:
            done, status = os.waitpid(self.child_pid, os.WNOHANG)
            if done:
                exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
                sample = self.last_child_output_sample[-512:].decode(errors="replace").replace("\x1b", "\\x1b")
                self._debug(
                    f"child exited pid={self.child_pid} status={status} exit_code={exit_code} last_output={sample!r}"
                )
                # Same handling as the master_fd-EOF path: switch away (multi
                # session) or relaunch+resume (single) so Claude exiting its own
                # picker on Esc doesn't take aGiT down. These two detectors race;
                # whichever sees the exit first must relaunch.
                if self._handle_active_session_exit():
                    return "continue"
                if self._relaunch_backend_or_exit():
                    return "continue"
                return exit_code
        return None

    def _drain_child_output(self) -> bytes | None:
        # Delegate to the session-owned BackendProcess for the bounded PTY read loop.
        return self.active.process.drain()

    def _diag_path(self, kind: str):
        # Diagnostic logs live in the *base* repo's .agit/ (one file per run), not
        # the session worktree's — the worktree is removed on exit, which would
        # both destroy the log and recreate a half-dir that breaks the next resume.
        base = self.base_repo
        root = (base.repo if base is not None else self.repo.repo) / ".agit"
        run = self._diag_run or time.strftime("%Y%m%d-%H%M%S")
        return root / f"{kind}-{run}.log"

    def _debug(self, message: str) -> None:
        if not self.debug_proxy:
            return
        try:
            path = self._diag_path("proxy-debug")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
        except OSError:
            pass

    def _raw_capture(self, tag: str, data: bytes) -> None:
        # Append a raw I/O chunk (child output "<", user input ">", or "EOF") for
        # byte-exact replay of an interactive glitch.
        if not self.raw_capture:
            return
        try:
            path = self._diag_path("proxy-raw")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S.')}{int(time.time() * 1000) % 1000:03d} {tag} {data!r}\n")
        except OSError:
            pass

    def _sanitize_state_trace(self) -> None:
        CommitEngine(self.repo, self.state, debug_fn=self._debug).sanitize_state_trace(self.backend)

    def _recover_nonempty_session(self):
        # When the recorded conversation turns out empty, fall back to this
        # worktree's newest conversation that has real content. Returns
        # (session_id, ExportedSession) or None if nothing resumable exists.
        return CommitEngine(self.repo, self.state, debug_fn=self._debug).recover_nonempty_session(
            self.backend, self.repo, self._stage_backend_resume
        )

    def _initialize_session_baseline(self) -> None:
        CommitEngine(self.repo, self.state, debug_fn=self._debug).initialize_session_baseline(
            self.backend,
            self.repo,
            should_continue_fn=self._should_continue_session,
            stage_backend_resume_fn=self._stage_backend_resume,
            debug_fn=self._debug,
        )

    def _mirror_session_to_base(self, session_id: str | None) -> None:
        # Link an aGiT-born conversation's transcript into the base repo's project
        # dir so a plain CLI run in the repo root can see/continue it. Idempotent
        # (no-op once linked, or when the session already lives at the base, or for
        # backends without per-directory storage).
        fn = getattr(self.backend, "mirror_to_base", None)
        if fn is None or not session_id or self.worktree is None:
            return
        try:
            fn(self.base_repo.repo, self.repo.repo, session_id)
        except Exception as error:
            self._debug(f"mirror_to_base failed: {error!r}")

    def _stage_backend_resume(self, session_id: str | None) -> None:
        fn = getattr(self.backend, "ensure_resumable", None)
        if fn is None or not session_id:
            return
        try:
            if not fn(self.repo.repo, session_id):
                self._debug(f"resume transcript not found for {session_id}")
        except Exception as error:
            self._debug(f"ensure_resumable failed: {error!r}")

    def _init_screen(self) -> None:
        self.rows, self.cols = self._terminal_size()
        # HistoryScreen keeps lines that scroll off the top so aGiT can offer
        # scrollback for backends that stream to the normal screen (Claude).
        self.screen = _BackgroundColorEraseScreen(self.cols, max(self.rows - 1, 1), history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.scroll_back = 0
        self._in_sync_update = False

    def _feed_child_output(self, output: bytes) -> None:
        ScreenRenderer.feed(self, output, pyte_hostile_csi_re=_PYTE_HOSTILE_CSI_RE)

    def _sync_terminal_modes(self, output: bytes) -> None:
        # OpenCode enables mouse reporting on its PTY. Because aGiT renders the
        # screen itself, the host terminal never sees those mode switches unless
        # we mirror them explicitly.
        for mode in (
            b"9",
            b"1000",
            b"1001",
            b"1002",
            b"1003",
            b"1004",
            b"1005",
            b"1006",
            b"1007",
            b"1015",
            b"1016",
            b"2004",
        ):
            if b"\x1b[?" + mode + b"h" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"h")
            if b"\x1b[?" + mode + b"l" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"l")
        # Track whether the backend drives the mouse itself. If it does, wheel
        # events are forwarded to it; if not, aGiT uses the wheel for scrollback.
        for mode in (b"1000", b"1002", b"1003"):
            if b"\x1b[?" + mode + b"h" in output:
                self.child_mouse = True
            if b"\x1b[?" + mode + b"l" in output:
                self.child_mouse = False
        # Mirror keyboard-protocol negotiation (kitty protocol, modifyOtherKeys)
        # so the host starts sending the enhanced key encodings the backend asked
        # for — Shift+Enter et al. arrive on stdin already encoded and are
        # forwarded to the backend like any other input.
        for match in _KEYBOARD_PROTO_RE.finditer(output):
            os.write(sys.stdout.fileno(), match.group(0))

    def _detect_host_terminal(self) -> None:
        # Pre-reactor: called once during run() startup, before _loop() starts.
        # The bounded 0.5 s select-and-read loop inside terminal.py is correct
        # here because no PTY children are running yet and no reactor iteration
        # is live.  Do NOT convert to a modal — there is nothing to drain yet.
        TerminalHost.detect_host_terminal(self, debug_fn=self._debug if self.debug_proxy else None)

    def _parse_host_terminal_responses(self, data: bytes) -> None:
        TerminalHost.parse_host_terminal_responses(self, data, debug_fn=self._debug if self.debug_proxy else None)

    def _answer_terminal_queries(self, output: bytes) -> None:
        if self.master_fd is None:
            return
        # These queries (OSC color/palette, cursor-position, device attributes)
        # only appear at startup. Skip the regex scans entirely when none of
        # their cheap markers are present, so heavy scroll output (potentially
        # megabytes per drain) is not scanned with regexes and never stalls the
        # event loop — which would otherwise back up the PTY and kill the backend.
        if b"\x1b]" not in output and b"\x1b[6n" not in output and b"\x1b[c" not in output and b"\x1b[0c" not in output:
            return
        response = bytearray()
        if self.host_fg_value and re.search(rb"\x1b\]10;\?(?:\x07|\x1b\\)", output):
            response += b"\x1b]10;" + self.host_fg_value + b"\x07"
        if self.host_bg_value and re.search(rb"\x1b\]11;\?(?:\x07|\x1b\\)", output):
            response += b"\x1b]11;" + self.host_bg_value + b"\x07"
        for match in re.finditer(rb"\x1b\]4;(\d+);\?(?:\x07|\x1b\\)", output):
            value = self.host_palette.get(match.group(1))
            if value:
                response += b"\x1b]4;" + match.group(1) + b";" + value + b"\x07"
        if self.screen is not None:
            for _ in range(output.count(b"\x1b[6n")):
                row = min(self.screen.cursor.y + 1, max(self.rows - 1, 1))
                col = min(self.screen.cursor.x + 1, self.cols)
                response += b"\x1b[%d;%dR" % (row, col)
        if self.host_da and re.search(rb"\x1b\[(?:0)?c", output):
            response += self.host_da
        if response:
            try:
                self.active.process.write(bytes(response))
            except OSError:
                pass

    def _track_sync_update(self, output: bytes) -> None:
        ScreenRenderer.track_sync_update(self, output)

    def _sync_hold(self, now: float) -> bool:
        return ScreenRenderer.sync_hold(self, now, self.SYNC_MAX_HOLD)

    def _render_output(self) -> None:
        ScreenRenderer.render_output(self, self._render, self.RENDER_MIN_INTERVAL, self.SYNC_MAX_HOLD)

    def _flush_pending_render(self) -> None:
        ScreenRenderer.flush_pending_render(self, self._render, self.RENDER_MIN_INTERVAL, self.SYNC_MAX_HOLD)

    def _cursor_sequence(self) -> str:
        return ScreenRenderer.cursor_sequence(self, self.rows, self.cols, self.scroll_back)

    def _render(self) -> None:
        if self.screen is None:
            return
        capturing = self.input.capturing
        ScreenRenderer.render(
            self,
            rows=self.rows,
            cols=self.cols,
            scroll_back=self.scroll_back,
            status_line_str=self._status_line(),
            input_capturing=capturing,
            input_text=self.input.text() if capturing else "",
            input_matches=self.input.matches() if capturing else [],
            input_selected=self.input.selected() if capturing else None,
            message=self.message,
            message_sticky=self._message_sticky,
            message_until=self.message_until,
        )

    def _append_command_palette(self, parts: list[str]) -> None:
        ScreenRenderer.append_command_palette(
            self,
            parts,
            rows=self.rows,
            cols=self.cols,
            input_text=self.input.text(),
            input_matches=self.input.matches(),
            input_selected=self.input.selected(),
        )

    def _append_message_popup(self, parts: list[str], message: str) -> None:
        ScreenRenderer.append_message_popup(self, parts, message, rows=self.rows, cols=self.cols)

    def _append_box(
        self, parts: list[str], row: int, col: int, width: int, lines: list[str], highlight: str | None = None
    ) -> None:
        ScreenRenderer.append_box(self, parts, row, col, width, lines, highlight, rows=self.rows)

    def _render_line(self, cells, sel: tuple[int, int] | None = None) -> str:
        return ScreenRenderer.render_line(self, cells, sel, cols=self.cols)

    def _hold_incomplete_tail(self, data: bytes) -> tuple[bytes, bytes]:
        # If the read ends mid escape-sequence (e.g. a mouse report split across
        # reads), hold the trailing partial so it is completed on the next read
        # rather than leaking to the backend as stray bytes (the "[<35;..." hex).
        match = _INCOMPLETE_TAIL_RE.search(data)
        if match:
            return data[: match.start()], data[match.start() :]
        return data, b""

    def _intercept_scroll(self, data: bytes) -> bytes:
        # Backends that drive the mouse (OpenCode) get all input forwarded so they
        # scroll themselves. For backends that do not (Claude), aGiT handles the
        # mouse: the wheel scrolls history, drag selects-and-copies, and
        # PageUp/PageDown also scroll. Consumed events are stripped from input.
        if self.child_mouse:
            return data
        page = max(self.rows - 2, 1)
        for match in _PAGE_KEY_RE.finditer(data):
            self._scroll(page if match.group(1) == b"5" else -page)
        data = _PAGE_KEY_RE.sub(b"", data)
        if b"\x1b[<" in data:
            for match in _SGR_MOUSE_EVENT_RE.finditer(data):
                self._handle_mouse(int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4))
            data = _SGR_MOUSE_RE.sub(b"", data)
        return data

    def _handle_mouse(self, button: int, col: int, row: int, kind: bytes) -> None:
        if button & 64:  # wheel
            self._scroll(-3 if button & 1 else 3)
            return
        y = max(0, min(row - 1, max(self.rows - 2, 0)))
        x = max(0, min(col - 1, self.cols - 1))
        is_left = (button & 0b11) == 0
        motion = bool(button & 32)
        if kind == b"M" and is_left and not motion:  # press
            self.sel_active = True
            self.sel_anchor = (y, x)
            self.sel_point = (y, x)
        elif kind == b"M" and motion and self.sel_active:  # drag (live, when motion is reported)
            self.sel_point = (y, x)
            self._render()
        elif kind == b"m" and self.sel_active:  # release
            self.sel_point = (y, x)  # capture the release point even without motion events
            if self.sel_anchor != self.sel_point:  # a drag, not a plain click
                self._copy_selection()
            self.sel_active = False
            self.sel_anchor = self.sel_point = None
            self._render()

    def _selection_ranges(self) -> dict[int, tuple[int, int]]:
        return ScreenRenderer.selection_ranges(self, self.cols)

    def _copy_selection(self) -> None:
        ScreenRenderer.copy_selection(
            self, self.rows, self.cols, self._copy_to_clipboard, lambda msg, **kw: self._set_message(msg, **kw)
        )

    def _copy_to_clipboard(self, text: str) -> None:
        payload = text.encode("utf-8", errors="replace")
        if shutil.which("pbcopy"):
            try:
                subprocess.run(["pbcopy"], input=payload, check=False)
                return
            except OSError:
                pass
        # OSC 52 clipboard fallback for terminals that support it.
        encoded = base64.b64encode(payload).decode("ascii")
        try:
            os.write(sys.stdout.fileno(), b"\x1b]52;c;" + encoded.encode("ascii") + b"\x07")
        except OSError:
            pass

    def _history_len(self) -> int:
        return ScreenRenderer.history_len(self)

    def _scroll(self, delta: int) -> None:
        ScreenRenderer.scroll(self, delta, self._render)

    def _visible_lines(self) -> list:
        return ScreenRenderer.visible_lines(self, self.rows)

    def _cell_sgr(self, cell) -> str:
        return ScreenRenderer.cell_sgr(self, cell)

    def _color_code(self, color: str, *, foreground: bool) -> str | None:
        return ScreenRenderer.color_code(self, color, foreground=foreground)

    def _hex_color_code(self, color: str, *, foreground: bool) -> str:
        return ScreenRenderer.hex_color_code(self, color, foreground=foreground)

    # Public aliases used by ScreenRenderer's internal self-calls when 'self'
    # is a ProxyRunner (duck-typing delegation; no underscore prefix).
    def cell_sgr(self, cell) -> str:
        return ScreenRenderer.cell_sgr(self, cell)

    def color_code(self, color: str, *, foreground: bool) -> str | None:
        return ScreenRenderer.color_code(self, color, foreground=foreground)

    def hex_color_code(self, color: str, *, foreground: bool) -> str:
        return ScreenRenderer.hex_color_code(self, color, foreground=foreground)

    def history_len(self) -> int:
        return ScreenRenderer.history_len(self)

    def scroll(self, delta: int, render_fn) -> None:
        ScreenRenderer.scroll(self, delta, render_fn)

    def visible_lines(self, rows: int) -> list:
        return ScreenRenderer.visible_lines(self, rows)

    def selection_ranges(self, cols: int) -> dict:
        return ScreenRenderer.selection_ranges(self, cols)

    def copy_selection(self, rows: int, cols: int, copy_fn, set_msg_fn) -> None:
        ScreenRenderer.copy_selection(self, rows, cols, copy_fn, set_msg_fn)

    def render_line(self, cells, sel=None, *, cols: int) -> str:
        return ScreenRenderer.render_line(self, cells, sel, cols=cols)

    def append_box(self, parts, row, col, width, lines, highlight=None, *, rows: int) -> None:
        ScreenRenderer.append_box(self, parts, row, col, width, lines, highlight, rows=rows)

    def append_command_palette(
        self, parts, *, rows: int, cols: int, input_text: str, input_matches, input_selected
    ) -> None:
        ScreenRenderer.append_command_palette(
            self,
            parts,
            rows=rows,
            cols=cols,
            input_text=input_text,
            input_matches=input_matches,
            input_selected=input_selected,
        )

    def append_message_popup(self, parts, message: str, *, rows: int, cols: int) -> None:
        ScreenRenderer.append_message_popup(self, parts, message, rows=rows, cols=cols)

    def cursor_sequence(self, rows: int, cols: int, scroll_back: int) -> str:
        return ScreenRenderer.cursor_sequence(self, rows, cols, scroll_back)

    def sync_hold(self, now: float, sync_max_hold: float) -> bool:
        return ScreenRenderer.sync_hold(self, now, sync_max_hold)

    def render_output(self, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        ScreenRenderer.render_output(self, render_fn, render_min_interval, sync_max_hold)

    def flush_pending_render(self, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        ScreenRenderer.flush_pending_render(self, render_fn, render_min_interval, sync_max_hold)

    def track_sync_update(self, output: bytes) -> None:
        ScreenRenderer.track_sync_update(self, output)

    def feed(self, output: bytes, *, pyte_hostile_csi_re) -> None:
        ScreenRenderer.feed(self, output, pyte_hostile_csi_re=pyte_hostile_csi_re)

    def init_screen(self, rows: int, cols: int) -> None:
        ScreenRenderer.init_screen(self, rows, cols)

    # Public aliases used by TerminalHost's internal self-calls when 'self' is a
    # ProxyRunner (same duck-typing contract as the ScreenRenderer aliases).
    def set_raw(self) -> None:
        TerminalHost.set_raw(self)

    def set_cooked(self) -> None:
        TerminalHost.set_cooked(self)

    def enable_host_mouse(self) -> None:
        TerminalHost.enable_host_mouse(self)

    def disable_host_terminal_modes(self) -> None:
        TerminalHost.disable_host_terminal_modes(self)

    def parse_host_terminal_responses(self, data: bytes, *, debug_fn=None) -> None:
        TerminalHost.parse_host_terminal_responses(self, data, debug_fn=debug_fn or self._debug)

    def _menu_label(self) -> str:
        # Human-readable name of the configured menu key, for the status line
        # and every message that points the user at the aGiT menu.
        return getattr(self.global_config, "menu_key_label", None) or "Ctrl-G"

    def _status_line(self) -> str:
        return ScreenRenderer.status_line(
            self,
            cols=self.cols,
            name=self.name,
            backend_name=self.backend.name,
            session_id=self.state.backend_session_id,
            base_branch=self._base_branch,
            worktree=self.worktree,
            scroll_back=self.scroll_back,
            user_declined=self._user_declined,
            short_session_fn=_short_session,
            menu_label=self._menu_label(),
            summarizer_on=self._summarization_enabled(),
            # The project the agent works on: the BASE repository, not the
            # session worktree under .agit/worktrees/ (an internal detail whose
            # long path mostly repeats the session name shown next to it).
            cwd=str(repo_path)
            if (repo_path := getattr(self.base_repo, "repo", None) or getattr(self.repo, "repo", None))
            else None,
        )

    def _summarization_enabled(self) -> bool:
        state_enabled = getattr(self.state, "summarization_enabled", None)
        if state_enabled is not None:
            return state_enabled
        if self.global_config is not None:
            gc_enabled = getattr(self.global_config, "summarization_enabled", None)
            if gc_enabled is not None:
                return gc_enabled
        return True

    def _render_status(self, text: str) -> None:
        prompt = text.replace("\r", "").replace("\n", "")
        line = f" aGiT> {prompt}"[: self.cols].ljust(self.cols)
        os.write(sys.stdout.fileno(), f"\x1b[{self.rows};1H\x1b[7m{line}\x1b[0m".encode())

    def _enter_host_screen(self) -> None:
        TerminalHost.enter_host_screen(self)

    def _enable_host_mouse(self) -> None:
        TerminalHost.enable_host_mouse(self)

    def _run_command(self, command: str) -> None:
        # aGiT commands in proxy mode are triggered via Ctrl-G and are plain
        # names; ":" is not a command trigger here (it is forwarded to the
        # backend like any other input).
        name, _, arg = command.partition(" ")
        if name in {"exit", "quit"}:
            if not self._run_exit_flow():
                self._render()
            return

        if name in {"git-stage", "git-user-commit"}:
            if name == "git-stage":
                self._set_message(self._stage_files_popup())
            else:
                created = self._create_user_commit_popup(repo=self.base_repo, state=self._user_state())
                self._set_message("Created user commit." if created else "No staged user changes to commit.")
            self._reload_user_declined()
            self._render()
            return

        if name == "git-status":
            self._set_message(self.base_repo.status() or "Working tree clean")
        elif name == "git-unstaged":
            self._prune_declined_untracked(self.base_repo, self._user_state())
            self._reload_user_declined()
            declined = self._user_declined
            if declined:
                self._set_message("Intentionally unstaged files:\n" + "\n".join(f"  {path}" for path in declined))
            else:
                self._set_message("No intentionally unstaged files.")
        elif name == "agent-backend":
            backends = available_backends()
            selected = arg.strip() or self._select_popup("Backend Agent", backends)
            if selected is None:
                self._set_message("Cancelled.")
                self._render()
                return
            if selected not in backends:
                self._set_message(f"Unknown backend: {selected}. Available: {', '.join(backends)}")
            elif selected == self.state.backend:
                self._set_message(f"Backend already set to {selected}")
            else:
                self._switch_backend(selected)
                return
        elif name == "session":
            self._handle_session_command(arg)
            return
        elif name == "summarizer":
            self._handle_summarizer_command(arg)
            return
        elif name == "git-base-branch":
            self._switch_base_command(arg)
            return
        elif name == "update":
            self._handle_update_command()
            return
        elif name == "":
            self._set_message("Select an aGiT command.")
        else:
            self._set_message(f"Unknown aGiT command: {name}")
        self._render()

    def _popup_read_input(self) -> bytes:
        # Read the user's next keypress for a modal popup WITHOUT suspending the
        # rest of the event loop: every session's PTY keeps being drained while
        # the popup waits, since back-pressure on a full PTY buffer blocks the
        # backend's writes and can stall or kill it (a popup can stay open for a
        # long time, and agents keep streaming behind it).
        stdin_fd = sys.stdin.fileno()
        dead: set[int] = set()
        while True:
            background = self._background_fds() if self.sessions else {}
            master = self.master_fd
            fds = [stdin_fd]
            if master is not None and master not in dead:
                fds.append(master)
            fds.extend(fd for fd in background if fd not in dead)
            readable, _, _ = select.select(fds, [], [], 1.0)
            for fd in readable:
                if fd == stdin_fd:
                    continue
                if fd in background:
                    # A dead background session is stopped and dropped by
                    # _pump_background, so the next pass stops selecting on it.
                    self._pump_background(background[fd])
                elif fd == master:
                    output = self._drain_child_output()
                    if output is None:
                        dead.add(fd)  # EOF: let the main loop handle the exit
                        continue
                    self.last_child_output = time.monotonic()
                    self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
                    self._answer_terminal_queries(output)
                    self._sync_terminal_modes(output)
                    self._track_sync_update(output)
                    # Feed the screen model but don't render: the popup overlays
                    # the view; the next normal render shows the updated screen.
                    self._feed_child_output(output)
            if stdin_fd in readable:
                return os.read(stdin_fd, 32)

    def _run_modal(self, modal: "PromptModal | SelectModal") -> "str | None":
        """Run *modal* to completion, keeping all session PTYs draining.

        This is the shared reactor iteration for modal dialogs.  It repeatedly:
          1. Renders the modal's current message via ``_set_message`` + ``_render``.
          2. Calls ``_popup_read_input()`` — which selects on stdin AND every
             session PTY so back-pressure never builds up while the popup is open.
          3. Passes the bytes to ``modal.feed()`` and acts on the returned action:

             ``("done",   value)``  — clears the message, renders, and returns value.
             ``("cancel", None)``   — clears the message, renders, and returns None.
             ``("exit",   None)``   — calls ``_run_exit_flow()``; returns None on
                                      confirmed exit, otherwise redraws and continues.
             ``("redraw", None)``   — redraws and continues.

        The call-shape is synchronous, which preserves the ~25 existing call
        sites unmodified.  ``_prompt_popup`` and ``_select_popup`` are thin
        facades that construct the appropriate modal and delegate here.
        """
        while True:
            self._set_message(modal.render_message(), seconds=60)
            self._render()
            data = self._popup_read_input()
            action, value = modal.feed(data)
            if action == "done":
                self._clear_message()
                self._render()
                return value
            if action == "cancel":
                self._clear_message()
                self._render()
                return None
            if action == "exit":
                if self._run_exit_flow():
                    return None
                # Exit declined: redraw the modal and keep listening.

    def _prompt_popup(self, title: str, prompt: str, *, default: str = "") -> str | None:
        """Free-text input popup.  Thin facade over ``_run_modal(PromptModal(...))``.

        Tests stub this method heavily; the name and signature are stable.
        """
        return self._run_modal(PromptModal(title, prompt, default=default))

    def _select_popup(self, title: str, options: list[str]) -> str | None:
        """Selection popup.  Thin facade over ``_run_modal(SelectModal(...))``.

        Tests stub this method heavily; the name and signature are stable.
        Returns ``""`` immediately when *options* is empty (unchanged behaviour).
        """
        if not options:
            return ""
        return self._run_modal(SelectModal(title, options))

    def _create_user_commit_popup(self, *, repo: GitRepo | None = None, state: AgitState | None = None) -> bool:
        # Defaults to the active worktree (capturing uncommitted worktree changes
        # before the next prompt). The user-facing `git-user-commit` command passes
        # the base repo/state instead, since the user's own edits live there.
        on_worktree = repo is None
        repo = repo or self.repo
        state = state or self.state
        if on_worktree:
            self._ensure_turn_branch()  # turn branches are a worktree concept only
        repo.add_tracked()
        self._review_untracked_popup(include_declined=False, repo=repo, state=state)
        if not repo.has_staged_changes():
            return False
        message = ""
        prompt = "Commit message:"
        while not message.strip():
            result = self._prompt_popup("User Commit", prompt)
            if result is None:
                return False
            message = result
            prompt = "Commit message is required. Enter a commit message:"
        repo.commit(build_user_commit_message(message=message, agit_session_id=state.session_id))
        state.clear_trace()
        return True

    def _review_untracked_popup(
        self, *, include_declined: bool, repo: GitRepo | None = None, state: AgitState | None = None
    ) -> str:
        repo = repo or self.repo
        state = state or self.state
        self._prune_declined_untracked(repo, state)
        untracked = repo.untracked_files()
        declined = set(state.declined_untracked())
        candidates = untracked if include_declined else [path for path in untracked if path not in declined]
        if not candidates:
            return "No untracked files to review."
        answer = self._prompt_popup(
            "Untracked Files",
            "Stage all new files? [y/N]\n" + "\n".join(candidates),
        )
        if answer is None:
            return "Cancelled."
        answer = answer.strip().lower()
        if answer in {"y", "yes"}:
            repo.stage_paths(candidates)
            state.remove_declined(candidates)
            return f"Staged {len(candidates)} untracked file(s)."
        else:
            state.add_declined(candidates)
            return f"Left {len(candidates)} untracked file(s) unstaged."

    def _stage_files_popup(self) -> str:
        # `git-stage`: one menu for the user's stageable base-tree files, in two
        # groups — *new* files (untracked, not yet decided) and *intentionally
        # unstaged* files (previously declined, e.g. at the pre-agent prompt). The
        # user picks which to stage; chosen files are staged+committed so the
        # agent's worktree (a checkout of base) can see them. Unpicked files are
        # left as they are. Selection is per-file (numbers, or 'a' for all).
        base, state = self.base_repo, self._user_state()
        self._prune_declined_untracked(base, state)
        declined = state.declined_untracked()
        new_files = [path for path in base.untracked_files() if path not in set(declined)]
        if not new_files and not declined:
            return "No files to stage."
        ordered: list[str] = []
        lines: list[str] = ["Select files to stage:", ""]
        for header, group in (("New files:", new_files), ("Intentionally unstaged:", declined)):
            if not group:
                continue
            lines.append(header)
            for path in group:
                ordered.append(path)
                lines.append(f"  {len(ordered)}. {path}")
        lines.append("")
        lines.append("Enter numbers (e.g. 1 3), 'a' for all, or blank to cancel:")
        answer = self._prompt_popup("Stage Files", "\n".join(lines))
        if answer is None:
            return "Cancelled."
        answer = answer.strip().lower()
        if not answer:
            return "Nothing staged."
        if answer in {"a", "all"}:
            selected = list(ordered)
        else:
            selected = [
                ordered[int(token) - 1]
                for token in answer.replace(",", " ").split()
                if token.isdigit() and 1 <= int(token) <= len(ordered)
            ]
        if not selected:
            return "No valid selection; nothing staged."
        return self._commit_user_files(selected, state)

    def _commit_user_files(self, paths: list[str], state: AgitState) -> str:
        # Stage the chosen base-tree files, drop them from the declined list, and
        # commit them as a user commit so they reach the session worktree on the
        # next idle base-sync. If the user cancels the message, leave them staged.
        self.base_repo.stage_paths(paths)
        state.remove_declined(paths)
        message = self._prompt_popup("User Commit", "Commit message for these files:")
        if message is None or not message.strip():
            return f"Staged {len(paths)} file(s); run git-user-commit to commit them."
        self.base_repo.commit(build_user_commit_message(message=message.strip(), agit_session_id=state.session_id))
        return f"Committed {len(paths)} file(s) to {self._base_branch}."

    def _create_agent_commit_from_turns_popup(
        self,
        *,
        turns,
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        quiet: bool,
        prompt_untracked: bool = True,
    ) -> bool:
        if prompt_untracked:

            def stage_untracked_fn(repo, state):
                self._review_untracked_popup(include_declined=False)
        else:
            # Non-interactive commit (worktree session or exit finalize): stage
            # the agent's new files too, except any the user intentionally declined.
            def stage_untracked_fn(repo, state):
                declined = set(state.declined_untracked())
                repo.stage_paths([path for path in repo.untracked_files() if path not in declined])

        def on_commit_fn(sha):
            self._last_agent_commit_id = sha
            if not quiet:
                self._set_message(self._agent_commit_message(), sticky=True)
            # The commit is made immediately; the LLM summary is computed in the
            # background and amended in afterwards (#8) so the UI never blocks.
            self._start_commit_summary(sha, turns)

        return CommitEngine(self.repo, self.state, debug_fn=self._debug).commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=stage_untracked_fn,
            pre_commit_fn=self._ensure_turn_branch,
            on_commit_fn=on_commit_fn,
            session_name=self.name,
            backend_commits=self._uncovered_backend_commits(),
        )

    def _uncovered_backend_commits(self) -> list[str]:
        """Unintegrated commits on the session's turn branch that the backend
        created itself and no aGiT commit accounts for yet (#35). Full SHAs,
        oldest first. Backend commits keep their own messages forever (their
        hashes must stay stable, #58), so "covered" cannot be read off the
        commit itself: an aGiT metadata commit covers everything before it on
        the branch (its ``covered_commits`` named them), leaving only commits
        NEWER than the newest metadata commit unaccounted for. Only commits
        ahead of base qualify at all."""
        if self.worktree is None or self._base_branch is None:
            return []
        try:
            branch = self.repo.current_branch()
            if not branch.startswith("agit/"):
                return []
            uncovered: list[str] = []
            for sha in self.repo.log_shas(self._base_branch, branch):  # oldest first
                if METADATA_HEADER in self.repo.commit_message(sha):
                    uncovered = []
                else:
                    uncovered.append(sha)
            return uncovered
        except Exception as error:
            self._debug(f"uncovered backend commit check failed: {error!r}")
            return []

    def _session_label(self) -> str:
        """The name of the session the runner is currently operating on — the
        one popups should attribute work to. Inside temp-swap windows
        (_with_session) this is the serviced background session, which is
        exactly the session whose commit/summary the popup announces."""
        return self.name or "main"

    def _agent_commit_message(self) -> str:
        # The auto-commit confirmation, including the short SHA of the commit aGiT
        # just made so the user can find it (e.g. `git show <id>`). Background
        # sessions auto-commit too, so the popup must say whose work it announces.
        commit_id = self._last_agent_commit_id
        session = self._session_label()
        if commit_id:
            return f"Created <aGiT> commit {commit_id} in session '{session}'."
        return f"Created <aGiT> commit in session '{session}'."

    # ------------------------------------------------------------------
    # Background commit summarization (#8)
    # ------------------------------------------------------------------
    #
    # The summary is an LLM call that can take many seconds; running it inside
    # the commit path froze the proxy UI and delayed integration past the next
    # turn. Instead the commit is created immediately and a worker thread
    # computes the summary; the main loop then amends it into the commit
    # message — only while the commit is still HEAD, unintegrated, and the
    # tree is clean, so an amend can never rewrite history or swallow staged
    # work. Integration waits for the summary up to SUMMARY_WAIT_SECONDS, then
    # proceeds without it (the summary still lands in git notes).

    def _make_summarizer(self):
        if not self._summarization_enabled():
            return None
        from agit.summaries import Summarizer, summary_scratch_dir
        from agit.backends.claude import ClaudeBackend
        from agit.backends.opencode import OpenCodeBackend

        backend_class = OpenCodeBackend if self.state.backend == "opencode" else ClaudeBackend
        model = self.state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        # The summarizer must NOT run in the session worktree (or the repo):
        # its headless calls record real backend sessions keyed by cwd, which
        # the parse worker / exit adoption would then resume instead of the
        # user's conversation (issues #8/#56).
        return Summarizer(backend_class(summary_scratch_dir()), model=model)

    def _start_commit_summary(self, sha: str, turns) -> None:
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        if self._summary_thread is not None and self._summary_thread.is_alive():
            # One summary at a time; a turn committed before the previous
            # summary finished keeps its prompt-based message (notes only).
            self._debug(f"summary worker busy; skipping summary for {sha}")
            return
        try:
            full_sha = self.repo.rev_parse(sha)
            # The committed snapshot is immutable, so the summary is computed
            # from exactly what landed — the whole turn's range when the base
            # branch is known, otherwise the commit's own diff.
            if self._base_branch is not None:
                diff = self.repo.diff_range(self._base_branch, full_sha)
            else:
                diff = self.repo.diff_range(f"{full_sha}^", full_sha)
        except Exception as error:
            self._debug(f"summary snapshot failed: {error!r}")
            return
        session_summary = self.state.session_summary
        # The summary may finish after the user switches sessions: remember the
        # owning repo/state/name so it is never applied to — or reported
        # against — a different session.
        result: dict = {
            "sha": full_sha,
            "short_sha": sha,
            "repo": self.repo,
            "state": self.state,
            "session_name": self._session_label(),
        }
        self._summary_result = None
        self._summary_pending = {"sha": full_sha, "since": time.monotonic()}

        def worker() -> None:
            try:
                result["summary"] = summarizer.summarize_commit(turns=turns, diff=diff, session_summary=session_summary)
            except Exception as error:  # surfaced by the service tick
                result["error"] = repr(error)
            else:
                try:
                    result["session_summary"] = summarizer.update_session_summary(
                        current_summary=session_summary,
                        turns=turns,
                        diff=diff,
                        commit_summary=result["summary"],
                    )
                except Exception as error:
                    # A failed rolling summary must not discard a good commit
                    # summary; the previous session summary simply stays current.
                    result["session_summary_error"] = repr(error)
            result["metadata"] = summary_metadata_lines(
                model=summarizer.model or self.state.model,
                tokens_input=summarizer.tokens_input,
                tokens_output=summarizer.tokens_output,
            )
            self._summary_result = result

        self._summary_thread = threading.Thread(target=worker, daemon=True, name="agit-summary")
        self._summary_thread.start()
        self._set_message(
            f"aGiT is summarizing commit {sha} in session '{self._session_label()}'...",
            seconds=self.SUMMARY_WAIT_SECONDS,
        )

    def _service_commit_summary(self) -> None:
        """Main-loop tick: apply a finished background summary (#8). All git
        and state mutations happen here, on the main thread."""
        result = self._summary_result
        if result is None:
            return
        self._summary_result = None
        self._summary_pending = None
        session = result.get("session_name") or "main"
        if "error" in result:
            # Includes UnusableSummaryError (backend returned "You've hit your
            # session limit..." or similar, issue #8): the commit keeps its
            # prompt-led message instead of getting the error as a subject.
            self._debug(f"commit summarization failed: {result['error']}")
            self._set_message(
                f"aGiT: commit summarization failed in session '{session}'; keeping the prompt-based message."
            )
            return
        sha, summary, repo, state = result["sha"], result["summary"], result["repo"], result["state"]
        try:
            # Amend first, then attach notes to whatever commit survives — notes
            # added before an amend would hang off the orphaned pre-amend object.
            target = self._amend_summary_into_head(repo, sha, summary, result.get("metadata"))
            if target:
                self._set_message(f"Summary added to commit {result['short_sha']} in session '{session}'.")
            else:
                # Already integrated or superseded: the summary lives in the
                # commit's git notes instead of its message.
                target = sha
                self._debug(f"summary for {sha} recorded as notes only")
            repo.notes_add(target, summary, namespace="agit/commit-summary")
            session_summary = result.get("session_summary")
            if session_summary:
                state.session_summary = session_summary
                state.session_summary_commit = target
                repo.notes_add(target, session_summary, namespace="agit/session-summary")
            elif "session_summary_error" in result:
                self._debug(f"session summary update failed: {result['session_summary_error']}")
        except Exception as error:
            self._debug(f"applying commit summary failed: {error!r}")

    def _amend_summary_into_head(self, repo, sha: str, summary: str, metadata: list[str] | None) -> str | None:
        """Amend the summary into *sha*'s message, only while that is safe:
        the commit is still HEAD, not yet integrated into base, and nothing is
        staged (``--amend`` would otherwise pull staged work into the commit).
        Returns the amended commit's full SHA, or None if no amend happened."""
        try:
            if repo.rev_parse("HEAD") != sha:
                return None
            if self._base_branch is None or sha not in repo.log_shas(self._base_branch, "HEAD"):
                return None
            if repo.has_staged_changes():
                return None
            message = repo.commit_message("HEAD")
            amended = apply_summary_to_message(message, summary, summary_metadata=metadata)
            if amended == message:
                return None  # already summarized: never amend twice
            new_short = repo.amend_commit(amended)
            if self._last_agent_commit_id and sha.startswith(str(self._last_agent_commit_id)):
                self._last_agent_commit_id = new_short
            return repo.rev_parse("HEAD")
        except Exception as error:
            self._debug(f"summary amend failed: {error!r}")
            return None

    def _summary_blocks_integration(self, now: float) -> bool:
        # Hold integration briefly so the summary can be amended in before the
        # commit leaves the turn branch — but never stall: past the deadline
        # the commit integrates as-is and the summary becomes notes-only.
        pending = self._summary_pending
        if pending is None:
            return False
        if now - pending["since"] >= self.SUMMARY_WAIT_SECONDS:
            return False
        thread = self._summary_thread
        if thread is None or not thread.is_alive():
            # Result ready (or worker gone): apply it now so integration can
            # proceed this tick instead of waiting for the next one.
            self._service_commit_summary()
            return self._summary_pending is not None
        return True

    def _set_message(self, message: str | None, *, seconds: float = 4.0, sticky: bool = False) -> None:
        # message may be None when relayed straight from a service result; storing
        # None simply leaves no popup to paint (the same state as a cleared message).
        self.message = message
        self.message_until = time.monotonic() + seconds
        # Sticky messages ignore the timeout and persist until the user's next
        # keypress clears them (see _clear_sticky_message_on_input).
        self._message_sticky = sticky
        # Request a repaint so the popup actually shows. Without this a message set
        # from the background idle loop (e.g. the auto-commit confirmation, set when
        # the agent has gone quiet and produces no output to trigger a render) would
        # never be painted.
        self._render_pending = True

    def _clear_message(self) -> None:
        self.message = None
        self.message_until = 0.0
        self._message_sticky = False

    def _clear_sticky_message_on_input(self) -> bool:
        # The next keypress dismisses a sticky message. Returns True if one was
        # showing (so the caller can repaint to remove the popup).
        if self._message_sticky:
            self._clear_message()
            return True
        return False

    def _confirm_exit(self) -> bool:
        choice = self._select_popup("Exit aGiT?", ["No, keep working", "Yes, exit"])
        return choice == "Yes, exit"

    def _run_exit_flow(self) -> bool:
        # THE single exit path (P6 Stage 3 — exit-path unification).
        #
        # Every interactive way out of aGiT goes through here:
        #   * Main loop: Ctrl-C in _reactor_stdin_phase → _run_exit_flow()
        #   * Modal: Ctrl-C inside any popup → _run_modal() → _run_exit_flow()
        #   * "exit" command: _run_command("exit") → _run_exit_flow()
        #
        # This guarantees _finalize_pending_work() is never skipped for
        # interactive exits.  Signal exits (SIGTERM/SIGHUP) go through
        # _handle_exit_signal which does a fast non-interactive teardown.
        #
        # Double-Ctrl-C: a second Ctrl-C while the confirmation popup is open
        # sets _popup_exit_force and exits immediately — but still gracefully
        # (finalize included). Returns True when aGiT is exiting.
        if self._popup_exit_pending:
            # A second Ctrl-C, inside one of the exit-confirmation popups: take
            # it as an emphatic yes — skip the questions, keep the finalize.
            self._popup_exit_force = True
            return True
        self._popup_exit_pending = True
        self._popup_exit_force = False
        try:
            if not self._confirm_exit() and not self._popup_exit_force:
                return False
            if not self._popup_exit_force:
                if not self._confirm_terminate_background_sessions() and not self._popup_exit_force:
                    return False
            self._finalize_pending_work()
            self._exit_child()
            return True
        finally:
            self._popup_exit_pending = False

    def _running_background_session_names(self) -> list[str]:
        # Names of background (non-active) sessions whose backend is still working.
        return [
            self._session_name(index)
            for index in range(len(self.sessions))
            if index != self.active_index and self._session_status(index) == "running"
        ]

    def _confirm_terminate_background_sessions(self) -> bool:
        # Second exit confirmation, shown only when background sessions are still
        # running: it names them and warns that exiting terminates them (which may
        # lose in-flight work). Returns True to proceed with exit, False to keep
        # working. No-op (returns True) when nothing is running in the background.
        names = self._running_background_session_names()
        if not names:
            return True
        listing = ", ".join(f"'{name}'" for name in names)
        lead = "background sessions are" if len(names) > 1 else "A background session is"
        choice = self._select_popup(
            f"{lead} still running ({listing}). Exiting now terminates them and may lose in-progress work.",
            ["No, keep working", "Yes, terminate them and exit"],
        )
        return choice == "Yes, terminate them and exit"

    def _commit_latest_turn_sync(self) -> None:
        # Synchronously (joining the parse worker) commit the latest completed
        # turn for the *current* session state, non-interactively.
        try:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self.agent_parse_thread.join(timeout=20)
            self._finish_agent_parse_if_ready(
                quiet=True, prompt_untracked=False, integrate=False, require_complete=False
            )
            if self._start_agent_parse() and self.agent_parse_thread:
                self.agent_parse_thread.join(timeout=20)
            self._finish_agent_parse_if_ready(
                quiet=True, prompt_untracked=False, integrate=False, require_complete=False
            )
        except Exception as error:  # never block on a commit failure
            self._debug(f"sync commit failed: {error!r}")

    def _finalize_pending_work(self) -> None:
        # On a confirmed exit, make sure the latest completed agent turn is
        # committed for *every* session before aGiT leaves — otherwise quitting
        # right after a turn drops a commit the idle/stable debounce had not yet
        # made. (Background sessions are committed via the context swap.)
        if self._finalized_on_exit:
            return  # already finalized (e.g. the backend exited and we ran this)
        self._finalized_on_exit = True
        self._exiting = True
        self._set_message("Finalizing commits before exit...", seconds=30)
        self._render()
        self._commit_latest_turn_sync()  # active session, in place
        # Give an in-flight commit summary a short grace period so it can be
        # amended in before the final integration; past that it is dropped
        # rather than holding the exit hostage (#8).
        if self._summary_thread is not None and self._summary_thread.is_alive():
            self._summary_thread.join(timeout=10)
        self._service_commit_summary()
        self._integrate_session_on_exit()
        self._remove_worktree_on_exit()
        for session in list(self.sessions):
            if session is self.active:
                continue
            saved = self.active
            self.active = session
            try:
                self._commit_latest_turn_sync()
                self._integrate_session_on_exit()
                self._remove_worktree_on_exit()
            finally:
                self.active = saved
        self._delete_orphan_merged_branches()

    def _adopt_latest_backend_session(self) -> None:
        # The user may have switched conversations inside the backend itself (e.g.
        # Claude's native session picker), which leaves aGiT's tracked id stale.
        # Point the resume record at the worktree's most recent conversation so the
        # next launch restores what they were actually using, not the id aGiT
        # originally spawned.
        try:
            latest = self.backend.latest_session_id(self.repo.repo)
        except Exception as error:
            self._debug(f"adopt latest backend session failed: {error!r}")
            return
        if latest and latest != self.state.backend_session_id:
            self._debug(f"adopting backend session {latest} (was {self.state.backend_session_id})")
            self.state.backend_session_id = latest
            self.state.last_backend_message_id = None  # recomputed from the transcript on resume
            self._persist_session_name(latest)

    def _persist_last_session_record(self) -> None:
        # Save just the resume pointer for the current (primary) session into the
        # repo-root state — the durable "last session" record. Only the minimal
        # fields needed to resume the conversation are kept; the worktree's working
        # tree and per-turn state (pending trace, etc.) are intentionally dropped.
        self._adopt_latest_backend_session()
        try:
            root = AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            root.data["backend"] = self.state.backend
            root.data["backend_session_id"] = self.state.backend_session_id
            root.data["backend_session_repo"] = self.state.data.get("backend_session_repo")
            root.data["last_backend_message_id"] = self.state.last_backend_message_id
            root.data["model"] = self.state.model
            root.data["agit_session_id"] = self.state.session_id
            root.save()
            self._debug(f"persisted last session record backend_session_id={self.state.backend_session_id}")
        except Exception as error:
            self._debug(f"persist last session record failed: {error!r}")

    def _note_pid_for_reaping(self, pid: int | None) -> None:
        # A signalled backend exits asynchronously; remember its pid so the main
        # loop can wait on it once it dies, instead of leaving a zombie for the
        # rest of the run. Host-level (shared across sessions), not swapped.
        if pid:
            self._reap_pids.append(pid)

    def _reap_stopped_children(self) -> None:
        pids = self._reap_pids
        if not pids:
            return
        remaining = []
        for pid in pids:
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
                if not done:
                    remaining.append(pid)
            except (ChildProcessError, OSError):
                pass  # already reaped elsewhere (or not our child anymore)
        self._reap_pids = remaining

    def _terminate_child(self) -> None:
        # Stop the current session's backend process and release its PTY, so its
        # worktree is no longer in use and can be removed.
        pid = self.child_pid
        if pid:
            self._note_pid_for_reaping(pid)
        # terminate() nulls the fd/pid on the session-owned process in place.
        self.active.process.terminate()

    def _remove_worktree_on_exit(self) -> None:
        # On exit, drop a fully-merged session's worktree so directories do not
        # pile up across runs. A session whose work could not be integrated
        # (conflict, uncommitted changes, or commits still ahead of the base)
        # keeps its worktree so the next startup can surface it. The backend
        # conversation persists (keyed by the worktree path) and is recreated if
        # the session is resumed, so nothing of value is lost.
        info = self.worktree
        if info is None or self._base_branch is None:
            return
        # Persist the primary session's resume pointer FIRST — before deciding
        # whether its worktree can be removed. _persist_last_session_record runs
        # _adopt_latest_backend_session, which captures a conversation the user
        # switched to inside the backend's own picker (Claude's session view).
        # Gating it behind a clean worktree removal meant a session that still had
        # uncommitted or unintegrated work — the usual state right after a mid-work
        # switch — never updated its resume pointer, so the next start resumed a
        # stale conversation and only the start after that landed on the right one
        # (the "first restart starts fresh, second restart resumes it" off-by-one).
        # Adopting writes both the worktree state (used when the worktree is kept)
        # and the repo-root state (used when it is removed), so the right
        # conversation resumes either way.
        if info.name == self._primary_worktree_name:
            self._persist_last_session_record()
        try:
            if self.merge_ctx or self.repo.merge_in_progress() or self.repo.has_changes():
                self._debug(f"keeping worktree '{info.name}' on exit: merge or uncommitted changes pending")
                return
            branch = self.repo.current_branch()
            if branch.startswith("agit/"):
                # Only drop the worktree if we can CONFIRM its branch is fully
                # contained in the base. If the base ref can't be resolved — e.g.
                # the user deleted/renamed the branch aGiT integrates into while it
                # was running — rev_parse raises and we keep the worktree (and its
                # branch) rather than risk discarding unmerged work.
                self.base_repo.rev_parse(self._base_branch)
                if self.base_repo.log_range(self._base_branch, branch):
                    self._debug(
                        f"keeping worktree '{info.name}' on exit: '{branch}' still ahead of {self._base_branch}"
                    )
                    return  # commits still ahead of base → unintegrated; keep it
        except Exception as error:
            self._debug(f"keeping worktree '{info.name}' on exit: {error!r}")
            return
        # Remember this session's conversation under its backend so switching back
        # to that backend (this run or a later one) resumes it.
        self._remember_session_for_backend()
        self._terminate_child()
        try:
            self._worktrees().remove(info.name)
            self._debug(f"removed merged worktree '{info.name}' on exit")
        except Exception as error:
            self._debug(f"exit worktree removal failed for '{getattr(info, 'name', '?')}': {error!r}")
        self.worktree = None

    def _exit_child(self) -> None:
        self.running = False
        self._disable_host_terminal_modes()
        # SIGINT to child delegated to the session's BackendProcess (does not
        # close fd -- the run() finally block handles that so it always runs).
        self.active.process.signal_exit()

    def _handle_exit_signal(self, signum, _frame) -> None:
        self.running = False
        self._disable_host_terminal_modes()
        self._cleanup_child()
        self._restore_terminal()
        raise SystemExit(128 + int(signum))

    def _cleanup_child(self) -> None:
        # Delegate SIGINT -> wait -> SIGTERM escalation to the session's BackendProcess.
        self.active.process.cleanup()

    def _pre_agent_commit_if_needed(self, prompt_text: str = "") -> bool:
        self._clear_agent_in_flight_if_idle()
        status = self.repo.status_short().strip()
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is True:
            self.pre_agent_reconciled_status = ""
        if status and self._agent_is_active():
            self._record_user_prompt(prompt_text)
            self._await_followup(prompt_text)
            return True
        if finished is False:
            self.pre_agent_reconciled_status = status
        if status and status != self.pre_agent_reconciled_status:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self._set_message("aGiT is checking existing git changes before sending your prompt...", seconds=60)
                self._render()
                return False
            if self._start_agent_parse():
                self._set_message("aGiT is checking existing git changes before sending your prompt...", seconds=60)
                self._render()
                return False
        if self.actions.has_pre_agent_user_changes():
            self._set_message("User changes detected before agent runs.")
            self._render()
            self._create_user_commit_popup()
        self._commit_base_user_edits_if_needed()
        # Trace every submitted prompt as a user message. The agent-active path
        # above already recorded; the held path records in _forward_pending_prompt.
        # This covers the remaining (clean / user-changes-committed) submits so no
        # follow-up is dropped from the commit trace. Dedup in the agent-commit
        # builder collapses this with the backend's own turn.user_prompt.
        self._record_user_prompt(prompt_text)
        return True

    def _handle_pre_compaction(self) -> None:
        # The transcript snapshot must happen before /compact is forwarded, but
        # the LLM summarization of it must NOT block the UI (#8): export here,
        # summarize on a worker thread, apply via _service_precompact_summary.
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        try:
            session_id = self.state.backend_session_id
            if not session_id:
                return
            exported = self.backend.export_session(self.repo.repo, session_id)
            if not exported or not exported.turns:
                return
        except Exception as error:
            self._debug(f"pre-compaction export failed: {error!r}")
            return
        if self._precompact_thread is not None and self._precompact_thread.is_alive():
            self._debug("pre-compaction summary worker busy; skipping")
            return
        current_summary = self.state.session_summary
        # The summary may finish after the user switches sessions: remember the
        # owning repo/state/name so it is applied to — and reported against —
        # the session that requested it, not whichever is active by then.
        result: dict = {"repo": self.repo, "state": self.state, "session_name": self._session_label()}
        self._precompact_result = None

        def worker() -> None:
            try:
                result["summary"] = summarizer.summarize_pre_compaction(
                    exported_session=exported,
                    current_summary=current_summary,
                )
            except Exception as error:
                result["error"] = repr(error)
            self._precompact_result = result

        self._precompact_thread = threading.Thread(target=worker, daemon=True, name="agit-precompact")
        self._precompact_thread.start()
        self._set_message(f"aGiT: Capturing session summary before compaction (session '{self._session_label()}')...")

    def _service_precompact_summary(self) -> None:
        result = self._precompact_result
        if result is None:
            return
        self._precompact_result = None
        session = result.get("session_name") or "main"
        if "error" in result:
            self._debug(f"pre-compaction summary failed: {result['error']}")
            self._set_message(f"aGiT: Pre-compaction summary failed (session '{session}').")
            return
        repo = result.get("repo") or self.repo
        state = result.get("state") or self.state
        try:
            summary = result["summary"]
            state.session_summary = summary
            head_sha = repo.rev_parse("HEAD")
            if head_sha:
                state.session_summary_commit = head_sha
                repo.notes_add(head_sha, summary, namespace="agit/session-summary")
            self._set_message(f"aGiT: Session summary captured (session '{session}').")
        except Exception as error:
            self._debug(f"applying pre-compaction summary failed: {error!r}")

    def _base_user_edits_pending(self) -> bool:
        # The user's own edits land in the BASE repo's working tree (the session
        # worktree is the agent's sandbox), so the worktree-side pre-agent check
        # never sees them. Detect tracked modifications, or new files the user
        # has not already declined, so they can be committed and synced into the
        # worktree before the agent runs.
        base = self.base_repo
        if base is None or self.worktree is None:
            return False
        try:
            if base.has_tracked_changes():
                return True
            declined = set(self._user_state().declined_untracked())
            return any(path not in declined for path in base.untracked_files())
        except Exception as error:
            self._debug(f"base user-edit check failed: {error!r}")
            return False

    def _commit_base_user_edits_if_needed(self) -> None:
        # Offer to commit the user's uncommitted base-repo edits before the
        # prompt reaches the agent, then sync the session worktree onto the new
        # base commit so the agent actually sees those edits. Declining skips
        # re-prompting until the base working tree changes again.
        if not self._base_user_edits_pending():
            self._base_edits_declined_status = None
            return
        status = self._base_edits_fingerprint()
        if status is not None and status == self._base_edits_declined_status:
            return  # already declined for this exact state; don't nag every prompt
        self._set_message("User changes detected in the base repo before agent runs.")
        self._render()
        if self._create_user_commit_popup(repo=self.base_repo, state=self._user_state()):
            self._base_edits_declined_status = None
            self._reload_user_declined()
            # Reflect the new base commit into the session worktrees now — before
            # the prompt is forwarded — so the agent works from the user's edits
            # instead of waiting for the next base-HEAD poll.
            self._base_advanced = False
            self._sync_idle_worktrees_to_base()
        else:
            # Snapshot AFTER the popup: it stages tracked edits (add_tracked), so
            # the pre-popup fingerprint would never match the declined state.
            self._base_edits_declined_status = self._base_edits_fingerprint()

    def _base_edits_fingerprint(self) -> str | None:
        # Status plus the actual diff content, so a decline is remembered for
        # exactly the edits the user saw — any further edit re-prompts.
        try:
            return self.base_repo.status_short() + self.base_repo.diff_head()
        except Exception:
            return None

    def _resume_pending_prompt_if_ready(self) -> None:
        if self.pending_forwarded is None:
            return
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is None:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self._set_message("aGiT is checking existing git changes before sending your prompt...", seconds=60)
                return
            # The parse already ran and deferred (its result is consumed): the
            # agent's latest turn is still in progress, so the uncommitted changes
            # belong to the in-flight agent — there is nothing of the user's to
            # commit before this prompt. Forward it now so the backend queues the
            # follow-up instead of holding it (and the "checking" message) forever.
            self._forward_pending_prompt()
            return
        if finished is False and self.actions.has_pre_agent_user_changes():
            self._set_message("User changes detected before agent runs.")
            self._render()
            if not self._create_user_commit_popup():
                self.pending_forwarded = None
                self.pending_prompt_text = ""
                self._set_message("Prompt not sent because existing user changes were not committed.")
                self._render()
                return
        self._commit_base_user_edits_if_needed()
        self._forward_pending_prompt()

    def _forward_pending_prompt(self) -> None:
        if self.pending_forwarded is None or self.master_fd is None:
            return
        forwarded = self.pending_forwarded
        prompt_text = self.pending_prompt_text
        self.pending_forwarded = None
        self.pending_prompt_text = ""
        self.passthrough_prompt.clear()
        self.passthrough_escape = None
        if prompt_text:
            self._record_user_prompt(prompt_text)
            self._ensure_turn_branch()  # a new prompt starts a turn on its own branch
        self.agent_in_flight = True
        self._clear_message()
        self.active.process.write(b"".join(forwarded))

    def _prune_declined_untracked(self, repo: GitRepo | None = None, state: AgitState | None = None) -> None:
        repo = repo or self.repo
        state = state or self.state
        state.keep_declined(repo.untracked_files())

    def _user_state(self) -> AgitState:
        # The user's working tree is the base repo (the session worktree is the
        # agent's sandbox and only holds tracked files). Its intentionally-unstaged
        # list and user commits live there. A fresh instance reads the latest
        # on-disk state so transient base-state writers elsewhere aren't clobbered.
        return AgitState(self.base_repo.repo, default_backend=self.global_config.default_backend)

    def _reload_user_declined(self) -> None:
        # Re-read the base repo's intentionally-unstaged list (after a command that
        # may have changed it) and seed the status-line cache.
        self._user_declined = self._user_state().declined_untracked()

    def _prune_user_declined(self) -> None:
        # Drop cached entries no longer untracked in the base tree (committed,
        # staged, or deleted out-of-band). Cheap enough for the base-poll cadence.
        untracked = set(self.base_repo.untracked_files())
        self._user_declined = [path for path in self._user_declined if path in untracked]

    def _forwarded_submits(self, forwarded: list[bytes]) -> bool:
        # Not every Enter submits the prompt — several keybindings insert a
        # NEWLINE in the backend's input box instead, and treating them as
        # submits ran the pre-agent flow (and could hold the keystroke or queue
        # a never-landing awaited prompt) on a mere line break:
        #   * Alt/Option+Enter sends ESC CR (the Apple Terminal / no-protocol
        #     way to get a newline in Claude's input),
        #   * a backslash immediately before Enter is Claude's "\<Enter>"
        #     line continuation,
        #   * newlines inside a bracketed paste are pasted content.
        data = b"".join(forwarded)
        data = _BRACKETED_PASTE_RE.sub(b"", data)
        data = data.replace(b"\x1b\r", b"").replace(b"\x1b\n", b"")
        for index, byte in enumerate(data):
            if byte not in (0x0D, 0x0A):
                continue
            if index == 0:
                # The Enter arrived in its own read; the backslash, if any, is
                # already at the end of the reconstructed prompt.
                if not bytes(self.passthrough_prompt).endswith(b"\\"):
                    return True
            elif data[index - 1 : index] != b"\\":
                return True
        return False

    def _update_passthrough_prompt(self, forwarded: list[bytes]) -> None:
        for chunk in forwarded:
            # Drop terminal escape sequences (arrow keys, etc.) so their residue
            # such as "[B" never leaks into the reconstructed prompt. State is
            # kept on the instance so sequences split across reads still match.
            if self.passthrough_escape is not None:
                self.passthrough_escape.extend(chunk)
                if _escape_sequence_complete(bytes(self.passthrough_escape)):
                    self.passthrough_escape = None
                continue
            if chunk == b"\x1b":
                self.passthrough_escape = bytearray(chunk)
                continue
            if chunk in {b"\r", b"\n"}:
                continue
            if chunk in {b"\x7f", b"\b"}:
                if self.passthrough_prompt:
                    self.passthrough_prompt.pop()
                continue
            if len(chunk) == 1 and chunk[0] >= 32:
                self.passthrough_prompt.extend(chunk)

    def _agent_is_active(self) -> bool:
        return self.agent_in_flight or (self.agent_parse_thread is not None and self.agent_parse_thread.is_alive())

    def _clear_agent_in_flight_if_idle(self) -> None:
        if self.agent_in_flight and time.monotonic() - self.last_child_output >= self.CHILD_IDLE_SECONDS:
            self.agent_in_flight = False

    def _record_user_prompt(self, prompt_text: str) -> None:
        CommitEngine(self.repo, self.state).record_user_prompt(prompt_text)

    def _await_followup(self, prompt_text: str) -> None:
        # Remember a prompt the user queued while the agent was busy so the next
        # commit waits for it to land as a turn (see _finish_agent_parse_if_ready),
        # keeping the queued prompt in the same commit as the turn it follows.
        self._awaited_followups = CommitEngine(self.repo, self.state).await_followup(
            prompt_text, self._awaited_followups
        )

    def _discover_spawned_session(self) -> str | None:
        # Identify the session aGiT just spawned: the newest one that did not
        # exist before launch. Falls back to the newest overall when no snapshot
        # was taken.
        refs = self.backend.list_sessions(self.repo.repo)
        if not refs:
            return None
        snapshot = self._pre_spawn_session_ids
        candidates = [ref for ref in refs if ref.id not in snapshot] if snapshot is not None else refs
        if not candidates:
            return None
        return max(candidates, key=lambda ref: ref.updated).id

    def _start_agent_parse(self) -> bool:
        # The worker holds its OWNING Session (issue #15): it can finish after
        # the user switched sessions, and resolving `self.backend` / `self.repo`
        # at that point would hit the WRONG session. CommitEngine.start_parse
        # captures the owning session explicitly and writes results back to it.
        return CommitEngine(self.repo, self.state, debug_fn=self._debug).start_parse(
            session=self.active,
            discover_session_id_fn=self._discover_spawned_session,
            debug_fn=self._debug,
        )

    def _finish_agent_parse_if_ready(
        self,
        *,
        quiet: bool,
        prompt_untracked: bool | None = None,
        integrate: bool = True,
        require_complete: bool = True,
    ) -> bool | None:
        if prompt_untracked is None:
            # Worktree sessions are isolated sandboxes, so agent commits there
            # auto-stage everything; only the main working tree prompts.
            prompt_untracked = self.worktree is None

        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        committed, new_awaited = engine.finish_parse_if_ready(
            session=self.active,
            quiet=quiet,
            prompt_untracked=prompt_untracked,
            require_complete=require_complete,
            awaited_followups=self._awaited_followups,
            agent_is_active_fn=self._agent_is_active,
            debug_fn=self._debug,
            note_session_change_fn=self._note_backend_session_change,
            mirror_fn=self._mirror_session_to_base,
            commit_fn=self._create_agent_commit_from_turns_popup,
        )
        self._awaited_followups = new_awaited
        if committed:
            self.agent_in_flight = False
            self.last_status = ""
            if not quiet:
                # Paint the "Created <aGiT> commit." confirmation NOW, before the
                # integrate below runs git merge/fast-forward on the main thread.
                self._render()
            if integrate:
                # Interactive integration only for the active session. The
                # sync/background commit path passes integrate=False. While a
                # background summary is pending, integration is deferred so the
                # summary can be amended in first; the idle-integration path
                # picks the commit up once it lands (or the wait deadline passes).
                if not self._summary_blocks_integration(time.monotonic()):
                    self._integrate_session_turn()
        return committed

    def _commit_available_agent_turns(self, *, quiet: bool) -> bool:
        if self._finish_agent_parse_if_ready(quiet=quiet) is True:
            return True
        self._start_agent_parse()
        return False

    def _maybe_agent_commit(self) -> None:
        now = time.monotonic()
        if self.file_change_event.is_set():
            self.file_change_event.clear()
            self.status_check_pending = True
        elif Observer is None:
            if now - self.last_poll < self.POLL_SECONDS:
                return
            self.last_poll = now
            self.status_check_pending = True
        self._clear_agent_in_flight_if_idle()
        if self.status_check_pending:
            status = self.repo.status_short()
            self._prune_declined_untracked()
            self.status_check_pending = False
            if status != self.last_status:
                self.last_status = status
                self.last_status_change = now
            if status.strip():
                self.parse_pending = True
                self.last_parse_attempt_status = ""
            else:
                self.parse_pending = False
                self.last_parse_attempt_status = ""
        else:
            status = self.last_status
        if not status.strip():
            if self.verbose:
                self._render_status("no git changes")
            self._integrate_agent_made_commits_if_idle(now)
            return
        if self.agent_parse_thread and self.agent_parse_thread.is_alive():
            if self.verbose:
                self._render_status(f"git changes found; parsing {self.backend.name} session")
            return
        if (
            now - self.last_status_change < self.FILE_STABLE_SECONDS
            or now - self.last_child_output < self.CHILD_IDLE_SECONDS
        ):
            if self.verbose:
                self._render_status(f"git changes found; waiting for {self.backend.name} to become idle")
            return
        finished = self._finish_agent_parse_if_ready(quiet=not self.verbose)
        if finished is True:
            self.parse_pending = False
            self.last_parse_attempt_status = ""
            committed = True
        else:
            if finished is False:
                self.parse_pending = False
                self.last_parse_attempt_status = status
            if not self.parse_pending or status == self.last_parse_attempt_status:
                if self.verbose:
                    self._render_status("git changes found; waiting for new file changes")
                return
            if now - self.last_parse_finish < self.PARSE_COOLDOWN_SECONDS:
                if self.verbose:
                    self._render_status("git changes found; waiting for parse cooldown")
                return
            self._start_agent_parse()
            return
        if not committed:
            if self.verbose:
                self._render_status("git changes found; no new final response available")
            return
        if self.verbose:
            self._render_status(self._agent_commit_message())
        else:
            self._set_message(self._agent_commit_message(), sticky=True)

    def _integrate_agent_made_commits_if_idle(self, now: float) -> None:
        # The agent can run `git commit` itself (some workflows ask it to).
        # Those turns leave the worktree CLEAN, so the auto-commit path — which
        # integration used to piggyback on exclusively — never runs, and the
        # turn branch sat ahead of base until exit/restart. When the session is
        # idle, its tree is clean, and its branch holds unintegrated commits,
        # integrate them now.
        if self.worktree is None or self.merge_ctx:
            return
        if self._base_branch is None or self._integration_paused:
            return
        if self._agent_is_active() or now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return
        if now - self._idle_integrate_at < self.BASE_POLL_SECONDS:
            return
        self._idle_integrate_at = now
        try:
            branch = self.repo.current_branch()
            if not branch.startswith("agit/") or not self.base_repo.log_range(self._base_branch, branch):
                return
        except Exception as error:
            self._debug(f"idle integrate check failed: {error!r}")
            return
        if self._summary_blocks_integration(now):
            return  # summary worker still running — amend first, then integrate
        if not self._attach_trace_to_backend_commits(now):
            return  # parse still in flight — integrate once the trace is attached
        self._debug(f"integrating agent-made commits on {branch} (worktree clean and idle)")
        self._integrate_session_turn()

    def _attach_trace_to_backend_commits(self, now: float) -> bool:
        """Before integrating commits the backend made itself, give the parse
        pipeline a chance to amend the trace/metadata onto them (#35).

        Returns True when integration may proceed: the trace was attached, no
        attachment is needed/possible, or the attach deadline passed (the
        commits then integrate as-is rather than sit on the branch forever).
        """
        if not self._uncovered_backend_commits():
            self._attach_uncovered_until = 0.0
            return True
        if self._attach_uncovered_until == 0.0:
            self._attach_uncovered_until = now + self.PARSE_COOLDOWN_SECONDS * 3
        finished = self._finish_agent_parse_if_ready(quiet=not self.verbose, integrate=False)
        if finished is True:
            # commit_turns amended the backend's HEAD commit; integrate it now.
            self._attach_uncovered_until = 0.0
            return True
        if finished is False:
            # Parse consumed with nothing to attach (e.g. the turn was already
            # committed): there is no trace to recover, integrate as-is.
            self._debug("no trace available for backend-made commits; integrating as-is")
            self._attach_uncovered_until = 0.0
            return True
        if now >= self._attach_uncovered_until:
            self._debug("trace attach deadline passed; integrating backend-made commits as-is")
            self._attach_uncovered_until = 0.0
            return True
        self._start_agent_parse()
        return False

    def _pause_child_ui(self) -> None:
        TerminalHost.pause_child_ui(self)

    def _resume_child_ui(self) -> None:
        TerminalHost.resume_child_ui(self, self._render)

    def _set_raw(self) -> None:
        TerminalHost.set_raw(self)

    def _set_cooked(self) -> None:
        TerminalHost.set_cooked(self)

    def _restore_terminal(self) -> None:
        TerminalHost.restore_terminal(self)

    def _disable_host_terminal_modes(self) -> None:
        TerminalHost.disable_host_terminal_modes(self)

    def _resize_child(self) -> None:
        if self.master_fd is None:
            return
        try:
            self.rows, self.cols = self._terminal_size()
            if self.screen is not None:
                self.screen.resize(max(self.rows - 1, 1), self.cols)
            self.scroll_back = 0  # history geometry changed; return to live view
            # PTY ioctl delegated to the session-owned BackendProcess.
            self.active.process.resize(max(self.rows - 1, 1), self.cols)
            self._render()
        except OSError:
            pass

    def _terminal_size(self) -> tuple[int, int]:
        return TerminalHost.terminal_size(self)


# ---------------------------------------------------------------------------
# P3 backward-compat layer: per-session state as runner attributes.
#
# Production runner methods use ``self.repo``, ``self.state``, ``self.backend``
# etc. throughout. These are Session-level fields that live on ``self.active``;
# the property layer delegates every Session.FIELDS name to the active session
# so that context-switching helpers like ``_with_session`` work correctly.
#
# The ``ProxyRunner.__new__`` test idiom has been replaced by
# ``ProxyRunner.for_testing()`` (Stage 1–3); Session.bare() lazy materialisation
# in the ``active`` getter and the ``_active_index_compat`` fallback have been
# removed (Stage 4). The delegation properties themselves remain because
# all production call sites still use the short forms.
# ---------------------------------------------------------------------------


def _delegate_to_active_session(field: str) -> property:
    def getter(self):
        return getattr(self.active, field)

    def setter(self, value):
        setattr(self.active, field, value)

    return property(
        getter,
        setter,
        doc=f"Per-session state: delegates to ``self.active.{field}``.",
    )


for _field in Session.FIELDS:
    setattr(ProxyRunner, _field, _delegate_to_active_session(_field))
del _field
