"""Screen renderer for the proxy package (#29, P1).

Contains :class:`ScreenRenderer` — extracted from :class:`~agit.proxy.runner.ProxyRunner`
— plus the module-level palette helpers and :func:`detect_color_mode` that the
renderer depends on.  The helpers are also re-exported from ``agit.proxy`` so
existing import sites (tests, shim) keep working unchanged.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time

import pyte


# ---------------------------------------------------------------------------
# xterm-256 palette helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _BackgroundColorEraseScreen
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ScreenRenderer
# ---------------------------------------------------------------------------

class ScreenRenderer:
    """Owns the pyte screen/stream and converts the grid to ANSI output.

    Per-session display state (screen, stream, scroll_back, sel_active,
    sel_anchor, sel_point, child_mouse) lives on each proxy Session object
    (agit.proxy.session); the runner exposes it under the same attribute names
    via properties that delegate to the active session (see runner.py), so the
    duck-typed delegation here keeps reading ``self.<attr>`` unchanged.

    Render-throttle state (_last_render, _render_pending, _in_sync_update,
    _sync_since) is host-level and is NOT swapped per session.
    """

    # Throttle / sync-update constants (same defaults as ProxyRunner class
    # constants so __new__-built test runners that never call __init__ still
    # resolve them via getattr-default style).
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps
    SYNC_MAX_HOLD = 0.05  # cap how long a backend synchronized-update may defer a paint

    def __init__(self, rows: int, cols: int, *, color_mode: str = "truecolor") -> None:
        self.rows = rows
        self.cols = cols
        self.color_mode = color_mode

        # Per-session display state (owned by Session; mirrored here)
        self.screen: pyte.Screen | None = None
        self.stream: pyte.ByteStream | None = None
        self.scroll_back: int = 0
        self.child_mouse: bool = False
        self.sel_active: bool = False
        self.sel_anchor: tuple[int, int] | None = None
        self.sel_point: tuple[int, int] | None = None

        # Render-throttle state (host-level, not swapped)
        self._last_render: float = 0.0
        self._render_pending: bool = False
        self._in_sync_update: bool = False
        self._sync_since: float = 0.0

    # ------------------------------------------------------------------
    # Screen initialisation
    # ------------------------------------------------------------------

    def init_screen(self, rows: int, cols: int) -> None:
        """Create (or replace) the pyte screen for the given terminal size."""
        self.rows = rows
        self.cols = cols
        self.screen = _BackgroundColorEraseScreen(cols, max(rows - 1, 1), history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.scroll_back = 0
        self._in_sync_update = False

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def feed(self, output: bytes, *, pyte_hostile_csi_re) -> None:
        """Feed child output into the pyte model (strips pyte-hostile CSI)."""
        if self.stream is not None:
            try:
                self.stream.feed(pyte_hostile_csi_re.sub(b"", output))
            except Exception as error:  # never let a parse hiccup kill the session
                getattr(self, "_debug", lambda message: None)(f"pyte feed error: {error!r}")

    # ------------------------------------------------------------------
    # Synchronized-update tracking
    # ------------------------------------------------------------------

    def track_sync_update(self, output: bytes) -> None:
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

    def sync_hold(self, now: float, sync_max_hold: float) -> bool:
        """True while a backend synchronized-update should still defer the paint."""
        return self._in_sync_update and now - self._sync_since < sync_max_hold

    # ------------------------------------------------------------------
    # Throttled render dispatch
    # ------------------------------------------------------------------

    def render_output(self, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        """Coalesce repaints driven by a flood of backend output to ~30fps."""
        now = time.monotonic()
        if self.sync_hold(now, sync_max_hold):
            self._render_pending = True
            return
        if now - self._last_render >= render_min_interval:
            self._last_render = now
            self._render_pending = False
            render_fn()
        else:
            self._render_pending = True

    def flush_pending_render(self, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        if not self._render_pending:
            return
        now = time.monotonic()
        if self.sync_hold(now, sync_max_hold):
            return
        if now - self._last_render >= render_min_interval:
            self._last_render = now
            self._render_pending = False
            render_fn()

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def cursor_sequence(self, rows: int, cols: int, scroll_back: int) -> str:
        """The trailing sequence that positions (and shows) or hides the cursor."""
        assert self.screen is not None
        if scroll_back > 0:
            # While scrolled into history, keep the cursor hidden (its live
            # position is not meaningful for the displayed lines).
            return "\x1b[?25l"
        cursor = self.screen.cursor
        cursor_row = min(cursor.y + 1, max(rows - 1, 1))
        cursor_col = min(cursor.x + 1, cols)
        return f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h"

    # ------------------------------------------------------------------
    # Visible lines / history / scrollback
    # ------------------------------------------------------------------

    def history_len(self) -> int:
        # `screen` always resolves (ScreenRenderer sets it in __init__; the
        # runner delegates it to the active Session) but plain pyte.Screen has
        # no history attribute, hence the getattr on it.
        history = getattr(self.screen, "history", None)
        return len(history.top) if history is not None else 0

    def scroll(self, delta: int, render_fn) -> None:
        new_back = max(0, min(self.scroll_back + delta, self.history_len()))
        if new_back != self.scroll_back:
            self.scroll_back = new_back
            # Selection coordinates refer to the displayed view, which just
            # shifted; drop any in-progress selection.
            self.sel_active = False
            self.sel_anchor = self.sel_point = None
            render_fn()

    def visible_lines(self, rows: int) -> list:
        """The (rows-1) lines to draw. Splices in history when scrolled back."""
        assert self.screen is not None
        num_rows = max(rows - 1, 1)
        live = [self.screen.buffer.get(row, {}) for row in range(num_rows)]
        if self.scroll_back <= 0 or not self.history_len():
            return live
        history = list(self.screen.history.top)
        combined = history + live
        end = len(combined) - self.scroll_back
        end = max(num_rows, min(end, len(combined)))
        return combined[end - num_rows:end]

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def selection_ranges(self, cols: int) -> dict[int, tuple[int, int]]:
        """Map each selected display row to its inclusive (start_col, end_col)."""
        if not (self.sel_active and self.sel_anchor and self.sel_point):
            return {}
        (r1, c1), (r2, c2) = sorted([self.sel_anchor, self.sel_point])
        ranges: dict[int, tuple[int, int]] = {}
        for row in range(r1, r2 + 1):
            start = c1 if row == r1 else 0
            end = c2 if row == r2 else cols - 1
            ranges[row] = (start, end)
        return ranges

    def copy_selection(self, rows: int, cols: int, copy_to_clipboard_fn, set_message_fn) -> None:
        lines = self.visible_lines(rows)
        text_lines = []
        for row, (start, end) in sorted(self.selection_ranges(cols).items()):
            cells = lines[row] if row < len(lines) else {}
            text = "".join((cells.get(x).data if cells.get(x) else " ") for x in range(start, end + 1))
            text_lines.append(text.rstrip())
        text = "\n".join(text_lines).strip("\n")
        if not text.strip():
            return
        copy_to_clipboard_fn(text)
        set_message_fn(f"Copied {len(text)} char(s) to clipboard.", seconds=2.0)

    # ------------------------------------------------------------------
    # Cell / line rendering
    # ------------------------------------------------------------------

    def cell_sgr(self, cell) -> str:
        """Reproduce exactly what OpenCode rendered into this cell, including
        the original colour encoding, so the cell is byte-equivalent to a
        native session on the same terminal."""
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
        fg = self.color_code(getattr(cell, "fg", "default"), foreground=True)
        bg = self.color_code(getattr(cell, "bg", "default"), foreground=False)
        if fg:
            codes.append(fg)
        if bg:
            codes.append(bg)
        return ";".join(codes)

    def color_code(self, color: str, *, foreground: bool) -> str | None:
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
            return self.hex_color_code(color.lower(), foreground=foreground)
        if color.startswith("bright"):
            key = color.removeprefix("bright")
            return str(bright_base + colors[key]) if key in colors else None
        return str(base + colors[color]) if color in colors else None

    def hex_color_code(self, color: str, *, foreground: bool) -> str:
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

    def render_line(self, cells, sel: tuple[int, int] | None = None, *, cols: int) -> str:
        rendered = []
        current = ""  # SGR body currently applied on the host terminal ("" == default)
        sel_start, sel_end = sel if sel else (-1, -1)
        for col in range(cols):
            cell = cells.get(col)
            base = "" if cell is None else self.cell_sgr(cell)
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

    # ------------------------------------------------------------------
    # Status line
    # ------------------------------------------------------------------

    def status_line(
        self,
        *,
        cols: int,
        name: str,
        backend_name: str,
        session_id: str | None,
        base_branch: str | None,
        worktree,
        scroll_back: int,
        user_declined: list,
        short_session_fn,
            menu_label: str = "Ctrl-G",
    ) -> str:
        declined = len(user_declined)
        session = f"{name or 'session'}" + (f" [{short_session_fn(session_id)}]" if session_id else "")
        if base_branch and worktree is not None:
            session += f" → {base_branch}"  # the branch this session's work merges into
        left = f" aGiT {menu_label} | {session} | {backend_name} "
        if scroll_back > 0:
            right = f" SCROLLBACK -{scroll_back} (scroll down to resume) "
        else:
            right = f" unstaged:{declined} " if declined else ""
        padding = " " * max(cols - len(left) - len(right), 0)
        return f"\x1b[7m{left}{padding}{right}\x1b[0m"

    # ------------------------------------------------------------------
    # Box / popup painting primitives
    # ------------------------------------------------------------------

    def append_box(
        self,
        parts: list[str],
        row: int,
        col: int,
        width: int,
        lines: list[str],
        highlight: str | None = None,
        *,
        rows: int,
    ) -> None:
        inner = max(width - 2, 1)
        border_top = "┌" + "─" * inner + "┐"
        border_bottom = "└" + "─" * inner + "┘"
        box_lines = [border_top]
        wrapped_lines: list[str] = []
        for line in lines:
            wrapped_lines.extend(textwrap.wrap(line, width=inner) or [""])
        max_body = max(rows - row - 2, 1)
        for line in wrapped_lines[:max_body]:
            content = line[:inner].ljust(inner)
            if highlight and line == highlight:
                box_lines.append("│" + "\x1b[7m" + content + "\x1b[0m" + "│")
            else:
                box_lines.append("│" + content + "│")
        box_lines.append(border_bottom)
        for offset, line in enumerate(box_lines):
            if row + offset >= rows:
                break
            parts.append(f"\x1b[{row + offset};{col}H\x1b[0m{line}")

    def append_command_palette(
        self,
        parts: list[str],
        *,
        rows: int,
        cols: int,
        input_text: str,
        input_matches: list[str],
        input_selected: str | None,
    ) -> None:
        width = min(max(52, cols // 2), cols - 4)
        row = 2
        col = max(2, (cols - width) // 2)
        lines = [
            "aGiT commands",
            f"> {input_text}",
            "Up/Down selects. Tab completes. Enter runs. Esc/Ctrl-C cancels.",
            "",
        ]
        lines.extend(input_matches[:8])
        self.append_box(parts, row, col, width, lines, highlight=input_selected, rows=rows)

    def append_message_popup(
        self,
        parts: list[str],
        message: str,
        *,
        rows: int,
        cols: int,
    ) -> None:
        width = min(max(52, cols // 2), cols - 4)
        row = 2
        col = max(2, (cols - width) // 2)
        self.append_box(parts, row, col, width, message.splitlines() or [message], rows=rows)

    # ------------------------------------------------------------------
    # Full-frame render
    # ------------------------------------------------------------------

    def render(
        self,
        *,
        rows: int,
        cols: int,
        scroll_back: int,
        status_line_str: str,
        input_capturing: bool,
        input_text: str,
        input_matches: list[str],
        input_selected: str | None,
        message: str | None,
        message_sticky: bool,
        message_until: float,
    ) -> None:
        if self.screen is None:
            return
        # Paint the whole screen inside one synchronized update (DECSET 2026) so
        # the host terminal applies the frame atomically and never shows it
        # half-drawn. Terminals that don't support 2026 ignore the markers and
        # fall back to the previous (unwrapped) full-repaint behaviour.
        parts = ["\x1b[?2026h\x1b[0m\x1b[?25l\x1b[H"]
        selection = self.selection_ranges(cols)
        for index, cells in enumerate(self.visible_lines(rows)):
            parts.append("\x1b[0m" + self.render_line(cells, selection.get(index), cols=cols))
            parts.append("\r\n")
        parts.append(status_line_str)
        if input_capturing:
            self.append_command_palette(
                parts,
                rows=rows,
                cols=cols,
                input_text=input_text,
                input_matches=input_matches,
                input_selected=input_selected,
            )
        elif message and (message_sticky or time.monotonic() < message_until):
            self.append_message_popup(parts, message, rows=rows, cols=cols)
        parts.append(self.cursor_sequence(rows, cols, scroll_back))
        parts.append("\x1b[?2026l")
        os.write(sys.stdout.fileno(), "".join(parts).encode())
