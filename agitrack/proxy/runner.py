from __future__ import annotations

import base64
import filecmp
import os
from agitrack.env import getenv_compat
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType
from typing import Any, Callable, cast
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

from agitrack.commits import AgitrackActions, UserCommitAborted
from agitrack.console import stdin_is_interactive, stdout_is_interactive
from agitrack.backends.setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agitrack.backends.proxy_agents import available_backends, backend_phrase, make_proxy_agent
from agitrack.commits import (
    apply_summary_to_message,
    build_manual_squash_trailer,
    is_fully_tracked_message,
    build_user_commit_message,
    summary_metadata_lines,
)
from agitrack.events import EventLog, exclude_log_file, resolve_log_path
from agitrack.git import GitRepo
from agitrack.git import read_cache as git_read_cache
from agitrack.git import hooks as git_hooks
from agitrack.config import GlobalConfig
from agitrack.git import RepoLock, already_running_message
from agitrack.proxy import host_prompt, sandbox
from agitrack.config import AgitrackState
from agitrack.git import WorktreeInfo, WorktreeManager, _sanitize_name, is_managed_branch
from agitrack.proxy.commit_engine import CommitEngine, turn_is_finished
from agitrack.proxy.integration import IntegrationService, MergeContext, MergePhase
from agitrack.proxy.platform import make_child_process, make_host_terminal, make_waker
from agitrack.proxy.process import BackendProcess
from agitrack.proxy.session import Session
from agitrack.proxy.textutil import strip_ansi

# ProxyRunner's session-sharing half, split out by concern (see sharing.py's header for why
# it is a mixin). The module-level helpers are re-exported because they were part of this
# module's surface before the split and are imported from here by tests.
from agitrack.proxy.sharing import (  # noqa: F401  (re-exported for import compatibility)
    SessionSharingMixin,
    _push_rejection_reason,
    _redact_and_cap,
    _shared_transcript_rows,
)
from agitrack.proxy.branch_watch import BranchWatchMixin
from agitrack.proxy.manual_commits import ManualCommitsMixin
from agitrack.proxy.updates import UpdatesMixin
from agitrack.transcripts import SessionRef


# Palette helpers, _BackgroundColorEraseScreen, and detect_color_mode live in
# renderer.py (P1). Re-import them here so call sites in this file are unchanged
# and the agitrack.proxy shim can export them under their original names.
from agitrack.proxy.renderer import (
    detect_color_mode,
    forced_canvas_osc_values,
    _BackgroundColorEraseScreen,
    ScreenRenderer,
)

# TerminalHost lives in terminal.py (P1).
from agitrack.proxy.terminal import TerminalHost
from agitrack.update import Updater, UpdateStatus, restart_agitrack

# Modal state-machines (P6 Stage 2): PromptModal and SelectModal encode the
# byte-handling logic for free-text and selection popups.
from agitrack.proxy.modal import PASTE_END, PASTE_START, PromptModal, SelectModal, _escape_sequence_complete

_SGR_MOUSE_RE = re.compile(rb"\x1b\[<\d+;\d+;\d+[Mm]")
_SGR_MOUSE_EVENT_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
# Legacy X10/normal mouse reports: ESC [ M then exactly three raw bytes (button, column, row —
# each the value offset by 32). Terminals that don't honour SGR mouse mode (?1006) — some tmux
# configs and the native Windows console — send THESE instead of the SGR form even though
# aGiTrack requested SGR. Their raw coordinate bytes are ordinary characters (column/row 3 is
# '#'), so if they go unrecognised they leak into the backend's input as stray text — the
# "mouse cursor hash". Matched and stripped alongside the SGR form. DOTALL: a coordinate byte
# can be any value, including ones the default '.' would skip.
_X10_MOUSE_RE = re.compile(rb"\x1b\[M.{3}", re.DOTALL)
# A trailing, not-yet-complete X10 report (ESC [ M with fewer than three coordinate bytes),
# held back like the SGR/CSI tail below so its bytes aren't forwarded split across reads.
_INCOMPLETE_X10_RE = re.compile(rb"\x1b\[M.{0,2}$", re.DOTALL)
# A mouse report that was CUT OFF in flight: its body is followed by the start of another
# escape sequence instead of its own terminator, which proves the remainder is never coming.
#
# This is not hypothetical. A terminal's input queue is small (measured: ~590-1022 bytes on
# macOS) and the tty DISCARDS whatever does not fit, so scrolling faster than aGiTrack drains
# stdin — which is what "scrolling extensively" is — chops reports in half and drops the tail.
# The orphaned prefix then reaches the backend as a REAL Escape keypress followed by visible
# junk ("[<64;120;45"). Escape is destructive in both backends: it interrupts the turn, and on
# Claude's native session picker it quits Claude, which after three exits inside 12 s makes
# aGiTrack stop relaunching and exit normally — "aGiTrack dies when scrolling extensively",
# with no crash report because nothing ever raised.
#
# The lookahead is what makes this safe: neither report form can contain an ESC (SGR bodies are
# digits and ';', X10 coordinates are the value plus 32, so always >= 32). An ESC there is
# therefore always debris, never input the user meant. Complete reports are stripped first, so
# these patterns only ever see leftovers.
#
# Two shapes, kept separate so each stays provably safe:
#   * a CSI that got as far as ``ESC [`` (optionally into a mouse body) and is followed by ANY
#     ESC. ``ESC [`` alone is never a complete keypress, so this can only be wreckage. A real
#     arrow key (``ESC [ A``) or PageUp (``ESC [ 5 ~``) does not match: the optional body needs
#     ``<`` or ``M``, so the lookahead lands on ``A``/``5``, not an ESC.
#   * a report cut down to its bare ESC. This one is only safe to remove BETWEEN two reports —
#     the queue drained partway, the terminal appended more, and the join left an orphan in the
#     middle of a burst. Matching a lone ESC on the lookahead ALONE would eat the Escape KEY
#     whenever a wheel report landed in the same read, and Escape is how the user interrupts a
#     turn. So the preceding complete report is matched too and put back (Python has no
#     variable-width lookbehind); an ESC anywhere else is left for the user.
#     An orphan at the very START of a chunk has no report in front of it to key off — that one
#     arrives via ``_input_tail`` and is settled by ``_input_tail_debris``, which knows what the
#     ESC followed in the chunk it actually came from.
_ABORTED_CSI_PREFIX_RE = re.compile(rb"\x1b\[(?:<[0-9;]*|M[^\x1b]{0,2})?(?=\x1b)")
# The mirror image: a report prefix whose remainder never came, followed by the user TYPING
# inside the hold window, so the two are joined and forwarded together ("\x1b[<65;80hello").
# Only the ``<`` form can be decided, and that is what makes this safe: ``ESC [ <`` is a mouse
# report and nothing else in the host→app direction, and a report's body is digits and ``;``
# until its ``M``/``m``. Any OTHER byte there proves the report died — the same reasoning
# `_escape_sequence_complete` already uses. A complete report can never match: the lookahead
# demands a non-body byte exactly where its own terminator sits, and no backtracking finds one.
# Deliberately NOT extended to the bare ``ESC [`` arm — ``\x1b[A`` is an arrow key, and a
# generic numeric CSI followed by a letter (``\x1b[200h``) is a legitimate sequence shape, so
# a prefix glued to text is genuinely undecidable there.
# Matched, then VALIDATED: anything introduced by ``ESC [ <`` is kept only if it is a whole,
# well-formed report. Pattern-matching alone is not enough — a joined-up fragment can end in a
# perfectly good ``M`` and still be junk (``\x1b[<65;43;25;43;28M`` has FOUR parameters), which
# a "body followed by a non-body byte" rule cannot see. Arity is the discriminator, so check it.
_SGR_CANDIDATE_RE = re.compile(rb"\x1b\[<[0-9;]*[Mm]?")
# The other half of the same wreck, and the shape that keeps coming back: a report's TAIL with
# its ``ESC [ <`` gone (``5;43;28M``, ``;28M``, ``8M``). The tty discards from the middle of the
# stream, not only at read boundaries, so ONE read can carry a dozen of these interleaved with
# whole reports — which is why holding a partial and resyncing one orphan cannot cover it.
_SGR_ORPHAN_TAIL_RE = re.compile(rb"<?[0-9;]*[Mm]")
_ABORTED_MOUSE_ESC_RE = re.compile(rb"(\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[M.{3})\x1b(?=\x1b\[[<M])", re.DOTALL)
# A chunk that ENDS with a whole mouse report. When the next byte after such a run is a lone
# ESC that never continues, the tty dropped the rest of one more report: a person reaching for
# Escape does not land it glued to the end of half a kilobyte of wheel reports with nothing
# after it. Used only to decide what a held lone ESC was, once its hold has expired.
_MOUSE_REPORT_TAIL_RE = re.compile(rb"(?:\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[M.{3})$", re.DOTALL)
# --- resynchronising after a mouse report we gave up on -------------------------------------
# Giving up on a held partial (its remainder did not arrive within the hold) leaves the OTHER
# half of that report still in the terminal's queue. It arrives as its own read, and it has no
# ESC in it at all — so neither the tail-holding nor the debris-stripping above can see it, and
# it reached the backend as literal text ("<65;80;26M" typed into the prompt box).
#
# So remember the prefix we dropped, and on the NEXT read consume the bytes that complete it.
# The check is exact rather than a guess: the concatenation must form a whole, well-formed SGR
# report. That is what stops it eating real input — a user typing "m" is only swallowed if the
# prefix we dropped was already "\x1b[<65;80;26", which is precisely the case where that "m" IS
# the report's terminator. One read only; anything else disarms it.
#
# The bracketed-paste markers get the same treatment, and just as exactly: they are fixed
# strings, so the missing half is a literal suffix. The X10 form (``ESC [ M`` + three RAW
# coordinate bytes) deliberately does NOT resync — its remainder is 1-3 bytes of any value
# from 0x20 up, indistinguishable from the user typing, so consuming them would risk eating
# real keystrokes to save a couple of stray characters. X10 only appears on terminals that
# ignore the SGR request, and unlike the SGR case its leak carries no ESC, so it cannot
# interrupt a turn or quit a picker — leaking is the safer side to err on there.
_SGR_PREFIX_RE = re.compile(rb"^\x1b\[?[<0-9;]*$")  # every strict prefix of a report or marker
_SGR_REMAINDER_RE = re.compile(rb"[\[<0-9;]*[Mm]")  # what the rest of one could look like
_SGR_REPORT_RE = re.compile(rb"\x1b\[<\d+;\d+;\d+[Mm]")  # what the two halves must add up to


# Terminal focus in/out reports (CSI I / CSI O), emitted on window focus changes
# when focus reporting is on. Like mouse reports they are not keystrokes.
_FOCUS_EVENT_RE = re.compile(rb"\x1b\[[IO]")
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
# them and aGiTrack syncs the rest from the raw output separately.
_PYTE_HOSTILE_CSI_RE = re.compile(rb"\x1b\[[<>=][0-9;:]*[ -/]*[@-~]")
# Keyboard-protocol negotiation the backend sends to its (virtual) terminal:
# kitty keyboard protocol push/set/pop/query (``CSI > flags u`` / ``CSI = ... u``
# / ``CSI < ... u`` / ``CSI ? u``) and xterm modifyOtherKeys (``CSI > 4 ; N m``).
# aGiTrack renders from a pyte model, so the HOST terminal never sees these unless
# they are mirrored — and without them the host keeps sending plain ``\r`` for
# Shift+Enter instead of the disambiguated encoding the backend's keybindings
# (e.g. Claude's newline-in-input) expect.
_KEYBOARD_PROTO_RE = re.compile(rb"\x1b\[(?:[><=][0-9;]*u|\?u|>4(?:;[0-9]+)?m)")
# The cheap presence check in front of `_sync_terminal_modes`: every private-mode set/reset
# (`ESC [ ? <n> h|l`) and every keyboard-protocol negotiation it looks for begins with `ESC [`
# and one of these four introducers, so a chunk without one cannot contain anything it wants.
_TERMINAL_MODE_MARKER_RE = re.compile(rb"\x1b\[[?><=]")
# Kitty keyboard protocol encoding for control keys: CSI <keycode> ; <modifiers> u
# For Ctrl-A through Ctrl-Z: keycode is 97-122 (lowercase a-z), modifier is 5 (Ctrl+base)
# We decode these back to plain control bytes (0x01-0x1a) so the menu key works
# even when the terminal is in kitty keyboard protocol mode.
_KITTY_CTRL_KEY_RE = re.compile(rb"\x1b\[(\d+);5u")
# Kitty keyboard protocol encoding for Escape key: CSI 27 u or CSI 27 ; 1 u
# We decode this back to plain \x1b so Esc handling works in kitty mode.
_KITTY_ESC_KEY_RE = re.compile(rb"\x1b\[27(?:;1)?u")
# xterm modifyOtherKeys (formatOtherKeys=0) encoding: CSI 27 ; <modifier> ; <code> ~
# This is the OTHER way a terminal can report modified keys (the kitty form above
# is CSI <code> ; <modifier> u). iTerm2 answers the backend's modifyOtherKeys
# negotiation with THIS ~-terminated form, so e.g. Ctrl-C arrives as \x1b[27;5;99~
# and Ctrl-G as \x1b[27;5;103~. Without decoding it, those are forwarded to the
# backend instead of opening aGiTrack's exit/menu. modifier 5 = Ctrl (1 + 4).
_MODIFY_OTHER_KEYS_RE = re.compile(rb"\x1b\[27;(\d+);(\d+)~")
# A bracketed paste (CSI 200~ ... CSI 201~, possibly split across reads, hence
# the `|$`): newlines inside it are pasted CONTENT, not prompt submissions. The same
# delimiters keep pasted text from answering aGiTrack's own popups — see
# ProxyRunner._track_bracketed_paste and the modal module's docstring.
_BRACKETED_PASTE_RE = re.compile(rb"\x1b\[200~.*?(?:\x1b\[201~|$)", re.S)

# OSC 52 (set/clear the terminal clipboard), BEL- or ST-terminated. The backend emits this to
# copy to the clipboard; because aGiTrack renders the screen via pyte (which consumes OSC and
# only the visible grid is re-emitted), it must be forwarded to the host terminal explicitly or
# a backend's copy is silently dropped — see ProxyRunner._forward_clipboard_osc.
_OSC52_RE = re.compile(rb"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")

# "No argument given", for parameters where None is itself a meaningful value (a transcript
# that genuinely has no mtime) and so cannot double as the default.
_UNSET: object = object()

# The host terminal's ANSWERS to the capability queries `detect_host_terminal` sends: the
# foreground/background/palette colour reports (OSC 10/11/4), the primary device attributes
# reply, and the kitty keyboard flags reply. One arriving on STDIN is a late answer, not a
# keypress — and since an OSC reply ends in BEL (0x07 = Ctrl-G, the menu key) it must never
# reach the key handler. Deliberately narrow: exactly the shapes aGiTrack asks for, none of
# which a keyboard can produce. See ProxyRunner._absorb_host_terminal_reply.
_HOST_TERMINAL_REPLY_RE = re.compile(
    rb"\x1b\](?:10|11|4);[^\x07\x1b]*(?:\x07|\x1b\\)"  # colour / palette reports
    rb"|\x1b\[\?[0-9;]*[cu]"  # primary device attributes; kitty keyboard flags
)

# The BEGINNING of one of those replies, with its terminator not yet read. A pty read is not
# framed, so a slow terminal's answer arrives SPLIT — `\x1b]11;rgb:ffff/ffff` in one read and
# `/ffff\x07` in the next. Neither half matches the pattern above, so both used to flow to the
# key handler: the tail's BEL is 0x07, i.e. Ctrl-G, so the palette opened by itself and the rest
# of the reply was typed at the agent — and a Ctrl-G the USER then pressed toggled it shut again,
# which is what "my menu key sometimes types into the chat" actually is. Held and re-joined
# instead. Deliberately as narrow as the pattern it completes: no keyboard emits `\x1b]10;` or
# `\x1b[?`, so a real keypress is never held.
_HOST_TERMINAL_REPLY_PREFIX_RE = re.compile(rb"(?:\x1b\](?:10|11|4);[^\x07\x1b]*|\x1b\[\?[0-9;]*)$")

# Give up on a partial reply past this many bytes: a terminal that answers is not going to take
# longer than this, and holding input forever on a truncated one would be worse than the bug.
_MAX_HELD_REPLY_BYTES = 256


# The claude CLI refuses to resume a session that is still held by a running
# background agent, printing this and exiting (e.g. a stale `bg` agent from an
# earlier run). Its own remedy is --fork-session, which aGiTrack applies
# automatically rather than crash-looping and exiting (#114).
_FORK_HINT_RE = re.compile(r"running as a background agent|--fork-session", re.IGNORECASE)

# Crash-loop guard for a repeatedly-exiting backend (_relaunch_backend_or_exit). The window
# must comfortably exceed the total backoff below plus spawn overhead, or the guard's own
# bookkeeping trips before the backoff it is meant to accommodate finishes playing out.
_RELAUNCH_WINDOW_SECONDS = 20.0
# Delay before the 1st/2nd/3rd relaunch attempt. No delay on the first retry (most exits are
# not auth-related — Esc from the session picker, a clean `/exit` — and should resume
# instantly); increasing delay after, so a backend whose apiKeyHelper is momentarily slow
# (1Password/biometric unlock) gets real wall-clock time to succeed before the guard gives up.
_RELAUNCH_BACKOFF_SECONDS: tuple[float, ...] = (0.0, 1.5, 4.0)


# Sentinel: the summarizer-model picker was dismissed (Esc), distinct from a real None choice
# ("same as the session model"). Lets _choose_summarizer_model report "no change" unambiguously.
_NO_MODEL_PICK = object()


def _decode_kitty_ctrl_keys(data: bytes) -> bytes:
    """Decode enhanced-keyboard-protocol control keys and Escape to plain bytes.

    When the backend negotiates an enhanced keyboard protocol, the host terminal
    starts reporting modified keys as escape sequences instead of plain bytes. Two
    encodings exist, and which one a terminal uses depends on the terminal:
    - kitty keyboard protocol:  Ctrl-A → \\x1b[97;5u,  Escape → \\x1b[27u
    - xterm modifyOtherKeys:    Ctrl-A → \\x1b[27;5;97~, Escape → \\x1b[27;1;27~
      (iTerm2 answers with this ~-terminated form).

    This function converts both back to plain bytes so the menu key matching and
    Esc handling work regardless of which form the host terminal uses.

    Only decodes pure-Ctrl keys (modifier 5 = Ctrl) and Escape; every other
    modified key is left untouched so it still reaches the backend encoded.
    """
    # First decode Escape key (both encodings).
    data = _KITTY_ESC_KEY_RE.sub(b"\x1b", data)

    def _ctrl_letter_byte(keycode: int) -> bytes | None:
        # keycode 97-122 = lowercase a-z → Ctrl-A=0x01 .. Ctrl-Z=0x1a.
        if 97 <= keycode <= 122:
            return bytes([keycode - 96])
        return None

    # kitty form: CSI <code> ; 5 u
    def replace_kitty_ctrl(match: re.Match) -> bytes:
        return _ctrl_letter_byte(int(match.group(1))) or match.group(0)

    data = _KITTY_CTRL_KEY_RE.sub(replace_kitty_ctrl, data)

    # modifyOtherKeys form: CSI 27 ; <modifier> ; <code> ~
    def replace_modify_other_keys(match: re.Match) -> bytes:
        modifier, keycode = int(match.group(1)), int(match.group(2))
        if keycode == 27 and modifier == 1:  # bare Escape
            return b"\x1b"
        if modifier == 5:  # pure Ctrl
            return _ctrl_letter_byte(keycode) or match.group(0)
        return match.group(0)

    return _MODIFY_OTHER_KEYS_RE.sub(replace_modify_other_keys, data)


def _short_session(session_id: str | None) -> str:
    if not session_id:
        return "(none)"
    return session_id[:8]


class RepoChangeHandler(FileSystemEventHandler):
    IGNORED_PARTS = {".agitrack", ".git", ".pytest_cache", ".venv", "__pycache__"}

    # Event types that mean "somebody READ a file", not "the worktree changed". watchdog's
    # inotify backend reports IN_OPEN and IN_CLOSE_NOWRITE as these; macOS FSEvents reports
    # only real modifications, which is why this was a Linux-only failure.
    #
    # Counting a read as a change is catastrophic here, not merely noisy: `_last_change_at` is
    # reset on every one, so `worktree_settled` (now - _last_change_at >= FILE_STABLE_SECONDS)
    # never becomes true, the commit gate never opens, and aGiTrack stops committing entirely
    # while looking perfectly healthy. Measured on a real repo with NOTHING being written:
    # 1536 opened + 1536 closed_no_write events in 25s — about 123 phantom "changes" a second,
    # from ordinary reads of files like .gitignore and AGENTS.md.
    #
    # `closed` (IN_CLOSE_WRITE) is deliberately NOT in this set: it follows an actual write.
    READ_ONLY_EVENT_TYPES = {"opened", "closed_no_write"}

    def __init__(self, repo_path, changed: threading.Event, wake: "threading.Event | None" = None) -> None:
        self.repo_path = repo_path
        self.changed = changed
        # The git worker sleeps on `wake`; setting it lets a real worktree write
        # wake the worker at once instead of waiting for its poll timeout.
        self.wake = wake

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Reads are not changes. Checked by event_type STRING rather than by class so this
        # works across watchdog versions (older ones simply never emit these types).
        if getattr(event, "event_type", "") in self.READ_ONLY_EVENT_TYPES:
            return
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
        if self.wake is not None:
            self.wake.set()


class _ModalRequest:
    """A dialog the git worker needs the main (reactor) thread to present.

    The worker can't touch stdin/screen, so it enqueues one of these on
    ``_modal_mailbox`` and blocks on ``done`` until the main thread has run the
    modal and stored the user's answer in ``result``.
    """

    __slots__ = ("modal", "done", "result")

    def __init__(self, modal) -> None:
        self.modal = modal
        self.done = threading.Event()
        self.result: "str | None" = None


class ProxyInput:
    # Order matters (shown in the palette). Only "sessions" starts with "s" so
    # that pressing s+Enter jumps straight to the session picker. Git-specific
    # commands are grouped under a "git-" prefix.
    COMMANDS = [
        "sessions",
        "agent-backend",
        "git-unstaged",
        "git-commit",
        "dashboard",
        "settings",
        "update",
        # Reads "exit aGiTrack" so it is never mistaken for "exit the menu" (Esc does that).
        # `_run_command` parses the command name from the first word, so the trailing word is
        # ignored and "exit"/"quit" still work when typed directly.
        "exit aGiTrack",
    ]

    def __init__(self, menu_key: bytes = b"\x07") -> None:
        self.capturing = False
        self.buffer = bytearray()
        self.selected_index = 0
        self.escape_buffer: bytearray | None = None
        # The control byte or sequence that opens the command menu (default Ctrl-G;
        # configurable via "menu_key" in ~/.agitrack/config.json). For shift-modified keys,
        # this is a multi-byte kitty keyboard protocol escape sequence.
        self.menu_key = menu_key
        # Buffer for accumulating partial matches of multi-byte menu key sequences.
        # Only used when menu_key is longer than 1 byte.
        self.menu_key_buffer = bytearray()
        # Bracketed-paste state. Pasted bytes are content, never keys: with the palette
        # open a pasted newline would RUN the highlighted command, and a pasted \x03 would
        # start the exit flow. Set by the CSI 200~/201~ markers in the stream.
        self.in_paste = False
        self._paste_marker: bytearray | None = None
        # Set when the user pressed Ctrl-Z: the runner suspends the whole aGiTrack session
        # to the shell rather than forwarding the byte (see ProxyRunner._suspend_to_shell).
        self.suspend_requested = False
        # Context-dependent commands shown ABOVE the fixed COMMANDS (the runner refreshes
        # this when the palette opens). Used to surface "merge" at the top when there are
        # unmerged worktrees the user should rescue.
        self.extra_commands: list[str] = []

    def _observe_paste_marker(self, char: bytes) -> bool:
        """Track CSI 200~/201~ across bytes; return True while *char* is part of a marker.

        Purely observational — the caller still routes the byte normally, so the markers
        reach the backend untouched. A partial match that turns out to be some other escape
        sequence simply resets, and that byte follows the ordinary path.
        """
        if self._paste_marker is None:
            if char != b"\x1b":
                return False
            self._paste_marker = bytearray(char)
            return False  # ESC may equally start a real key sequence: let it flow on
        candidate = bytes(self._paste_marker) + char
        if candidate in (PASTE_START, PASTE_END):
            self.in_paste = candidate == PASTE_START
            self._paste_marker = None
            # While capturing, the palette's own escape reader was accumulating these same
            # bytes; the final "~" never reaches it (this observer consumes it), so clear
            # the half-built sequence or it would swallow the next real keypress.
            self.escape_buffer = None
            return True
        if PASTE_START.startswith(candidate) or PASTE_END.startswith(candidate):
            self._paste_marker.extend(char)
            return self.in_paste  # mid-marker: only swallow when already inside a paste
        self._paste_marker = None
        return False

    def feed(self, data: bytes) -> tuple[list[bytes], bytes, str | None, bool]:
        forwarded: list[bytes] = []
        command = None
        should_exit = False
        for byte in data:
            char = bytes([byte])
            # Watch for the bracketed-paste delimiters wherever they appear, so `in_paste`
            # is already right for the bytes that follow them in this very chunk. This is
            # OBSERVATION only: the marker bytes keep flowing through the normal paths
            # below, so passthrough to the backend is byte-for-byte unchanged.
            marker = self._observe_paste_marker(char)
            if marker or self.in_paste:
                # Inside a paste every byte is content the user copied, never a key. With the
                # palette open, printable characters type into it (visible and editable) but a
                # newline does NOT run the highlighted command and \x03 does not start the exit
                # flow; with the palette closed this is ordinary passthrough to the agent, the
                # delimiters included, so the backend still sees the paste exactly as sent.
                if not self.capturing:
                    forwarded.append(char)
                elif marker:
                    pass  # the delimiters themselves are never typed into the palette
                elif byte >= 32:
                    self.buffer.extend(char)
                    self.selected_index = 0
                elif char in {b"\r", b"\n"} and self.buffer and not self.buffer.endswith(b" "):
                    self.buffer.extend(b" ")  # a line break in a one-line field reads as a space
                self.selected_index = 0 if not marker else self.selected_index
                continue
            if char == b"\x1c":
                # Ctrl-\\ (SIGQUIT). Never forwarded: on a cooked pty it would kill the
                # backend outright, losing the conversation, and it means nothing to a TUI.
                continue
            if char == b"\x1a" and self.capturing:
                continue  # Ctrl-Z is not text: never type a control byte into the palette
            if char == b"\x1a":
                # Ctrl-Z. Raw mode means the kernel does not turn this into SIGTSTP, so it
                # would otherwise be forwarded to the backend as a plain byte — and Claude
                # answers it by tearing down its UI and waiting for a resume that can never
                # arrive on a pty it does not control, leaving a blank, dead screen. aGiTrack
                # owns the terminal, so aGiTrack does the suspending.
                self.suspend_requested = True
                continue
            if char == b"\x03":
                if self.capturing:
                    # Inside aGiTrack's own command palette, Ctrl-C cancels it
                    # (like Esc) rather than starting the exit flow.
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                    self.escape_buffer = None
                    continue
                # Ctrl-C starts aGiTrack's exit flow: the first press opens the
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
                if len(self.menu_key) == 1 and char == self.menu_key:
                    # The menu key again while the palette is open closes it (a toggle), like
                    # Esc — instead of typing a literal control byte into the command buffer.
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                    self.escape_buffer = None
                    continue
                if char in {b"\r", b"\n"}:
                    typed = self.buffer.decode(errors="ignore").strip()
                    command = self._resolve_command(typed)
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

            # Check for menu key match (single byte or multi-byte sequence)
            if len(self.menu_key) == 1:
                # Single byte menu key (plain ctrl-<letter>)
                if char == self.menu_key:
                    self.capturing = True
                    self.selected_index = 0
                    self.escape_buffer = None
                    continue
                forwarded.append(char)
            else:
                # Multi-byte menu key sequence (ctrl+shift+<letter>)
                self.menu_key_buffer.append(byte)
                buffer_bytes = bytes(self.menu_key_buffer)

                # Check if we have a complete match
                if buffer_bytes == self.menu_key:
                    self.capturing = True
                    self.selected_index = 0
                    self.escape_buffer = None
                    self.menu_key_buffer.clear()
                    continue

                # Check if we have a partial match (prefix of menu_key)
                if self.menu_key.startswith(buffer_bytes):
                    # Still accumulating, don't forward yet
                    continue

                # No match - forward the buffered bytes (which includes current byte)
                # and clear the buffer
                forwarded.append(buffer_bytes)
                self.menu_key_buffer.clear()

        return forwarded, b"", command, should_exit

    def text(self) -> str:
        return self.buffer.decode(errors="ignore")

    def _named(self, head: str) -> list[str]:
        """Commands whose NAME starts with `head` — strictly, with no fallback."""
        return [command for command in self.extra_commands + self.COMMANDS if command.startswith(head)]

    def matches(self) -> list[str]:
        commands = self.extra_commands + self.COMMANDS
        text = self.text()
        if not text:
            return commands
        # Filter on the command NAME only. Several commands take an argument (`sessions 2`,
        # `sessions new`, `agent-backend opencode`), and matching the whole string meant the
        # moment a space was typed nothing matched. The `or commands` keeps the DISPLAY from
        # going blank; what Enter runs is decided by _resolve_command, which does not fall back.
        return self._named(text.split(" ", 1)[0]) or commands

    def _resolve_command(self, typed: str) -> str:
        """The command Enter should run for `typed` — name resolved, argument preserved.

        Never falls back to the displayed list. Both fallbacks used to run a command the user
        did not type: with unmerged work present "merge" is PREPENDED to that list, so both
        `sessions 2` (an argument matched no name) and `frobnicate` (a typo) ran **merge**.
        """
        head, _, argument = typed.partition(" ")
        argument = argument.strip()
        if not typed:
            return self.selected() or typed
        if not self._named(head):
            # No command by that name: hand the text on so the user is TOLD it is unknown.
            return typed
        selected = self.selected()
        if not argument:
            return selected or typed
        if selected and (selected == typed or selected.startswith(typed)):
            # The whole typed string is itself a command name ("exit aGiTrack"), not a name
            # plus an argument — appending the "argument" would duplicate half the name.
            return selected
        return f"{selected or head} {argument}"

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


class ProxyRunner(BranchWatchMixin, ManualCommitsMixin, SessionSharingMixin, UpdatesMixin):
    # The active session's integration target. It is a Session.FIELDS entry exposed
    # via a custom property added at the bottom of this module; declared here so mypy
    # knows its type (the property is attached dynamically).
    _base_branch: "str | None"
    # Per-session flag (a Session.FIELDS entry, delegated like the others); declared here
    # so mypy can resolve its type at the read sites above its dynamic attachment.
    _pending_merge_prompt: bool

    # Bracketed-paste tracking (_track_bracketed_paste). Class-level defaults so instances
    # built by for_testing(), which bypasses __init__, carry them too.
    _in_bracketed_paste: bool = False
    _paste_scan_tail: bytes = b""

    # Defaults for the tunable timings; overridden per-instance from the global
    # config in __init__ (see GlobalConfig.timings). Kept as class constants so
    # `self.X` still resolves for runners built via __new__ (tests).
    FILE_STABLE_SECONDS = 8.0
    CHILD_IDLE_SECONDS = 4.0
    # A backend TUI's idle heartbeat (measured at 8 bytes/second on Linux) keeps
    # `last_child_output` permanently fresh, which would let the PTY veto turn-end detection
    # forever (see `_backend_idle_for`). Once the transcript — which only advances on real
    # backend work — has been quiet for this multiple of the idle window, the transcript wins.
    TRANSCRIPT_IDLE_FACTOR = 3.0
    # How often to retry resolving a session's transcript path while it is still unresolved.
    # The path is cached once found; only misses are retried, so this costs one project-dir
    # scan per interval and never per tick (see `_active_transcript_mtime`).
    TRANSCRIPT_LOOKUP_RETRY_SECONDS = 5.0
    POLL_SECONDS = 2.0
    PARSE_COOLDOWN_SECONDS = 10.0
    BASE_POLL_SECONDS = 3.0
    IDLE_AFTER_SECONDS = 30.0  # idle threshold: no input/output/work for this long ⇒ low-power loop
    IDLE_POLL_SECONDS = 30.0  # select timeout while idle (vs ACTIVE_POLL_SECONDS when working)
    ACTIVE_POLL_SECONDS = 1.0  # select timeout while active; every background sweep self-throttles to ≥2s anyway
    BASE_EDIT_CHECK_SECONDS = 3.0
    CWD_CHECK_SECONDS = 3.0
    BASE_DRIFT_CHECK_SECONDS = 2.0
    # Floor on the gap between git-pipeline passes. The reactor sets `_git_wake` every loop
    # iteration, so without this the worker ran ~74 passes/second, holding `_pipeline_lock`
    # essentially continuously — for work whose own sub-checks are throttled to seconds.
    GIT_PASS_MIN_INTERVAL = 0.25
    # Class-level defaults so every construction path has them (`for_testing` does not run
    # `__init__`); both are rebound per instance, so nothing is shared.
    _last_git_pass = 0.0  # monotonic start of the last git-pipeline pass
    _pipeline_lock_wanted = 0  # main threads waiting on _pipeline_lock (the worker yields)
    _parse_carry: bytes = b""  # output not yet parsed (see PARSE_MAX_BYTES)
    _parse_at = 0  # how far into _parse_carry the parser has got
    _mouse_resync: "bytes | None" = None  # half of a mouse report we gave up on (see _arm_mouse_resync)
    _stall_reported = 0.0  # longest stall already reported for the current loop iteration
    _stall_worst = 0.0  # worst reactor stall this run (reported in a crash report's context)
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps
    SYNC_MAX_HOLD = 0.05  # cap how long a backend synchronized-update may defer a paint
    # How long a trailing, not-yet-complete escape sequence (including a lone ESC) may be
    # held before it is treated as final. This is the classic ESC-disambiguation timeout:
    # long enough that the rest of a split sequence has certainly arrived (the remainder of
    # a mouse report is microseconds behind), short enough that the Escape KEY is delivered
    # imperceptibly late. See _hold_incomplete_tail for why holding the lone ESC matters.
    INPUT_TAIL_HOLD = 0.05
    SUMMARY_WAIT_SECONDS = 45.0  # how long integration waits for a background commit summary (#8)
    # On an INTERACTIVE exit we summarize the final turn's commit (see _start_commit_summary)
    # and wait up to this grace for the summary — whether freshly started at exit-finalize or
    # already running from the live path — to be amended in before integrating; past it the
    # commit integrates with its prompt-based message and the summary lands as git notes only,
    # so teardown is never held hostage. Long enough for a typical summarizer round-trip to
    # land, bounded so a slow/hung one can't stall exit. Per session. (Non-interactive/signal
    # teardown skips fresh summaries entirely and only ever joins an already-running one here.)
    EXIT_SUMMARY_GRACE_SECONDS = 30.0
    EXIT_SHARE_TIMEOUT = (
        45.0  # cap the exit-path auto-share push; long enough for a real push, short enough to never truly hang
    )
    SHARED_FETCH_TIMEOUT = 30.0  # cap the shared-session listing fetch so a slow/offline remote can't hang the menu
    RESUME_FETCH_TIMEOUT = 300.0  # cap the full-transcript fetch when resuming a shared session; generous since the user can cancel manually
    SHARE_PUSH_TIMEOUT = (
        120.0  # cap a background share/update push to origin so a stalled remote can't strand the worker thread
    )

    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        use_worktrees: bool = True,
        manual_commits: bool = False,
        backend_args: list[str] | None = None,
        commit_guidance: bool = True,
        full_agent_messages: bool = False,
        delay_merge: bool = False,
        sandbox: bool = True,
        allowed_edit_paths: list[str] | None = None,
        backend_command: list[str] | None = None,
        # Optional path to a user-facing event log (an AI change detected, a commit made, a
        # merge integrated, an update available). None ⇒ no event log. See agitrack.events.
        log_file: str | None = None,
        # True when cli.py already ran the blocking startup gh-availability prompt for
        # this launch, so run() skips its own in-TUI gh notice (avoids double-nagging).
        gh_prechecked: bool = False,
        # Suppress the privacy acknowledgment (set on an in-app menu re-exec, where the
        # user already acknowledged it earlier this session) — see _acknowledge_privacy_warning.
        skip_privacy_ack: bool = False,
        # Called once every blocking startup question has been answered and the TUI is about to
        # take the terminal. The dashboard opens from here rather than from cli.py: a browser
        # window arriving while questions are still waiting in the console pulls the user away
        # from a prompt they have not noticed is there.
        on_startup_complete: "Callable[[], None] | None" = None,
        # Optional injected collaborators (default to production construction).
        # These keyword arguments are for testing and advanced use; the CLI call
        # site passes only the first five parameters and is unaffected.
        _global_config: "GlobalConfig | None" = None,
        _state: "AgitrackState | None" = None,
        _integration: "IntegrationService | None" = None,
        _lock: "RepoLock | None" = None,
    ) -> None:
        # Attach the initial session; per-session state lives on it.
        self.active = Session.bare()
        self.repo = repo
        self._use_worktrees = use_worktrees  # #9: when False, run on the current branch directly
        # Manual-commit mode: user-triggered commits with per-turn agent work recorded as hidden
        # "latent" commits on refs/agitrack/manual/<session_id> instead of landing on the branch.
        # Implies no worktrees (resolved in cli.py). Off by default — a strict addition that leaves
        # the worktree and existing no-worktree paths untouched.
        self._manual_commits = manual_commits
        # Manual-mode runtime state (set in _setup_manual_commit_mode): whether the fold/reset
        # hooks were installed, the last observed HEAD (poll fallback baseline), and the cached
        # working-tree snapshot the latent gate hands to the latent record.
        self._manual_hooks_installed = False
        self._manual_last_head: str | None = None
        self._manual_pending_tree: str | None = None
        # Whether installing the worktree base-commit guard had to step the PERSISTENT auto-track
        # pre-commit hook aside (they share one hook slot). Drives its restore on teardown, so a
        # worktree run after a background/no-worktree one never silently uninstalls it.
        self._displaced_autotrack_hook = False
        # Which backends' session-start auto-start hooks this run took out (see
        # _displace_background_autostart). Empty when there were none to take.
        self._displaced_agent_autostart: list[str] = []
        # Whether the PERSISTENT auto-track pre-commit hook was already armed when this run set up
        # (see _setup_manual_commit_mode). False means WE armed it, so teardown gives the slot
        # back rather than leaving an interactive session having turned auto-start on. None means
        # no setup has run yet, so nothing may act on it.
        self._autotrack_hook_preexisting: bool | None = None
        # Facts about the turn the agent is currently running, refreshed from every session
        # export (see _note_in_flight). Lets the fold trailer attribute a commit the AGENT makes
        # ITSELF before that turn ends — a turn only becomes a pending latent turn once complete.
        self._in_flight: dict | None = None
        self._warned_parallel_no_worktree = False  # one-time shared-tree caveat for extra --no-worktree sessions
        # No-worktree only: the branch HEAD when this session started. Commits made past it that
        # aGiTrack didn't author are the agent's own (#35 cover applies); the anchor floors the
        # scan so pre-existing user history is never covered. Set at startup in no-worktree mode.
        self._noworktree_base_head: str | None = None
        # When True, tell a coding agent that aGiTrack auto-commits so it doesn't self-commit
        # (--no-commit-guidance / config turns it off). Appended to the agent's system prompt
        # where the backend supports it (Claude).
        self._commit_guidance = commit_guidance
        # Effective sandbox settings (CLI flag wins over config; resolved in cli.py).
        self._sandbox = sandbox
        self._allowed_edit_paths = list(allowed_edit_paths or [])
        # Per-run override for the "include all agent messages" trace option
        # (--full-agent-messages). True forces it on for this run regardless of the
        # per-repo config; False (the default) defers to ``state.full_agent_messages``.
        self._full_agent_messages = full_agent_messages
        # --delay-merge: when True, a turn's committed changes are NOT auto-integrated
        # into the base branch. Instead the user is told to review/edit them in the
        # working directory and merge on their confirmation (session menu → "Merge
        # reviewed changes"). Off by default (aGiTrack integrates each turn immediately).
        self._delay_merge = delay_merge
        # Extra CLI args forwarded verbatim to every backend spawn (#32).
        self._backend_args = list(backend_args or [])
        # Per-run override (from --backend-command) for the command that launches the
        # backend, replacing its executable so the agent runs under a user wrapper. When
        # empty, the per-backend config value (GlobalConfig.backend_command) applies. The
        # override wins for whatever backend is active; the config form is keyed by backend.
        self._backend_command = list(backend_command or [])
        # User-facing event log (an AI change detected, a commit made, an update available).
        # Keyed on the base repo root so a relative --log-file resolves the same everywhere.
        self.events = EventLog(resolve_log_path(log_file, repo.repo))
        # Keep the log out of the user's tree. Under -b the tracker's own `git add` swept an
        # in-repo events.log into the AGENT'S commit — aGiTrack telemetry about a turn ending
        # up inside the turn, attributed to the AI, in permanent history.
        exclude_log_file(repo.repo, self.events.path)
        # cli.py already showed the blocking startup gh prompt ⇒ suppress the in-TUI notice.
        self._gh_prechecked = gh_prechecked
        # One-shot: the hook install path runs again on every worktree-guard restore, and the
        # core.hooksPath notice must not repeat down the session.
        self._hooks_path_notice_shown = False
        # Trailing bytes of a host-terminal reply whose terminator has not arrived yet
        # (see _HOST_TERMINAL_REPLY_PREFIX_RE).
        self._held_reply_prefix = bytearray()
        self._skip_privacy_ack = skip_privacy_ack
        self._on_startup_complete = on_startup_complete
        self._force_new_session = new_session  # start a fresh conversation, do not resume
        self.name = "main"  # session label (multiplexer assigns names to others)
        self._primary_worktree_name: str | None = None  # session kept across exits for auto-resume
        self._exit_resume_worktree: str | None = None  # session active at exit → auto-resumes next start
        self.worktree: WorktreeInfo | None = None  # set when this session runs in a git worktree
        self.global_config = _global_config if _global_config is not None else GlobalConfig()
        # Load THIS repo's .agitrack/config.json overlay so repo-scoped settings are both READ
        # and WRITABLE in the session. Without it `repo_path` is None and save_repo() silently
        # drops every "this repository only" settings change (e.g. turning worktrees off), so
        # the change never persists and reverts on the next launch. Uses the base repo path
        # (this arg, before any worktree is created) so the overlay is the durable one.
        if getattr(self.global_config, "repo_path", "set") is None:
            self.global_config.load_repo_overlay(repo.repo)
        self._apply_timings(self.global_config.timings)
        self.state = (
            _state
            if _state is not None
            # An explicit --backend seeds the state's default so a brand-new repo (no
            # stored backend) resolves to it rather than to the configured default,
            # which may be unset (there is no hardcoded fallback).
            else AgitrackState(repo.repo, default_backend=backend or self.global_config.default_backend)
        )
        if backend:
            if backend != self.state.backend:
                self.state.remember_backend_session()
                self.state.backend_session_id = self.state.stored_backend_session(backend)
                self.state.last_backend_message_id = None
            # Record the explicit `--backend` choice even when it only CONFIRMS what the state
            # already resolves to. On a fresh repo the state was seeded with this very flag as an
            # in-memory default, so `backend != self.state.backend` was false and nothing was ever
            # written: `--backend claude` (documented as "also saved as the global default") left
            # both the state file and the global config empty, and every later reader of them saw
            # no backend at all.
            self.state.backend = backend
            if self.global_config.default_backend != backend:
                self.global_config.default_backend = backend
        self.backend = make_proxy_agent(self.state.backend)
        self.actions = AgitrackActions(repo, self.state, verbose=verbose)
        self.verbose = verbose
        # Use the menu key sequence (single byte for ctrl-<letter>, multi-byte
        # escape sequence for ctrl+shift+<letter>)
        menu_key_seq = self.global_config.menu_key_sequence
        self.input = ProxyInput(menu_key=menu_key_seq if menu_key_seq else self.global_config.menu_key_byte)
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
        # aGiTrack must provide wheel-driven scrollback (Claude streams to the normal
        # screen and relies on native scrollback, which aGiTrack's render replaces).
        self.child_mouse = False
        self.scroll_back = 0
        self._last_render = 0.0
        # A cross-backend session switch requested from the (nested) sessions modal is deferred to
        # the top-level reactor; doing it inline from the modal breaks host console input on
        # Windows. Holds the target Session until the timers phase runs it. (See _switch_to_session_index.)
        self._pending_switch_session: "Session | None" = None
        self._render_pending = False
        # Synchronized output (DECSET 2026): while the backend is mid-update we
        # hold the repaint so a half-drawn frame is never shown (tearing), and
        # each repaint aGiTrack emits is itself wrapped in a 2026 update so the host
        # applies the whole frame atomically (the flicker fix).
        self._in_sync_update = False
        self._sync_since = 0.0
        # Mouse drag selection -> clipboard (for backends aGiTrack renders itself).
        self.sel_active = False
        self.sel_anchor: tuple[int, int] | None = None
        self.sel_point: tuple[int, int] | None = None
        self._input_tail = b""
        self._input_tail_at = 0.0  # monotonic time the held tail was first held (INPUT_TAIL_HOLD)
        self._input_tail_debris = False  # the held tail is a dropped mouse report, not a keypress
        self._parse_carry = b""  # backend output past this pass's parse budget
        self._parse_at = 0
        self._last_git_pass = 0.0  # monotonic start of the last git-pipeline pass
        self._pipeline_lock_wanted = 0  # main threads waiting on _pipeline_lock (worker yields)
        self.last_child_output = 0.0
        self.last_user_input = 0.0  # monotonic time of the user's last keystroke (drives idle backoff)
        self.last_child_output_sample = b""
        # (session id, resolved transcript path) cache for _active_transcript_mtime, so the
        # liveness stat doesn't re-scan the project dirs every idle tick.
        self._transcript_path_cache: tuple[str | None, Path | None] = (None, None)
        self._transcript_lookup_at = 0.0  # throttle for retrying an unresolved transcript path
        self._commit_gate_last: str | None = None  # last logged commit-gate snapshot (debug trace)
        self._commit_gate_logged_at = 0.0
        self._last_spawn_command: list[str] = []  # the exact command of the most recent spawn
        # Set when the backend exits repeatedly and aGiTrack stops relaunching, so
        # run() can echo the reason on the restored host screen instead of leaving
        # the user with a silent flash (#114).
        self._backend_exit_notice: str | None = None
        # The claude session we want to resume is held by a running background agent:
        # fork a copy on the next spawn (claude --fork-session), and remember we did so
        # we only try once rather than looping (#114).
        self._fork_next_spawn = False
        self._forked_for_busy = False
        self.last_status = ""
        self.last_status_change = 0.0
        self.message: str | None = None
        self.message_until = 0.0
        # Scroll position of an over-tall popup message and how far it can scroll (set on
        # render). 0/0 = the message fits, so PgUp/PgDn pass through to history scrolling.
        self._message_scroll = 0
        self._message_max_scroll = 0
        # Track whether we proactively enabled the kitty keyboard protocol for
        # shift-modified menu keys. Used for cleanup on exit.
        self._kitty_keyboard_enabled = False
        # A sticky message stays up until the user's next keypress instead of
        # timing out — used for the auto-commit confirmation so the user actually
        # sees that aGiTrack committed (and isn't misled by the backend asking to
        # commit work aGiTrack has already captured).
        self._message_sticky = False
        # Per-session status notices (commit/summary lifecycle), keyed by session
        # name and composed into a multi-line popup so concurrent sessions each
        # get their own line. Host-level (a display concern shared across sessions).
        self._session_notices: dict[str, tuple[str, float, bool]] = {}
        self._notice_shown = False  # True while self.message reflects the notices
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
        # A turn was forwarded to the agent and aGiTrack has not yet reconciled it into a
        # commit. Unlike ``agent_in_flight`` (cleared by an idle TIMER), this stays set
        # across the quiet window between the agent finishing and the commit landing.
        self.turn_awaiting_commit = False
        # An interrupted turn whose changes the user chose to KEEP for the next turn's commit.
        # The parse that carried the cancellation is consumed either way, so without this the
        # kept work would be reclassified as the user's own dirt the moment the handler
        # returned — and offered back to them as a plain user commit, with no trace.
        self.cancelled_work_kept = False
        self.kept_cancelled_paths: frozenset[str] = frozenset()
        # Untracked paths that already existed when the current turn began: they are the USER's
        # files, not this turn's output, so the turn's commit must not sweep them in.
        self.untracked_before_turn: frozenset[str] = frozenset()
        # The same question across a whole no-worktree fold (which can absorb several turns):
        # the files present before every one of them. None until the first turn after a fold.
        self.user_untracked_since_fold: frozenset[str] | None = None
        # (base sha, session sha) the user last answered "Leave for later" for, so the conflict
        # box — raised from a poll — asks once per situation rather than every couple of seconds.
        self.conflict_deferred_for: tuple[str, str] | None = None
        self.pre_agent_reconciled_status = ""
        self.last_parse_attempt_status = ""
        self.last_parse_finish = 0.0
        self.passthrough_prompt = bytearray()
        self.passthrough_escape: bytearray | None = None
        # Whether the terminal is mid bracketed-paste right now, tracked over every stdin
        # read so a popup opened DURING a paste knows the bytes it is about to see are
        # pasted content rather than the user answering it (_track_bracketed_paste).
        self._in_bracketed_paste = False
        self._paste_scan_tail = b""
        self.pending_forwarded: list[bytes] | None = None
        self.pending_prompt_text = ""
        # Session ids that existed before aGiTrack launched a fresh backend session,
        # used to identify (and then pin to) the session aGiTrack actually spawned
        # rather than chasing whichever session is globally newest.
        self._pre_spawn_sessions: dict[str, float] | None = None
        # Throttle for _service_native_session_switch (see SESSION_WATCH_SECONDS).
        self._session_watch_at = 0.0
        # Raw responses captured from the host terminal so we can answer the
        # same queries OpenCode makes (foreground/background/palette colors and
        # device attributes). Without these, OpenCode cannot detect the real
        # terminal theme and its colors do not match a native session.
        self.host_fg_value: bytes | None = None
        self.host_bg_value: bytes | None = None
        self.host_palette: dict[bytes, bytes] = {}
        self.host_da: bytes | None = None
        self.host_kitty_keyboard: bool = False
        # The early capability service (see _start_early_capability_service): the thread that
        # answers the backend's terminal queries between the spawn and the reactor, and what it
        # read while doing so.
        self._early_capability_thread: threading.Thread | None = None
        self._early_capability_stop = threading.Event()
        self._early_capability_buffer = bytearray()
        self.color_mode = detect_color_mode()
        if os.name == "nt" and self.color_mode == "16":
            # The modern Windows console (conhost / Windows Terminal) renders 24-bit color and
            # aGiTrack enables VT processing on it, but it sets neither COLORTERM nor TERM, so
            # detect_color_mode lands on its 16-color fallback. That downsamples the backend's
            # truecolor UI when aGiTrack re-emits it — flattening Claude's subtle palette to
            # near-greyscale (OpenCode's bolder colors survived 16-color, which is why only Claude
            # looked wrong). Render truecolor on Windows so the backend's colors are preserved.
            self.color_mode = "truecolor"
        # Agent background (renderer.py, "Agent background"): by default aGiTrack paints no
        # canvas at all, so every cell the backend leaves untouched keeps the host terminal's
        # own background. Only an explicit "dark"/"light" overrides that, and then for the
        # whole session. The canvas state is host-level, not per session.
        self.agent_background = getattr(self.global_config, "agent_background", "terminal")
        self._canvas: tuple[str, str] | None = None
        ScreenRenderer.apply_agent_background(self)  # derived here, and only re-derived if the setting is edited
        # Single-writer management: only one aGiTrack may auto-commit/merge in a
        # working tree. A second instance is refused at startup (see `run`).
        self.management_lock = _lock if _lock is not None else RepoLock(repo.repo / ".agitrack" / "lock")
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
        # _base_branch is per-session (a Session.FIELDS entry that delegates to the
        # active session); each session integrates into its own branch.
        self._repo_dir_branch: str | None = None  # branch checked out in the repo dir (for the status bar)
        # _pending_merge_prompt is per-session (a Session field) — see Session.FIELDS.
        self._integration_paused = False  # reserved; integration is no longer globally paused
        self._base_drift_check_at = 0.0
        self._agent_branch_check_at = 0.0  # throttles the poll of the worktree's own checked-out branch
        self.turn = 0  # per-session transient-branch counter
        self.merge_ctx: MergeContext | None = None  # in-progress agent merge resolution
        self._pending_enter_at: float | None = None  # deferred submit of an injected prompt
        self._pending_enter_fd: int | None = None  # the PTY that injected prompt's Enter must go to
        self._base_advanced = False  # base moved; sync idle sessions onto it on the next loop pass
        self._last_base_head: str | None = None  # last-polled base HEAD, to catch out-of-band commits
        self._base_head_mtime = -1.0  # mtime signature of the repo dir's .git/HEAD (gates the branch poll)
        self._base_ref_mtime = -1.0  # mtime signature of the base branch ref (gates the base-HEAD poll)
        self._base_edits_declined_status: str | None = None  # base status the user declined to commit
        self._popup_exit_pending = False  # a popup Ctrl-C exit flow is running
        self._popup_exit_force = False  # second Ctrl-C inside the exit confirmation
        # True only while an EXIT-CONFIRMATION popup is open (the "Exit aGiTrack?" / terminate-
        # background-sessions dialogs). It is the ONLY popup where Ctrl-C may confirm/force the
        # exit. In every other popup — including the keep/delete-worktree and copy-back prompts
        # shown DURING the exit finalize — Ctrl-C must not exit aGiTrack directly (see _run_modal).
        self._exit_confirmation_active = False
        self._reap_pids: list[int] = []  # signalled backends awaiting their waitpid
        self._idle_integrate_at = 0.0  # throttle for integrating agent-made commits
        self._attach_uncovered_until = 0.0  # deadline for attaching traces to backend-made commits (#35)
        self._summary_thread: threading.Thread | None = None  # background commit-summary worker (#8)
        self._summary_result: dict | None = None  # finished summary awaiting main-thread application
        self._summary_pending: dict | None = None  # {"sha", "since"} while a summary is being computed
        # {"head", "latent", "single"} for the newest no-worktree AUTO fold, so a late summary can
        # still find the real commit that absorbed its latent turn (see _folded_head_for).
        self._last_auto_fold: dict | None = None
        self._precompact_thread: threading.Thread | None = None  # background pre-compaction summary worker
        self._precompact_result: dict | None = None
        # Automatic backend self-update: when the agent's own updater is sandbox-blocked,
        # aGiTrack runs it from its UNCONFINED proxy (not the sandboxed agent) on a background
        # thread, plus the finished result the main loop surfaces. See _maybe_auto_update_backend.
        self._backend_update_thread: threading.Thread | None = None
        self._backend_update_result: dict | None = None
        # Name of the backend we've already evaluated for an automatic self-update (when its own
        # updater is sandbox-blocked). Switching to a different backend re-evaluates (once).
        self._backend_update_checked_for: str | None = None
        self._base_poll_at = 0.0  # throttle for the base-HEAD poll
        self._warned_backend_session = False  # one-shot "use agit to start sessions" notice
        # Backends whose repo-root "last session" record has already been written during THIS
        # exit. First writer wins (see _remember_session_for_backend).
        self._backends_remembered_on_exit: set[str] = set()
        # Auto-share (issue #55): for sessions the user opted to keep shared, the
        # last-pushed content hash per backend session id, plus the in-flight
        # background push thread (only one at a time). Triggered per commit.
        self._auto_share_hash: dict[str, str] = {}
        self._auto_share_thread: threading.Thread | None = None
        # The live auto-share worker (off-thread) leaves its result here for the main loop to
        # surface; a notice is shown only on FAILURE/behind (success is silent, since it fires
        # every commit). None ⇒ nothing to report. See _service_auto_share_outcome.
        self._auto_share_outcome: dict | None = None
        # Session ids already told (this run) that auto-share is trimming their oversized
        # transcript — so the truncation notice shows ONCE, not on every commit's auto-share.
        self._auto_share_truncation_warned: set[str] = set()
        # aGiTrack session ids (stable, never drift) that saw at least one committed turn
        # THIS run. The exit-path auto-share consults this so a session that was only
        # resumed and never typed into is not re-shared — robust where a transcript
        # digest is not, since Claude's resume id-churn changes the digest across runs
        # even with no user input.
        self._sessions_with_activity: set[str] = set()
        # Backend message ids of interrupted (user-cancelled) turns we've already
        # offered a commit-or-discard choice for, so a "decide later" answer doesn't
        # re-prompt every parse cycle. In-memory and per-run by design — a restart
        # offers the choice again if the changes are still sitting there.
        self._cancel_prompted: set[str] = set()
        # Worktree files we've already offered to copy into the base repo, keyed by
        # repo-relative path → content fingerprint, so a file the user accepted/left in
        # place is re-offered only once its content changes again (per-run, in-memory).
        self._worktree_sessions_cache: tuple[float, list] | None = None  # memoizes _worktree_sessions
        self._copy_prompted: dict[str, tuple[int, int]] = {}
        # Repo-relative paths the user declined to copy: muted so the SAME set isn't
        # re-asked even as its contents change. A genuinely new path un-mutes the whole
        # set (ask about all again); cleared on session switch and aGiTrack restart.
        self._copy_declined: set[str] = set()
        # Remembered copy-back decisions, kept while the candidate file SET is stable and reset
        # when it changes (a path added/removed) or aGiTrack restarts (these are in-memory only):
        #   _copy_always      — the user chose to copy this set, so later offers copy without
        #                       re-asking (the "always copy" half; "always NOT copy" is _copy_declined).
        #   _overwrite_always — the user chose "always overwrite", so a conflict (a file that already
        #                       exists in the base) is overwritten without the per-set overwrite prompt.
        #   _copy_decision_set — the candidate set the two flags above were decided for; a different
        #                       set clears them so the user confirms again.
        self._copy_always: bool = False
        self._overwrite_always: bool = False
        self._copy_decision_set: set[str] = set()
        # A turn's copy offer, collected on the git worker (the worktree read) and handed to
        # the main thread to PRESENT — so the worker never blocks on the popup and keeps the
        # commit/summary/merge pipeline flowing while the user decides. `(context, collected)`.
        self._pending_copy_offer: tuple[str, tuple[list[str], list[tuple[str, tuple[int, int]]], set[str]]] | None = (
            None
        )
        # Deferred "the base repo has newer files — overwrite the worktree's older copies?"
        # prompts: (worktree path, repo-relative paths) queued when a worktree is reopened
        # (possibly before the UI is up) and presented by the loop. See _refresh_worktree_from_base.
        self._pending_env_refresh: list[tuple[Path, list[str]]] = []
        # Deferred "copy the full environment into this NEW worktree, or only tracked files?"
        # prompts: (worktree path, name), queued at creation and presented by the loop so startup
        # stays fast and the prompt has a UI. See _present_pending_env_copy.
        self._pending_env_copy: list[tuple[Path, str]] = []
        # Leftover worktree directory names found at a --no-worktree startup, queued for a
        # "delete them?" prompt (their presence can confuse a resumed agent into editing them
        # instead of the base repo). Presented by the loop. See _present_pending_noworktree_cleanup.
        self._pending_noworktree_cleanup: list[str] = []
        # Per-worktree record of files copied INTO the worktree FROM the base (path → {rel:
        # fingerprint}), so the copy-back-to-base offer can skip files that merely came from the
        # base and weren't changed — otherwise they'd loop base → worktree → base. See
        # _mark_env_from_base and _collect_copy_candidates.
        self._env_from_base: dict[str, dict[str, tuple[int, int]]] = {}
        # Per-worktree time (epoch seconds) the initial base→worktree environment copy finished.
        # The copy-back offer skips anything NOT modified since (mtime <= watermark): those files
        # came from the base copy, so offering them back would be a no-op loop. Files the agent
        # creates/edits in the worktree afterward have a newer mtime and are still offered. This
        # catches what the per-file registry can't — a copied DIRECTORY reported collapsed.
        self._env_copy_watermark: dict[str, float] = {}
        # Settings-menu scratch: edits collected as pending changes (each with its chosen
        # scope) and written only when the user confirms "save" on the way out.
        self._settings_pending: dict[str, tuple[object, str, bool]] = {}
        self._settings_pending_timings: dict[str, tuple[float, str]] = {}
        # Lifecycle flags read before their first conditional assignment. These
        # MUST be initialized here: their getattr() guards were removed in P7,
        # and for_testing() seeding them alone would hide a missing init from
        # the suite (the real __init__ is the production path).
        self._monitor_base_edits = False
        self._base_check_at = 0.0
        # Base-repo files the agent has stranded that we've already flagged. Kept separate from
        # the baseline so a warned file is not folded into "not stranded" — it stays counted for
        # the exit reminder, while the warned-set stops us re-nagging about the same file.
        self._base_warned_files: set[str] = set()
        self._cwd_drift_checked = False
        self._cwd_check_at = 0.0
        self._cwd_launch_at = 0.0  # epoch of the latest backend launch (set in _spawn)
        # Shared-session resume runs the (possibly slow) transcript fetch on a worker
        # thread while the menu waits (cancellably); the main loop completes the
        # resume. The cancel Event lets the user stop a slow fetch (Esc) and lets the
        # exit path stop any in-flight fetch immediately.
        self._shared_resume_thread: threading.Thread | None = None
        self._shared_resume_result: dict | None = None
        self._shared_resume_cancel: "threading.Event | None" = None
        # Fire-and-forget network share ops (e.g. unshare) run on daemon threads so the
        # session never freezes; _service_background_share_ops surfaces each result as a
        # notice when it finishes. Each entry: {key, thread, box, outcome_fn}.
        self._background_share_ops: list[dict] = []
        # A share refused because the SHARED copy is already newer (PublishResult.behind):
        # the background op stashes the context here, and _service_share_conflicts later
        # (on the main thread, NOT mid-iteration over _background_share_ops) prompts the
        # user to overwrite or merge. Each entry: {payload, store, display, session_id}.
        self._pending_share_conflicts: list[dict] = []
        # Set when the menu key (Ctrl-G) is pressed inside an open popup: closes the WHOLE
        # command menu (every nested level) back to the agent, rather than going up one level.
        self._exit_menu_requested = False
        self._relaunch_times: list[float] = []
        # Per-background-session crash-loop guard (keyed by id(session)): a background backend
        # that exits is relaunched+resumed like the active one, but a backend that won't stay up
        # is finally dropped instead of looping. (Session uses __slots__, so this can't live on
        # the session object.) See _relaunch_background_session.
        self._bg_relaunch_times: dict[int, list[float]] = {}
        # A switch's user-commit / copy offer is deferred until the switched-to session's latest
        # agent turn has been reconciled (parsed + committed) in the background, so the agent's
        # own changes aren't offered as the user's. Holds the Session awaiting its offer (or None);
        # the bool tracks that we've already kicked the one reconciling parse. See
        # _service_deferred_switch_offer.
        self._pending_switch_copy_offer: "Session | None" = None
        self._switch_offer_parse_started = False
        self._exiting = False
        self._finalized_on_exit = False
        # The exit-time "delete this repo's session worktrees or keep them?" decision, asked
        # once and applied to every one of them (None = not yet asked). See
        # _should_delete_worktrees_on_exit.
        self._exit_delete_worktrees: bool | None = None
        # The IDLE session worktrees the same answer applies to: on disk but not open in this
        # run. Captured when the question is asked (before any removal empties the live
        # sessions' own records) and consumed by _delete_idle_worktrees_on_exit.
        self._exit_idle_worktrees: list[str] = []
        # Set when the user presses Esc on the on-exit copy offer: aborts the in-progress
        # exit so they can deal with the worktree files themselves (see _run_exit_flow).
        self._exit_aborted = False
        # The metrics dashboard, when started from the Ctrl-G menu, runs as a separate
        # background process (read-only HTTP server) rather than in-process, so its git
        # log work and energy use are charged to that child, not the TUI (#110). It is
        # a detached daemon (like `agitrack -d`), so it keeps running after aGiTrack
        # quits — and after the terminal closes — until `agitrack -d stop`.
        # None until first started; the handle only powers the "already running" fast path.
        self._dashboard_url: str | None = None
        # The port the dashboard child actually bound, so the remote-access hint can name it
        # in a copy-pasteable `ssh -L` command even when we're only reusing a running daemon.
        self._dashboard_port: int = 0
        # Set once the interactive exit flow has committed to quitting (worktree
        # removed). The reactor loop checks this after running a menu command so an
        # "exit"/"quit" command breaks the loop instead of falling through to the
        # timers phase and running git in the deleted worktree.
        self._exit_requested = False
        # The user's intentionally-unstaged files belong to the base working tree
        # (their repo), not the ephemeral session worktree; cache the list so the
        # status line can show its count without a per-frame disk read.
        self._user_declined: list[str] = []
        self.sessions: list[Session] = []
        self.worktree_manager: WorktreeManager | None = None

        # Diagnostic switches (off by default, identical on every platform; user docs in
        # README "Debugging / diagnostic logs" and AGENTS.md "Diagnostics & Debugging"):
        #   DEBUG_PROXY -> proxy-debug-<run>.log   (human-readable event log; --verbose implies it)
        #   DEBUG_RAW   -> proxy-raw-<run>.log      (byte-exact I/O capture; implies DEBUG_PROXY)
        # Honour the AGITRACK_/AGIT_ prefixed names (the project convention) AND the bare
        # DEBUG_RAW / DEBUG_PROXY: these are throwaway switches a user sets ad hoc in their shell,
        # and the bare form is the obvious thing to reach for (a prefixed-only flag silently
        # produced no log). Truthy = 1/true/yes (case-insensitive).
        def _debug_flag(name: str) -> bool:
            value = getenv_compat(name)
            if value is None:
                value = os.environ.get(name)
            return (value or "").strip().lower() in {"1", "true", "yes"}

        self.raw_capture = _debug_flag("DEBUG_RAW")
        self.debug_proxy = verbose or self.raw_capture or _debug_flag("DEBUG_PROXY")
        # One diagnostic-log file per run, in the base repo's .agitrack/ (survives the
        # per-run worktree teardown).
        self._diag_run = time.strftime("%Y%m%d-%H%M%S")
        # Self-update: a background check runs on a throttle; when the user opts
        # in, the update is applied once every session is finished and committed,
        # then aGiTrack re-execs itself.
        self._updater = Updater()
        self._update_status: UpdateStatus | None = None  # latest completed check result
        self._update_check_at = 0.0  # throttle for the periodic background check
        self._update_check_thread: threading.Thread | None = None
        self._update_worker_result: UpdateStatus | None = None  # worker -> main handoff
        self._update_offered = False  # have we notified the user about the current update?
        self._update_pending = False  # user accepted; apply once sessions finish
        self._update_applying = False  # apply+restart in progress
        self._pending_restart = False  # re-exec aGiTrack after the loop tears down
        self._pending_msi_handoff = False  # restart goes through the elevated MSI installer, not re-exec
        self._pending_pip_handoff = False  # Windows pip upgrade runs in a helper after exit, not re-exec
        self._reopen_after_exit = False  # host terminal closed; user asked to reopen in a new window
        # Git worker (the automatic commit/merge pipeline runs here, never on the main
        # reactor thread, so typing is never blocked by a git subprocess). The worker
        # marshals any user dialog back to the main thread via `_modal_mailbox`.
        self._main_thread_ident: int | None = None  # set in run(); identifies the reactor thread
        self._git_worker: threading.Thread | None = None  # the dedicated git pipeline thread
        self._stop_worker = False  # set to break the worker loop (exit/restart/teardown)
        self._git_wake = threading.Event()  # wakes the worker on file changes / session changes
        self._pipeline_lock = threading.RLock()  # guards a pipeline pass vs. session swap / exit
        self._modal_mailbox: queue.Queue = queue.Queue()  # worker → main: dialogs to present
        self._wake_r = -1  # self-pipe read end (added to select) — wakes the reactor on demand
        self._wake_w = -1  # self-pipe write end — the worker writes to wake the main loop
        # Platform host-I/O (issue #118), created in run(): the host terminal (raw mode +
        # a select-able stdin source) and the reactor waker. POSIX wraps termios/os.pipe;
        # Windows uses the Win32 console + socketpairs. None until run() (and in tests).
        self._host: Any = None
        self._waker: Any = None
        self._last_shutdown_check = 0.0  # rate-limit the .agitrack/shutdown sentinel poll

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
        self.IDLE_AFTER_SECONDS = timings["idle_after_seconds"]
        self.IDLE_POLL_SECONDS = timings["idle_poll_seconds"]

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
        # _base_branch is per-session; keep the single IntegrationService pointed at
        # whichever session is active (including the temporary actives set by
        # _with_session during background integration), so each merges into its own
        # branch. Skipped until the service exists (early construction).
        svc = self.__dict__.get("_integration_svc")
        if svc is not None:
            svc.base_branch = getattr(session, "_base_branch", None)

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
            # _base_branch is per-session now; seed the service from the active session.
            active = self.__dict__.get("_active_session")
            base_branch = getattr(active, "_base_branch", None) if active is not None else None
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
        a :data:`~agitrack.proxy.session.Session.FIELDS` entry is routed to the
        session; all other keyword arguments are set directly on the runner.

        Example::

            runner = ProxyRunner.for_testing(
                repo=fake_repo,
                state=AgitrackState(tmp_path),
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
                "last_user_input": 0.0,
                "_transcript_path_cache": (None, None),
                "_transcript_lookup_at": 0.0,
                "_message_scroll": 0,
                "_message_max_scroll": 0,
                "_message_sticky": False,
                "events": EventLog(None),  # disabled event log by default in tests
                "_gh_prechecked": False,
                "_skip_privacy_ack": False,
                "_session_notices": {},
                "_notice_shown": False,
                "_awaited_followups": [],
                "host_fg_value": None,
                "host_bg_value": None,
                "host_palette": {},
                "host_da": None,
                "host_kitty_keyboard": False,
                "_early_capability_thread": None,
                "_early_capability_stop": threading.Event(),
                "_early_capability_buffer": bytearray(),
                "color_mode": "truecolor",
                "agent_background": "terminal",
                "_canvas": None,
                "management_lock": None,
                "base_repo": None,
                "_repo_dir_branch": None,
                "_integration_paused": False,
                "_base_drift_check_at": 0.0,
                "_pending_enter_at": None,
                "_pending_enter_fd": None,
                "_base_advanced": False,
                "_last_base_head": None,
                "_base_head_mtime": -1.0,
                "_base_ref_mtime": -1.0,
                "_base_edits_declined_status": None,
                "_popup_exit_pending": False,
                "_popup_exit_force": False,
                "_exit_confirmation_active": False,
                "_reap_pids": [],
                "_idle_integrate_at": 0.0,
                "_attach_uncovered_until": 0.0,
                "_precompact_thread": None,
                "_precompact_result": None,
                "_base_poll_at": 0.0,
                "_warned_backend_session": False,
                "_backends_remembered_on_exit": set(),
                "_auto_share_hash": {},
                "_auto_share_thread": None,
                "_auto_share_outcome": None,
                "_auto_share_truncation_warned": set(),
                "_backend_update_thread": None,
                "_backend_update_result": None,
                "_backend_update_checked_for": None,
                "_sessions_with_activity": set(),
                "_cancel_prompted": set(),
                "_worktree_sessions_cache": None,
                "_copy_prompted": {},
                "_copy_declined": set(),
                "_copy_always": False,
                "_overwrite_always": False,
                "_copy_decision_set": set(),
                "_pending_copy_offer": None,
                "_pending_env_refresh": [],
                "_pending_env_copy": [],
                "_pending_noworktree_cleanup": [],
                "_env_from_base": {},
                "_env_copy_watermark": {},
                "_exit_delete_worktrees": None,
                "_exit_idle_worktrees": [],
                "_settings_pending": {},
                "_settings_pending_timings": {},
                "_full_agent_messages": False,
                "_delay_merge": False,
                "_user_declined": [],
                "sessions": [],
                "worktree_manager": None,
                "raw_capture": False,
                "debug_proxy": False,
                "_diag_run": "test",
                "_force_new_session": False,
                "_primary_worktree_name": None,
                "_exit_resume_worktree": None,
                "global_config": None,
                # Lazily-set fields that getattr() guards in production methods:
                "_monitor_base_edits": False,
                "_base_check_at": 0.0,
                "_base_warned_files": set(),
                "_cwd_drift_checked": False,
                "_cwd_check_at": 0.0,
                "_cwd_launch_at": 0.0,
                "_shared_resume_thread": None,
                "_shared_resume_result": None,
                "_shared_resume_cancel": None,
                "_background_share_ops": [],
                "_pending_share_conflicts": [],
                "_exit_menu_requested": False,
                "_use_worktrees": True,
                "_manual_commits": False,
                "_manual_hooks_installed": False,
                "_manual_last_head": None,
                "_manual_pending_tree": None,
                "_in_flight": None,
                "_noworktree_base_head": None,
                "_warned_parallel_no_worktree": False,
                "_sandbox": True,
                "_allowed_edit_paths": [],
                "_backend_command": [],
                "_commit_guidance": True,
                "_relaunch_times": [],
                "_bg_relaunch_times": {},
                "_fork_next_spawn": False,
                "_forked_for_busy": False,
                "_backend_exit_notice": None,
                "_last_spawn_command": [],
                "_exiting": False,
                "_finalized_on_exit": False,
                "_exit_aborted": False,
                "_pending_switch_copy_offer": None,
                "_switch_offer_parse_started": False,
                "_dashboard_url": None,
                "_dashboard_port": 0,
                "_exit_requested": False,
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
                "_reopen_after_exit": False,
                "_main_thread_ident": threading.get_ident(),
                "_git_worker": None,
                "_stop_worker": False,
                "_git_wake": threading.Event(),
                "_pipeline_lock": threading.RLock(),
                "_modal_mailbox": queue.Queue(),
                "_wake_r": -1,
                "_wake_w": -1,
                "_host": None,
                "_waker": None,
                "_last_shutdown_check": 0.0,
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

        # Tests build minimal AgitrackStates without a backend; production always resolves
        # one before launch (there is no hardcoded fallback anymore), so reading
        # ``state.backend`` on an unconfigured state now raises. Seed a concrete backend so
        # the test factory keeps producing a launch-ready session unless a test set one.
        # Guarded so tests that pass a non-AgitrackState stub (no ``data`` dict) are untouched.
        #
        # The default is overridable via ``backend_name=`` so a test can ask for a session on
        # the OTHER backend without hand-building a state. It matters that this is reachable:
        # while it was an unconditional "claude", every one of the ~260 tests built through this
        # factory ran on Claude and no runner-level behaviour was ever exercised on OpenCode —
        # which is how OpenCode came to ship with no turn-end signal at all (see
        # tests/test_turn_end_detection.py).
        default_backend = runner_overrides.pop("backend_name", "claude")
        state_data = getattr(session.state, "data", None)
        if isinstance(state_data, dict) and not state_data.get("backend"):
            state_data["backend"] = default_backend

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
        if not stdin_is_interactive() or not stdout_is_interactive():
            raise RuntimeError("The interactive TUI requires an interactive terminal. Use --json for non-TTY use.")
        if not self._ensure_backend_available():
            return 1
        if not self.management_lock.acquire():
            # repo_root lets the refusal name the holder's MODE (background vs interactive) and how
            # to stop it; getattr keeps this a message-quality detail that can never break the refusal.
            print(already_running_message(self.management_lock.owner_pid(), repo_root=getattr(self.repo, "repo", None)))
            return 1
        # Privacy warning LAST in the cooked-mode startup — after the backend
        # install/availability gate above, after the second-instance lock check (a refused
        # instance is never asked to acknowledge), and after cli.py's gh-login and menu-key
        # prompts — so it's the final thing the user acknowledges right before the full-screen
        # TUI takes over, not buried above those configuration steps.
        from agitrack.cli import _acknowledge_privacy_warning

        if not _acknowledge_privacy_warning(skip=self._skip_privacy_ack):
            self.management_lock.release()
            return 1
        self.state.save()
        # Record this interactive session's mode so `agitrack --status` can report it (cleared on
        # teardown). The shared repo lock only carries a pid, not the mode.
        from agitrack.proxy.background import write_background_mode, write_proxy_status

        # ALSO register in the global registry, so `agitrack --daemons` can answer "what is
        # aGiTrack running?" with the whole truth. Listed, never stoppable (see daemons.py).
        try:
            from agitrack import daemons

            daemons.register("session", self.base_repo.repo)
        except Exception as error:
            self._debug(f"session registry entry failed: {error!r}")
        # ...and in the user-wide repo list the dashboard's switcher offers. Tracking a repo is
        # what makes it worth listing, so it is recorded here rather than left to whoever opens a
        # dashboard: reachable only through `ensure_hub_for`, it was missed by every start that
        # opens no dashboard (scripted, no TTY, or `open_dashboard_on_start` off). Best-effort.
        try:
            from agitrack import repos as repo_registry

            repo_registry.remember(self.base_repo.repo)
        except Exception as error:
            self._debug(f"remembering repo for the dashboard switcher failed: {error!r}")
        write_proxy_status(
            self.base_repo, commits="manual" if self._manual_commits else "auto", worktree=self._use_worktrees
        )
        # Record the commit mode for a later auto-start on commit, so it resumes the mode the user
        # LAST chose — not the last one a *background* run happened to use. Without this, switching
        # to interactive auto and then committing would silently auto-start a manual-commit daemon.
        write_background_mode(self.base_repo, manual=self._manual_commits)
        # Close any stretch a `agitrack stop` had switched tracking off for. The startup baseline
        # below already excludes the RESUMED conversation's earlier turns, but only that one: this
        # is what covers a conversation the user started (or switched to) while aGiTrack was off.
        # Recorded against the base repo, so a worktree session reads the same floor.
        try:
            from agitrack import tracking_gap

            if tracking_gap.resume_tracking(self.base_repo.repo) is not None:
                self._debug("resumed tracking after a stop; turns from the gap stay out of commits")
        except Exception as error:  # never let a gap record keep a session from starting
            self._debug(f"resuming after stop failed: {error!r}")
        # Keep git's comment char off '#' for this repo, or editing one of our commit messages
        # in an EDITOR (amend, rebase -i reword) silently deletes every '# ...' heading — the
        # whole trace/metadata structure. Repo-local, idempotent, never overrides a set value.
        self.base_repo.ensure_comment_char_preserves_headings()
        # Record this launch's CLI arguments so a later MSI self-update can re-launch with
        # the same flags (--repo / --backend / --no-worktree …) after replacing agitrack.exe.
        # Frozen-Windows only; a no-op everywhere else.
        self._write_msi_last_args()
        # Manual-commit mode never auto-prompts for a commit — the user commits when THEY
        # choose (the working tree stays dirty on purpose; turns are tracked latently).
        if not self._manual_commits and self.actions.has_pre_agent_user_changes():
            print("User changes detected before the agent starts.")
            # Skippable only WITHOUT worktrees. A worktree session is checked out from HEAD, so
            # anything left uncommitted is simply absent from the tree the agent works in — the
            # user would be editing one copy while the agent reads another, and the agent's
            # commits would then look like they reverted work the user never committed. Under
            # --no-worktree the agent edits this very tree, so uncommitted changes are still
            # right there and declining costs nothing.
            try:
                self.actions.create_user_commit(allow_skip=not self._use_worktrees)
            except UserCommitAborted:
                # The mandatory (worktree-mode) prompt has an exit now; take it cleanly rather
                # than launching into a worktree that is missing the user's uncommitted work.
                print(
                    "aGiTrack not started — the pre-agent commit was declined.\n"
                    "Commit or stash your changes, or run `agitrack --no-worktree` to let the "
                    "agent work in this tree as it is."
                )
                return 1
        # Every blocking question is answered by here (backend availability, the second-instance
        # lock, the privacy acknowledgment, and the pre-agent commit above; cli.py asked its gh
        # and menu-key questions before constructing us). Only now is it safe to open a browser.
        if self._on_startup_complete is not None:
            try:
                self._on_startup_complete()
            except Exception as error:  # never let a side errand stop the session starting
                self._debug(f"startup-complete hook failed: {error!r}")
        # Base-merge-only: run even the first session in a worktree so the base
        # branch is only advanced by integration, never edited by a live agent.
        self._announce_diagnostics()
        self._base_branch = self.base_repo.current_branch()
        self._warn_if_base_is_detached()
        self._integration.base_branch = self._base_branch
        # Cached branch checked out in the repo directory, refreshed by the drift
        # poll. The status bar bolds a session's integration branch when it differs
        # from this (e.g. after "keep integrating into X while the repo is on Y").
        self._repo_dir_branch = self._base_branch
        self._cleanup_stale_state_on_startup()
        self._reload_user_declined()  # files the pre-agent flow left intentionally unstaged
        self._setup_base_merge_only_session()
        self._apply_new_session_if_requested()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        # Stage the resume (retarget the backend's recorded working dir to THIS directory) BEFORE
        # the startup spawn. Crucially this is NOT gated on _should_continue_session: that gate
        # asks "does this session belong to this repo?" using the backend's recorded directory —
        # which is exactly what has gone stale (the repo was renamed, or worktrees were turned off
        # so the session that last ran in a now-removed worktree must move to the base repo). So
        # without retargeting first, our own recorded session is mistaken for a stranger and a
        # FRESH one starts in the wrong/old dir. Staging only ever moves OUR recorded session id
        # (set for this repo), so it's safe; after it, _spawn's gate sees the dir match and resumes.
        if self.state.backend_session_id and not self._force_new_session:
            self._resync_pin_to_the_live_conversation()
            self._stage_backend_resume(self.state.backend_session_id)
        self._init_screen()
        self._spawn()
        # ANSWER THE BACKEND FROM THE MOMENT IT EXISTS. It asks what background it is drawing on
        # within milliseconds of starting and gives up in well under a second, but everything
        # that could answer used to happen much later: a dozen startup steps, and then
        # `_detect_host_terminal` — which BLOCKS for its whole timeout on a terminal that never
        # replies at all (Apple Terminal implements no colour report). The question expired
        # unread on every launch, the backend fell back to its own default (dark), and the
        # session opened dark in a white terminal, only coming right seconds later once the
        # agent had been told the truth some other way. A platform-derived background needs no
        # round trip, so it is known HERE — before anything else runs — and the responder starts
        # at once. `_detect_host_terminal` still refines it: a real reply always wins.
        self._seed_derived_host_background()
        self._start_early_capability_service()
        self._start_file_watcher()
        # Identify the reactor thread and open the self-pipe the git worker writes to
        # so it can wake `select` on demand (e.g. to present a dialog) instead of
        # waiting for the next poll tick. Then start the git worker itself.
        self._main_thread_ident = threading.get_ident()
        # Host I/O via the platform layer (#118): the waker (self-pipe on POSIX, socketpair
        # on Windows) the git worker uses to break the reactor out of select, and the host
        # terminal (termios fd 0 on POSIX, Win32 console + a stdin bridge socket on Windows).
        self._waker = make_waker()
        self._wake_r = self._waker.wake_fileno()
        self._host = make_host_terminal(self)
        self._start_git_worker()
        # Register the initial session as the sole (active) entry in the
        # multiplexer. Additional sessions are appended by `_new_session`.
        self.sessions = [self.active]
        self._reconcile_sessions_on_startup()
        # Reclaim dangling shared-session snapshots from prior runs, in the
        # background so startup never waits on it (issue #55).
        threading.Thread(target=lambda: self._sweep_orphan_shared_sessions(fetch=True), daemon=True).start()
        # Recommend installing / logging into gh when it's unavailable, since the
        # dashboard's committer identities and session sharing depend on it (#76).
        # Runs in the background so a slow `gh auth status` never blocks startup.
        # Skipped when cli.py already showed the blocking startup gh prompt (no double-nag).
        if not self._gh_prechecked:
            threading.Thread(target=self._notify_if_gh_unavailable, daemon=True).start()
        # Resolve the GitHub login now, off the hot path, so neither a live nor an
        # exit-time auto-share has to shell out to `gh` mid-share (that lookup can
        # stall and would otherwise eat into the bounded share budget).
        threading.Thread(target=self._warm_share_login, daemon=True).start()
        try:
            self._enter_host_screen()
            self._set_raw()  # POSIX saves old_attrs here; Windows enters console raw mode
            self._detect_host_terminal()
            # The host terminal's own answers are known now, so the backend's copies of those
            # questions can be answered — and they must be answered NOW, not when the reactor
            # finally reads the child: the backend asked in its first milliseconds and gives up
            # in well under a second (see _start_early_capability_service).
            self._start_early_capability_service()
            # Install the canvas the `agent_background` setting asks for BEFORE the first
            # paint, so a forced dark/light session opens in it rather than switching over a
            # frame later. Under the default this is a no-op and the terminal's own colours
            # stand, which is exactly what should happen.
            self._apply_agent_background()
            self._resize_child()
            # SIGWINCH/SIGHUP don't exist on native Windows — there the host terminal's
            # resize watcher feeds _resize_child via consume_resize_pending() in the select
            # phase, and console-close is handled by the platform console-control handler.
            # While the TUI owns the screen, a background thread that dies must not print its
            # traceback: stderr IS the alternate screen, so an unhandled worker exception
            # scribbles over the session and leaves the display out of step with what
            # aGiTrack thinks is on it. Record it instead; the local guards handle the known
            # workers, this catches every other thread, now and later.
            self._prev_thread_excepthook = threading.excepthook
            threading.excepthook = self._thread_excepthook
            if sys.platform != "win32":
                self.original_sigwinch = signal.getsignal(signal.SIGWINCH)
                self.original_signal_handlers = {
                    signal.SIGTERM: signal.getsignal(signal.SIGTERM),
                    signal.SIGHUP: signal.getsignal(signal.SIGHUP),
                }
                signal.signal(signal.SIGWINCH, lambda _signum, _frame: self._resize_child())
                signal.signal(signal.SIGTERM, self._handle_exit_signal)
                signal.signal(signal.SIGHUP, self._handle_exit_signal)
            self._setup_worktree_confinement_notice()
            # Start from a clean hook slate. A PRIOR run that crashed (no teardown) can leave the
            # OTHER mode's managed hooks in the base repo's .git/hooks — and modes can switch
            # between runs (worktree ↔ no-worktree/manual). A stale manual fold-hook would rewrite
            # a worktree run's commits; a stale base-commit guard would block a no-worktree run's
            # commits. Both removals only touch aGiTrack's OWN marked hooks (restoring any chained
            # user hook) and are no-ops when absent, so clearing then re-installing exactly what
            # THIS mode wants makes every mode-switch safe regardless of how the last run ended.
            self._reset_hook_slate()
            # Startup is done, so the reactor becomes the child's sole reader again: join the
            # early responder and give the screen everything it read while it was answering.
            self._stop_early_capability_service()
            # Paint once before entering the loop. Every other frame is driven by backend output
            # or a keystroke, so until the backend emits its first byte there was nothing on
            # screen at all — and if that first byte was slow, the terminal sat showing whatever
            # was there before until some unrelated event (a mouse click) forced a repaint.
            self._render()
            exit_code = self._loop()  # the timers phase auto-applies a sandbox-blocked backend update
        except Exception as error:
            # The reactor died. The teardown below is about to switch the terminal back out
            # of the alternate screen, taking the traceback with it, so capture it to a file
            # FIRST and tell the user where it is once they have their terminal back.
            from agitrack.proxy.crash import crash_message, write_crash_report

            exit_code = 1
            path = write_crash_report(
                self._crash_report_root(),
                error,
                context={
                    "terminal": f"{self.rows}x{self.cols}",
                    "backend": getattr(self.backend, "name", "?"),
                    "session": self._session_label(),
                    "scrollback": self.scroll_back,
                    "history": self._history_len(),
                    "worst_reactor_stall": f"{self._stall_worst:.1f}s",
                },
            )
            self._crash_notice = crash_message(path, error)
        finally:
            # Idempotent: a crash between the spawn and the reactor must not leave the early
            # responder reading the child's fd behind everyone's back.
            self._stop_early_capability_service()
            if self._prev_thread_excepthook is not None:
                threading.excepthook = self._prev_thread_excepthook
                self._prev_thread_excepthook = None
            if self.original_sigwinch is not None and hasattr(signal, "SIGWINCH"):
                signal.signal(signal.SIGWINCH, self.original_sigwinch)
            for signum, handler in self.original_signal_handlers.items():
                signal.signal(signum, handler)
            self._stop_git_worker()  # join before tearing the worktree/PTY down
            self._stop_file_watcher()
            # Deliberately NOT stopping a Ctrl-G dashboard here: it is terminal-owned and
            # keeps serving after aGiTrack quits (stops with the terminal or `-d stop`).
            self._remove_base_commit_guard()  # uninstall the pre-commit guard + restore any chained hook
            self._teardown_manual_commit_mode()  # uninstall the manual-commit hooks + restore any chained
            self._restore_background_autostart()  # put back the session-start hooks this run displaced
            self._cleanup_child()
            self._restore_terminal()
            if self._host is not None:
                self._host.stop()  # stop the Windows stdin/resize threads; no-op on POSIX
                self._host = None
            if self._waker is not None:
                self._waker.close()
                self._waker = None
            self._wake_r = self._wake_w = -1
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                # Nulling routes through the platform child: a no-op extra on POSIX (the fd
                # is closed above), but on Windows the setter closes the ConPTY bridge socket
                # (os.close can't close a socket fd there), so it isn't leaked.
                self.master_fd = None
            try:
                from agitrack.proxy.background import clear_proxy_status

                clear_proxy_status(self.base_repo)
                try:
                    from agitrack import daemons

                    daemons.deregister()
                except Exception:
                    pass
            except Exception:
                pass
            self.management_lock.release()
            # The host terminal closed mid-work and the user chose "Reopen" in the
            # forced-exit dialog: launch a fresh window now that the lock is free so
            # the new instance can acquire it and auto-resume the session.
            if self._reopen_after_exit:
                self._reopen_in_new_window()
        # The backend kept exiting on launch and aGiTrack stopped relaunching: the
        # terminal is restored now, so surface why on the normal screen rather than
        # exiting with just a flash (#114).
        if self._backend_exit_notice:
            print(self._backend_exit_notice)
        # A self-update was applied; re-exec aGiTrack in place now that the terminal
        # is restored and the management lock is released. Does not return.
        if self._crash_notice:
            # The terminal is the user's again (the finally above restored it), so this is
            # the first thing they can actually read.
            print(self._crash_notice, file=sys.stderr)
        if self._pending_restart:
            # Finalize may have removed the worktree this process was launched in; the
            # re-exec'd aGiTrack would then start with a deleted CWD and crash before it
            # could discover the repo. Move to the base repo root (which always exists)
            # first so the restart lands cleanly.
            try:
                os.getcwd()
            except OSError:
                try:
                    os.chdir(self.base_repo.repo)
                except OSError:
                    pass
            # An MSI self-update can't re-exec in place — the new MSI replaces the very
            # agitrack.exe we'd re-run. Hand off to the elevated installer instead; it
            # re-launches the updated build itself. If the hand-off can't start (e.g. the
            # user declined the UAC prompt), fall through to a normal re-exec so the user
            # keeps running the current version.
            if self._pending_msi_handoff and self._launch_msi_bootstrapper():
                return exit_code
            # A Windows pip self-update likewise can't re-exec in place: the deferred upgrade
            # only runs once we exit and release the lock on agitrack.exe. Hand off to the
            # detached helper, which upgrades and relaunches the updated build itself. If it
            # can't be spawned, fall through to a normal re-exec of the current version.
            if (
                self._pending_pip_handoff
                and self._updater is not None
                and self._updater.launch_pip_bootstrapper(["--skip-privacy-ack"])
            ):
                return exit_code
            # This restart follows an in-app (menu) update; the user already saw
            # and acknowledged the privacy warning when this session started, so
            # don't make them acknowledge it again. (A startup-time update re-execs
            # via the CLI without this flag, so that path still shows the warning.)
            restart_agitrack(["--skip-privacy-ack"])
        return exit_code

    def _ensure_backend_available(self) -> bool:
        # A user-supplied launch command (--backend-command / config backend_command)
        # replaces the backend's own binary — a wrapper that ultimately execs the agent —
        # so the default backend CLI need not be installed on PATH. Skip the install gate
        # in that case (it would otherwise block a perfectly valid wrapped setup).
        if self._launch_command():
            return True
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
        fork = self._fork_next_spawn and resume
        self._fork_next_spawn = False
        if resume:
            # --fork-session resumes this conversation but mints a NEW id (the original is left
            # to its running background agent); a plain resume keeps the id it is given.
            session_id = self.state.backend_session_id
        else:
            session_id = self.backend.new_session_id()
            if session_id:
                # The backend lets aGiTrack choose the id (Claude), so it is pinned already.
                self.state.backend_session_id = session_id
        # Snapshot the directory's sessions AND how recently each was written, in EVERY case.
        # `_discover_spawned_session` uses it to tell "the user switched conversations inside the
        # backend" from "an unrelated conversation happens to live in this directory", and the
        # mtime is what makes BOTH kinds of switch visible:
        #   • to a NEW conversation — Claude `/clear`, OpenCode `/new`: an id that is not in the
        #     snapshot at all (verified against the real Claude CLI: `/clear` writes a brand-new
        #     transcript under a new id the moment it runs, before the next prompt).
        #   • BACK to an EXISTING one — Claude `/resume`, OpenCode's session switcher: the id IS
        #     in the snapshot, so an id-only snapshot could never see it. A mtime that has moved
        #     since launch can, and a conversation nobody has touched still cannot.
        # Taking it unconditionally is also strictly SAFER than skipping it, which is what used to
        # happen in the two most ordinary cases — Claude pins its id up front, and every relaunch
        # is a resume. With no snapshot, discovery returns None and the parse falls back to the
        # pinned id forever, so everything after a `/clear` was recorded against the conversation
        # the user had just thrown away: no prompts, no tokens, no trace.
        self._pre_spawn_sessions = {ref.id: ref.updated for ref in self.backend.list_sessions(self.repo.repo)}
        command = self.backend.spawn_command(
            self.repo.repo,
            session_id=session_id,
            resume=resume,
            fork=fork,
            commit_guidance=self._commit_guidance,
            use_worktrees=self._use_worktrees,
            # In worktree mode self.repo.repo is the worktree; naming the base repo lets the
            # agent note spell out both absolute paths so the agent never mistakes the base
            # checkout for its worktree (and strands edits that are never committed).
            base_repo=self.base_repo.repo,
            executable=self._launch_command() or None,
        )
        # Forward any backend-specific args the user passed through aGiTrack (#32),
        # before the sandbox wrapper so they reach the backend, not sandbox-exec.
        command = command + getattr(self, "_backend_args", [])
        command = self._confine_to_worktree(command)
        # Remember the exact command we tried to launch so a backend that dies on startup
        # can report it (the #1 question when "backend exited repeatedly" — did aGiTrack even
        # find/launch the right binary?).
        self._last_spawn_command = list(command)
        # Pseudo-terminal mechanics delegated to the platform child process (POSIX PTY
        # via BackendProcess, or Windows ConPTY via NtChildProcess); policy (command
        # construction, sandbox wrapping) stays here in the runner. The session owns its
        # child process; child_pid / master_fd remain readable on the runner via the
        # Session-delegating compat properties.
        self.active.process = make_child_process(command, str(self.repo.repo), extra_env=self._backend_child_env())
        # Re-arm the cwd-drift check for this launch, and remember when it started:
        # only turns recorded at/after this time count, so a stale cwd left in the
        # transcript before this launch can't trigger a false drift warning (#72).
        self._cwd_drift_checked = False
        self._cwd_check_at = 0.0
        self._cwd_launch_at = time.time()

    def _launch_command(self) -> list[str]:
        # The command that launches the current backend, replacing its executable with a
        # user wrapper (e.g. ["somewrapper", "claude"]). A per-run --backend-command
        # override wins; otherwise the per-backend config value applies. Resolved per
        # spawn because the active backend can change mid-session (menu switch / new
        # session with another backend), so the right wrapper follows the backend. Empty
        # ⇒ launch the backend binary directly.
        if self._backend_command:
            return list(self._backend_command)
        config = self.global_config
        if config is None:
            return []
        getter = getattr(config, "backend_command", None)
        return list(getter(self.backend.name)) if callable(getter) else []

    def _notify_if_gh_unavailable(self) -> None:
        # Recommend installing / authenticating gh when it isn't usable, so the
        # user knows why features that depend on it (the dashboard's committer
        # identities, session sharing) are limited (#76). Best-effort and one-shot;
        # runs off the startup thread so a slow auth check never blocks the UI.
        try:
            from agitrack.metrics.github import gh_status

            status = gh_status()
        except Exception as error:
            self._debug(f"gh availability check failed: {error!r}")
            return
        message = self._gh_unavailable_hint(status)
        if message is None:
            return  # gh is installed and authenticated — nothing to suggest
        self._set_message(message, seconds=12.0)
        self._render()

    def _warm_share_login(self) -> None:
        # Populate the GitHub-login cache at startup so auto-share never resolves it
        # mid-share. Only when sharing is actually reachable (backend supports it and
        # a remote exists), to avoid a pointless `gh` call on solo/offline repos.
        try:
            if not getattr(self.backend, "supports_session_sharing", False):
                return
            if self.global_config.github_login or not self.base_repo.remote_exists():
                return
            self._cached_or_resolve_login()
        except Exception as error:
            self._debug(f"warm share login failed: {error!r}")

    @staticmethod
    def _gh_unavailable_hint(status: str) -> str | None:
        if status == "missing":
            return (
                "GitHub CLI (gh) isn't installed. aGiTrack features that rely on it are limited —\n"
                "the dashboard can't resolve committer GitHub identities, and session sharing\n"
                "is unavailable. Install it from https://cli.github.com then run `gh auth login`."
            )
        if status == "unauthenticated":
            return (
                "GitHub CLI (gh) isn't logged in. aGiTrack features that rely on it are limited —\n"
                "the dashboard can't resolve committer GitHub identities, and session sharing\n"
                "is unavailable. Run `gh auth login` to enable them."
            )
        return None

    def _setup_worktree_confinement_notice(self) -> None:
        # When confinement is requested but the platform can't enforce it (no
        # sandbox), silently watch the base repo so we can warn *if and when* the
        # agent actually writes into it (see _warn_if_base_edited). We no longer show
        # a proactive "can't sandbox" notice at startup — it confused users by
        # flagging a capability limitation even when nothing went wrong.
        self._monitor_base_edits = False
        if self.worktree is None:
            return
        if not (self._sandbox and sandbox.is_enabled()):
            return  # user disabled confinement
        if sandbox.is_available():
            return  # the sandbox enforces it; nothing to monitor
        self._monitor_base_edits = True
        self._base_check_at = 0.0
        self._base_warned_files = set()
        try:
            self._base_status_baseline: set[str] = set(self.base_repo.status_short().splitlines())
        except Exception:
            self._base_status_baseline = set()

    def _base_commit_guard_active(self) -> bool:
        """Whether to enforce "no agent commits to the base repo" via the pre-commit guard hook.

        Active exactly when confinement is wanted in worktree mode but the OS sandbox can't
        enforce it — i.e. on any platform where ``sandbox.is_available()`` is False (Windows, or
        POSIX without bwrap/seatbelt). On POSIX with a working sandbox the base repo is already
        read-only to the agent, so the hook would be redundant. Turned off by disabling
        confinement (``--no-sandbox`` / ``sandbox: false`` / ``AGITRACK_SANDBOX=0``)."""
        return bool(
            self._use_worktrees
            and self.worktree is not None
            and self._sandbox
            and sandbox.is_enabled()
            and not sandbox.is_available()
        )

    def _reset_hook_slate(self) -> None:
        """Start this run from a clean hook slate: clear whatever a previous run left in the base
        repo's ``.git/hooks``, then install exactly what THIS mode wants.

        A prior run that crashed (no teardown) leaves the OTHER mode's managed hooks behind, and
        modes switch between runs (worktree ↔ no-worktree/manual). A stale manual fold-hook would
        rewrite a worktree run's commits; a stale base-commit guard would block a no-worktree
        run's commits. Both removals only touch aGiTrack's OWN marked hooks (restoring any chained
        user hook) and are no-ops when absent, so clear-then-install makes every mode switch safe
        regardless of how the last run ended.

        A method rather than four inline calls in :meth:`run` because the mode-switch tests need
        to perform exactly this sequence: as a copy in the test file it silently drifted from the
        original — the copy stayed green while the real startup changed, which is the failure
        mode the whole composition harness exists to prevent.
        """
        self._remove_base_commit_guard()
        self._teardown_manual_commit_mode()
        self._install_base_commit_guard()  # hard-stop agent commits to base when no OS sandbox
        self._setup_manual_commit_mode()  # --manual-commits: latent-commit hooks + trailer files
        self._displace_background_autostart()  # this session is the single writer; see below

    def _install_base_commit_guard(self) -> None:
        # Install the pre-commit guard in the base repo when active. Skipped (with a note) when a
        # custom core.hooksPath is configured, since our hook in the default dir wouldn't run and
        # overriding the user's hooksPath would disable their own hooks.
        if not self._base_commit_guard_active():
            return
        try:
            if self.base_repo.core_hooks_path():
                self._debug("base-commit guard skipped: core.hooksPath is set")
                return
            # A previous no-worktree/background run leaves the PERSISTENT auto-track hook as
            # `pre-commit`, chaining any project hook via `pre-commit.agitrack-orig`. The guard
            # wants that same slot pair, and only ONE aGiTrack hook can occupy it — with the
            # backup slot already taken by the project hook, installing the guard on top would
            # overwrite (and permanently lose) the auto-track hook. So step it aside cleanly
            # first (restoring the project hook), and put it back in `_remove_base_commit_guard`.
            # Nothing is lost meanwhile: the auto-track hook defers to a live tracker anyway, and
            # this session IS one.
            hooks_dir = self.base_repo.hooks_dir()
            if git_hooks.is_autotrack_hook(hooks_dir / "pre-commit"):
                git_hooks.remove_autotrack_precommit_hook(hooks_dir, debug=self._debug)
                self._displaced_autotrack_hook = True
            git_hooks.install_base_commit_guard(hooks_dir, debug=self._debug)
        except Exception as error:  # never let hook setup block startup
            self._debug(f"base-commit guard install failed: {error!r}")

    def _remove_base_commit_guard(self) -> None:
        try:
            if self.base_repo is not None and not self.base_repo.core_hooks_path():
                git_hooks.remove_base_commit_guard(self.base_repo.hooks_dir(), debug=self._debug)
                if self._displaced_autotrack_hook:
                    # Restore the persistent auto-track hook this worktree run stepped aside
                    # (see `_install_base_commit_guard`) — it must OUTLIVE aGiTrack, so a worktree
                    # session must not be what silently uninstalls it.
                    self._displaced_autotrack_hook = False
                    self._install_autotrack_precommit_hook()
        except Exception as error:
            self._debug(f"base-commit guard removal failed: {error!r}")

    def _displace_background_autostart(self) -> None:
        """Take BACKGROUND mode's start-on-session-open hooks out for the length of this run.

        Those hooks exist to start a background daemon when an agent opens a session in this
        repo — and this run IS an agent session in this repo, spawned by aGiTrack itself. Left
        installed, the backend would fire them at our own child and try to start a daemon beside
        the TUI, which is the double-writer aGiTrack never allows. The fire-time lock probe would
        refuse it anyway, but relying on a race to prevent something we can simply not arm is the
        wrong way round: interactive mode owns the repository while it runs, so it holds the
        hooks too.

        Records exactly which backends were armed, so :meth:`_restore_background_autostart` can
        put back what was there and nothing else — a repo where auto-start was never enabled must
        not come out of an interactive session with it enabled."""
        try:
            from agitrack.backends import agent_hooks

            if self.base_repo is None:
                return
            armed = agent_hooks.installed_autostart_backends(self.base_repo.repo)
            if not armed:
                return
            agent_hooks.remove_agent_autostart(self.base_repo.repo, debug=self._debug)
            self._displaced_agent_autostart = armed
            self._debug(f"session-start autostart hooks displaced for this run: {armed}")
        except Exception as error:  # never let hook setup block startup
            self._debug(f"session-start autostart displace failed: {error!r}")

    def _restore_background_autostart(self) -> None:
        """Put back what :meth:`_displace_background_autostart` took, and only that.

        Symmetrical with the auto-track pre-commit hook's displace/restore: these hooks must
        OUTLIVE aGiTrack, so an interactive session must not be what silently uninstalls them."""
        armed, self._displaced_agent_autostart = self._displaced_agent_autostart, []
        if not armed or self.base_repo is None:
            return
        try:
            from agitrack.backends import agent_hooks

            for backend in armed:
                agent_hooks.install_agent_autostart(self.base_repo.repo, backend, debug=self._debug)
            self._debug(f"session-start autostart hooks restored: {armed}")
        except Exception as error:
            self._debug(f"session-start autostart restore failed: {error!r}")

    def _autotrack_hook_will_survive_exit(self) -> bool:
        """Whether a `git commit` made AFTER aGiTrack exits will still fold in the tracking.

        That is the persistent auto-track ``pre-commit`` hook's whole job, and it outlives the
        session — so in the ordinary case the user can exit with turns pending and lose nothing.
        It is absent in exactly two situations: a custom ``core.hooksPath`` (git ignores the hooks
        dir we install into) and the ``autotrack_hook: off`` opt-out."""
        try:
            if self.base_repo is None or self.base_repo.core_hooks_path():
                return False
            if getattr(self.global_config, "autotrack_hook", "auto") == "off":
                return False
            # ...and it is absent in a third: this session INSTALLED it into a repo that did not
            # have it, so teardown gives the slot back (see `_teardown_manual_commit_mode`).
            # Asking while the hook is sitting right there would answer "yes, your pending turns
            # will fold into your next commit" about a hook that is about to be removed.
            if self._autotrack_hook_preexisting is False:
                return False
            return git_hooks.is_autotrack_hook(self.base_repo.hooks_dir() / "pre-commit")
        except Exception as error:
            self._debug(f"autotrack hook check failed: {error!r}")
            return False

    def _install_autotrack_precommit_hook(self) -> None:
        """Install (or refresh) the PERSISTENT auto-track ``pre-commit`` hook, honoring the
        repo's ``autotrack_hook`` opt-out. Shared by no-worktree startup and the worktree
        guard's restore path."""
        try:
            hooks_path = self.base_repo.core_hooks_path()
            if hooks_path:
                # Say it once, on the pre-TUI console. The skip was `_debug`-only, so the string
                # "core.hooksPath" never reached the user — and the status line then asserted
                # the opposite. See BackgroundRunner._install_autotrack_hook.
                if not self._hooks_path_notice_shown:
                    self._hooks_path_notice_shown = True
                    print(
                        f"aGiTrack: this repo sets core.hooksPath ({hooks_path}), so git ignores "
                        "the hooks directory aGiTrack installs into.\n"
                        "  Tracking works normally while aGiTrack runs; commits made after it "
                        "exits are NOT tracked.\n"
                        "  Your own hooks are left exactly as they are."
                    )
                return
            hooks_dir = self.base_repo.hooks_dir()
            if getattr(self.global_config, "autotrack_hook", "auto") == "off":
                git_hooks.remove_autotrack_precommit_hook(hooks_dir, debug=self._debug)
                return
            from agitrack import __version__
            from agitrack.proc import agitrack_invocation

            git_hooks.install_autotrack_precommit_hook(
                hooks_dir,
                invoke=agitrack_invocation(),
                repo_root=str(self.base_repo.repo),
                version=__version__,
                debug=self._debug,
            )
        except Exception as error:
            self._debug(f"autotrack hook install failed: {error!r}")

    def _backend_child_env(self) -> dict[str, str] | None:
        """Extra environment for the interactive backend child only. When aGiTrack will apply
        this backend's update itself (it's a brew-managed backend on macOS and update checks are
        on), disable OpenCode's in-app auto-update so the user isn't shown its prompt — which is
        redundant with aGiTrack's silent update and, under the sandbox, fails outright. Does NOT
        affect aGiTrack's own `opencode upgrade`, which runs from the unconfined proxy with the
        normal environment."""
        env: dict[str, str] = {}
        checks_on = self.global_config is None or getattr(self.global_config, "check_for_updates", True)
        if getattr(self.backend, "name", None) == "opencode" and checks_on and self._backend_update_via_agitrack():
            env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        # Mark the agent's process tree so the base-repo pre-commit guard (agitrack/git/hooks.py)
        # can tell THIS agent's commits from the user's. Set only when the guard is active (see
        # _base_commit_guard_active). Any git the agent runs inherits this; the user's own shell
        # and agents run outside aGiTrack don't have it, so they commit freely.
        if self._base_commit_guard_active():
            env[git_hooks.ENV_GUARD] = "1"
        if os.name == "nt":
            # On Windows aGiTrack skips host-terminal capability detection (the console doesn't
            # answer the OSC/DA color queries the POSIX path uses), so a backend that probes the
            # terminal for color support sees none and renders greyscale (observed with Claude;
            # OpenCode forces color itself). aGiTrack DOES render 24-bit color to the host (VT
            # processing is on — OpenCode's colors prove the pipeline), so tell the child the
            # terminal is truecolor-capable. Scoped to Windows; POSIX keeps real detection.
            env.setdefault("COLORTERM", "truecolor")
            env.setdefault("FORCE_COLOR", "3")
        return env or None

    def _confine_to_worktree(self, command: list[str]) -> list[str]:
        # Wrap the backend so it can only write inside its session worktree (plus
        # the repo's .git), not the base repo it lives in. A no-op when there is
        # no worktree, or when confinement is disabled / unavailable (the loop
        # then warns if the base working tree is touched).
        if self.worktree is None:
            return command
        if not self._sandbox:
            return command
        base = self.base_repo
        if base is None:
            return command
        if sandbox.will_confine(base=str(base.repo), worktree=str(self.repo.repo)):
            command = self._relax_nested_backend_sandbox(command)
        return sandbox.wrap_command(
            command,
            base=str(base.repo),
            worktree=str(self.repo.repo),
            allowed_paths=self._allowed_edit_paths,
        )

    def _relax_nested_backend_sandbox(self, command: list[str]) -> list[str]:
        """Turn off Codex's OWN sandbox on macOS, where it cannot run inside aGiTrack's.

        Codex sandboxes each command it runs with ``sandbox-exec`` (seatbelt), and aGiTrack wraps
        the whole backend in ``sandbox-exec`` too. Seatbelt does not nest: the inner profile
        cannot be applied inside the outer one, so every filesystem probe Codex made failed.
        Observed live — Codex reported "The first filesystem probe hit a sandbox restriction",
        escalated to asking the user to approve each command by hand, and the turn produced no
        edit at all. Same nesting limit that already stops a Homebrew self-update running inside
        the agent sandbox.

        This is a genuine TRADE, not a free removal of a redundant layer. aGiTrack's profile is
        ``(allow default)`` with a deny on the base repo and the worktrees root (see
        :func:`sandbox.build_profile`), so it protects the REPOSITORY, not the rest of the disk;
        Codex's ``workspace-write`` is the stricter one *outside* the repo. Dropping it means the
        agent can write elsewhere on the machine (``$HOME``, ``/tmp``, other checkouts) as it can
        under the other two backends, which have no OS sandbox of their own. The alternative is a
        Codex session that cannot edit any file at all, so the trade is made deliberately and
        documented in README/AGENTS rather than quietly.

        Scoped as narrowly as the problem: macOS only (where the nesting actually fails —
        ``bwrap`` on Linux composes fine with Codex's Landlock/seccomp sandbox), Codex only, and
        only when aGiTrack's confinement is actually active. An unconfined run (``--no-sandbox``,
        or a platform with no sandbox) keeps Codex's own protection untouched.
        """
        if getattr(self.backend, "name", None) != "codex":
            return command
        if not sandbox._have_sandbox_exec():
            return command  # not seatbelt ⇒ the sandboxes compose; leave Codex's alone
        # `-c` overrides are Codex's own flags, so they must land on the codex command itself —
        # appending after the sandbox wrapper would pass them to sandbox-exec instead.
        return [*command, "-c", 'sandbox_mode="danger-full-access"']

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
        self.turn_awaiting_commit = False
        self.untracked_before_turn = frozenset()
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
        # (so wheel events are forwarded to it), Claude does not (so aGiTrack keeps
        # the wheel for scrollback). Without this reset, switching OpenCode→Claude
        # would leave child_mouse stuck True and break Claude's scrollback.
        self.child_mouse = False
        self.scroll_back = 0

    def _restart_agent(self, message: str) -> None:
        # Tear down the running TUI and relaunch it for the current backend and
        # session state, re-baselining so existing history is not re-committed.
        self._teardown_child()
        if os.name == "nt":
            # Return the HOST terminal to its clean fresh-launch baseline before the new backend
            # comes up. A fresh aGiTrack launch starts with no host modes set; a restart must
            # too, otherwise mouse reporting (and kitty/paste/etc.) that the OUTGOING backend
            # mirrored onto the host stays on — and if the incoming backend doesn't use it (e.g.
            # Claude after OpenCode) nothing ever turns it off, so host-emitted mouse codes leak
            # into the backend as literal text. The incoming backend re-establishes only its own
            # modes via _sync_terminal_modes as its output streams in. (Windows-only: the POSIX
            # restart paths are long-proven and the leak is native-Windows specific.)
            self._disable_host_terminal_modes()
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._resize_child()
        # Re-assert host mouse reporting for the new backend so wheel scrollback
        # keeps working regardless of what the previous backend left behind.
        self._enable_host_mouse()
        self._reassert_host_raw()  # a restart can knock the host console out of raw mode
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
        # Re-arm the automatic self-update check so it re-evaluates for the backend being
        # switched to (a sandbox-blocked update is then applied on switch too).
        self._backend_update_checked_for = None
        self._drop_model_pin_on_switch(name)
        if self.worktree is None:
            # A non-worktree session has nothing to multiplex; restart the single
            # backend in place (legacy behaviour).
            self._debug(f"_switch_backend: name={name} worktree=None -> _restart_agent (respawn in place)")
            resume_id = self.state.stored_backend_session(name)
            if not resume_id:
                # NOTHING TO RESUME MEANS A BRAND-NEW CONVERSATION, so it is named like one.
                # A different agent, a different transcript, no shared history with what is on
                # screen — inheriting the outgoing backend's session name silently left two
                # unrelated conversations wearing one label, which the resume list then could
                # not tell apart. Deliberately unlike a native `/clear`, which is this same
                # session continuing and keeps its name on purpose (see
                # `_service_native_session_switch`); a backend switch is not that.
                session_name = self._prompt_session_name(f"New {name} session", default=self._next_session_name())
                if session_name is None:
                    return  # backing out of the name backs out of the switch, as with worktrees
                self.name = session_name
                # Make it durable NOW, exactly as the startup naming flow does. The new backend
                # has not spawned, so there is no conversation id to key it to and it is stored
                # as PENDING. Without this the record kept the OUTGOING session's name pending —
                # observed live: named 'alpha', switched to codex as 'beta', and the state file
                # still read `pending_session_name: alpha`, so a crash before the first turn
                # would have linked the previous session's name to the new conversation.
                #
                # Written through `self.state`, NOT a second `AgitrackState` on the same path:
                # without a worktree they are the same file, and the state writes just below
                # (`remember_backend_session`, `backend`, …) save this object's older in-memory
                # copy straight over anything another instance wrote. The name vanished.
                self._record_startup_session_name(self.state, None, session_name)
            self._remember_repo_backend(name)
            self.state.remember_backend_session()
            self.state.backend = name
            self.backend = make_proxy_agent(name)
            self.state.backend_session_id = resume_id
            self.state.last_backend_message_id = None
            # A model belongs to its backend. Worktree sessions each load their own state file,
            # so only this shared-state branch can carry the previous backend's model id into the
            # new backend's commits (and into the model aGiTrack asks that CLI to run).
            self.state.model = (self.state.recall_session(name) or {}).get("model")
            self.state.clear_trace()
            self._restart_agent(f"Backend set to {name}")
            if resume_id:
                # Going BACK to this backend's conversation restores the name it was given, the
                # mirror of naming a fresh one above; without it the status bar kept the outgoing
                # backend's label while a different conversation ran underneath. Never ASK here —
                # a resumed conversation is by definition not new work.
                #
                # Read through `self.state`, not `_restore_or_ask_session_name`: that helper opens
                # its own `AgitrackState` on the repo root, which without a worktree is the file
                # `self.state` already has loaded. `save()` writes `data` wholesale, so the two
                # copies overwrite each other and the lookup misses names this session itself
                # wrote. (The same two-instance hazard exists wherever a second AgitrackState is
                # opened on a no-worktree session's own path — see `_persist_session_name`.)
                known = self.state.session_name_for(resume_id)
                if known:
                    self.name = known
            return
        # Keep the current backend's session running in the background and switch
        # to this backend's own session.
        index = self._live_session_for_backend(name)
        self._debug(f"_switch_backend: name={name} worktree={self.name} live_index={index}")
        if index is not None:
            self._debug("_switch_backend: -> _switch_active (resume live bg session)")
            self._remember_repo_backend(name)
            self._switch_active(index)
            return
        # Resume this backend's last conversation if we remember one (recreating
        # its worktree at the same path so the backend finds its transcript).
        record = self._recall_backend_session(name)
        if record and record.get("id"):
            self._debug(f"_switch_backend: -> _new_session resume (fresh spawn) id={record.get('id')}")
            self._remember_repo_backend(name)
            self._new_session(
                record.get("worktree") or self._next_session_name(), backend=name, resume_session_id=record["id"]
            )
            return
        # Otherwise start fresh, confirming the session name first.
        self._debug("_switch_backend: -> _new_session fresh (prompt name)")
        session_name = self._prompt_session_name(f"New {name} session", default=self._next_session_name())
        if session_name is None:
            return
        self._remember_repo_backend(name)
        self._new_session(session_name, backend=name)

    def _drop_model_pin_on_switch(self, name: str) -> None:
        """Remove a ``--model`` passthrough before switching to ``name``, and say so.

        Backend args are forwarded VERBATIM to whatever backend is spawned, including after a
        Ctrl-G switch. So launching with `--backend codex --model gpt-5.4-mini` and switching to
        claude handed claude a model id it does not have: the next turn died instantly, with the
        error buried inside the backend's own pane and no aGiTrack-level warning anywhere. The
        repo's own harness command hits this — 8 of 9 checks pass and the turn fails.

        Dropping it means the switched-to backend uses its own default, which is the only model
        that is certain to exist there. Stated out loud, because a pin the user typed on the
        command line silently disappearing would be its own surprise."""
        args = getattr(self, "_backend_args", [])
        if not args:
            return
        kept: list[str] = []
        dropped: list[str] = []
        skip_next = False
        for index, arg in enumerate(args):
            if skip_next:
                skip_next = False
                dropped.append(arg)
                continue
            if arg in ("--model", "-m"):
                dropped.append(arg)
                skip_next = index + 1 < len(args)
                continue
            if arg.startswith("--model="):
                dropped.append(arg)
                continue
            kept.append(arg)
        if not dropped:
            return
        self._backend_args = kept
        self._set_message(
            f"Dropped {' '.join(dropped)} — a model belongs to one backend, and {name} would "
            "reject it. Using its own default model.",
            seconds=8.0,
        )

    def _remember_repo_backend(self, name: str) -> None:
        """Record the switch for THIS repo only (repo-scoped), not globally.

        Switching backend in one repo — to try it, or because one repo suits it — must not
        change the user's default in every other repo. The repo overlay value drives this repo's
        backend on next launch.

        Called once per branch of ``_switch_backend``, AFTER that branch has committed to the
        switch, rather than once up front: both fresh-session branches ask for a name first and
        treat cancelling as cancelling the switch, and writing the default before the question
        left a repo permanently pointed at a backend it never actually switched to.
        """
        self.global_config.set("default_backend", name, scope="repo")

    def _persisted_session_names(self) -> set[str]:
        # Sanitized names recorded for past conversations — both the durable
        # repo-root record and the worktree directory names the backend still
        # knows. A new session must not reuse one of these: resuming that past
        # conversation later recreates its worktree at worktree_path(name), so a
        # collision would put two backends in one directory.
        return {_sanitize_name(name) for name in self._agitrack_named_sessions().values() if name}

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
        # A friendly random word (neutral/harmless), avoiding existing worktrees and
        # live sessions. A word is easier to remember than ``session-N`` and, since
        # sessions are tracked/shared as ``<github-id(s)>/<name>``, far less likely to
        # clash with another contributor's session names. The user can still rename it
        # or type their own at the new-session prompt.
        from agitrack.proxy.session_names import random_session_name

        return random_session_name(self._taken_session_names())

    def _session_name_taken(self, name: str) -> bool:
        return _sanitize_name(name) in self._taken_session_names()

    def _dedupe_session_name(self, base: str) -> str:
        # A collision-free local name derived from ``base`` (e.g. a shared
        # session's "<sharer>-<name>"), so the resume prompt's default can be
        # accepted as-is even when the same session was imported before.
        candidate = _sanitize_name(base)
        if not self._session_name_taken(candidate):
            return candidate
        suffix = 2
        while self._session_name_taken(f"{candidate}-{suffix}"):
            suffix += 1
        return f"{candidate}-{suffix}"

    def _prompt_session_name(self, title: str, *, default: str) -> str | None:
        # Ask for a session name, rejecting duplicates (a session and its worktree
        # are 1:1, so names must be unique). Returns the chosen name, or None on
        # cancel / empty input. The hint must match the MODE — under --no-worktree there is no
        # worktree to promise, and the sessions menu already says "shares this directory".
        #
        # The field label says what the name BUYS, not "name for the new session" again: the
        # popup already carries a title, and every caller's title says which session is being
        # named — so repeating it here read as the same sentence twice, in slightly different
        # words. Callers keep their titles free of "Name for…" for the same reason.
        prompt = "It gets its own git worktree. Name:" if self._use_worktrees else "It shares this directory. Name:"
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
            root = self._root_state()
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
        # FIRST WRITER PER BACKEND WINS during an exit. The exit finalize runs the session the
        # user quit in FIRST and every other session after it (see _finalize_pending_work), so
        # the first writer for a backend is its most recently active session — and later writers
        # are older ones that would otherwise clobber it. Last-wins left this record describing a
        # DIFFERENT session from `backend_session_id` (which is written only for the exit-resume
        # session), and startup then mixed the two: the conversation from one session and, via
        # the `prior_worktree` name fallback, the NAME of another. Observed live as a restart
        # coming back labelled with the previous session's name.
        try:
            # Inside the try with everything else: `self.state` is legitimately absent on some
            # teardown paths, and this method has always been best-effort about that.
            remembered = self._backends_remembered_on_exit
            backend_name = self.state.backend
            root = self._root_state()
            if not (self._exiting and backend_name in remembered):
                # Recorded only while exiting: a call made BEFORE the exit walk would otherwise
                # sit in the set and suppress the first — and correct — writer of the walk.
                if self._exiting:
                    remembered.add(backend_name)
                root.remember_session(
                    backend_name,
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

    def _adopt_pending_session_name(self, root_state, session_id: str | None) -> str | None:
        """Recover the name a previous run chose but was killed before it could link: the backend
        conversation now exists, so key the name to it and clear the pending record."""
        pending = root_state.pending_session_name if session_id else None
        if not pending:
            return None
        root_state.name_session(session_id, pending)
        root_state.remember_pending_session_name(None)
        self._debug(f"recovered pending session name '{pending}' for {session_id}")
        return pending

    def _record_startup_session_name(self, root_state, session_id: str | None, name: str) -> None:
        """Make a freshly chosen startup name durable IMMEDIATELY — at session creation, not on exit.

        Keyed by the conversation id when one already exists (a resume); otherwise stored as the
        pending name, because a brand-new conversation has no id until the backend creates it. Either
        way the name survives a non-graceful exit."""
        if not name or self._AUTO_NAME_RE.match(name):
            return
        try:
            if session_id:
                root_state.name_session(session_id, name)
            else:
                root_state.remember_pending_session_name(name)
        except Exception as error:
            self._debug(f"record startup session name failed: {error!r}")

    def _root_state(self) -> AgitrackState:
        """The durable repo-root state — this session's OWN object when they are the same file.

        WITHOUT A WORKTREE, ``self.state`` *is* the repo-root state file. ``AgitrackState.save()``
        writes its whole ``data`` dict (no merge), so a second instance opened on that same path
        is a second copy of one JSON document and whichever saves last silently discards the
        other's writes. That is not theoretical: a session name written through a second instance
        vanished when the session state saved a moment later, leaving ``session_names`` empty and
        the name unrecoverable — the resume list then showed the conversation unnamed.

        In WORKTREE mode the session state is a different file under the worktree, the two never
        collide, and a fresh instance reading the latest bytes from disk is exactly right.
        """
        root = Path(self.base_repo.repo)
        state = getattr(self, "state", None)
        if state is not None and getattr(state, "path", None) == root / ".agitrack" / "state.json":
            return state
        return AgitrackState(root, default_backend=getattr(self.global_config, "default_backend", None))

    def _announce_diagnostics(self) -> None:
        """Print WHERE the diagnostics go, before the TUI takes the screen.

        C13: `--verbose` produced ZERO extra on-screen output — the verbose and non-verbose
        transcripts differed only in the session name, and the status bar carried no marker.
        Everything went to `.agitrack/proxy-debug-<stamp>.log`, whose path was never printed
        anywhere, so the flag read as broken and the file it wrote was undiscoverable. It also
        records every keystroke and grows ~10 KB/min while idle, which is worth knowing before
        leaving it on."""
        if not self.debug_proxy:
            return
        try:
            path = self._diag_path("proxy-debug")
        except Exception:
            return
        print(f"aGiTrack: verbose diagnostics → {path}")
        if self.raw_capture:
            print(f"aGiTrack: raw I/O capture   → {self._diag_path('proxy-raw')}")
        print("  It records your keystrokes and grows steadily; delete it when you are done.")

    def _warn_if_base_is_detached(self) -> None:
        """Say so — loudly, once, before the agent starts — when the repo is on a detached HEAD.

        B8: a detached HEAD was never detected and never mentioned; the only trace anywhere in
        the UI was the status bar rendering "→ HEAD". Integration then FAST-FORWARDED the
        detached HEAD onto the aGiTrack commit without asking, leaving `main` untouched and the
        turn branch deleted — so the agent's work was reflog-only from the next
        `git checkout main` onward, and looked to the user like it had simply vanished.

        aGiTrack still works here (the commits are real, and `git switch -c` keeps them), so this
        warns rather than refuses: refusing would strand the legitimate detached-HEAD workflows
        (bisect, an inspected tag) that people do run agents inside."""
        try:
            if not self.base_repo.is_detached():
                return
            head = self.base_repo.rev_parse("HEAD")[:12]
        except Exception as error:
            self._debug(f"detached-HEAD check failed: {error!r}")
            return
        print(
            f"aGiTrack: this repository is on a DETACHED HEAD ({head}) — no branch is checked out.\n"
            "  Commits aGiTrack makes will be integrated onto that detached HEAD, so they belong\n"
            "  to no branch: the next `git checkout <branch>` leaves them reachable only from the\n"
            "  reflog, and `git gc` will eventually discard them.\n"
            f"  To keep this work: `git switch -c <name>` now, or `git branch <name> {head}` later."
        )

    def _persist_session_name(self, session_id: str | None, *, overwrite: bool = False) -> None:
        # Link this session's user-given name to its backend conversation id in
        # the durable repo-root record as soon as the id is known — and again
        # whenever the backend forks a new id — not only on clean exit. Waiting
        # for exit strands the name under a stale id (or never records it) when
        # the worktree is kept, aGiTrack crashes, or the conversation id drifts
        # across resumes, leaving the session unnamed in the resume list.
        #
        # FILL-ONLY unless ``overwrite``. This wrote self.name — the name of the session doing
        # the resuming — onto whatever conversation was now active, so RESUMING a past
        # conversation renamed it: alpha → hill → gamma → delta, and the list then showed two
        # unrelated conversations both called "hill" with the original names unrecoverable.
        # A rename is a deliberate act with its own command (`_rename_session`), and that is the
        # only caller that passes overwrite=True.
        name = self.name
        if not session_id or not name or self._AUTO_NAME_RE.match(name):
            return
        try:
            root = self._root_state()
            existing = root.session_name_for(session_id)
            if existing and not overwrite:
                return  # this conversation already has a name of its own; it is not ours to change
            if existing != name:
                root.name_session(session_id, name)
            if root.pending_session_name:
                root.remember_pending_session_name(None)  # now durably linked to the conversation
        except Exception as error:
            self._debug(f"persist session name failed: {error!r}")

    # --- live-session multiplexer ---

    def _worktrees(self) -> WorktreeManager:
        if self.worktree_manager is None:
            self.worktree_manager = WorktreeManager(self.base_repo)
        return self.worktree_manager

    def _apply_new_session_if_requested(self) -> None:
        # `agitrack --new-session`: start a fresh backend conversation (don't resume)
        # and mint a new aGiTrack session id.
        if not self._force_new_session:
            return
        self.state.backend_session_id = None
        self.state.last_backend_message_id = None
        self.state.new_agitrack_session_id()

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
            # A session that last ran in a worktree shouldn't be abandoned just because the
            # user switched off worktrees: adopt the most recent prior worktree session's
            # conversation as the resume target so it continues HERE in the base repo. run()'s
            # startup staging (_stage_backend_resume) retargets that transcript's recorded cwd
            # to the base directory, so it resumes in the right place. The leftover worktree
            # DIRECTORIES are kept — switching back to worktree mode later resumes them in place
            # (the user can delete them by hand if they want the space back).
            resume_id: str | None = None
            named = False
            if not self._force_new_session:
                # Pick the conversation to continue HERE: the latest worktree session, unless the
                # base repo's own recorded session is genuinely more recent (transcript recency,
                # not just "is a session already recorded"). Then RE-POINT it at this base
                # directory by re-assigning through the setter — even when the id is unchanged.
                # The id persisted from the worktree run still records the OLD worktree as its
                # repo (`backend_session_repo`), which makes the startup resume gate reject it and
                # _initialize_session_baseline reset to a fresh session BEFORE staging ever runs.
                # The setter updates backend_session_repo to the base repo so the resume is honored.
                chosen = self.state.backend_session_id
                newest = self._newest_worktree_session()
                if newest is not None and newest[0] != chosen and newest[1] >= self._session_updated_at(chosen):
                    chosen = newest[0]
                if chosen:
                    self.state.backend_session_id = chosen  # setter re-points backend_session_repo at base
                    resume_id = chosen
                    # Keep the session's NAME (the worktree it came from, e.g. "candle") rather than
                    # leaving the default "main" — so the status bar and the resume list stay
                    # consistent across worktree ⇄ no-worktree switches. Persist it under this id so
                    # switching back to worktree mode resolves the same name.
                    name = self._resolve_session_name(chosen) or self._adopt_pending_session_name(self.state, chosen)
                    if name and not self._AUTO_NAME_RE.match(name):
                        self.name = name
                        named = True
                        self._persist_session_name(chosen)
                    self._debug(f"--no-worktree: resuming session {chosen} as '{self.name}' in the base repo")
            if not named:
                # No name carried over — a brand-new directory, a fresh --new-session, or a
                # conversation that never got named. Ask, exactly as worktree mode does via
                # _resolve_startup_session_name, instead of silently labelling the session with the
                # "main" placeholder. Only reached from run(), which requires a TTY, so this never
                # blocks headless/background runs (those use BackgroundRunner, not this path).
                self.name = self._prompt_startup_name(resume_id is not None)
                # Durable straight away: keyed by the conversation when one exists, otherwise as the
                # pending name, so killing aGiTrack before the first turn doesn't lose it.
                self._record_startup_session_name(self.state, resume_id, self.name)
                if resume_id:
                    self._persist_session_name(resume_id)
            self._set_message(
                "Running without a worktree: the agent edits this branch directly (visible live), "
                "but there's no isolation or auto-integration. Extra sessions started this way share "
                "this directory and edit the same files at once.",
                seconds=12.0,
            )
            # Leftover worktree directories aren't used in --no-worktree mode, and their presence
            # can confuse a resumed agent into editing them instead of the base repo. Queue a
            # "delete them?" prompt for the loop to surface (we're pre-TUI here). Default is keep.
            try:
                root = self._worktrees().root
                self._pending_noworktree_cleanup = sorted(p.name for p in root.iterdir() if p.is_dir())
            except Exception as error:
                self._debug(f"no-worktree leftover scan failed: {error!r}")
                self._pending_noworktree_cleanup = []
            # Anchor the no-worktree cover scan so commits the agent makes itself from here get
            # aGiTrack metadata (#35), while pre-existing history is never touched.
            if self._noworktree_base_head is None:
                self._load_noworktree_anchor()
            return
        root_state = self.state  # the durable repo-root "last session" record
        backend_name = root_state.backend
        prior_message_id = root_state.last_backend_message_id
        prior_model = root_state.model
        prior_record = root_state.recall_session(backend_name) or {}
        # Which conversation to continue at startup: aGiTrack's own last session if we
        # have one, otherwise the repo's most recent backend conversation (e.g. one
        # you ran with plain claude/opencode before aGiTrack). Resume is by id, which
        # the backend resolves regardless of which directory it runs in.
        resume_id = None if self._force_new_session else self._startup_resume_id(root_state)
        # The worktree name is only a usable fallback NAME for `resume_id` when the record is
        # ABOUT that conversation. Two records feed this decision and they can disagree: the
        # resume pointer is written for the session the user quit in, while this per-backend
        # record was (before the first-writer-wins fix) whichever session finalized last. Naming
        # the resumed conversation after a worktree belonging to a DIFFERENT conversation is how
        # a restart came back showing the previous session's name with the new session inside it.
        prior_worktree = prior_record.get("worktree") if prior_record.get("id") == resume_id else None
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
        # Load this worktree's state and reconcile its merge branch BEFORE aligning: a
        # prior run's assignment must be honored (and the cross-branch guard compares the
        # align target against it), with a confirmation prompt deferred to the TUI.
        self.state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
        self._reconcile_merge_branch(self._base_branch)
        self._align_session_to_base(repo)
        self.turn = self._turn_from_branch(repo.current_branch())
        self.state.backend = backend_name
        # The worktree is recreated fresh each run (its working state is not kept),
        # so seed its resume pointer to continue the chosen conversation.
        if not self.state.backend_session_id and resume_id:
            if prior_model:
                self.state.model = prior_model
            self.state.last_backend_message_id = prior_message_id
            self.state.backend_session_id = resume_id  # setter records this worktree as its repo
        self.backend = make_proxy_agent(backend_name)
        # Heal a worktree an earlier aGiTrack left holding a conversation that has since moved to
        # a sibling (see _release_sibling_owned_conversations). `resume_id` is what the durable
        # record asks this run to continue, so it is kept whatever else is cleaned up.
        self._release_sibling_owned_conversations(keep=resume_id)
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)

    def _resolve_session_name(self, session_id: str | None) -> str | None:
        """A friendly name for a backend conversation id, tried in order: (1) the persisted
        session-name record, (2) the worktree DIRECTORY whose state recorded this id (its dir
        name), (3) the worktree the backend's conversation listing keys it under. Lets a
        ``--no-worktree`` resume of a worktree session keep that session's name (e.g. "candle")
        instead of falling back to the default "main", and keeps the name consistent across
        worktree ⇄ no-worktree switches even after the leftover worktree dir is deleted."""
        if not session_id:
            return None
        try:
            persisted = self._user_state().session_name_for(session_id)
            if persisted and not self._AUTO_NAME_RE.match(persisted):
                return persisted
        except Exception as error:
            self._debug(f"resolve-session-name persisted lookup failed: {error!r}")
        try:
            for info in self._worktrees().list():
                state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
                if state.backend_session_id == session_id:
                    return info.name
        except Exception as error:
            self._debug(f"resolve-session-name worktree-state scan failed: {error!r}")
        try:
            for key, ref in self.backend.list_worktree_sessions(self._worktrees().root):
                if ref.id == session_id and key:
                    return key
        except Exception as error:
            self._debug(f"resolve-session-name conversation scan failed: {error!r}")
        return None

    def _startup_resume_id(self, root_state) -> str | None:
        """The conversation a fresh launch should resume.

        Two records normally answer this, and a SIGKILL destroys both: the root resume pointer is
        written only by the graceful exit path (`_persist_last_session_record`), and the repo scan
        looks in the BASE project directory while a worktree session's conversation is keyed by
        the WORKTREE path. So a killed run came back as a brand-new randomly-named session with a
        fresh conversation while the real one sat in `.agitrack/worktrees/<name>` — no work lost,
        but the session's identity was. The worktree scan is the same question the `--no-worktree`
        start already asks, so ask it here too before giving up. Recovering the id also recovers
        the NAME: `_adopt_pending_session_name` needs a conversation to key the surviving
        `pending_session_name` to, and returns None without one."""
        resume_id = root_state.backend_session_id or self._repo_latest_session_id()
        if resume_id:
            return resume_id
        newest = self._newest_worktree_session()
        if newest is not None:
            self._debug(f"no resume pointer (crash?); continuing the newest worktree conversation {newest[0]}")
            return newest[0]
        return None

    def _newest_worktree_session(self) -> tuple[str, float] | None:
        """``(id, last-updated)`` of the most recently active worktree conversation that HAS
        CONTENT — the one a ``--no-worktree`` start should continue in the base repo. Ranked by
        transcript recency, so it follows the conversation the user actually last worked in (even
        one in a worktree since removed — the transcript is kept keyed by the worktree path).

        Candidate ids come from TWO sources, so a session is found even if one misses: the
        backend's worktree conversation listing (project dirs keyed by worktree path) AND the
        session id each existing worktree's own aGiTrack state recorded. They are ranked by
        CONTENT recency; a candidate with no readable content (recency 0) — e.g. the fresh EMPTY
        id a backend mints on resume/picker-open — is skipped, since adopting it makes
        baseline-init fall back via ``recover_nonempty_session`` to an unrelated OLDER session
        (the "an older session opened" bug). Returns None when no candidate has content."""
        candidates: dict[str, float] = {}  # session id -> file-mtime fallback for ranking
        try:
            for _key, ref in self.backend.list_worktree_sessions(self._worktrees().root):
                if getattr(ref, "label", None):  # has a real user prompt → real content
                    candidates[ref.id] = max(candidates.get(ref.id, 0.0), ref.updated)
        except Exception as error:
            self._debug(f"newest-worktree conversation scan failed: {error!r}")
        try:
            for info in self._worktrees().list():
                sid = AgitrackState(info.path, default_backend=self.global_config.default_backend).backend_session_id
                if sid:
                    candidates.setdefault(sid, 0.0)
        except Exception as error:
            self._debug(f"newest-worktree state scan failed: {error!r}")
        best: tuple[str, float] | None = None
        for sid, fallback in candidates.items():
            recency = self._session_recency(sid, fallback)
            if recency <= 0:
                continue  # empty / unreadable transcript — nothing real to resume
            if best is None or recency > best[1]:
                best = (sid, recency)
        if best is not None:
            self._debug(f"newest worktree session with content: {best[0]}")
        return best

    def _session_recency(self, session_id: str | None, fallback: float) -> float:
        """A session's last-activity time, preferring CONTENT recency (message timestamps) over
        the transcript's file mtime. aGiTrack rewrites a transcript when it stages / retargets a
        resume, bumping the FILE mtime without adding a message — so ranking by mtime can make an
        older session look newest after aGiTrack touches it (the self-perpetuating "older session
        keeps resuming" bug). Content timestamps don't move, so they rank by genuine user
        activity; ``fallback`` (the mtime) is used only when the backend can't read them."""
        fn = getattr(self.backend, "session_last_activity", None)
        if fn is not None and session_id:
            try:
                ts = fn(session_id)
                if ts is not None:
                    return float(ts)
            except Exception as error:
                self._debug(f"content-recency lookup failed for {session_id}: {error!r}")
        return fallback

    def _session_updated_at(self, session_id: str | None) -> float:
        """Recency of a recorded session id (0.0 when unknown), so a ``--no-worktree`` start
        overrides the base repo's last-session pointer only when a worktree session is genuinely
        more recent — never demoting a newer base-repo conversation to an older one. Uses CONTENT
        recency (see :meth:`_session_recency`) so an aGiTrack file-touch can't skew the call."""
        if not session_id:
            return 0.0
        content = self._session_recency(session_id, 0.0)
        if content:
            return content
        # Fall back to the file-mtime recency the session listings expose.
        try:
            refs = list(self.backend.list_sessions(self.base_repo.repo))
            refs += [ref for _key, ref in self.backend.list_worktree_sessions(self._worktrees().root)]
        except Exception as error:
            self._debug(f"session-recency lookup failed: {error!r}")
            return 0.0
        return max((ref.updated for ref in refs if ref.id == session_id), default=0.0)

    _AUTO_NAME_RE = re.compile(r"^session-\d+$")

    def _resync_pin_to_the_live_conversation(self) -> None:
        """Before resuming, re-point the pin at the conversation most recently written here.

        In-session discovery only runs when a turn completes, so a user who switches conversation
        inside the backend and QUITS before typing (`/clear`, then Ctrl-C) leaves the pin on the
        conversation they discarded. The next launch then resumes THAT one — the reported symptom:
        aGiTrack asked for a name for the new session and reopened the old one under it.

        This asks the same question worktree and background mode ask continuously (`what is the
        newest human conversation in this directory?`), just once, at startup. The trade-off is
        the same as theirs: a conversation the user drove here OUTSIDE aGiTrack, more recently
        than the pinned one, is adopted. That is the honest reading of "most recent work in this
        directory", and it is what those modes have always done — the pin only ever moves to a
        conversation `list_sessions` already vouched for as human-driven.
        """
        pinned = self.state.backend_session_id
        if not pinned:
            return
        try:
            listed = self.backend.list_sessions(self.repo.repo)
        except Exception as error:
            self._debug(f"pin resync skipped: {error!r}")
            return
        # Only conversations that HAVE CONTENT can be resumed into. Claude mints a fresh EMPTY
        # session id on every resume/picker action; it is newest by mtime and has nothing in it,
        # and adopting one drops the user into a blank session on the next start. A ref's label
        # is its first real user prompt, so `label` is None exactly when there is no real turn.
        # Ranked by CONTENT recency, not the file's mtime. aGiTrack touches transcripts itself —
        # staging a resume, mirroring to the base repo, retargeting a recorded cwd — so an
        # abandoned conversation can be the newest FILE here while carrying nothing newer than
        # the pinned one. Moving the pin onto it is what made a session come back running a
        # conversation the user had thrown away with `/clear`, under that session's name.
        refs = {ref.id: self._session_recency(ref.id, ref.updated) for ref in listed if ref.label or ref.id == pinned}
        if pinned not in refs:
            return  # not our directory's conversation (a worktree/rename case): leave staging to it
        newest = max(refs.items(), key=lambda item: item[1])
        if newest[0] == pinned or newest[1] <= refs[pinned]:
            return
        self._debug(f"pin resync: {pinned} -> {newest[0]} (newer conversation in this directory)")
        self.state.backend_session_id = newest[0]
        self._restore_or_ask_session_name(newest[0], quiet=True)

    # How often to ask the backend which conversation is live. One cheap directory listing;
    # slow enough to be free, fast enough that the status bar catches up within a breath.
    SESSION_WATCH_SECONDS = 2.0

    def _service_native_session_switch(self) -> None:
        """Follow a conversation switch the user made with the BACKEND's OWN commands, live.

        `/clear`, `/resume`, OpenCode's `/new` and its session picker all change which
        conversation is being written, and until now aGiTrack only noticed at the end of a turn
        (or not at all). The status bar kept showing the previous name and id, so the one place
        the user checks was actively wrong about what they were talking to.

        Adopts only a conversation that HAS CONTENT: an empty transcript is not evidence of a
        switch (Claude mints one on every resume/picker action), and it is also the moment a name
        can be asked for meaningfully. That costs at most one turn of latency on a brand-new
        conversation, and buys immunity to the blank-session trap.
        """
        now = time.monotonic()
        # getattr: the runner's session-level fields go through the Session property layer,
        # so an __init__ assignment is not guaranteed to land as a plain runner attribute.
        if now - getattr(self, "_session_watch_at", 0.0) < self.SESSION_WATCH_SECONDS:
            return
        self._session_watch_at = now
        if not self.running or self.agent_in_flight or self.input.capturing:
            return  # never interrupt a running turn or an open palette
        try:
            refs = self.backend.list_sessions(self.repo.repo)
        except Exception as error:
            self._debug(f"session watch failed: {error!r}")
            return
        with_content = [ref for ref in refs if ref.label]
        if not with_content:
            return
        newest = max(with_content, key=lambda ref: ref.updated)
        current = self.state.backend_session_id
        if not current:
            # Nothing is bound yet, so this cannot be a switch — there is nothing to switch
            # away FROM. See _bind_first_conversation.
            self._bind_first_conversation(with_content)
            return
        if newest.id == current:
            return
        ours = next((ref.updated for ref in refs if ref.id == current), None)
        if ours is None and not self._written_since_launch(newest):
            # Our conversation exists only as an ID: --new-session (and any blank session) mints
            # one, and the backend writes no transcript until the first prompt. Every OTHER
            # conversation in the directory is therefore "newer than ours", so a --no-worktree run
            # adopted the PREVIOUS run's conversation as a switch: the status bar and state file
            # named the old conversation while the backend really was running the new one, and the
            # name given at the startup prompt seconds earlier was asked for a second time. A
            # conversation not written since this run started is history, not a switch. (A genuine
            # `/resume` of an old conversation DOES write to it, so it still counts as one.)
            self._debug(f"ignoring {newest.id}: not written since launch and our conversation is still empty")
            return
        if ours is not None and newest.updated <= ours:
            return  # ours is still the live one; a stale sibling must not pull tracking off it
        if self._session_recency(newest.id, newest.updated) <= self._session_recency(current, ours or 0.0):
            # The listing ranks by FILE mtime, which aGiTrack's own writes move: staging a resume,
            # mirroring a conversation to the base repo and retargeting a recorded cwd all touch a
            # transcript without adding a message to it. So confirm against the message timestamps
            # before believing a conversation has taken over — otherwise a conversation the user
            # abandoned reads as a fresh switch, and gets adopted (and named) as this session's.
            self._debug(f"ignoring {newest.id}: newer file, but no newer messages than {current}")
            return
        if not self._conversation_idle(newest.id, self.CHILD_IDLE_SECONDS):
            # The new conversation's first turn is still RUNNING. A conversation becomes visible
            # here the moment the user's prompt is written — long before the agent has answered
            # it — and every signal that normally says "the agent is busy" is keyed to the
            # conversation just left, which `/clear` froze. So without this the switch was
            # handled mid-turn: the commit below captured a turn with no response yet, and the
            # offer opened a modal over a working agent (which then relocated the session away
            # from files the turn had not written yet). Come back when it has finished.
            self._debug(f"native switch to {newest.id} deferred: its turn is still running")
            return
        self._debug(f"native session switch: {current} -> {newest.id}")
        # Captured BEFORE the tracked id moves: if the conversation is relocated to a worktree of
        # its own, this is what the outgoing session must be put back to (see
        # _restore_outgoing_session_identity).
        prior_message_id = self.state.last_backend_message_id
        self._abandon_summary_for_switched_session(current, newest.id)
        self.state.backend_session_id = newest.id
        self.state.last_backend_message_id = None
        self._initialize_session_baseline()
        self._rebase_baseline_for_new_conversation(newest.id)
        # Commit the turn that just ran BEFORE any dialog. A conversation is only visible here
        # once it HAS CONTENT, so by the time a switch is detected its first turn has already
        # finished and its changes are sitting in the tree. Two things would otherwise eat it:
        # the offer below is a modal, and a modal blocks the reactor — and with it the commit
        # pass — for as long as the user takes to answer; and relocation moves the session out
        # from under those changes entirely. Observed live as a `/clear`, a turn that wrote a
        # file, and no commit for it anywhere.
        self._commit_latest_turn_sync()
        outcome = self._relocate_switched_conversation(
            newest.id, previous_id=current, previous_message_id=prior_message_id
        )
        if outcome == self._RELOCATION_MOVED:
            return
        if self.worktree is not None:
            # STAYING PUT in worktree mode: the session keeps its name. A worktree session and
            # its name are 1:1 — the name IS the directory — so renaming here (which is what
            # _restore_or_ask_session_name does for an unknown conversation) splits the two:
            # the status bar and the turn branches take the new name while the directory keeps
            # the old, exit cleanup then removes the worktree by its directory name and leaks
            # the branches filed under the other one, and _remember_session_for_backend finally
            # overwrites the chosen name with the directory's — so the name the user typed is
            # lost and two conversations end up sharing one. Link the new conversation to THIS
            # session's name instead; the offer above is where a differently-named session is
            # made, and it makes a directory to match.
            #
            # This holds however the offer ended, including backing out of the naming step: the
            # conversation IS running in this session — its turns commit on this session's branch
            # and the status bar says so — and the record has to say what is actually happening.
            # (The commit flow files the same link independently, via
            # `_note_backend_session_change`, so withholding it here would only produce a
            # half-state.) What used to hurt was the consequence, not the record: going back to
            # the session's EARLIER conversation demanded a new name. It no longer does — see
            # `_reopen_conversation_in_session` — and the resume list now shows each conversation's
            # own prompt, so two conversations of one session are told apart.
            self._persist_session_name(newest.id)
            self._render()
            return
        # Without a worktree the same reasoning holds, minus the directory: the conversation is
        # running in THIS session, so it takes this session's name rather than being asked for
        # another one. Asking was wrong twice over. The user had named the session seconds or
        # minutes earlier and a `/clear` demanded a second name for what is plainly the same
        # session; and the question is a MODAL, which blocks the reactor — and with it the poll
        # that folds no-worktree auto mode's latent turns into real commits — until it is
        # answered. Left unanswered (Esc, or simply not noticed behind the backend's own screen)
        # nothing committed until aGiTrack exited, and the name was never linked to the new
        # conversation, so the NEXT run asked for a name too. Both reported together.
        self._restore_or_ask_session_name(newest.id, adopt=True)

    def _rebase_baseline_for_new_conversation(self, session_id: str) -> None:
        """Don't let a BRAND-NEW conversation's first turn be written off as already accounted for.

        ``initialize_session_baseline`` sets ``last_backend_message_id`` to the newest complete
        turn, meaning "everything up to here is already committed". That is right for a resume at
        startup, where those turns really were committed by an earlier run. It is exactly wrong
        for a conversation the user just started with `/clear`: the switch watcher can only see a
        conversation once it HAS CONTENT, so the first turn has already run — and marking it
        accounted-for means its changes are never committed by anyone. The file the agent just
        wrote stays in the tree with no commit, in either worktree.

        "Brand new" is decided by the ``_pre_spawn_sessions`` snapshot, the same since-launch
        filter the commit path uses: a conversation absent from it did not exist when the backend
        started, so nothing of it can have been committed. A conversation that WAS there (a
        `/resume` back onto older work) keeps the computed baseline — resetting that would
        re-commit its whole history as one enormous turn.
        """
        snapshot = self._pre_spawn_sessions
        if snapshot is None or session_id in snapshot:
            return
        self._debug(f"conversation {session_id} is new since launch; committing its turns from the start")
        self.state.last_backend_message_id = None

    # What came of the own-worktree offer. "Declined" is a decision (the user chose to keep the
    # conversation in this session); "cancelled" is the absence of one (Esc / quit at the name
    # prompt, or a relocation that could not proceed) — and the caller treats them differently.
    _RELOCATION_MOVED = "moved"
    _RELOCATION_DECLINED = "declined"
    _RELOCATION_CANCELLED = "cancelled"

    def _relocate_switched_conversation(
        self, session_id: str, *, previous_id: str | None = None, previous_message_id: str | None = None
    ) -> str:
        """Offer the just-switched-to conversation a worktree of its own, and make it.

        A conversation started with the backend's own `/clear` or `/resume` runs in the process
        aGiTrack already spawned, whose cwd is this session's worktree — so by default it shares
        that worktree, that turn branch and that merge target with the conversation before it.
        aGiTrack CAN give it its own, the same way ``sessions → New session`` and ``Resume a past
        conversation`` do: create a worktree and respawn the backend there with ``--resume``, so
        the conversation itself is preserved and only the process moves.

        That costs a backend restart, so it is asked rather than assumed. Returns True when the
        relocation happened and the caller must not touch the outgoing session any further.

        The retirement of the OLD process is the delicate part and the reason this is not simply
        ``_new_session``: that process has ALREADY left the old conversation and is writing the
        new one. Leaving it alive would put two backends on a single transcript. So its child is
        torn down and only then does the new session spawn. The outgoing worktree, its commits
        and its conversation all survive — it simply becomes dormant, and reappears in the
        sessions menu as an idle worktree to resume.
        """
        if self.worktree is None or not self._use_worktrees:
            return self._RELOCATION_DECLINED  # --no-worktree / manual: no worktrees to hand out
        choice = self._select_popup(
            "New conversation — give it its own worktree?",
            [
                "Yes — move it to a new worktree (the backend restarts)",
                f"No — keep it here, in '{self.name}'",
            ],
            # Kept under ~62 characters a line: the popup does not re-wrap, so a longer line
            # folds at the box edge and the overflow lands hard against the left border.
            detail=[
                f"This conversation started inside {self.backend.name}, in the process",
                f"aGiTrack spawned for session '{self.name}'. Unless it moves, it",
                "shares that session's worktree",
                f"(.agitrack/worktrees/{self.worktree.path.name}), its branch, and",
                f"its merge into {self._base_branch or 'the base branch'}.",
                "",
                "Moving it makes a worktree of its own and restarts the",
                "backend there, resuming this same conversation.",
                f"Session '{self.name}' keeps its worktree and its commits, and",
                "stays available under sessions → resume.",
            ],
        )
        if choice is None or choice.startswith("No"):
            # Esc reads as "leave it alone" — a decision to keep the conversation in this session,
            # so it takes this session's name (unlike backing out of the naming step below).
            return self._RELOCATION_DECLINED
        # NOTHING UNCOMMITTED MAY BE LEFT BEHIND. The caller commits the just-finished turn
        # before opening this offer, but that commit can decline to capture the turn (observed
        # live: a `/clear` turn that CREATED a file left it untracked and the turn branch empty).
        # Relocating on top of that would walk the session away from changes no commit holds —
        # they would sit untracked in a worktree the user has left, which is the silent-loss
        # failure aGiTrack exists to prevent. So refuse, and stay put: the ordinary commit pass
        # picks the turn up once the reactor is running again (verified live), and the offer
        # comes back on the next switch.
        if self.repo.has_changes():
            self._debug("relocation declined: outgoing worktree still has uncommitted changes")
            self._select_popup(
                "Not moved — this turn isn't committed yet",
                ["ok"],
                detail=[
                    f"The turn that just ran left changes in '{self.name}' that are not",
                    "committed yet, and moving the session now would leave them behind",
                    "in a worktree you are no longer in.",
                    "",
                    f"This conversation stays in '{self.name}' for now. The changes are",
                    "committed as usual in a moment.",
                ],
            )
            return self._RELOCATION_CANCELLED  # asked to move and could not: not a choice to stay
        name = self._name_for_switched_conversation(session_id)
        if name is None:
            # Backed out of naming (Esc, or quitting): the conversation stays where it is, but the
            # user never said it belongs to this session, so it is not given this session's name.
            self._set_message(
                f"Not moved — no name given. This conversation is still running in '{self.name}'; "
                "the offer comes back on the next conversation switch.",
                seconds=10.0,
            )
            return self._RELOCATION_CANCELLED
        outgoing = self.active
        # Land the turn that has just been committed on the base branch BEFORE cutting the new
        # worktree from it. The turn belongs to the conversation being moved, but it ran — and
        # was committed — in the worktree being left; a worktree cut from a base branch that
        # does not have it yet opens WITHOUT the files the conversation just created, and the
        # resumed agent reads its own last turn as never having happened ("b.txt isn't there…
        # it landed outside this working directory", live). Same call the pre-prompt path makes,
        # so --delay-merge and conflicts are handled exactly as they are everywhere else.
        self._integrate_committed_turn_before_new_turn()
        self._stop_file_watcher()
        self._teardown_child()  # the process that switched; the new session spawns its own
        self._new_session(name, resume_session_id=session_id)
        if not (self.sessions and self.sessions[-1] is self.active):
            # _new_session bailed (e.g. the worktree could not be created) and left a bare
            # session behind. Put the outgoing one back rather than stranding the runner with
            # no screen and no PTY; its child is gone, and the session-exit handling respawns.
            self._debug("relocation failed to start the new session; restoring the outgoing one")
            self.active = outgoing
            self._render()
            return self._RELOCATION_CANCELLED
        if outgoing in self.sessions:
            self.sessions.remove(outgoing)
        self._restore_outgoing_session_identity(outgoing, previous_id, previous_message_id, moved=session_id)
        self._set_message(
            f"Moved this conversation into its own worktree '{name}'. "
            f"Session '{getattr(outgoing, 'name', '?')}' is idle — resume it from the sessions menu.",
            seconds=10.0,
        )
        self._render()
        return self._RELOCATION_MOVED

    def _restore_outgoing_session_identity(
        self, outgoing, previous_id: str | None, previous_message_id: str | None, *, moved: str
    ) -> None:
        """Give the session the conversation just LEFT its own identity back.

        Detecting the switch re-points the (still active) session at the new conversation —
        necessarily, because the turn that has just run belongs to it and has to be committed
        here. Relocation then carries that conversation off to a worktree of its own, and unless
        this runs, the worktree it left behind is still recorded as running it: two worktrees,
        one conversation. That record outlives the run, and the next start resolves the moved
        conversation's NAME from whichever worktree claims it (``_resolve_session_name`` scans
        worktree state, then the backend's per-worktree conversation listing) — which is the
        reported "after a restart the two worktrees are mixed up", and why deleting the worktrees
        was the only cure.

        Two records have to be put back, in both places a worktree can claim a conversation:

        * the outgoing worktree's ``.agitrack/state.json`` — restored to the conversation it was
          on before the switch, so a later resume of that session continues ITS work, and so
          ``_adopt_latest_backend_session`` on exit doesn't re-adopt the moved one;
        * the outgoing worktree's per-directory transcript (Claude keeps one per project dir):
          the moved conversation ran in that directory, so a copy of it sits there and makes the
          old worktree the answer to "which session is this conversation?" forever. It is dropped
          once the new worktree's copy is in place — the conversation itself is untouched.
        """
        state = getattr(outgoing, "state", None)
        if state is not None:
            try:
                state.backend_session_id = previous_id  # setter re-points backend_session_repo too
                state.last_backend_message_id = previous_message_id
                self._debug(f"outgoing session restored to conversation {previous_id}")
            except Exception as error:
                self._debug(f"restoring the outgoing session's conversation failed: {error!r}")
        worktree = getattr(outgoing, "worktree", None)
        # The OUTGOING session's backend: it is the one that recorded the conversation there, and
        # by now `self.backend` belongs to the session that has just taken over.
        forget = getattr(getattr(outgoing, "backend", None) or self.backend, "forget_session_in", None)
        if forget is None or worktree is None or getattr(worktree, "path", None) is None:
            return
        try:
            if forget(worktree.path, moved):
                self._debug(f"dropped {moved}'s transcript from the worktree it moved out of")
        except Exception as error:
            self._debug(f"forget_session_in failed: {error!r}")

    def _sibling_worktree_conversations(self) -> dict[str, str]:
        """``conversation id -> the OTHER worktree that records it as its own``.

        Read from each sibling worktree's own ``.agitrack/state.json``, which is where a session
        says which conversation it is running. Two worktrees naming the same conversation is
        corruption by construction — aGiTrack never runs one conversation in two places."""
        owners: dict[str, str] = {}
        if self.worktree is None:
            return owners
        try:
            for info in self._worktrees().list():
                if info.name == self.worktree.name:
                    continue
                sid = AgitrackState(
                    info.path, default_backend=getattr(self.global_config, "default_backend", None)
                ).backend_session_id
                if sid:
                    owners[sid] = info.name
        except Exception as error:
            self._debug(f"sibling worktree scan failed: {error!r}")
        return owners

    def _release_sibling_owned_conversations(self, *, keep: str | None = None) -> None:
        """Let go of a conversation that belongs to a SIBLING session — the self-heal.

        Relocation now leaves both worktrees consistent, but a worktree that was mixed up by an
        EARLIER aGiTrack (or by a relocation interrupted part-way) stays mixed up on disk: its
        state file still names the conversation that moved out, and Claude's copy of that
        conversation still sits in its project directory. Both outlive the run, which is why the
        only cure used to be deleting the worktrees. So a worktree session checks, as it opens,
        whether what it is holding is really its own:

        * its RECORDED conversation, when a sibling worktree claims the same one and the durable
          name record does not name US as its session — released, and this worktree falls back to
          the newest conversation it genuinely owns (its own work, not the one that moved away);
        * any sibling-owned TRANSCRIPT left in this worktree's project directory — dropped, so
          ``latest_session_id`` here stops answering with someone else's conversation and
          ``_adopt_latest_backend_session`` cannot re-adopt it on the way out. Only ever a
          duplicate: ``forget_session_in`` refuses to remove a conversation's last copy.

        ``keep`` is the conversation the user explicitly asked to resume in this action; it is
        never released or dropped, so resuming a past conversation under a new name still works
        while its old worktree is still on disk.
        """
        if self.worktree is None or not self._use_worktrees:
            return
        owners = self._sibling_worktree_conversations()
        if not owners:
            return
        mine = self.state.backend_session_id
        if mine and mine != keep and mine in owners and self._recorded_session_name(mine) != self.worktree.name:
            # Prefer what this run was asked to continue here; otherwise this worktree's own
            # newest work. (At startup the two differ exactly when the state file is the corrupt
            # one and the durable record still knows which conversation belongs here.)
            replacement = keep if keep and keep not in owners else self._own_conversation_here(exclude=set(owners))
            self._debug(
                f"'{self.worktree.name}' was holding {mine}, which belongs to '{owners[mine]}'; "
                f"releasing it for {replacement}"
            )
            self.state.backend_session_id = replacement
            self.state.last_backend_message_id = None
            mine = replacement
        forget = getattr(self.backend, "forget_session_in", None)
        if forget is None:
            return
        for sid in owners:
            if sid in (mine, keep):
                continue
            try:
                if forget(self.repo.repo, sid):
                    self._debug(f"dropped sibling conversation {sid} from '{self.worktree.name}'")
            except Exception as error:
                self._debug(f"forget_session_in failed for {sid}: {error!r}")

    def _recorded_session_name(self, session_id: str | None) -> str | None:
        """The name the durable repo-root record gives a conversation (None if unknown)."""
        try:
            root = self._root_state()
            return root.session_name_for(session_id)
        except Exception as error:
            self._debug(f"session name lookup failed: {error!r}")
            return None

    def _own_conversation_here(self, *, exclude: set[str]) -> str | None:
        """The newest conversation WITH CONTENT recorded in this session's directory, ignoring
        ``exclude`` (conversations that belong to other sessions). None when there is none — a
        fresh conversation is then started, which is the right answer for a worktree whose only
        conversation turned out to be somebody else's."""
        try:
            refs = [ref for ref in self.backend.list_sessions(self.repo.repo) if ref.label and ref.id not in exclude]
        except Exception as error:
            self._debug(f"own-conversation lookup failed: {error!r}")
            return None
        return max(refs, key=lambda ref: ref.updated).id if refs else None

    def _name_for_switched_conversation(self, session_id: str) -> str | None:
        """The name for a conversation being moved into its own worktree: the one it already
        carries if aGiTrack has seen it, otherwise asked for with a fresh suggestion. None when
        the user cancels. A fresh suggestion (not the current session's name) because the new
        worktree is a sibling of the current one and two live sessions cannot share a name."""
        try:
            root = self._root_state()
            known = root.session_name_for(session_id)
        except Exception as error:
            self._debug(f"session name lookup failed: {error!r}")
            known = None
        if known and not self._live_session_name_taken(known):
            return known
        # Re-ask until the name is free. A session and its worktree are 1:1, so accepting a name
        # another LIVE session already holds would point two backends at one directory — the
        # same reason `_resume_conversation` asks again rather than silently renaming.
        title = "This conversation is moving to a session of its own"
        while True:
            chosen = self._prompt_session_name(title, default=self._next_session_name())
            if chosen is None:
                return None
            if not self._live_session_name_taken(chosen):
                return chosen
            title = f"'{chosen}' is already open — two live sessions can't share a worktree"

    def _bind_first_conversation(self, with_content: list) -> None:
        """Adopt the conversation this session just started — WITHOUT asking for a name again.

        A session started with ``--new-session`` (and any fresh session) has no
        ``backend_session_id`` until the backend writes its first transcript, which happens on
        the user's FIRST PROMPT. Until that moment the switch watcher is tracking nothing, so
        the conversation appearing looked exactly like a native switch: aGiTrack asked "New
        conversation detected — name this session?" for a session the user had named seconds
        earlier at the startup prompt. Two questions for one session, and answering the second
        with a different name splits the name record between the two.

        There is no switch here. Bind the id, KEEP the name already chosen, and make the link
        durable so a crash before the first commit doesn't lose it.

        Which conversation is "ours" needs two filters at once, and neither alone is enough.
        WRITTEN SINCE LAUNCH (the ``_pre_spawn_sessions`` snapshot, as the commit path uses):
        under ``--no-worktree`` the directory can hold conversations from long before this run,
        and an unbound session must never adopt one of those however recent it looks. HAS REAL
        CONTENT: Claude mints empty transcripts on picker actions, and they sort newest —
        binding to one strands the session on a blank conversation. So the candidate is the
        newest conversation satisfying BOTH, not merely the newest of either.
        """
        snapshot = self._pre_spawn_sessions
        if snapshot is None:
            return  # a restored session object has no snapshot; adopting blind is not safe
        # `>` not `>=`: a conversation nobody has touched compares equal to its snapshot entry
        # and drops out. Absent from the snapshot means it did not exist at launch — ours.
        fresh = [ref for ref in with_content if ref.updated > snapshot.get(ref.id, float("-inf"))]
        if not fresh:
            return  # nothing of ours written yet — stay unbound and look again next tick
        spawned = max(fresh, key=lambda ref: ref.updated).id
        self._debug(f"binding session '{self.name}' to its first conversation {spawned}")
        self.state.backend_session_id = spawned
        self.state.last_backend_message_id = None
        self._initialize_session_baseline()
        if spawned not in snapshot:
            # The conversation did not exist at launch, so every turn in it is OURS and there is
            # no history to skip. The baseline just computed assumes a RESUME — "everything
            # already recorded predates us" — and parks the watermark on the newest complete
            # turn. For a backend whose conversation only becomes visible once its first turn
            # FINISHES (OpenCode's session store), that turn is the newest complete one, so the
            # baseline swallowed the very work it was binding to: nothing was committed, and the
            # file it wrote was later offered back as "intentionally unstaged or git-ignored".
            # (A conversation that WAS in the snapshot is one the user resumed inside the
            # backend; there the baseline is right and its older turns must not be re-committed.)
            self.state.last_backend_message_id = None
        # Durable now, keyed by the id that finally exists. This also clears the pending-name
        # record written at startup, when there was no conversation to key the name to.
        self._persist_session_name(spawned)
        self._render()

    def _written_since_launch(self, ref) -> bool:
        """Whether ``ref`` has been written since aGiTrack spawned this session's backend.

        The same test ``_bind_first_conversation`` uses, against the ``_pre_spawn_sessions``
        snapshot: absent from it means the conversation did not exist at launch (ours), and a
        newer mtime than its snapshot entry means it has been written since (also ours). With no
        snapshot (a restored session object) the question cannot be answered, so keep the old
        behaviour rather than block a real switch."""
        snapshot = self._pre_spawn_sessions
        if snapshot is None:
            return True
        return ref.updated > snapshot.get(ref.id, float("-inf"))

    def _abandon_summary_for_switched_session(self, leaving_id: str | None, joining_id: str) -> None:
        """Drop summary work belonging to the conversation we are switching AWAY from.

        Summary state (``_summary_pending`` / ``_summary_result`` / the rolling
        ``state.session_summary``) is keyed by SESSION-THE-OBJECT, not by backend conversation
        id — one slot per aGiTrack session, because normally one aGiTrack session tracks one
        conversation for its whole life. A native switch (`/clear`, `/resume`, OpenCode's
        picker) breaks that assumption: the same aGiTrack session is now pointed at a different
        conversation, while the old one's summary state is still sitting in those slots. Two
        things then go wrong, both silently:

        * a summary worker started for the OLD conversation's commit lands after the switch and
          is applied/reported against the NEW conversation — and its ``sha`` is no longer the
          head it was computed for, so the amend fails and the user is told summarization
          failed in a session that never asked for one;
        * ``state.session_summary`` — the ROLLING summary — still describes the old
          conversation, so the new one's very first commit is summarized "in the context of"
          work it has nothing to do with, and that wrong context then compounds into every
          later summary of the new conversation.

        So: forget the pending/finished result for the outgoing conversation, and clear the
        rolling summary so the new one starts from a blank slate. The worker thread itself is
        left to finish and die on its own (it is a daemon and holds no lock); only its RESULT
        is disowned, because joining it here would block the reactor on a live LLM call.
        """
        if not leaving_id or leaving_id == joining_id:
            return
        pending, result = self._summary_pending, self._summary_result
        if pending is not None or result is not None:
            self._debug(f"discarding summary work for {leaving_id} (switched to {joining_id})")
        self._summary_pending = None
        self._summary_result = None
        # A stale rolling summary is not merely useless on a new conversation — it is wrong
        # input that the next summary is generated FROM.
        self.state.session_summary = None
        self.state.session_summary_commit = None

    def _restore_or_ask_session_name(self, session_id: str, *, quiet: bool = False, adopt: bool = False) -> None:
        """Put the status bar back in step with the conversation now being tracked.

        Two cases, and they must not be confused. A conversation aGiTrack has seen before carries
        a name the user already chose: going BACK to it (`/resume`) has to restore that name and
        id, not invent a new one. A conversation it has not seen is new work, and gets asked for
        a name — the same question `session → New` asks.

        ``adopt`` takes the third case out of that second one: a conversation the user started
        INSIDE this session (a native `/clear`) is this session continuing, not new work, so it
        inherits the session's existing name silently instead of prompting for one.
        """
        try:
            # `getattr`, not attribute access: a failed lookup here falls through to ASKING for a
            # name, so an incidental AttributeError would silently re-ask for a conversation the
            # user has already named — the exact thing this method exists to prevent.
            root = self._root_state()
            known = root.session_name_for(session_id)
        except Exception as error:
            self._debug(f"session name lookup failed: {error!r}")
            known = None
        if known:
            self.name = known
            if not quiet:
                self._set_message(f"Switched back to session '{known}' ({_short_session(session_id)}).")
            self._render()
            return
        # Repaint FIRST so the new id is on the status bar behind the prompt: the question makes
        # sense only once the user can see that the conversation really did change.
        self._render()
        if quiet:
            return  # startup: the normal naming flow runs right after and would ask twice
        if adopt:
            # Link the new conversation to the name this session already carries. `self.name` is
            # left alone — it is still the right label, and the status bar has just repainted with
            # it against the new id. Nothing is asked, so nothing blocks.
            self._persist_session_name(session_id)
            return
        # A FRESH suggestion, not `self.name`: this conversation is new work, and offering the
        # previous session's name invites keeping it — leaving two conversations sharing one name
        # and the status bar unable to say which is which. `_next_session_name` is the same
        # generator `session -> New` uses, so a switch made inside the backend and one made from
        # the menu suggest names from the same pool and never collide with a name already taken.
        name = self._prompt_session_name("New conversation detected", default=self._next_session_name())
        if name:
            self.name = name
            self._persist_session_name(session_id)
        self._render()

    def _repo_latest_session_id(self) -> str | None:
        # The conversation a bare `claude -c` / `opencode` would continue in the
        # repo aGiTrack was launched in (its most recent recorded session).
        try:
            return self.backend.latest_session_id(self.base_repo.repo)
        except Exception as error:
            self._debug(f"latest_session_id failed: {error!r}")
            return None

    def _resolve_startup_session_name(self, root_state, resume_id, prior_worktree) -> str:
        # Keep the conversation's existing (user-given) name if it has one; only
        # prompt when there is no real name yet. Auto `session-N` names don't count.
        # A fresh conversation (``--new-session``, so ``resume_id`` is None) always
        # prompts for its own name — it must NOT inherit the prior session's name,
        # since it is a brand-new conversation, not a continuation of that one.
        existing = root_state.session_name_for(resume_id)
        if not existing and resume_id and prior_worktree and not self._AUTO_NAME_RE.match(prior_worktree):
            # Resuming a conversation whose name only lived in the last-session
            # record; key it by the conversation id too so it stays linked once that
            # record moves on.
            existing = prior_worktree
            root_state.name_session(resume_id, existing)
        if not existing:
            existing = self._adopt_pending_session_name(root_state, resume_id)
        if existing:
            return existing
        name = self._prompt_startup_name(resume_id is not None)
        self._record_startup_session_name(root_state, resume_id, name)
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

    def _startup_default_name(self) -> str:
        # A friendly random word for the very first session, mirroring
        # ``_next_session_name`` for in-session new sessions. ``session-N`` is
        # forgettable and, since sessions are tracked/shared as
        # ``<github-id(s)>/<name>``, two people each starting ``session-1`` collide;
        # a random word is easier to remember and far less likely to clash. The
        # startup taken-set is used because ``self.sessions`` isn't built yet. The
        # user can still type their own at the new-session prompt.
        from agitrack.proxy.session_names import random_session_name

        return random_session_name(self._startup_taken_names())

    def _prompt_startup_name(self, continuing: bool) -> str:
        # Pre-reactor, cooked mode: the alt-screen has not been entered yet so
        # the terminal is still in line-buffered cooked mode.  Using input()
        # here is intentional — the reactor loop is not running, so there are
        # no PTY fds to drain, and the simple cooked readline is the right tool.
        # Do NOT convert this to a modal; modals require the reactor to be live.
        default = self._startup_default_name()
        print("Continuing a conversation that has no name yet." if continuing else "Starting a new session.")
        taken = self._startup_taken_names()
        while True:
            try:
                raw = input(f"Name this session [{default}]: ").strip()
            except (EOFError, KeyboardInterrupt, OSError):
                # No interactive stdin (piped/scripted run, or a closed/uncaptured
                # fd): accept the default name rather than failing startup.
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
        # Cross-branch safeguard: only ever merge a worktree's OWN recorded merge
        # branch into it. If the base we're about to merge differs from the branch this
        # worktree was set up for, refuse and warn loudly — one session's branch must
        # never be merged into another session's worktree (the demo-video→dev bug).
        own = self._worktree_merge_branch(repo)
        if own is not None and own != self._base_branch:
            self._set_message(
                f"⚠ Prevented a cross-branch merge: NOT merging '{self._base_branch}' into a session "
                f"worktree that merges into '{own}'. Sessions on different branches are kept separate.",
                seconds=15.0,
            )
            self._render()
            self._debug(f"cross-branch align blocked: base '{self._base_branch}' != worktree base '{own}'")
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

    def _worktree_merge_branch(self, repo: GitRepo) -> str | None:
        # The merge branch recorded in the worktree's OWN state file — read straight
        # from disk so it reflects THIS worktree regardless of which session is active.
        # None when unknown (e.g. a worktree created before this was tracked).
        try:
            return AgitrackState(repo.repo).merge_branch
        except Exception:
            return None

    def _worktree_has_pending_work(self, repo: GitRepo, branch: str) -> bool:
        # Pending = uncommitted changes, or commits on its branch not yet in base.
        return self._integration.worktree_has_pending_work(repo, branch)

    def _cleanup_stale_state_on_startup(self) -> None:
        # A non-graceful exit (the backend dying, SIGKILL, a crash) can leave junk
        # that makes the *next* run misbehave until a second restart — chiefly:
        #   • prunable git worktree registrations whose directories are gone, and
        #   • orphaned worktree *directories* that are no longer valid worktrees
        #     (e.g. only a `.agitrack/` recreated by a write after teardown).
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
        # Reconcile worktrees left by previous runs: integrate any pending commits
        # into the base. The worktree DIRECTORIES are kept (worktrees are persistent
        # now — their environment and any leftover files survive across runs and stay
        # resumable from the session list). Worktrees whose work cannot be merged
        # cleanly (a conflict, or uncommitted changes) are flagged for the user to resolve.
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
                if self._cleanup_stale_worktree(info):
                    continue
                # Committed work is integrated by the call above; UNCOMMITTED work left by a
                # crash needs the recovery policy (commit a finished turn, then merge). Startup
                # used to stop here and merely flag it, so the same SIGKILL left the turn
                # committed+merged when the user ran `agitrack --recover` (what the editor
                # extension does) and uncommitted behind a warning when they simply relaunched.
                if self._recover_stale_worktree(info):
                    continue
                flagged.append(info.name)
            except Exception as error:
                self._debug(f"reconcile skipped '{info.name}': {error!r}")
                flagged.append(info.name)
        self._delete_orphan_merged_branches()
        notes: list[str] = []
        if active_pending:
            notes.append(f"this session ('{self.name}') has commits to integrate")
        if flagged:
            notes.append(f"{len(flagged)} stale session(s) need attention: {', '.join(flagged)}")
        if notes:
            if active_pending:
                instruction = (
                    f"Open {self._menu_label()} → session and choose "
                    "'✓ Integrate this session's commits' (or 'resolve' for a flagged session)."
                )
            else:
                instruction = f"Open {self._menu_label()} → session to resolve them."
            self._set_message("⚠ " + "; ".join(notes) + ".\n" + instruction, seconds=12.0)

    def _recover_stale_worktree(self, info) -> bool:
        """Finish what a crashed session left in `info`: commit its finished turn, then merge.

        The same policy (and the same code) as `agitrack --recover`, run here because startup
        already holds the repo lock that the standalone entry point would take. Returns True
        when nothing is left for the user to deal with. A turn that was still in flight is
        never committed — the policy leaves it and reports it flagged, as before."""
        try:
            from agitrack.recovery import RecoveryService

            report = RecoveryService(self.base_repo, self.global_config, debug_fn=self._debug).recover_worktree(info)
        except Exception as error:
            self._debug(f"startup recovery of '{info.name}' failed: {error!r}")
            return False
        if report.recovered or report.integrated:
            self._debug(f"startup recovered '{info.name}': {report.summary()}")
        return not report.flagged

    def _cleanup_stale_worktree(self, info) -> bool:
        # Integrate a dormant worktree's pending commits (if any) and delete it.
        # Returns False (keep + flag) when it has uncommitted changes or its work
        # conflicts with the base and so needs the user/agent to resolve.
        result = self._integration.cleanup_stale_worktree(info, self._worktrees())
        if result:
            self._debug(f"cleaned stale worktree '{info.name}'")
        return result

    def _delete_orphan_merged_branches(self) -> None:
        # Remove agitrack/* branches that no worktree checks out and that are already
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
        if self._delay_merge and self.worktree is not None and not self._exiting:
            # --delay-merge: hold the merge so the user can review/edit first and
            # confirm it themselves. (Exit still finalizes via _integrate_session_on_exit
            # so committed work isn't stranded across runs.)
            self._notify_merge_deferred()
            return
        result = self._integrate_turn_or_conflict()
        if result == "conflict" and not self._exiting:
            # On exit there is no UI to drive a resolution; the work stays on its
            # branch for the next startup / session menu to surface.
            self._prompt_resolve_conflict(self.repo.current_branch())

    def _notify_merge_deferred(self) -> None:
        # Tell the user their turn was committed but not merged (only under
        # --delay-merge), naming the WORKING DIRECTORY so they know where to review —
        # crucial when it's a worktree, whose location they may not otherwise know.
        if not self._active_has_pending():
            return
        base = self._base_branch or "the base branch"
        self._set_message(
            f"Changes committed but not merged into {base} yet (--delay-merge). Review and edit them in "
            f"{self.repo.repo}, then merge via {self._menu_label()} → session → "
            f'"Merge reviewed changes into {base}".',
            seconds=30.0,
        )
        self._render()

    def _integrate_committed_turn_before_new_turn(self) -> None:
        # A new prompt is about to start a turn. If the previous turn was already
        # committed but its integration was deferred — typically because a
        # background commit summary was still being computed
        # (_summary_blocks_integration) — merge it into the base NOW, before the
        # new turn begins, instead of letting it ride along on the same branch and
        # only land once the whole new turn finishes.
        #
        # The merge is attempted directly (bypassing the summary hold): a pending
        # summary, if any, simply lands as git notes once it finishes
        # (_amend_summary_into_head sees the commit already in base and records
        # notes-only). A conflict is left on the branch with no resolve popup — per
        # the design it waits until the current agent call ends.
        if self.worktree is None or self.merge_ctx or self._base_branch is None:
            return
        if self._integration_paused or self._delay_merge:
            return  # --delay-merge: the user merges on their own confirmation
        try:
            if self.repo.has_tracked_changes() or self.repo.merge_in_progress():
                # A real in-flight tracked edit or a mid-merge: leave it for the normal
                # path. But UNTRACKED leftovers (files the agent created that the user
                # declined to stage) must NOT block this merge — they're not part of the
                # turn branch and don't affect the fast-forward, yet has_changes() counts
                # them, which stranded a ready, summarized commit whenever the next prompt
                # started before it had merged.
                return
            branch = self.repo.current_branch()
            if not is_managed_branch(branch):
                return
            if not self.base_repo.log_range(self._base_branch, branch):
                return  # nothing committed ahead of base to integrate
        except Exception as error:
            self._debug(f"pre-prompt integration check failed: {error!r}")
            return
        # Ignore a "conflict" result: integrate_turn_or_conflict already backed the
        # merge out, leaving the tree clean, and the conflict is surfaced when the
        # current turn finishes rather than interrupting the new prompt.
        self._integrate_turn_or_conflict()

    def _integrate_turn_or_conflict(self) -> str:
        # Try to fast-forward the current session's turn branch into the base.
        # Returns "integrated" (base advanced), "conflict" (the merge was backed
        # out and needs resolution), or "skip" (nothing to integrate).
        if self.worktree is None or self._base_branch is None or self.merge_ctx:
            return "skip"
        if self._integration_paused:
            return "skip"  # base switched out-of-band; merging is paused
        turn_branch = self.repo.current_branch()
        if not is_managed_branch(turn_branch):
            return "skip"
        try:
            if not self.repo.merge(self._base_branch):
                # Back out of the conflicted merge so the worktree is clean again;
                # the chosen resolution path re-starts the merge from here.
                self.repo.merge_abort()
                return "conflict"
            self._advance_base_to(turn_branch)
            self._debug(f"integrated '{self.name}' {turn_branch} -> {self._base_branch}")
            self._announce_agent_commit()
            return "integrated"
        except Exception as error:
            self._debug(f"integration failed for '{self.name}': {error!r}")
            return "skip"

    def _conflict_signature(self, source_branch: str) -> tuple[str, str] | None:
        """What the unresolved conflict IS: the base tip and the session's tip.

        Answering "leave for later" is an answer about this exact pair. If either moves — the
        base gains work, or this session commits another turn — the situation is new and worth
        asking about again. None when the shas can't be read, which simply means "ask"."""
        try:
            base = self.base_repo.rev_parse(self._base_branch) if self._base_branch else ""
            return (base, self.repo.rev_parse(source_branch))
        except Exception as error:
            self._debug(f"conflict signature failed: {error!r}")
            return None

    def _prompt_resolve_conflict(self, source_branch: str, *, force: bool = False) -> None:
        # A finished turn cannot fast-forward into the base because it conflicts
        # with work another session already integrated. Surface the same resolve
        # options the session menu offers for a conflicting session, labelled
        # with this session and its backend, then dispatch the choice.
        signature = self._conflict_signature(source_branch)
        if not force and signature is not None and signature == self.conflict_deferred_for:
            # Already answered "leave for later" for this exact state. The box is raised from a
            # background POLL, so without this the same question returned every couple of
            # seconds — measured live at 1s, 3s and 4s after the user dismissed it. `force` is
            # for the menu, where the user has just explicitly asked to integrate and must not
            # be met with silence.
            return
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
            self.conflict_deferred_for = signature  # don't ask again until something changes
            self._set_message(
                f"'{self.name}' has unintegrated work that conflicts with '{self._base_branch}'. "
                f"Resolve it any time via {self._menu_label()} → session.",
                seconds=8.0,
            )
            self._render()
            return
        self.conflict_deferred_for = None  # they chose to resolve it now
        self._start_merge_for_active(auto=choice.startswith("Merge automatically"))

    def _integrate_session_on_exit(self) -> None:
        # Clean up the current session's branch when aGiTrack exits: integrate any
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
        # When the base is the checked-out branch this is a working-tree
        # fast-forward; when the user has checked out a different branch in the
        # directory, advance_base_to advances the base's ref directly — but only
        # on a true fast-forward, never a force, so it can never move the wrong
        # branch or drop commits. Callers wrap this in try/except and treat a
        # failure (e.g. not a fast-forward) as "not integrated".
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
        for index in range(len(self.sessions)):
            if index == self.active_index:
                if not self.agent_in_flight:
                    self._align_session_to_base(self.repo)
                continue
            snapshot = self.sessions[index]
            if getattr(snapshot, "repo", None) is None or getattr(snapshot, "agent_in_flight", False):
                continue
            # Switch to the idle session so _base_branch (and the integration service)
            # reflect ITS OWN merge branch — never the active session's, which would
            # merge a different branch into this worktree (cross-branch contamination).
            self._with_session(snapshot, lambda: self._align_session_to_base(self.repo))

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
            f"[aGiTrack] Merge conflict: the base branch '{self._base_branch}' gained changes from another "
            f"session that conflict with your work in {listing}. The conflicting base commits are: {commits}. "
            "Please open the conflicted files, resolve every <<<<<<< / ======= / >>>>>>> marker keeping both "
            "changes' intent, and save. Do NOT run git or commit — aGiTrack will create the merge commit once you are done."
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
            if self.master_fd == fd:
                # The common case (the prompt's session is still active): write through its
                # platform child, which knows how to reach the backend (a raw os.write to a
                # bridge-socket fd would be wrong on Windows).
                self.active.process.write(b"\r")
            else:
                # A backgrounded session's PTY (POSIX multi-session merge); a transient
                # POSIX child writes straight to its master fd.
                BackendProcess(fd).write(b"\r")
        except OSError:
            return  # Enter never reached the backend; the merge gate stays closed
        if self.merge_ctx is not None and self.master_fd == fd:
            self.merge_ctx["prompt_sent_at"] = time.monotonic()
            if self.merge_ctx.phase is MergePhase.PENDING:
                self.merge_ctx.phase = MergePhase.RESOLVING

    def _begin_agent_merge(self, source_branch: str) -> None:
        # A merge is in progress (conflicted) in the worktree. Ask the session's
        # agent to resolve it; aGiTrack finalizes once the conflicts are gone.
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
        self._begin_agent_turn()
        self._set_message(
            f"Merge conflict in {', '.join(files) or 'this session'} — asking the agent to resolve it… "
            f"aGiTrack will commit the merge once the agent finishes (or use {self._menu_label()} → session → Complete merge).",
            seconds=12.0,
        )
        self._render()

    def _note_backend_session_change(self, new_session_id: str | None) -> None:
        # Keep the durable name record pointing at the conversation this session
        # is actually running (ids drift when the backend forks on resume).
        self._persist_session_name(new_session_id)
        previous = self.state.backend_session_id
        # Preserve shared-session recognition across the id drift: if this session
        # was shared/auto-shared under its previous id, remember new→previous so it
        # still shows as shared (and keeps auto-updating) after resume (#55). Also
        # carries the original share name onto the new id (round-trip re-sharing).
        self._record_shared_alias_on_drift(previous, new_session_id)
        # If the worktree's active conversation changed to a different backend
        # session that aGiTrack didn't start, the user likely started it from inside
        # the backend. Warn once that such sessions share this branch.
        if (
            self.worktree is not None
            and previous
            and new_session_id
            and new_session_id != previous
            and not self._warned_backend_session
        ):
            self._warned_backend_session = True
            # Reached only when the live watcher could NOT offer its own-worktree choice — a
            # switch made mid-turn, while the watcher stands down. Sticky rather than a modal:
            # this runs from the commit flow, which does not always execute on the reactor
            # thread, and a modal raised off it would wedge input.
            self._set_message(
                "Detected a new conversation started inside the backend, mid-turn. It stays in this "
                f"session's worktree (.agitrack/worktrees/{self.worktree.path.name}), so its changes are "
                "tracked on this session's branch. For a conversation with its own worktree, start it "
                f"from {self._menu_label()} → sessions → “+ New session (own worktree)”.",
                sticky=True,
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
                agitrack_session_id=self.state.session_id,
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

    def _handle_session_command(self, arg: str) -> str:
        # Ctrl-G then "sessions": manage the live concurrent sessions.
        arg = arg.strip()
        if arg in {"new", "fresh"}:
            return self._prompt_new_session()
        if arg.isdigit():
            self._switch_to_session_index(int(arg) - 1)
            return self._MENU_DONE
        return self._session_menu()

    def _handle_summarizer_command(self, arg: str) -> str:
        sub = arg.strip().lower()
        if sub in ("on", "off"):
            enabled = sub == "on"
            self._persist_summarizer_enabled(enabled)
            self._set_message(f"Summarizer {'enabled' if enabled else 'disabled'}.")
            self._render()
            return self._MENU_DONE
        if sub == "model":
            self._select_summarizer_model_popup()
            return self._MENU_DONE
        # Interactive menu: "Set model" opens the model picker, whose Esc returns here
        # (one level up); Esc on this list goes up one level to the Ctrl-G palette.
        while True:
            enabled = self._summarization_enabled()
            model = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            choice = self._select_popup(
                "Summarizer",
                [
                    f"Toggle ({'ON' if enabled else 'OFF'})",
                    f"Set model (current: {model})",
                ],
            )
            if choice is None:  # Esc → up one level to the palette
                self._render()
                return self._MENU_UP
            if choice.startswith("Toggle"):
                self._persist_summarizer_enabled(not enabled)
                self._set_message(f"Summarizer {'enabled' if not enabled else 'disabled'}.")
            elif choice.startswith("Set model"):
                self._select_summarizer_model_popup()
            self._render()

    def _choose_summarizer_model(self) -> "str | None | object":
        """Show the summarizer-model picker and RETURN the chosen model name, None for "same as
        the session model", or ``_NO_MODEL_PICK`` if the user backed out. Does not persist — so
        both the immediate popup and the settings menu's pending-save flow can reuse it. Lists
        the current backend's models (smallest tier first as the cheap default); a backend whose
        models can't be listed falls back to free-text entry."""
        from agitrack.summaries.model_select import list_available_models, smallest_model

        backend_name = self.state.backend
        current = self.state.summarization_model or self.global_config.summarization_model
        models = list_available_models(backend_name)
        if models:
            smallest = smallest_model(backend_name, models)
            ordered = [smallest, *(m for m in models if m != smallest)] if smallest else list(models)
            label_for: dict[str, str | None] = {}
            options: list[str] = []
            for model in ordered:
                label = f"{model}  (smallest — default)" if model == smallest else model
                label_for[label] = model
                options.append(label)
            clear_label = "Same as the agent's session model"
            label_for[clear_label] = None
            options.append(clear_label)
            choice = self._select_popup(f"Summarizer model (current: {current or 'same as session'})", options)
            return _NO_MODEL_PICK if choice is None else label_for.get(choice, choice)
        # No model list available (the backend's CLI couldn't be queried) — type a name.
        raw = self._prompt_popup(
            "Summarizer Model",
            f"Current: {current or '(same as session)'}\nEnter model (empty to clear):",
            default=current or "",
        )
        return _NO_MODEL_PICK if raw is None else (raw.strip() or None)

    def _select_summarizer_model_popup(self) -> None:
        # Pick a model and persist it immediately (the `:summarizer model` path). The settings
        # menu instead stores the same choice as a pending edit (see _edit_one_setting).
        chosen = self._choose_summarizer_model()
        if chosen is not _NO_MODEL_PICK:
            self._persist_summarizer_model(chosen if chosen is None else str(chosen))
            self._set_message(f"Summarizer model: {self.global_config.summarization_model or '(same as session)'}")
        self._render()

    def _persist_summarizer_model(self, value: str | None) -> None:
        # Persist the choice in the GLOBAL config so it survives restarts, session
        # switches, and applies across the whole repo; clear any per-session override
        # (which lives in a transient worktree state) so the global value takes effect.
        self.global_config.summarization_model = value
        self.state.summarization_model = None

    def _persist_summarizer_enabled(self, enabled: bool) -> None:
        # Persist the on/off toggle in the GLOBAL config so it survives restarts, like the
        # model. The per-session state lives in the session worktree's .agitrack/config.json,
        # which is removed when the worktree is torn down on exit — a toggle written only
        # there always reset to the default ("on") on the next launch (the reported bug).
        if self.global_config is not None:
            self.global_config.summarization_enabled = enabled
        # Keep the active session's view consistent for the current run.
        self.state.summarization_enabled = enabled

    # --- switch base branch ---

    def _prompt_merge_branch(self, title: str, current: str | None) -> str | None:
        # Pick a real (non-managed) branch as a session's merge destination; the
        # session's current target is marked. Returns the branch, or None on cancel.
        branches = [b for b in self.base_repo.list_branches() if not is_managed_branch(b)]
        if not branches:
            self._set_message("No branches available to merge into.")
            self._render()
            return None
        label_for: dict[str, str] = {}
        options: list[str] = []
        for branch in branches:
            label = f"{branch}  (current target)" if branch == current else branch
            label_for[label] = branch
            options.append(label)
        choice = self._select_popup(title, options)
        return None if choice is None else label_for.get(choice, choice)

    def _change_session_merge_branch_menu(self) -> str:
        # Session-config entry: change the merge destination of ANY session. Returns _MENU_DONE
        # once it acts/reports (so the menu closes and that's visible) or _MENU_UP on a plain
        # Esc/cancel (re-show the list).
        if not self.sessions:
            self._set_message("No sessions.")
            self._render()
            return self._MENU_DONE
        label_for: dict[str, int] = {}
        options: list[str] = []
        for index in range(len(self.sessions)):
            target = getattr(self.sessions[index], "_base_branch", None) or "?"
            label = f"{self._session_name(index)}  → {target}"
            label_for[label] = index
            options.append(label)
        choice = self._select_popup("Change the merge branch of which session?", options)
        if choice is None:
            return self._MENU_UP
        index = label_for[choice]
        session = self.sessions[index]
        if getattr(session, "agent_in_flight", False):
            # A running session's merge branch can't change mid-turn (its in-flight work
            # would split across branches); only idle sessions can be re-pointed.
            self._set_message(
                f"'{self._session_name(index)}' is running a turn — change its merge branch when it's idle.",
                seconds=8.0,
            )
            self._render()
            return self._MENU_DONE
        current = getattr(session, "_base_branch", None)
        target = self._prompt_merge_branch(f"Merge '{self._session_name(index)}' into which branch?", current)
        if not target:
            return self._MENU_UP
        if target == current:
            self._set_message(f"'{self._session_name(index)}' already merges into '{target}'.")
            self._render()
            return self._MENU_DONE
        if session is self.active:
            self._retarget_active_session(target)
        else:
            # Re-target a background session by temporarily making it active.
            self._with_session(session, lambda: self._retarget_active_session(target))
        return self._MENU_DONE

    def _integrate_session_keeping_alive(self) -> None:
        # Commit the latest turn and integrate it into the current base WITHOUT
        # the exit semantics of _finalize_pending_work — the backend child keeps
        # running and the worktree is kept. With _exiting set by the caller, a
        # conflict is left on the branch (no resolve popup) for the caller's
        # unintegrated-sessions check to report.
        self._commit_latest_turn_sync()
        if self._summary_thread is not None and self._summary_thread.is_alive():
            self._summary_thread.join(timeout=10)
        self._service_commit_summary()
        self._integrate_session_turn()

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

    def _session_menu(self) -> str:
        # One loop, two outcomes: Esc on the list → _MENU_UP (one level up to the Ctrl-G
        # palette); a context transition (switch/new/resume/integrate) → _MENU_DONE (unwind
        # to the agent). A configuration sub-action (rename, change merge branch, share,
        # manage shared, stop) or a child menu the user backed out of falls through to
        # ``continue``, re-showing this list so Esc inside it lands one level up here.
        while True:
            options: list[str] = []
            actions: list[tuple[str, object]] = []
            if self.merge_ctx or (self.worktree is not None and self.repo.merge_in_progress()):
                options.append("✓ Complete merge for this session")
                actions.append(("complete-merge", None))
            if not self.merge_ctx and self.worktree is not None and self._active_has_pending():
                # The active session has committed work not yet in the base — offer an
                # explicit, discoverable way to integrate it now (otherwise the only path
                # is re-selecting the current session, which isn't obvious). Under
                # --delay-merge this is the confirm point for reviewed changes; otherwise
                # it surfaces leftover commits (e.g. from a session resumed at startup).
                base = self._base_branch or "the base branch"
                label = (
                    "✓ Merge reviewed changes into "
                    if self._delay_merge
                    else "✓ Integrate this session's commits into "
                )
                options.append(label + base)
                actions.append(("integrate-active", None))
            shared_ids = self._my_shared_session_ids()  # mark which sessions are shared (#55)
            live_names = set()
            for index, session in enumerate(self.sessions):
                live_names.add(self._session_name(index))
                marker = "* " if index == self.active_index else "  "
                backend = getattr(getattr(session, "backend", None), "name", "?")
                label = f"{marker}{self._session_name(index)} [{self._session_status(index)}] ({backend})"
                merge_branch = getattr(session, "_base_branch", None)
                if merge_branch:
                    label += f" → {merge_branch}"  # the branch this session merges into
                if index == self.active_index and not self.merge_ctx and self._active_has_pending():
                    label += " — commits to integrate"
                sid = getattr(getattr(session, "state", None), "backend_session_id", None)
                # Recognise a shared session across resume id-drift (lineage-aware).
                if sid and self._session_auto_shared(sid):
                    label += " · ⇪ auto-share"
                elif sid and self._session_is_shared(sid, shared_ids):
                    label += " · ⇪ shared"
                options.append(label)
                actions.append(("switch", index))
            for info in self._dormant_worktrees(live_names):
                if self._dormant_has_pending(info):
                    options.append(f"  {info.name} [unmerged changes — resolve]")
                    actions.append(("resolve", info.name))
                else:
                    options.append(f"  {info.name} [idle — resume]")
                    actions.append(("resume", info.name))
            options.append(
                "+ New session (own worktree)" if self._use_worktrees else "+ New session (shares this directory)"
            )
            actions.append(("new", None))
            if self._use_worktrees and self.sessions:
                options.append("")  # gap: separate session-creation from session-management
                actions.append(("separator", None))
                options.append("✎ Rename a session")
                actions.append(("rename", None))
                options.append("⤳ Change a session's merge branch")
                actions.append(("merge-branch", None))
            if self._resumable_sessions():
                options.append("↻ Resume a past conversation…")
                actions.append(("resume-past", None))
            # "Share" is offered for every backend so the user gets a clear answer;
            # a backend without a portable transcript says so when chosen. "Resume a
            # shared session" only appears where it can actually work.
            options.append("")  # gap: set the sharing group apart
            actions.append(("separator", None))
            options.append("⇪ Share this session with collaborators…")
            actions.append(("share", None))
            if getattr(self.backend, "supports_session_sharing", False):
                options.append("⇩ Resume a shared session…")
                actions.append(("resume-shared", None))
                options.append("⚙ Manage shared sessions…")
                actions.append(("manage-shared", None))
            if len(self.sessions) > 1:
                options.append("- Stop a session")
                actions.append(("stop", None))
            choice = self._select_popup("Sessions", options)
            if choice is None:  # Esc → up one level (to the Ctrl-G palette), silently
                return self._MENU_UP
            kind, value = actions[options.index(choice)]
            if kind == "separator":
                continue  # the blank spacer row isn't an action; re-show the list
            # Context transitions — these move you into a different session, so unwind to the agent.
            if kind == "switch":
                assert isinstance(value, int)  # "switch" pairs with a session index
                if value == self.active_index:
                    self._select_current_session()
                    return self._MENU_DONE
                # Defer EVERY live-session switch to the top-level reactor (timers phase) so it
                # runs OUTSIDE this nested sessions modal — the same context as a Ctrl-G backend
                # switch, which works. Running a switch inline from the modal breaks host console
                # input on Windows: the switch's teardown/SetConsoleMode races the reader thread's
                # blocking ReadFile and wedges it (input then never arrives — the session-switch
                # hang). This applies to a same-backend switch too, not just cross-backend. Close
                # the menu now; the deferred switch fires next tick and does a full state reset
                # (fresh respawn) into the target session.
                self._pending_switch_session = self.sessions[value]
                return self._MENU_DONE
            if kind == "integrate-active":
                self._integrate_active_session()  # explicit "integrate now" for the active session
                return self._MENU_DONE
            if kind == "complete-merge":
                self._finalize_agent_merge()
                return self._MENU_DONE
            if kind == "resolve":
                assert isinstance(value, str)  # "resolve" pairs with a worktree name
                self._resolve_dormant_worktree(value)
                return self._MENU_DONE
            if kind == "resume":
                assert isinstance(value, str)  # "resume" pairs with a worktree name
                self._new_session(value)
                return self._MENU_DONE
            if kind == "new":
                if self._prompt_new_session() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            # Sub-menus that may transition (resume) OR be backed out of: a transition
            # unwinds to the agent; backing out re-shows this list (Esc lands here).
            if kind == "resume-past":
                if self._resume_session_menu() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            if kind == "resume-shared":
                if self._resume_shared_session_menu() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            if kind == "manage-shared":
                if self._manage_shared_sessions_menu() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            # Configuration actions: a COMPLETED action closes the menu so its result/progress
            # is visible (never bounce back to the list); only backing out (Esc before doing
            # anything) re-shows this list. Each handler signals which it was.
            if kind == "rename":
                signal = self._rename_session_menu()
            elif kind == "merge-branch":
                signal = self._change_session_merge_branch_menu()
            elif kind == "share":
                signal = self._share_session()
            else:
                signal = self._stop_session_menu()
            if signal == self._MENU_DONE:
                return self._MENU_DONE
            # else _MENU_UP: nothing was done (Esc/cancel) → re-show this list

    def _active_has_pending(self) -> bool:
        # True if the active session has committed work not yet in the base.
        return self._integration.active_has_pending(self.repo, self.worktree)

    def _active_has_mergeable_work(self) -> bool:
        # True if the (idle) active session has work worth merging now: committed-but-
        # unmerged commits, or uncommitted committable changes. Excludes mid-turn state,
        # where uncommitted changes are expected and auto-committed.
        return (
            self.worktree is not None
            and not self.merge_ctx
            and not self.agent_in_flight
            and (self._active_has_pending() or self._active_has_committable_changes())
        )

    def _select_current_session(self) -> None:
        # Picking the session you're already in: if it has work to merge, integrate it now
        # (the commit/merge — or conflict-resolve — messages pop up directly); otherwise
        # just acknowledge you're already here, rather than the confusing "nothing to
        # integrate".
        if self._active_has_mergeable_work() and self._base_branch is not None:
            self._merge_active_into(self._base_branch)
        else:
            self._set_message(f"Already in session '{self.name}'.")
            self._render()

    def _integrate_active_session(self) -> None:
        # Selecting the current session offers to integrate its outstanding
        # commits (the "merge box"), since there is nothing to switch to.
        if self.worktree is None or self._base_branch is None:
            self._set_message("This session has no worktree to integrate.")
            self._render()
            return
        if self.repo.has_changes():
            # Best effort: the user asked to merge, so don't dead-end them on uncommitted
            # changes. Only a turn that is GENUINELY still running should block (its edits
            # are mid-flight). Refresh the in-flight flag first (the git worker may not have
            # run since the turn went quiet), then if a turn really is active, ask them to
            # finish/stop it; otherwise commit the committable leftovers now and proceed.
            self._clear_agent_in_flight_if_idle()
            if self._agent_is_active():
                self._set_message(
                    "Finish or stop the current turn before integrating — a turn is still running.",
                    seconds=8.0,
                )
                self._render()
                return
            if self._active_has_committable_changes():
                self._set_message(f"Committing '{self.name}' changes before merging…", seconds=30)
                self._render()
                self._commit_latest_turn_sync()
            # Anything still uncommitted now is non-committable (git-ignored or intentionally
            # unstaged files) and can't enter the merge, but committed work still can —
            # fall through and integrate that rather than refuse the whole merge.
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
            # force: the user just picked "integrate this session" from the menu, so a previous
            # "leave for later" must not swallow the answer to a question they asked again.
            self._prompt_resolve_conflict(self.repo.current_branch(), force=True)
            return
        self._set_message(f"'{self.name}' has nothing to integrate.")
        self._render()

    # ------------------------------------------------------------------
    # Ctrl-G "merge": rescue un-integrated worktrees into a chosen branch
    # ------------------------------------------------------------------

    def _unmerged_worktrees(self) -> list[tuple[str, str]]:
        """Worktrees carrying un-integrated work, as ``(label, name)`` — ``name`` is
        empty for the active session, else the dormant worktree's name.

        "Un-integrated work" is either committed-but-unmerged commits OR uncommitted
        committable changes (tracked edits / untracked files that are neither
        git-ignored nor intentionally unstaged). The active session is only offered
        when idle — mid-turn uncommitted changes are expected and auto-committed, so
        surfacing "merge" then would be noise."""
        items: list[tuple[str, str]] = []
        try:
            if self._active_has_mergeable_work():
                items.append((f"{self.name} (current session)", ""))
        except Exception:
            pass
        live = {self._session_name(index) for index in range(len(self.sessions))}
        for info in self._dormant_worktrees(live):
            try:
                if self._dormant_has_pending(info):  # already counts uncommitted changes
                    items.append((f"{info.name} (dormant)", info.name))
            except Exception:
                continue
        return items

    def _active_has_committable_changes(self) -> bool:
        """True if the active worktree has uncommitted changes worth committing: tracked
        edits, or untracked files that aren't git-ignored and weren't intentionally
        unstaged. Excludes ignored/declined files so we don't offer to merge noise."""
        try:
            if self.repo.has_tracked_changes():
                return True
            declined = set(self.state.declined_untracked()) if self.state is not None else set()
            return any(path not in declined for path in self.repo.untracked_entries())
        except Exception:
            return False

    def _has_unmerged_work(self) -> bool:
        """True when some worktree still has work that hasn't been integrated — gates
        the top-level Ctrl-G "merge" command."""
        return bool(self._unmerged_worktrees())

    def _handle_merge_command(self) -> None:
        items = self._unmerged_worktrees()
        if not items:
            self._set_message("No unmerged worktrees — everything is integrated.")
            self._render()
            return
        if len(items) == 1:
            name = items[0][1]
        else:
            choice = self._select_popup("Merge which worktree's changes?", [label for label, _ in items])
            if choice is None:
                return
            name = next(n for label, n in items if label == choice)
        target = self._choose_merge_target(name)
        if target is None:
            return
        if name == "":
            self._merge_active_into(target)
        else:
            # Load the dormant worktree as the active session, then merge it — this
            # reuses the integration + conflict-resolution path (the agent can resolve
            # conflicts in the now-live session, just like the resolve flow).
            self._new_session(name)
            self._merge_active_into(target)

    def _choose_merge_target(self, name: str) -> str | None:
        """Offer the three merge destinations the user asked for: the branch checked out
        in the working directory, the session's own merge branch, or any other branch.
        Returns the chosen branch name, or None on cancel."""
        session_branch = self._base_branch if name == "" else (self._dormant_merge_branch(name) or self._base_branch)
        current = self._repo_dir_branch or self.base_repo.current_branch()
        _CUSTOM = "\x00custom"
        options: list[tuple[str, str]] = []
        # The session's OWN branch first when it differs: that is where this session's work
        # already belongs, so it is both the safe default and the only one that changes nothing.
        # Picking the repo directory's branch instead RE-TARGETS the session (see
        # `_merge_active_into`), which is worth saying out loud rather than discovering.
        if session_branch and session_branch != current:
            options.append((f"Session's branch ({session_branch})", session_branch))
            options.append((f"Current branch ({current}) — re-targets this session", current))
        else:
            options.append((f"Current branch ({current})", current))
        options.append(("A different branch…", _CUSTOM))
        choice = self._select_popup("Merge into which branch?", [label for label, _ in options])
        if choice is None:
            return None
        target = next(branch for label, branch in options if label == choice)
        if target == _CUSTOM:
            return self._prompt_merge_branch("Merge into which branch?", session_branch)
        return target

    def _merge_active_into(self, target: str) -> None:
        """Integrate the active session's un-integrated work into ``target`` (re-pointing
        the session's merge branch to it). Reuses _integrate_active_session, which best-
        effort commits any uncommitted committable changes first (so nothing is stranded)
        and surfaces the same resolve options on a conflict."""
        if self.worktree is None or self._base_branch is None:
            self._set_message("This session has no worktree to merge.")
            self._render()
            return
        self._base_branch = target  # syncs the IntegrationService to the chosen destination
        # ...and the worktree's RECORDED merge branch with it. The cross-branch guard reads that
        # record, not `_base_branch`, so without this the menu offered a destination it then
        # refused: picking "Current branch (main)" for a session that merges into `feature`
        # produced the cross-branch warning and did nothing at all. The guard is there to stop
        # AUTOMATIC cross-branch merges; a destination the user just picked from a menu is the
        # opposite of automatic, so it re-targets the session rather than being blocked by it.
        if self.state is not None:
            self.state.merge_branch = target
        self._integrate_active_session()

    def _dormant_merge_branch(self, name: str) -> str | None:
        """The merge branch a dormant worktree recorded for itself, if any."""
        try:
            info = next((i for i in self._worktrees().list() if i.name == name), None)
            if info is None:
                return None
            return getattr(AgitrackState(info.path), "merge_branch", None)
        except Exception:
            return None

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
    _WORKTREE_SESSIONS_TTL = 10.0  # seconds to memoize the slow `opencode session list` enumeration

    def _worktree_sessions(self) -> list:
        # (worktree-name, SessionRef) for every conversation this backend recorded
        # under any of the repo's aGiTrack worktrees — including ones whose worktree
        # has since been removed (the backend keeps the transcript keyed by path
        # or id). The worktree directory name IS the session's user-given name, so
        # this is how a named session — and its name — survives across runs even
        # after its worktree is gone.
        #
        # Briefly MEMOIZED: a single open of the resume menu calls this 3× (via
        # _resumable_sessions and _agitrack_named_sessions), each a slow `opencode session
        # list` subprocess — without the cache, entering the menu fired several of them back
        # to back and the TUI hung for seconds. The short TTL covers one menu render while
        # staying fresh enough that a just-created session still appears next time.
        now = time.monotonic()
        cached = self._worktree_sessions_cache
        if cached is not None and now - cached[0] < self._WORKTREE_SESSIONS_TTL:
            return cached[1]
        try:
            result = list(self.backend.list_worktree_sessions(self._worktrees().root))
        except Exception as error:
            self._debug(f"list_worktree_sessions failed: {error!r}")
            result = []
        self._worktree_sessions_cache = (time.monotonic(), result)
        return result

    def _resumable_sessions(self) -> list:
        # Past conversations to offer for resume: those the backend recorded in
        # the base repo (a plain claude/opencode run, or --no-worktree mode) plus
        # every conversation recorded under an aGiTrack worktree of this repo. The
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
        # A session that produced no commits still reserves its name in the durable
        # record, but the backend may no longer enumerate it (no transcript under a
        # current worktree path). Surface those named conversations too, so a
        # reserved name is never stranded — unresumable yet un-reusable (#75).
        # Resuming one recreates its worktree under the saved name and continues the
        # conversation if the backend still has it, or starts fresh there if not.
        # Date them by when they were last named (not the Unix epoch, which showed
        # as an absurd "20000d ago").
        try:
            root = self._root_state()
        except Exception:
            root = None
        for sid, name in self._agitrack_named_sessions().items():
            if sid and sid not in seen:
                seen.add(sid)
                named_at = root.session_named_at(sid) if root is not None else 0.0
                refs.append(SessionRef(id=sid, updated=named_at, label=name))
        refs = sorted(refs, key=lambda ref: getattr(ref, "updated", 0) or 0, reverse=True)
        return refs[: self.RESUME_LIST_LIMIT]

    def _agitrack_named_sessions(self) -> dict:
        # Friendly names for conversations aGiTrack itself created/named, keyed by
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
            root = self._root_state()
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

    def _resume_session_menu(self) -> str:
        sessions = self._resumable_sessions()
        if not sessions:
            self._set_message("No past conversations found to resume.")
            self._render()
            return self._MENU_UP
        live_ids = {getattr(getattr(s, "state", None), "backend_session_id", None) for s in self.sessions}
        names = self._agitrack_named_sessions()
        options: list[str] = []
        for ref in sessions:
            mark = "● " if ref.id in live_ids else "  "
            # NAME and PROMPT, not one or the other. A session can hold several conversations —
            # every `/clear` that stayed in it adds one, and they are ALL named after that session
            # — so a name-only row rendered them as two identical "alpha" lines with no way to
            # tell which held your work. The first prompt is what distinguishes them.
            name, prompt = names.get(ref.id), (ref.label or "").strip()
            if name and prompt and prompt != name:
                label = f"{name} · {prompt}"[:64]
            else:
                label = (name or prompt or "(no prompt recorded)").strip()[:64]
            options.append(f"{mark}{_short_session(ref.id)}  {self._format_age(ref.updated)}  {label}")
        choice = self._select_popup("Resume a conversation (newest first)", options)
        if choice is None:  # Esc → back up one level to the sessions menu
            return self._MENU_UP
        ref = sessions[options.index(choice)]
        self._resume_conversation(names.get(ref.id) or self._next_session_name(), ref.id)
        return self._MENU_DONE

    def _resume_conversation(self, name: str, session_id: str, *, backend: str | None = None) -> None:
        # If this conversation is already live, just switch to it; otherwise
        # create a worktree for it and resume the backend by id there. ``backend``
        # pins the new session to a specific backend (e.g. resuming a shared
        # OpenCode session while the active backend is Claude).
        for index, session in enumerate(self.sessions):
            if getattr(getattr(session, "state", None), "backend_session_id", None) == session_id:
                self._switch_to_session_index(index)
                return
        held_by = self._live_session_index_for_name(name)
        if held_by is not None and self._conversation_belongs_to_session(session_id, held_by, backend=backend):
            # It is not a name CLASH — it is the same session. This conversation ran in that
            # live session's worktree (a `/clear` left it behind there, or the user answered
            # "keep it here"), so going back to it is a conversation switch inside that session,
            # not a second session that needs a name of its own. Being asked to invent a name to
            # return to your own earlier conversation — and having it renamed for good — is the
            # reported bug; there is no second worktree to name here.
            self._reopen_conversation_in_session(held_by, session_id, name)
            return
        if held_by is not None:
            # A genuine clash: a DIFFERENT session holds this name. Two live sessions can't share
            # one — a session and its git worktree are 1:1, so they would run two backends in one
            # directory. Don't silently rename: ASK, explaining why, and offer a random word they
            # can keep or change (Esc cancels the resume).
            chosen = self._prompt_session_name(
                f"A session named '{name}' is already open, and it is a different session — "
                f"two live sessions can't share a worktree",
                default=self._next_session_name(),
            )
            if chosen is None:
                return  # user cancelled — leave the active session as-is
            name = chosen
        # Resuming relocates and re-spawns the backend (OpenCode in --no-worktree mode also
        # exports+imports the session to retarget its directory) — a few seconds. Paint a
        # notice first so the screen shows progress instead of looking frozen.
        self._set_message(f"Resuming '{name}'…")
        self._render()
        self._new_session(name, resume_session_id=session_id, backend=backend)

    def _live_session_name_taken(self, name: str) -> bool:
        return self._live_session_index_for_name(name) is not None

    def _live_session_index_for_name(self, name: str) -> int | None:
        """Index of the LIVE session holding ``name``, or None. Names are compared sanitized,
        exactly as the worktree directory they map to."""
        sanitized = _sanitize_name(name)
        for index in range(len(self.sessions)):
            if _sanitize_name(self._session_name(index)) == sanitized:
                return index
        return None

    def _conversation_belongs_to_session(self, session_id: str, index: int, *, backend: str | None = None) -> bool:
        """Whether ``session_id`` is an earlier conversation OF the live session at ``index`` —
        one that ran in its worktree — rather than an unrelated conversation whose name happens
        to collide.

        Decided from the durable name record (which session a conversation was last named
        under), backed by the worktree's own directory: a `/clear` conversation that stayed
        behind is filed under that session's name AND has its transcript there. A shared session
        being imported (``backend`` pins a backend for it) is never "the same session" — it is
        another machine's conversation that merely arrives with a colliding name.
        """
        if backend is not None or not (0 <= index < len(self.sessions)):
            return False
        session = self.sessions[index]
        worktree = getattr(session, "worktree", None)
        if worktree is None:
            return False  # --no-worktree: sessions share a directory, so "belongs to" means nothing
        name = self._session_name(index)
        if self._recorded_session_name(session_id) != name:
            return False
        try:
            repo = getattr(session, "repo", None)
            recorded_here = {ref.id for ref in self.backend.list_sessions(repo.repo)} if repo is not None else set()
        except Exception as error:
            self._debug(f"conversation-ownership listing failed: {error!r}")
            return False
        return session_id in recorded_here

    def _reopen_conversation_in_session(self, index: int, session_id: str, name: str) -> None:
        """Put a live session back on one of its own earlier conversations, in place: same
        worktree, same name, only the backend process restarts (with ``--resume``)."""
        self._switch_to_session_index(index)
        self._commit_latest_turn_sync()  # don't let the outgoing conversation's turn go unrecorded
        self._set_message(f"Reopening '{name}' on conversation {_short_session(session_id)}…")
        self._render()
        self._switch_to_session(session_id)

    def _live_session_for_lineage(self, owner: str, name: str) -> int | None:
        """Index of a live session that is the SAME shared session as ``owner/name`` —
        matched by its recorded shared-lineage origin, not its backend id (which differs
        per collaborator). None when no open session shares that lineage."""
        user = self._user_state()
        for index, session in enumerate(self.sessions):
            sid = getattr(getattr(session, "state", None), "backend_session_id", None)
            rec = user.shared_origin(sid) if sid else None
            if rec and rec.get("name") == name and (not owner or rec.get("owner") == owner):
                return index
        return None

    @staticmethod
    def _is_real_keypress(data: bytes) -> bool:
        """Whether *data* carries an actual keystroke rather than only terminal-emitted
        escape sequences (mouse reports, focus in/out). Lets a 'press any key' notice
        ignore an incidental mouse move while host mouse reporting is on."""
        stripped = _SGR_MOUSE_RE.sub(b"", data)
        stripped = _X10_MOUSE_RE.sub(b"", stripped)
        stripped = _FOCUS_EVENT_RE.sub(b"", stripped)
        return bool(stripped)

    def _format_age(self, updated: float) -> str:
        # An unknown/unset timestamp (0 or None) must not be rendered as a date —
        # it would show as ~20000d ago (the Unix epoch). Say so honestly instead.
        if not updated or updated <= 0:
            return "date unknown"
        delta = max(0, int(time.time() - updated))
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

    def _prompt_new_session(self) -> str:
        # Returns _MENU_DONE once a session is actually started (a transition → agent), or
        # _MENU_UP if any prompt is backed out of (silently re-show the sessions menu).
        name = self._prompt_session_name("New Session", default=self._next_session_name())
        if name is None:
            return self._MENU_UP
        # Fork the current conversation, or start a blank one?
        fork = self._prompt_fork_or_blank()
        if fork is None:
            return self._MENU_UP
        # A fork continues the current session's work, so default its merge branch to
        # the current session's; a blank session defaults to the repo directory's.
        base = self._prompt_new_session_base(default=self._base_branch if fork else None)
        if base is None:
            return self._MENU_UP
        # The new session merges into its OWN branch — independent of the other
        # sessions and of the branch checked out in the repo directory.
        if fork and self._fork_current_session(name, base_branch=base):
            return self._MENU_DONE
        if fork:
            self._set_message("Couldn't fork the current session; starting a blank one instead.", seconds=8.0)
        # A new session inherits the CURRENT session's backend, not the global default —
        # the user is coding in this backend, so a sibling session should start there too
        # (the global default may be stale, e.g. left at opencode). Captured here because
        # _new_session replaces self.active (and thus self.state) before reading it.
        self._new_session(name, base_branch=base, backend=self.state.backend)
        return self._MENU_DONE

    def _can_fork_active(self) -> bool:
        # A fork copies the active conversation, so it needs a portable transcript
        # (the same capability session sharing relies on) and a session to copy.
        return bool(getattr(self.state, "backend_session_id", None)) and getattr(
            self.backend, "supports_session_sharing", False
        )

    def _prompt_fork_or_blank(self) -> bool | None:
        # True = fork the current session, False = blank, None = cancel. No prompt
        # (blank) when there's no live conversation to fork.
        if not self._can_fork_active():
            return False
        choice = self._select_popup(
            "Start the new session as…",
            [
                "Blank session — a fresh conversation",
                f"Fork '{self.name}' — copy this conversation (under a new id)",
            ],
        )
        if choice is None:
            return None
        return choice.startswith("Fork")

    def _fork_current_session(self, name: str, *, base_branch: str | None = None) -> bool:
        # Duplicate the active conversation under a FRESH backend id, so the fork runs
        # independently and never clashes with the original. Returns False if forking
        # isn't possible (the caller then starts a blank session).
        agent = self.backend
        src_id = self.state.backend_session_id
        if not src_id or not getattr(agent, "supports_session_sharing", False):
            return False
        source_name = self.name  # the original session's name, captured before switching
        # The LATEST conversation state lives in the active session's worktree (its
        # cwd), so export from there first; the base repo may only hold an older
        # mirrored copy. (Same order as the auto-share path.)
        transcript = agent.export_session_raw(self.repo.repo, src_id) or agent.export_session_raw(
            self.base_repo.repo, src_id
        )
        new_id = getattr(agent, "new_import_id", lambda: None)() or agent.new_session_id()
        if not transcript or not new_id:
            return False
        # Install the copied transcript under the new id (does not touch the original),
        # then start a session resuming it — same path as a "keep both" shared resume.
        if not agent.import_shared_session(self.base_repo.repo, src_id, transcript, as_id=new_id):
            return False
        self._new_session(name, resume_session_id=new_id, backend=self.state.backend, base_branch=base_branch)
        if self.state.backend_session_id == new_id:
            # The fork took: record that this session resumes a copy of another
            # conversation, so its first commit notes the inherited context/tokens
            # (issue: track fork/copy in the interaction trace).
            self.state.set_session_origin_event(kind="fork", source=src_id, source_name=source_name)
        return True

    def _merge_target_default(self) -> str | None:
        # The default merge destination for a new session: the branch checked out in
        # the repo directory ("the base branch").
        return self._repo_dir_branch or (self.base_repo.current_branch() if self.base_repo is not None else None)

    def _prompt_new_session_base(self, *, default: str | None = None) -> str | None:
        # Which branch the new session merges into. Defaults to ``default`` (a fork's
        # current branch) or the repo directory's branch ("the base branch"); picking
        # another lets the session work toward a different branch without checking it
        # out. Returns the chosen branch, None on cancel, or the default when there's
        # nothing else to offer.
        default_branch = default or self._merge_target_default()
        if not self._use_worktrees or default_branch is None:
            return default_branch
        others = [b for b in self.base_repo.list_branches() if b != default_branch and not is_managed_branch(b)]
        if not others:
            return default_branch
        default_label = f"{default_branch}  (default)"
        options = [default_label, *others]
        choice = self._select_popup("Merge this session's changes into which branch?", options)
        if choice is None:
            return None
        return default_branch if choice == default_label else choice

    def _stop_session_menu(self) -> str:
        options = [self._session_name(index) for index in range(len(self.sessions))]
        choice = self._select_popup("Stop which session?", options)
        if choice is None:
            return self._MENU_UP
        self._stop_session(options.index(choice))
        return self._MENU_DONE

    def _fork_lineage_on_rename(self, session_id: str | None) -> None:
        # Rename-as-fork (#55): a deliberate rename re-identifies the session, so drop
        # any tracked shared-lineage origin. Sharing it now publishes a NEW
        # `<you>/<new-name>` entry of its own instead of updating the session it was
        # resumed from (or first shared as) — a rename means "make a separate copy".
        if session_id and self._user_state().shared_origin(session_id):
            self._user_state().set_shared_origin(session_id, owner=None, name=None)

    def _rename_session_menu(self) -> str:
        options = [self._session_name(index) for index in range(len(self.sessions))]
        choice = self._select_popup("Rename which session?", options)
        if choice is None:
            return self._MENU_UP
        index = options.index(choice)
        new_name = self._prompt_popup("Rename session", "New name for this session:", default=self._session_name(index))
        if new_name is None or not new_name.strip():
            return self._MENU_UP
        self._rename_session(index, new_name.strip())
        return self._MENU_DONE

    def _rename_session(self, index: int, new_name: str) -> None:
        # Rename a session by moving its worktree directory. The worktree is in use
        # (the backend's cwd is inside it), so release the backend first, move, then
        # restart it pointing at the new path.
        if not (0 <= index < len(self.sessions)):
            return
        sanitized = _sanitize_name(new_name)
        if self._session_name_taken(sanitized) and sanitized != _sanitize_name(self._session_name(index)):
            self._set_message(f"'{sanitized}' is already in use.", seconds=6.0)
            self._render()
            return
        self._switch_active(index)
        info = self.worktree
        if info is None or not self._use_worktrees:
            self._set_message("This session has no worktree to rename.")
            self._render()
            return
        if _sanitize_name(info.name) == sanitized:
            self._set_message("Name unchanged.")
            self._render()
            return
        old_name = info.name
        sid = self.state.backend_session_id
        # Release the worktree (the backend has its cwd inside) before moving it.
        self._stop_file_watcher()
        self._teardown_child()
        try:
            new_info = self._worktrees().move(old_name, sanitized)
        except Exception as error:
            self._debug(f"rename worktree failed: {error!r}")
            # Recover: respawn in the original worktree so the session isn't stranded.
            self._init_screen()
            self._spawn()
            self._start_file_watcher()
            self._set_message(f"Could not rename session: {error}", seconds=8.0)
            self._render()
            return
        self.name = new_info.name
        self.worktree = new_info
        self.repo = GitRepo(new_info.path)
        self.turn = self._turn_from_branch(self.repo.current_branch())
        self.state = AgitrackState(new_info.path, default_backend=self.global_config.default_backend)
        if sid:
            self.state.backend_session_id = sid  # re-point backend_session_repo at the new path
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
        # Whether this session was tracking a shared lineage BEFORE the rename forks
        # it (the fork below clears that origin). Used to warn the user that a later
        # share now creates a new shared entry instead of updating the original.
        shared_before_rename = bool(sid and self._user_state().shared_origin(sid))
        if sid:
            self._stage_backend_resume(sid)  # re-link the transcript under the new path
            # The one place a stored name SHOULD be replaced: the user just asked for it.
            self._persist_session_name(sid, overwrite=True)
            self._fork_lineage_on_rename(sid)
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._resize_child()
        self._enable_host_mouse()
        self._start_file_watcher()
        if shared_before_rename:
            self._set_message(
                f"Renamed session to '{new_info.name}'. This makes it a separate copy — sharing it now "
                "creates a NEW shared session rather than updating the one it came from.",
                seconds=12.0,
            )
        else:
            self._set_message(f"Renamed session to '{new_info.name}'.")
        self._render()

    def _switch_active(self, index: int) -> None:
        if not (0 <= index < len(self.sessions)) or index == self.active_index:
            return
        # Hold the pipeline lock across the foreground swap so the git worker (which
        # reads `self.active`) can't be mid-pass while the pointer moves. Draining the
        # modal mailbox while we wait keeps a worker that's blocked on a dialog from
        # deadlocking us. (RLock: reentrant when already held by this thread, e.g. the
        # background-session conflict path that calls us from the locked cluster.)
        self._debug(f"switch_active: idx={index} acquiring pipeline lock")
        self._acquire_pipeline_lock_from_main()
        try:
            self._debug("switch_active: lock held; joining parse worker")
            self._join_parse_worker_before_swap()
            # Swap under the outgoing session's parse lock: if the join above timed
            # out, the still-running worker writes its result to its owning Session
            # under this same lock, so it sees either fully-before or fully-after.
            lock = self.agent_parse_lock or threading.Lock()
            with lock:
                self.active = self.sessions[index]
            self.scroll_back = 0
            # Switch IN PLACE: keep the target session's backend RUNNING and resume its existing
            # (backgrounded) ConPTY. NEVER tear it down or respawn it — an earlier Windows
            # workaround did exactly that (_restart_agent), but teardown calls cleanup() →
            # interrupt(), which sends Ctrl-C/ETX to the very session you're switching to and
            # cancels its in-progress turn ("Interrupted"). The leak/hang that workaround was
            # meant to fix is now fixed at its root (subprocess console isolation — see
            # proc.console_isolation_kwargs), so the in-place resume is correct on every platform.
            if os.name == "nt":
                # Clear the OUTGOING backend's host terminal-mode mirror so the host returns to a
                # clean baseline (mouse/kitty/paste); the incoming backend re-establishes only its
                # own modes via _sync_terminal_modes as its output streams in. Host-side only — it
                # never touches a backend's ConPTY, so it can't interrupt the target.
                self._disable_host_terminal_modes()
            self._resize_child()
            self._enable_host_mouse()
            self._reassert_host_raw()  # a switch can knock the host console out of raw mode
            self._set_message(f"Switched to session '{self._session_name(index)}'")
            self._render()
            self._debug("switch_active: swapped + rendered")
            # The copy-back mute is per active-session-visit: a switch resets it so the
            # session we just landed on gets offered its own worktree-only files (background
            # sessions are never interrupted mid-run; this is where we catch up). Only when
            # it's idle — a session still mid-turn gets offered by the turn path once it settles.
            # The remembered copy/overwrite decisions are reset too: a different session has a
            # different worktree and file set, so its copy-back must be confirmed afresh.
            self._copy_declined = set()
            self._copy_always = False
            self._overwrite_always = False
            self._copy_decision_set = set()
            if not self._agent_is_active():
                # Defer this session's user-commit / copy offer until its latest agent turn has
                # been reconciled (parsed + committed) in the BACKGROUND — otherwise the agent's
                # own just-made changes can be offered as the user's (a Claude change shown in the
                # "user commit" box right after switching to a session whose turn hadn't been
                # parsed yet). _service_deferred_switch_offer parses, then offers once only
                # genuine user changes remain.
                self._debug("switch_active: deferring copy-to-base until transcript reconciled")
                self._pending_switch_copy_offer = self.active
                self._switch_offer_parse_started = False
        finally:
            self._pipeline_lock.release()
        self._debug("switch_active: lock released; prompting merge-target if diverged")
        # The newly-active session may merge into a different branch than the one
        # checked out in the repo directory — ask where its changes should merge.
        self._prompt_merge_target_if_diverged()
        self._debug("switch_active: complete")

    def _switch_to_session_index(self, index: int) -> None:
        # Entry point for switching to another LIVE session by index.
        #
        # On WINDOWS, switching to a session that runs a DIFFERENT backend by resuming its
        # backgrounded ConPTY steals the host console: keystrokes stop reaching aGiTrack
        # afterwards (confirmed via DEBUG_RAW — the console input mode drops and even a fresh
        # ReadFile receives nothing). A FRESH backend spawn never does this (the first
        # Claude→OpenCode switch, which spawns, works). So for a cross-backend switch on Windows,
        # become the target session and then RESPAWN its backend fresh (resuming the same
        # conversation) — a clean ConPTY that doesn't steal the console. This is the
        # "(re)initialize the new backend" the manual backend-then-session two-step achieves.
        if not (0 <= index < len(self.sessions)):
            return
        target = self.sessions[index]
        target_name = getattr(getattr(target, "backend", None), "name", None) or getattr(
            getattr(target, "state", None), "backend", None
        )
        current_name = getattr(self.backend, "name", None)
        self._debug(f"_switch_to_session_index: idx={index} target_backend={target_name} cur={current_name}")
        self._switch_active(index)

    def _run_pending_session_switch(self) -> None:
        # Execute a sessions-menu cross-backend switch deferred out of the (nested) sessions modal,
        # now that we're at the top of the reactor loop — the same context a Ctrl-G backend switch
        # runs in. Doing it here instead of inline in the modal keeps host console input alive.
        target = getattr(self, "_pending_switch_session", None)
        if target is None:
            return
        self._pending_switch_session = None
        if target in self.sessions:
            self._switch_to_session_index(self.sessions.index(target))

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
            # The backend exited on its own while backgrounded (some backends — notably Claude
            # on Windows — quit when they lose the foreground). The ACTIVE session already
            # auto-relaunches+resumes in this case (_relaunch_backend_or_exit); a background
            # session must be just as resilient, or switching away from such a backend silently
            # loses the session. Try to bring it back in the background first; only drop it if it
            # won't stay up (crash-loop guard) so it isn't lost forever.
            if not self._relaunch_background_session(session):
                self._stop_session(self.sessions.index(session), commit=False)

    def _relaunch_background_session(self, session: Session) -> bool:
        # Respawn a background session's backend IN THE BACKGROUND, resuming the same
        # conversation, after it exited unexpectedly. Mirrors the active session's
        # _relaunch_backend_or_exit (teardown → re-baseline → spawn) but omits every
        # foreground/host step (no host-mode/mouse/raw changes, no _render) so it never paints
        # over or disturbs the foreground session. Returns False (→ caller stops the session)
        # when the per-session crash-loop guard trips, so a backend that refuses to stay up is
        # dropped rather than relaunched forever.
        if session not in self.sessions:
            return False
        key = id(session)
        now = time.monotonic()
        recent = [t for t in self._bg_relaunch_times.get(key, []) if now - t < 30.0]
        if len(recent) >= 3:
            self._debug(f"background session '{getattr(session, 'name', '?')}' exited 3x/30s; dropping")
            self._bg_relaunch_times.pop(key, None)
            return False
        recent.append(now)
        self._bg_relaunch_times[key] = recent

        def _respawn() -> None:
            self._teardown_child()  # close the dead child's bridge fd before respawning
            self._reset_agent_tracking()
            self._sanitize_state_trace()
            self._initialize_session_baseline()
            self._init_screen()  # the session's own (per-session) screen; no host paint
            self._spawn()  # resumes self.state.backend_session_id in the background

        try:
            self._with_session(session, _respawn)
        except Exception as error:
            self._debug(f"background relaunch of '{getattr(session, 'name', '?')}' failed: {error!r}")
            return False
        session.last_child_output = time.monotonic()  # don't immediately re-poll as idle/dead
        self._debug(f"relaunched background session '{getattr(session, 'name', '?')}' after backend exit")
        return True

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
            self.state = self._root_state()
            self.state.backend = backend_name
            self.backend = make_proxy_agent(backend_name)
            self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
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
        self.state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
        self.state.backend = backend_name
        self.backend = make_proxy_agent(backend_name)
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
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
        # Defer integration while this session's commit summary is still being
        # computed, exactly as the active path does: it lets the summary be
        # amended into the message (not notes-only) and keeps this session's
        # "summarizing…" status line up so concurrent sessions are each visible.
        if self._summary_blocks_integration(time.monotonic()):
            return "skip"
        return self._integrate_turn_or_conflict()

    # How far the background poll may back off for a session that keeps turning up nothing, and
    # how many empty passes it takes to get there (the interval doubles each time).
    BACKGROUND_IDLE_POLL_SECONDS = 30.0
    _BACKGROUND_POLL_MAX_MISSES = 4

    def _background_poll_interval(self, session: Session) -> float:
        """How often to run this background session's commit/integrate pass.

        ``POLL_SECONDS`` while it is producing work, doubling towards
        ``BACKGROUND_IDLE_POLL_SECONDS`` once it stops. This pass runs on the REACTOR thread —
        it swaps ``self.active``, so it cannot go on the git worker — and each one is a burst of
        git (status, log, integrate) measured at ~25 ms. At a flat 2 s that is a fixed tax per
        background session on the thread that also services the keyboard, paid forever by
        sessions that have been idle for hours. A session that produces ANY backend output is
        back on the fast cadence for its next pass (see :meth:`_note_background_poll`), so
        finished work is still committed and integrated promptly.
        """
        misses = min(getattr(session, "_bg_poll_misses", 0) or 0, self._BACKGROUND_POLL_MAX_MISSES)
        return min(self.POLL_SECONDS * (2**misses), self.BACKGROUND_IDLE_POLL_SECONDS)

    def _note_background_poll(self, session: Session) -> None:
        """Record whether this pass has anything new to look at, for the backoff above.

        "New" is the session's backend having SPOKEN since the last pass: a turn cannot have
        finished — so nothing can be owed a commit — without the agent writing something. The
        watermark is the ``last_child_output`` timestamp, which the background PTY drain keeps
        current whether or not the session is on screen.
        """
        spoke_at = getattr(session, "last_child_output", 0.0) or 0.0
        if spoke_at != (getattr(session, "_bg_poll_seen", 0.0) or 0.0):
            session._bg_poll_seen = spoke_at
            session._bg_poll_misses = 0
        else:
            session._bg_poll_misses = (getattr(session, "_bg_poll_misses", 0) or 0) + 1

    def _service_background_sessions(self) -> None:
        # Integrate background sessions as soon as they go idle, so finished work
        # lands in the base without waiting to be switched to. A background
        # session whose finished turn cannot fast-forward is brought to the
        # foreground and its resolve options box is surfaced (session + backend).
        now = time.monotonic()
        self._debug_session_liveness(now)
        if self.merge_ctx is not None:
            return
        for index in range(len(self.sessions)):
            if index == self.active_index:
                continue
            session = self.sessions[index]
            if getattr(session, "merge_ctx", None) is not None:
                self._with_session(session, self._maybe_complete_agent_merge)
                continue
            if now - getattr(session, "last_child_output", 0.0) < self.CHILD_IDLE_SECONDS:
                continue
            if now - getattr(session, "last_poll", 0.0) < self._background_poll_interval(session):
                continue
            session.last_poll = now
            self._note_background_poll(session)
            if self._with_session(session, self._commit_and_integrate_background) == "conflict":
                self._switch_active(index)
                self._prompt_resolve_conflict(self.repo.current_branch())
                return

    def _open_session_worktree(self, name: str, *, base_branch: str | None = None) -> tuple[WorktreeInfo, GitRepo]:
        worktrees = self._worktrees()
        path = worktrees.worktree_path(name)
        if self._is_valid_worktree(path):
            repo = GitRepo(path)
            # Reusing a persistent worktree from a previous run. If it was created with the full
            # environment, keep it in sync with the base: copy in newly-added base files silently
            # and queue an "overwrite the worktree's older copies?" prompt for any base files now
            # newer (deferred, since we may be mid-startup with no UI yet). A tracked-only worktree
            # is left as-is — the user opted out of carrying the environment.
            self._refresh_worktree_from_base(worktrees, path)
            return WorktreeInfo(name=path.name, path=path, branch=repo.current_branch()), repo
        # A leftover, *invalid* dir (e.g. only `.agitrack/` recreated by a write after
        # the previous run tore the worktree down) would otherwise make GitRepo
        # fail and drop us into a fresh session — the "first restart starts new,
        # second restart resumes" off-by-one. Clear it so `create` (which prunes
        # first) can re-add the worktree cleanly.
        if path.exists():
            self._debug(f"removing invalid leftover worktree dir {path}")
            shutil.rmtree(path, ignore_errors=True)
        base = base_branch or self._base_branch or self.base_repo.current_branch()
        info = worktrees.create(name, base=base)
        # `create` only places the tracked files git checks out. Whether to also copy the full
        # base environment (untracked + git-ignored) is the user's choice — queued as a deferred
        # prompt so startup stays fast (the copy can be slow) and the prompt has a UI to appear in.
        self._pending_env_copy.append((info.path, info.name))
        return info, GitRepo(info.path)

    def _refresh_worktree_from_base(self, worktrees: WorktreeManager, path) -> None:
        """Re-sync a reused full-environment worktree with the base, ASKING before changing it:
        if the base has environment files this worktree lacks (new since) or newer versions of
        ones it has, queue a single "update this worktree from the base?" prompt for the loop.
        Nothing is copied silently — the user decides — so a worktree preserved across runs can
        be brought up to date (e.g. after the base's .gitignore/deps changed) on demand. A
        tracked-only worktree (the user opted out of the environment) is left untouched."""
        try:
            if not AgitrackState(path, default_backend=self.global_config.default_backend).copy_full_env:
                return  # tracked-only worktree — nothing to keep in sync
            pending = sorted(set(worktrees.base_missing_entries(path)) | set(worktrees.base_newer_entries(path)))
        except Exception as error:
            self._debug(f"worktree env refresh failed for {path}: {error!r}")
            return
        if pending:
            self._pending_env_refresh.append((path, pending))

    def _present_pending_env_copy(self) -> None:
        """Main thread: surface the deferred "copy the full environment into the new worktree, or
        only the tracked files?" prompt for each freshly created worktree. Default (and Esc) is
        tracked-only, so startup stays fast unless the user opts in. On opt-in the copy runs here
        (rsync delta) behind a "creating…" message, and the choice is persisted on the worktree."""
        while self._pending_env_copy:
            path, name = self._pending_env_copy.pop(0)
            try:
                choice = self._select_popup(
                    f"New worktree '{name}' at {path}.\n"
                    f"git has checked out the tracked files. Also copy the FULL environment "
                    f"(untracked + git-ignored files: .env, node_modules, virtualenvs, build output) "
                    f"so the agent has the same setup as the base repo? This can take a moment for a "
                    f"large tree; by default only the tracked files are used.",
                    ["Only the tracked files", "Copy the full environment"],
                )
            except Exception as error:
                self._debug(f"env-copy prompt failed: {error!r}")
                continue
            if choice != "Copy the full environment":
                continue  # tracked-only (default / Esc): nothing more to do
            self._set_message(f"Creating worktree '{name}' — copying the environment, please wait…")
            self._render()
            try:
                copied = self._worktrees().copy_base_environment(path)
                self._mark_env_from_base(path, copied)
                # Stamp the moment the copy finished: the copy-back offer won't ask about anything
                # not modified since (the copied files), only what the agent edits/creates after.
                self._env_copy_watermark[str(path)] = time.time()
                AgitrackState(path, default_backend=self.global_config.default_backend).copy_full_env = True
            except Exception as error:
                self._debug(f"env copy for '{name}' failed: {error!r}")
                self._set_message(f"Could not copy the environment into '{name}': {error}", seconds=8.0)
                self._render()
                continue
            self._set_message(f"Copied {len(copied)} environment entr(y/ies) into worktree '{name}'.", seconds=6.0)
            self._render()

    def _mark_env_from_base(self, path, rels: list[str]) -> None:
        """Record that ``rels`` were copied INTO the worktree FROM the base, with their current
        fingerprints. The copy-back-to-base offer skips a file still matching its recorded
        fingerprint, so files that merely came from the base aren't offered to be copied back —
        which would otherwise create a copy loop (base → worktree → base → …). A file the agent
        later edits no longer matches, so genuine changes are still offered."""
        if not rels:
            return
        registry = self._env_from_base.setdefault(str(path), {})
        for rel in rels:
            fingerprint = self._file_fingerprint(Path(path) / rel)
            if fingerprint is not None:
                registry[rel] = fingerprint

    def _present_pending_env_refresh(self) -> None:
        """Main thread: surface any deferred "the base repo has new/changed files — update this
        worktree?" prompts queued while a worktree was reopened (possibly before the UI was up).
        Covers both files the worktree lacks and ones the base now has a newer version of. One
        prompt per worktree. Pulling the base's latest into the worktree is the safe, expected
        default (it's only the agent's sandbox), so "Yes" is the default option; declining keeps
        the worktree as-is."""
        while self._pending_env_refresh:
            dest, rels = self._pending_env_refresh.pop(0)
            try:
                choice = self._select_popup(
                    f"The base repo has {len(rels)} untracked / git-ignored file(s) (e.g. .env, "
                    f"dependencies, build output) that git won't sync into this session's worktree "
                    f"({dest}). Copy them from the base now?\n"
                    f"(Committed changes are merged into the worktree automatically — this is only "
                    f"for files git doesn't track.)\n\nFile(s):",
                    ["Yes, update from the base repo", "No, keep the worktree as-is"],
                    detail=rels,
                )
            except Exception as error:
                self._debug(f"env-refresh prompt failed: {error!r}")
                continue
            if choice and choice.startswith("Yes"):
                copied = self._worktrees().copy_entries(dest, rels)
                self._mark_env_from_base(dest, copied)
                self._set_message(f"Updated {len(copied)} file(s) in the worktree from the base repo.", seconds=6.0)
                self._render()

    def _present_pending_noworktree_cleanup(self) -> None:
        """Main thread: at a --no-worktree start, offer to DELETE leftover session worktree
        directories. They aren't used without worktrees, and their presence can confuse a resumed
        agent into editing them instead of the base repo, so deleting them is the safe option.
        Default is keep; deleting also removes any uncommitted work in those directories."""
        names = self._pending_noworktree_cleanup
        if not names:
            return
        self._pending_noworktree_cleanup = []
        try:
            choice = self._select_popup(
                f"{len(names)} session worktree director(ies) still exist under .agitrack/worktrees/. "
                f"In --no-worktree mode they're unused, and a resumed agent can get confused into editing "
                f"them instead of the base repo. Delete them now?\n"
                f"(Permanently removes those directories AND any uncommitted work in them. The "
                f"conversations themselves are kept and stay resumable.)\n\nWorktree(s):",
                ["Keep them", "Delete them"],
                detail=names,
            )
        except Exception as error:
            self._debug(f"no-worktree cleanup prompt failed: {error!r}")
            return
        if choice != "Delete them":
            return
        worktrees = self._worktrees()
        # Deleting can take a while for large worktree directories, so show progress up front
        # rather than letting the screen look frozen mid-removal.
        self._set_message(f"Deleting {len(names)} leftover worktree director(ies)…", seconds=30)
        self._render()
        removed = 0
        for name in names:
            try:
                worktrees.remove(name)  # git worktree remove + prune, or force-rmtree a stray dir
                removed += 1
            except Exception as error:
                self._debug(f"no-worktree cleanup: removing '{name}' failed: {error!r}")
        self._set_message(f"Deleted {removed} leftover worktree director(ies).", seconds=6.0)
        self._render()

    def _is_valid_worktree(self, path) -> bool:
        # A real linked worktree has a `.git` file pointing at its gitdir and a
        # resolvable current branch; a torn-down leftover (only `.agitrack/`) has not.
        if not (path / ".git").exists():
            return False
        try:
            GitRepo(path).current_branch()
            return True
        except Exception:
            return False

    def _warn_parallel_no_worktree_sessions(self) -> None:
        """Explain the shared-tree caveat the first time a user opens an additional
        --no-worktree session in a run. Parallel sessions are allowed; this only makes
        sure the user knows they have no isolation (they edit the same files at once and
        a turn's commit captures whatever is in the tree)."""
        if self._warned_parallel_no_worktree:
            return
        self._warned_parallel_no_worktree = True
        self._select_popup(
            "These sessions share this directory (--no-worktree): they have no isolation, so "
            "they edit the same files at the same time and a turn's commit captures whatever is "
            "in the working tree then — coordinate as you would with another person editing the "
            "same checkout. Restart without --no-worktree to give each session its own worktree.",
            ["Got it"],
        )

    def _active_backend_name(self) -> str | None:
        """The backend the CURRENT session is running, or None when it cannot be told.

        Read from the live proxy agent first (always right for this run) and only then from
        the session state, whose getter RAISES when no backend is configured anywhere — this
        is called precisely to avoid that raise reaching a session-creating path."""
        name = getattr(self.backend, "name", None)
        if name:
            return str(name)
        try:
            return self.state.backend
        except Exception:
            return None

    def _new_session(
        self,
        name: str,
        *,
        resume_session_id: str | None = None,
        backend: str | None = None,
        base_branch: str | None = None,
    ) -> None:
        # Any dialog this method needs must be shown NOW, while the OUTGOING session is still
        # active: `self.active = Session.bare()` below leaves the runner with no screen and no PTY,
        # so a popup opened after it renders NOTHING. The user then sees the previous popup frozen
        # on screen with every key doing nothing — aGiTrack looks dead, when it is really blocked
        # on an invisible dialog that a blind Enter would dismiss. (Hit via the --no-worktree
        # shared-tree caveat, which used to be raised from inside the branch below.)
        if not self._use_worktrees:
            self._warn_parallel_no_worktree_sessions()
        # The backend THIS RUN is using, read before the swap below empties `self.active`.
        # A new session inherits it. Seeding the new state from the global default alone was
        # not enough: `agitrack --backend claude` on a repo with no configured default never
        # recorded one, so the new state resolved to no backend at all and
        # `make_proxy_agent(self.state.backend)` raised "No coding agent backend is configured
        # for this session" — a crash mid-relocation, after the new worktree existed and
        # before the outgoing session's identity was restored, which left BOTH worktrees
        # claiming one conversation: exactly the corruption the relocation fix prevents.
        inherited_backend = backend or self._active_backend_name()
        # Fresh per-session runtime state; the outgoing active session keeps
        # its state on its own Session object in self.sessions.
        self.active = Session.bare()
        if self._use_worktrees:
            # The branch this new session merges into — its own, independent of the
            # other sessions and of the branch checked out in the repo directory.
            base = base_branch or self._merge_target_default()
            try:
                info, repo = self._open_session_worktree(name, base_branch=base)
            except Exception as error:
                self._set_message(f"Could not create worktree: {error}", seconds=8.0)
                self._render()
                return
            self.name = info.name
            self.worktree = info
            self.repo = repo
            self._base_branch = base  # this session integrates into `base` (per-session)
            self.turn = self._turn_from_branch(repo.current_branch())
            self.state = AgitrackState(
                info.path, default_backend=inherited_backend or self.global_config.default_backend
            )
            if base_branch is None:
                # No branch was explicitly chosen (e.g. resuming a dormant worktree): honor
                # any merge branch a prior run assigned it, confirming the change with the user.
                self._reconcile_merge_branch(base)
            else:
                self.state.merge_branch = base  # an explicit choice wins; record it
            started_msg = f"Started session '{info.name}' in .agitrack/worktrees/{info.path.name}"
        else:
            # --no-worktree: sessions run directly on the base tree with no isolation,
            # editing the same files in parallel (the user accepts this; they were warned above,
            # before the active session was swapped out). There is no worktree and no per-session
            # merge branch — commits land on the branch checked out in the directory, like the
            # startup no-worktree session.
            self.name = name
            self.worktree = None
            self.repo = self.base_repo
            self._base_branch = self.base_repo.current_branch()
            self.turn = 0
            self.state = AgitrackState(
                self.base_repo.repo, default_backend=inherited_backend or self.global_config.default_backend
            )
            if not resume_session_id:
                # A blank session is a FRESH conversation, not a continuation of whatever
                # the shared base-dir state last recorded (worktree sessions each got a
                # clean state dir; here the state file is shared, so reset it explicitly).
                self.state.backend_session_id = None
                self.state.last_backend_message_id = None
                self.state.new_agitrack_session_id()
                self.state.clear_trace()
            started_msg = f"Started session '{name}' (no worktree — shares this directory)"
        if backend:
            # Pin this session to a specific backend (e.g. created by switching
            # backends), independent of the global default.
            self.state.backend = backend
        if resume_session_id:
            # Resume this exact backend conversation.
            self.state.backend_session_id = resume_session_id
            self._persist_session_name(resume_session_id)
        self.backend = make_proxy_agent(self.state.backend)
        # Reopening a dormant worktree passes no conversation: it runs whatever its state file
        # says, so that file has to be trustworthy. Heal it here if an earlier aGiTrack left it
        # pointing at (or holding a copy of) a conversation that has since moved to a sibling
        # worktree — the "switched to the old session and got the new one" report. Before
        # staging, so a conversation this call is explicitly resuming is never touched.
        self._release_sibling_owned_conversations(keep=resume_session_id)
        if resume_session_id:
            # Stage the transcript into THIS session's directory before spawning, so a
            # `--resume` finds it. Crucial when the conversation last ran somewhere else:
            # a shared session imported under the base repo, or — when worktrees are off —
            # a session that previously ran in a worktree. `_stage_backend_resume` also
            # retargets the transcript's recorded cwd to this directory, so resuming a
            # worktree session under --no-worktree runs in the base dir, not the old
            # (now-gone) worktree path.
            self._stage_backend_resume(resume_session_id)
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        if os.name == "nt":
            # Clear the OUTGOING backend's host-terminal-mode mirror before the new backend comes
            # up, so this session starts from the same clean host baseline a fresh aGiTrack launch
            # would (mouse reporting, kitty, paste, …). Without this, modes the previous backend
            # mirrored onto the host persist; if they don't match what the new backend expects,
            # host-emitted mouse codes leak into it as literal text. The new backend re-establishes
            # only its own modes via _sync_terminal_modes as its output streams in. (Windows-only:
            # the POSIX session paths are long-proven and the leak is native-Windows specific.)
            self._disable_host_terminal_modes()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        self.sessions.append(self.active)
        self._resize_child()
        self._enable_host_mouse()
        self._reassert_host_raw()  # a switch can knock the host console out of raw mode
        self._set_message(started_msg)
        self._render()

    def _stop_session(self, index: int, *, commit: bool = True) -> None:
        if not (0 <= index < len(self.sessions)):
            return
        if len(self.sessions) <= 1:
            self._set_message("Cannot stop the only session.")
            self._render()
            return
        session = self.sessions[index]
        self._bg_relaunch_times.pop(id(session), None)  # drop its background crash-loop guard
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
                self.active.process.interrupt()  # SIGINT on POSIX, ConPTY ETX on Windows
                self._note_pid_for_reaping(self.child_pid)
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = None  # closes the ConPTY bridge socket on Windows (see run())
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
        # aGiTrack should stay up and relaunch+resume the conversation rather than
        # quitting with it. Guard against a crash loop: if the backend keeps dying
        # quickly, stop relaunching and exit normally.
        # Root-cause case: the backend refused to resume because the session is held
        # by a running background agent. Fork a copy (claude's own remedy) once,
        # instead of relaunching into the same refusal until the crash-loop guard
        # gives up. The forked id is discovered and adopted on the first parse (#114).
        if self._should_fork_after_busy_exit():
            self._forked_for_busy = True
            self._fork_next_spawn = True
            self.child_pid = None  # already gone
            try:
                self._restart_agent("Previous session is busy; forked a copy to continue.")
            except Exception as error:
                self._debug(f"fork relaunch failed, exiting: {error!r}")
                self._backend_exit_notice = self._format_backend_exit_notice()
                self._finalize_on_backend_exit()
                return False
            return True
        now = time.monotonic()
        recent = [t for t in self._relaunch_times if now - t < _RELAUNCH_WINDOW_SECONDS]
        if len(recent) >= 3:
            self._debug(f"backend exited 3x within {_RELAUNCH_WINDOW_SECONDS:.0f}s; quitting instead of relaunching")
            self._backend_exit_notice = self._format_backend_exit_notice()
            self._finalize_on_backend_exit()
            return False
        recent.append(now)
        self._relaunch_times = recent
        self.child_pid = None  # already gone
        # A backend that exits INSTANTLY (bad key, no network, apiKeyHelper still warming up
        # 1Password/biometric auth) can burn all 3 attempts in a fraction of a second, giving a
        # transient — but slightly slow — credential fetch no real chance to succeed on retry.
        # Backing off before each relaunch trades a bit of latency in the genuine crash-loop
        # case (which was going to fail anyway) for a real second and third chance here. Skip
        # the wait on the very first retry: most exits (Esc from the session picker, a clean
        # `/exit`) are not auth-related and should resume instantly.
        delay = _RELAUNCH_BACKOFF_SECONDS[min(len(recent) - 1, len(_RELAUNCH_BACKOFF_SECONDS) - 1)]
        if delay:
            time.sleep(delay)
        try:
            # _restart_agent tears down the dead PTY, re-baselines (so existing
            # history is not re-committed) and respawns; _spawn resumes the same
            # conversation via _should_continue_session.
            self._restart_agent("Backend exited — relaunched and resumed (Ctrl-C to quit aGiTrack).")
        except Exception as error:
            self._debug(f"relaunch failed, exiting: {error!r}")
            self._finalize_on_backend_exit()
            return False
        return True

    def _should_fork_after_busy_exit(self) -> bool:
        # True when the just-exited backend was claude refusing to resume a session
        # that is still running as a background agent, and we have not already forked
        # for this. Keyed off claude's own message so it tracks the CLI's behaviour
        # rather than guessing. State-guarded so it is inert in tests without state.
        if self._forked_for_busy:
            return False
        if getattr(self.state, "backend", None) != "claude":
            return False
        if not getattr(self.state, "backend_session_id", None):
            return False
        text = strip_ansi(self.last_child_output_sample.decode(errors="replace"))
        return bool(_FORK_HINT_RE.search(text))

    def _format_backend_exit_notice(self) -> str | None:
        # The backend died on launch several times in a row (e.g. the claude CLI refusing to
        # resume a session held by a running background agent, or — on Windows — a backend
        # that isn't actually launchable). In proxy mode that output lived on the alt-screen,
        # which the terminal restore discards, so the user otherwise just sees a flash and a
        # bare prompt (#114). Surface the EXACT failure: the command aGiTrack ran, what the
        # binary resolved to, the exit code, and the backend's own output (raw if it isn't
        # cleanly decodable) — plus a written log so nothing is lost to scrollback.
        backend = getattr(self.state, "backend", None) or "the backend"
        notice = f"aGiTrack: {backend} exited repeatedly on launch and was not relaunched."

        command = list(getattr(self, "_last_spawn_command", []) or [])
        if command:
            notice += f"\nCommand: {' '.join(command)}"
            try:
                from agitrack.proc import which_executable

                resolved = which_executable(command[0])
                notice += f"\nResolved '{command[0]}' to: {resolved or '(not found on PATH)'}"
            except Exception:  # diagnostics must never themselves crash the notice
                pass

        exit_code = self._last_backend_exit_code()
        if exit_code is not None:
            notice += f"\nExit code: {exit_code}"

        raw = self.last_child_output_sample
        text = strip_ansi(raw.decode(errors="replace"))
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if lines:
            tail = "\n".join("  " + line for line in lines[-20:])
            notice += f"\nLast output from {backend}:\n{tail}"
        elif raw:
            # No cleanly readable text (often just terminal control bytes) — show the escaped
            # raw tail so an early CreateProcess/cmd.exe error is still visible.
            escaped = raw[-400:].decode(errors="replace").replace("\x1b", "\\x1b")
            notice += f"\nRaw {backend} output (no readable text): {escaped!r}"
        else:
            notice += f"\n{backend} produced no output before exiting."

        log_path = self._write_backend_exit_log(backend, command, exit_code, raw)
        if log_path is not None:
            notice += f"\nFull details written to: {log_path}"
        notice += (
            "\nTry `agitrack --new-session` to start a fresh conversation, "
            "or `agitrack --json` to see the full backend error."
        )
        return notice

    def _last_backend_exit_code(self) -> int | None:
        # Best-effort exit code of the just-dead child (the platform child keeps it after the
        # process is gone); None when unavailable. Coerced to int so a test mock's poll()
        # can't leak a non-numeric value into the notice.
        process = getattr(self.active, "process", None) if getattr(self, "active", None) else None
        if process is None:
            return None
        try:
            code = process.poll()
        except Exception:
            return None
        return code if isinstance(code, int) else None

    def _write_backend_exit_log(self, backend: str, command: list[str], exit_code: int | None, raw: bytes):
        # Persist the full, unstripped backend output to the base repo's .agitrack/ so the
        # reason survives the alt-screen teardown and the user (or a bug report) can read it.
        try:
            path = self._diag_path("backend-exit")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                handle.write(f"backend: {backend}\n")
                handle.write(f"command: {command}\n")
                handle.write(f"resolved: {self._resolved_launch(command)}\n")
                handle.write(f"exit_code: {exit_code}\n")
                handle.write("--- raw output (utf-8, replace) ---\n")
                handle.write(raw.decode(errors="replace"))
            return path
        except Exception:  # diagnostics must never crash the exit path
            return None

    def _resolved_launch(self, command: list[str]):
        # What the platform layer actually launches for `command` — on Windows this exposes
        # the cmd.exe /c <shim> wrapping (or, tellingly, that the bare name was used because no
        # runnable shim was found). The single most useful line when a backend dies on startup.
        if not command:
            return command
        try:
            if sys.platform == "win32":
                from agitrack.proxy.platform.nt import _resolve_windows_command

                appname, args = _resolve_windows_command(command)
                return [appname, *args]
            from agitrack.proc import which_executable

            return [which_executable(command[0]) or command[0], *command[1:]]
        except Exception:
            return command

    def _finalize_on_backend_exit(self) -> None:
        # The only session's backend process is gone and we are NOT relaunching
        # (e.g. a real exit, or a crash loop). Commit/integrate its last turn and
        # persist the resume pointer before aGiTrack leaves, instead of just dropping
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
            RepoChangeHandler(self.repo.repo, self.file_change_event, self._git_wake),
            str(self.repo.repo),
            recursive=True,
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

    def _start_git_worker(self) -> None:
        """Start the dedicated git thread. From here on the automatic commit/merge
        pipeline for the foreground session runs there, never on the main reactor
        thread, so a `git status`/commit/merge can never block the user's typing."""
        self._stop_worker = False
        worker = threading.Thread(target=self._git_worker_loop, name="agitrack-git", daemon=True)
        self._git_worker = worker
        worker.start()

    def _stop_git_worker(self) -> None:
        """Stop and join the git worker. Idempotent. Called before exit-time finalize
        and before a restart re-exec so the foreground git that runs there can never
        race the worker.

        While joining, keep cancelling any dialog the worker is blocked on (returning
        None) so it can observe the stop and unwind — otherwise a worker waiting on an
        unanswered modal would never finish. Capped so teardown can't hang."""
        worker = self._git_worker
        if worker is None:
            return
        self._stop_worker = True
        self._git_wake.set()
        deadline = time.monotonic() + 10.0
        while worker.is_alive() and time.monotonic() < deadline:
            self._cancel_pending_modals()
            worker.join(timeout=0.05)
        self._git_worker = None

    def _cancel_pending_modals(self) -> None:
        """Unblock any worker waiting on a queued dialog by returning None for it.
        Used during teardown, where presenting the dialog is neither possible nor
        wanted."""
        while True:
            try:
                request = self._modal_mailbox.get_nowait()
            except queue.Empty:
                return
            request.result = None
            request.done.set()

    def _git_worker_loop(self) -> None:
        """The git thread's main loop: sleep on `_git_wake` (set by the file watcher
        on worktree writes, and once per reactor tick), then run one foreground
        pipeline pass under `_pipeline_lock`. Backs off to the idle cadence when the
        user has stepped away, so it doesn't spin (preserving the battery win).

        Two things keep this thread from getting in the user's way, because it holds the
        lock the SUBMIT path needs (`_acquire_pipeline_lock_from_main`):

        * ``GIT_PASS_MIN_INTERVAL`` floors the gap between passes. The reactor sets
          ``_git_wake`` once per loop iteration, so ``wait`` returned instantly and passes
          ran back to back — measured at ~74 per second, holding the lock essentially
          continuously for work whose own sub-checks are throttled to seconds anyway.
        * ``_pipeline_lock_wanted`` is the main thread saying "I need this now". Python
          locks are not fair, and a worker that re-acquires the instant it releases will
          barge ahead of a thread already waiting: with half-second passes the submit path
          waited up to 1.8 s. Yielding to it costs one deferred pass.
        """
        while self.running and not self._stop_worker:
            timeout = self.IDLE_POLL_SECONDS if self._is_idle() else self.POLL_SECONDS
            self._git_wake.wait(timeout=timeout)
            self._git_wake.clear()
            if not self.running or self._stop_worker:
                break
            # Sleep out the remainder of the interval in slices. NOT `_git_wake.wait()`: the
            # reactor re-sets that event every loop iteration, so waiting on it returns at once
            # and the floor never applies — which is how the rate got to ~74 passes/second in
            # the first place. Slices keep a stop responsive.
            deadline = self._last_git_pass + self.GIT_PASS_MIN_INTERVAL
            while self.running and not self._stop_worker:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))
            if not self.running or self._stop_worker:
                break
            while self._pipeline_lock_wanted and self.running and not self._stop_worker:
                time.sleep(0.005)  # the user is submitting; let the main thread through first
            self._last_git_pass = time.monotonic()
            try:
                with self._pipeline_lock:
                    if self.running and not self._stop_worker and self.master_fd is not None:
                        self._run_git_pipeline()
            except Exception as error:  # a git failure must never kill the worker
                self._debug(f"git worker pass failed: {error!r}")

    def _run_git_pipeline(self) -> None:
        """One foreground-session git pass — the work that used to run inline in the
        reactor's timers phase. Operates only on the active (foreground) session and
        never swaps `self.active`, so it can run concurrently with the main thread's
        stdin/render without misrouting. Any user dialog it raises is marshaled to the
        main thread via `_run_modal`; any repaint just flags `_render_pending`.

        Background / multi-session servicing (which DOES swap `self.active`) stays on
        the main thread, mutually excluded from this pass by `_pipeline_lock`.
        """
        if self._stop_worker or not self.running or self.master_fd is None:
            return
        if self.merge_ctx:
            # A foreground merge is being resolved; don't make normal commits meanwhile.
            self._maybe_complete_agent_merge()
            return
        self._maybe_agent_commit()
        self._poll_base_advanced()
        self._service_manual_commit_mode()  # --manual-commits: react to a user/external commit
        self._warn_if_base_edited()
        self._warn_if_cwd_drifted()

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
            started = time.monotonic()  # everything after select is work, not waiting
            self._stall_reported = 0.0
            # --- phase 2: pty-output --------------------------------------
            sentinel: str | int | None = self._reactor_pty_output_phase(readable)
            self._note_phase("pty-output", started)
            if sentinel == "continue":
                continue
            if sentinel == "break":
                break
            # --- phase 3: stdin -------------------------------------------
            sentinel = self._reactor_stdin_phase(readable)
            self._note_phase("stdin", started)
            if sentinel == "continue":
                continue
            if sentinel == "break":
                break
            # A stdin action may have torn the session down for a restart/exit (e.g. a
            # menu "update" → _apply_update_and_restart, which finalizes and REMOVES the
            # worktree). Stop now rather than fall through to the timers phase, whose
            # _ensure_worktree_alive / _maybe_agent_commit would run git in the deleted
            # worktree (FileNotFoundError). Mirrors the exit command's "break" above.
            if not self.running:
                break
            # --- phase 4: timers / background tasks -----------------------
            self._reactor_timers_phase()
            self._note_phase("timers", started)
            # A deferred update can apply here (sessions just went idle), tearing the
            # worktree down mid-phase; don't reap/inspect children against it.
            if not self.running:
                break
            # --- phase 5: child-exit --------------------------------------
            sentinel = self._reactor_child_exit_phase()
            self._note_phase("child-exit", started)
            if sentinel == "continue":
                continue
            if isinstance(sentinel, int):
                return sentinel
        return 0

    # How long one pass through the reactor's work phases may take before it is recorded as a
    # stall. While the loop is inside a phase it is NOT reading stdin, so the terminal's ~1 KB
    # input queue fills and then DISCARDS — which is why a stall surfaces to the user as stray
    # keystrokes and half a mouse report appearing all at once when it finally unblocks.
    # 0.5, not 2.0: E1's stalls measured 0.96 s and 2.71 s, so the 2.0 s floor recorded ONE of
    # them and left the other invisible — an intermittent latency bug is exactly the kind that
    # cannot be reproduced on demand, so the threshold has to sit below the smallest symptom
    # anyone has reported rather than above it. A keystroke that takes half a second to reach the
    # agent is already a user-visible freeze (bare codex over the same PTY answers in 0.25-0.44
    # MILLIseconds), and the note costs one monotonic() per phase.
    STALL_WARN_SECONDS = 0.5

    def _note_phase(self, phase: str, started: float) -> None:
        """Record how long the reactor has been away from ``select`` this iteration.

        A user-visible freeze ("pressing Enter took half a minute") is always some call inside
        one of these phases refusing to return, and after the fact there is nothing left to say
        which. This names the phase and the elapsed time, unconditionally — the whole point is
        to catch a stall nobody can reproduce on demand, so it must not need DEBUG_PROXY to
        have been set in advance. It costs one ``monotonic()`` per phase."""
        elapsed = time.monotonic() - started
        if elapsed < self.STALL_WARN_SECONDS:
            return
        if elapsed <= self._stall_reported:
            return  # already reported this iteration at a shorter prefix; only report growth
        self._stall_reported = elapsed
        self._stall_worst = max(self._stall_worst, elapsed)
        self._debug(f"reactor stalled {elapsed:.3f}s through phase {phase}")
        try:
            from agitrack.proxy.crash import write_stall_note

            write_stall_note(self._crash_report_root(), phase, elapsed)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Reactor phases (called exclusively from _loop)
    # ------------------------------------------------------------------

    def _select_timeout(self) -> float:
        """How long the reactor blocks in ``select`` this iteration.

        ``select`` returns the instant stdin or any PTY fd becomes readable, so
        this timeout only bounds how long we may sit idle before re-running the
        timer-driven work — it never delays the user's keystrokes or the
        backend's output. The value is the time until the *next* thing that must
        happen:

        * a queued repaint → ``0.016`` (coalesced output frame, ~next tick);
        * a deferred prompt-submit (the injected Enter, scheduled 0.4s out) →
          the exact time remaining to it, so it still fires promptly even though
          the normal active poll is a full second;
        * otherwise ``ACTIVE_POLL_SECONDS`` while there is work to service, or
          ``IDLE_POLL_SECONDS`` once :meth:`_is_idle` says nothing does — the
          long idle block is what lets the CPU sleep and saves battery while the
          user is away.

        Every background sweep self-throttles to its own interval (≥2s), so the
        active poll being a coarse 1s never makes any check run more often.
        """
        if self._input_tail:
            # A partial escape sequence is being held. Wake when its hold expires so the
            # Escape key is delivered on time even if no other input ever arrives.
            return max(0.0, self._input_tail_at + self.INPUT_TAIL_HOLD - time.monotonic())
        if self._parse_at < len(self._parse_carry):
            return 0.0  # backend output still unparsed: come straight back for it
        if self._render_pending or self._child_input_pending():
            # Input a backend's full PTY queue refused is owed to it: come back promptly to
            # push the rest rather than leaving the user's keystrokes parked for a whole tick.
            return 0.016
        if self._pending_enter_at is not None:
            remaining = self._pending_enter_at - time.monotonic()
            return max(0.0, min(remaining, self.ACTIVE_POLL_SECONDS))
        return self.IDLE_POLL_SECONDS if self._is_idle() else self.ACTIVE_POLL_SECONDS

    def _is_idle(self) -> bool:
        """True when no foreground or background work needs the fast poll loop.

        Idle means: no turn in flight (active or background), no merge being
        resolved, no per-turn pipeline work pending (file change to process,
        parse/commit due, deferred Enter or queued prompt), no background worker
        thread alive, no timed notice still on screen, and no keystroke or
        backend output within ``IDLE_AFTER_SECONDS``. A sticky message (one that
        waits for a keypress) does NOT keep us awake — the keypress that
        dismisses it wakes ``select`` on its own.
        """
        now = time.monotonic()
        # An active turn, or output recent enough to still be inside the
        # post-turn commit-debounce window, keeps the fast loop.
        if self.agent_in_flight or self.merge_ctx is not None:
            return False
        if now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return False
        if now - self.last_user_input < self.IDLE_AFTER_SECONDS:
            return False
        # Pending per-turn pipeline work. file_change_event is set by the file
        # watcher thread (it does not wake select), so it must be checked here.
        if self.file_change_event.is_set() or self.parse_pending or self.status_check_pending:
            return False
        if self.pending_forwarded or self.pending_prompt_text:
            return False
        if self._pending_enter_at is not None or self._base_advanced:
            return False
        # A timed notice must expire/refresh on schedule; a sticky one need not.
        if self.message is not None and not self._message_sticky and now < self.message_until:
            return False
        for attr in (
            "agent_parse_thread",
            "_summary_thread",
            "_precompact_thread",
            "_auto_share_thread",
            "_shared_resume_thread",
            "_update_check_thread",
        ):
            thread = getattr(self, attr, None)
            if thread is not None and thread.is_alive():
                return False
        # Any *background* session still working (the active one is covered above).
        # Background turns are committed/integrated by _service_background_sessions
        # once they have been quiet for CHILD_IDLE_SECONDS, with POLL_SECONDS between
        # service passes; stay non-idle a little past that window so a just-finished
        # background turn is integrated promptly rather than waiting a full idle tick.
        background_quiet = self.CHILD_IDLE_SECONDS + self.POLL_SECONDS + 1.0
        for index in range(len(self.sessions)):
            if index == self.active_index:
                continue
            session = self.sessions[index]
            if getattr(session, "agent_in_flight", False) or getattr(session, "merge_ctx", None) is not None:
                return False
            if now - getattr(session, "last_child_output", 0.0) < background_quiet:
                return False
        return True

    def _reactor_select_phase(self) -> tuple[dict, list]:
        """Phase 1 — compute the fd set, block in select, drain background PTYs.

        Returns (background_fds_map, readable_list).  Background sessions are
        drained here so their PTY buffers never fill up regardless of which
        phase the main loop is in.

        On POSIX: watches stdin fd, PTY master fd, background session fds, and
        the self-pipe wake fd.  On Windows: watches the stdin socket bridge, the
        pywinpty output socket bridge, background sockets, and the wake socket.
        All are sockets (or POSIX fds on POSIX) and thus selectable.
        """
        timeout = self._select_timeout()
        background = self._background_fds()
        stdin_fd = self._stdin_fileno()
        # Filter out None (unspawned / torn-down session) so select never sees it.
        watch = [fd for fd in [stdin_fd, self.master_fd, *background] if fd is not None]
        if self._wake_r >= 0:
            watch.append(self._wake_r)  # the git worker's wake channel (e.g. it queued a dialog)
        readable, _, _ = select.select(watch, [], [], timeout)
        if self._wake_r in readable:
            if self._waker is not None:
                self._waker.drain()  # drain the wake byte(s); presence is the signal
            else:
                try:
                    os.read(self._wake_r, 4096)
                except OSError:
                    pass
        # On Windows a resize is delivered by the host's watcher, not SIGWINCH; pick it up
        # here (POSIX always returns False — it uses the SIGWINCH handler instead).
        if self._host is not None and self._host.consume_resize_pending():
            self._resize_child()
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
            self._forward_clipboard_osc(output)
            self._track_sync_update(output)
            self._feed_child_output(output)
            self._render_output()
        return None

    def _reactor_stdin_phase(self, readable: list) -> "str | None":
        """Phase 3 — read stdin and route bytes to the active session or handler.

        Returns a loop-control sentinel or None to continue normally.
        """
        stdin_fd = self._stdin_fileno()
        # A held escape-sequence tail whose hold has expired is delivered even though nothing
        # is readable — that is what keeps the Escape KEY working now that a lone trailing ESC
        # is held (see _hold_incomplete_tail). Everything below then runs on an empty read.
        flush_tail = stdin_fd not in readable and self._input_tail_expired()
        if stdin_fd not in readable and not flush_tail:
            # Diagnostic (throttled): if a hang ever recurs where the loop spins but no keystroke
            # is delivered, this records whether the host's console→bridge reader thread is alive.
            if self.debug_proxy:
                now = time.monotonic()
                if now - getattr(self, "_dbg_stdin_idle_at", 0.0) > 5.0:
                    self._dbg_stdin_idle_at = now
                    stats = "n/a"
                    if self._host is not None and hasattr(self._host, "stdin_reader_stats"):
                        stats = self._host.stdin_reader_stats()
                    self._debug(f"stdin idle: fd={stdin_fd} reader[{stats}]")
            return None
        if flush_tail and self._input_tail_debris:
            # The hold expired on an ESC that arrived glued to a run of wheel reports and was
            # never continued: the tty threw the rest of that report away when its queue
            # overflowed. Forwarding it is a spurious Escape at the backend — the thing that
            # takes Claude's session down mid-scroll. Drop it; it was never a keystroke.
            self._debug(f"dropped a truncated mouse report's ESC (tail={self._input_tail!r})")
            self._arm_mouse_resync(self._input_tail)  # its other half is still in the queue
            self._input_tail, self._input_tail_at, self._input_tail_debris = b"", 0.0, False
            return None
        data = b"" if flush_tail else self._read_stdin(4096)
        # Only a real keystroke resets the idle backoff — NOT a mouse wheel / move /
        # click or a focus in/out report. Scrolling history is passive reading: it
        # needs no commits or background polling, so it must not pin aGiTrack in the
        # active loop. (select still woke instantly to redraw the scrolled view; this
        # only governs whether we then drop back to the low-power idle cadence.)
        # On a tail flush the read is empty and the keystroke (an Escape) is the held tail,
        # so judge that instead — otherwise pressing Escape would not count as user activity.
        if self._is_real_keypress(data or (self._input_tail if flush_tail else b"")):
            self.last_user_input = time.monotonic()
        # (stdin bytes are raw-captured centrally in _read_stdin so modal/wait reads are logged too)
        self._debug(f"stdin: {data!r} menu_key={self.input.menu_key!r}")
        # A popup message taller than the screen scrolls with PgUp/PgDn, handled before
        # anything else so a long notice can be read in full (and isn't dismissed mid-read).
        if self._message_max_scroll > 0 and not self.input.capturing and _PAGE_KEY_RE.search(data):
            page = max(self.rows - 4, 1)
            for match in _PAGE_KEY_RE.finditer(data):
                step = page if match.group(1) == b"6" else -page  # PgDn scrolls down, PgUp up
                self._message_scroll = max(0, min(self._message_scroll + step, self._message_max_scroll))
            data = _PAGE_KEY_RE.sub(b"", data)
            self.message_until = time.monotonic() + 30  # keep the notice up while scrolling
            self._render()
            if not data:
                return None
        # Any keypress dismisses a sticky message (e.g. the auto-commit
        # confirmation) so it no longer overlays the live view; repaint to
        # remove the popup even if the key produces no child echo.
        if self._clear_sticky_message_on_input():
            self._render_pending = True
        # A held ESC already known to be the head of a report the tty truncated is dropped as
        # it rejoins the stream. It has no complete report in FRONT of it here (it leads the
        # chunk), so _strip_aborted_mouse_reports cannot recognise it — and widening that
        # pattern to catch it would eat the Escape KEY whenever a wheel report shared the read.
        # The flag settles it instead: it was decided against the chunk the ESC really came from.
        # ...unless the missing remainder turned up after all: joining them back into a whole
        # report is strictly better than dropping the ESC and orphaning the rest as literal text.
        if self._input_tail == b"\x1b" and self._input_tail_debris and not _SGR_REPORT_RE.match(b"\x1b" + data):
            self._debug("dropped a truncated mouse report's ESC on rejoin")
            self._input_tail, self._input_tail_debris = b"", False
        data = self._input_tail + data
        if not flush_tail:
            # A report we gave up on last time leaves its back half in the queue, arriving as
            # its own ESC-less read. Consume it before anything else looks at these bytes.
            data = self._consume_mouse_resync(data)
        held = self._input_tail
        data, self._input_tail = self._hold_incomplete_tail(data, flush=flush_tail)
        if flush_tail:
            self._arm_mouse_resync(held)  # we just gave up mid-report; expect its other half
        self._input_tail_at = time.monotonic() if self._input_tail else 0.0
        # Remember WHY a lone ESC is being held, while the chunk it came from is still in hand:
        # sitting at the end of a run of wheel reports it is the head of one more report, not
        # the user pressing Escape (which arrives in a read of its own).
        self._input_tail_debris = self._input_tail == b"\x1b" and _MOUSE_REPORT_TAIL_RE.search(data) is not None
        data = self._intercept_scroll(data)
        self._debug(f"after intercept: {data!r}")
        # Decode kitty keyboard protocol control keys back to plain bytes.
        # When the terminal is in kitty mode (enabled by the backend), Ctrl-A
        # through Ctrl-Z are sent as escape sequences like \x1b[97;5u instead
        # of plain bytes like \x01. We decode them so the menu key matching works.
        data = _decode_kitty_ctrl_keys(data)
        self._debug(f"after kitty decode: {data!r}")
        # An ANSWER from the terminal is not a keystroke. Absorb any that reach the reactor.
        data = self._absorb_host_terminal_reply(data)
        was_capturing = self.input.capturing
        # Snapshot the palette's visible state so we can tell a real change (typing, selection move)
        # from no-op input like mouse-motion reports — and avoid repainting the palette on the latter.
        prev_palette = (bytes(self.input.buffer), self.input.selected_index) if was_capturing else None
        forwarded, local_echo, command, should_exit = self.input.feed(data)
        self._debug(f"feed result: forwarded={forwarded!r} command={command!r} capturing={self.input.capturing}")
        if getattr(self.input, "suspend_requested", False):
            self.input.suspend_requested = False
            self._suspend_to_shell()
            return "continue"
        if self.input.capturing and not was_capturing:
            # The Ctrl-G palette just opened: surface a top-level "merge" command when
            # there is unmerged worktree work, so the user is reminded to rescue it
            # before it gets lost. Recomputed each open so it reflects current state.
            self.input.extra_commands = ["merge"] if self._has_unmerged_work() else []
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
            # Repaint only when the palette actually changed (just opened, or the typed text /
            # selection moved). A no-op feed — e.g. a mouse moving over the palette, whose motion
            # reports are dropped — leaves the state identical, so we skip the repaint that used to
            # flash the palette and slow it down on every mouse move.
            if not was_capturing or (bytes(self.input.buffer), self.input.selected_index) != prev_palette:
                self._render()
        elif was_capturing and command is None:
            self._render()
        if forwarded:
            self.scroll_back = 0  # interacting snaps back to the live view
            submit = self._forwarded_submits(forwarded)
            self._update_passthrough_prompt(forwarded)
            submitted_prompt = ""
            # Everything below is the SUBMIT PATH, and it is on the clock: the user is
            # watching for their prompt to be sent. It asks the same handful of read-only
            # git questions through several independent helpers — five `rev-parse
            # --abbrev-ref HEAD`, three `diff --quiet`, three `ls-files --others` for one
            # Enter — and on Windows a process spawn is ~38 ms, so those repeats WERE the
            # latency (740 ms measured, 100% of it in git). One scope de-duplicates them;
            # any command that writes — on this thread or on the git worker — drops the
            # cache from under it (see git/repo.py's read_cache).
            with git_read_cache():
                if submit:
                    submitted_prompt = self.passthrough_prompt.decode(errors="ignore").strip()
                    if submitted_prompt.startswith("/compact"):
                        self._handle_pre_compaction()
                    # Submit-time commits touch the foreground worktree's index, the same
                    # one the git worker commits the agent's turn into — take the pipeline
                    # lock so the two never run at once (draining dialogs while we wait).
                    self._acquire_pipeline_lock_from_main()
                    try:
                        if not self._pre_agent_commit_if_needed(submitted_prompt):
                            self.pending_forwarded = [chunk for chunk in forwarded if chunk in {b"\r", b"\n"}]
                            self.pending_prompt_text = submitted_prompt
                            forwarded = [chunk for chunk in forwarded if chunk not in {b"\r", b"\n"}]
                            submit = False
                    finally:
                        self._pipeline_lock.release()
                if submit:
                    self.passthrough_prompt.clear()
                    self.passthrough_escape = None
                if forwarded:
                    if submit:
                        self._begin_agent_turn()
                        if submitted_prompt:
                            # Flush the previous turn's deferred integration first, then
                            # put this new prompt on its own branch (same shared index as
                            # the worker → under the pipeline lock).
                            self._acquire_pipeline_lock_from_main()
                            try:
                                self._integrate_committed_turn_before_new_turn()
                                self._ensure_turn_branch()
                            finally:
                                self._pipeline_lock.release()
                            # Drop the previous turn's "created & merged" status line so
                            # it doesn't linger into — and read as belonging to — the
                            # new turn (which would show "created" before "summarizing").
                            self._set_session_notice(self._session_label(), None)
                    self.active.process.write(b"".join(forwarded))
        if command:
            self._run_command(command)
            # An "exit"/"quit" command runs the same finalize-and-teardown flow as
            # Ctrl-C, which removes the worktree. It must break the loop here just
            # like the Ctrl-C path above — otherwise this iteration falls through to
            # the timers phase and _maybe_agent_commit runs git in the deleted
            # worktree (FileNotFoundError on exit).
            if self._exit_requested:
                return "break"
        return None

    def _reactor_timers_phase(self) -> None:
        """Phase 4 — main-thread servicing. The foreground commit/merge pipeline runs
        on the git worker (`_run_git_pipeline`); this phase keeps only terminal I/O,
        prompt forwarding, dialog hand-off, and the background / multi-session git
        that swaps `self.active` (guarded by `_pipeline_lock`, non-blocking so a busy
        worker never stalls the reactor — and thus never delays typing)."""
        if not self.running:
            return  # a restart/exit is underway; touch no (possibly-removed) worktree
        self._check_shutdown_request()
        if not self.running:
            return  # an external graceful-shutdown request just fired
        self._run_pending_session_switch()  # a cross-backend switch deferred out of the sessions modal
        self._drain_parse_carry()  # backend output past the last pass's parse budget
        self._flush_child_input()  # input a backend's full PTY queue would not take yet
        self._flush_pending_render()
        self._flush_pending_enter()
        self._drain_modal_mailbox()  # present any dialog the git worker queued
        self._present_pending_copy_offer()  # the turn's "copy stranded files?" popup, if any
        self._present_pending_env_copy()  # "copy the full environment into the new worktree?" popup
        self._present_pending_env_refresh()  # "base has newer files — update the worktree?" popup
        self._present_pending_noworktree_cleanup()  # "--no-worktree: delete leftover worktree dirs?" popup
        self._resume_pending_prompt_if_ready()
        self._service_shared_resume()  # complete a shared-session resume once fetched
        # Background / multi-session git (these swap self.active via _with_session, so
        # they must be mutually exclusive with the worker's foreground pass). Skip the
        # whole cluster this tick when the worker holds the lock — it runs on the next.
        if self._pipeline_lock.acquire(blocking=False):
            try:
                self._ensure_worktree_alive()  # recovers a vanished worktree (PTY/watcher work)
                self._follow_agent_worktree_branch()  # follow an agent `git checkout` in the worktree
                self._check_base_branch_drift()  # may surface a dir-change session swap
                self._service_commit_summaries()  # apply finished background summaries (#8)
                self._service_precompact_summary()
                self._service_background_sessions()
                self._service_deferred_switch_offer()  # a switch's copy/commit offer, once reconciled
                self._service_native_session_switch()  # /clear, /resume, OpenCode's picker
                if self._base_advanced:
                    self._base_advanced = False
                    self._sync_idle_worktrees_to_base()
            finally:
                self._pipeline_lock.release()
        self._maybe_check_for_update()
        self._maybe_apply_pending_update()
        if not self.running:
            # _maybe_apply_pending_update finalized and removed the worktree for a
            # restart; stop before any further servicing.
            return
        self._service_background_share_ops()  # surface finished background unshare/etc. results
        self._service_auto_share_outcome()  # surface a live auto-share failure (else it's silent)
        self._service_share_conflicts()  # prompt overwrite/merge for a share refused as behind
        self._service_backend_update()  # surface a finished backend auto-update result
        self._maybe_auto_update_backend()  # auto-apply a brew-managed backend update; once per backend
        self._service_session_notices()  # expire/refresh per-session status lines
        self._git_wake.set()  # nudge the worker so its pass tracks the reactor's cadence

    def _reactor_child_exit_phase(self) -> "str | int | None":
        """Phase 5 — reap stopped children and check whether the active child exited.

        Returns a loop-control sentinel (``"continue"``, an ``int`` exit code),
        or ``None`` to proceed normally.
        """
        self._reap_stopped_children()  # collect SIGINT'd backends as they exit
        if self.child_pid is not None:
            # Non-blocking exit check via the platform child (POSIX waitpid / Windows
            # ConPTY isalive) — os.waitpid/os.WNOHANG don't exist on native Windows.
            exit_code = self.active.process.poll()
            if exit_code is not None:
                sample = self.last_child_output_sample[-512:].decode(errors="replace").replace("\x1b", "\\x1b")
                self._debug(f"child exited pid={self.child_pid} exit_code={exit_code} last_output={sample!r}")
                # Same handling as the master_fd-EOF path: switch away (multi
                # session) or relaunch+resume (single) so Claude exiting its own
                # picker on Esc doesn't take aGiTrack down. These two detectors race;
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

    def _debug_session_liveness(self, now: float) -> None:
        # (debug-gated, throttled) Snapshot every session's backend + whether its child
        # process is still alive, to proxy-debug. Diagnoses reports of a session silently
        # stopping while backgrounded ("Claude stops after I switch away"): the log then shows
        # exactly which session's process exited and when. Only the active session is skipped
        # (left as "active") so poll() never disturbs the foreground backend.
        if not self.debug_proxy:
            return
        if now - getattr(self, "_dbg_liveness_at", 0.0) < 5.0:
            return
        self._dbg_liveness_at = now
        parts = []
        for index, session in enumerate(self.sessions):
            backend = getattr(getattr(session, "backend", None), "name", "?")
            if index == self.active_index:
                state = "active"
            else:
                proc = getattr(session, "process", None)
                try:
                    state = "exited" if (proc is not None and proc.poll() is not None) else "alive"
                except Exception:
                    state = "err"
            parts.append(f"{index}:{backend}:{state}")
        self._debug("session liveness " + " ".join(parts))

    def _diag_path(self, kind: str):
        # Diagnostic logs live in the *base* repo's .agitrack/ (one file per run), not
        # the session worktree's — the worktree is removed on exit, which would
        # both destroy the log and recreate a half-dir that breaks the next resume.
        base = self.base_repo
        root = (base.repo if base is not None else self.repo.repo) / ".agitrack"
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
        # Link an aGiTrack-born conversation's transcript into the base repo's project
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
        # The backend spawns with its cwd at self.repo.repo, but a resumed transcript
        # can carry an OLD recorded cwd (e.g. a worktree this session previously ran
        # in) that Claude's --resume would restore — leaving it operating in the wrong
        # directory, most visibly under --no-worktree. Align the transcript's cwd with
        # the launch dir (a no-op when they already match, so the normal in-worktree
        # resume is untouched and its shared hardlink is preserved).
        retarget = getattr(self.backend, "retarget_working_dir", None)
        if retarget is not None:
            # Pass the launch dir's current branch so retargeting a worktree session onto the base
            # repo (the --no-worktree switch) also moves the stale ``agitrack/…`` worktree branch
            # off the relocated rows — otherwise every row keeps the worktree branch and the resumed
            # agent still reads its whole history as "in a worktree." Best-effort: None just skips it.
            try:
                launch_branch = self.repo.current_branch()
            except Exception as error:
                self._debug(f"resume branch lookup failed: {error!r}")
                launch_branch = None
            try:
                if retarget(self.repo.repo, session_id, str(self.repo.repo), git_branch=launch_branch):
                    self._debug(f"retargeted resumed session {session_id} cwd to {self.repo.repo}")
            except Exception as error:
                self._debug(f"retarget_working_dir failed: {error!r}")

    def _init_screen(self) -> None:
        self.rows, self.cols = self._terminal_size()
        # HistoryScreen keeps lines that scroll off the top so aGiTrack can offer
        # scrollback for backends that stream to the normal screen (Claude).
        self.screen = _BackgroundColorEraseScreen(self.cols, max(self.rows - 1, 1), history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.scroll_back = 0
        self._in_sync_update = False

    def _forward_clipboard_osc(self, output: bytes) -> None:
        # Forward the backend's OSC 52 clipboard writes to the host terminal. aGiTrack renders
        # the active session's screen via pyte, which consumes OSC sequences and re-emits only
        # the visible grid — so a backend's "copy to clipboard" (e.g. Claude's, which emits
        # OSC 52) is otherwise swallowed and the clipboard never changes, even though the backend
        # reports it copied. Only the FOREGROUND session reaches this path, so a background
        # session can't hijack the clipboard. Forwarded verbatim; the host terminal honors it
        # exactly as it does for a backend whose output isn't pyte-rendered.
        for seq in _OSC52_RE.findall(output):
            try:
                os.write(sys.stdout.fileno(), seq)
            except OSError:
                pass

    # How much backend output may be parsed in ONE reactor pass. `drain()` hands over up to
    # 256 KB, and pyte is ~2.5 ms per 4 KB of coloured TUI output, so an unbounded feed held
    # the GIL for ~107 ms — three times the repaint budget the renderer is so careful about.
    # Nothing else can run in that window, including the stdin reader thread, so the tty's
    # ~1 KB queue (about 85 wheel reports) overflows and the kernel discards INSIDE a single
    # feed. That is the "stray text in the input box" leak, and it is why bounding the parse
    # matters more than any filter downstream. 16 KB is ~6 ms — comfortably inside the duty
    # cycle measured to lose nothing.
    PARSE_MAX_BYTES = 8192

    def _feed_child_output(self, output: bytes) -> None:
        """Parse backend output into the screen model, bounded per pass.

        Anything over the bound is carried to the next pass IN ORDER — never dropped, never
        reordered. Splitting between feeds is safe because pyte's stream parser keeps its state
        across calls, so a sequence cut in half is resumed rather than mis-parsed."""
        if output:
            # Compact before appending. Consuming with an OFFSET rather than deleting from the
            # front matters: `del buf[:n]` is O(remaining), which made a 400 KB backlog
            # quadratic and pushed the worst pass to 46 ms — worse than the unbounded feed it
            # was meant to bound.
            self._parse_carry = self._parse_carry[self._parse_at :] + output
            self._parse_at = 0
        if self._parse_at >= len(self._parse_carry):
            return
        chunk = self._parse_carry[self._parse_at : self._parse_at + self.PARSE_MAX_BYTES]
        self._parse_at += len(chunk)
        if self._parse_at >= len(self._parse_carry):
            self._parse_carry, self._parse_at = b"", 0
        ScreenRenderer.feed(self, chunk, pyte_hostile_csi_re=_PYTE_HOSTILE_CSI_RE)

    def _drain_parse_carry(self) -> bool:
        """Parse the next bounded slice of carried-over output. True while more is owed, which
        keeps the reactor on the fast poll so the screen never lags the backend."""
        if self._parse_at >= len(self._parse_carry):
            return False
        self._feed_child_output(b"")
        self._render_output()
        return self._parse_at < len(self._parse_carry)

    def _sync_terminal_modes(self, output: bytes) -> None:
        # OpenCode enables mouse reporting on its PTY. Because aGiTrack renders the
        # screen itself, the host terminal never sees those mode switches unless we mirror them.
        # Mirror button/wheel/SGR/paste and 1002 (button-event tracking = motion WHILE a button is
        # held = drag, which the backend needs for drag-select / copy). But on Windows do NOT
        # mirror 1003 (any-event tracking = motion with NO button = hover): the host floods hover
        # reports (`\x1b[<35;..M`) that don't round-trip cleanly and leak into the backend as
        # literal text. Excluding only 1003 stops the hover flood while keeping drag-copy working
        # (the confirmed leak is button 35 = no-button motion; see the proxy-raw trace and
        # devtools/winmouse/FINDINGS.md). Disables are always mirrored.
        #
        # CHEAP REJECT FIRST, exactly as `_answer_terminal_queries` does. Everything below is a
        # private-mode set/reset or a keyboard-protocol negotiation, and every one of those
        # starts `ESC [` + one of `? > < =`. Without this guard the method ran THIRTY full
        # substring scans plus a regex over every chunk the backend produced — measured 4.46 ms
        # per megabyte of ordinary scroll output containing no escape sequence in it at all, on
        # the reactor thread that also reads stdin. One pass decides it instead: 4.46 -> 0.29 ms.
        # (A regex, not four `in` checks: those cost 1.97 ms for the same buffer, because each
        # is its own full scan, while the regex's leading literal lets it memchr for ESC once.)
        # A sequence split across two chunks is missed either way — the scans below are
        # whole-buffer too — so the guard changes nothing about what is detected.
        if not _TERMINAL_MODE_MARKER_RE.search(output):
            return
        _no_host_enable = {b"1003"} if os.name == "nt" else set()
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
            if b"\x1b[?" + mode + b"h" in output and mode not in _no_host_enable:
                self._mirror_to_host(b"\x1b[?" + mode + b"h")
            if b"\x1b[?" + mode + b"l" in output:
                self._mirror_to_host(b"\x1b[?" + mode + b"l")
        # Track whether the backend drives the mouse itself. If it does, wheel
        # events (and all other mouse events) are forwarded to it; if not, aGiTrack uses the
        # wheel for scrollback. This applies on Windows too: the ConPTY input path DOES
        # deliver clean SGR mouse to a VT-input backend (verified end-to-end against claude and
        # opencode — see devtools/winmouse/FINDINGS.md), so a mouse-driving backend owns the mouse
        # the same way it does on POSIX.
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
            seq = match.group(0)
            # Kitty-protocol sequences end in ``u``; only mirror them to a host that
            # speaks the protocol, or they leak as visible codes (the modifyOtherKeys
            # ``...m`` form is an ordinary CSI any terminal consumes, so always mirror it).
            if seq.endswith(b"u") and not self.host_kitty_keyboard:
                continue
            self._mirror_to_host(seq)

    def _mirror_to_host(self, seq: bytes) -> None:
        # Write a backend-requested terminal mode to the host AND raw-capture it (tag "M") so a
        # DEBUG_RAW trace shows exactly which mouse/keyboard-protocol modes were pushed to the host
        # — the evidence for a mode that one backend enables and a switch leaves stuck for the next.
        self._raw_capture("M", seq)
        os.write(sys.stdout.fileno(), seq)

    def _detect_host_terminal(self) -> None:
        # Pre-reactor: called once during run() startup, before _loop() starts.
        # The bounded 0.5 s select-and-read loop inside terminal.py is correct
        # here because no PTY children are running yet and no reactor iteration
        # is live.  Do NOT convert to a modal — there is nothing to drain yet.
        debug_fn = self._debug if self.debug_proxy else None
        if self._host is not None:
            self._host.detect_host_terminal(debug_fn=debug_fn)
        else:
            TerminalHost.detect_host_terminal(self, debug_fn=debug_fn)
        # SOME TERMINALS NEVER ANSWER. macOS Terminal.app is the common one: it draws true
        # colour perfectly well but implements no colour REPORT, so the query above times out
        # every launch. With nothing to relay, the backend fell back to its own default — dark —
        # and painted a dark UI in a white terminal, and aGiTrack could not tell that was wrong
        # because it had no background to compare the agent against either. Ask the platform
        # instead; `detect_host_background` is read-only and never prompts. Only a terminal that
        # stayed silent is asked, so a real answer is never second-guessed.
        self._seed_derived_host_background()
        # If the menu key requires shift modifier, enable the kitty keyboard protocol
        # so the terminal sends distinguishable escape sequences. This has to be HERE, after
        # detection: `_enable_kitty_keyboard` does nothing unless `host_kitty_keyboard` says
        # the terminal speaks the protocol, and that is exactly what detection just found out.
        # It lived inside `_seed_derived_host_background` for a while, which broke it outright
        # in both directions — that method returns early once a background is known, so a
        # terminal that ANSWERS OSC 11 never reached it at all, and the pre-detection call
        # (right after the spawn) ran it before `host_kitty_keyboard` was known and it
        # no-opped. Either way a shift-modified menu key stopped being distinguishable.
        if self.global_config is not None and self.global_config.is_shift_modified:
            self._enable_kitty_keyboard()

    def _seed_derived_host_background(self) -> None:
        """Fill in the terminal's background from the platform when it will not report one.

        SOME TERMINALS NEVER ANSWER `OSC 11`. macOS Terminal.app is the common one: it draws
        true colour perfectly well but implements no colour REPORT, so detection waits out its
        whole timeout on every launch and learns nothing. With no background to relay, the
        backend falls back to its own default — dark — and paints a dark UI in a white terminal.

        Called twice on purpose: once immediately after the spawn, so the responder has an
        answer before the backend's question arrives, and again after `_detect_host_terminal`,
        for the terminal that answers but whose reply carried no usable colour. Only a value we
        do not already have is filled in, so a real reply is never overwritten by a derived one.
        """
        if self.host_bg_value:
            return
        from agitrack.proxy.host_background import detect_host_background

        try:
            derived = detect_host_background()
        except Exception as error:  # a colour hint is never worth failing startup
            self._debug(f"host background derivation failed: {error!r}")
            return
        if derived:
            self.host_bg_value = derived
            self._debug(f"host background derived from the platform: {derived!r}")

    def _enable_kitty_keyboard(self) -> None:
        # Proactively enable the kitty keyboard protocol for shift-modified menu keys.
        # This allows Ctrl+Shift+<letter> to be distinguished from Ctrl+<letter>.
        # The protocol is: CSI > flags u, where flags=1 means "disambiguate escape codes"
        if not self.host_kitty_keyboard:
            # The host doesn't speak the protocol; sending CSI > 1 u would just leak
            # as a visible code at startup without enabling anything.
            return
        try:
            os.write(sys.stdout.fileno(), b"\x1b[>1u")
            self._kitty_keyboard_enabled = True
            if self.debug_proxy:
                self._debug("Enabled kitty keyboard protocol for shift-modified menu key")
        except OSError:
            # Terminal doesn't support it or write failed
            if self.debug_proxy:
                self._debug("Failed to enable kitty keyboard protocol (terminal may not support it)")

    def _parse_host_terminal_responses(self, data: bytes) -> None:
        TerminalHost.parse_host_terminal_responses(self, data, debug_fn=self._debug if self.debug_proxy else None)

    def _absorb_host_terminal_reply(self, data: bytes) -> bytes:
        """Take the host terminal's ANSWERS out of the input stream, and learn from them.

        `detect_host_terminal` writes its queries and waits a bounded time for the replies; a
        terminal slower than that (tmux relaying to an outer terminal, a forwarded/remote session)
        answers afterwards, and the answer then arrives as ordinary input. That is not benign: an
        OSC reply is terminated by BEL, and BEL is 0x07 — Ctrl-G, the default menu key. A late
        background-colour report opened the command palette by itself, and the rest of the reply
        was typed at the agent. So a reply is consumed here rather than fed to the key handler,
        and the values in it still reach the capability cache, which repairs the theme aGiTrack
        reports to the backend instead of leaving it at whatever detection managed to collect.

        Only the three answers aGiTrack actually asks for are matched (OSC 10/11/4, primary
        device attributes, kitty keyboard flags) — a shape no keyboard produces. Inside a
        bracketed paste nothing is absorbed: there the bytes are content the user copied, and a
        pasted transcript of a terminal session must arrive at the agent intact.
        """
        if not data or self._in_bracketed_paste:
            return data
        # Lazily created: the runner is also built by test helpers and by paths that bypass
        # __init__, and a diagnostic buffer must never be the reason a method raises.
        pending: bytearray = self.__dict__.setdefault("_held_reply_prefix", bytearray())
        held = bytes(pending)
        if held:
            # A partial reply from the previous read: re-join before matching, so a reply split
            # across pty reads is still recognised as one.
            pending.clear()
            data = held + data
        if b"\x1b]" not in data and b"\x1b[?" not in data:
            return data  # cheap reject: every reply shape starts with one of these
        replies = _HOST_TERMINAL_REPLY_RE.findall(data)
        remainder = _HOST_TERMINAL_REPLY_RE.sub(b"", data) if replies else data
        # Hold a trailing INCOMPLETE reply rather than letting its bytes — the BEL above all —
        # reach the key handler.
        partial = _HOST_TERMINAL_REPLY_PREFIX_RE.search(remainder)
        if partial and len(partial.group(0)) <= _MAX_HELD_REPLY_BYTES:
            pending.extend(partial.group(0))
            remainder = remainder[: partial.start()]
            self._debug(f"holding a partial host-terminal reply: {partial.group(0)!r}")
        if replies:
            self._parse_host_terminal_responses(b"".join(replies))
            self._debug(f"absorbed {len(replies)} late host-terminal reply/replies: {replies!r}")
        return remainder

    # ------------------------------------------------------------------
    # Early capability service (spawn → reactor)
    # ------------------------------------------------------------------

    EARLY_CAPABILITY_POLL = 0.05  # how long the early responder blocks per select

    def _start_early_capability_service(self) -> None:
        """Answer the backend's terminal-capability queries while aGiTrack is still starting.

        A backend asks the terminal what colours it is drawing on (OSC 10/11) in its first
        milliseconds and waits only briefly for the answer — codex's window measured between
        50 ms and 150 ms on this machine. aGiTrack spawns the backend EARLY on purpose, so it
        loads while aGiTrack finishes starting, but nothing read its output until the reactor
        began: one or two slow startup steps later (`_reset_hook_slate` alone measured 0.33 s).
        The query therefore expired unread in the pty buffer and the backend fell back to its
        no-information defaults — codex drew its DARK palette, and stopped tinting its panels
        at all, in a white terminal, in EVERY session, however light the terminal was. That is
        the invariant "the backend detects the same theme it would in a native session" broken
        outright, and it also fed the agent-theme inference a screen that misrepresented the
        agent, so aGiTrack painted a dark canvas over a light terminal to match it.

        Polling between startup steps cannot fix it: the query arrives DURING one of them.
        So the service runs on its own thread for exactly the startup window. It is the only
        reader of the child's fd until :meth:`_stop_early_capability_service` joins it — which
        happens before the reactor's first select — and everything it reads is kept and handed
        to the normal output path there, so nothing is lost from the screen.

        Requires the host terminal's own answers, so it starts after `_detect_host_terminal`.
        """
        if sys.platform == "win32":
            # Nothing to relay: the Windows console does not answer the OSC/DA queries, so
            # `_detect_host_terminal` collects nothing there — and the child's read fd is a
            # bridged SOCKET, which `os.read` cannot take on Windows anyway.
            return
        if self.master_fd is None or self._early_capability_thread is not None:
            return
        self._early_capability_stop.clear()
        self._early_capability_buffer = bytearray()
        thread = threading.Thread(target=self._early_capability_loop, daemon=True, name="agitrack-early-capability")
        self._early_capability_thread = thread
        thread.start()

    def _early_capability_loop(self) -> None:
        while not self._early_capability_stop.is_set():
            fd = self.master_fd
            if fd is None:
                return
            try:
                readable, _, _ = select.select([fd], [], [], self.EARLY_CAPABILITY_POLL)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                chunk = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return  # the child is gone; the reactor's own drain reports it
            if not chunk:
                return
            self._early_capability_buffer += chunk
            try:
                self._answer_terminal_queries(chunk)
            except Exception as error:  # startup must survive anything this touches
                self._debug(f"early capability answer failed: {error!r}")

    def _stop_early_capability_service(self) -> None:
        """Join the early responder and hand what it read to the normal output path.

        Called before the reactor starts, so from here on the reactor is the sole reader again.
        The bytes are replayed through the same steps the reactor's pty phase applies, minus
        the render — the startup paint follows immediately."""
        thread = self._early_capability_thread
        if thread is None:
            return
        self._early_capability_stop.set()
        thread.join(timeout=2.0)
        self._early_capability_thread = None
        output = bytes(self._early_capability_buffer)
        self._early_capability_buffer = bytearray()
        if not output:
            return
        self._debug(f"early capability service read {len(output)} byte(s) before the reactor")
        self._raw_capture("<", output)
        self.last_child_output = time.monotonic()
        self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
        self._sync_terminal_modes(output)
        self._forward_clipboard_osc(output)
        self._track_sync_update(output)
        self._feed_child_output(output)

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
        # A forced background ("dark"/"light") is reported to the backend in place of the
        # terminal's real colours, so a backend that themes itself from them agrees with the
        # canvas aGiTrack paints. The default ("terminal") relays the truth — the terminal's
        # own answer, or the one derived from the platform when it gave none (see
        # forced_canvas_osc_values and _seed_derived_host_background). That truthful relay is
        # the whole reason nothing has to adapt: the backend themes to the real terminal.
        forced = forced_canvas_osc_values(self)
        fg_value = forced[0] if forced else self.host_fg_value
        bg_value = forced[1] if forced else self.host_bg_value
        if fg_value and re.search(rb"\x1b\]10;\?(?:\x07|\x1b\\)", output):
            response += b"\x1b]10;" + fg_value + b"\x07"
        if bg_value and re.search(rb"\x1b\]11;\?(?:\x07|\x1b\\)", output):
            response += b"\x1b]11;" + bg_value + b"\x07"
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
            if self.debug_proxy:
                # WHAT THE BACKEND WAS ACTUALLY TOLD. A backend themes itself from this answer,
                # so when its colours look wrong the first question is whether it got one at
                # all and what was in it — and the answer is written from two places (here and
                # the early service) to a child pty nothing else can see. Guarded by the flag,
                # and the branch only runs when a query was really present (the cheap reject
                # above skips ordinary output), so it costs nothing in a normal session.
                self._debug(f"answered backend capability query with {bytes(response)!r} (forced={forced is not None})")
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

    # The interpreter-wide hook we displaced while the TUI owns the screen (restored on exit).
    _prev_thread_excepthook: "Callable[[threading.ExceptHookArgs], object] | None" = None

    def _thread_excepthook(self, args: "threading.ExceptHookArgs") -> None:
        """Where a background thread's unhandled exception goes while the TUI is up.

        Python's default prints it to stderr, which is the terminal aGiTrack is drawing on:
        the traceback lands on top of the session and the display no longer matches the
        model. Log it, keep a crash report so it is diagnosable, and let the session live."""
        try:
            name = getattr(args.thread, "name", "?")
            self._debug(f"thread {name} died: {args.exc_type.__name__}: {args.exc_value!r}")
            if args.exc_value is not None:
                from agitrack.proxy.crash import write_crash_report

                write_crash_report(self._crash_report_root(), args.exc_value, context={"thread": name, "fatal": False})
        except Exception:
            pass

    def _crash_report_root(self) -> "Path":
        """Where a crash report goes: the BASE repo, never the session worktree.

        The worktree under ``.agitrack/worktrees/`` is removed on exit, so a report written
        there is destroyed by the very teardown that follows the crash — which is why a
        reported death could leave no trace at all to investigate. Matches ``_diag_path``,
        which puts the DEBUG_PROXY/DEBUG_RAW logs in the base repo for the same reason."""
        base = getattr(self.base_repo, "repo", None)
        return base if base is not None else self.repo.repo

    # Set while a frame is being written. A SIGWINCH handler repaints (see _resize_child),
    # and a signal can land on ANY bytecode boundary: without this, that repaint would
    # interleave its escape sequences with the frame already going out, which is how a
    # terminal ends up in a mode the user cannot get out of.
    _rendering = False
    _crash_notice = ""

    def _render(self) -> None:
        if self.screen is None:
            return
        # The git worker must never write to the terminal (only the main reactor
        # thread owns the screen). When it asks to render, just request a repaint;
        # the main loop flushes it within ~16 ms via _flush_pending_render.
        if not self._on_main_thread():
            self._render_pending = True
            self._wake_main_loop()
            return
        if self._rendering:  # re-entered from a signal handler: let the frame in flight finish
            self._render_pending = True
            return
        self._rendering = True
        try:
            self._render_frame()
        except Exception as error:
            # A repaint is display only. Dropping a frame costs nothing (the next event
            # paints again); raising here would end the session, which is the difference
            # between a flicker and losing the terminal.
            self._debug(f"render failed: {error!r}")
        finally:
            self._rendering = False

    def _render_frame(self) -> None:
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
            message_scroll=self._message_scroll,
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
        ScreenRenderer.append_message_popup(
            self, parts, message, rows=self.rows, cols=self.cols, scroll=self._message_scroll
        )

    def _append_box(
        self, parts: list[str], row: int, col: int, width: int, lines: list[str], highlight: str | None = None
    ) -> None:
        ScreenRenderer.append_box(self, parts, row, col, width, lines, highlight, rows=self.rows)

    def _render_line(self, cells, sel: tuple[int, int] | None = None) -> str:
        return ScreenRenderer.render_line(self, cells, sel, cols=self.cols)

    def _hold_incomplete_tail(self, data: bytes, *, flush: bool = False) -> tuple[bytes, bytes]:
        """Split *data* into (deliver now, hold for the next read).

        If the read ends mid escape-sequence (e.g. a mouse report split across reads), the
        trailing partial is held so it is completed on the next read rather than leaking to
        the backend as stray bytes (the "[<35;..." hex, or a half-delivered legacy "[M" X10
        report).

        **The lone trailing ESC is held too**, and that is the point. A wheel report is
        ``\\x1b[<64;10;10M``; the tty hands aGiTrack whatever bytes happen to be queued, so
        one read in twelve ends exactly on that ESC. Forwarding it delivered a REAL Escape
        keypress to the backend and then leaked the rest of the report as literal text —
        measured at ~48 spurious Escapes per 20 000 wheel events. Escape is destructive in
        both backends (it interrupts a turn, and on Claude's native session picker it quits
        Claude outright), and three backend exits inside 12 s make aGiTrack stop relaunching
        and exit normally — "aGiTrack dies when scrolling extensively", with no crash report
        because nothing ever raised.

        Holding it costs the Escape KEY at most ``INPUT_TAIL_HOLD``; ``flush=True`` (driven by
        that timer in the reactor) then releases it, so Escape still works when nothing follows.
        """
        if flush:
            # The hold expired, so nothing more is coming. A CSI/X10 partial that never
            # completed lost its remainder when the tty's input queue overflowed: it can
            # never be a keystroke, and prepending it to whatever the user types next is
            # exactly how "[<35;124;48" fragments ended up in commit traces — drop it.
            # A lone ESC matches neither pattern and falls through: that is the Escape key.
            expired = _INCOMPLETE_TAIL_RE.search(data) or _INCOMPLETE_X10_RE.search(data)
            return (data[: expired.start()] if expired else data), b""
        match = _INCOMPLETE_TAIL_RE.search(data) or _INCOMPLETE_X10_RE.search(data)
        if match:
            return data[: match.start()], data[match.start() :]
        if data.endswith(b"\x1b"):  # a bare trailing ESC: the start of a sequence, or the key
            return data[:-1], b"\x1b"
        return data, b""

    @staticmethod
    def _strip_orphaned_report_tails(data: bytes) -> bytes:
        """Remove report TAILS whose ``ESC [ <`` the tty threw away (``5;43;28M``, ``;28M``,
        ``8M``), of which one read can carry many, interleaved with intact reports.

        Two guards keep this off real input, because these fragments are plain characters:

        * the read must contain at least one WHOLE report — we only ever do this while
          demonstrably mid-burst, never to a read that is just someone typing;
        * each fragment must sit directly against a report (or against another fragment we
          already dropped), which is how debris from a clipped burst actually appears.

        A literal byte that is not a fragment resets that adjacency, so typing lands intact:
        ``hello`` after a report keeps its ``h`` (no fragment matches there), and once ``h`` is
        kept the rest is ordinary text again. The residue is a user typing something like
        ``8M`` immediately against a wheel report, which is accepted."""
        if not _SGR_MOUSE_RE.search(data):
            return data
        out = bytearray()
        index, end, after_report = 0, len(data), False
        while index < end:
            whole = _SGR_MOUSE_RE.match(data, index)
            if whole:
                out += whole.group()
                index, after_report = whole.end(), True
                continue
            # Consume the whole RUN of fragments, not one at a time: a clipped burst leaves
            # several back to back, and judging them individually breaks the chain — the first
            # of a run has no report directly behind or in front of it, so it survives and
            # every fragment after it survives with it.
            run = index
            while (orphan := _SGR_ORPHAN_TAIL_RE.match(data, run)) and orphan.group():
                run = orphan.end()
            if run > index and (after_report or _SGR_MOUSE_RE.match(data, run)):
                index, after_report = run, True  # the run is debris from this burst
                continue
            out.append(data[index])
            index, after_report = index + 1, False
        return bytes(out)

    @staticmethod
    def _strip_aborted_mouse_reports(data: bytes) -> bytes:
        """Remove mouse reports the tty truncated (see ``_ABORTED_CSI_PREFIX_RE``).

        A COMPLETE report can never match: the lookahead demands an ESC where its own
        terminator (``M``/``m``, or the third X10 coordinate) actually sits, and no amount of
        backtracking finds one — which is why this can run before the normal stripping, and
        for backends that own the mouse. The loop repeats because removing one orphan can
        bring an ESC into position behind another (a burst clipped repeatedly by the queue)."""
        data = ProxyRunner._strip_orphaned_report_tails(data)
        if b"\x1b" not in data:
            return data
        while True:
            # ``\1`` puts the preceding complete report back: it is matched only to prove the
            # orphan sits INSIDE a burst, and is not itself debris.
            stripped = _ABORTED_MOUSE_ESC_RE.sub(
                rb"\1",
                _SGR_CANDIDATE_RE.sub(
                    lambda m: m.group() if _SGR_REPORT_RE.fullmatch(m.group()) else b"",
                    _ABORTED_CSI_PREFIX_RE.sub(b"", data),
                ),
            )
            if stripped == data:
                return data
            data = stripped

    def _flush_child_input(self) -> bool:
        """Retry input a backend's PTY would not accept earlier (see ``BackendProcess.write``).

        Every session, not just the active one: a session backgrounded mid-flood still owes its
        child whatever was queued, and nothing else would ever deliver it. Returns whether any
        session is still owed, which keeps the reactor on the fast poll until it drains."""
        owed = False
        for session in self.sessions or []:
            process = getattr(session, "process", None)
            if process is None:
                continue
            try:
                owed = process.flush_input() or owed
            except OSError as error:
                # The child is gone; the read path detects the exit and tears the session down.
                self._debug(f"flush to backend failed: {error!r}")
        return owed

    def _child_input_pending(self) -> bool:
        return any(
            getattr(session, "process", None) is not None and session.process.pending_input()
            for session in self.sessions or []
        )

    def _arm_mouse_resync(self, dropped: bytes) -> None:
        """Remember that we just gave up on *dropped* mid-mouse-report, so the other half of it
        can be recognised when it turns up on its own (see ``_SGR_PREFIX_RE``)."""
        self._mouse_resync = dropped if _SGR_PREFIX_RE.match(dropped) else None

    def _consume_mouse_resync(self, data: bytes) -> bytes:
        """Strip the completion of the report we gave up on, if *data* starts with it.

        Disarms unconditionally: the remainder is in the terminal's queue directly behind the
        half we dropped, so it is either at the front of the very next read or it was discarded
        too. Carrying the expectation any further would be guessing at unrelated input."""
        prefix, self._mouse_resync = self._mouse_resync, None
        if not prefix or not data:
            return data
        for marker in (PASTE_START, PASTE_END):  # fixed strings: the suffix is exact
            if marker.startswith(prefix) and data.startswith(marker[len(prefix) :]):
                self._debug(f"resynced after a dropped paste marker: {prefix!r} + {marker[len(prefix) :]!r}")
                return data[len(marker) - len(prefix) :]
        match = _SGR_REMAINDER_RE.match(data)
        if not match or not _SGR_REPORT_RE.fullmatch(prefix + match.group()):
            return data  # not the missing half — leave whatever this is alone
        self._debug(f"resynced after a dropped mouse report: {prefix!r} + {match.group()!r}")
        return data[match.end() :]

    def _input_tail_expired(self) -> bool:
        """True when a held escape-sequence tail has waited out ``INPUT_TAIL_HOLD`` and must
        now be treated as final (delivered, if it is a lone ESC; dropped if it is a partial
        sequence whose remainder never arrived)."""
        return bool(self._input_tail) and time.monotonic() - self._input_tail_at >= self.INPUT_TAIL_HOLD

    def _intercept_scroll(self, data: bytes) -> bytes:
        # Debris first, and for EVERY backend. A mouse report the tty chopped in half is not
        # input any backend should see: forwarded, its leading ESC is a real Escape keypress
        # (see _ABORTED_CSI_PREFIX_RE). Backends that drive the mouse themselves get the rest
        # of the stream untouched, so this only ever removes bytes that were already garbage.
        data = self._strip_aborted_mouse_reports(data)
        # Backends that drive the mouse (OpenCode) get all input forwarded so they
        # scroll themselves. For backends that do not (Claude), aGiTrack handles the
        # mouse: the wheel scrolls history, drag selects-and-copies, and
        # PageUp/PageDown also scroll. Consumed events are stripped from input.
        if self.child_mouse:
            return data
        # Every scroll event in this chunk moves the view; the SCREEN is painted once at the
        # end. A trackpad flick arrives as one read holding hundreds of wheel reports, and
        # painting each of them was the whole cost (see ScreenRenderer.scroll).
        delta = 0
        page = max(self.rows - 2, 1)
        for match in _PAGE_KEY_RE.finditer(data):
            delta += page if match.group(1) == b"5" else -page
        data = _PAGE_KEY_RE.sub(b"", data)
        selection_moved = False
        if b"\x1b[<" in data:
            for match in _SGR_MOUSE_EVENT_RE.finditer(data):
                button = int(match.group(1))
                delta += self._mouse_scroll_delta(button)
                selection_moved |= self._track_selection(
                    button, int(match.group(2)), int(match.group(3)), match.group(4)
                )
            data = _SGR_MOUSE_RE.sub(b"", data)
        if b"\x1b[M" in data:
            # Legacy X10 reports (terminals that ignored ?1006). The three bytes are button,
            # column, row each offset by 32; the offset button matches the SGR button numbers
            # (wheel = 64/65), so the wheel still scrolls. Strip them so the raw coordinate
            # bytes don't leak into the backend's input.
            for match in _X10_MOUSE_RE.finditer(data):
                delta += self._mouse_scroll_delta(match.group()[3] - 32)
            data = _X10_MOUSE_RE.sub(b"", data)
        if delta:
            self._scroll(delta)
        elif selection_moved:
            # ONE frame for the whole chunk, for the same reason the wheel coalesces above: a
            # drag arrives as a run of motion reports in a single read, and painting each of
            # them is hundreds of full frames — the repaint storm that starves the stdin
            # reader (see AGENTS.md, "Scrolling and repaint budget"). Skipped entirely when
            # the view also scrolled, since `_scroll` already paints.
            self._render()
        return data

    @staticmethod
    def _mouse_scroll_delta(button: int) -> int:
        """How far this mouse report scrolls the view: 3 lines per wheel notch, 0 for
        anything else. Selection is handled separately, by :meth:`_track_selection`."""
        if not button & 64:  # not the wheel
            return 0
        return -3 if button & 1 else 3

    def _track_selection(self, button: int, col: int, row: int, kind: bytes) -> bool:
        """Drag-select on the rendered screen, copying to the clipboard on release.

        Returns whether the selection CHANGED, so the caller can paint one frame for a whole
        chunk of motion reports rather than one per report.

        **Receiving these events at all is the proof that this is needed.** aGiTrack turns on
        mouse reporting (``?1000h``) so the wheel scrolls its history, and a terminal that
        honours that mode stops doing its OWN selection and forwards the drag to us instead —
        so in exactly the terminals this code runs in, aGiTrack's copy is the ONLY copy the
        user has. A terminal that keeps selecting natively (which is what the reporter of #112
        was using) never forwards a plain left drag, so it never reaches here and nothing about
        it changes: no highlight, no "Copied ..." message, native selection as before.

        This was removed with #112 on the reasoning that dropping it "removes only the harmful
        case". That was half right: it also removed the only way to copy text out of aGiTrack
        in every terminal that DOES forward the drag — the common case on Linux (VTE/gnome-
        terminal, konsole, xterm, alacritty, foot), where selecting text stopped working
        altogether. Neither the terminal nor aGiTrack was copying.

        Only the plain left button drives this. The wheel is scrollback, and a drag with a
        modifier held is left alone — that is the chord terminals use for their own
        selection-while-tracking override. A press-and-release without movement is a CLICK,
        not a selection: it copies nothing and shows no message.
        """
        if button & 64:  # wheel; scrollback's business, not selection's
            return False
        if button & 0b11:  # not the left button
            return False
        if button & 0b11100:  # Shift (4) / Alt (8) / Ctrl (16) held — not a plain drag
            return False
        y = max(0, min(row - 1, max(self.rows - 2, 0)))
        x = max(0, min(col - 1, self.cols - 1))
        motion = bool(button & 32)
        if kind == b"M" and not motion:  # press
            self.sel_active = True
            self.sel_anchor = self.sel_point = (y, x)
            return False  # nothing is highlighted yet: a press alone selects no cells
        if kind == b"M" and motion and self.sel_active:  # drag (terminals that report motion)
            if self.sel_point == (y, x):
                return False  # same cell, unchanged highlight — do not ask for a repaint
            self.sel_point = (y, x)
            return True
        if kind == b"m" and self.sel_active:  # release
            # Capture the release point even from a terminal that reports no motion at all:
            # press + release coordinates are enough to know what was swept over.
            self.sel_point = (y, x)
            if self.sel_anchor != self.sel_point:  # a drag, not a plain click
                self._copy_selection()
            self.sel_active = False
            self.sel_anchor = self.sel_point = None
            return True  # the highlight has to come off the screen
        return False

    def _selection_ranges(self) -> dict[int, tuple[int, int]]:
        return ScreenRenderer.selection_ranges(self, self.cols)

    def _copy_selection(self) -> None:
        ScreenRenderer.copy_selection(
            self, self.rows, self.cols, self._copy_to_clipboard, lambda msg, **kw: self._set_message(msg, **kw)
        )

    # Native clipboard writers, in the order they are tried, per platform. macOS has exactly
    # one; LINUX HAS THREE AND IS GUARANTEED NONE OF THEM — a Wayland session needs `wl-copy`,
    # an X11 one `xclip` or `xsel`, and a bare container or an SSH login has neither, which is
    # why the OSC 52 path below is not a fallback but a companion (see _copy_to_clipboard).
    _CLIPBOARD_COMMANDS: dict[str, tuple[list[str], ...]] = {
        "darwin": (["pbcopy"],),
        "linux": (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]),
        "win32": (["clip"],),
    }

    def _copy_to_clipboard(self, text: str) -> None:
        """Put *text* on the clipboard, by every route that might work.

        BOTH a native helper AND OSC 52 are used, deliberately, rather than stopping at the
        first success. They reach DIFFERENT clipboards over SSH: a native helper sets the
        clipboard of the machine aGiTrack runs on, which over a remote session is not the one
        the user is about to paste into, while OSC 52 travels back down the terminal connection
        to the local one. Locally they are the same clipboard and the same text, so writing
        twice costs a process spawn and changes nothing. Whichever is meaningless in the
        current setup is silently ignored by whatever receives it.
        """
        from agitrack.proc import console_isolation_kwargs

        payload = text.encode("utf-8", errors="replace")
        for command in self._CLIPBOARD_COMMANDS.get(sys.platform, ()):
            if not shutil.which(command[0]):
                continue
            try:
                # `clip.exe` interprets its stdin using the console codepage, which is only
                # UTF-8 if something set it that way — so an accented or CJK character would
                # land mangled. UTF-16LE is the encoding it recognises regardless of codepage
                # (verified on Windows 11: `héllo-wörld-你好` round-trips exactly).
                data = text.encode("utf-16-le", errors="replace") if command[0] == "clip" else payload
                # console_isolation_kwargs for the same reason every other short-lived child
                # gets it: aGiTrack holds the host console in raw mode, and a helper that
                # inherits it can call SetConsoleMode and leave input echoing as escape codes
                # (see proc.py). detach_stdin=False — the text is fed through `input=`.
                subprocess.run(command, input=data, check=False, **console_isolation_kwargs(detach_stdin=False))
                break
            except OSError:
                continue
        encoded = base64.b64encode(payload).decode("ascii")
        try:
            os.write(sys.stdout.fileno(), b"\x1b]52;c;" + encoded.encode("ascii") + b"\x07")
        except OSError:
            pass

    def _history_len(self) -> int:
        return ScreenRenderer.history_len(self)

    def _scroll(self, delta: int) -> None:
        # Scrolling paints at most one frame per RENDER_MIN_INTERVAL, exactly like backend
        # output. When a frame is skipped the repaint is owed, so flag it and wake the loop:
        # the last event of a flick must still land on screen, not wait for the next keypress.
        if ScreenRenderer.scroll(self, delta, self._render, min_interval=self.RENDER_MIN_INTERVAL):
            self._render_pending = True
            self._wake_main_loop()

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

    # Agent background (renderer.py): the canvas the `agent_background` setting implies and
    # the canvas-aware "back to normal" sequence every painted region resets to.
    def canvas_sgr_body(self) -> str:
        return ScreenRenderer.canvas_sgr_body(self)

    def apply_agent_background(self) -> None:
        ScreenRenderer.apply_agent_background(self)

    def _apply_agent_background(self, *, repaint: bool = True) -> None:
        """Install the canvas the `agent_background` setting asks for.

        Called before the first paint and again when the setting is edited — never from the
        render path or a timer, because the canvas must be a function of the SETTING alone.
        Under the default ("terminal") this clears the canvas, leaving the host terminal's
        own background showing, and the repaint below is skipped as there is nothing to fill.
        """
        try:
            self.apply_agent_background()
            if repaint and self._canvas is not None:
                # Fill the alternate screen with the canvas NOW. `enter_host_screen` cleared it
                # to the terminal's own background a moment ago, so without this the user
                # watches a white (or black) screen for as long as the backend takes to draw
                # its first frame.
                from agitrack.proxy.renderer import write_frame

                write_frame((self.reset_sgr() + "\x1b[2J\x1b[H").encode())
        except Exception as error:  # a display detail is never worth a failed startup
            self._debug(f"agent background could not be applied: {error!r}")

    def reset_sgr(self) -> str:
        return ScreenRenderer.reset_sgr(self)

    def history_len(self) -> int:
        return ScreenRenderer.history_len(self)

    def scroll(self, delta: int, render_fn, *, min_interval: float = 0.0) -> bool:
        return ScreenRenderer.scroll(self, delta, render_fn, min_interval=min_interval)

    def visible_lines(self, rows: int) -> list:
        return ScreenRenderer.visible_lines(self, rows)

    def selection_ranges(self, cols: int) -> dict:
        return ScreenRenderer.selection_ranges(self, cols)

    def copy_selection(self, rows: int, cols: int, copy_fn, set_msg_fn) -> None:
        ScreenRenderer.copy_selection(self, rows, cols, copy_fn, set_msg_fn)

    def render_line(self, cells, sel=None, *, cols: int) -> str:
        return ScreenRenderer.render_line(self, cells, sel, cols=cols)

    def append_box(
        self, parts, row, col, width, lines, highlight=None, *, rows: int, scrollable: bool = False, scroll: int = 0
    ) -> None:
        ScreenRenderer.append_box(
            self, parts, row, col, width, lines, highlight, rows=rows, scrollable=scrollable, scroll=scroll
        )

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

    def append_message_popup(self, parts, message: str, *, rows: int, cols: int, scroll: int = 0) -> None:
        ScreenRenderer.append_message_popup(self, parts, message, rows=rows, cols=cols, scroll=scroll)

    def wrapped_message_height(self, lines, cols: int) -> int:
        return ScreenRenderer.wrapped_message_height(self, lines, cols)

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
        # and every message that points the user at the aGiTrack menu.
        return getattr(self.global_config, "menu_key_label", None) or "Ctrl-G"

    def _status_line(self) -> str:
        return ScreenRenderer.status_line(
            self,
            cols=self.cols,
            name=self.name,
            backend_name=self.backend.name,
            session_id=self.state.backend_session_id,
            base_branch=self._base_branch,
            current_dir_branch=self._repo_dir_branch,
            # Manual mode only: committing is the user's job, so the count of turns waiting for
            # their commit belongs where they can see it, not only in the exit dialog.
            manual_pending=self._manual_pending_count() if self._manual_commits else 0,
            worktree=self.worktree,
            scroll_back=self.scroll_back,
            user_declined=self._user_declined,
            short_session_fn=_short_session,
            menu_label=self._menu_label(),
            summarizer_on=self._summarization_enabled(),
            # The project the agent works on: the BASE repository, not the
            # session worktree under .agitrack/worktrees/ (an internal detail whose
            # long path mostly repeats the session name shown next to it).
            cwd=str(repo_path)
            if (repo_path := getattr(self.base_repo, "repo", None) or getattr(self.repo, "repo", None))
            else None,
        )

    def _summarization_enabled(self) -> bool:
        # The GLOBAL config is the durable source of truth (survives restarts and worktree
        # teardown), so it wins. The per-session worktree state — which always defaults to
        # "on" on a fresh worktree — must NOT shadow it, or the toggle never persists. Fall
        # back to the per-session value only when there is no global config (e.g. tests).
        gc_enabled = getattr(self.global_config, "summarization_enabled", None)
        if gc_enabled is not None:
            return bool(gc_enabled)
        state_enabled = getattr(self.state, "summarization_enabled", None)
        return True if state_enabled is None else bool(state_enabled)

    def _render_status(self, text: str) -> None:
        # Writes straight to stdout, so only the main reactor thread may run it: the
        # git worker calls it (verbose mode) but must never touch the screen. This is
        # a debug-only status line, so dropping it off-thread is harmless.
        if not self._on_main_thread():
            return
        prompt = text.replace("\r", "").replace("\n", "")
        line = f" aGiTrack> {prompt}"[: self.cols].ljust(self.cols)
        os.write(sys.stdout.fileno(), f"\x1b[{self.rows};1H\x1b[7m{line}\x1b[0m".encode())

    def _enter_host_screen(self) -> None:
        if self._host is not None:
            self._host.enter_host_screen()
        else:
            TerminalHost.enter_host_screen(self)

    def _enable_host_mouse(self) -> None:
        TerminalHost.enable_host_mouse(self)

    # --- menu navigation convention -----------------------------------------
    #
    # Every Ctrl-G menu is one ``while True`` loop and returns just TWO signals, so **Esc
    # always moves up exactly one level** and the code's call hierarchy mirrors the on-screen
    # hierarchy. To author a menu: build options → ``_select_popup`` → dispatch, where
    #
    #   Esc (``_select_popup`` returns None)  → ``return self._MENU_UP``  (back out one level)
    #   a context transition (switch/new/resume a session)
    #                                         → ``return self._MENU_DONE`` (unwind to the agent)
    #   any other action (config, or a child menu the user backed out of)
    #                                         → ``continue``               (re-show this menu)
    #
    # A child menu is entered by CALLING it; the parent does
    # ``if child() == self._MENU_DONE: return self._MENU_DONE`` else ``continue`` — so the
    # child's Esc lands back on this parent, while a transition unwinds past every level.
    # ``_after_menu_command`` turns a top-level menu's UP into "re-open the Ctrl-G palette",
    # the parent of every command menu. Navigation is silent — no "closed"/"cancelled"
    # message on the way up — and modal closes don't repaint the bare screen (see
    # ``_run_modal``), so stepping between levels is instant and flicker-free.
    #
    # Parent → child today: _run_command → {_session_menu, _settings_menu, summarizer menu,
    # backend picker}; _session_menu → {_rename_session_menu, _change_session_merge_branch_menu,
    # _share_session, _manage_shared_sessions_menu, _resume_session_menu,
    # _resume_shared_session_menu, …}; _manage_shared_sessions_menu → _manage_one_shared_session;
    # _settings_menu → {_edit_one_setting, _settings_timings_menu → _edit_one_timing}.
    _MENU_UP = "up"
    _MENU_DONE = "done"

    def _after_menu_command(self, signal: str) -> None:
        """A top-level command menu closed. Esc/back (UP) goes up one level to the Ctrl-G
        command palette; a transition (DONE) drops through to the agent."""
        if self._exit_menu_requested:
            # Ctrl-G inside the menu asked to close everything: drop to the agent, don't reopen
            # the palette — even though the unwind surfaced as _MENU_UP.
            self._exit_menu_requested = False
            return
        if signal == self._MENU_UP:
            self._reopen_command_palette()

    def _reopen_command_palette(self) -> None:
        # Re-open the input-layer Ctrl-G palette (the parent of every command menu) so Esc
        # out of a command lands back here rather than at the agent. Mirrors the state reset
        # the palette does when first opened with the menu key.
        self.input.capturing = True
        self.input.buffer.clear()
        self.input.selected_index = 0
        self.input.escape_buffer = None
        self.input.extra_commands = ["merge"] if self._has_unmerged_work() else []
        self._render()

    def _manage_unstaged_menu(self) -> None:
        """Interactive editor for the base repo's intentionally-unstaged ("declined") set — the
        untracked files/folders aGiTrack will NOT offer to commit. Reuses the ``git-unstaged``
        command (was a read-only list). The user can REMOVE entries (re-stage them, so they're
        offered/committed again) or ADD untracked paths to keep unstaged. The set is persisted in
        the base repo's state file, so it can equally be edited there on disk; this menu reads it
        fresh each round so on-disk edits are reflected too."""
        repo = self.base_repo
        while True:
            state = self._user_state()  # fresh read each round → reflects on-disk edits + our saves
            self._prune_declined_untracked(repo, state)  # drop entries no longer untracked on disk
            declined = sorted(state.declined_untracked())
            addable = [path for path in repo.untracked_entries() if path not in set(declined)]
            options: list[str] = []
            if addable:
                options.append("Add untracked path(s) to keep unstaged…")
            if declined:
                options.append("Re-stage a path (remove from the list)…")
                options.append("Re-stage ALL (clear the list)")
            options.append("← Done")
            if declined:
                title = (
                    f"Intentionally unstaged — aGiTrack will NOT commit these ({len(declined)}).\n"
                    f"Edit the list here, or directly on disk in {state.path}.\n\nUnstaged:"
                )
            else:
                title = f"Nothing is intentionally unstaged.\n(The list is stored in {state.path}.)"
            choice = self._select_popup(title, options, detail=declined or None)
            if choice is None or choice.startswith("←"):
                break
            if choice.startswith("Add"):
                pick = self._select_popup(
                    "Keep which untracked path unstaged? (aGiTrack won't commit it)",
                    [*addable, "← Back"],
                    detail=addable,
                )
                if pick and not pick.startswith("←"):
                    state.add_declined([pick])
                    self._set_message(f"Keeping '{pick}' unstaged.")
            elif choice.startswith("Re-stage ALL"):
                confirm = self._select_popup(
                    f"Re-stage all {len(declined)} path(s)? They'll be offered for commit again.",
                    ["Yes, re-stage all", "← Back"],
                )
                if confirm == "Yes, re-stage all":
                    state.keep_declined([])  # keep none → clears the list
                    self._set_message("Cleared the intentionally-unstaged list.")
            elif choice.startswith("Re-stage"):
                pick = self._select_popup(
                    "Re-stage which path? (stop keeping it unstaged)",
                    [*declined, "← Back"],
                    detail=declined,
                )
                if pick and not pick.startswith("←"):
                    state.remove_declined([pick])
                    self._set_message(f"Re-staged '{pick}' — it'll be offered for commit again.")
        self._reload_user_declined()
        self._render()

    def _run_command(self, command: str) -> None:
        # aGiTrack commands in proxy mode are triggered via Ctrl-G and are plain
        # names; ":" is not a command trigger here (it is forwarded to the
        # backend like any other input). On a confirmed "exit"/"quit" this runs
        # the teardown flow (which sets self._exiting); the caller checks that
        # flag to break the reactor loop.
        self._exit_menu_requested = False  # fresh menu session: clear any stale close request
        name, _, arg = command.partition(" ")
        if name in {"exit", "quit"}:
            if not self._run_exit_flow():
                self._render()
            return

        if name == "git-commit":
            created = self._create_user_commit_popup(
                repo=self.base_repo, state=self._user_state(), include_declined=True
            )
            if self._manual_commits:
                msg = (
                    "Committed your changes (with the pending agent turns folded in)."
                    if created
                    else "No changes to commit."
                )
            else:
                msg = (
                    "Committed your changes to the base repo." if created else "No changes to commit in the base repo."
                )
            self._set_message(msg)
            self._reload_user_declined()
            self._render()
            return

        if name == "git-unstaged":
            self._manage_unstaged_menu()
            return
        elif name == "agent-backend":
            backends = available_backends()
            selected = arg.strip() or self._select_popup("Backend Agent", backends)
            if selected is None:  # Esc on the picker → up one level to the palette
                self._after_menu_command(self._MENU_UP)
                return
            if selected not in backends:
                self._set_message(f"Unknown backend: {selected}. Available: {', '.join(backends)}")
            elif selected == self.state.backend:
                self._set_message(f"Backend already set to {selected}")
            else:
                self._switch_backend(selected)
                return
        elif name == "sessions":
            self._after_menu_command(self._handle_session_command(arg))
            return
        elif name == "settings":
            self._after_menu_command(self._settings_menu())
            return
        elif name == "update":
            self._handle_update_command()
            return
        elif name == "dashboard":
            self._handle_dashboard_command()
            return
        elif name == "merge":
            self._handle_merge_command()
            return
        elif name == "":
            self._set_message("Select an aGiTrack command.")
        else:
            self._set_message(f"Unknown aGiTrack command: {name}")
        self._render()

    # --- settings menu ------------------------------------------------------
    #
    # The menu is a small form: edits accumulate as PENDING changes (each with the scope
    # the user chose — repo-local or global) and are written only when the user confirms
    # "save" on the way out. Esc always goes up ONE level (value editor → list, scope →
    # value editor, sub-list → main list, main list → close), and closing with unsaved
    # changes asks whether to save or discard them.

    @staticmethod
    def _fmt_setting(value: object) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, list):
            return ", ".join(str(p) for p in value) if value else "(none)"
        return str(value) if value not in (None, "") else "(unset)"

    def _pending_count(self) -> int:
        return len(self._settings_pending) + len(self._settings_pending_timings)

    def _choose_scope(self, label: str) -> str | None:
        """Ask whether an edit applies repo-locally or globally. None = Esc/Back."""
        pick = self._select_popup(
            f"Apply '{label}' to:",
            [
                "This repository only (.agitrack/config.json)",
                "Global — all repositories (~/.agitrack/config.json)",
                "← Back",
            ],
        )
        if pick is None or pick.startswith("←"):
            return None
        return "repo" if pick.startswith("This repository") else "global"

    def _settings_specs(self) -> list[dict]:
        """Declarative registry of the user-editable config options shown in the settings
        menu. Labels are self-explanatory and the ORDER groups related settings (backend →
        session isolation → agent behavior → commit summaries → aGiTrack itself). ``kind``
        drives the editor; ``restart`` marks settings resolved at launch (so a change applies
        to new sessions / the next launch). Effective values are read from ``global_config``
        (repo overlay > global > built-in default).

        Note the two distinct "worktree" settings, kept adjacent and worded so they aren't
        confused: ``use_worktrees`` decides WHETHER each session gets its own worktree, while
        ``sandbox`` only CONFINES the agent's writes to that worktree (and is moot when
        worktrees are off)."""
        return [
            {
                "key": "default_backend",
                # Never hand-write the list: the options right below come from the registry, so a
                # literal label went stale the moment a backend was added — this one offered Codex
                # under a heading that said the choice was Claude-or-OpenCode.
                "label": f"Default coding agent ({backend_phrase()})",
                "kind": "choice",
                "options": available_backends(),
                "restart": True,
            },
            # --- session isolation: the worktree toggle, then the sandbox that depends on it ---
            {
                "key": "use_worktrees",
                "label": "Isolate each session in its own git worktree (off: edit the current branch directly)",
                "kind": "bool",
                "restart": True,
            },
            {
                "key": "sandbox",
                "label": "Sandbox: block the agent from writing outside its worktree (no effect when worktrees are off)",
                "kind": "bool",
                "restart": True,
            },
            {
                "key": "allowed_edit_paths",
                "label": "Folders/files the agent may write to outside its worktree (sandbox carve-out)",
                "kind": "paths",
                "restart": True,
            },
            {
                "key": "manual_commits",
                "label": "Manual commits: user-triggered, per-turn agent work hidden until you commit (implies no worktree)",
                "kind": "bool",
                "restart": True,
            },
            {
                "key": "background",
                "label": "Background mode: track a user-driven backend session with no TUI (implies no worktree; launch with -b)",
                "kind": "bool",
                "restart": True,
            },
            {
                "key": "autotrack_hook",
                "label": "Auto-start tracking when aGiTrack isn't running — on a commit, or when an agent turn leaves changes (auto/off; `agitrack --remove-hooks` also turns it off)",
                "kind": "text",
            },
            {
                "key": "self_update",
                "label": "Keep aGiTrack up to date automatically (off: only tell me an update exists)",
                "kind": "bool",
            },
            # --- agent behavior ---
            {
                "key": "commit_guidance",
                "label": "Ask the agent not to make its own git commits (aGiTrack commits each turn)",
                "kind": "bool",
                "restart": True,
            },
            # --- session sharing ---
            {
                "key": "share_max_transcript_bytes",
                "label": "Max size of a shared session (larger sessions are trimmed to fit)",
                "kind": "size_mb",
            },
            # --- commit summaries ---
            {"key": "summarization_enabled", "label": "Write an AI summary for each commit", "kind": "bool"},
            {"key": "summarization_model", "label": "Model used to write commit summaries", "kind": "model"},
            # --- aGiTrack itself ---
            {
                "key": "agent_background",
                "label": "Background behind the agent (terminal: keep your terminal's own)",
                "kind": "choice",
                "options": list(GlobalConfig.AGENT_BACKGROUND_CHOICES),
            },
            {"key": "check_for_updates", "label": "Automatically check for aGiTrack updates", "kind": "bool"},
            {
                "key": "menu_key",
                "label": "Key that opens this aGiTrack menu (e.g. ctrl-g)",
                "kind": "text",
                "restart": True,
            },
            {"key": "timings", "label": "Polling & debounce timings (advanced) …", "kind": "timings"},
        ]

    def _setting_value_display(self, spec: dict) -> str:
        key, kind = spec["key"], spec["kind"]
        if kind == "timings":
            n = len(self._settings_pending_timings)
            return f"{n} unsaved change(s)" if n else ""
        if key in self._settings_pending:  # a pending (unsaved) edit shows its new value
            value, scope, _ = self._settings_pending[key]
            shown = (
                f"{value // (1024 * 1024)} MB"
                if kind == "size_mb" and isinstance(value, int)
                else self._fmt_setting(value)
            )
            return f"{shown}  · UNSAVED → {scope}"
        if kind == "bool":
            text = "on" if bool(getattr(self.global_config, key)) else "off"
        elif kind == "paths":
            paths = self.global_config.allowed_edit_paths
            text = ", ".join(paths) if paths else "(none)"
        elif kind == "size_mb":
            text = f"{int(getattr(self.global_config, key)) // (1024 * 1024)} MB"
        else:
            value = getattr(self.global_config, key, None)
            text = str(value) if value not in (None, "") else "(default)"
        suffix = {"repo": " · repo", "global": " · global", "default": ""}[self.global_config.source(key)]
        return f"{text}{suffix}"

    def _settings_menu(self) -> str:
        """Ctrl-G → "settings": browse and edit ALL config options. Changes are collected
        as PENDING edits (each with its chosen scope) and written only when you confirm on
        the way out. Esc goes up one level; closing with unsaved changes asks to save them.
        Returns _MENU_UP so closing the menu lands back on the Ctrl-G palette."""
        self._settings_pending = {}
        self._settings_pending_timings = {}
        while True:
            specs = self._settings_specs()
            options = []
            for spec in specs:
                disp = self._setting_value_display(spec)
                options.append(f"{spec['label']}: {disp}" if disp else spec["label"])
            pending = self._pending_count()
            close = f"← Close (save {pending} change(s))" if pending else "← Close"
            options += ["", close]
            title = "Settings — Esc = back; edits are saved only when you close"
            choice = self._select_popup(title, options)
            if choice is None or choice.startswith("← Close") or not choice.strip():
                # Leaving the top level: offer to save/discard any unsaved edits.
                if pending and self._confirm_save_pending() == "keep":
                    continue  # "Keep editing" → stay in the menu
                self._set_message("Settings closed.")
                self._render()
                return self._MENU_UP
            spec = specs[options.index(choice)]
            if spec["kind"] == "timings":
                self._settings_timings_menu()
            else:
                self._edit_one_setting(spec)

    def _edit_one_setting(self, spec: dict) -> None:
        """Edit one setting into a PENDING change. Esc at the value prompt returns to the
        list; Esc at the scope prompt returns to the value prompt (one level up each time)."""
        key, kind, label = spec["key"], spec["kind"], spec["label"]
        while True:
            if kind == "bool":
                pick = self._select_popup(label, ["Turn ON", "Turn OFF", "← Back"])
                if pick is None or pick.startswith("←"):
                    return
                value: object = pick == "Turn ON"
            elif kind == "choice":
                pick = self._select_popup(label, [*spec["options"], "← Back"])
                if pick is None or pick.startswith("←"):
                    return
                value = pick
            elif kind == "model":
                chosen = self._choose_summarizer_model()  # picker (smallest tier first) or free-text
                if chosen is _NO_MODEL_PICK:
                    return
                value = chosen
            elif kind == "paths":
                current = self._pending_paths(key)
                raw = self._prompt_popup(
                    label, f"Paths separated by '{os.pathsep}' (blank = none):", default=os.pathsep.join(current)
                )
                if raw is None:
                    return
                value = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
            elif kind == "size_mb":
                from agitrack.sessions.share_cap import HARD_MAX_SHARED_BYTES

                hard_mb = HARD_MAX_SHARED_BYTES // (1024 * 1024)
                raw = self._prompt_popup(label, f"Size in MB (1–{hard_mb}):", default=str(self._pending_size_mb(key)))
                if raw is None:
                    return
                try:
                    mb = int(float(raw.strip()))
                except ValueError:
                    self._set_message("Enter a whole number of MB.")
                    self._render()
                    continue
                if not 1 <= mb <= hard_mb:
                    # Enforce the hard limit in the editor too — a shared file over this is
                    # rejected by Git hosts, so we never let it be configured that high.
                    self._set_message(f"Must be between 1 and {hard_mb} MB (Git's per-file size limit).")
                    self._render()
                    continue
                value = mb * 1024 * 1024
            else:  # text
                raw = self._prompt_popup(label, "New value (blank = unset):", default=self._pending_text(key))
                if raw is None:
                    return
                value = raw.strip() or None
            scope = self._choose_scope(label)
            if scope is None:  # Esc at scope → one level up: re-edit the value
                continue
            self._settings_pending[key] = (value, scope, bool(spec.get("restart")))
            shown = (
                f"{value // (1024 * 1024)} MB"
                if kind == "size_mb" and isinstance(value, int)
                else self._fmt_setting(value)
            )
            self._set_message(f"'{label}' → {shown} ({scope}) — unsaved; choose Close to save.", seconds=7.0)
            self._render()
            return

    def _pending_size_mb(self, key: str) -> int:
        # Current value (MB) to seed the editor: a pending edit if any, else the effective config.
        raw: object = (
            self._settings_pending[key][0] if key in self._settings_pending else getattr(self.global_config, key)
        )
        return raw // (1024 * 1024) if isinstance(raw, int) else 0

    def _pending_paths(self, key: str) -> list[str]:
        if key in self._settings_pending:
            pending = self._settings_pending[key][0]
            if isinstance(pending, list):
                return [str(p) for p in pending]
        return self.global_config.allowed_edit_paths

    def _pending_text(self, key: str) -> str:
        if key in self._settings_pending:
            return self._fmt_setting(self._settings_pending[key][0]).replace("(unset)", "")
        return str(getattr(self.global_config, key, None) or "")

    def _settings_timings_menu(self) -> None:
        from agitrack.config.settings import DEFAULT_TIMINGS

        keys = list(DEFAULT_TIMINGS)
        while True:
            timings = self.global_config.timings
            options = []
            for k in keys:
                if k in self._settings_pending_timings:
                    v, scope = self._settings_pending_timings[k]
                    options.append(f"{k}: {v:g}s  · UNSAVED → {scope}")
                else:
                    options.append(f"{k}: {timings[k]:g}s")
            options += ["", "← Back"]
            choice = self._select_popup("Timings (seconds) — Esc/← Back returns to settings", options)
            if choice is None or choice == "← Back" or not choice.strip():
                return  # one level up: back to the settings list
            tkey = keys[options.index(choice)]
            self._edit_one_timing(tkey, timings[tkey])

    def _edit_one_timing(self, tkey: str, current: float) -> None:
        while True:
            default = (
                f"{self._settings_pending_timings[tkey][0]:g}"
                if tkey in self._settings_pending_timings
                else str(current)
            )
            raw = self._prompt_popup(tkey, f"New value in seconds (current {current:g}):", default=default)
            if raw is None:
                return  # back to the timings list
            try:
                seconds = float(raw)
                if seconds <= 0:
                    raise ValueError
            except ValueError:
                self._set_message("Enter a positive number of seconds.")
                self._render()
                continue
            scope = self._choose_scope(tkey)
            if scope is None:  # Esc at scope → re-edit the value
                continue
            self._settings_pending_timings[tkey] = (seconds, scope)
            self._set_message(f"'{tkey}' → {seconds:g}s ({scope}) — unsaved; choose Close to save.", seconds=7.0)
            self._render()
            return

    def _confirm_save_pending(self) -> str:
        """Prompt to save/discard pending settings edits when closing. Returns 'keep' if the
        user chose to keep editing (the menu should stay open), else 'saved'/'discarded'."""
        n = self._pending_count()
        pick = self._select_popup(
            f"You have {n} unsaved settings change(s). Save them?",
            ["Yes, save them", "No, discard them", "← Keep editing"],
        )
        if pick is None or pick.startswith("←"):
            return "keep"
        if pick.startswith("No"):
            self._settings_pending = {}
            self._settings_pending_timings = {}
            self._set_message(f"Discarded {n} unsaved settings change(s).", seconds=6.0)
            self._render()
            return "discarded"
        # Write every pending edit to its chosen scope.
        needs_restart = False
        for key, (value, scope, restart) in self._settings_pending.items():
            self.global_config.set(key, value, scope=scope)
            needs_restart = needs_restart or restart
        # The background setting needs no restart: the canvas is derived from it right here
        # (the ONLY place other than startup that touches it) and the repaint below shows it.
        self.agent_background = getattr(self.global_config, "agent_background", "terminal")
        self._apply_agent_background(repaint=False)
        self._render_pending = True
        by_scope: dict[str, dict[str, float]] = {}
        for tkey, (value, scope) in self._settings_pending_timings.items():
            by_scope.setdefault(scope, {})[tkey] = value
        for scope, changes in by_scope.items():
            store = self.global_config.repo_data if scope == "repo" else self.global_config.data
            merged = dict(store.get("timings") or {})
            merged.update(changes)
            self.global_config.set("timings", merged, scope=scope)
            needs_restart = True  # timings are read at launch
        self._settings_pending = {}
        self._settings_pending_timings = {}
        if needs_restart:
            # These settings are read only at launch (worktrees on/off, default backend,
            # timings), so they don't apply to the running session — offer to restart now
            # rather than silently leaving them inert until the next manual launch.
            pick = self._select_popup(
                f"Saved {n} change(s). Some take effect only after a restart. Restart aGiTrack now?",
                ["Yes, restart now", "Not now"],
            )
            if pick == "Yes, restart now":
                self._restart_now("Restarting aGiTrack to apply the new settings…")
            else:
                self._set_message(
                    f"Saved {n} settings change(s). The restart-only ones apply next time you start aGiTrack.",
                    seconds=10.0,
                )
                self._render()
            return "saved"
        self._set_message(f"Saved {n} settings change(s).", seconds=8.0)
        self._render()
        return "saved"

    def _handle_dashboard_command(self) -> None:
        """Ctrl-G → "dashboard": open this repo on aGiTrack's dashboard.

        One hub serves every repository on one port (agitrack/metrics/hub.py), so this no longer
        spawns or owns anything: it makes sure the hub is up, remembers this repo so the hub's
        switcher offers it, and opens the path for it. Which VIEW opens (aGiTrack's own tracking,
        or the reconstruction from local transcripts) is the hub's decision — a repo with nothing
        tracked yet opens on the backtrace rather than on an empty page.

        The hub is a detached daemon: it keeps serving after aGiTrack quits and after this
        terminal closes, until `agitrack -d stop`. A popup says so the first time it starts.
        """
        from agitrack.metrics import open_dashboard_in_browser, remote_browser_hint
        from agitrack.metrics.hub import ensure_hub_for, log_path, running_hub
        from agitrack.repos import BACKTRACE

        was_running = running_hub() is not None
        try:
            # The reconstruction can take minutes to build, but it is built inside the hub on
            # first request, so nothing here blocks the reactor: the browser waits, not the TUI.
            url, view = ensure_hub_for(self.base_repo.repo)
        except Exception as error:
            self._set_message(f"Could not start the dashboard: {error}")
            self._render()
            return
        if not url:
            self._set_message(f"Could not start the dashboard. See {log_path()}.")
            self._render()
            return
        record = running_hub() or {}
        port = record.get("port", 0)
        port = int(port) if isinstance(port, int) else 0
        self._dashboard_url = url
        self._dashboard_port = port
        opened = open_dashboard_in_browser(url)
        label = "Backtrace view" if view == BACKTRACE else "Dashboard"
        if was_running:
            # No "opening in your browser": the browser has ALREADY been opened by the time this
            # is painted, so announcing it as about to happen is wrong.
            self._set_message(f"{label} at {url}." if opened else f"{label} ready. {remote_browser_hint(url, port)}")
            self._render()
            return
        detail = [
            "The dashboard runs as its own daemon: it KEEPS RUNNING after you quit aGiTrack",
            "(even after this terminal closes). Stop it any time with: agitrack -d stop",
            "It serves every repository aGiTrack knows, switchable from the page header.",
        ]
        if view == BACKTRACE:
            from agitrack.metrics.suggest import SUBSTITUTION_NOTICE

            detail = SUBSTITUTION_NOTICE.splitlines() + [""] + detail
        self._select_popup(
            f"{label} live at {url}", ["ok"], detail=detail + ([] if opened else [remote_browser_hint(url, port)])
        )
        self._set_message(f"{label} live at {url}." if opened else f"{label} live. {remote_browser_hint(url, port)}")
        self._render()

    def _dashboard_email_logins(self) -> dict[str, str]:
        """Map the current user's git email → their GitHub login, so the dashboard can
        label this session's fresh, unpushed commits with a GitHub ID even though `gh`
        (which only knows commits already on the remote) can't resolve them yet.
        Best-effort: an empty map just means the email/no-reply heuristics still apply."""
        try:
            login = self._cached_or_resolve_login()
            email = self.base_repo._run(["git", "config", "user.email"], check=False).stdout.strip()
            if login and email:
                return {email.lower(): login}
        except Exception:
            pass
        return {}

    def _popup_read_input(self) -> bytes:
        # Read the user's next keypress for a modal popup WITHOUT suspending the
        # rest of the event loop: every session's PTY keeps being drained while
        # the popup waits, since back-pressure on a full PTY buffer blocks the
        # backend's writes and can stall or kill it (a popup can stay open for a
        # long time, and agents keep streaming behind it).
        stdin_fd = self._stdin_fileno()
        dead: set[int] = set()
        while True:
            background = self._background_fds() if self.sessions else {}
            master = self.master_fd
            fds = [fd for fd in [stdin_fd, master] if fd is not None and fd not in dead]
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
                return self._read_stdin(32)

    def _run_modal(self, modal: "PromptModal | SelectModal") -> "str | None":
        """Run *modal* to completion, keeping all session PTYs draining.

        This is the shared reactor iteration for modal dialogs.  It repeatedly:
          1. Renders the modal's current message via ``_set_message`` + ``_render``.
          2. Calls ``_popup_read_input()`` — which selects on stdin AND every
             session PTY so back-pressure never builds up while the popup is open.
          3. Passes the bytes to ``modal.feed()`` and acts on the returned action:

             ``("done",   value)``  — clears the message and returns value.
             ``("cancel", None)``   — clears the message and returns None.
             ``("exit",   None)``   — calls ``_run_exit_flow()``; returns None on
                                      confirmed exit, otherwise redraws and continues.
             ``("redraw", None)``   — redraws and continues.

        On ``done``/``cancel`` we deliberately do NOT repaint here: the modal's last
        frame stays on screen and we only flag ``_render_pending``. When this modal was
        opened from a parent menu, the parent's next ``_select_popup`` paints the next
        menu straight over this one — so stepping between menu levels never flashes the
        bare backend screen in between (smooth, instant navigation). When nothing follows
        (the interaction returns to the agent), the reactor flushes that one pending frame
        within a few ms, clearing the popup cleanly.

        The call-shape is synchronous, which preserves the ~25 existing call
        sites unmodified.  ``_prompt_popup`` and ``_select_popup`` are thin
        facades that construct the appropriate modal and delegate here.

        When called from the git worker thread (which must never touch stdin/the
        screen), the modal is marshaled to the main reactor thread via
        ``_run_modal_via_main`` and this call blocks until the user answers there.
        """
        if not self._on_main_thread():
            return self._run_modal_via_main(modal)
        return self._run_modal_inline(modal)

    def _run_modal_inline(self, modal: "PromptModal | SelectModal") -> "str | None":
        """The actual modal loop — always runs on the main reactor thread (directly,
        or via ``_drain_modal_mailbox`` for a worker-queued dialog). Reads stdin and
        paints the screen, so it must never run off the main thread."""
        # If the Ctrl-G command palette is open, SUSPEND it for the duration of this dialog. The
        # renderer draws the palette OR a message — never both — so a dialog presented while the
        # palette is up (e.g. a background offer, or a worker-queued confirm) would otherwise stay
        # hidden behind the palette while still consuming the user's keystrokes: dangerous, since
        # the user answers a prompt they can't see. Suspending it makes the dialog render ON TOP;
        # we restore the palette (its typed text + selection) afterwards so the user answers the
        # dialog and then continues in the palette exactly where they left off.
        palette_suspended = bool(getattr(self.input, "capturing", False))
        saved_buffer = bytes(self.input.buffer) if palette_suspended else b""
        saved_index = self.input.selected_index if palette_suspended else 0
        if palette_suspended:
            self.input.capturing = False
        try:
            return self._run_modal_loop(modal)
        finally:
            if palette_suspended:
                # There was no menu stack here (the dialog was raised over the open palette, not from
                # a menu command), so a menu-key press inside the dialog leaves _exit_menu_requested
                # set with nothing to unwind — clear it so it can't close the NEXT menu prematurely.
                self._exit_menu_requested = False
                self.input.buffer = bytearray(saved_buffer)
                self.input.selected_index = saved_index
                self.input.capturing = True
                self._render_pending = True  # repaint the restored palette over the closed dialog

    def _run_modal_loop(self, modal: "PromptModal | SelectModal") -> "str | None":
        # A paste already under way when this popup opened sent its CSI 200~ to the backend,
        # so the popup would see only unmarked content; carry the state in (the modal clears
        # it on the closing marker).
        if self._in_bracketed_paste:
            modal.in_paste = True
        try:
            return self._run_modal_body(modal)
        finally:
            # Text the user pasted while the popup was up was meant for the agent, not for
            # the popup — hand it over now rather than dropping it. The bytes are replayed
            # exactly as they arrived (markers included), so together with the opening marker
            # the backend already received, it reads one coherent paste.
            pasted = bytes(getattr(modal, "pasted", b""))
            if pasted and self.active is not None:
                try:
                    self.active.process.write(pasted)
                except Exception:
                    pass  # a dead/replaced backend must never break closing a dialog

    def _run_modal_body(self, modal: "PromptModal | SelectModal") -> "str | None":
        needs_render = True
        while True:
            if self._exit_menu_requested:
                # A Ctrl-G in a deeper popup asked to close the whole menu: don't even paint
                # this one. Every nested level's next popup short-circuits the same way, so the
                # menu unwinds straight to the agent (the flag is consumed in _after_menu_command).
                self._clear_message()
                self._render_pending = True
                return None
            # Repaint only when something changed. A "noop" feed (e.g. mouse-motion reports from a
            # mouse moving over the popup) leaves needs_render False, so we keep reading without
            # repainting — no title flash, no per-move slowdown.
            if needs_render:
                self._set_message(modal.render_message(), seconds=60)
                self._render()
                needs_render = False
            data = self._popup_read_input()
            menu_key = getattr(self.input, "menu_key", b"\x07")
            if menu_key and menu_key in data and not getattr(modal, "in_paste", False):
                # The menu key while a popup is open closes the WHOLE menu (a toggle), matching
                # the palette. Treat it as a cancel, but flag the full-close so the unwind above
                # doesn't stop one level up.
                self._exit_menu_requested = True
                self._clear_message()
                self._render_pending = True
                return None
            action, value = modal.feed(data)
            if action == "noop":
                continue  # nothing changed (e.g. a mouse-move report) — don't repaint
            if action == "redraw":
                needs_render = True
                continue
            if action == "done":
                self._clear_message()
                self._render_pending = True
                return value
            if action == "cancel":
                self._clear_message()
                self._render_pending = True
                if self._finalized_on_exit:
                    # Esc on ANY popup shown during the exit/finalize sequence cancels the WHOLE
                    # exit — the user changed their mind. _run_exit_flow / _finalize_pending_work
                    # see this flag and stay running; commits/merges already made this exit are
                    # kept and nothing is deleted. _finalized_on_exit is set only for the duration
                    # of the exit finalize, so this never fires for ordinary (non-exit) popups.
                    self._exit_aborted = True
                return None
            if action == "exit":
                if self._finalized_on_exit and not self._exit_confirmation_active:
                    # Ctrl-C inside a popup shown DURING the exit/finalize sequence that is NOT
                    # the exit-confirmation dialog — the keep/delete-worktree prompt, the copy-back
                    # offer, the on-exit user-commit box. Only the exit-confirmation dialog may
                    # exit aGiTrack on Ctrl-C; here it must NEVER exit directly. Treat it like Esc:
                    # cancel this popup and abort the whole exit so nothing is deleted and aGiTrack
                    # stays running. Clearing the force flag is essential — a double-Ctrl-C used to
                    # confirm the exit set it, and without clearing it the abort would be overridden
                    # (the exit would proceed). The user can press Ctrl-C again at the agent to
                    # re-open the exit confirmation. ``_finalized_on_exit`` (not _popup_exit_pending)
                    # gates this so it also covers finalize reached on a restart / backend-exit.
                    self._clear_message()
                    self._render_pending = True
                    self._exit_aborted = True
                    self._popup_exit_force = False
                    return None
                if self._run_exit_flow():
                    return None
                # Exit declined: the exit flow painted over us, so repaint the modal and keep listening.
                needs_render = True

    def _on_main_thread(self) -> bool:
        """Whether the caller is the main reactor thread (or no worker is running,
        as in tests/early construction, where everything is effectively 'main')."""
        return self._main_thread_ident is None or threading.get_ident() == self._main_thread_ident

    def _run_modal_via_main(self, modal: "PromptModal | SelectModal") -> "str | None":
        """Worker → main hand-off: queue *modal* for the reactor thread to present
        and block until it returns the user's answer. The git worker has no access
        to stdin or the screen, so every dialog it raises is shown by the main
        thread (which drains ``_modal_mailbox`` in its timers phase)."""
        request = _ModalRequest(modal)
        self._modal_mailbox.put(request)
        self._wake_main_loop()  # don't wait for the next poll tick to present it
        request.done.wait()
        return request.result

    def _drain_modal_mailbox(self) -> None:
        """Main thread: present every modal the worker queued, returning each answer
        to the blocked worker. Safe to call when no worker is running (no-op)."""
        while True:
            try:
                request = self._modal_mailbox.get_nowait()
            except queue.Empty:
                return
            try:
                request.result = self._run_modal_inline(request.modal)  # on main thread
            finally:
                request.done.set()

    def _wake_main_loop(self) -> None:
        """Wake the reactor out of ``select`` at once (e.g. a worker queued a dialog
        or finished work that should repaint). Writes a byte to the self-pipe whose
        read end is in the select set. No-op before the waker exists (tests)."""
        if self._waker is None:
            return
        self._waker.wake()

    def _acquire_pipeline_lock_from_main(self) -> None:
        """Acquire ``_pipeline_lock`` from the main thread WITHOUT deadlocking on a
        worker that holds it while blocked on a modal: keep draining the modal
        mailbox while we wait, so the worker's dialog is serviced and it can release.
        Pair every call with ``self._pipeline_lock.release()``.

        ``_pipeline_lock_wanted`` asks the git worker to stop taking the lock until we have
        had it. Python locks are not fair: a worker that re-acquires the instant it releases
        barges ahead of a thread already waiting, and this call is on the SUBMIT path, so
        that shows up directly as the delay between pressing Enter and anything happening."""
        self._pipeline_lock_wanted += 1
        try:
            while not self._pipeline_lock.acquire(timeout=0.02):
                self._drain_modal_mailbox()
        finally:
            self._pipeline_lock_wanted -= 1
        self._drain_modal_mailbox()

    def _prompt_popup(
        self, title: str, prompt: str, *, default: str = "", detail: list[str] | None = None
    ) -> str | None:
        """Free-text input popup.  Thin facade over ``_run_modal(PromptModal(...))``.

        Tests stub this method heavily; the name and signature are stable. ``detail`` is an
        optional list of lines (e.g. the files being committed) shown between the title and the
        input, windowed and PgUp/PgDn-scrollable when it overflows.
        """
        return self._run_modal(PromptModal(title, prompt, default=default, detail=detail, viewport_rows=self.rows))

    def _select_popup(self, title: str, options: list[str], *, detail: list[str] | None = None) -> str | None:
        """Selection popup.  Thin facade over ``_run_modal(SelectModal(...))``.

        Tests stub this method heavily; the name and signature are stable.
        Returns ``""`` immediately when *options* is empty (unchanged behaviour).
        ``detail`` is an optional list of lines (e.g. a file list) shown between the
        title and the options, windowed and PgUp/PgDn-scrollable when it overflows.
        """
        if not options:
            return ""
        return self._run_modal(SelectModal(title, options, detail=detail, viewport_rows=self.rows))

    def _create_user_commit_popup(
        self,
        *,
        repo: GitRepo | None = None,
        state: AgitrackState | None = None,
        include_declined: bool = False,
    ) -> bool:
        # Defaults to the active worktree (capturing uncommitted worktree changes
        # before the next prompt). The user-facing `git-commit` command passes
        # the base repo/state instead, since the user's own edits live there.
        # ``include_declined`` re-offers every untracked (added-but-unstaged) file at the
        # stage prompt — used for the base-repo paths, where the user is explicitly
        # committing, so a file is always offer-able and never permanently un-stageable.
        # The automatic worktree capture keeps it False so the agent's own untracked
        # decline still sticks.
        on_worktree = repo is None
        repo = repo or self.repo
        state = state or self.state
        if self._manual_commits:
            # Capture any agent turn that just finished (its parse may have completed while the
            # user was opening this menu) as a latent commit BEFORE folding, so its metadata/
            # trace are included rather than raced past. A turn still mid-parse is recorded on
            # the next loop tick; a commit made mid-parse OUTSIDE aGiTrack can't be intercepted.
            try:
                self._finish_agent_parse_if_ready(quiet=True)
            except Exception as error:
                self._debug(f"manual pre-commit turn flush failed: {error!r}")
        if on_worktree:
            self._ensure_turn_branch()  # turn branches are a worktree concept only
        repo.add_tracked()
        self._review_untracked_popup(include_declined=include_declined, repo=repo, state=state)
        if not repo.has_staged_changes():
            return False
        # Show exactly which files this commit will capture AND where it lands (the base repo vs
        # an isolated session worktree), so the user isn't typing a message blind or unsure which
        # tree they're committing to. Scrollable in the popup when there are many files.
        try:
            changed = repo.staged_changes()
        except Exception:
            changed = []
        repo_path = getattr(repo, "repo", None)
        base_path = getattr(self.base_repo, "repo", None)
        is_base = base_path is not None and str(repo_path) == str(base_path)
        where = "the BASE repo" if is_base else "this session's WORKTREE"
        detail = [f"Committing {len(changed)} file(s) to {where} ({repo_path}):", *changed]
        message = ""
        # Esc cancels the prompt (_prompt_popup returns None) and we continue without
        # committing — note that in the prompt so the user knows it's a safe way out.
        prompt = "Commit message (press Esc to continue without committing):"
        while not message.strip():
            result = self._prompt_popup("User Commit", prompt, detail=detail)
            if result is None:
                return False
            message = result
            prompt = "Commit message is required — enter one, or press Esc to continue without committing:"
        if self._manual_commits:
            # Manual-commit mode: fold the pending latent turns' tracking into this one
            # commit inline, so it's fully tracked whether or not the prepare-commit-msg
            # hook is installed (the hook is idempotent and skips a message that already
            # carries the metadata header). Uses the session's own latent ref, not the
            # passed state, since that's where the turns were recorded.
            commit_message = (
                message.strip()
                + "\n\n"
                + build_manual_squash_trailer(
                    agitrack_session_id=self.state.session_id,
                    latent_bodies=self._manual_pending_bodies(),
                    # getattr, not `repo.repo`: this path is reached with a repo STAND-IN in
                    # tests, and an unknown root only means "redact nothing" (fail-open), which
                    # is never worth an AttributeError on the user's commit.
                    repo_root=getattr(repo, "repo", None),
                )
            )
        else:
            commit_message = build_user_commit_message(
                message=message, agitrack_session_id=state.session_id, repo_root=getattr(repo, "repo", None)
            )
        try:
            repo.commit(commit_message)
        except Exception as error:
            # A failed commit (a repo pre-commit hook rejecting it, a git config/identity problem,
            # a racing change) must NOT crash aGiTrack — it used to propagate as an uncaught
            # GitError and exit with "Command failed: git commit -F -" on the console. Surface the
            # git error and leave the changes staged so the user can fix the cause and retry.
            self._debug(f"user commit failed: {error!r}")
            self._set_message(
                f"Commit failed — your changes are left staged, unchanged:\n{error}",
                seconds=15.0,
            )
            self._render()
            return False
        if self._manual_commits:
            # The pending turns are now folded into HEAD: advance the latent ref onto it
            # (0 pending again) and refresh the baseline so the poll fallback doesn't also
            # cover this commit. Idempotent with the post-commit hook, which does the same.
            try:
                new_head = repo.rev_parse("HEAD")
                repo.update_ref(self._manual_ref(), new_head)
                self._manual_last_head = new_head
            except Exception as error:
                self._debug(f"manual ref reset after user commit failed: {error!r}")
            self._render_manual_trailer()
        state.clear_trace()
        return True

    def _review_untracked_popup(
        self, *, include_declined: bool, repo: GitRepo | None = None, state: AgitrackState | None = None
    ) -> str:
        repo = repo or self.repo
        state = state or self.state
        self._prune_declined_untracked(repo, state)
        untracked = repo.untracked_entries()
        declined = set(state.declined_untracked())
        candidates = untracked if include_declined else [path for path in untracked if path not in declined]
        if not candidates:
            return "No untracked files to review."
        # The file list goes in the modal's detail pane, which windows it to the terminal
        # height and scrolls with PgUp/PgDn — so every file stays reachable.
        listing = "\n".join(candidates)
        # A CHOICE, not typed text: the answer decides whether files enter the commit, and a
        # typed "[y/N]" box accepts anything — a stray keystroke, or an answer to a question
        # the user misread, silently took the default. Up/Down + Enter can only ever return
        # one of the offered options (Esc still cancels).
        stage_label = f"Stage all {len(candidates)} file(s)"
        # "Leave them unstaged" FIRST, matching the console pass's `[y/N]` default. This popup
        # led with "Stage all", so the TUI re-asked the question the console had just asked and
        # offered the OPPOSITE default: a bare Enter swept the user's own untracked files into
        # the commit — the one outcome that is hard to undo, on the most reflexive keypress.
        answer = self._select_popup(
            "Untracked Files",
            ["Leave them unstaged", stage_label],
            detail=listing.splitlines(),
        )
        if answer is None:
            return "Cancelled."
        if answer == stage_label:
            repo.stage_paths(candidates)
            state.remove_declined(candidates)
            return f"Staged {len(candidates)} untracked file(s)."
        state.add_declined(candidates)
        return f"Left {len(candidates)} untracked file(s) unstaged."

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
            # The MAIN working tree (no worktree) can hold the user's own untracked files alongside
            # the agent's, so this commit stages only what the turn itself created — the paths that
            # were not already untracked when the turn began. It must never ASK: this runs between
            # the agent finishing and the commit landing, and a modal there interrupts the user
            # mid-flow to answer for files the agent, not they, just wrote.
            def stage_untracked_fn(repo, state):
                declined = set(state.declined_untracked())
                existing = self.untracked_before_turn
                repo.stage_paths(
                    [path for path in repo.untracked_entries() if path not in declined and path not in existing]
                )
        else:
            # Non-interactive commit (worktree session or exit finalize): stage
            # the agent's new files too, except any the user intentionally declined.
            def stage_untracked_fn(repo, state):
                declined = set(state.declined_untracked())
                repo.stage_paths([path for path in repo.untracked_entries() if path not in declined])

        def on_commit_fn(sha, trace_text, is_cover):
            self._last_agent_commit_id = sha
            # The promise made to "keep them for your next turn" is now kept: this commit
            # carries those files, so they stop being agent work awaiting a commit.
            self._forget_kept_cancelled_work()
            self.events.emit(
                "commit",
                sha=(sha or "")[:12],
                type="cover" if is_cover else "agent",
                backend=self.state.backend,
                session=self._session_label(),
            )
            # Mark that this session saw a real turn this run (stable aGiTrack session id,
            # not the drift-prone backend id) so the exit-path auto-share knows there
            # is genuinely something new to push.
            self._sessions_with_activity.add(self.state.session_id)
            # Don't announce the commit yet: the "created" popup is shown only once
            # the commit is merged into the base branch (see _integrate_turn_or_conflict),
            # so the user is never told a commit landed before it actually has.
            self._commit_merged_pending = True
            self._commit_summarized = False
            # When the backend agent committed its own work, aGiTrack puts a merge-shaped
            # "cover" commit on top instead of amending (which would change the
            # agent's commit hashes). Tell the user briefly what just happened (#35).
            if is_cover:
                self._set_session_notice(
                    self._session_label(),
                    "aGiTrack is committing a cover — a wrapper commit over the agent's own commits "
                    "that adds the interaction trace without changing their hashes.",
                    seconds=8.0,
                )
            # The commit is made immediately; the LLM summary is computed in the
            # background and amended in afterwards (#8) so the UI never blocks. The
            # trace passed here is exactly the one that landed in the commit (built
            # inside commit_turns), which is the summarizer's sole input.
            self._start_commit_summary(sha, trace_text)

        # In manual-commit mode a turn may produce no net working-tree change (a
        # planning/Q&A turn, or one whose edits the user already folded into HEAD),
        # so it records no latent commit and on_commit_fn never fires. That is still
        # genuine session activity worth sharing — the user conversed with the agent
        # and tokens were spent — so mark it here at turn-detection time. Otherwise a
        # manual session whose last turn left the tree unchanged is silently skipped
        # by _auto_share_on_exit (which gates on _sessions_with_activity), which is
        # why "the last manual session couldn't be shared."
        if self._latent_tracking and turns:
            self._sessions_with_activity.add(self.state.session_id)

        # Commit(s) the backend agent made ITSELF that no aGiTrack commit accounts for yet —
        # e.g. one it ran mid-turn (`git commit`) before this now-complete turn finished. Its
        # lines are already in history under their own hash but this turn's token count spans the
        # work that produced them, so this commit lists them in ``covered_commits`` (and leads
        # with the explanatory note) in BOTH modes: worktree mode covers them with a merge-shaped
        # cover commit; no-worktree latent mode records them in the folded turn body. Previously
        # the latent path forced this empty, so a mid-turn agent commit was never attributed.
        #
        # Gated on the turn's final message having been sent: the exit finalize passes
        # ``require_complete=False`` to capture in-flight work, but covering a still-running turn's
        # own commit would attribute it before the turn finished. When the last turn is incomplete
        # we leave those commits uncovered — a later cycle (or a resumed session) covers them once
        # the final response lands. Normal turns always arrive complete, so this is a no-op there.
        # An INTERRUPTED turn counts as finished (`turn_is_finished`): no later response is coming,
        # so waiting for one strands the attribution of a commit the agent made before the Esc.
        turn_complete = bool(turns) and turn_is_finished(turns[-1])
        uncovered = self._uncovered_backend_commits() if turn_complete else []
        # The agent committed its own work MID-TURN and left nothing further to commit, so the
        # tree is clean. The latent gate only fires on a CHANGED tree, so it would record
        # nothing at all — and the agent's commit carries only an in-flight block, which
        # explicitly promises the trace and tokens land later. Both paths then decline and the
        # turn's whole accounting is lost, silently.
        #
        # In AUTO mode take the COVER path instead (the metadata-only commit worktree mode
        # already makes over such hashes). Manual mode must not commit for the user, so it keeps
        # the latent path — `_manual_gate` widens to record on a clean tree when commits are
        # owed, and the record waits for the user's next commit to fold it in.
        use_latent = self._latent_tracking
        if uncovered and self._noworktree_auto:
            try:
                if self.repo.snapshot_worktree_tree() == self.repo.comparable_tree("HEAD"):
                    use_latent = False
            except Exception as error:
                self._debug(f"clean-tree cover check failed: {error!r}")
        committed = CommitEngine(
            self.repo,
            self.state,
            debug_fn=self._debug,
            # The --full-agent-messages flag forces the option on for this run; None
            # otherwise so the per-repo config decides.
            full_agent_messages=self._full_agent_messages or None,
        ).commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=stage_untracked_fn,
            pre_commit_fn=self._ensure_turn_branch,
            on_commit_fn=on_commit_fn,
            session_name=self.name,
            # Every no-worktree mode (manual OR auto) diverts the per-turn write to a hidden
            # latent commit and never touches HEAD/the index; the trailer/hook fold it into the
            # user's or agent's own commit (no-worktree auto also folds it itself, see
            # _auto_fold_latent_pending). In manual-record mode commit_turns never makes a cover —
            # ``backend_commits`` only feeds the recorded body's ``covered_commits`` metadata.
            manual_gate_fn=self._manual_gate if use_latent else None,
            manual_record_fn=self._manual_record if use_latent else None,
            # The latent path never stages, so the engine cannot read the commit's contents
            # off the index; this reports them from the snapshot instead (interrupted turns).
            changed_paths_fn=self._manual_changed_paths if use_latent else None,
            backend_commits=uncovered,
        )
        if committed and uncovered and self._latent_tracking:
            # The folded turn body now attributes these agent-made commits via ``covered_commits``.
            # Advance the no-worktree cover anchor past them so a LATER turn's body (recorded before
            # this one folds) can't list the same hashes again — the daemon does the same with its
            # ``tracked_head`` watermark. HEAD is unchanged by the latent record (it sits at the
            # agent's own commit), so this pins the anchor there.
            try:
                self._set_noworktree_base_head(self.repo.rev_parse("HEAD"))
            except Exception as error:
                self._debug(f"no-worktree cover anchor advance failed: {error!r}")
        return committed

    # --- the no-worktree coverage anchor (persistent) -------------------------
    #
    # Everything after this sha on the branch is work aGiTrack is responsible for accounting
    # for; everything before it is the user's pre-existing history and must never be claimed.
    #
    # PERSISTED, and deliberately in the same file the background daemon uses. It used to live
    # only in memory and be re-initialised to the current HEAD on every start, which lost
    # coverage exactly when it mattered most: if the agent committed mid-turn (leaving an
    # in-flight block that still owes its trace and tokens) and aGiTrack restarted before the
    # turn finished, the anchor moved forward ONTO that commit, so it could never be seen as
    # uncovered again and its accounting was gone for good. The daemon had already solved this
    # with `tracked-head`; sharing the file also means switching between `-b` and the
    # interactive TUI carries the watermark across instead of resetting it.

    def _noworktree_anchor_path(self) -> Path | None:
        # In no-worktree mode `repo` IS the base checkout, so it is the right root either way;
        # `base_repo` is preferred only because it is the durable one, and it is not always set
        # yet (early startup, and tests that build a runner directly). None when neither is —
        # the anchor is then in-memory only, exactly as it was before it became persistent.
        root = self.base_repo if self.base_repo is not None else self.repo
        return (root.repo / ".agitrack" / "tracked-head") if root is not None else None

    def _load_noworktree_anchor(self) -> None:
        """Restore the persisted anchor, or start it at the current HEAD on a repo never tracked
        before (so pre-existing history is never retroactively attributed to the agent)."""
        path = self._noworktree_anchor_path()
        try:
            saved = path.read_text(encoding="utf-8").strip() if path is not None else ""
        except OSError:
            saved = ""
        if saved:
            try:
                # `rev-parse` echoes a well-formed sha back without checking it EXISTS, so the
                # object store is what decides — a watermark naming a commit this repo no longer
                # has (a re-clone) must re-anchor rather than leave every later scan running from
                # a sha git cannot resolve. And presence alone is not enough either:
                # ANCESTOR of HEAD, not merely present. A watermark can survive as an object while
                # ceasing to describe this branch — any history rewrite does it: a rebase, a squash, an
                # amend of an earlier commit, `reset --hard` onto a different line, or a force-push
                # someone else made. `git log <orphan>..HEAD` still succeeds against such a sha, so the
                # scan silently runs over the WRONG range: it walks commits that were already accounted
                # for, and can re-attribute them. (Observed for real after rewriting this repo's history:
                # the anchor pointed at a commit that no longer existed on the branch.) Re-anchoring at
                # HEAD is the safe recovery — it can only ever under-claim, never attribute the user's
                # own history to the agent.
                if self.repo.has_object_local(saved) and self.repo.is_ancestor(saved, "HEAD"):
                    self._noworktree_base_head = self.repo.rev_parse(saved)
                    return
                # VISIBLE, not just a debug line. Every failure path in this area used to log to
                # a sink nobody reads without --verbose, so a silent loss of AI attribution could
                # never be noticed — the single worst property this feature can have. Re-anchoring
                # means commits between the old anchor and HEAD will not be attributed, and the
                # user is the only one who can judge whether that matters.
                self._debug(f"no-worktree anchor {saved!r} no longer describes this branch; re-anchoring")
                self._set_message(
                    "aGiTrack: the git history was rewritten since this repo was last tracked, so the "
                    "coverage marker was reset. Agent commits made before the rewrite won't be attributed.",
                    seconds=12.0,
                )
            except Exception as error:
                self._debug(f"stale no-worktree anchor {saved!r}: {error!r}")
        try:
            self._set_noworktree_base_head(self.repo.rev_parse("HEAD"))
        except Exception as error:
            self._debug(f"no-worktree cover anchor failed: {error!r}")

    def _set_noworktree_base_head(self, sha: str | None) -> None:
        self._noworktree_base_head = sha
        if not sha:
            return
        path = self._noworktree_anchor_path()
        if path is None:
            return  # no repo to persist against; the in-memory anchor still works for this run
        try:
            path.parent.mkdir(parents=True, exist_ok=True)  # .agitrack/ may not exist yet
            try:
                self.state.ensure_local_ignore()  # keep .agitrack/ out of the user's commits
            except Exception as error:  # a stub state in tests, or an unwritable exclude file
                self._debug(f"local ignore for the anchor failed: {error!r}")
            path.write_text(sha + "\n", encoding="utf-8")
        except OSError as error:
            self._debug(f"no-worktree anchor write failed: {error!r}")

    def _uncovered_backend_commits(self) -> list[str]:
        """Commits the backend created itself that no aGiTrack commit accounts for
        yet (#35). Full SHAs, oldest first. Backend commits keep their own messages
        forever (their hashes must stay stable, #58), so "covered" cannot be read off
        the commit itself: an aGiTrack metadata commit covers everything before it on
        the branch (its ``covered_commits`` named them), leaving only commits NEWER
        than the newest metadata commit unaccounted for.

        Worktree mode: commits ahead of base on the session's managed turn branch.
        No-worktree mode (only when actually running with --no-worktree): commits on
        the current branch made since this session started (anchored at the startup
        HEAD so pre-existing user history is never covered) — that's where the agent's
        changes legitimately live without a worktree."""
        try:
            if not self._use_worktrees:
                anchor = self._noworktree_base_head
                if not anchor:
                    return []
                head_ref = "HEAD"
            else:
                if self.worktree is None or self._base_branch is None:
                    return []
                branch = self.repo.current_branch()
                if not is_managed_branch(branch):
                    return []
                anchor = self._base_branch
                head_ref = branch
            uncovered: list[str] = []
            for sha in self.repo.log_shas(anchor, head_ref):  # oldest first
                # A commit stamped only with an IN-FLIGHT block does not reset the list: it
                # carries attribution but not the turn's trace/tokens, so it is still uncovered.
                if is_fully_tracked_message(self.repo.commit_message(sha)):
                    uncovered = []
                elif self.repo.arrived_from_elsewhere(sha):
                    # Merged/pulled in rather than written here (a `git merge main`, a PR
                    # merged on GitHub): not the agent's work, so never claim it — and it
                    # accounts for nothing, so it must not reset the list either.
                    continue
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
        # The auto-commit confirmation, shown only once the commit has landed in
        # the base branch (worktree sessions) or been committed and summarized
        # (no-worktree sessions). Includes the short SHA so the user can find the
        # commit (e.g. `git show <id>`), the session it belongs to (background
        # sessions auto-commit too), the base it landed on, and whether it was
        # summarized.
        commit_id = self._last_agent_commit_id
        session = self._session_label()
        base = self._base_branch or "the base branch"
        summarized = " (summarized)" if self._commit_summarized else ""
        if self._manual_commits:
            # Manual mode: the turn is recorded on the hidden latent ref, NOT committed to
            # the branch — say so, and point at how to commit for real.
            return (
                f"Recorded agent turn {commit_id or ''} in session '{session}'{summarized} — "
                f"commit when ready ({self._menu_label()} → git-commit) to fold it into {base}."
            )
        head = f"Created <aGiTrack> commit {commit_id}" if commit_id else "Created <aGiTrack> commit"
        return f"{head} in session '{session}' — merged into {base}{summarized}."

    def _announce_agent_commit(self) -> None:
        # Surface the deferred "created" notice for the session whose commit just
        # landed — at integration time for a worktree session (the commit is now
        # in the base), or after the commit/summary for a no-worktree session
        # (which never integrates). Only fires when there is a just-made agent
        # commit to announce; a base fast-forward with nothing pending, or an
        # already-announced commit, stays silent. Uses a session notice (not a
        # sticky message) so it never stomps the "summarizing…" notice and the
        # ordering — summarizing first, then created — is preserved.
        if not self._commit_merged_pending:
            return
        self._commit_merged_pending = False
        self._set_session_notice(self._session_label(), self._agent_commit_message())
        self._commit_summarized = False
        # Sync an opted-in shared session to GitHub HERE — piggy-backing on the
        # commit so the network round-trip happens at the commit cadence, not on a
        # frequent timer (issue #55, avoid hammering the remote).
        self._maybe_auto_share_active()

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
        from agitrack.summaries import Summarizer, summary_scratch_dir
        from agitrack.backends import backend_class as _backend_class, backend_name as _backend_name

        from agitrack.summaries.model_select import compatible_summarization_model

        backend_name = _backend_name(self.state.backend)
        backend_class = _backend_class(backend_name)
        model = self.state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        # The summarizer runs THIS session's backend; a summarization_model configured for a
        # different backend (e.g. a Claude id while the session runs OpenCode) is invalid there and
        # makes every summary fail — drop it and use the backend's default instead.
        model = compatible_summarization_model(backend_name, model)
        if model is None and backend_name != "claude":
            # Fall back to the model this SESSION runs, not to the backend's own global default.
            # The summarizer deliberately runs OUTSIDE the repo (see below), so a PROJECT-scoped
            # model pin cannot apply there — and the backend then reaches for whatever its global
            # default is, which on a machine that pins models per project is one the user may
            # have no access to at all. Measured live on OpenCode: EVERY summary failed this way
            # ("summarizer backend exited with 1", a 403 from the default provider), silently,
            # leaving raw prompts as commit subjects where Claude sessions got real summaries.
            # Codex has the same project-scoped pin (`.codex/config.toml`) and so the same
            # exposure. Claude is left alone: its CLI's own default is the right choice there,
            # and the recorded session model can carry a variant suffix its `--model` flag
            # would reject.
            model = compatible_summarization_model(backend_name, self.state.model)
        # The summarizer must NOT run in the session worktree (or the repo):
        # its headless calls record real backend sessions keyed by cwd, which
        # the parse worker / exit adoption would then resume instead of the
        # user's conversation (issues #8/#56).
        # Honour a configured backend wrapper for the summarizer's headless calls too,
        # so the agent binary is always launched the same way the user asked.
        launch = self._launch_command()
        return Summarizer(backend_class(summary_scratch_dir(), launch_command=launch or None), model=model)

    def _start_commit_summary(self, sha: str, trace_text: str) -> None:
        # On a NON-interactive teardown — a closed terminal / signal exit, where ``screen`` is
        # already None — skip a fresh summarizer call: there is no one watching and teardown
        # must stay fast (a full LLM round-trip per session is the main reason exit used to drag
        # on for tens of seconds). On an INTERACTIVE exit the user is present and has opted into a
        # slightly slower exit, so we DO summarize the turn committed at exit-finalize and give it
        # a bounded grace to land (_finalize_summary_then_integrate_on_exit joins it), falling back
        # to the commit's prompt-based message if it doesn't finish in time.
        if self._exiting and self.screen is None:
            return
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        # The owning session: summary state is per session (so two sessions can
        # summarize at once), and the worker writes its result back to THIS
        # session even if the user switches away before it finishes.
        owner = self.active
        if owner._summary_thread is not None and owner._summary_thread.is_alive():
            # One summary at a time PER SESSION; a turn committed before this
            # session's previous summary finished keeps its prompt-based message.
            self._debug(f"summary worker busy for '{self._session_label()}'; skipping summary for {sha}")
            return
        try:
            full_sha = self.repo.rev_parse(sha)
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
        owner._summary_result = None
        owner._summary_pending = {"sha": full_sha, "since": time.monotonic()}
        model = self.state.model

        def worker() -> None:
            try:
                result["summary"] = summarizer.summarize_commit(trace=trace_text)
            except Exception as error:  # surfaced by the service tick
                result["error"] = repr(error)
            else:
                try:
                    result["session_summary"] = summarizer.update_session_summary(
                        current_summary=session_summary,
                        trace=trace_text,
                        commit_summary=result["summary"],
                    )
                except Exception as error:
                    # A failed rolling summary must not discard a good commit
                    # summary; the previous session summary simply stays current.
                    result["session_summary_error"] = repr(error)
            result["metadata"] = summary_metadata_lines(
                model=summarizer.model or model,
                tokens_input=summarizer.tokens_input,
                tokens_output=summarizer.tokens_output,
                tokens_cache_read=summarizer.tokens_cache_read,
            )
            # Write back to the OWNING session (not self.active, which may have
            # changed): the service tick swaps each session in to apply it.
            owner._summary_result = result

        owner._summary_thread = threading.Thread(target=worker, daemon=True, name="agit-summary")
        owner._summary_thread.start()
        self._set_session_notice(
            self._session_label(),
            f"aGiTrack is summarizing the latest commit in session '{self._session_label()}'…",
            seconds=self.SUMMARY_WAIT_SECONDS,
        )

    def _service_commit_summaries(self) -> None:
        """Apply finished background summaries for every session. Each session has
        its own summary worker/result (so two can summarize concurrently), so the
        active session is serviced in place and the rest via a context swap."""
        self._service_commit_summary()  # active session, in place
        for session in list(self.sessions):
            if session is not self.active:
                self._with_session(session, self._service_commit_summary)

    def _service_commit_summary(self) -> None:
        """Main-loop tick: apply this session's finished background summary (#8).
        All git and state mutations happen here, on the main thread."""
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
            self._set_session_notice(
                session,
                f"aGiTrack: commit summarization failed in session '{session}'; keeping the prompt-based message.",
            )
            return
        sha, summary, repo, state = result["sha"], result["summary"], result["repo"], result["state"]
        try:
            # Amend first, then attach notes to whatever commit survives — notes
            # added before an amend would hang off the orphaned pre-amend object.
            target = self._amend_summary_into_head(repo, sha, summary, result.get("metadata"))
            if target:
                # The commit is now summarized; the "created" notice (set at
                # integration for a worktree session, or just below for a
                # no-worktree one) will note this. No separate "summary added"
                # popup — that fired before the merge and confused the ordering.
                self._commit_summarized = True
            else:
                # Already integrated or superseded: the summary lives in the
                # commit's git notes instead of its message. Prefer the commit that
                # ABSORBED this latent turn (a multi-turn auto fold, which must not be
                # amended) over the latent sha itself — a note on a latent commit hangs
                # off an object no branch reaches, so `git log --notes` never shows it
                # and `git gc` eventually collects it.
                fold = getattr(self, "_last_auto_fold", None)
                target = sha
                if fold and sha in (fold.get("latent") or ()) and fold.get("head"):
                    target = str(fold["head"])
                self._debug(f"summary for {sha} recorded as notes only on {target}")
            repo.notes_add(target, summary, namespace="agitrack/commit-summary")
            if self._manual_commits:
                # Manual mode: the summary just landed as a note on a pending latent commit —
                # refresh the trailer so a subsequent commit folds the summarized message in.
                self._render_manual_trailer()
            session_summary = result.get("session_summary")
            if session_summary:
                state.session_summary = session_summary
                state.session_summary_commit = target
                repo.notes_add(target, session_summary, namespace="agitrack/session-summary")
            elif "session_summary_error" in result:
                self._debug(f"session summary update failed: {result['session_summary_error']}")
            if self.worktree is None:
                # No integration step will announce a no-worktree commit, and the
                # summary has now landed, so surface the "created (summarized)"
                # notice here — replacing the "summarizing…" notice in the right
                # order. Idempotent: a no-op if the commit was already announced.
                self._announce_agent_commit()
        except Exception as error:
            self._debug(f"applying commit summary failed: {error!r}")

    def _folded_head_for(self, sha: str, head: str) -> str | None:
        """HEAD, when it is the auto-fold commit that absorbed latent commit *sha* as its ONLY
        turn — else None.

        No-worktree AUTO mode summarizes the **latent** commit, then folds that latent commit into
        a real commit on the working branch. So by the time a slow summary lands, its sha is never
        HEAD and :meth:`_amend_summary_into_head` used to give up, stranding the summary as a note
        on a commit that is not in any branch's history — unreachable, and lost to `git gc`. Every
        commit in the reported repo lost its summary this way. Mapping the latent sha back to the
        fold commit gives the late summary the same second chance worktree mode has always had.

        Restricted to single-turn folds: a multi-turn fold's message is a squash of several turn
        bodies, and this summary describes only one of them, so it goes to notes instead."""
        fold = getattr(self, "_last_auto_fold", None)
        if not fold or fold.get("head") != head or not fold.get("single"):
            return None
        return head if sha in (fold.get("latent") or ()) else None

    def _amend_summary_into_head(self, repo, sha: str, summary: str, metadata: list[str] | None) -> str | None:
        """Amend the summary into *sha*'s message, only while that is safe:
        the commit is still HEAD (or was just folded into HEAD, see :meth:`_folded_head_for`),
        not yet integrated into base, and nothing is staged (``--amend`` would otherwise pull
        staged work into the commit).
        Returns the amended commit's full SHA, or None if no amend happened."""
        try:
            head = repo.rev_parse("HEAD")
            if head != sha and self._folded_head_for(sha, head) is None:
                return None
            # Worktree mode: the commit sits on a turn branch ahead of base; once merged into
            # base, amending would rewrite integrated history, so only amend while it's still
            # unintegrated (sha in base..HEAD). No-worktree mode: the commit is made DIRECTLY on
            # the working branch (HEAD == base), so base..HEAD is always empty — that check would
            # wrongly skip every amend, leaving the summary as notes-only and the commit message
            # without a summary. There it's enough that the commit is still HEAD with nothing
            # staged, so skip the containment check.
            if self.worktree is not None:
                if self._base_branch is None or sha not in repo.log_shas(self._base_branch, "HEAD"):
                    return None
            if repo.has_staged_changes():
                return None
            message = repo.commit_message("HEAD")
            amended = apply_summary_to_message(
                message, summary, summary_metadata=metadata, repo_root=getattr(repo, "repo", None)
            )
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
        self._message_scroll = 0  # a fresh message starts at the top
        # Sticky messages ignore the timeout and persist until the user's next
        # keypress clears them (see _clear_sticky_message_on_input).
        self._message_sticky = sticky
        # A one-off message takes over the popup from the per-session notices; the
        # notices reappear (via _service_session_notices) once it expires.
        self._notice_shown = False
        # Request a repaint so the popup actually shows. Without this a message set
        # from the background idle loop (e.g. the auto-commit confirmation, set when
        # the agent has gone quiet and produces no output to trigger a render) would
        # never be painted.
        self._render_pending = True

    def _clear_message(self) -> None:
        self.message = None
        self.message_until = 0.0
        self._message_sticky = False

    # ------------------------------------------------------------------
    # Per-session status notices (commit/summary lifecycle, multi-line)
    # ------------------------------------------------------------------
    #
    # Commit/summary progress is reported per session, keyed by session name, so
    # several concurrent sessions each get their own line in the popup instead of
    # clobbering one shared message. The notices compose into ``self.message``;
    # one-off ``_set_message`` calls (errors, menu results) temporarily take over
    # and the notices reassert themselves on the next service tick.

    def _set_session_notice(self, name: str, text: str | None, *, seconds: float = 6.0) -> None:
        """Set, replace, or (text=None) clear the status line for session *name*."""
        if text is None:
            self._session_notices.pop(name, None)
        else:
            self._session_notices[name] = (text, time.monotonic() + seconds, False)
        self._refresh_notice_message()

    def _refresh_notice_message(self) -> None:
        # Drop expired notices, then reflect the live ones into the popup. Skips
        # over a still-showing one-off message so an error/confirmation is not
        # stomped; that message's expiry brings the notices back on a later tick.
        now = time.monotonic()
        self._session_notices = {n: v for n, v in self._session_notices.items() if v[1] > now}
        one_off_showing = (
            not self._notice_shown and self.message is not None and (self._message_sticky or now < self.message_until)
        )
        if one_off_showing:
            return
        lines = [text for text, _until, _sticky in self._session_notices.values()]
        if lines:
            composed = "\n".join(lines)
            # Only request a repaint when the popup's content actually changes.
            # _service_session_notices runs every reactor tick, so re-flagging a
            # render each time (even when the text is unchanged) forces a
            # full-frame repaint at tick cadence — which flickers the notice popup
            # on terminals without synchronized-update support while the backend is
            # also streaming output. message_until is refreshed silently (it only
            # gates the render's "still showing" check, and the per-notice expiries
            # are fixed at set time, so updating it never needs a repaint).
            changed = not self._notice_shown or composed != self.message
            self.message = composed
            self.message_until = max(until for _t, until, _s in self._session_notices.values())
            self._message_sticky = False
            self._notice_shown = True
            if changed:
                self._render_pending = True
        elif self._notice_shown:
            # The notices just emptied and we owned the popup: clear it.
            self._clear_message()
            self._notice_shown = False
            self._render_pending = True

    def _service_session_notices(self) -> None:
        # Main-loop tick: expire per-line notices and keep the popup current (so a
        # finished line disappears even while another session's line lives on).
        if self._session_notices or self._notice_shown:
            self._refresh_notice_message()

    def _clear_sticky_message_on_input(self) -> bool:
        # The next keypress dismisses a sticky message. Returns True if one was
        # showing (so the caller can repaint to remove the popup).
        if self._message_sticky:
            self._clear_message()
            return True
        return False

    def _exit_confirmation_popup(self, title: str, options: list[str]) -> str | None:
        """A `_select_popup` marked as THE exit-confirmation dialog — the one and only popup
        where Ctrl-C is allowed to confirm/force the exit (the double-Ctrl-C fast path). Every
        other popup, including the keep/delete-worktree and copy-back prompts shown during the
        exit finalize, treats Ctrl-C as a cancel and never exits aGiTrack directly."""
        self._exit_confirmation_active = True
        try:
            return self._select_popup(title, options)
        finally:
            self._exit_confirmation_active = False

    def _confirm_exit(self) -> bool:
        # "(Ctrl-C again)" hints that a second Ctrl-C confirms the exit (the
        # double-Ctrl-C fast path in _run_exit_flow), matching the keystroke that
        # opened this prompt.
        title = "Exit aGiTrack?"
        # Manual-commit mode: the fold hooks are removed on exit, so a `git commit` made
        # AFTER aGiTrack quits won't attach the interaction tracking. Remind the user to
        # commit inside aGiTrack while they still can (the turns aren't lost either way —
        # they stay recorded and fold into the next commit made in a future aGiTrack run).
        pending = self._manual_pending_count()
        if pending:
            turns = "turn" if pending == 1 else "turns"
            # What happens next depends on whether the PERSISTENT auto-track hook will still be
            # there once aGiTrack is gone. When it is, a plain `git commit` in any terminal still
            # folds the tracking in — verified — so telling the user otherwise would push them
            # into committing before they are ready, in the one mode built around them choosing
            # when to commit. When it is not (a custom core.hooksPath, or autotrack_hook: off),
            # the old warning is the true one.
            if self._autotrack_hook_will_survive_exit():
                title = (
                    f"Exit aGiTrack? You have {pending} uncommitted agent {turns}. They're saved — your "
                    "next `git commit`, in any terminal, still folds in the interaction tracking."
                )
            else:
                title = (
                    f"Exit aGiTrack? You have {pending} uncommitted agent {turns}. Commit now in aGiTrack "
                    f"({self._menu_label()} → git-commit) to include the interaction tracking — with the "
                    "auto-track hook unavailable in this repo, a commit made afterwards won't."
                )
        choice = self._exit_confirmation_popup(title, ["No, keep working", "Yes, exit (Ctrl-C again)"])
        return choice == "Yes, exit (Ctrl-C again)"

    def _run_exit_flow(self) -> bool:
        # THE single exit path (P6 Stage 3 — exit-path unification).
        #
        # Every interactive way out of aGiTrack goes through here:
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
        # (finalize included). Returns True when aGiTrack is exiting.
        if self._popup_exit_pending:
            # A second Ctrl-C, inside one of the exit-confirmation popups: take
            # it as an emphatic yes — skip the questions, keep the finalize.
            self._popup_exit_force = True
            self._exit_requested = True
            return True
        self._popup_exit_pending = True
        self._popup_exit_force = False
        self._exit_aborted = False
        try:
            # Exiting always asks first (a deliberate safety net), regardless of
            # whether there is pending work. Whether there is anything to *finalize*
            # only affects the message shown during teardown (see _finalize_pending_work).
            if not self._confirm_exit() and not self._popup_exit_force:
                self._set_message("Exit cancelled — still running; nothing was shut down.")
                self._render()
                return False
            if not self._popup_exit_force:
                if not self._confirm_terminate_background_sessions() and not self._popup_exit_force:
                    self._set_message("Exit cancelled — kept working; the background sessions are still running.")
                    self._render()
                    return False
            head_before_finalize = self._base_head_sha()
            self._finalize_pending_work()
            if self._exit_aborted and not self._popup_exit_force:
                # Esc on an on-exit popup: undo the "we're exiting" state and stay running so
                # the user can deal with the files. Already-made commits/merges stand (they
                # never lose work); nothing was deleted.
                self._exiting = False
                self._finalized_on_exit = False
                # Finalize STOPPED the git worker (so its exit-time commits could run on this
                # thread without racing it). Staying running with no worker meant the automatic
                # commit pipeline was dead for the rest of the session: a finished turn sat
                # uncommitted until something else happened to run git on the main thread —
                # measured live at 3m25s.
                self._start_git_worker()
                # Finalize disabled host mouse/focus/paste reporting up front; we're staying
                # running, so restore it for the live session.
                self._enable_host_mouse()
                # Only claim commits when some were actually made: an abort at the keep/delete
                # question happens BEFORE `_commit_latest_turn_sync`, so the reassurance was
                # being printed over an exit that had committed nothing at all.
                committed = self._base_head_sha() != head_before_finalize
                self._set_message(
                    "Exit cancelled. Your worktree and its files were NOT deleted — copy/move what you "
                    "need, then exit again." + (" (Commits already made this exit were kept.)" if committed else ""),
                    seconds=12.0,
                )
                self._render()
                return False
            self._exit_child()
            self._exit_requested = True
            return True
        finally:
            self._popup_exit_pending = False

    def _base_head_sha(self) -> str | None:
        """The base repo's HEAD, or None when it can't be read. Used to tell whether a step
        actually committed anything before saying so."""
        try:
            return self.base_repo.rev_parse("HEAD")
        except Exception:
            return None

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
        choice = self._exit_confirmation_popup(
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
            if self._noworktree_auto:
                # In no-worktree AUTO the commit above only records a hidden LATENT commit; the
                # real one is made by the throttled poll (_auto_fold_latent_pending). Callers
                # reach here precisely when that poll is about to stop running — a modal is
                # opening, the session context is being swapped, the process is tearing down — so
                # "committed synchronously" has to include the fold, or the turn stays latent
                # until exit. That is the reported `/clear` bug: the switch handler commits before
                # its dialog for exactly this reason and the latent record alone did not honour
                # it. Forced past the summary-defer gate, as the pre-prompt fold is: a summary
                # that lands later amends into the fold commit (see _folded_head_for).
                self._auto_fold_latent_pending(force=True)
        except Exception as error:  # never block on a commit failure
            self._debug(f"sync commit failed: {error!r}")

    def _finalize_pending_work(self) -> None:
        # On a confirmed exit, make sure the latest completed agent turn is
        # committed for *every* session before aGiTrack leaves — otherwise quitting
        # right after a turn drops a commit the idle/stable debounce had not yet
        # made. (Background sessions are committed via the context swap.)
        if self._finalized_on_exit:
            return  # already finalized (e.g. the backend exited and we ran this)
        self._finalized_on_exit = True
        self._exiting = True
        # ALWAYS show a notice the moment exit begins, before any of the (sometimes
        # multi-second) teardown below — joining the git worker, pushing an
        # auto-shared session, integrating, removing worktrees, joining threads. A
        # silent terminal during that wait looks frozen. Name the specific work when
        # we can; otherwise a generic notice. (On a forced/terminal-closed exit the
        # screen is already None, so _render no-ops — harmless.) Computed before
        # stopping the worker so it still reflects any in-flight turn.
        detail = self._describe_exit_finalize()
        self._set_message(detail or "Finalizing things before exiting…", seconds=30)
        self._render()
        # Stop the host terminal reporting mouse/focus/paste NOW, before the (sometimes
        # multi-second) finalize below. Finalize doesn't service stdin, and parts of it run
        # cooked (prompts, backend teardown), so a mouse scroll while "Finalizing…" shows would
        # otherwise echo its raw SGR report (`\x1b[<…M`) onto the screen as literal text. The
        # backend is on its way out, so it doesn't need these events either. (Re-enabled on the
        # _exit_aborted path, where we stay running.)
        self._disable_host_terminal_modes()
        # Stop the git worker first so the exit-time foreground commits below run on
        # this (main) thread without racing a worker pass. After this join the
        # finalize git is the only git touching the worktree.
        self._stop_git_worker()
        self._cancel_inflight_shared_fetches()  # stop any unfinished session fetch at once
        # The session the user was working in when they quit is the one to
        # auto-resume next start — not necessarily the primary (they may have
        # switched to a new or shared session). It is finalized first, below.
        self._exit_resume_worktree = getattr(self.worktree, "name", None)
        # Decide keep-vs-delete for this run's worktrees ONCE, now, in the genuine active
        # context (before any per-session swap) so the prompt reflects the real foreground
        # screen and is asked a single time for the whole exit. Esc on it cancels the exit.
        self._should_delete_worktrees_on_exit()
        if self._exit_aborted:
            return
        self._commit_latest_turn_sync()  # active session, in place
        self._auto_share_on_exit()  # push the latest conversation if auto-shared
        self._finalize_summary_then_integrate_on_exit()
        if self._exit_aborted:
            return  # Esc on the active session's copy offer — leave the rest untouched
        for session in list(self.sessions):
            if session is self.active:
                continue
            saved = self.active
            self.active = session
            try:
                self._commit_latest_turn_sync()
                self._auto_share_on_exit()
                self._finalize_summary_then_integrate_on_exit()
            finally:
                self.active = saved
            if self._exit_aborted:
                return  # Esc on a session's copy offer — stop finalizing the rest
        # The keep/delete answer covers the idle worktrees the question listed too, so honor it
        # for them here — after every live session has been finalized above (a session's own
        # worktree is removed by its finalize, and must not be swept a second time as "idle").
        self._delete_idle_worktrees_on_exit()
        self._delete_orphan_merged_branches()
        # NB: deliberately NOT sweeping orphan shared-session snapshots here. That
        # sweep runs `git fsck` over the whole object graph — seconds on a large
        # repo — and would stall exit even when the session made no commits. It is
        # redundant anyway: each publish reclaims its
        # previous snapshot immediately, and the startup sweep (a background thread)
        # mops up any stragglers. So leave exit fast.

    def _finalize_summary_then_integrate_on_exit(self) -> None:
        # Give the current session's in-flight commit summary — the one the exit-finalize
        # commit just started on an interactive exit, or one already running from the live
        # path — a bounded grace to land so it can be amended into the commit before the
        # final integration; past that it is dropped to git notes rather than holding the
        # exit hostage (#8). Runs for the active session in place and for each background
        # session under a context swap.
        if self._summary_thread is not None and self._summary_thread.is_alive():
            self._summary_thread.join(timeout=self.EXIT_SUMMARY_GRACE_SECONDS)
        self._service_commit_summary()
        # No-worktree AUTO mode records each turn as a hidden LATENT commit; the real commit is
        # normally made by the throttled poll (_auto_fold_latent_pending). That poll does NOT run
        # during exit teardown, so fold any pending latent turns HERE — otherwise a turn finished
        # just before quitting is recorded latently but never lands on HEAD. Forced past the
        # summary-defer gate: the summary was already joined and serviced just above. A no-op in
        # every other mode, and idempotent (a clean tree vs HEAD means the live poll already folded).
        self._auto_fold_latent_pending(force=True)
        self._integrate_session_on_exit()
        if self._exit_aborted:
            return  # Esc on a finalize popup (e.g. a merge prompt) aborted the exit — keep the worktree
        self._finalize_worktree_on_exit()

    def _adopt_latest_backend_session(self) -> None:
        # The user may have switched conversations inside the backend itself (e.g.
        # Claude's native session picker), which leaves aGiTrack's tracked id stale.
        # Point the resume record at the worktree's most recent conversation so the
        # next launch restores what they were actually using, not the id aGiTrack
        # originally spawned.
        try:
            latest = self.backend.latest_session_id(self.repo.repo)
        except Exception as error:
            self._debug(f"adopt latest backend session failed: {error!r}")
            return
        if not latest or latest == self.state.backend_session_id:
            return
        tracked = self.state.backend_session_id
        if tracked and self._session_recency(latest, 0.0) < self._session_recency(tracked, 0.0):
            # Never adopt BACKWARDS. "Latest" is whatever the backend reports as this directory's
            # most recent conversation, and a conversation aGiTrack merely touched (staged,
            # mirrored, retargeted) can hold that title while carrying nothing newer than what we
            # are already on. Adopting it re-files this session's NAME onto a dead conversation
            # (`_persist_session_name` below), which is how a session came back, after a restart,
            # running a conversation the user had abandoned with `/clear` hours earlier.
            self._debug(f"not adopting {latest}: it is older than the tracked conversation")
            return
        if latest in self._sibling_worktree_conversations():
            # Not ours to adopt. A conversation that ran here and then moved to its own worktree
            # leaves a copy of its transcript behind, and on an old aGiTrack that copy can still
            # be the newest thing in this directory — so this would adopt a SIBLING's conversation
            # and, worse, re-file its name under this session (``_persist_session_name``). That
            # rewrite is what made a later start open this worktree running the other session's
            # conversation. The stale copy is cleaned up when the worktree next opens; until then,
            # simply decline it.
            self._debug(f"not adopting {latest}: it belongs to another session's worktree")
            return
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
            root = self._root_state()
            root.data["backend"] = self.state.backend
            root.data["backend_session_id"] = self.state.backend_session_id
            root.data["backend_session_repo"] = self.state.data.get("backend_session_repo")
            root.data["last_backend_message_id"] = self.state.last_backend_message_id
            root.data["model"] = self.state.model
            root.data["agitrack_session_id"] = self.state.session_id
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

    def _waitpid_nowait(self, pid: int) -> tuple[int, int]:
        """Non-blocking child-exit check.  Returns (pid, status) if exited, (0, 0) if still running.
        On POSIX: os.waitpid(WNOHANG).  On Windows: poll the ConPTY handle via poll_exited()."""
        if sys.platform == "win32":
            handle = self.active.process._handle
            if handle is not None and handle.poll_exited():
                return pid, 0
            return 0, 0
        return os.waitpid(pid, os.WNOHANG)

    def _reap_stopped_children(self) -> None:
        pids = self._reap_pids
        if not pids:
            return
        if os.name == "nt":
            # No zombie reaping on Windows: a ConPTY child is fully closed by terminate(),
            # and os.waitpid/os.WNOHANG don't exist there. Nothing to wait on.
            self._reap_pids = []
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

    def _idle_worktrees_on_exit(self) -> list[WorktreeInfo]:
        """This repo's session worktrees that are NOT open in this run.

        A worktree stops being a live session without being deleted, and more often than the
        exit question used to assume: ``/clear`` → "give it its own worktree" moves the
        conversation out and DROPS the session it left behind (``_relocate_switched_conversation``
        removes it from ``self.sessions``), stopping a session from the sessions menu keeps its
        worktree recoverable, and every worktree a previous run kept stays on disk (they are
        persistent by design). None of those are in ``self.sessions``, so an exit question built
        from live sessions alone named only some of the directories it was deciding about — and
        answering "Delete them" then left the rest on disk with no way to say otherwise. The
        decision covers all of them, so the question has to list all of them.
        """
        if not self._use_worktrees:
            return []
        live = {
            name for session in self.sessions if (name := getattr(getattr(session, "worktree", None), "name", None))
        }
        try:
            return [info for info in self._worktrees().list() if info.name not in live]
        except Exception as error:
            self._debug(f"exit idle-worktree scan failed: {error!r}")
            return []

    def _should_delete_worktrees_on_exit(self) -> bool:
        """Ask once per exit whether to delete this repo's session worktrees or keep them,
        spelling out their exact on-disk locations so the user can find them either way. The
        answer is cached and applied to EVERY session worktree — the live sessions' and the idle
        ones left by an earlier run or by a session that ended during this one. A signal teardown
        (no UI) defaults to KEEP. Pressing Esc CANCELS the exit (sets ``_exit_aborted``) rather
        than choosing for the user — so a stray keystroke never deletes or commits them to
        leaving."""
        if self._exit_delete_worktrees is not None:
            return self._exit_delete_worktrees
        # The exact directories the decision applies to: every live session's worktree, plus
        # every idle one on disk. Captured now, before the per-session finalize starts removing
        # them (which clears the very records this list is built from).
        listing: list[str] = []
        for session in self.sessions:
            wt = getattr(session, "worktree", None)
            if wt is not None and getattr(wt, "path", None) is not None:
                listing.append(str(wt.path))
        if not listing and self.worktree is not None:
            listing = [str(self.worktree.path)]
        idle = self._idle_worktrees_on_exit()
        self._exit_idle_worktrees = [info.name for info in idle]
        live_paths = set(listing)
        listing += [f"{info.path}  (idle — no session open in it)" for info in idle if str(info.path) not in live_paths]
        if not listing:
            # No worktrees at all (e.g. --no-worktree): nothing to decide, don't prompt.
            self._exit_delete_worktrees = False
            return False
        if self.screen is None:
            self._exit_delete_worktrees = False
            return False
        choice = self._select_popup(
            "Delete this repo's session worktree(s) on exit, or keep them for next time?\n"
            "Keeping preserves each session's full environment and any work-in-progress so it "
            "resumes instantly next time; deleting reclaims the disk (committed work has already "
            "been integrated into your branch). This covers the idle worktrees too — any one "
            "still holding uncommitted or unmerged work is kept regardless, so nothing is lost. "
            "The worktree(s) are at:\n\nLocation(s):",
            ["Keep them", "Delete them"],
            detail=listing,
        )
        if choice is None:
            # Esc → cancel the exit entirely; the caller (_finalize_pending_work) checks this
            # and stays running. Deliberately NOT cached, so the next exit asks again.
            self._exit_aborted = True
            return False
        self._exit_delete_worktrees = choice == "Delete them"
        return self._exit_delete_worktrees

    def _delete_idle_worktrees_on_exit(self) -> None:
        """Apply the exit-time DELETE answer to the worktrees no session had open.

        Same safety rule the live sessions get: a worktree is removed only once its work is
        provably in the base. ``_cleanup_stale_worktree`` integrates whatever it can and returns
        False for anything left uncommitted, mid-merge or conflicting — those keep their
        directory (and are surfaced again by the next startup reconcile) rather than being
        discarded on the way out. Nothing here can abort the exit: it runs after the last
        session has been finalized, with no UI left to ask anything."""
        names = self._exit_idle_worktrees
        self._exit_idle_worktrees = []
        if not names or not self._exit_delete_worktrees:
            return
        try:
            infos = {info.name: info for info in self._worktrees().list()}
        except Exception as error:
            self._debug(f"exit idle-worktree removal skipped: {error!r}")
            return
        for name in names:
            info = infos.get(name)
            if info is None:
                continue  # already gone (removed by a session finalize, or externally)
            try:
                if not self._cleanup_stale_worktree(info):
                    self._debug(f"keeping idle worktree '{name}' on exit: its work is not integrated")
                    continue
                self._worktrees().remove(name)
                self._debug(f"removed idle worktree '{name}' on exit (user chose delete)")
            except Exception as error:
                self._debug(f"exit removal of idle worktree '{name}' failed: {error!r}")

    def _finalize_worktree_on_exit(self) -> None:
        # On exit aGiTrack asks (once) whether to KEEP this run's session worktrees — preserving
        # each one's full environment and work-in-progress for an instant resume — or DELETE them
        # to reclaim disk. Either way we first persist the resume pointer. When keeping, we only
        # offer to copy newer/leftover worktree files into the base. When deleting, a worktree is
        # removed only once it is safe (fully merged, its files rescued via the copy offer);
        # unintegrated or unresolvable work keeps its worktree regardless of the choice.
        info = self.worktree
        if info is None or self._base_branch is None:
            return
        # Before anything else, remind about any edits the agent stranded in the base repo
        # (only possible on an unsandboxed host) — the worktree may be removed below, so this is
        # the last chance to point the user at work aGiTrack couldn't track.
        self._remind_stranded_base_edits_on_exit()
        # Persist the resume pointer. _persist_last_session_record runs
        # _adopt_latest_backend_session, which captures a conversation the user switched to
        # inside the backend's own picker (Claude's session view), and writes both the
        # worktree state and the repo-root "last session" record, so the right conversation
        # auto-resumes next start. Persist for the session the user was actually in when they
        # quit (set in _finalize_pending_work), so quitting from a freshly-started or resumed
        # *shared* session auto-resumes THAT next start, not the original primary.
        if info.name == (self._exit_resume_worktree or self._primary_worktree_name):
            self._persist_last_session_record()
        # Remember this session's conversation under its backend so switching back to that
        # backend (this run or a later one) resumes it.
        self._remember_session_for_backend()
        delete = self._should_delete_worktrees_on_exit()
        if self._exit_aborted:
            return  # Esc on the keep/delete prompt cancelled the exit
        if not delete:
            # KEEP: just offer to copy the worktree's not-merged / newer files into the base repo
            # so the user — who works in the base directory — has them too. Interactive only (a
            # signal teardown has no UI, and nothing is being deleted, so there's nothing to
            # rescue). Esc on the offer aborts the exit so the user can deal with the files first.
            if self.screen is not None:
                self._offer_copy_unstaged_to_base(context="exit")
                if self._exit_aborted:
                    return
            return
        # DELETE: only remove a worktree we can CONFIRM is fully contained in the base, so no
        # unintegrated work is ever discarded. A merge in progress, commits still ahead of the
        # base, or an unresolvable base ref → keep it (and let the next startup surface it).
        try:
            if self.merge_ctx or self.repo.merge_in_progress():
                self._debug(f"keeping worktree '{info.name}' on exit: a merge is in progress")
                return
            has_leftover_files = self.repo.has_changes()
            branch = self.repo.current_branch()
            if is_managed_branch(branch):
                self.base_repo.rev_parse(self._base_branch)
                if self.base_repo.log_range(self._base_branch, branch):
                    self._debug(f"keeping worktree '{info.name}' on exit: '{branch}' still ahead of base")
                    return
        except Exception as error:
            self._debug(f"keeping worktree '{info.name}' on exit: {error!r}")
            return
        # About to delete: rescue the worktree's uncommitted/ignored files first on an
        # interactive exit (the "exit" copy offer warns deletion is permanent and Esc aborts).
        # On a signal teardown there's no UI — keep a worktree that still has leftover files
        # rather than silently losing the user's work.
        if self.screen is not None:
            self._offer_copy_unstaged_to_base(context="exit")
            if self._exit_aborted:
                return
        elif has_leftover_files:
            self._debug(f"keeping worktree '{info.name}' on signal exit: leftover files, no UI to copy them out")
            return
        # Deletion can take a while for a large worktree (full copied environment,
        # node_modules, build output, …), so announce it the moment we start rather than
        # leaving the screen looking frozen. No-ops on a signal teardown (screen is None ⇒
        # _render does nothing).
        self._set_message(f"Deleting worktree '{info.name}'…", seconds=30)
        self._render()
        self._terminate_child()
        try:
            self._worktrees().remove(info.name)
            self._debug(f"removed worktree '{info.name}' on exit (user chose delete)")
        except Exception as error:
            self._debug(f"exit worktree removal failed for '{getattr(info, 'name', '?')}': {error!r}")
        self.worktree = None

    def _exit_child(self) -> None:
        self.running = False
        self._disable_host_terminal_modes()
        # SIGINT to child delegated to the session's BackendProcess (does not
        # close fd -- the run() finally block handles that so it always runs).
        self.active.process.signal_exit()

    def _check_shutdown_request(self) -> None:
        """Honor an external graceful-shutdown request: a ``<repo>/.agitrack/shutdown`` file.

        This is the cross-platform equivalent of the SIGTERM/SIGHUP a closing terminal
        sends — used by the VS Code extension on Windows, where a separate process can't
        deliver a catchable signal (Node's ``process.kill`` hard-kills there). Polled
        cheaply on a ~1 s interval; on finding the file we remove it, finalize the pending
        turn (so a just-finished turn is committed, not stranded), and stop the loop."""
        now = time.monotonic()
        if now - self._last_shutdown_check < 1.0:
            return
        self._last_shutdown_check = now
        try:
            path = self.base_repo.repo / ".agitrack" / "shutdown"
            if not path.exists():
                return
        except (OSError, AttributeError, TypeError):
            return  # no real base repo (e.g. a unit-test runner) ⇒ nothing to honor
        try:
            path.unlink()
        except OSError:
            pass
        self._debug("external shutdown request — finalizing and exiting")
        try:
            self._finalize_pending_work()
        except Exception as error:  # never let a slow/failing finalize block the shutdown
            self._debug(f"shutdown finalize failed: {error!r}")
        self.running = False

    def _handle_exit_signal(self, signum, _frame) -> None:
        # A self-update is mid-flight (pip is uninstalling/reinstalling aGiTrack). Do
        # NOT tear down: unwinding would abort the apply and could leave aGiTrack
        # half-uninstalled. The upgrade runs in its own session (see updater) so this
        # SIGHUP/SIGTERM never reached it; ignore the signal and let the apply +
        # restart finish. (The user can still hard-kill with SIGKILL if truly stuck.)
        if self._update_applying:
            return
        self.running = False
        # Closing the terminal (the VSCode terminal, an SSH/Mosh session, a `kill`)
        # delivers SIGHUP/SIGTERM. Finalize pending work first so a just-completed turn
        # is committed and integrated instead of being stranded — i.e. exit gracefully,
        # not abruptly. Rendering is suppressed (the terminal may already be gone:
        # `_render()` no-ops when `screen` is None) and the whole finalize is guarded so
        # a slow or failing finalize can never stop the process from exiting.
        #
        # Whether work was actually in progress is captured *before* finalizing (which
        # commits it away) so the out-of-terminal prompt only fires when the close
        # interrupted something — a clean close stays silent.
        had_work = self._had_unfinalized_work()
        self.screen = None
        try:
            self._finalize_pending_work()
        except Exception:
            pass
        self._disable_host_terminal_modes()
        self._cleanup_child()
        self._restore_terminal()
        if had_work:
            self._prompt_forced_exit()
        raise SystemExit(128 + int(signum))

    def _describe_exit_finalize(self) -> str | None:
        """A specific, user-facing description of what exit finalize will actually
        do — or ``None`` when everything is already committed and merged (a clean,
        silent exit).

        It deliberately ignores the base repo's own dirty/untracked files and a
        worktree's git-ignored or intentionally-declined files: aGiTrack never
        commits those on exit, so they must not make exit claim there is something
        to "finalize". The signals are aGiTrack's own pending work: a turn still in
        flight, a session with committable or committed-but-unmerged work
        (``_unmerged_worktrees`` already excludes ignored/declined noise), or a
        commit summary still being written. Best-effort; any error reads as
        "nothing to report".
        """
        actions: list[str] = []
        try:
            agent_busy = self._agent_is_active() or any(getattr(s, "agent_in_flight", False) for s in self.sessions)
            summarizing = any(self._session_summary_in_progress(s) for s in self.sessions)
            if self._manual_commits:
                # Manual mode never commits to the branch on exit — an in-flight turn is only
                # RECORDED (latently); nothing is committed or merged. Word it accordingly so the
                # notice never claims aGiTrack is "committing" work the user hasn't asked it to.
                if agent_busy:
                    actions.append("recording the latest agent turn")
                if summarizing:
                    actions.append("saving the commit summary")
                if not actions:
                    return None
                return "Finishing up before exit — " + ", and ".join(actions) + "…"
            if agent_busy:
                actions.append("committing the latest turn")
            unmerged = self._unmerged_worktrees()
            if unmerged:
                actions.append("committing and merging unsaved work in " + ", ".join(label for label, _ in unmerged))
            if summarizing:
                actions.append("saving the commit summary")
        except Exception:
            return None
        if not actions:
            return None
        return "Finishing up before exit — " + ", and ".join(actions) + "…"

    @staticmethod
    def _session_summary_in_progress(session) -> bool:
        """Whether *session* still has a commit summary being computed (a pending
        slot or a live summarizer thread) that exit finalize would wait to apply."""
        if getattr(session, "_summary_pending", None) is not None:
            return True
        thread = getattr(session, "_summary_thread", None)
        return thread is not None and thread.is_alive()

    def _had_unfinalized_work(self) -> bool:
        """Whether aGiTrack still has work to finalize on exit — an agent turn in
        flight, a session with committable/unmerged work, or a commit summary being
        written (see :meth:`_describe_exit_finalize`). Drives the forced-exit prompt
        and the exit confirmation, so a clean close (everything committed and merged)
        exits silently. Ignores the base repo's own dirt and declined/ignored files.
        """
        return self._describe_exit_finalize() is not None

    def _prompt_forced_exit(self) -> None:
        """Prompt the user about an exit forced by the host terminal closing.

        The TUI is gone by the time SIGHUP/SIGTERM arrives, so the user can no
        longer be asked anything inside the terminal. Surface a blocking dialog
        through the OS window server instead (macOS only — a silent no-op on
        SSH/headless/Linux, and time-boxed so it can never hang a system
        restart). "Reopen aGiTrack" arms a best-effort relaunch in a fresh
        window, performed after the management lock is released (see `run`).
        """
        if not host_prompt.can_show_dialog():
            return
        try:
            choice = host_prompt.confirm_forced_exit(
                "Choose Reopen aGiTrack to keep working in a new window, or Quit aGiTrack to close."
            )
        except Exception as error:
            self._debug(f"forced-exit prompt failed: {error!r}")
            return
        if choice == host_prompt.REOPEN:
            self._reopen_after_exit = True

    def _reopen_in_new_window(self) -> None:
        """Relaunch aGiTrack in a fresh window after a forced exit.

        Called from `run`'s teardown once the management lock is released so the
        new instance can take it. Mirrors `restart_agitrack`'s
        `python -m agitrack <argv>` invocation (the original terminal is gone, so
        this opens a new window) and starts it in the base repo root so the last
        session auto-resumes. Best-effort; never raises.
        """
        try:
            from agitrack.proc import agitrack_invocation

            args = " ".join(shlex.quote(arg) for arg in [*agitrack_invocation()[1:], *sys.argv[1:]])
            command = f"{shlex.quote(sys.executable)} {args}".rstrip()
            host_prompt.reopen_in_new_terminal(command, str(self.base_repo.repo))
        except Exception as error:
            self._debug(f"reopen in new window failed: {error!r}")

    def _cleanup_child(self) -> None:
        # Delegate SIGINT -> wait -> SIGTERM escalation to the session's BackendProcess.
        self.active.process.cleanup()

    def _offer_pre_agent_user_commit(self) -> bool:
        """AUTO mode: before a prompt reaches the agent, offer to commit the user's OWN
        uncommitted edits into their own commit, so they aren't bundled into the agent's work.

        Never blocks the prompt. Returns True when the user declined (unstaged / Esc) and edits
        still remain — the caller then forwards the prompt and warns (via
        :meth:`_warn_uncommitted_edits_ride_along`) that those edits will be committed together
        with the agent's changes. Returns False when there's nothing to warn about: the edits were
        committed, unstaging left nothing (so the prompt just goes — no second Enter), or manual
        mode (the tree is intentionally dirty there and the user commits on their own schedule).

        Also returns False in the window between the agent finishing a turn and aGiTrack committing
        it: the uncommitted edits sitting in the tree are the AGENT's, and the turn's own commit is
        about to claim them. Offering to commit (or stage) them as the user's own work there is
        simply wrong — most visibly under --no-worktree, where the agent edits this very tree."""
        if self._manual_commits or self.turn_awaiting_commit:
            return False
        if self.cancelled_work_kept:
            # An interrupted turn's changes the user chose to keep "for your next turn": they
            # are the agent's, and this prompt IS that next turn, so its commit will claim
            # them with a full trace. Offering them here as the user's own would take the same
            # files out of that commit and into an untraced user commit instead.
            return False
        if self._noworktree_auto:
            # A just-completed turn's changes may still sit UNCOMMITTED in the tree, recorded only
            # as a latent commit: no-worktree auto folds them into a real commit on a throttled
            # poll, and that fold is HELD while the turn's background summary is in flight (up to
            # SUMMARY_WAIT_SECONDS — 45s). ``turn_awaiting_commit`` has already dropped by then (the
            # latent commit was recorded, which clears it), so without this the check below would
            # see the AGENT's uncommitted work and offer to commit it as the USER's own — the
            # reported bug where submitting a new prompt while aGiTrack is still summarizing asks
            # about the just-finished turn's files. Land the owed turn commit now (force past the
            # summary wait; a late summary amends into the fold commit, see _folded_head_for) so the
            # tree is clean and there is nothing to offer. A no-op when nothing is pending: it
            # returns early on a clean tree / absent latent tip, so genuine user-only edits (no turn
            # owed) still fall through to the offer below.
            self._auto_fold_latent_pending(force=True)
        if not self._tree_is_dirty(self.repo):
            return False  # cheap, and free inside the submit's read scope (see _tree_is_dirty)
        if not self.actions.has_pre_agent_user_changes():
            return False
        if getattr(self.active, "live_background_task_ids", None):
            # A background task the agent started is still running, and it writes this
            # tree — the same files the user might also edit — so ownership of the
            # uncommitted changes is UNKNOWABLE here (there is no OS-level "which process
            # changed this file"). Don't claim them as the user's: stand down, say why,
            # and let the agent's next commit take them (it claims background work
            # anyway). The explicit Ctrl-G git-commit remains the way to commit one's
            # own edits separately while a task runs.
            self._set_message(
                "Background task still running: the uncommitted files could be your changes "
                "or the task's edits. They will be committed after the next agent turn; use "
                "Ctrl-G git-commit to commit your own edits separately.",
                seconds=8,
            )
            self._render()
            return False
        self._set_message("Uncommitted user changes detected — committing them before the agent runs.")
        self._render()
        committed = self._create_user_commit_popup()
        return not committed and self.actions.has_pre_agent_user_changes()

    def _warn_uncommitted_edits_ride_along(self) -> None:
        """Warn (AUTO mode) that the user's still-uncommitted edits will be captured together with
        the agent's changes when this turn is committed. Shown AFTER the prompt is forwarded, so
        the send itself (which clears transient messages) doesn't wipe the warning."""
        self._set_message(
            "Proceeding with your uncommitted changes — they'll be committed together with "
            "the agent's changes when it finishes.",
            seconds=6,
        )
        self._render()

    def _pre_agent_commit_if_needed(self, prompt_text: str = "") -> bool:
        self._clear_agent_in_flight_if_idle()
        if self._manual_commits:
            # Manual mode: never block a prompt on a pre-flight parse or a user-commit
            # reconcile. The working tree is intentionally dirty (latently-tracked agent work)
            # and the user commits on their own schedule, so there is nothing to check before
            # forwarding — the "checking existing git changes…" step (and its message) doesn't
            # apply. Still capture a just-completed prior turn as a latent commit (non-blocking)
            # and treat a prompt sent while the agent is still working as a follow-up merged
            # into that turn's trace; then forward immediately.
            self._finish_agent_parse_if_ready(quiet=True)
            self._record_user_prompt(prompt_text)
            if self._agent_is_active():
                self._await_followup(prompt_text)
            return True
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
                self._set_message("aGiTrack is checking existing git changes before sending your prompt...", seconds=60)
                self._render()
                return False
            if self._start_agent_parse():
                self._set_message("aGiTrack is checking existing git changes before sending your prompt...", seconds=60)
                self._render()
                return False
        # Commit the user's own uncommitted work before the agent runs, from whichever
        # tree holds it: this session's worktree (the agent's tree) and/or the base repo
        # (your working directory) — both, when both are dirty. The base commit is then
        # merged into the worktree so the agent starts from your edits. (Manual-commit mode
        # returned early above — it never reaches here.)
        warn_uncommitted = self._offer_pre_agent_user_commit()
        self._commit_base_user_edits_if_needed()
        # Trace every submitted prompt as a user message. The agent-active path
        # above already recorded; the held path records in _forward_pending_prompt.
        # This covers the remaining (clean / user-changes-committed) submits so no
        # follow-up is dropped from the commit trace. Dedup in the agent-commit
        # builder collapses this with the backend's own turn.user_prompt.
        self._record_user_prompt(prompt_text)
        if warn_uncommitted:
            self._warn_uncommitted_edits_ride_along()
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
        self._set_message(
            f"aGiTrack: Capturing session summary before compaction (session '{self._session_label()}')..."
        )

    def _service_precompact_summary(self) -> None:
        result = self._precompact_result
        if result is None:
            return
        self._precompact_result = None
        session = result.get("session_name") or "main"
        if "error" in result:
            self._debug(f"pre-compaction summary failed: {result['error']}")
            self._set_message(f"aGiTrack: Pre-compaction summary failed (session '{session}').")
            return
        repo = result.get("repo") or self.repo
        state = result.get("state") or self.state
        try:
            summary = result["summary"]
            state.session_summary = summary
            head_sha = repo.rev_parse("HEAD")
            if head_sha:
                state.session_summary_commit = head_sha
                repo.notes_add(head_sha, summary, namespace="agitrack/session-summary")
            self._set_message(f"aGiTrack: Session summary captured (session '{session}').")
        except Exception as error:
            self._debug(f"applying pre-compaction summary failed: {error!r}")

    @staticmethod
    def _tree_is_dirty(repo: GitRepo) -> bool:
        """Whether ``git status`` reports anything at all in *repo*'s working tree.

        A ONE-DIRECTIONAL pre-check in front of the precise "does the user have changes"
        questions, which cost three git subprocesses each (`ls-files --others`, `diff --quiet`,
        `diff --cached --quiet`). An EMPTY status is conclusive — no tracked edit and no
        untracked file can exist without appearing in it — so the expensive checks are skipped
        outright. A NON-empty status is NOT conclusive (it also lists things those checks
        deliberately exclude, such as aGiTrack's own `.agitrack/`), so it only means "keep
        asking", never "yes". That asymmetry is what makes this safe to put in front of them.

        Free on the submit path: `_pre_agent_commit_if_needed` has already read this status,
        and the surrounding `git_read_cache()` scope serves it from that read. On a clean tree —
        the common case when submitting a prompt — this turns six git spawns into none.
        """
        try:
            return bool(repo.status_short().strip())
        except Exception:
            return True  # unreadable status: fall through to the precise checks

    def _base_user_edits_pending(self) -> bool:
        # The user's own edits land in the BASE repo's working tree (the session
        # worktree is the agent's sandbox), so the worktree-side pre-agent check
        # never sees them. Any uncommitted change there — tracked edit OR new
        # (non-ignored) file — counts, so it can be committed and synced into the
        # worktree before the agent runs. Re-prompt nagging is avoided by the
        # fingerprint gate in `_commit_base_user_edits_if_needed` (decline is
        # remembered until the base tree changes again), NOT by permanently
        # excluding a once-declined file — which used to strand it (the user could
        # never get it committed/synced through aGiTrack).
        base = self.base_repo
        if base is None or self.worktree is None:
            return False
        if not self._tree_is_dirty(base):
            return False  # nothing uncommitted anywhere: skip the three precise checks
        try:
            # Any uncommitted work counts: a tracked-file edit, OR a new untracked
            # (added-but-unstaged) file — `untracked_files()` lists those (and already
            # excludes aGiTrack's own `.agitrack/`). The user is offered to stage and
            # commit it; a "don't stage" answer only suppresses re-prompting for the
            # same tree state via the fingerprint gate, never permanently, so a file is
            # always offer-able.
            return base.has_tracked_changes() or bool(base.untracked_files())
        except Exception as error:
            self._debug(f"base user-edit check failed: {error!r}")
            return False

    def _commit_base_user_edits_if_needed(self) -> None:
        # Offer to commit the user's uncommitted base-repo edits before the
        # prompt reaches the agent, then sync the session worktree onto the new
        # base commit so the agent actually sees those edits. Declining skips
        # re-prompting until the base working tree changes again.
        if self._manual_commits:
            return  # user-triggered commits only: never auto-prompt in manual mode
        if not self._base_user_edits_pending():
            self._base_edits_declined_status = None
            return
        status = self._base_edits_fingerprint()
        if status is not None and status == self._base_edits_declined_status:
            return  # already declined for this exact state; don't nag every prompt
        self._set_message(
            "Uncommitted changes in the base repo (your working directory) — committing them onto "
            "the base branch and syncing them into this session's worktree so the agent sees them."
        )
        self._render()
        if self._create_user_commit_popup(repo=self.base_repo, state=self._user_state(), include_declined=True):
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
        with git_read_cache():  # the held prompt's release repeats the submit path's reads
            self._resume_pending_prompt_now()

    def _resume_pending_prompt_now(self) -> None:
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is None:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self._set_message("aGiTrack is checking existing git changes before sending your prompt...", seconds=60)
                return
            # The parse already ran and deferred (its result is consumed): the
            # agent's latest turn is still in progress, so the uncommitted changes
            # belong to the in-flight agent — there is nothing of the user's to
            # commit before this prompt. Forward it now so the backend queues the
            # follow-up instead of holding it (and the "checking" message) forever.
            self._forward_pending_prompt()
            return
        # Offer to commit the user's own pre-existing edits first (AUTO mode only) — but never
        # drop the prompt over it: whether they commit, unstage, or decline, the turn still goes
        # through (with a warning when uncommitted edits remain). Manual mode never reaches here
        # via the guard: its tree is intentionally dirty and the user commits when they choose.
        warn_uncommitted = self._offer_pre_agent_user_commit() if finished is False else False
        self._commit_base_user_edits_if_needed()
        self._forward_pending_prompt()
        if warn_uncommitted:
            self._warn_uncommitted_edits_ride_along()

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
            # Flush the previous turn's deferred integration before starting this
            # one so it merges now rather than riding along with the new turn.
            self._integrate_committed_turn_before_new_turn()
            self._ensure_turn_branch()  # a new prompt starts a turn on its own branch
        self._begin_agent_turn()
        self._clear_message()
        if prompt_text:
            # Drop the previous turn's "created & merged" status line so it does
            # not linger into the new turn (clearing the registry too, or the
            # notice service tick would just repaint it).
            self._set_session_notice(self._session_label(), None)
        self.active.process.write(b"".join(forwarded))

    def _prune_declined_untracked(self, repo: GitRepo | None = None, state: AgitrackState | None = None) -> None:
        repo = repo or self.repo
        state = state or self.state
        state.keep_declined(repo.untracked_entries())

    def _user_state(self) -> AgitrackState:
        # The user's working tree is the base repo (the session worktree is the agent's sandbox
        # and only holds tracked files). Its intentionally-unstaged list and user commits live
        # there — and callers WRITE through this (`set_shared_origin`), which is why it goes
        # through `_root_state`: in worktree mode that is still a fresh instance reading the
        # latest on-disk bytes, and without a worktree it is the session's own object, so those
        # writes are no longer thrown away by the session state's next save.
        return self._root_state()

    def _reload_user_declined(self) -> None:
        # Re-read the base repo's intentionally-unstaged list (after a command that
        # may have changed it) and seed the status-line cache.
        self._user_declined = self._user_state().declined_untracked()

    def _prune_user_declined(self) -> None:
        # Drop cached entries no longer untracked in the base tree (committed,
        # staged, or deleted out-of-band). Cheap enough for the base-poll cadence.
        untracked = set(self.base_repo.untracked_entries())
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

    def _begin_agent_turn(self) -> None:
        """A turn has just been handed to the agent.

        Raises ``turn_awaiting_commit`` (cleared only once a parse result reconciles the turn) and
        snapshots the untracked files that already exist. Anything untracked that appears AFTER this
        point was produced by the turn, so its commit can stage it without asking — while the user's
        own pre-existing untracked files are left untouched."""
        self.agent_in_flight = True
        self.turn_awaiting_commit = True
        try:
            # Files kept from an interrupted turn are the AGENT's work awaiting this very
            # commit, so they must not count as "already there" — that exclusion is what
            # kept them out of the commit that was promised to include them.
            self.untracked_before_turn = frozenset(self.repo.untracked_entries()) - self.kept_cancelled_paths
            # Files that predate EVERY turn folded so far are the USER's, not any turn's output:
            # a file the agent writes in turn 1 is absent from turn 1's snapshot, so intersecting
            # the snapshots singles out exactly the ones no turn created. The no-worktree fold
            # can then leave them alone even when the user answered nothing at all (Esc on the
            # untracked prompt neither stages nor declines, and they were being swept into an
            # agent commit whose message never mentioned them).
            previous = self.user_untracked_since_fold
            self.user_untracked_since_fold = (
                self.untracked_before_turn if previous is None else (previous & self.untracked_before_turn)
            )
        except Exception as error:
            # Unknown baseline: fall back to the worktree rule (stage the turn's files, minus any
            # the user explicitly declined) rather than blocking the commit on a prompt.
            self._debug(f"untracked snapshot failed: {error!r}")
            self.untracked_before_turn = frozenset()
            self.user_untracked_since_fold = frozenset()

    # How often the commit-gate trace may log while nothing changes. The gate is evaluated on
    # every pipeline pass; without a throttle a stuck runner would write thousands of identical
    # lines. Any CHANGE to the gate state logs immediately regardless of this.
    COMMIT_GATE_TRACE_SECONDS = 30.0

    def _trace_commit_gate(self, now: float, worktree_settled: bool) -> None:
        """Debug-gated snapshot of every input that decides whether a turn gets committed.

        When aGiTrack "just stops committing", the cause is always one of these flags latching,
        and none of them is observable from outside the process — the reported no-worktree hangs
        each cost a full reproduction to identify. Logging them (DEBUG_PROXY=1) turns that into
        one line. Emitted on every state change, and at most once per COMMIT_GATE_TRACE_SECONDS
        otherwise, so a healthy run costs ~2 lines a minute."""
        if not self.debug_proxy:
            return
        try:
            transcript_mtime = self._active_transcript_mtime()
            state = (
                f"settled={worktree_settled} idle={self._backend_idle_for(self.CHILD_IDLE_SECONDS)} "
                f"status_check_pending={self.status_check_pending} parse_pending={self.parse_pending} "
                f"parse_alive={bool(self.agent_parse_thread and self.agent_parse_thread.is_alive())} "
                f"parse_active={getattr(self.active, 'agent_parse_active', None)} "
                f"parse_result={getattr(self.active, 'agent_parse_result', None) is not None} "
                f"in_flight={self.agent_in_flight} awaiting_commit={self.turn_awaiting_commit} "
                f"last_status={self.last_status.strip()[:40]!r} "
                f"last_attempt={self.last_parse_attempt_status.strip()[:40]!r} "
                f"transcript_quiet={'n/a' if transcript_mtime is None else round(time.time() - transcript_mtime)} "
                f"pty_quiet={round(now - self.last_child_output, 1)}"
            )
        except Exception as error:  # tracing must never break the pipeline
            self._debug(f"commit gate trace failed: {error!r}")
            return
        changed = state != getattr(self, "_commit_gate_last", None)
        if not changed and now - getattr(self, "_commit_gate_logged_at", 0.0) < self.COMMIT_GATE_TRACE_SECONDS:
            return
        self._commit_gate_last = state
        self._commit_gate_logged_at = now
        self._debug(f"commit gate {state}")

    def _active_transcript_mtime(self) -> float | None:
        # The mtime of the active session's live transcript, as a cheap "is the backend still
        # working" signal (see `_backend_idle_for`). The transcript PATH is resolved and cached,
        # so the per-tick cost is a single `stat`, not a re-scan of every project dir.
        session_id = getattr(self.state, "backend_session_id", None) if self.state else None
        if not session_id:
            return None
        # A backend with no stat-able transcript file can still expose the signal directly
        # (OpenCode keeps its sessions in a SQLite database, so it answers from a bounded
        # read-only query instead of a path). Ask for that first: without it this method
        # returns None on such a backend and `_backend_idle_for` degrades to the PTY alone —
        # which is exactly the failure this whole mechanism exists to prevent, in both
        # directions (never commits behind an idle heartbeat; commits mid-turn behind a quiet
        # sub-agent). Cheap enough to call per tick, and any failure just yields None.
        direct = getattr(self.backend, "session_activity_mtime", None)
        if direct is not None:
            try:
                return direct(session_id)
            except Exception as error:
                self._debug(f"transcript activity lookup failed: {error!r}")
                return None
        cached_id, cached_path = self._transcript_path_cache
        # A MISS must not be cached forever. The transcript often does not exist yet when a
        # session starts (the backend creates it on the first turn), and the miss used to be
        # stored keyed by session id — so `cached_id == session_id` on every later call and the
        # lookup was never retried. That pinned this to None for the whole run, which made
        # `_backend_idle_for` fall back to the PTY alone; with a TUI heartbeat the PTY is never
        # quiet, so the turn end was never detected and the session committed NOTHING, ever.
        # Retry a miss on a throttle: one project-dir scan per interval, not per tick.
        if cached_id != session_id or cached_path is None:
            now = time.monotonic()
            if cached_id != session_id or now - self._transcript_lookup_at >= self.TRANSCRIPT_LOOKUP_RETRY_SECONDS:
                self._transcript_lookup_at = now
                resolver = getattr(self.backend, "session_transcript_path", None)
                cached_path = None
                if resolver is not None:
                    try:
                        cached_path = resolver(session_id)
                    except Exception as error:
                        self._debug(f"transcript path lookup failed: {error!r}")
                self._transcript_path_cache = (session_id, cached_path)
        if cached_path is None:
            return None
        try:
            return cached_path.stat().st_mtime
        except OSError:
            self._transcript_path_cache = (None, None)  # vanished — re-resolve next time
            return None

    def _conversation_activity_mtime(self, session_id: str | None) -> float | None:
        """Last-activity mtime of ANY conversation, not just the tracked one. Same two sources as
        :meth:`_active_transcript_mtime` (a direct query, else the transcript file), without its
        path cache — this is asked about a conversation aGiTrack is not tracking yet, so caching
        by id would fight the active session's entry."""
        if not session_id:
            return None
        direct = getattr(self.backend, "session_activity_mtime", None)
        if direct is not None:
            try:
                return direct(session_id)
            except Exception as error:
                self._debug(f"conversation activity lookup failed: {error!r}")
                return None
        resolver = getattr(self.backend, "session_transcript_path", None)
        if resolver is None:
            return None
        try:
            path = resolver(session_id)
            return path.stat().st_mtime if path is not None else None
        except Exception as error:  # a missing/unreadable transcript is simply "unknown"
            self._debug(f"conversation transcript lookup failed for {session_id}: {error!r}")
            return None

    def _conversation_idle(self, session_id: str, seconds: float) -> bool:
        """Whether the backend has finished working IN ``session_id`` (as opposed to in the
        conversation aGiTrack currently tracks).

        The distinction only matters after a native switch, and there it is the whole ballgame.
        `/clear` freezes the tracked conversation's transcript, so every idleness signal keyed to
        it reads "the agent is done" the instant the user switches away — while the agent is in
        fact working on the FIRST TURN of the new conversation. Acting on that read committed a
        turn that had not happened yet ("committing finished turn with no final text response",
        seen live) and opened a modal over a running agent; answering it relocated the session
        out from under a turn still writing files, stranding them uncommitted in the worktree the
        user had just left. So the switch waits for the conversation it is switching TO."""
        return self._backend_idle_for(seconds, transcript_mtime=self._conversation_activity_mtime(session_id))

    def _backend_idle_for(self, seconds: float, *, transcript_mtime: "float | None | object" = _UNSET) -> bool:
        """Whether the backend has produced NOTHING for ``seconds`` — judged by the PTY output
        and the session transcript together.

        A sub-agent runs inside the main agent and may print nothing to the terminal while it
        works, but Claude keeps appending its sidechain messages to the transcript, so the file
        mtime advances. Without the transcript check aGiTrack would read the quiet terminal as
        "turn finished" and try to commit (and, in no-worktree mode, offer to commit the user's
        changes) while the turn is in fact still running.

        The converse also has to be handled, or nothing ever commits. A backend TUI can emit a
        low-rate idle HEARTBEAT with nothing running, and every byte of it refreshes
        ``last_child_output``. Measured in a real Linux Claude session: a steady 8 bytes/second,
        every second, never a gap — invisible on screen (a few escape bytes), but more than
        enough to keep the PTY from ever being quiet for CHILD_IDLE_SECONDS. That pinned this
        to False forever: the turn-end path (`_maybe_agent_commit`) never ran, and turns were
        only ever committed by the pre-agent flow when the user typed their NEXT prompt, leaving
        the worktree dirty for as long as the user stayed away. It reproduced on Linux and not
        on macOS, so the heartbeat's exact rate is platform- and version-specific — which is
        precisely why the PTY cannot be the sole authority here.
        So the transcript — which advances only on real backend work — gets the final say: once
        it has been quiet for ``TRANSCRIPT_IDLE_FACTOR`` x the window, the backend is idle no
        matter how loud the terminal is. The factor keeps the PTY authoritative for the common
        case (both quiet ⇒ idle at ``seconds``) and only overrules a chattering one, so a real
        sub-agent still gets the full transcript-freshness guard above.

        ``transcript_mtime`` overrides which transcript is consulted. The default (the ACTIVE
        session's) is wrong in exactly one place: right after a native switch, where the tracked
        conversation is the one the user just left — frozen, therefore "quiet", while the agent
        is busy in the conversation they switched TO. See ``_conversation_idle``."""
        mtime = self._active_transcript_mtime() if transcript_mtime is _UNSET else transcript_mtime
        transcript_quiet_for = time.time() - mtime if isinstance(mtime, (int, float)) else None
        if transcript_quiet_for is not None and transcript_quiet_for < seconds:
            return False  # real backend work — possibly a sub-agent printing nothing to the PTY
        if time.monotonic() - self.last_child_output >= seconds:
            return True  # PTY quiet too (or no transcript to consult): the original fast path
        # PTY still chattering. Only a transcript can overrule it — with no transcript to read
        # there is no second opinion, so keep the old conservative answer.
        return transcript_quiet_for is not None and transcript_quiet_for >= seconds * self.TRANSCRIPT_IDLE_FACTOR

    def _clear_agent_in_flight_if_idle(self) -> None:
        if self.agent_in_flight and self._backend_idle_for(self.CHILD_IDLE_SECONDS):
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
        # Identify the conversation actually being driven here: the most recently written session
        # that has been WRITTEN TO SINCE LAUNCH — either it did not exist before (aGiTrack's own
        # session; a `/clear` or `/new`) or its transcript has moved on since the snapshot (a
        # `/resume` or session switch back onto an older conversation).
        #
        # "Since launch" is the whole safety property. A directory can hold conversations that
        # have nothing to do with this run, and those are exactly the ones nobody has touched —
        # so they can never be adopted, however recent they look. Returns None when no snapshot
        # exists (a restored session object) or when nothing has moved, and the caller then keeps
        # its pinned id. `list_sessions` supplies the other half of the filter: it already drops
        # programmatic (SDK) transcripts, so an agent farming work out to `claude -p` workers
        # cannot have one of them adopted as the tracked conversation.
        snapshot = self._pre_spawn_sessions
        if snapshot is None:
            return None
        refs = self.backend.list_sessions(self.repo.repo)
        # `>` not `>=`: an untouched session must compare equal to its snapshot and drop out.
        candidates = [ref for ref in refs if ref.updated > snapshot.get(ref.id, float("-inf"))]
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
            # The prompt the user just pressed Enter on is HELD until this parse finishes;
            # wake the reactor the moment it does instead of letting it wait out the poll.
            on_finish=self._wake_main_loop,
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
            # Only the live interactive path (integrate=True) prompts about a
            # cancelled turn's leftover changes; the sync/exit finalize paths leave
            # them as-is rather than raising a modal during teardown.
            on_cancelled_fn=self._handle_cancelled_turn if integrate else None,
            note_in_flight_fn=self._note_in_flight,
        )
        self._awaited_followups = new_awaited
        if committed is not None:
            # A parse result was consumed: the turn is now reconciled (committed, or concluded with
            # nothing to commit). Any dirt left in the tree from here on is the user's own.
            self.turn_awaiting_commit = False
        if committed:
            self.agent_in_flight = False
            self.last_status = ""
            if not quiet:
                # Repaint now so the "summarizing…" notice (set by the commit) is
                # visible before the integrate below runs git merge/fast-forward on
                # the main thread; the "created & merged" notice replaces it once
                # the turn actually lands in the base.
                self._render()
            if integrate:
                # Interactive integration only for the active session. The
                # sync/background commit path passes integrate=False.
                self._integrate_or_defer_after_commit()
        return committed

    def _integrate_or_defer_after_commit(self) -> None:
        """What happens to a turn the moment its commit lands: merge it, or say where it is.

        Under ``--delay-merge`` nothing is merged, so there is nothing to wait for a summary
        for — only the user to be told where their held-back work is. Routing the notice
        through the summary-gated integration below meant it never appeared at all in the
        DEFAULT configuration: a summary is always in flight immediately after a commit, and
        the idle catch-up path returns early under --delay-merge by design. Proven live — with
        summaries off the notice paints, with them on the string never reaches the terminal.

        Otherwise: while a background summary is pending, integration is deferred so the
        summary can be amended in first; the idle-integration path picks the commit up once it
        lands (or the wait deadline passes).
        """
        if self._delay_merge:
            self._notify_merge_deferred()
            return
        if not self._summary_blocks_integration(time.monotonic()):
            self._integrate_session_turn()

    def _remember_kept_cancelled_work(self) -> None:
        """Record that an interrupted turn's changes were KEPT for the next turn's commit.

        "Keep them (commit with your next turn)" is a promise, and three other paths used to
        break it. The parse is consumed either way, so ``turn_awaiting_commit`` drops and the
        agent's leftover files become "the user's own dirt": the copy-back watcher offered to
        copy them to the base repo as "intentionally unstaged or git-ignored", the next prompt
        offered them as a plain USER commit (no trace, no attribution), and under --no-worktree
        the next turn's commit skipped them because they already existed when it began. This
        flag keeps the work owned by the agent until a commit actually claims it."""
        self.cancelled_work_kept = True
        try:
            self.kept_cancelled_paths = frozenset(self.repo.untracked_entries())
        except Exception as error:
            # Worst case the paths are unknown: ownership (the flag) still holds, and the
            # worktree commit path stages every non-declined untracked file anyway.
            self._debug(f"kept-cancelled untracked snapshot failed: {error!r}")
            self.kept_cancelled_paths = frozenset()

    def _forget_kept_cancelled_work(self) -> None:
        """A commit claimed the kept work (or the user discarded it): ownership ends here."""
        self.cancelled_work_kept = False
        self.kept_cancelled_paths = frozenset()

    def _handle_cancelled_turn(self, turns) -> bool:
        """Offer commit-or-discard for a user-cancelled turn's leftover changes.

        Called by the commit engine when the user interrupted the agent (Esc) and
        it produced no committable response — so the normal auto-commit won't fire
        and the agent's partial edits would otherwise sit uncommitted in the
        worktree. If there are such changes, ask what to do with them.

        Returns True once the changes were committed or discarded (so the engine
        advances the parse watermark past the cancelled turn); False to leave the
        turn untouched — either nothing to act on, or the user chose to keep the
        changes and decide later (the next real turn's commit will fold them in).
        """
        last = turns[-1] if turns else None
        if last is None:
            return False
        turn_id = last.assistant_message_id or last.user_message_id or ""
        if turn_id and turn_id in self._cancel_prompted:
            return False  # already offered this turn — don't nag on every parse
        try:
            if not self.repo.has_changes():
                return False  # the cancellation left nothing behind to act on
        except Exception as error:
            self._debug(f"cancelled-turn change check failed: {error!r}")
            return False
        if turn_id:
            self._cancel_prompted.add(turn_id)
        choice = self._select_popup(
            "The agent was interrupted before finishing, leaving uncommitted changes.",
            ["Keep them (commit with your next turn)", "Commit the changes now", "Discard the changes"],
        )
        if choice is None or choice.startswith("Keep"):
            self._remember_kept_cancelled_work()
            self._set_message("Keeping the agent's changes — they'll be committed with your next turn.")
            self._render()
            return False
        if choice.startswith("Commit"):
            committed = self._create_agent_commit_from_turns_popup(
                turns=turns,
                backend=getattr(self.backend, "name", None) or self.state.backend,
                backend_session_id=self.state.backend_session_id,
                model=self.state.model,
                quiet=False,
                prompt_untracked=self.worktree is None,
            )
            if committed:
                self._forget_kept_cancelled_work()
            self._set_message(
                "Committed the agent's interrupted changes." if committed else "Nothing staged to commit."
            )
            self._render()
            return committed
        # Discard — destructive, so confirm before throwing the work away.
        confirm = self._select_popup(
            "Discard ALL uncommitted changes from the interrupted turn? This cannot be undone.",
            ["No, keep them", "Yes, discard"],
        )
        if confirm == "Yes, discard":
            try:
                self.repo.discard_all_changes()
            except Exception as error:
                self._debug(f"discard cancelled-turn changes failed: {error!r}")
                self._set_message("Could not discard the changes.")
                self._render()
                return False
            self._forget_kept_cancelled_work()
            self._set_message("Discarded the agent's interrupted changes.")
            self._render()
            return True
        self._set_message("Keeping the agent's changes.")
        self._render()
        return False

    def _commit_available_agent_turns(self, *, quiet: bool) -> bool:
        if self._finish_agent_parse_if_ready(quiet=quiet) is True:
            return True
        self._start_agent_parse()
        return False

    def _maybe_agent_commit(self) -> None:
        now = time.monotonic()
        if self.file_change_event.is_set():
            self.file_change_event.clear()
            self._last_change_at = now  # a real worktree write just landed
            self.status_check_pending = True
        elif Observer is None:
            if now - self.last_poll < self.POLL_SECONDS:
                return
            self.last_poll = now
            self.status_check_pending = True
        self._clear_agent_in_flight_if_idle()
        # Defer the `git status` subprocess out of the active edit/type window. A
        # commit can only happen once the worktree AND the backend have been quiet
        # for the stable/idle windows, so reading status on every keystroke-adjacent
        # file event buys nothing — and doing it on the main thread is exactly what
        # made typing lag right after an edit. Hold off until things settle; then a
        # single read serves the commit. (With no file watcher, `_last_change_at` is
        # never advanced, so this naturally reduces to the old poll-then-read once
        # the backend goes idle.)
        worktree_settled = now - self._last_change_at >= self.FILE_STABLE_SECONDS
        self._trace_commit_gate(now, worktree_settled)
        # Transcript-aware: a silent sub-agent keeps the turn running even with a quiet terminal,
        # so aGiTrack must not treat this as the turn ending (would commit / offer mid-turn).
        backend_idle = self._backend_idle_for(self.CHILD_IDLE_SECONDS)
        if self.status_check_pending and worktree_settled and backend_idle:
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
        elif self.status_check_pending:
            # Worktree or backend still active — revisit next tick without touching git.
            if self.verbose:
                self._render_status(f"git changes found; waiting for {self.backend.name} to become idle")
            return
        else:
            status = self.last_status
        if not status.strip():
            if self.verbose:
                self._render_status("no git changes")
            # The agent went idle and the tree is settled and clean: this turn left nothing for
            # aGiTrack to commit, so it is no longer awaiting one. (Safety valve — without it a
            # turn that changed no files would suppress the pre-agent user-commit offer forever.)
            self.turn_awaiting_commit = False
            self._integrate_agent_made_commits_if_idle(now)
            # A turn can leave only git-ignored files (nothing staged, clean tree),
            # so there's no commit to hang the copy offer on — offer here too.
            self._maybe_offer_copy_when_idle(now)
            return
        if self.agent_parse_thread and self.agent_parse_thread.is_alive():
            if self.verbose:
                self._render_status(f"git changes found; parsing {self.backend.name} session")
            return
        if now - self._last_change_at < self.FILE_STABLE_SECONDS or not self._backend_idle_for(self.CHILD_IDLE_SECONDS):
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
                # The turn concluded with nothing to commit (e.g. the only changed
                # files were untracked ones the user declined to stage). They still
                # live only in the worktree, so offer to copy them across — the same
                # offer the committed path makes below.
                self._maybe_offer_copy_when_idle(now)
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
        # Do NOT announce the commit here. A worktree session announces it only
        # once it merges into the base (via _announce_agent_commit at
        # integration); meanwhile the "summarizing…" notice must stand. A
        # no-worktree session never integrates, so it announces here — but only
        # after any summary has landed (so summarizing shows first), which the
        # summary service handles; if summarization is off there is no summary to
        # wait for, so announce immediately.
        if self.verbose:
            self._render_status("committed agent turn")
        elif self.worktree is None and self._summary_pending is None:
            self._announce_agent_commit()
        # The turn's committed changes integrate into the base branch, but any files
        # the agent left UNCOMMITTED in the worktree (e.g. untracked files the user
        # declined to stage) never reach the base working directory. Offer to copy
        # those over so they aren't stranded in the session's worktree. Hand the offer to the
        # main thread (it presents the popup) so this worker pass returns and the next
        # commit/summary/merge keeps flowing while the user decides.
        self._request_copy_offer()

    def _maybe_offer_copy_when_idle(self, now: float) -> None:
        """Idle-gated entry to the copy offer, used by the turn-polling loop so the
        offer fires after a turn that produced **no** commit too (e.g. the agent only
        touched git-ignored files, so nothing was staged) — not just committed turns.
        Skipped while a turn is running or a merge is in progress, so it never
        interrupts the agent; the offer itself is fingerprint-deduped, so a file is
        offered only once per change."""
        if self.worktree is None or self.merge_ctx:
            return
        if self._agent_is_active() or now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return
        self._request_copy_offer()

    def _offer_user_commit_for_worktree_edits(self) -> None:
        """Offer to COMMIT the user's own uncommitted edits in this worktree (tracked
        changes, or new files they haven't declined) — those belong in git, not just
        copied to the base directory. Run right before the copy offer so that when a user
        edit AND copy-able leftovers both exist, BOTH prompts appear: a commit prompt for
        the edits here, and the copy prompt (for declined/ignored files) afterwards.
        A no-op in the normal case, where the turn's auto-commit already committed the
        user's tracked edits and only declined/ignored files remain."""
        actions = getattr(self, "actions", None)
        check = getattr(actions, "has_pre_agent_user_changes", None)
        if check is None:
            return
        try:
            if check():
                self._set_message("Uncommitted changes in this session's worktree — commit them?")
                self._render()
                self._create_user_commit_popup()
        except Exception as error:
            self._debug(f"pre-copy user-commit offer failed: {error!r}")

    def _offer_copy_unstaged_to_base(self, *, context: str = "turn") -> None:
        """Offer to copy the worktree's would-be-stranded files into the base repo dir.

        Only relevant for an isolated worktree session: changes committed this turn
        integrate into the base branch, but files the agent left uncommitted (declined
        untracked files) or that are git-ignored live only in the worktree and the user —
        working in the base directory — never sees them. The user's OWN uncommitted edits
        are offered for a git commit first (`_offer_user_commit_for_worktree_edits`), so a
        commit prompt and the copy prompt can both appear when both apply.

        ``context``:
          - ``"turn"``   — after an idle turn in the ACTIVE session (default). Background
            sessions are never interrupted; their files are offered on switch/exit instead.
          - ``"switch"`` — when the user switches TO this session.
          - ``"exit"``   — last offer before the worktree (and these files) are deleted: the
            prompt warns that discarding them is permanent, and pressing Esc aborts the exit
            so the user can deal with the files themselves.

        A file already accepted/left in place isn't re-offered until its content changes
        (fingerprint). Declining mutes the whole current SET of paths: aGiTrack won't ask
        again while only those files keep changing — only a genuinely NEW path re-opens the
        set (ask about all again), in EVERY context including exit. The mute clears on
        session switch and aGiTrack restart.

        Runs collect (the worktree read) then present (the popup + copy) inline — used for the
        ``switch``/``exit`` contexts, which are already on the main thread. The per-turn offer
        instead goes through ``_request_copy_offer`` so the git worker never blocks on it."""
        collected = self._collect_copy_candidates(context=context)
        if collected is not None:
            self._present_copy_offer(collected, context=context)

    def _request_copy_offer(self, *, context: str = "turn") -> None:
        """Git-worker side of the turn copy offer: do the worktree read HERE (off the main
        thread) and, if there are fresh files, stash them for the main thread to present. The
        worker never blocks on the popup, so commit/summary/merge keep flowing while the user
        decides — only a merge CONFLICT (its own popup) holds integration up."""
        if self._pending_copy_offer is not None:
            return  # a previously collected batch is still waiting to be presented
        if self.cancelled_work_kept:
            # Those files are an interrupted turn's work, kept for the next turn's commit —
            # not leftovers "intentionally unstaged or git-ignored", which is what this offer
            # would call them seconds after aGiTrack promised to commit them. The exit offer
            # is deliberately NOT gated: nothing may vanish with the worktree unasked.
            return
        collected = self._collect_copy_candidates(context=context)
        if collected is not None:
            self._pending_copy_offer = (context, collected)
            self._wake_main_loop()  # present it on the next reactor tick

    def _service_deferred_switch_offer(self) -> None:
        """Present a switch's user-commit / copy offer that _switch_active deferred — but only
        AFTER the switched-to session's latest agent turn is reconciled, so the agent's own
        changes aren't offered as the user's. The CALLER holds ``_pipeline_lock`` (the timers-
        phase background cluster), so the parse/commit here can't race the git worker; the
        background parse keeps the main thread responsive (we re-check each tick, never block).

        Sequence: commit any finished parse; if uncommitted changes remain and we haven't yet
        checked the transcript for them, kick ONE background parse and wait; once reconciled
        (committed, or nothing more to attribute), make the offer."""
        target = self._pending_switch_copy_offer
        if target is None:
            return
        if target is not self.active:
            self._pending_switch_copy_offer = None  # switched away before it fired — drop it
            self._switch_offer_parse_started = False
            return
        if self._agent_is_active():
            return  # a turn is running; the post-turn pipeline reconciles it — wait
        self._finish_agent_parse_if_ready(quiet=True)  # commit a just-finished agent turn
        if self.agent_parse_thread is not None and self.agent_parse_thread.is_alive():
            return  # a parse is running; offer next tick once it lands
        if not self._switch_offer_parse_started and self.repo.status_short().strip():
            # Uncommitted changes remain and we haven't checked the transcript for them yet: run
            # ONE parse to attribute/commit any agent turn, then offer on a later tick. Bounded to
            # a single parse so non-committable leftovers (declined/ignored files) still get offered.
            if self._start_agent_parse():
                self._switch_offer_parse_started = True
                return
        self._pending_switch_copy_offer = None
        self._switch_offer_parse_started = False
        self._offer_copy_unstaged_to_base(context="switch")

    def _present_pending_copy_offer(self) -> None:
        """Main thread: present a copy offer the git worker collected for this turn, if any.
        Deliberately NOT under `_pipeline_lock` — the worker must keep committing/merging
        while this popup is open. A merge that needs the user (a conflict) queues its own
        popup via the modal mailbox, so the two are answered one at a time."""
        pending = self._pending_copy_offer
        if pending is None:
            return
        self._pending_copy_offer = None
        context, collected = pending
        if self.worktree is None or self.base_repo is None:
            return  # the worktree went away before we could ask (a switch/exit) — drop it
        self._present_copy_offer(collected, context=context)

    def _collect_copy_candidates(
        self, *, context: str
    ) -> tuple[list[str], list[tuple[str, tuple[int, int]]], set[str]] | None:
        """The worktree read + dedup behind the copy offer (git/file I/O, no popup). Returns
        the fresh files to offer as ``(rels, fresh, current_set)``, or None when there is
        nothing to ask about. Safe to run on the git worker."""
        if self.worktree is None or self.base_repo is None:
            return None
        base_dir = self.base_repo.repo
        wt_dir = self.repo.repo
        if base_dir == wt_dir:
            return None
        if context != "turn":
            # switch/exit run on the main thread: offer to COMMIT the user's own edits first.
            # The turn path runs on the git worker and must NOT raise a (blocking) popup or
            # commit from there; those edits are still offered at the next switch/exit.
            self._offer_user_commit_for_worktree_edits()
            if context == "exit" and self._exit_aborted:
                # Esc on the user-commit box aborted the exit — don't also open the copy box.
                return None
        candidates = self._uncommitted_worktree_files()
        if not candidates:
            return None
        current = set(candidates)
        # Remembered copy-back decisions are kept only while the candidate SET is stable. Any
        # change to the set (a path added OR removed) resets them so the user confirms again:
        # the "always copy" / "always overwrite" flags, the decline mute, and the per-file
        # fingerprint memory all clear, so "all files" means all again. (They also reset on
        # restart, being in-memory only.)
        if current != self._copy_decision_set:
            self._copy_decision_set = set(current)
            self._copy_always = False
            self._overwrite_always = False
            self._copy_declined.clear()
            self._copy_prompted.clear()
        # Files copied INTO this worktree FROM the base must not be offered to be copied BACK —
        # that would loop base → worktree → base. Two complementary signals: the per-file
        # fingerprint registry, and the env-copy WATERMARK — anything not modified since the
        # initial copy finished came from that copy (this also covers a copied DIRECTORY, which
        # the offer reports collapsed as ``dir/`` and so the per-file registry can't match). The
        # agent's own later edits have a newer mtime / different fingerprint and still flow.
        from_base = self._env_from_base.get(str(wt_dir), {})
        watermark = self._env_copy_watermark.get(str(wt_dir), 0.0)
        fresh: list[tuple[str, tuple[int, int]]] = []
        for rel in candidates:
            fingerprint = self._file_fingerprint(wt_dir / rel)
            if fingerprint is None:
                continue
            if from_base.get(rel) == fingerprint:
                continue  # came from the base unchanged — copying it back would be a no-op loop
            if watermark and self._entry_mtime(wt_dir / rel) <= watermark:
                continue  # not modified since the initial base→worktree copy — don't re-offer it
            if rel in self._copy_declined:
                continue  # muted by a prior decline; not re-asked even on exit (no new file)
            if self._copy_prompted.get(rel) == fingerprint:
                continue  # already offered/copied at this exact content
            if not self._differs_from_base(wt_dir / rel, base_dir / rel):
                continue  # identical to the base copy (incl. after a prior copy) — nothing to do
            fresh.append((rel, fingerprint))
        if not fresh:
            return None
        for rel, fingerprint in fresh:
            self._copy_prompted[rel] = fingerprint
        rels = [rel for rel, _ in fresh]
        return (rels, fresh, current)

    def _present_copy_offer(
        self,
        collected: tuple[list[str], list[tuple[str, tuple[int, int]]], set[str]],
        *,
        context: str,
    ) -> None:
        """Main-thread side: show the copy popup(s) and copy the files. Runs WITHOUT the
        pipeline lock so the git worker keeps committing/merging behind the popup; a merge
        that needs the user (a conflict) just queues its own popup, answered one at a time."""
        if self.base_repo is None:
            return
        rels, fresh, current = collected
        base_dir = self.base_repo.repo
        wt_dir = self.repo.repo
        on_exit = context == "exit"
        # The file list is shown vertically (one per line, scrolls with PgUp/PgDn), led by
        # a "File(s):" line (set off with a blank line above it) so it's clear the message
        # refers to these names.
        deleting_worktree = on_exit and bool(self._exit_delete_worktrees)
        if on_exit and deleting_worktree:
            title = (
                f"This session's worktree ({wt_dir}) has {len(rels)} file(s) not in the base repo "
                f"that will be DELETED when the worktree is removed on exit. Copy them into the base "
                f"repo ({base_dir}) first? (Press Esc to cancel the exit and handle them yourself.)\n"
                f"\nFile(s):"
            )
            options = ["No, discard them with the worktree", "Yes, copy to the base repo"]
        elif on_exit:
            title = (
                f"This session's worktree ({wt_dir}) has {len(rels)} file(s) the base repo "
                f"({base_dir}) doesn't have, or that are newer here. The worktree is KEPT on exit, "
                f"but you work in the base directory — copy them across before quitting? "
                f"(Press Esc to cancel the exit and handle them yourself.)\n"
                f"\nFile(s):"
            )
            options = ["No, leave them in the worktree", "Yes, copy to the base repo"]
        else:
            title = (
                f"This session's worktree ({wt_dir}) has {len(rels)} file(s) that won't be merged "
                f"into the base — intentionally unstaged or git-ignored. Copy them into the base repo ({base_dir})?\n"
                f"(Decline and aGiTrack won't ask about this set again until the files change as a set "
                f"or you switch sessions.)\n"
                f"\nFile(s):"
            )
            options = ["No, leave them in the worktree", "Yes, copy to the base repo"]
        if self._copy_always:
            # The user already chose to copy this set, so don't re-ask — copy without a prompt.
            # (Kept until the candidate set changes or aGiTrack restarts. The overwrite of any
            # base file is still its own decision below, unless "always overwrite" was chosen.)
            choice: str | None = options[-1]  # the "Yes, copy…" option
        else:
            choice = self._select_popup(title, options, detail=rels)
        if on_exit and choice is None:
            # Esc on the exit offer cancels the exit entirely (handled by the caller) so the
            # user can copy/move the files themselves — nothing is discarded or removed. Forget
            # we offered them so the NEXT exit re-offers (the user didn't actually resolve them).
            for rel in rels:
                self._copy_prompted.pop(rel, None)
            self._exit_aborted = True
            return
        if choice is None or choice.startswith("No"):
            if on_exit and deleting_worktree:
                self._set_message(f"Discarding {len(rels)} file(s) with the worktree.", seconds=6.0)
                self._render()
            elif on_exit:
                # The worktree is kept, so the files are NOT discarded — they simply stay in
                # the worktree, available at its path for next time.
                self._set_message(f"Left {len(rels)} file(s) in this session's worktree ({wt_dir}).", seconds=6.0)
                self._render()
            else:
                self._copy_declined |= current  # mute the whole set until it changes / switch
                self._notice_files_remain(wt_dir, rels)
            return
        # The user chose to copy: remember it for this set so later offers (same set, even as
        # the contents change) copy without re-asking — kept until the set changes / restart.
        self._copy_always = True
        # Files that would overwrite something already in the base are handled in one up-front
        # choice: overwrite them all, keep them all (the new files still copy), confirm each one,
        # or "always overwrite" — which also remembers the choice so later offers for this set
        # overwrite without re-asking (until the set changes / restart). A prior "always
        # overwrite" skips this prompt entirely.
        conflicts = [rel for rel in rels if (base_dir / rel).exists()]
        overwrite_mode = "all"  # no conflicts → moot
        if conflicts and self._overwrite_always:
            overwrite_mode = "all"  # remembered "always overwrite" — don't ask again
        elif conflicts:
            answer = self._select_popup(
                f"{len(conflicts)} of these already exist in the base repo. Overwrite them?",
                [
                    "No, keep the base versions",
                    "Yes, overwrite all",
                    "Yes, always overwrite (don't ask again)",
                    "Let me confirm each one",
                ],
                detail=conflicts,
            )
            if answer == "Yes, always overwrite (don't ask again)":
                overwrite_mode = "all"
                self._overwrite_always = True
            elif answer == "Yes, overwrite all":
                overwrite_mode = "all"
            elif answer == "Let me confirm each one":
                overwrite_mode = "each"
            else:  # "No, keep the base versions" or cancelled
                overwrite_mode = "none"
        remained: list[str] = []
        failed: list[tuple[str, str]] = []
        copied = 0
        # Announce the copy BEFORE it runs (a wholly-ignored directory like node_modules can
        # take a moment to copy), so the user sees that work is happening rather than the
        # "Copied …" confirmation appearing as if from nowhere. Only when something will
        # actually be copied — a "keep the base versions" choice on every conflict copies
        # nothing, so it must not claim to be copying.
        attempting = [rel for rel, _ in fresh if not ((base_dir / rel).exists() and overwrite_mode == "none")]
        if attempting:
            self._set_message(f"Copying {len(attempting)} file(s) into the base repo directory…")
            self._render()
        for rel, _ in fresh:
            src, dst = wt_dir / rel, base_dir / rel
            if dst.exists():
                if overwrite_mode == "none":
                    remained.append(rel)  # keep all base versions
                    continue
                if overwrite_mode == "each":
                    per = self._select_popup(
                        f"'{rel}' already exists in the base repo. Overwrite it?",
                        ["No, keep the base version", "Yes, overwrite"],
                    )
                    if per != "Yes, overwrite":
                        remained.append(rel)
                        continue
            try:
                if src.is_dir():  # a wholly-ignored directory, reported as `dir/`
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                copied += 1
            except OSError as error:
                # Surface the actual error (and tell the user they can copy it by hand) rather
                # than silently lumping it in with "left in the worktree" — a failed copy of a
                # directory looked just like a declined one, which was confusing.
                self._debug(f"copy {rel} to base failed: {error!r}")
                failed.append((rel, str(error)))
        if copied:
            # aGiTrack itself just wrote these into the base repo (the copy-back of stranded
            # worktree files) — that is NOT the agent editing outside its worktree, so fold them
            # into the un-sandboxed base-edit monitor's baseline. Otherwise _warn_if_base_edited
            # would flag aGiTrack's own copy as "Agent edited the base repo".
            self._rebaseline_base_edits()
        if failed:
            shown = "; ".join(f"{rel} ({err})" for rel, err in failed[:3])
            more = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
            self._set_message(
                f"Could not copy {len(failed)} item(s) into the base repo — copy them manually "
                f"from {wt_dir}. Error: {shown}{more}",
                seconds=15.0,
            )
            self._render()
        if remained:
            self._notice_files_remain(wt_dir, remained)
        elif copied and not failed:
            self._set_message(f"Copied {copied} file(s) into the base repo directory.")
            self._render()

    def _uncommitted_worktree_files(self) -> list[str]:
        """Repo-relative paths of files left in the worktree that no commit will carry
        into the base directory: unstaged tracked edits, untracked files, and
        git-*ignored* files (build output, local data, etc. the agent may have created).

        Always skipped: aGiTrack's own ``.agitrack/`` and any path whose final name
        starts with ``_`` or ``.`` — generated/hidden scaffolding (``__pycache__``,
        ``.venv``, ``.env`` …) the user doesn't want copied back. So a turn that touched
        only such files yields an empty list and is never offered for copying."""
        files: list[str] = []
        try:
            status = self.repo.status_short_ignored()
        except Exception as error:
            self._debug(f"status for uncommitted-files check failed: {error!r}")
            return files
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:  # a rename: take the destination
                path = path.split(" -> ", 1)[1]
            path = path.strip().strip('"')
            if not path or path.startswith(".agitrack/"):
                continue
            if self._copy_name_is_skipped(path):
                continue
            files.append(path)
        return files

    @staticmethod
    def _copy_name_is_skipped(path: str) -> bool:
        """True for paths whose final component starts with ``_`` or ``.`` (a wholly
        ignored directory is reported as ``dir/``, so the trailing slash is stripped
        first). These are never offered for copying into the base directory."""
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return name.startswith("_") or name.startswith(".")

    @staticmethod
    def _file_fingerprint(path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    @staticmethod
    def _entry_mtime(path) -> float:
        """Modification time (epoch seconds) of a worktree entry — a file, or a collapsed
        ``dir/``. For a directory this is the directory's own mtime, which moves when entries are
        added/removed in it: enough to tell "touched after the env copy" from "came from it".
        Missing/unreadable → 0.0 (treated as ancient, i.e. not modified-since)."""
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _differs_from_base(self, wt_path: Path, base_path: Path) -> bool:
        """Whether a worktree entry (file OR directory) actually differs from the base repo's
        copy — so the copy-back offer only prompts when there's a real difference, and never again
        once a copy has made them identical. A missing base counterpart, or a file⇄dir type change,
        counts as a difference. Directories are compared recursively. Best-effort: on any
        comparison error, treat as differing so a genuine change is never silently dropped."""
        try:
            if not base_path.exists():
                return True
            if wt_path.is_dir() != base_path.is_dir():
                return True
            if wt_path.is_dir():
                return self._dirs_differ(wt_path, base_path)
            # Size first (free, and settles most cases); when the sizes match, the CONTENT
            # decides. This used to accept filecmp's SHALLOW verdict as proof of "identical",
            # and a matching (size, mtime) is not proof of anything: two different files
            # written in the same clock tick compare equal, and the copy-back offer then
            # dropped a file the user had actually changed — losing their work silently.
            # Windows, with the coarser file timestamps, lost that race as a matter of course.
            if wt_path.stat().st_size != base_path.stat().st_size:
                return True
            return not filecmp.cmp(str(wt_path), str(base_path), shallow=False)
        except OSError:
            return True

    def _dirs_differ(self, a: Path, b: Path) -> bool:
        """Recursively whether directory ``a`` differs from ``b`` (extra/missing entries, or any
        file whose CONTENT differs). dircmp compares shallowly (size+mtime), so any flagged file
        is content-confirmed to avoid mtime-jitter false positives. Short-circuits on the first
        real difference, so an identical tree (e.g. node_modules copied from base) is cheap."""
        try:
            cmp = filecmp.dircmp(str(a), str(b))
        except OSError:
            return True
        if cmp.left_only or cmp.right_only or cmp.funny_files:
            return True
        if cmp.diff_files:
            _match, mismatch, errors = filecmp.cmpfiles(str(a), str(b), cmp.diff_files, shallow=False)
            if mismatch or errors:
                return True
        return any(self._dirs_differ(a / sub, b / sub) for sub in cmp.common_dirs)

    def _notice_files_remain(self, wt_dir, files: list[str]) -> None:
        listing = ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
        self._set_message(
            f"{len(files)} file(s) left in this session's worktree: {listing}. They're available at "
            f"{wt_dir}; the worktree is kept across runs, so they'll still be there next time.",
            seconds=15.0,
        )
        self._render()

    def _integrate_agent_made_commits_if_idle(self, now: float) -> None:
        # The agent can run `git commit` itself (some workflows ask it to).
        # Those turns leave the tree CLEAN, so the auto-commit path — which
        # integration used to piggyback on exclusively — never runs, and the
        # commits sat unaccounted-for until exit/restart. When the session is
        # idle and its tree is clean, account for those commits now.
        if self.merge_ctx:
            return
        if self._integration_paused or self._delay_merge:
            return  # --delay-merge: don't touch the branch behind the user's back
        if self._agent_is_active() or now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return
        if now - self._idle_integrate_at < self.BASE_POLL_SECONDS:
            return
        self._idle_integrate_at = now
        if not self._use_worktrees:
            # No-worktree: the agent's own commits live on the current branch (there is no
            # separate base to integrate into). The prepare-commit-msg fold hook normally folds
            # the pending tracking INTO the agent's commit, and then there is nothing uncovered
            # and nothing to do here.
            #
            # But the hook can only fold a turn that has FINISHED. When the agent commits
            # MID-TURN it stamps an in-flight block instead — attribution without the trace or
            # the tokens, and its own text promises they "land in a later commit". If the turn
            # then finishes having left nothing further to commit (the agent already committed
            # every file it touched), the tree is clean, the latent fold has nothing to fold,
            # and that promised later commit never happens: the turn's whole token accounting
            # is lost, silently. `_uncovered_backend_commits` is precisely the question "is
            # anything still owed a record?" — it already treats an in-flight-only commit as
            # uncovered — so let the #35 cover pass run whenever it says yes.
            #
            # (This branch used to be unreachable: `_latent_tracking` is defined as
            # `not _use_worktrees`, so an earlier `if self._latent_tracking: return` fired first
            # on exactly the sessions this is written for.)
            if not self._uncovered_backend_commits() or self._summary_blocks_integration(now):
                return
            if self._manual_commits:
                # Manual mode: the user decides when to commit, so aGiTrack must not add one.
                # The pending chain keeps the record and folds into the user's next commit —
                # `_reset_stale_manual_ref` is what must not throw it away in the meantime.
                return
            self._attach_trace_to_backend_commits(now)  # parse → commit_turns → cover commit
            return
        if self.worktree is None or self._base_branch is None:
            return
        try:
            branch = self.repo.current_branch()
            if not is_managed_branch(branch) or not self.base_repo.log_range(self._base_branch, branch):
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

    # Host-terminal operations route through the platform host object (POSIX termios /
    # Windows Win32 console) once run() created it; before that (unit tests construct a
    # runner without run()), they fall back to the POSIX TerminalHost mixin directly.
    def _suspend_to_shell(self) -> None:
        """Ctrl-Z: stop aGiTrack like any other foreground job, and come back intact.

        The terminal is handed back the way exiting hands it back, then this process stops
        itself; the shell reports "Stopped" and takes over. ``fg`` continues execution right
        here, so the inverse — alternate screen, raw mode, mouse reporting — is re-asserted
        and the screen repainted from aGiTrack's own model. The backend keeps running
        throughout and never learns any of this happened, which is the point: it is not
        asked to suspend itself on a pty where nothing could resume it.

        Windows has no job control, so there Ctrl-Z stays a byte for the backend.
        """
        if sys.platform == "win32":
            return
        import signal as _signal

        try:
            if self._host is not None:
                self._host.suspend_host()
            else:
                TerminalHost.suspend_host(self)
        except Exception as error:
            self._debug(f"suspend: restoring the terminal failed: {error!r}")
        try:
            os.kill(os.getpid(), _signal.SIGSTOP)  # returns when the shell resumes us
        except Exception as error:
            self._debug(f"suspend: stopping failed: {error!r}")
        try:
            if self._host is not None:
                self._host.resume_host()
            else:
                TerminalHost.resume_host(self)
        except Exception as error:
            self._debug(f"resume: retaking the terminal failed: {error!r}")
        self._render_pending = True
        self._render()

    def _pause_child_ui(self) -> None:
        if self._host is not None:
            self._host.pause_child_ui()
        else:
            TerminalHost.pause_child_ui(self)

    def _resume_child_ui(self) -> None:
        if self._host is not None:
            self._host.resume_child_ui(self._render)
        else:
            TerminalHost.resume_child_ui(self, self._render)

    def _set_raw(self) -> None:
        if self._host is not None:
            self._host.set_raw()
        else:
            TerminalHost.set_raw(self)

    def _set_cooked(self) -> None:
        if self._host is not None:
            self._host.set_cooked()
        else:
            TerminalHost.set_cooked(self)

    def _reassert_host_raw(self) -> None:
        # Windows only: after a backend switch, forcibly re-apply console raw mode in case the
        # switch knocked the host console back to cooked/echo (all input then echoes as codes,
        # Ctrl-C dies, the menu can't be navigated). A no-op on POSIX (the PTY raw mode is stable
        # across a switch) and whenever the host doesn't expose it.
        host = self._host
        reassert = getattr(host, "reassert_raw", None)
        if callable(reassert):
            changed = reassert()
            if changed:
                self._debug(f"reassert_host_raw: re-applied console mode (changed={changed})")

    def _restore_terminal(self) -> None:
        if self._host is not None:
            self._host.restore_terminal()
        else:
            TerminalHost.restore_terminal(self)

    def _disable_host_terminal_modes(self) -> None:
        if self._host is not None:
            self._host.disable_host_terminal_modes()
        else:
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
        except Exception as error:
            # This runs INSIDE the SIGWINCH handler, so an exception here does not fail a
            # resize: it propagates into whatever the main thread happened to be executing.
            # A resize is never worth ending a session for.
            self._debug(f"resize failed: {error!r}")

    def _terminal_size(self) -> tuple[int, int]:
        if self._host is not None:
            return self._host.terminal_size()
        return TerminalHost.terminal_size(self)

    def _stdin_fileno(self) -> int:
        """The select-able stdin fd: the real terminal fd on POSIX, the host's
        console→socket bridge fd on Windows (Windows can't select on a console handle)."""
        return self._host.stdin_fileno() if self._host is not None else sys.stdin.fileno()

    def _read_stdin(self, length: int) -> bytes:
        if self._host is not None:
            data = self._host.read_stdin(length)
        else:
            data = os.read(sys.stdin.fileno(), length)
        # Capture EVERY stdin read here (not just the main reactor phase) so input read by
        # popups/modals and the wait-with-cancel loops is logged too — essential for diagnosing
        # a post-switch state where the menu's own keys (arrows/Esc) arrive misencoded. Each
        # _raw_capture call opens+writes+closes, so the log is flushed line-by-line and survives a
        # hard-kill (needed when a leak has broken Ctrl-C and graceful exit isn't possible).
        if data:
            self._raw_capture(">", data)
            self._track_bracketed_paste(data)
        return data

    def _track_bracketed_paste(self, data: bytes) -> None:
        """Follow the terminal's bracketed-paste state across reads.

        Every popup this runner opens consumes keystrokes, so it must be able to tell a
        paste from typing: the newlines inside a paste are content, and answering a dialog
        with them silently picked its default while the user was only pasting a prompt at
        the agent. The modals recognise the markers in their own input, but a paste already
        in flight when a popup opens delivers CSI 200~ to the BACKEND and the popup sees
        only the tail — hence this state, which seeds each modal (see _run_modal_loop).

        Reads are 32 bytes at a time, so a marker can straddle two of them: the last few
        bytes of each read are carried over and re-scanned with the next.
        """
        scan = self._paste_scan_tail + data
        start, end = scan.rfind(PASTE_START), scan.rfind(PASTE_END)
        if start > end:
            self._in_bracketed_paste = True
        elif end > start:
            self._in_bracketed_paste = False
        self._paste_scan_tail = scan[-(len(PASTE_START) - 1) :]


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
    # _base_branch gets a custom property below (it must also keep the single
    # IntegrationService in sync on every write), so skip the plain delegation.
    if _field == "_base_branch":
        continue
    setattr(ProxyRunner, _field, _delegate_to_active_session(_field))
del _field


def _get_base_branch(self):
    return self.active._base_branch


def _set_base_branch(self, value):
    # Per-session integration target. Writing it also re-points the single
    # IntegrationService, so a session always merges into its own branch.
    self.active._base_branch = value
    svc = self.__dict__.get("_integration_svc")
    if svc is not None:
        svc.base_branch = value


# Attached via setattr (not a plain assignment) so mypy uses the class-level
# annotation above rather than flagging the property vs. str|None mismatch.
setattr(
    ProxyRunner,
    "_base_branch",
    property(_get_base_branch, _set_base_branch, doc="Per-session integration target (delegates to self.active)."),
)
