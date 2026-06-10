"""Host-terminal control for the proxy package (#29, P1).

Contains :class:`TerminalHost` — extracted from :class:`~agit.proxy.runner.ProxyRunner`
— which owns all interactions with the *host* terminal: raw/cooked mode
switching, alt-screen enter/leave, mouse enable/disable, terminal-capability
detection (fg/bg/palette/DA query-and-cache), and terminal-size querying.
"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import time
import tty


class TerminalHost:
    """Host-terminal control: raw mode, alt screen, mouse, capability detection.

    The class is instantiated once per ProxyRunner (host-level, not per
    session).  It holds the raw-mode save/restore attrs and the cached
    host-terminal colour responses so OpenCode can learn the real terminal
    theme.
    """

    def __init__(self) -> None:
        # Saved cooked-mode attrs so _set_cooked can restore them.
        self.old_attrs = None

        # Raw responses captured from the host terminal so we can answer the
        # same queries OpenCode makes (foreground/background/palette colors and
        # device attributes). Without these, OpenCode cannot detect the real
        # terminal theme and its colors do not match a native session.
        self.host_fg_value: bytes | None = None
        self.host_bg_value: bytes | None = None
        self.host_palette: dict[bytes, bytes] = {}
        self.host_da: bytes | None = None

    # ------------------------------------------------------------------
    # Terminal mode
    # ------------------------------------------------------------------

    def set_raw(self) -> None:
        tty.setraw(sys.stdin.fileno())

    def set_cooked(self) -> None:
        if self.old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_attrs)

    def restore_terminal(self) -> None:
        # Disable mouse reporting *before* handing the terminal back, then drop any
        # mouse reports the terminal already queued — otherwise those buffered SGR
        # sequences (e.g. "\x1b[<35;..M") leak to the shell as stray hex after exit.
        self.disable_host_terminal_modes()
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass
        self.set_cooked()
        os.write(sys.stdout.fileno(), b"\x1b[?1049l\x1b[0m\r\n")

    def pause_child_ui(self) -> None:
        self.set_cooked()
        os.write(sys.stdout.fileno(), b"\x1b[0m\r\n")

    def resume_child_ui(self, render_fn) -> None:
        self.set_raw()
        render_fn()

    # ------------------------------------------------------------------
    # Alt screen / mouse
    # ------------------------------------------------------------------

    def enter_host_screen(self) -> None:
        os.write(sys.stdout.fileno(), b"\x1b[?1049h\x1b[2J\x1b[H")
        self.enable_host_mouse()

    def enable_host_mouse(self) -> None:
        # Enable SGR mouse reporting on the host (1000 = button press/release +
        # wheel) so aGiT receives wheel events for scrollback and press/release
        # for its own copy. This is the minimal mode that reliably reports the
        # wheel; richer motion tracking (1002/1003) changes wheel reporting on
        # some terminals and is avoided. Backends that manage the mouse
        # themselves (OpenCode) re-assert their own modes afterwards.
        os.write(sys.stdout.fileno(), b"\x1b[?1000h\x1b[?1006h")

    def disable_host_terminal_modes(self) -> None:
        # Reset modes commonly enabled by full-screen TUIs: mouse tracking,
        # focus reporting, bracketed paste, alternate-scroll, cursor visibility,
        # and styling. Emit this independently from cooked-mode restoration so it
        # can also run from signal handlers before Python exits.
        os.write(
            sys.stdout.fileno(),
            b"\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1004l"
            b"\x1b[?1005l\x1b[?1006l\x1b[?1007l\x1b[?1015l\x1b[?1016l\x1b[?2004l"
            # Undo any keyboard-protocol state mirrored for the backend: pop the
            # kitty flags and switch modifyOtherKeys off.
            b"\x1b[<u\x1b[>4;0m"
            b"\x1b[?25h\x1b[0m",
        )

    # ------------------------------------------------------------------
    # Terminal size
    # ------------------------------------------------------------------

    def terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
            return size.lines, size.columns
        except OSError:
            return 24, 80

    # ------------------------------------------------------------------
    # Host-terminal capability detection
    # ------------------------------------------------------------------

    def detect_host_terminal(self, debug_fn=None) -> None:
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
        self.parse_host_terminal_responses(bytes(buffer), debug_fn=debug_fn)

    def parse_host_terminal_responses(self, data: bytes, *, debug_fn=None) -> None:
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
        if debug_fn:
            debug_fn(
                f"host terminal fg={self.host_fg_value!r} bg={self.host_bg_value!r} "
                f"palette={len(self.host_palette)} da={self.host_da!r}"
            )

    # ------------------------------------------------------------------
    # Resize (stdout write side only; PTY ioctl stays in runner for now)
    # ------------------------------------------------------------------
