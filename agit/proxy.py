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
from agit.commit_message import build_agent_commit_message, build_user_commit_message
from agit.git import GitRepo
from agit.global_config import GlobalConfig
from agit.lock import RepoLock
from agit.session import turns_after
from agit.state import AgitState


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
    COMMANDS = ["status", "stage", "unstaged", "user-commit", "session", "agent-backend", "exit"]

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
    FILE_STABLE_SECONDS = 8.0
    CHILD_IDLE_SECONDS = 4.0
    POLL_SECONDS = 2.0
    PARSE_COOLDOWN_SECONDS = 10.0
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps

    def __init__(self, repo: GitRepo, *, verbose: bool = False, backend: str | None = None) -> None:
        self.repo = repo
        self.global_config = GlobalConfig()
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
        # working tree. A second instance runs read-only (tracking disabled).
        self.management_lock = RepoLock(repo.repo / ".agit" / "lock")
        self.tracking_enabled = True
        self.debug_proxy = verbose or os.environ.get("AGIT_DEBUG_PROXY", "").strip().lower() in {"1", "true", "yes"}

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Proxy mode requires an interactive terminal. Use --mode json for non-TTY use.")
        if not self._ensure_backend_available():
            return 1
        self.tracking_enabled = self.management_lock.acquire()
        self.state.save()
        if self.tracking_enabled and self.actions.has_pre_agent_user_changes():
            print("User changes detected before the agent starts.")
            self.actions.create_user_commit()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
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
            if not self.tracking_enabled:
                self._set_message(
                    "Another aGiT process is already running on this repo —\n"
                    "this instance is read-only (no auto-commits).",
                    seconds=6.0,
                )
                self._render()
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
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.repo.repo)
            os.execvp(command[0], command)
        self.child_pid = pid
        self.master_fd = fd

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

    def _switch_backend(self, name: str) -> None:
        if not backend_installed(name):
            self._set_message(f"'{name}' is not installed.\n{install_hint(name)}", seconds=8.0)
            self._render()
            return
        # Remember the current backend's session, then bring up the newly
        # selected backend (restoring its own session for this repo if known).
        self.state.remember_backend_session()
        self.global_config.default_backend = name
        self.state.backend = name
        self.backend = make_proxy_agent(name)
        self.state.backend_session_id = self.state.stored_backend_session(name)
        self.state.last_backend_message_id = None
        self.state.clear_trace()
        self._restart_agent(f"Backend set to {name}")

    def _handle_session_command(self, arg: str) -> None:
        # Reached via Ctrl-G then "session". With no argument it opens an
        # interactive menu; typed arguments are also accepted for convenience.
        arg = arg.strip()
        if arg in {"new", "fresh"}:
            self._start_new_session()
        elif arg in {"sync", "latest", "refresh"}:
            self._sync_tracked_session()
        elif arg:
            target = self._resolve_session_id(arg)
            if target is None:
                self._set_message(f"No session matching '{arg}' for {self.backend.name}.")
                self._render()
            else:
                self._switch_to_session(target)
        else:
            self._session_menu()

    def _session_menu(self) -> None:
        refs = sorted(self.backend.list_sessions(self.repo.repo), key=lambda ref: ref.updated, reverse=True)
        current = self.state.backend_session_id
        options = ["+ New session"]
        targets: list[str] = ["__new__"]
        if refs:
            options.append("> Sync to most recent")
            targets.append("__sync__")
        for ref in refs[:8]:
            marker = "* " if ref.id == current else "  "
            label = (ref.label or "").strip().replace("\n", " ")
            if len(label) > 36:
                label = label[:35] + "…"
            options.append(f"{marker}{_short_session(ref.id)}  {_humanize_age(ref.updated)}  {label}".rstrip())
            targets.append(ref.id)
        title = f"Sessions ({self.backend.name}) — tracking {_short_session(current) if current else '(new)'}"
        choice = self._select_popup(title, options)
        if choice is None:
            self._set_message("Cancelled.")
            self._render()
            return
        target = targets[options.index(choice)]
        if target == "__new__":
            self._start_new_session()
        elif target == "__sync__":
            self._sync_tracked_session()
        else:
            self._switch_to_session(target)

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

    def _loop(self) -> int:
        assert self.master_fd is not None
        while self.running:
            timeout = 0.016 if self._render_pending else 0.2
            readable, _, _ = select.select([sys.stdin.fileno(), self.master_fd], [], [], timeout)
            if self.master_fd in readable:
                output = self._drain_child_output()
                if output is None:
                    sample = self.last_child_output_sample[-2048:].decode(errors="replace").replace("\x1b", "\\x1b")
                    self._debug(f"master_fd closed (backend gone); last_output={sample!r}")
                    break
                if output:
                    self.last_child_output = time.monotonic()
                    self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
                    self._answer_terminal_queries(output)
                    self._sync_terminal_modes(output)
                    self._feed_child_output(output)
                    self._render_output()
            if sys.stdin.fileno() in readable:
                data = os.read(sys.stdin.fileno(), 4096)
                data = self._input_tail + data
                data, self._input_tail = self._hold_incomplete_tail(data)
                data = self._intercept_scroll(data)
                was_capturing = self.input.capturing
                forwarded, local_echo, command, should_exit = self.input.feed(data)
                if should_exit:
                    if not self._confirm_exit():
                        self._render()
                        continue
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
                    if submit:
                        prompt_text = self.passthrough_prompt.decode(errors="ignore").strip()
                        if not self._pre_agent_commit_if_needed(prompt_text):
                            self.pending_forwarded = [chunk for chunk in forwarded if chunk in {b"\r", b"\n"}]
                            self.pending_prompt_text = prompt_text
                            forwarded = [chunk for chunk in forwarded if chunk not in {b"\r", b"\n"}]
                            submit = False
                    if submit:
                        self.passthrough_prompt.clear()
                        self.passthrough_escape = None
                    if forwarded:
                        if submit:
                            self.agent_in_flight = True
                        os.write(self.master_fd, b"".join(forwarded))
                if command:
                    self._run_command(command)
            self._flush_pending_render()
            self._resume_pending_prompt_if_ready()
            self._maybe_agent_commit()
            if self.child_pid is not None:
                done, status = os.waitpid(self.child_pid, os.WNOHANG)
                if done:
                    exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
                    sample = self.last_child_output_sample[-512:].decode(errors="replace").replace("\x1b", "\\x1b")
                    self._debug(f"child exited pid={self.child_pid} status={status} exit_code={exit_code} last_output={sample!r}")
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

    def _debug(self, message: str) -> None:
        if not getattr(self, "debug_proxy", False):
            return
        try:
            path = self.repo.repo / ".agit" / "proxy-debug.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
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

    def _initialize_session_baseline(self) -> None:
        if not self._should_continue_session():
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            return
        session = self.backend.export_session(self.repo.repo, self.state.backend_session_id)
        if not session:
            self.state.last_backend_message_id = None
            return
        if session.model:
            self.state.model = session.model
        complete = [turn for turn in session.turns if turn.assistant_message_id]
        self.state.last_backend_message_id = complete[-1].assistant_message_id if complete else None
        self.state.clear_trace()

    def _init_screen(self) -> None:
        self.rows, self.cols = self._terminal_size()
        # HistoryScreen keeps lines that scroll off the top so aGiT can offer
        # scrollback for backends that stream to the normal screen (Claude).
        self.screen = pyte.HistoryScreen(self.cols, max(self.rows - 1, 1), history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.scroll_back = 0

    def _feed_child_output(self, output: bytes) -> None:
        if self.stream is not None:
            try:
                self.stream.feed(output)
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

    def _render_output(self) -> None:
        # Coalesce repaints driven by a flood of backend output (e.g. fast
        # scrolling) to ~30fps so aGiT does not overwhelm the host terminal's
        # stdout, which would block the loop and back up the backend's PTY.
        now = time.monotonic()
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
        if now - self._last_render >= self.RENDER_MIN_INTERVAL:
            self._last_render = now
            self._render_pending = False
            self._render()

    def _render(self) -> None:
        if self.screen is None:
            return
        parts = ["\x1b[0m\x1b[?25l\x1b[H"]
        selection = self._selection_ranges()
        for index, cells in enumerate(self._visible_lines()):
            parts.append("\x1b[0m" + self._render_line(cells, selection.get(index)))
            parts.append("\r\n")
        parts.append(self._status_line())
        if self.input.capturing:
            self._append_command_palette(parts)
        elif self.message and time.monotonic() < self.message_until:
            self._append_message_popup(parts, self.message)
        if self.scroll_back > 0:
            # While scrolled into history, keep the cursor hidden (its live
            # position is not meaningful for the displayed lines).
            parts.append("\x1b[?25l")
        else:
            cursor = self.screen.cursor
            cursor_row = min(cursor.y + 1, max(self.rows - 1, 1))
            cursor_col = min(cursor.x + 1, self.cols)
            parts.append(f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h")
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
        declined = len([path for path in self.state.declined_untracked() if (self.repo.repo / path).exists()])
        if not getattr(self, "tracking_enabled", True):
            left = f" aGiT READ-ONLY (another aGiT is managing this repo) | {self.backend.name} passthrough "
        else:
            left = f" aGiT Ctrl-G commands | {self.backend.name} passthrough "
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
            self.running = False
            self._exit_child()
            return

        if name in {"stage", "user-commit", "session", "agent-backend"} and not getattr(self, "tracking_enabled", True):
            self._set_message("Read-only: another aGiT process is managing this repo.")
            self._render()
            return

        if name in {"stage", "user-commit"}:
            if name == "stage":
                self._set_message(self._review_untracked_popup(include_declined=True))
            else:
                created = self._create_user_commit_popup()
                self._set_message("Created user commit." if created else "No staged user changes to commit.")
            self._render()
            return

        if name == "status":
            self._set_message(self.repo.status_short() or "Working tree clean")
        elif name == "unstaged":
            self._prune_declined_untracked()
            declined = self.state.declined_untracked()
            if declined:
                self._set_message("Intentionally unstaged: " + ", ".join(declined))
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

    def _create_user_commit_popup(self) -> bool:
        self.repo.add_tracked()
        self._review_untracked_popup(include_declined=False)
        if not self.repo.has_staged_changes():
            return False
        message = ""
        prompt = "Commit message:"
        while not message.strip():
            message = self._prompt_popup("User Commit", prompt)
            if message is None:
                return False
            prompt = "Commit message is required. Enter a commit message:"
        self.repo.commit(build_user_commit_message(message=message, agit_session_id=self.state.session_id))
        self.state.clear_trace()
        return True

    def _review_untracked_popup(self, *, include_declined: bool) -> str:
        self._prune_declined_untracked()
        untracked = self.repo.untracked_files()
        declined = set(self.state.declined_untracked())
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
            self.repo.stage_paths(candidates)
            self.state.remove_declined(candidates)
            return f"Staged {len(candidates)} untracked file(s)."
        else:
            self.state.add_declined(candidates)
            return f"Left {len(candidates)} untracked file(s) unstaged."

    def _create_agent_commit_from_turns_popup(self, *, turns, backend: str, backend_session_id: str | None, model: str | None, quiet: bool) -> bool:
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

        self.repo.add_tracked()
        self._review_untracked_popup(include_declined=False)
        if not self.repo.has_staged_changes():
            return False

        latest_prompt = " / ".join(subject_prompts) or f"{backend} changes"
        self.repo.commit(
            build_agent_commit_message(
                latest_prompt=latest_prompt,
                trace=self.state.pending_trace(),
                backend=backend,
                backend_session_id=backend_session_id,
                agit_session_id=self.state.session_id,
                model=model or self.state.model,
                token_usage=self.state.pending_token_usage(),
                trace_turn_limit=self.state.trace_turn_limit,
            )
        )
        self.state.clear_trace()
        if not quiet:
            self._set_message("Created <agent> commit.")
        return True

    def _set_message(self, message: str, *, seconds: float = 4.0) -> None:
        self.message = message
        self.message_until = time.monotonic() + seconds

    def _clear_message(self) -> None:
        self.message = None
        self.message_until = 0.0

    def _confirm_exit(self) -> bool:
        # A read-only observer has nothing to lose, so it exits immediately.
        if not getattr(self, "tracking_enabled", True):
            return True
        choice = self._select_popup("Exit aGiT?", ["No, keep working", "Yes, exit"])
        return choice == "Yes, exit"

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
        if not getattr(self, "tracking_enabled", True):
            return True  # read-only: forward the prompt, never commit
        self._clear_agent_in_flight_if_idle()
        status = self.repo.status_short().strip()
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is True:
            self.pre_agent_reconciled_status = ""
        if status and self._agent_is_active():
            self._record_user_prompt(prompt_text)
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
        return True

    def _resume_pending_prompt_if_ready(self) -> None:
        if self.pending_forwarded is None:
            return
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is None:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self._set_message("aGiT is checking existing git changes before sending your prompt...", seconds=60)
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
        self.agent_in_flight = True
        self._clear_message()
        os.write(self.master_fd, b"".join(forwarded))

    def _prune_declined_untracked(self) -> None:
        self.state.keep_declined(self.repo.untracked_files())

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
                # Stay pinned to the session aGiT owns; only discover when the id
                # is not yet known (a freshly spawned backend-assigned session).
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

    def _finish_agent_parse_if_ready(self, *, quiet: bool) -> bool | None:
        if self.agent_parse_thread and self.agent_parse_thread.is_alive():
            return None
        if self.agent_parse_result is None:
            return None
        session_id, session, last_message_id = self.agent_parse_result
        self.agent_parse_result = None
        if not session:
            self._debug(f"agent parse consumed without session session_id={session_id}")
            return False
        self.state.backend_session_id = session.session_id or session_id
        if session.model:
            self.state.model = session.model
        turns = turns_after(session, last_message_id)
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
        )
        if committed:
            self.agent_in_flight = False
            self.state.last_backend_message_id = complete_turns[-1].assistant_message_id
            self.last_status = ""
            self._debug(f"agent commit created session_id={self.state.backend_session_id} assistant_id={self.state.last_backend_message_id}")
        return committed

    def _commit_available_agent_turns(self, *, quiet: bool) -> bool:
        if self._finish_agent_parse_if_ready(quiet=quiet) is True:
            return True
        self._start_agent_parse()
        return False

    def _maybe_agent_commit(self) -> None:
        if not getattr(self, "tracking_enabled", True):
            return  # read-only observer: never auto-commit
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
            self._render_status("Created <agent> commit.")
        else:
            self._set_message("Created <agent> commit.")

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
        self._set_cooked()
        self._disable_host_terminal_modes()
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
