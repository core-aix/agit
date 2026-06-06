from __future__ import annotations

import base64
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import textwrap
import time
import tty

import pyte

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised only without optional dependency
    FileSystemEvent = None
    FileSystemEventHandler = object
    Observer = None

from agit.actions import AgitActions
from agit.backend_setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agit.backends.proxy_agents import available_backends, make_proxy_agent
from agit.commit_message import build_agent_commit_message, build_agent_merge_message, build_user_commit_message
from agit.git import GitRepo
from agit.global_config import GlobalConfig
from agit.lock import RepoLock, already_running_message
from agit import sandbox
from agit.session import turns_after
from agit.session_runtime import SESSION_FIELDS, capture_session, default_session_fields, restore_session
from agit.state import AgitState
from agit.worktree import WorktreeInfo, WorktreeManager, _sanitize_name


# Map every xterm-256 palette colour back to its index so that colours pyte
# collapsed to hex can be re-emitted in their original 256-colour encoding.
# First occurrence wins, which keeps the ANSI palette indices (0-15) that
# OpenCode's "system" theme relies on, so the host terminal's own palette is
# respected instead of being frozen to fixed RGB values.
_PALETTE_256: list[tuple[int, int, int]] = []
_REVERSE_256: dict[str, int] = {}


def _build_palette_256() -> None:
    try:
        import pyte.graphics as graphics
    except Exception:  # pragma: no cover - pyte always present in practice
        return
    for index in range(256):
        hex_value = graphics.FG_BG_256[index]
        _REVERSE_256.setdefault(hex_value, index)
        _PALETTE_256.append((int(hex_value[0:2], 16), int(hex_value[2:4], 16), int(hex_value[4:6], 16)))


_build_palette_256()


def _nearest_256(red: int, green: int, blue: int) -> int:
    best_index = 0
    best_distance = None
    for index, (pr, pg, pb) in enumerate(_PALETTE_256):
        distance = (pr - red) ** 2 + (pg - green) ** 2 + (pb - blue) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _nearest_ansi16(red: int, green: int, blue: int) -> int:
    best_index = 0
    best_distance = None
    for index in range(16):
        pr, pg, pb = _PALETTE_256[index]
        distance = (pr - red) ** 2 + (pg - green) ** 2 + (pb - blue) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def detect_color_mode(environ=None) -> str:
    # Mirror the colour-depth detection OpenCode itself uses so that aGiT
    # re-emits colours in the exact encoding OpenCode produced. aGiT and the
    # backend share an environment, so the same depth applies to both.
    env = os.environ if environ is None else environ
    colorterm = (env.get("COLORTERM") or "").strip().lower()
    if colorterm in {"truecolor", "24bit"}:
        return "truecolor"
    term = (env.get("TERM") or "").strip().lower()
    if "256" in term:
        return "256"
    if colorterm or term:
        return "16"
    return "16"


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


def _short_session(session_id: str | None) -> str:
    if not session_id:
        return "(none)"
    return session_id[:8]


def _humanize_age(updated: float) -> str:
    if not updated:
        return ""
    delta = max(time.time() - float(updated), 0.0)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


class _BackgroundColorEraseScreen(pyte.HistoryScreen):
    # pyte erases cells using the cursor's *full* SGR attributes, so a backend
    # that clears the screen (or a line) while underline — or any glyph
    # attribute — is still active leaves the blanked cells carrying that
    # attribute. The host terminal then renders those underlined blanks as stray
    # horizontal lines that linger after the view is dismissed (seen on Claude's
    # session-choice picker). Real terminals do background-colour-erase: erased
    # cells keep only the background colour, not glyph attributes. Mirror that by
    # blanking everything except the background on the cursor attrs we erase with.
    def _erase_attrs(self):
        return self.cursor.attrs._replace(
            data=" ",
            fg="default",
            bold=False,
            italics=False,
            underscore=False,
            strikethrough=False,
            reverse=False,
            blink=False,
        )

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        saved = self.cursor.attrs
        self.cursor.attrs = self._erase_attrs()
        try:
            super().erase_in_line(how, private)
        finally:
            self.cursor.attrs = saved

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        saved = self.cursor.attrs
        self.cursor.attrs = self._erase_attrs()
        try:
            super().erase_in_display(how, *args, **kwargs)
        finally:
            self.cursor.attrs = saved

    def report_device_status(self, mode: int = 0, private: bool = False, **kwargs) -> None:
        # pyte's stream invokes report_device_status(mode, private=True) for
        # DEC-private DSR queries — notably ``\x1b[?6n`` (cursor-position request),
        # which Claude/Ink emits while redrawing — but pyte's own
        # Screen.report_device_status() doesn't accept ``private`` and raises
        # TypeError mid-parse. aGiT swallows feed errors to stay alive, but that
        # drops the rest of the output chunk: it truncated Claude's option-menu
        # collapse redraw, leaving stale menu rows on screen. aGiT answers terminal
        # queries itself (_answer_terminal_queries), so pyte's report is unused —
        # just accept ``private`` and never raise so the feed completes.
        if private:
            return
        try:
            super().report_device_status(mode)
        except TypeError:
            pass


class RepoChangeHandler(FileSystemEventHandler):
    IGNORED_PARTS = {".agit", ".git", ".pytest_cache", ".venv", "__pycache__"}

    def __init__(self, repo_path, changed: threading.Event) -> None:
        self.repo_path = repo_path
        self.changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        try:
            relative = os.path.relpath(event.src_path, self.repo_path)
        except ValueError:
            relative = event.src_path
        if any(part in self.IGNORED_PARTS for part in relative.split(os.sep)):
            return
        self.changed.set()


def _escape_sequence_complete(sequence: bytes) -> bool:
    if sequence.startswith(b"\x1b[<"):
        return sequence[-1:] in {b"M", b"m"}
    if sequence.startswith(b"\x1b[M"):
        return len(sequence) >= 6
    if sequence.startswith(b"\x1b["):
        return len(sequence) >= 3 and 0x40 <= sequence[-1] <= 0x7E
    return len(sequence) >= 2


class ProxyInput:
    # Order matters (shown in the palette). Only "session" starts with "s" so
    # that pressing s+Enter jumps straight to the session picker. Git-specific
    # commands are grouped under a "git-" prefix.
    COMMANDS = ["session", "agent-backend", "git-base-branch", "git-status", "git-stage", "git-user-commit", "exit"]

    def __init__(self) -> None:
        self.capturing = False
        self.buffer = bytearray()
        self.selected_index = 0
        self.escape_buffer: bytearray | None = None

    def feed(self, data: bytes) -> tuple[list[bytes], bytes, str | None, bool]:
        forwarded: list[bytes] = []
        command = None
        should_exit = False
        for byte in data:
            char = bytes([byte])
            if char == b"\x03":
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

            if char == b"\x07":
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

    def best_match(self) -> str | None:
        return next(iter(self.matches()), None)


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
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps
    SYNC_MAX_HOLD = 0.05  # cap how long a backend synchronized-update may defer a paint

    def __init__(self, repo: GitRepo, *, verbose: bool = False, backend: str | None = None, new_session: bool = False) -> None:
        self.repo = repo
        self._force_new_session = new_session  # start a fresh conversation, do not resume
        self.name = "main"  # session label (multiplexer assigns names to others)
        self._primary_worktree_name: str | None = None  # session kept across exits for auto-resume
        self.worktree = None  # set when this session runs in a git worktree
        self.global_config = GlobalConfig()
        self._apply_timings(self.global_config.timings)
        self.state = AgitState(repo.repo, default_backend=self.global_config.default_backend)
        if backend and backend != self.state.backend:
            self.state.remember_backend_session()
            self.state.backend = backend
            self.global_config.default_backend = backend
            self.state.backend_session_id = self.state.stored_backend_session(backend)
            self.state.last_backend_message_id = None
        self.backend = make_proxy_agent(self.state.backend)
        self.actions = AgitActions(repo, self.state, verbose=verbose)
        self.verbose = verbose
        self.input = ProxyInput()
        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.last_poll = 0.0
        self.status_check_pending = False
        self.file_change_event = threading.Event()
        self.file_observer = None
        self.parse_pending = False
        self.last_parse_start = 0.0
        self.running = True
        self.old_attrs = None
        self.original_sigwinch = None
        self.original_signal_handlers = {}
        self.rows = 24
        self.cols = 80
        self.screen: pyte.Screen | None = None
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
        self.management_lock = RepoLock(repo.repo / ".agit" / "lock")
        # Multiplexer: the active session's live state is on `self`; other
        # sessions are kept as Session snapshots and swapped on to be serviced.
        # With a single session this stays empty/identity and the loop is
        # unchanged. `base_repo` is the main working tree (worktrees branch off it).
        self.base_repo = repo
        self._base_branch: str | None = None  # integration target branch (set at startup)
        self._integration_paused = False  # set when the base repo is switched off _base_branch out-of-band
        self._base_drift_check_at = 0.0
        self.turn = 0  # per-session transient-branch counter
        self.merge_ctx = None  # in-progress agent merge resolution, if any
        self._pending_enter_at: float | None = None  # deferred submit of an injected prompt
        self._pending_enter_fd: int | None = None  # the PTY that injected prompt's Enter must go to
        self._base_advanced = False  # base moved; sync idle sessions onto it on the next loop pass
        self._last_base_head: str | None = None  # last-polled base HEAD, to catch out-of-band commits
        self._base_poll_at = 0.0  # throttle for the base-HEAD poll
        self._warned_backend_session = False  # one-shot "use agit to start sessions" notice
        # The user's intentionally-unstaged files belong to the base working tree
        # (their repo), not the ephemeral session worktree; cache the list so the
        # status line can show its count without a per-frame disk read.
        self._user_declined: list[str] = []
        self.sessions: list = []
        self.active_index = 0
        self.worktree_manager: WorktreeManager | None = None
        # AGIT_DEBUG_RAW records every raw child-output / user-input chunk so an
        # interactive glitch (e.g. Claude's native session picker) can be replayed
        # byte-for-byte; it implies debug logging too.
        self.raw_capture = os.environ.get("AGIT_DEBUG_RAW", "").strip().lower() in {"1", "true", "yes"}
        self.debug_proxy = (verbose or self.raw_capture
                            or os.environ.get("AGIT_DEBUG_PROXY", "").strip().lower() in {"1", "true", "yes"})
        # One diagnostic-log file per run, in the base repo's .agit/ (survives the
        # per-run worktree teardown).
        self._diag_run = time.strftime("%Y%m%d-%H%M%S")

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
        self.sessions = [capture_session(self)]
        self.active_index = 0
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
            return self._loop()
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
        command = self._confine_to_worktree(command)
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.repo.repo)
            os.execvp(command[0], command)
        self.child_pid = pid
        self.master_fd = fd

    def _setup_worktree_confinement_notice(self) -> None:
        # When confinement is requested but the platform can't enforce it (no
        # sandbox), watch the base repo and warn if the agent writes into it, so
        # edits outside the worktree don't silently go untracked.
        self._monitor_base_edits = False
        if getattr(self, "worktree", None) is None:
            return
        if not (self.global_config.sandbox and sandbox.is_enabled()):
            return  # user disabled confinement
        if sandbox.is_available():
            return  # the sandbox enforces it; no warning needed
        self._monitor_base_edits = True
        self._base_check_at = 0.0
        try:
            self._base_status_baseline = set(self.base_repo.status_short().splitlines())
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
        if self._base_branch is None or not getattr(self, "tracking_enabled", True):
            return
        now = time.monotonic()
        if now - getattr(self, "_base_drift_check_at", 0.0) < 2.0:
            return
        self._base_drift_check_at = now
        try:
            current = self.base_repo.current_branch()
        except Exception:
            return
        drifted = current != self._base_branch
        if drifted and not self._integration_paused:
            self._integration_paused = True
            self._debug(f"base branch drift: repo on '{current}', integration target '{self._base_branch}'")
            self._set_message(
                f"⚠ Base branch changed outside aGiT — the repo is now on '{current}', but\n"
                f"aGiT integrates into '{self._base_branch}'. Worktree merging is PAUSED so\n"
                f"your work isn't merged into the wrong branch (sessions keep running and\n"
                f"committing to their own branches).\n"
                f"To resume: run  git checkout {self._base_branch}  in the repo — merging\n"
                f"continues automatically. To instead make '{current}' the base, quit and\n"
                f"relaunch aGiT from there.",
                seconds=30.0,
            )
            self._render()
        elif not drifted and self._integration_paused:
            self._integration_paused = False
            self._debug(f"base branch restored to '{self._base_branch}'; integration resumed")
            self._set_message(
                f"Base branch back on '{self._base_branch}' — worktree merging resumed.",
                seconds=8.0,
            )
            self._render()

    def _warn_if_base_edited(self) -> None:
        # Fallback for un-sandboxed platforms: detect the agent editing the base
        # repo (its working tree gaining uncommitted changes beyond the startup
        # baseline) and warn, since those edits bypass aGiT's worktree tracking.
        if not getattr(self, "_monitor_base_edits", False):
            return
        now = time.monotonic()
        if now - getattr(self, "_base_check_at", 0.0) < self.BASE_EDIT_CHECK_SECONDS:
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
        if getattr(self, "worktree", None) is None or self._base_branch is None:
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
        if getattr(self, "_cwd_drift_checked", False):
            return
        if getattr(self, "worktree", None) is None:
            return
        now = time.monotonic()
        if now - getattr(self, "_cwd_check_at", 0.0) < self.CWD_CHECK_SECONDS:
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
            "To recover: Ctrl-G → session → start a NEW session (it launches fresh in the "
            "worktree) and re-send your request there; resuming this conversation will keep "
            "landing in the wrong directory. Any work already done in the other directory "
            "stays there — move it into the worktree by hand if you need it tracked.",
            seconds=30.0,
        )
        self._render()

    def _confine_to_worktree(self, command: list[str]) -> list[str]:
        # Wrap the backend so it can only write inside its session worktree (plus
        # the repo's .git), not the base repo it lives in. A no-op when there is
        # no worktree, or when confinement is disabled / unavailable (the loop
        # then warns if the base working tree is touched).
        if getattr(self, "worktree", None) is None:
            return command
        if not self.global_config.sandbox:
            return command
        base = getattr(self, "base_repo", None)
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
            self._new_session(record.get("worktree") or self._next_session_name(),
                              backend=name, resume_session_id=record["id"])
            return
        # Otherwise start fresh, confirming the session name first.
        session_name = self._prompt_session_name(f"New {name} session", default=self._next_session_name())
        if session_name is None:
            self._set_message("Cancelled.")
            self._render()
            return
        self._new_session(session_name, backend=name)

    def _taken_session_names(self) -> set[str]:
        # Sanitized names already in use: live sessions plus on-disk worktrees.
        used: set[str] = set()
        try:
            used.update(info.name for info in self._worktrees().list())
        except Exception:
            pass
        used.update(_sanitize_name(self._session_name(index)) for index in range(len(self.sessions)))
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
        info = getattr(self, "worktree", None)
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

    # --- live-session multiplexer ---

    def _worktrees(self) -> WorktreeManager:
        if self.worktree_manager is None:
            self.worktree_manager = WorktreeManager(self.base_repo)
        return self.worktree_manager

    def _apply_new_session_if_requested(self) -> None:
        # `agit --new-session`: start a fresh backend conversation (don't resume)
        # and mint a new aGiT session id.
        if not getattr(self, "_force_new_session", False):
            return
        self.state.backend_session_id = None
        self.state.last_backend_message_id = None
        self.state.new_agit_session_id()

    def _turn_from_branch(self, branch: str) -> int:
        match = re.search(r"/t(\d+)$", branch or "")
        return int(match.group(1)) if match else 0

    def _setup_base_merge_only_session(self) -> None:
        # Move the initial session into its own worktree so the base tree is only
        # ever advanced by integration. Reuses an existing worktree (resuming a
        # previous run) or creates a fresh one; falls back to running on the base
        # tree (legacy behaviour, no auto-integration) if neither is possible.
        root_state = self.state  # the durable repo-root "last session" record
        backend_name = root_state.backend
        prior_message_id = root_state.last_backend_message_id
        prior_model = root_state.model
        prior_worktree = (root_state.recall_session(backend_name) or {}).get("worktree")
        # Which conversation to continue at startup: aGiT's own last session if we
        # have one, otherwise the repo's most recent backend conversation (e.g. one
        # you ran with plain claude/opencode before aGiT). Resume is by id, which
        # the backend resolves regardless of which directory it runs in.
        if getattr(self, "_force_new_session", False):
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
            existing = prior_worktree
        if existing:
            return existing
        name = self._prompt_startup_name(resume_id is not None)
        if resume_id:
            root_state.name_session(resume_id, name)
        return name

    def _first_free_session_name(self) -> str:
        # `session-N` not already taken by a worktree on disk. (self.sessions is
        # not built yet at startup, so this avoids the live-session check.)
        try:
            used = {info.name for info in self._worktrees().list()}
        except Exception:
            used = set()
        number = 1
        while f"session-{number}" in used:
            number += 1
        return f"session-{number}"

    def _prompt_startup_name(self, continuing: bool) -> str:
        # Asked in cooked mode, before the alt-screen is entered.
        default = self._first_free_session_name()
        print("Continuing a conversation that has no name yet."
              if continuing else "Starting a new session.")
        try:
            taken = {info.name for info in self._worktrees().list()}
        except Exception:
            taken = set()
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
            if repo.merge_in_progress() or repo.has_changes():
                return
            branch = repo.current_branch()
            if branch.startswith("agit/") and self.base_repo.log_range(self._base_branch, branch):
                # Has its own unintegrated commits: don't re-point (that would
                # orphan the work). Merge any new base commits in instead, keeping
                # it current — but only if the merge is clean.
                if not self.base_repo.log_range(branch, self._base_branch):
                    return  # already contains the base; nothing to merge
                if repo.merge(self._base_branch):
                    self._debug(f"merged base '{self._base_branch}' into session branch {branch}")
                else:
                    repo.merge_abort()  # conflicts; leave for integration to resolve
                    self._debug(f"base '{self._base_branch}' conflicts with {branch}; left for integration")
                return
            if repo.rev_parse("HEAD") != self.base_repo.rev_parse(self._base_branch):
                repo.switch_detach(self._base_branch)
                self._debug(f"re-pointed session worktree to current base '{self._base_branch}'")
        except Exception as error:
            self._debug(f"align to base failed: {error!r}")

    def _worktree_has_pending_work(self, repo: GitRepo, branch: str) -> bool:
        # Pending = uncommitted changes, or commits on its branch not yet in base.
        try:
            if repo.has_changes():
                return True
            return bool(self.base_repo.log_range(self._base_branch, branch))
        except Exception:
            return True  # err on the side of keeping the worktree

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
        if not getattr(self, "tracking_enabled", True):
            return
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
                "⚠ " + "; ".join(notes) + ".\nUse Ctrl-G → session (s) to handle them.",
                seconds=12.0,
            )

    def _cleanup_stale_worktree(self, info) -> bool:
        # Integrate a dormant worktree's pending commits (if any) and delete it.
        # Returns False (keep + flag) when it has uncommitted changes or its work
        # conflicts with the base and so needs the user/agent to resolve.
        repo = GitRepo(info.path)
        if repo.merge_in_progress() or repo.has_changes():
            return False
        branch = repo.current_branch()
        if branch.startswith("agit/") and self.base_repo.log_range(self._base_branch, branch):
            if not repo.merge(self._base_branch):  # base into the stale branch
                repo.merge_abort()
                return False
            self.base_repo.merge_ff_only(branch)
        self._worktrees().remove(info.name)
        self._debug(f"cleaned stale worktree '{info.name}'")
        return True

    def _delete_orphan_merged_branches(self) -> None:
        # Remove agit/* branches that no worktree checks out and that are already
        # contained in the base branch (stale leftovers).
        try:
            checked_out = {entry.get("branch") for entry in self.base_repo.worktree_list()}
            for branch in self.base_repo.list_branches("agit/"):
                if branch in checked_out:
                    continue
                if not self.base_repo.log_range(self._base_branch, branch):
                    self.base_repo.delete_branch(branch, force=True)
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
        if result == "conflict" and not getattr(self, "_exiting", False):
            # On exit there is no UI to drive a resolution; the work stays on its
            # branch for the next startup / session menu to surface.
            self._prompt_resolve_conflict(self.repo.current_branch())

    def _integrate_turn_or_conflict(self) -> str:
        # Try to fast-forward the current session's turn branch into the base.
        # Returns "integrated" (base advanced), "conflict" (the merge was backed
        # out and needs resolution), or "skip" (nothing to integrate).
        if self.worktree is None or self._base_branch is None or self.merge_ctx:
            return "skip"
        if getattr(self, "_integration_paused", False):
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
        backend = getattr(getattr(self, "backend", None), "name", "?")
        choice = self._select_popup(
            f"Session '{self.name}' ({backend}) finished, but its changes conflict with '{self._base_branch}'.",
            [
                "Merge automatically (agent resolves conflicts)",
                "Merge manually (you resolve here, then Complete merge)",
                "Leave for later (resolve via Ctrl-G → session)",
            ],
        )
        if not choice or choice.startswith("Leave"):
            self._set_message(
                f"'{self.name}' has unintegrated work that conflicts with '{self._base_branch}'. "
                "Resolve it any time via Ctrl-G → session.",
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
        if getattr(self, "worktree", None) is None or getattr(self, "_base_branch", None) is None:
            return
        if self.merge_ctx or self.repo.merge_in_progress() or self.repo.has_changes():
            return
        branch = self.repo.current_branch()
        if not branch.startswith("agit/"):
            return  # already detached / on no turn branch
        try:
            if not self.base_repo.log_range(self._base_branch, branch):
                # Nothing ahead of base: just drop the empty turn branch.
                self._advance_base_to(branch)
                return
            if self.repo.merge(self._base_branch):
                self._advance_base_to(branch)
            else:
                self.repo.merge_abort()
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
        current = self.base_repo.current_branch()
        if current != self._base_branch:
            raise RuntimeError(
                f"base repo is on '{current}', not the integration branch '{self._base_branch}'"
            )
        self.base_repo.merge_ff_only(source_branch)
        self.repo.switch_detach(self._base_branch)
        if self.repo.current_branch() != source_branch:
            self.base_repo.delete_branch(source_branch, force=True)
        # The base moved; other idle sessions should fast-forward onto it.
        self._base_advanced = True

    def _sync_idle_worktrees_to_base(self) -> None:
        # Keep every idle session's worktree current with the (just-advanced) base:
        # a session with nothing of its own is re-pointed onto the base, and one
        # that has its own committed work has the new base commits merged into its
        # turn branch (cleanly, or skipped on conflict) — see `_align_session_to_base`.
        # Only idle, clean worktrees are touched, so in-flight work is left alone.
        if getattr(self, "_integration_paused", False):
            return  # base switched out-of-band; don't re-point worktrees meanwhile
        for index in range(len(self.sessions)):
            if index == self.active_index:
                repo, in_flight = getattr(self, "repo", None), getattr(self, "agent_in_flight", False)
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
        if getattr(self, "worktree", None) is None or not self.repo.is_detached():
            return
        self.turn = (self.turn or 0) + 1
        next_branch = self._worktrees().turn_branch(self.name, self.turn, backend=self.backend.name)
        self.repo.switch(next_branch, create=True)

    def _merge_resolution_prompt(self, files: list[str], context: str) -> str:
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
            os.write(self.master_fd, payload)
        except OSError:
            return
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
            os.write(fd, b"\r")
        except OSError:
            return
        if self.merge_ctx is not None and self.master_fd == fd:
            self.merge_ctx["prompt_sent_at"] = time.monotonic()

    def _begin_agent_merge(self, source_branch: str) -> None:
        # A merge is in progress (conflicted) in the worktree. Ask the session's
        # agent to resolve it; aGiT finalizes once the conflicts are gone.
        files = self.repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self._base_branch, paths=files)
        except Exception:
            context = ""
        self._inject_prompt(self._merge_resolution_prompt(files, context))
        self.merge_ctx = {
            "source_branch": source_branch,
            "context": context,
            "started": time.monotonic(),
            "auto_tried": False,
            "prompt_sent_at": None,  # set once the submit Enter goes out
        }
        self.agent_in_flight = True
        self._set_message(
            f"Merge conflict in {', '.join(files) or 'this session'} — asking the agent to resolve it… "
            "aGiT will commit the merge once the agent finishes (or use Ctrl-G → session → Complete merge).",
            seconds=12.0,
        )
        self._render()

    def _note_backend_session_change(self, new_session_id: str | None) -> None:
        # If the worktree's active conversation changed to a different backend
        # session that aGiT didn't start, the user likely started it from inside
        # the backend. Warn once that such sessions share this branch.
        previous = self.state.backend_session_id
        if (
            self.worktree is not None
            and previous
            and new_session_id
            and new_session_id != previous
            and not getattr(self, "_warned_backend_session", False)
        ):
            self._warned_backend_session = True
            self._set_message(
                "Detected a new conversation started inside the backend. Its changes are tracked on "
                f"this session's branch. To get a separate branch, start sessions with Ctrl-G → session → New.",
                seconds=12.0,
            )

    def _maybe_complete_agent_merge(self) -> None:
        # Auto-finalize a pending agent merge only after the agent has actually
        # engaged with the injected prompt (produced output after we submitted
        # it) and then gone idle — never before the prompt has even been sent.
        ctx = self.merge_ctx
        if not ctx or ctx.get("auto_tried"):
            return
        sent_at = ctx.get("prompt_sent_at")
        if not sent_at:  # the submit Enter has not gone out yet
            return
        if self.last_child_output <= sent_at:  # agent has not responded yet
            return
        if time.monotonic() - self.last_child_output < self.CHILD_IDLE_SECONDS + 2:
            return
        ctx["auto_tried"] = True
        self._finalize_agent_merge()

    def _finalize_agent_merge(self) -> bool:
        ctx = self.merge_ctx
        if not ctx:
            return False
        source_branch = ctx["source_branch"]
        try:
            if not self.repo.merge_in_progress():
                self.merge_ctx = None  # already resolved/aborted elsewhere
                return False
            self.repo.add_all()
            if self.repo.has_conflict_markers() or self.repo.unmerged_paths():
                self._set_message(
                    "Conflict markers remain. Resolve them (or ask the agent again), "
                    "then Ctrl-G → session → Complete merge.",
                    seconds=10.0,
                )
                return False
            self.repo.commit(
                build_agent_merge_message(
                    session_name=self.name,
                    base_branch=self._base_branch,
                    source_branch=source_branch,
                    agit_session_id=self.state.session_id,
                    backend=self.backend.name,
                    backend_session_id=self.state.backend_session_id,
                    conflicting_commits=ctx.get("context"),
                )
            )
            self._advance_base_to(source_branch)
            self.merge_ctx = None
            self.agent_in_flight = False
            self._set_message(
                f"Merge resolved and committed — integrated '{self.name}' into {self._base_branch}.",
                seconds=6.0,
            )
            self._render()
            return True
        except Exception as error:
            self._debug(f"finalize agent merge failed: {error!r}")
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

    # --- switch base branch ---

    def _base_switch_candidates(self) -> list[str]:
        # User branches the base could switch to (never aGiT's transient ones).
        try:
            branches = self.base_repo.list_branches()
        except Exception:
            return []
        return [b for b in branches if not b.startswith("agit/") and b != self._base_branch]

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
        target = arg.strip() if arg.strip() in candidates else self._select_popup(
            f"Switch base from '{self._base_branch}' to:", candidates
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
        try:
            if repo is None or repo.merge_in_progress() or repo.has_changes():
                return True
            branch = repo.current_branch()
            return branch.startswith("agit/") and bool(self.base_repo.log_range(self._base_branch, branch))
        except Exception:
            return True

    def _unintegrated_session_names(self) -> list[str]:
        blocked: list[str] = []
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
                "Resolve it (Ctrl-G → session), then try again.",
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
        if getattr(self, "worktree", None) is None:
            return
        try:
            if self.repo.has_changes() or self.repo.merge_in_progress():
                return
            self.repo.switch_detach(self._base_branch)
            self.turn = 0
        except Exception as error:
            self._debug(f"re-point failed for '{self.name}': {error!r}")

    def _repoint_all_sessions_to_base(self) -> None:
        # Re-point every live session at the new base without stopping any of them.
        self._repoint_current_to_base()  # active, in place
        if len(self.sessions) > 1:
            active_snapshot = capture_session(self)
            for index, session in enumerate(self.sessions):
                if index == self.active_index:
                    continue
                restore_session(self, session)
                try:
                    self._repoint_current_to_base()
                finally:
                    for field in SESSION_FIELDS:
                        setattr(session, field, getattr(self, field))
            restore_session(self, active_snapshot)
        self.sessions[self.active_index] = capture_session(self)

    def _session_menu(self) -> None:
        self.sessions[self.active_index] = capture_session(self)  # refresh active snapshot
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
            if value == self.active_index:
                self._integrate_active_session()
            else:
                self._switch_active(value)
        elif kind == "resume-past":
            self._resume_session_menu()
        elif kind == "complete-merge":
            self._finalize_agent_merge()
        elif kind == "resolve":
            self._resolve_dormant_worktree(value)
        elif kind == "resume":
            self._new_session(value)
        elif kind == "new":
            self._prompt_new_session()
        else:
            self._stop_session_menu()

    def _active_has_pending(self) -> bool:
        # True if the active session has committed work not yet in the base.
        if self.worktree is None or self._base_branch is None:
            return False
        try:
            return bool(self.base_repo.log_range(self._base_branch, self.repo.current_branch()))
        except Exception:
            return False

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
        self.merge_auto_resolve = choice.startswith("Merge automatically")
        self._start_merge_for_active(auto=self.merge_auto_resolve)

    def _start_merge_for_active(self, *, auto: bool) -> None:
        # Begin merging base into the (now active) session's branch.
        if self.worktree is None or self._base_branch is None:
            return
        source_branch = self.repo.current_branch()
        try:
            clean = self.repo.merge(self._base_branch)
        except Exception as error:
            self._set_message(f"Could not start merge: {error}", seconds=8.0)
            self._render()
            return
        if clean:
            self._advance_base_to(source_branch)
            self._set_message(f"Integrated '{self.name}' into {self._base_branch} (no conflicts).", seconds=6.0)
            self._render()
            return
        if auto:
            self._begin_agent_merge(source_branch)
        else:
            files = self.repo.unmerged_paths()
            try:
                context = self.base_repo.log_range(source_branch, self._base_branch, paths=files)
            except Exception:
                context = ""
            self.merge_ctx = {"source_branch": source_branch, "context": context, "started": time.monotonic(), "auto_tried": True}
            self._set_message(
                f"Conflicts in {', '.join(files) or 'this session'}. Resolve them here (edit the files or ask the agent), "
                "then Ctrl-G → session → Complete merge.",
                seconds=12.0,
            )
            self._render()

    RESUME_LIST_LIMIT = 20

    def _resumable_sessions(self) -> list:
        # Past conversations come straight from the backend agent's own session
        # record for the repo aGiT was launched in. Worktrees are ephemeral
        # (emptied on quit), so we never key the list off them; resuming is done
        # by session id, which the backend resolves regardless of directory.
        try:
            refs = self.backend.list_sessions(self.base_repo.repo)
        except Exception as error:
            self._debug(f"list_sessions failed: {error!r}")
            return []
        refs = sorted(refs, key=lambda ref: getattr(ref, "updated", 0) or 0, reverse=True)
        return refs[: self.RESUME_LIST_LIMIT]

    def _agit_named_sessions(self) -> dict:
        # Friendly names for conversations aGiT itself created/named, keyed by
        # backend session id, recovered from the durable repo-root record. Used
        # only to label/resume the list that comes from the backend.
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
        return names

    def _resume_session_menu(self) -> None:
        sessions = self._resumable_sessions()
        if not sessions:
            self._set_message("No past conversations found to resume.")
            self._render()
            return
        live_ids = {
            getattr(getattr(s, "state", None), "backend_session_id", None) for s in self.sessions
        }
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
        return any(
            _sanitize_name(self._session_name(index)) == sanitized
            for index in range(len(self.sessions))
        )

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
        self.sessions[self.active_index] = capture_session(self)
        self.active_index = index
        restore_session(self, self.sessions[index])
        self.scroll_back = 0
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Switched to session '{self._session_name(index)}'")
        self._render()

    def _pump_background(self, session) -> None:
        # Keep a background session's screen current by draining + feeding its
        # output. No render and no commit here — committing happens separately in
        # _service_background_sessions (synchronously, via _with_session), since
        # the async parse worker is not safe to run while a session is swapped out.
        if session not in self.sessions:
            return
        saved = capture_session(self)
        restore_session(self, session)
        died = False
        try:
            output = self._drain_child_output()
            if output is None:
                died = True
            elif output:
                self.last_child_output = time.monotonic()
                self._answer_terminal_queries(output)
                self._feed_child_output(output)
        finally:
            for field in SESSION_FIELDS:
                setattr(session, field, getattr(self, field))
            restore_session(self, saved)
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
            self.sessions[self.active_index] = capture_session(self)
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
        self.sessions[self.active_index] = capture_session(self)
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Worktree was deleted externally; recreated '{info.name}'.", seconds=8.0)
        self._render()

    def _with_session(self, session, fn):
        if session not in self.sessions:
            return None
        saved = capture_session(self)
        restore_session(self, session)
        try:
            return fn()
        finally:
            for field in SESSION_FIELDS:
                setattr(session, field, getattr(self, field))
            restore_session(self, saved)

    def _commit_and_integrate_background(self) -> str:
        if self.repo.has_changes():
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
        try:
            info, repo = self._open_session_worktree(name)
        except Exception as error:
            self._set_message(f"Could not create worktree: {error}", seconds=8.0)
            self._render()
            return
        self.sessions[self.active_index] = capture_session(self)
        for field, value in default_session_fields().items():
            setattr(self, field, value)
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
        self.backend = make_proxy_agent(self.state.backend)
        self.actions = AgitActions(self.repo, self.state, verbose=self.verbose)
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        self.sessions.append(capture_session(self))
        self.active_index = len(self.sessions) - 1
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
        active = index == self.active_index
        if not active:
            saved = capture_session(self)
            restore_session(self, self.sessions[index])
        try:
            if commit:
                self._commit_latest_turn_sync()
            self._stop_file_watcher()
            if self.child_pid:
                try:
                    os.kill(self.child_pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
            worktree = self.worktree
        finally:
            if not active:
                restore_session(self, saved)
        # Leave the worktree's branch on disk (recoverable); just drop the live session.
        self._debug(f"stopped session index={index} worktree={getattr(worktree, 'path', None)}")
        self.sessions.pop(index)
        if index < self.active_index:
            self.active_index -= 1
        elif active:
            self.active_index = min(self.active_index, len(self.sessions) - 1)
            restore_session(self, self.sessions[self.active_index])
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
        self.sessions.pop(self.active_index)
        self.active_index = min(self.active_index, len(self.sessions) - 1)
        restore_session(self, self.sessions[self.active_index])
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
        recent = [t for t in getattr(self, "_relaunch_times", []) if now - t < 12.0]
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
        newest = max(refs, key=lambda ref: ref.updated)
        if newest.id == self.state.backend_session_id:
            self._set_message(f"Already tracking the most recent session ({_short_session(newest.id)}).")
            self._render()
            return
        self.state.backend_session_id = newest.id
        self.state.last_backend_message_id = None
        self._initialize_session_baseline()
        self._set_message(f"Now tracking session {_short_session(newest.id)}")
        self._render()

    def _resolve_session_id(self, arg: str) -> str | None:
        ids = [ref.id for ref in self.backend.list_sessions(self.repo.repo)]
        if arg in ids:
            return arg
        matches = [session_id for session_id in ids if session_id.startswith(arg)]
        return matches[0] if len(matches) == 1 else None

    def _start_file_watcher(self) -> None:
        if Observer is None:
            return
        observer = Observer()
        observer.schedule(RepoChangeHandler(self.repo.repo, self.file_change_event), str(self.repo.repo), recursive=True)
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
        assert self.master_fd is not None
        while self.running:
            timeout = 0.016 if self._render_pending else 0.2
            background = self._background_fds()
            readable, _, _ = select.select([sys.stdin.fileno(), self.master_fd, *background], [], [], timeout)
            for fd in readable:
                if fd in background:
                    self._pump_background(background[fd])
            if self.master_fd in readable:
                output = self._drain_child_output()
                if output is None:
                    sample = self.last_child_output_sample[-2048:].decode(errors="replace").replace("\x1b", "\\x1b")
                    self._debug(f"master_fd closed (backend gone); last_output={sample!r}")
                    self._raw_capture("EOF", b"")
                    if self._handle_active_session_exit():
                        continue
                    if self._relaunch_backend_or_exit():
                        continue
                    break
                if output:
                    self._raw_capture("<", output)
                    self.last_child_output = time.monotonic()
                    self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
                    self._answer_terminal_queries(output)
                    self._sync_terminal_modes(output)
                    self._track_sync_update(output)
                    self._feed_child_output(output)
                    self._render_output()
            if sys.stdin.fileno() in readable:
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
                    if not self._confirm_exit():
                        self._render()
                        continue
                    if not self._confirm_terminate_background_sessions():
                        self._render()
                        continue
                    self._finalize_pending_work()
                    self._exit_child()
                    break
                if local_echo:
                    self._render_status(local_echo.decode(errors="ignore"))
                if self.input.capturing:
                    self._render()
                elif was_capturing and command is None:
                    self._render()
                if forwarded:
                    self.scroll_back = 0  # interacting snaps back to the live view
                    submit = any(chunk in {b"\r", b"\n"} for chunk in forwarded)
                    self._update_passthrough_prompt(forwarded)
                    submitted_prompt = ""
                    if submit:
                        submitted_prompt = self.passthrough_prompt.decode(errors="ignore").strip()
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
                        os.write(self.master_fd, b"".join(forwarded))
                if command:
                    self._run_command(command)
            self._flush_pending_render()
            self._flush_pending_enter()
            if self.merge_ctx:
                # A merge is being resolved; don't make normal commits meanwhile.
                self._maybe_complete_agent_merge()
            else:
                self._check_base_branch_drift()  # pause merging before any integration runs
                self._resume_pending_prompt_if_ready()
                self._ensure_worktree_alive()
                self._maybe_agent_commit()
                self._service_background_sessions()
                self._poll_base_advanced()
                self._warn_if_base_edited()
                self._warn_if_cwd_drifted()
            if self._base_advanced:
                self._base_advanced = False
                self._sync_idle_worktrees_to_base()
            if self.child_pid is not None:
                done, status = os.waitpid(self.child_pid, os.WNOHANG)
                if done:
                    exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
                    sample = self.last_child_output_sample[-512:].decode(errors="replace").replace("\x1b", "\\x1b")
                    self._debug(f"child exited pid={self.child_pid} status={status} exit_code={exit_code} last_output={sample!r}")
                    # Same handling as the master_fd-EOF path: switch away (multi
                    # session) or relaunch+resume (single) so Claude exiting its own
                    # picker on Esc doesn't take aGiT down. These two detectors race;
                    # whichever sees the exit first must relaunch.
                    if self._handle_active_session_exit():
                        continue
                    if self._relaunch_backend_or_exit():
                        continue
                    return exit_code
        return 0

    def _drain_child_output(self) -> bytes | None:
        # Read all currently-available output in one go (capped) and render once,
        # instead of re-rendering after every 4 KB. During heavy output (e.g.
        # fast scrolling in OpenCode) this keeps the PTY drained so the backend's
        # writes never block, which otherwise stalls/kills the backend.
        assert self.master_fd is not None
        chunks: list[bytes] = []
        total = 0
        # Bound per-iteration output so the (pure-Python) pyte parse stays small
        # and the loop keeps draining the PTY promptly; leftover output is read
        # on the next iteration.
        while total < 262_144:
            try:
                data = os.read(self.master_fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            total += len(data)
            readable, _, _ = select.select([self.master_fd], [], [], 0)
            if self.master_fd not in readable:
                break
        if not chunks:
            return None  # EOF or read error with nothing buffered
        return b"".join(chunks)

    def _diag_path(self, kind: str):
        # Diagnostic logs live in the *base* repo's .agit/ (one file per run), not
        # the session worktree's — the worktree is removed on exit, which would
        # both destroy the log and recreate a half-dir that breaks the next resume.
        base = getattr(self, "base_repo", None)
        root = (base.repo if base is not None else self.repo.repo) / ".agit"
        run = getattr(self, "_diag_run", "") or time.strftime("%Y%m%d-%H%M%S")
        return root / f"{kind}-{run}.log"

    def _debug(self, message: str) -> None:
        if not getattr(self, "debug_proxy", False):
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
        if not getattr(self, "raw_capture", False):
            return
        try:
            path = self._diag_path("proxy-raw")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S.')}{int(time.time() * 1000) % 1000:03d} {tag} {data!r}\n")
        except OSError:
            pass

    def _sanitize_state_trace(self) -> None:
        changed = False
        clean = []
        for item in self.state.pending_trace():
            role = item.get("role")
            content = item.get("content")
            if role == "agent" and isinstance(content, str) and self.backend.is_event_blob(content):
                changed = True
                continue
            clean.append(item)
        if changed:
            self.state.data["pending_trace"] = clean
            self.state.save()
            self._debug("removed raw backend event blob from pending trace")

    def _recover_nonempty_session(self):
        # When the recorded conversation turns out empty, fall back to this
        # worktree's newest conversation that has real content. Returns
        # (session_id, ExportedSession) or None if nothing resumable exists.
        try:
            candidate = self.backend.latest_session_id(self.repo.repo)
        except Exception as error:
            self._debug(f"recover non-empty session failed: {error!r}")
            return None
        if not candidate or candidate == self.state.backend_session_id:
            return None
        self._stage_backend_resume(candidate)
        session = self.backend.export_session(self.repo.repo, candidate)
        if session and session.turns:
            return candidate, session
        return None

    def _initialize_session_baseline(self) -> None:
        if not self._should_continue_session():
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            return
        # Stage the conversation's transcript into this worktree so both the
        # export below and `--resume` in `_spawn` can find it — the backend may
        # have recorded it under a different directory (the repo root before aGiT,
        # or a previous worktree).
        self._stage_backend_resume(self.state.backend_session_id)
        session = self.backend.export_session(self.repo.repo, self.state.backend_session_id)
        if not session or not session.turns:
            # The recorded session has no actual conversation — e.g. a stale pointer
            # left by a previous run that adopted an empty session Claude spun up on
            # resume/picker. Rather than drop into a blank session, recover this
            # worktree's most recent conversation that actually has content (the
            # backend's latest_session_id skips empty transcripts).
            recovered = self._recover_nonempty_session()
            session = recovered[1] if recovered else None
            if recovered:
                self._debug(f"recorded session empty; recovered non-empty {recovered[0]}")
                self.state.backend_session_id = recovered[0]
            else:
                self.state.backend_session_id = None
                self.state.last_backend_message_id = None
                return
        if session.model:
            self.state.model = session.model
        complete = [turn for turn in session.turns if turn.assistant_message_id]
        self.state.last_backend_message_id = complete[-1].assistant_message_id if complete else None
        self.state.clear_trace()

    def _mirror_session_to_base(self, session_id: str | None) -> None:
        # Link an aGiT-born conversation's transcript into the base repo's project
        # dir so a plain CLI run in the repo root can see/continue it. Idempotent
        # (no-op once linked, or when the session already lives at the base, or for
        # backends without per-directory storage).
        fn = getattr(self.backend, "mirror_to_base", None)
        if fn is None or not session_id or getattr(self, "worktree", None) is None:
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
        if self.stream is not None:
            try:
                self.stream.feed(_PYTE_HOSTILE_CSI_RE.sub(b"", output))
            except Exception as error:  # never let a parse hiccup kill the session
                self._debug(f"pyte feed error: {error!r}")

    def _sync_terminal_modes(self, output: bytes) -> None:
        # OpenCode enables mouse reporting on its PTY. Because aGiT renders the
        # screen itself, the host terminal never sees those mode switches unless
        # we mirror them explicitly.
        for mode in (b"9", b"1000", b"1001", b"1002", b"1003", b"1004", b"1005", b"1006", b"1007", b"1015", b"1016", b"2004"):
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

    def _detect_host_terminal(self) -> None:
        # Ask the host terminal the same questions OpenCode asks on startup and
        # cache the raw answers. OpenCode adapts its entire theme to the
        # reported foreground/background, so relaying the real values is what
        # makes its colors match a native session.
        queries = bytearray(b"\x1b]10;?\x07\x1b]11;?\x07")
        for index in range(16):
            queries += b"\x1b]4;%d;?\x07" % index
        queries += b"\x1b[c"  # primary device attributes; also a response sentinel
        try:
            os.write(sys.stdout.fileno(), bytes(queries))
        except OSError:
            return
        buffer = bytearray()
        deadline = time.monotonic() + 0.5
        stdin_fd = sys.stdin.fileno()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([stdin_fd], [], [], deadline - time.monotonic())
            if stdin_fd not in readable:
                break
            try:
                chunk = os.read(stdin_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            if re.search(rb"\x1b\[\?[0-9;]*c", bytes(buffer)):
                break
        self._parse_host_terminal_responses(bytes(buffer))

    def _parse_host_terminal_responses(self, data: bytes) -> None:
        if not data:
            return
        fg = re.search(rb"\x1b\]10;([^\x07\x1b]*)(?:\x07|\x1b\\)", data)
        if fg:
            self.host_fg_value = fg.group(1)
        bg = re.search(rb"\x1b\]11;([^\x07\x1b]*)(?:\x07|\x1b\\)", data)
        if bg:
            self.host_bg_value = bg.group(1)
        for match in re.finditer(rb"\x1b\]4;(\d+);([^\x07\x1b]*)(?:\x07|\x1b\\)", data):
            self.host_palette[match.group(1)] = match.group(2)
        da = re.search(rb"\x1b\[\?[0-9;]*c", data)
        if da:
            self.host_da = da.group(0)
        self._debug(f"host terminal fg={self.host_fg_value!r} bg={self.host_bg_value!r} palette={len(self.host_palette)} da={self.host_da!r}")

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
                os.write(self.master_fd, bytes(response))
            except OSError:
                pass

    def _track_sync_update(self, output: bytes) -> None:
        # Honor the synchronized-update mode (DECSET 2026): backends wrap a
        # multi-write repaint in BSU (?2026h) / ESU (?2026l) so consumers can
        # apply it atomically. While inside such an update aGiT defers its own
        # repaint, so it never paints a half-drawn frame (the cause of tearing).
        # Only the last marker in the chunk decides the resulting state; a
        # stuck-open update is bounded by SYNC_MAX_HOLD in the paint deciders.
        begin = output.rfind(b"\x1b[?2026h")
        end = output.rfind(b"\x1b[?2026l")
        if begin == -1 and end == -1:
            return
        in_update = begin > end
        if in_update and not self._in_sync_update:
            self._sync_since = time.monotonic()
        self._in_sync_update = in_update

    def _sync_hold(self, now: float) -> bool:
        # True while a backend synchronized-update should still defer the paint.
        return self._in_sync_update and now - self._sync_since < self.SYNC_MAX_HOLD

    def _render_output(self) -> None:
        # Coalesce repaints driven by a flood of backend output (e.g. fast
        # scrolling) to ~30fps so aGiT does not overwhelm the host terminal's
        # stdout, which would block the loop and back up the backend's PTY.
        now = time.monotonic()
        if self._sync_hold(now):
            self._render_pending = True
            return
        if now - self._last_render >= self.RENDER_MIN_INTERVAL:
            self._last_render = now
            self._render_pending = False
            self._render()
        else:
            self._render_pending = True

    def _flush_pending_render(self) -> None:
        if not self._render_pending:
            return
        now = time.monotonic()
        if self._sync_hold(now):
            return
        if now - self._last_render >= self.RENDER_MIN_INTERVAL:
            self._last_render = now
            self._render_pending = False
            self._render()

    def _cursor_sequence(self) -> str:
        # The trailing sequence that positions (and shows) or hides the cursor.
        assert self.screen is not None
        if self.scroll_back > 0:
            # While scrolled into history, keep the cursor hidden (its live
            # position is not meaningful for the displayed lines).
            return "\x1b[?25l"
        cursor = self.screen.cursor
        cursor_row = min(cursor.y + 1, max(self.rows - 1, 1))
        cursor_col = min(cursor.x + 1, self.cols)
        return f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h"

    def _render(self) -> None:
        if self.screen is None:
            return
        # Paint the whole screen inside one synchronized update (DECSET 2026) so
        # the host terminal applies the frame atomically and never shows it
        # half-drawn. Terminals that don't support 2026 ignore the markers and
        # fall back to the previous (unwrapped) full-repaint behaviour.
        parts = ["\x1b[?2026h\x1b[0m\x1b[?25l\x1b[H"]
        selection = self._selection_ranges()
        for index, cells in enumerate(self._visible_lines()):
            parts.append("\x1b[0m" + self._render_line(cells, selection.get(index)))
            parts.append("\r\n")
        parts.append(self._status_line())
        if self.input.capturing:
            self._append_command_palette(parts)
        elif self.message and (getattr(self, "_message_sticky", False) or time.monotonic() < self.message_until):
            self._append_message_popup(parts, self.message)
        parts.append(self._cursor_sequence())
        parts.append("\x1b[?2026l")
        os.write(sys.stdout.fileno(), "".join(parts).encode())

    def _append_command_palette(self, parts: list[str]) -> None:
        width = min(max(52, self.cols // 2), self.cols - 4)
        row = 2
        col = max(2, (self.cols - width) // 2)
        text = self.input.text()
        matches = self.input.matches()
        selected = self.input.selected()
        lines = [
            "aGiT commands",
            f"> {text}",
            "Up/Down selects. Tab completes. Enter runs. Ctrl-C exits.",
            "",
        ]
        lines.extend(matches[:8])
        self._append_box(parts, row, col, width, lines, highlight=selected)

    def _append_message_popup(self, parts: list[str], message: str) -> None:
        width = min(max(52, self.cols // 2), self.cols - 4)
        row = 2
        col = max(2, (self.cols - width) // 2)
        self._append_box(parts, row, col, width, message.splitlines() or [message])

    def _append_box(self, parts: list[str], row: int, col: int, width: int, lines: list[str], highlight: str | None = None) -> None:
        inner = max(width - 2, 1)
        border_top = "┌" + "─" * inner + "┐"
        border_bottom = "└" + "─" * inner + "┘"
        box_lines = [border_top]
        wrapped_lines: list[str] = []
        for line in lines:
            wrapped_lines.extend(textwrap.wrap(line, width=inner) or [""])
        max_body = max(self.rows - row - 2, 1)
        for line in wrapped_lines[:max_body]:
            content = line[:inner].ljust(inner)
            if highlight and line == highlight:
                box_lines.append("│" + "\x1b[7m" + content + "\x1b[0m" + "│")
            else:
                box_lines.append("│" + content + "│")
        box_lines.append(border_bottom)
        for offset, line in enumerate(box_lines):
            if row + offset >= self.rows:
                break
            parts.append(f"\x1b[{row + offset};{col}H\x1b[0m{line}")

    def _plain_row(self, row: int) -> str:
        assert self.screen is not None
        cells = self.screen.buffer.get(row, {})
        chars = []
        for col in range(self.cols):
            cell = cells.get(col)
            chars.append((getattr(cell, "data", None) or " ")[:1] if cell is not None else " ")
        return "".join(chars)

    def _render_row(self, row: int) -> str:
        assert self.screen is not None
        return self._render_line(self.screen.buffer.get(row, {}))

    def _render_line(self, cells, sel: tuple[int, int] | None = None) -> str:
        rendered = []
        current = ""  # SGR body currently applied on the host terminal ("" == default)
        sel_start, sel_end = sel if sel else (-1, -1)
        for col in range(self.cols):
            cell = cells.get(col)
            base = "" if cell is None else self._cell_sgr(cell)
            char = (cell.data or " ") if cell is not None else " "
            if sel is not None and sel_start <= col <= sel_end:
                style = (base + ";7") if base else "7"  # reverse-video the selection
            else:
                style = base
            if style != current:
                rendered.append("\x1b[" + (style or "0") + "m")
                current = style
            rendered.append(char)
        if current:
            rendered.append("\x1b[0m")
        return "".join(rendered)

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
        # Map each selected display row to its inclusive (start_col, end_col).
        if not (self.sel_active and self.sel_anchor and self.sel_point):
            return {}
        (r1, c1), (r2, c2) = sorted([self.sel_anchor, self.sel_point])
        ranges: dict[int, tuple[int, int]] = {}
        for row in range(r1, r2 + 1):
            start = c1 if row == r1 else 0
            end = c2 if row == r2 else self.cols - 1
            ranges[row] = (start, end)
        return ranges

    def _copy_selection(self) -> None:
        lines = self._visible_lines()
        text_lines = []
        for row, (start, end) in sorted(self._selection_ranges().items()):
            cells = lines[row] if row < len(lines) else {}
            text = "".join((cells.get(x).data if cells.get(x) else " ") for x in range(start, end + 1))
            text_lines.append(text.rstrip())
        text = "\n".join(text_lines).strip("\n")
        if not text.strip():
            return
        self._copy_to_clipboard(text)
        self._set_message(f"Copied {len(text)} char(s) to clipboard.", seconds=2.0)

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
        history = getattr(getattr(self, "screen", None), "history", None)
        return len(history.top) if history is not None else 0

    def _scroll(self, delta: int) -> None:
        new_back = max(0, min(self.scroll_back + delta, self._history_len()))
        if new_back != self.scroll_back:
            self.scroll_back = new_back
            # Selection coordinates refer to the displayed view, which just
            # shifted; drop any in-progress selection.
            self.sel_active = False
            self.sel_anchor = self.sel_point = None
            self._render()

    def _visible_lines(self) -> list:
        # The (rows-1) lines to draw. When scrolled back, splice in history lines
        # that scrolled off the top so the user can read earlier messages.
        assert self.screen is not None
        rows = max(self.rows - 1, 1)
        live = [self.screen.buffer.get(row, {}) for row in range(rows)]
        if self.scroll_back <= 0 or not self._history_len():
            return live
        history = list(self.screen.history.top)
        combined = history + live
        end = len(combined) - self.scroll_back
        end = max(rows, min(end, len(combined)))
        return combined[end - rows:end]

    def _cell_sgr(self, cell) -> str:
        # Reproduce exactly what OpenCode rendered into this cell, including the
        # original colour encoding (see _hex_color_code), so the cell is
        # byte-equivalent in colour to a native session on the same terminal.
        codes = []
        if getattr(cell, "bold", False):
            codes.append("1")
        if getattr(cell, "italics", False):
            codes.append("3")
        if getattr(cell, "underscore", False):
            codes.append("4")
        if getattr(cell, "blink", False):
            codes.append("5")
        if getattr(cell, "reverse", False):
            codes.append("7")
        if getattr(cell, "strikethrough", False):
            codes.append("9")
        fg = self._color_code(getattr(cell, "fg", "default"), foreground=True)
        bg = self._color_code(getattr(cell, "bg", "default"), foreground=False)
        if fg:
            codes.append(fg)
        if bg:
            codes.append(bg)
        return ";".join(codes)

    def _color_code(self, color: str, *, foreground: bool) -> str | None:
        if color in {"default", ""}:
            return None
        base = 30 if foreground else 40
        bright_base = 90 if foreground else 100
        colors = {
            "black": 0,
            "red": 1,
            "green": 2,
            "brown": 3,
            "yellow": 3,
            "blue": 4,
            "magenta": 5,
            "cyan": 6,
            "white": 7,
            "grey": 7,
            "gray": 7,
        }
        if len(color) == 6 and all(char in "0123456789abcdefABCDEF" for char in color):
            return self._hex_color_code(color.lower(), foreground=foreground)
        if color.startswith("bright"):
            key = color.removeprefix("bright")
            return str(bright_base + colors[key]) if key in colors else None
        return str(base + colors[color]) if color in colors else None

    def _hex_color_code(self, color: str, *, foreground: bool) -> str:
        # Re-emit a hex colour in the same encoding OpenCode used, decided by the
        # shared terminal colour depth. Truecolor terminals get 24-bit colour;
        # 256-colour terminals (e.g. Apple Terminal) get the original palette
        # index so their own palette renders it, exactly like a native session.
        red = int(color[0:2], 16)
        green = int(color[2:4], 16)
        blue = int(color[4:6], 16)
        prefix = "38" if foreground else "48"
        mode = getattr(self, "color_mode", "truecolor")
        if mode == "truecolor":
            return f"{prefix};2;{red};{green};{blue}"
        index = _REVERSE_256.get(color)
        if index is None:
            index = _nearest_256(red, green, blue)
        if mode == "256":
            return f"{prefix};5;{index}"
        # 16-colour terminals: fall back to the nearest ANSI base/bright code.
        ansi = index if index < 16 else _nearest_ansi16(red, green, blue)
        base = 30 if foreground else 40
        bright_base = 90 if foreground else 100
        return str(base + ansi) if ansi < 8 else str(bright_base + ansi - 8)

    def _status_line(self) -> str:
        declined = len(getattr(self, "_user_declined", []))
        session_id = self.state.backend_session_id
        session = f"{self.name or 'session'}" + (f" [{_short_session(session_id)}]" if session_id else "")
        base = getattr(self, "_base_branch", None)
        if base and getattr(self, "worktree", None) is not None:
            session += f" → {base}"  # the branch this session's work merges into
        left = f" aGiT Ctrl-G | {session} | {self.backend.name} "
        if getattr(self, "scroll_back", 0) > 0:
            right = f" SCROLLBACK -{self.scroll_back} (scroll down to resume) "
        else:
            right = f" unstaged:{declined} " if declined else ""
        padding = " " * max(self.cols - len(left) - len(right), 0)
        return f"\x1b[7m{left}{padding}{right}\x1b[0m"

    def _render_status(self, text: str) -> None:
        prompt = text.replace("\r", "").replace("\n", "")
        line = f" aGiT> {prompt}"[: self.cols].ljust(self.cols)
        os.write(sys.stdout.fileno(), f"\x1b[{self.rows};1H\x1b[7m{line}\x1b[0m".encode())

    def _enter_host_screen(self) -> None:
        os.write(sys.stdout.fileno(), b"\x1b[?1049h\x1b[2J\x1b[H")
        self._enable_host_mouse()

    def _enable_host_mouse(self) -> None:
        # Enable SGR mouse reporting on the host (1000 = button press/release +
        # wheel) so aGiT receives wheel events for scrollback and press/release
        # for its own copy. This is the minimal mode that reliably reports the
        # wheel; richer motion tracking (1002/1003) changes wheel reporting on
        # some terminals and is avoided. Backends that manage the mouse
        # themselves (OpenCode) re-assert their own modes afterwards.
        os.write(sys.stdout.fileno(), b"\x1b[?1000h\x1b[?1006h")

    def _run_command(self, command: str) -> None:
        # aGiT commands in proxy mode are triggered via Ctrl-G and are plain
        # names; ":" is not a command trigger here (it is forwarded to the
        # backend like any other input).
        name, _, arg = command.partition(" ")
        if name in {"exit", "quit"}:
            if not self._confirm_exit():
                self._render()
                return
            if not self._confirm_terminate_background_sessions():
                self._render()
                return
            self._finalize_pending_work()
            self.running = False
            self._exit_child()
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
        elif name == "git-base-branch":
            self._switch_base_command(arg)
            return
        elif name == "":
            self._set_message("Select an aGiT command.")
        else:
            self._set_message(f"Unknown aGiT command: {name}")
        self._render()

    def _prompt_popup(self, title: str, prompt: str, *, default: str = "") -> str | None:
        value = default
        escape_buffer: bytearray | None = None
        while True:
            self._set_message(f"{title}\n{prompt}\n> {value}", seconds=60)
            self._render()
            data = os.read(sys.stdin.fileno(), 32)
            if data == b"\x1b":
                self._clear_message()
                self._render()
                return None
            for byte in data:
                char = bytes([byte])
                if escape_buffer is not None:
                    escape_buffer.extend(char)
                    if _escape_sequence_complete(bytes(escape_buffer)):
                        escape_buffer = None
                    continue
                if char == b"\x03":
                    self._exit_child()
                    return None
                if char == b"\x1b":
                    escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    self._clear_message()
                    self._render()
                    return value
                if char in {b"\x7f", b"\b"}:
                    value = value[:-1]
                elif byte >= 32:
                    value += char.decode(errors="ignore")

    def _select_popup(self, title: str, options: list[str]) -> str | None:
        if not options:
            return ""
        selected = 0
        escape_buffer: bytearray | None = None
        while True:
            lines = [title, "Up/Down selects. Enter confirms.", ""]
            for index, option in enumerate(options):
                prefix = "> " if index == selected else "  "
                lines.append(prefix + option)
            self._set_message("\n".join(lines), seconds=60)
            self._render()
            data = os.read(sys.stdin.fileno(), 32)
            if data == b"\x1b":
                self._clear_message()
                self._render()
                return None
            for byte in data:
                char = bytes([byte])
                if escape_buffer is not None:
                    escape_buffer.extend(char)
                    sequence = bytes(escape_buffer)
                    if sequence == b"\x1b[A":
                        selected = (selected - 1) % len(options)
                        escape_buffer = None
                    elif sequence == b"\x1b[B":
                        selected = (selected + 1) % len(options)
                        escape_buffer = None
                    elif _escape_sequence_complete(sequence):
                        escape_buffer = None
                    continue
                if char == b"\x03":
                    self._exit_child()
                    return None
                if char == b"\x1b":
                    escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    self._clear_message()
                    self._render()
                    return options[selected]

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
            message = self._prompt_popup("User Commit", prompt)
            if message is None:
                return False
            prompt = "Commit message is required. Enter a commit message:"
        repo.commit(build_user_commit_message(message=message, agit_session_id=state.session_id))
        state.clear_trace()
        return True

    def _review_untracked_popup(self, *, include_declined: bool, repo: GitRepo | None = None, state: AgitState | None = None) -> str:
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
            selected = [ordered[int(token) - 1] for token in answer.replace(",", " ").split()
                        if token.isdigit() and 1 <= int(token) <= len(ordered)]
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

    def _create_agent_commit_from_turns_popup(self, *, turns, backend: str, backend_session_id: str | None, model: str | None, quiet: bool, prompt_untracked: bool = True) -> bool:
        if not turns:
            return False
        pending_users = [item.get("content") for item in self.state.pending_trace() if item.get("role") == "user" and item.get("content")]
        remaining_pending_users = list(pending_users)
        self.state.data["pending_trace"] = []
        self.state.save()
        subject_prompts: list[str] = []
        for turn in turns:
            if turn.user_prompt:
                subject_prompts.append(turn.user_prompt)
                self.state.append_trace("user", turn.user_prompt)
                if turn.user_prompt in remaining_pending_users:
                    remaining_pending_users.remove(turn.user_prompt)
            if turn.final_response:
                self.state.append_trace("agent", turn.final_response)
            self.state.add_token_usage(turn.tokens)

        for pending_user in remaining_pending_users:
            subject_prompts.append(pending_user)
            self.state.append_trace("user", pending_user)

        self._ensure_turn_branch()
        self.repo.add_tracked()
        if prompt_untracked:
            self._review_untracked_popup(include_declined=False)
        else:
            # Non-interactive commit (worktree session or exit finalize): stage
            # the agent's new files too, except any the user intentionally declined.
            declined = set(self.state.declined_untracked())
            self.repo.stage_paths([path for path in self.repo.untracked_files() if path not in declined])
        if not self.repo.has_staged_changes():
            return False

        # The subject lists every user prompt that led to this commit, joined by
        # " / " (a commit can cover several turns — e.g. a prompt queued mid-turn).
        # subject_prompts holds only this commit's prompts, not the whole session,
        # so it never accumulates stale earlier prompts; the builder truncates the
        # joined text to GitHub's subject width and keeps the full text below it.
        subject_text = " / ".join(subject_prompts) if subject_prompts else f"{backend} changes"
        self._last_agent_commit_id = self.repo.commit(
            build_agent_commit_message(
                latest_prompt=subject_text,
                trace=self.state.pending_trace(),
                backend=backend,
                backend_session_id=backend_session_id,
                agit_session_id=self.state.session_id,
                model=model or self.state.model,
                token_usage=self.state.pending_token_usage(),
                trace_turn_limit=self.state.trace_turn_limit,
                session_name=getattr(self, "name", None),
            )
        )
        self.state.clear_trace()
        if not quiet:
            self._set_message(self._agent_commit_message(), sticky=True)
        return True

    def _agent_commit_message(self) -> str:
        # The auto-commit confirmation, including the short SHA of the commit aGiT
        # just made so the user can find it (e.g. `git show <id>`).
        commit_id = getattr(self, "_last_agent_commit_id", None)
        return f"Created <agent> commit {commit_id}." if commit_id else "Created <agent> commit."

    def _set_message(self, message: str, *, seconds: float = 4.0, sticky: bool = False) -> None:
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
        if getattr(self, "_message_sticky", False):
            self._clear_message()
            return True
        return False

    def _confirm_exit(self) -> bool:
        choice = self._select_popup("Exit aGiT?", ["No, keep working", "Yes, exit"])
        return choice == "Yes, exit"

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
            self._finish_agent_parse_if_ready(quiet=True, prompt_untracked=False, integrate=False, require_complete=False)
            if self._start_agent_parse() and self.agent_parse_thread:
                self.agent_parse_thread.join(timeout=20)
            self._finish_agent_parse_if_ready(quiet=True, prompt_untracked=False, integrate=False, require_complete=False)
        except Exception as error:  # never block on a commit failure
            self._debug(f"sync commit failed: {error!r}")

    def _finalize_pending_work(self) -> None:
        # On a confirmed exit, make sure the latest completed agent turn is
        # committed for *every* session before aGiT leaves — otherwise quitting
        # right after a turn drops a commit the idle/stable debounce had not yet
        # made. (Background sessions are committed via the context swap.)
        if getattr(self, "_finalized_on_exit", False):
            return  # already finalized (e.g. the backend exited and we ran this)
        self._finalized_on_exit = True
        self._exiting = True
        self._set_message("Finalizing commits before exit...", seconds=30)
        self._render()
        self._commit_latest_turn_sync()  # active session, in place
        self._integrate_session_on_exit()
        self._remove_worktree_on_exit()
        if len(self.sessions) > 1:
            active_snapshot = capture_session(self)
            for index, session in enumerate(self.sessions):
                if index == self.active_index:
                    continue
                restore_session(self, session)
                try:
                    self._commit_latest_turn_sync()
                    self._integrate_session_on_exit()
                    self._remove_worktree_on_exit()
                finally:
                    for field in SESSION_FIELDS:
                        setattr(session, field, getattr(self, field))
            restore_session(self, active_snapshot)
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

    def _terminate_child(self) -> None:
        # Stop the current session's backend process and release its PTY, so its
        # worktree is no longer in use and can be removed.
        if getattr(self, "child_pid", None):
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            self.child_pid = None
        if getattr(self, "master_fd", None) is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    def _remove_worktree_on_exit(self) -> None:
        # On exit, drop a fully-merged session's worktree so directories do not
        # pile up across runs. A session whose work could not be integrated
        # (conflict, uncommitted changes, or commits still ahead of the base)
        # keeps its worktree so the next startup can surface it. The backend
        # conversation persists (keyed by the worktree path) and is recreated if
        # the session is resumed, so nothing of value is lost.
        info = getattr(self, "worktree", None)
        if info is None or getattr(self, "_base_branch", None) is None:
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
        if info.name == getattr(self, "_primary_worktree_name", None):
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
                    self._debug(f"keeping worktree '{info.name}' on exit: '{branch}' still ahead of {self._base_branch}")
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
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def _handle_exit_signal(self, signum, _frame) -> None:
        self.running = False
        self._disable_host_terminal_modes()
        self._cleanup_child()
        self._restore_terminal()
        raise SystemExit(128 + int(signum))

    def _cleanup_child(self) -> None:
        if not self.child_pid:
            return
        try:
            done, _status = os.waitpid(self.child_pid, os.WNOHANG)
            if done:
                return
            os.kill(self.child_pid, signal.SIGINT)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                done, _status = os.waitpid(self.child_pid, os.WNOHANG)
                if done:
                    return
                time.sleep(0.05)
            os.kill(self.child_pid, signal.SIGTERM)
        except ChildProcessError:
            return
        except ProcessLookupError:
            return

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
        # Trace every submitted prompt as a user message. The agent-active path
        # above already recorded; the held path records in _forward_pending_prompt.
        # This covers the remaining (clean / user-changes-committed) submits so no
        # follow-up is dropped from the commit trace. Dedup in the agent-commit
        # builder collapses this with the backend's own turn.user_prompt.
        self._record_user_prompt(prompt_text)
        return True

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
        os.write(self.master_fd, b"".join(forwarded))

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
        self._user_declined = [path for path in getattr(self, "_user_declined", []) if path in untracked]

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
        if prompt_text:
            self.state.append_trace("user", prompt_text)

    def _await_followup(self, prompt_text: str) -> None:
        # Remember a prompt the user queued while the agent was busy so the next
        # commit waits for it to land as a turn (see _finish_agent_parse_if_ready),
        # keeping the queued prompt in the same commit as the turn it follows.
        norm = " ".join((prompt_text or "").split())
        if norm:
            self._awaited_followups = getattr(self, "_awaited_followups", [])
            self._awaited_followups.append(norm)

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
        parse_lock = getattr(self, "agent_parse_lock", None)
        if parse_lock is None:
            parse_lock = threading.Lock()
            self.agent_parse_lock = parse_lock
        with parse_lock:
            if getattr(self, "agent_parse_active", False):
                return False
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                return False
            if self.agent_parse_result is not None:
                return False
            self.agent_parse_active = True

        last_message_id = self.state.last_backend_message_id

        def worker() -> None:
            try:
                self._debug("agent parse worker started")
                if getattr(self, "worktree", None) is not None:
                    # A worktree's working directory is unique to this aGiT
                    # session, so the newest backend session there is always this
                    # session's current conversation — including one the user
                    # started from inside the backend itself. Track it so its work
                    # is still committed (to this session's branch).
                    session_id = self.backend.latest_session_id(self.repo.repo) or self.state.backend_session_id
                else:
                    # No worktree isolation (fallback / observer): stay pinned to
                    # the owned session to avoid drifting to an unrelated one.
                    session_id = self.state.backend_session_id or self._discover_spawned_session()
                session = self.backend.export_session(self.repo.repo, session_id) if session_id else None
                turn_count = len(session.turns) if session else 0
                final_count = len([turn for turn in session.turns if turn.final_response]) if session else 0
                self._debug(f"agent parse worker finished session_id={session_id} turns={turn_count} finals={final_count}")
                self.agent_parse_result = (session_id, session, last_message_id)
            finally:
                self.last_parse_finish = time.monotonic()
                with self.agent_parse_lock:
                    self.agent_parse_active = False

        self.last_parse_start = time.monotonic()
        self._debug(f"agent parse started last_message_id={last_message_id}")
        self.agent_parse_thread = threading.Thread(target=worker, name="agit-session-parse", daemon=True)
        self.agent_parse_thread.start()
        return True

    def _finish_agent_parse_if_ready(self, *, quiet: bool, prompt_untracked: bool | None = None, integrate: bool = True, require_complete: bool = True) -> bool | None:
        if prompt_untracked is None:
            # Worktree sessions are isolated sandboxes, so agent commits there
            # auto-stage everything; only the main working tree prompts.
            prompt_untracked = getattr(self, "worktree", None) is None
        if self.agent_parse_thread and self.agent_parse_thread.is_alive():
            return None
        if self.agent_parse_result is None:
            return None
        session_id, session, last_message_id = self.agent_parse_result
        self.agent_parse_result = None
        if not session:
            self._debug(f"agent parse consumed without session session_id={session_id}")
            return False
        new_session_id = session.session_id or session_id
        self._note_backend_session_change(new_session_id)
        self.state.backend_session_id = new_session_id
        # Surface this worktree conversation in the repo root too, so it can be
        # continued with a plain `claude` run there (the transcript exists now).
        self._mirror_session_to_base(new_session_id)
        if session.model:
            self.state.model = session.model
        turns = turns_after(session, last_message_id)
        # A prompt the user queued WHILE the agent was working belongs in the same
        # commit as the turn it follows — otherwise the brief idle gap after the
        # first turn lets the debounce commit it, and the queued prompt then lands
        # as a second commit. Hold off until every queued prompt has shown up as a
        # turn in the transcript. Only while the agent is still active, though: a
        # queued prompt the user cancelled never lands and must not block forever.
        awaited = getattr(self, "_awaited_followups", [])
        if awaited:
            seen = {" ".join((turn.user_prompt or "").split()) for turn in session.turns}
            awaited = [prompt for prompt in awaited if prompt not in seen]
            self._awaited_followups = awaited
            if require_complete and awaited and self._agent_is_active():
                self._debug(f"deferring agent commit: {len(awaited)} queued follow-up(s) not yet in transcript")
                return None
            self._awaited_followups = []  # committing now: drop any cancelled queue entries
        # Don't commit while the latest prompt is still being answered (the
        # backend's last message was a tool call, not a final response). The
        # idle/file-stable debounce can otherwise fire during a mid-turn pause and
        # carve one prompt into several commits — code first, tests next. Wait for
        # the turn to finish; forced flushes (exit) pass require_complete=False so
        # work-in-progress is never lost when the worktree is torn down.
        if require_complete and turns and not turns[-1].complete:
            self._debug(f"deferring agent commit: latest turn still in progress session_id={new_session_id}")
            return None
        complete_turns = [turn for turn in turns if turn.final_response]
        if not complete_turns:
            self._debug(f"agent parse consumed without final response session_id={self.state.backend_session_id} turns={len(turns)}")
            return False
        committed = self._create_agent_commit_from_turns_popup(
            turns=turns,
            backend=self.backend.name,
            backend_session_id=self.state.backend_session_id,
            model=session.model or self.state.model,
            quiet=quiet,
            prompt_untracked=prompt_untracked,
        )
        if committed:
            self.agent_in_flight = False
            self.state.last_backend_message_id = complete_turns[-1].assistant_message_id
            self.last_status = ""
            self._debug(f"agent commit created session_id={self.state.backend_session_id} assistant_id={self.state.last_backend_message_id}")
            if not quiet:
                # Paint the "Created <agent> commit." confirmation NOW, before the
                # integrate below runs git merge/fast-forward on the main thread.
                # Otherwise the popup wouldn't appear until that work returned and
                # the loop got back to a render — making it look like nothing
                # happened. (quiet covers background/exit commits, which set no
                # message and may have another session swapped in.)
                self._render()
            if integrate:
                # Interactive integration (may prompt / inject an agent merge) only
                # for the active session. The sync/background commit path passes
                # integrate=False and integrates non-interactively itself, so an
                # agent merge is never injected into a session that is merely
                # swapped in — which would mis-target another backend's PTY.
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
            return
        if self.agent_parse_thread and self.agent_parse_thread.is_alive():
            if self.verbose:
                self._render_status(f"git changes found; parsing {self.backend.name} session")
            return
        if now - self.last_status_change < self.FILE_STABLE_SECONDS or now - self.last_child_output < self.CHILD_IDLE_SECONDS:
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
            if now - getattr(self, "last_parse_finish", 0.0) < self.PARSE_COOLDOWN_SECONDS:
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

    def _pause_child_ui(self) -> None:
        self._set_cooked()
        os.write(sys.stdout.fileno(), b"\x1b[0m\r\n")

    def _resume_child_ui(self) -> None:
        self._set_raw()
        self._render()

    def _set_raw(self) -> None:
        tty.setraw(sys.stdin.fileno())

    def _set_cooked(self) -> None:
        if self.old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_attrs)

    def _restore_terminal(self) -> None:
        # Disable mouse reporting *before* handing the terminal back, then drop any
        # mouse reports the terminal already queued — otherwise those buffered SGR
        # sequences (e.g. "\x1b[<35;..M") leak to the shell as stray hex after exit.
        self._disable_host_terminal_modes()
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass
        self._set_cooked()
        os.write(sys.stdout.fileno(), b"\x1b[?1049l\x1b[0m\r\n")

    def _disable_host_terminal_modes(self) -> None:
        # Reset modes commonly enabled by full-screen TUIs: mouse tracking,
        # focus reporting, bracketed paste, alternate-scroll, cursor visibility,
        # and styling. Emit this independently from cooked-mode restoration so it
        # can also run from signal handlers before Python exits.
        os.write(
            sys.stdout.fileno(),
            b"\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1004l"
            b"\x1b[?1005l\x1b[?1006l\x1b[?1007l\x1b[?1015l\x1b[?1016l\x1b[?2004l"
            b"\x1b[?25h\x1b[0m",
        )

    def _resize_child(self) -> None:
        if self.master_fd is None:
            return
        try:
            import fcntl
            import struct

            self.rows, self.cols = self._terminal_size()
            if self.screen is not None:
                self.screen.resize(max(self.rows - 1, 1), self.cols)
            self.scroll_back = 0  # history geometry changed; return to live view
            winsize = struct.pack("HHHH", max(self.rows - 1, 1), self.cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            self._render()
        except OSError:
            pass

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
            return size.lines, size.columns
        except OSError:
            return 24, 80
