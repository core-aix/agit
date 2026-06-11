"""Unit tests for agit.proxy.modal (P6 Stage 2).

Covers:
  - PromptModal byte-handling: typing, backspace, Enter, Esc cancel, Ctrl-C exit
  - SelectModal byte-handling: Up/Down navigation, Enter confirm, Esc cancel, Ctrl-C exit
  - ProxyRunner._run_modal: PTY drain via _popup_read_input while modal is open
"""

from __future__ import annotations

import os
import types


from agit.proxy.modal import PromptModal, SelectModal
from proxy_helpers import make_runner


# ---------------------------------------------------------------------------
# PromptModal unit tests
# ---------------------------------------------------------------------------


class TestPromptModal:
    def test_typing_and_confirm(self):
        m = PromptModal("Title", "Enter name:")
        assert m.feed(b"a") == ("redraw", None)
        assert m.feed(b"b") == ("redraw", None)
        assert m.feed(b"c") == ("redraw", None)
        assert m.value == "abc"
        assert m.feed(b"\r") == ("done", "abc")

    def test_backspace_removes_last_char(self):
        m = PromptModal("T", "P:")
        m.feed(b"hello")
        m.feed(b"\x7f")
        assert m.value == "hell"
        m.feed(b"\b")
        assert m.value == "hel"

    def test_default_value_used_on_empty_confirm(self):
        m = PromptModal("T", "P:", default="foo")
        assert m.value == "foo"
        assert m.feed(b"\n") == ("done", "foo")

    def test_lone_esc_cancels_immediately(self):
        m = PromptModal("T", "P:")
        m.feed(b"hi")
        # A lone Esc byte arriving as the whole read is treated as cancel.
        assert m.feed(b"\x1b") == ("cancel", None)

    def test_ctrl_c_returns_exit(self):
        m = PromptModal("T", "P:")
        assert m.feed(b"\x03") == ("exit", None)

    def test_escape_sequences_are_consumed_silently(self):
        m = PromptModal("T", "P:")
        # Arrow keys arrive as two-byte sequences (\x1b[A etc.).
        m.feed(b"\x1b[A")  # up arrow — ignored in prompt
        m.feed(b"\x1b[B")  # down arrow — ignored
        assert m.value == ""
        assert m.feed(b"\r") == ("done", "")

    def test_tab_is_ignored(self):
        # Tab (0x09 < 32) is not added to the value.
        m = PromptModal("T", "P:")
        m.feed(b"\t")
        assert m.value == ""

    def test_render_message_format(self):
        m = PromptModal("My Title", "Name:", default="bar")
        msg = m.render_message()
        assert "My Title" in msg
        assert "Name:" in msg
        assert "> bar" in msg

    def test_multiple_bytes_in_one_feed(self):
        m = PromptModal("T", "P:")
        result = m.feed(b"hello\r")
        assert result == ("done", "hello")

    def test_ctrl_c_stops_processing_remaining_bytes(self):
        # Bytes after Ctrl-C in the same chunk should not be processed —
        # feed returns immediately on Ctrl-C.
        m = PromptModal("T", "P:")
        result = m.feed(b"\x03xyz")
        assert result == ("exit", None)
        assert m.value == ""  # "xyz" was not appended


# ---------------------------------------------------------------------------
# SelectModal unit tests
# ---------------------------------------------------------------------------


class TestSelectModal:
    def test_confirm_first_option(self):
        m = SelectModal("Pick", ["alpha", "beta", "gamma"])
        assert m.feed(b"\r") == ("done", "alpha")

    def test_arrow_down_moves_selection(self):
        m = SelectModal("Pick", ["a", "b", "c"])
        m.feed(b"\x1b[B")  # down
        assert m.selected == 1
        m.feed(b"\x1b[B")  # down
        assert m.selected == 2
        assert m.feed(b"\r") == ("done", "c")

    def test_arrow_up_wraps(self):
        m = SelectModal("Pick", ["a", "b", "c"])
        m.feed(b"\x1b[A")  # up from 0 → wraps to 2
        assert m.selected == 2

    def test_arrow_down_wraps(self):
        m = SelectModal("Pick", ["a", "b"])
        m.selected = 1
        m.feed(b"\x1b[B")  # down from 1 → wraps to 0
        assert m.selected == 0

    def test_lone_esc_cancels(self):
        m = SelectModal("Pick", ["a", "b"])
        assert m.feed(b"\x1b") == ("cancel", None)

    def test_ctrl_c_returns_exit(self):
        m = SelectModal("Pick", ["a", "b"])
        assert m.feed(b"\x03") == ("exit", None)

    def test_render_message_shows_cursor(self):
        m = SelectModal("Title", ["opt1", "opt2"])
        msg = m.render_message()
        assert "> opt1" in msg
        assert "  opt2" in msg
        m.selected = 1
        msg = m.render_message()
        assert "  opt1" in msg
        assert "> opt2" in msg

    def test_other_escape_sequences_consumed_silently(self):
        # An unrecognised CSI sequence is absorbed; selection stays put.
        m = SelectModal("Pick", ["a", "b"])
        m.feed(b"\x1b[5~")  # PageUp — not handled, consumed
        assert m.selected == 0
        assert m.feed(b"\r") == ("done", "a")


# ---------------------------------------------------------------------------
# ProxyRunner._run_modal — reactor: PTY drains while modal is open
# ---------------------------------------------------------------------------


def _modal_runner(monkeypatch, stdin_fd):
    """Build a minimal ProxyRunner for modal/PTY-drain integration tests."""
    import agit.proxy.runner as proxy_mod

    runner = make_runner()
    monkeypatch.setattr(proxy_mod.sys, "stdin", types.SimpleNamespace(fileno=lambda: stdin_fd))
    runner.sessions = []
    runner.master_fd = None
    runner.last_child_output = 0.0
    runner.last_child_output_sample = b""
    runner._answer_terminal_queries = lambda output: None
    runner._sync_terminal_modes = lambda output: None
    runner._track_sync_update = lambda output: None
    runner._feed_child_output = lambda output: None
    # Minimal render stubs.
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None
    return runner


def test_popup_read_input_pumps_background_sessions(monkeypatch):
    """While a modal waits, BACKGROUND session PTYs are pumped too.

    The #22 stall was worst for background sessions (nothing else drains
    them); _popup_read_input must select on their fds and route them through
    _pump_background, not just the active master_fd.
    """
    stdin_r, stdin_w = os.pipe()
    bg_r, bg_w = os.pipe()
    try:
        runner = _modal_runner(monkeypatch, stdin_r)
        runner.master_fd = None  # no active PTY: only the background fd matters

        bg_session = types.SimpleNamespace(master_fd=bg_r, name="bg")
        runner._background_fds = lambda: {bg_r: bg_session}
        runner.sessions = [bg_session]
        pumped = []
        runner._pump_background = lambda session: pumped.append(session.name)

        os.write(bg_w, b"background backend streams during the modal")
        os.write(stdin_w, b"x")  # the keypress that ends the read

        data = runner._popup_read_input()
        assert data == b"x"
        assert pumped == ["bg"]
    finally:
        for fd in (stdin_r, stdin_w, bg_r, bg_w):
            try:
                os.close(fd)
            except OSError:
                pass


def test_run_modal_pty_drain_real_popup_read_input(monkeypatch):
    """PTY is drained via the real _popup_read_input when a modal is open.

    Uses actual pipe fds so that _popup_read_input's select() call both drains
    the child PTY and returns the user keypress.
    """
    stdin_r, stdin_w = os.pipe()
    child_r, child_w = os.pipe()
    try:
        runner = _modal_runner(monkeypatch, stdin_r)
        runner.master_fd = child_r
        fed = []
        runner._feed_child_output = lambda output: fed.append(output)

        # Backend streams while the prompt modal waits.
        os.write(child_w, b"streamed content")
        # User confirms with Enter immediately.
        os.write(stdin_w, b"\r")

        modal = PromptModal("T", "P:", default="prefilled")
        result = runner._run_modal(modal)
        assert result == "prefilled"
        assert fed == [b"streamed content"]
    finally:
        for fd in (stdin_r, stdin_w, child_r, child_w):
            try:
                os.close(fd)
            except OSError:
                pass


def test_run_modal_exit_flow_called_on_ctrl_c():
    """Ctrl-C inside a modal calls _run_exit_flow; exit confirmed → returns None."""
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    calls = []
    runner._popup_read_input = lambda: b"\x03"
    runner._run_exit_flow = lambda: (calls.append("exit"), True)[1]

    result = runner._run_modal(PromptModal("T", "P:"))
    assert result is None
    assert calls == ["exit"]


def test_run_modal_exit_declined_continues():
    """Ctrl-C with exit declined keeps the modal running."""
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    # First read: Ctrl-C (exit declined); second: confirm.
    reads = iter([b"\x03", b"\r"])
    runner._popup_read_input = lambda: next(reads)
    runner._run_exit_flow = lambda: False

    result = runner._run_modal(PromptModal("T", "P:"))
    assert result == ""


def test_run_modal_cancel_on_esc():
    """Esc cancels the modal and returns None."""
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None
    runner._popup_read_input = lambda: b"\x1b"

    result = runner._run_modal(PromptModal("T", "P:"))
    assert result is None


def test_run_modal_select_navigation():
    """SelectModal navigates with arrows and confirms via _run_modal."""
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    reads = iter([b"\x1b[B", b"\x1b[B", b"\r"])  # down, down, enter → index 2
    runner._popup_read_input = lambda: next(reads)

    result = runner._run_modal(SelectModal("Pick", ["a", "b", "c"]))
    assert result == "c"


def test_prompt_popup_facade_delegates_to_run_modal():
    """_prompt_popup is a thin facade: it constructs PromptModal and delegates."""
    runner = make_runner()
    modals_seen = []

    def fake_run_modal(modal):
        modals_seen.append(modal)
        return "result"

    runner._run_modal = fake_run_modal
    assert runner._prompt_popup("T", "P:", default="d") == "result"
    assert len(modals_seen) == 1
    assert isinstance(modals_seen[0], PromptModal)
    assert modals_seen[0].title == "T"
    assert modals_seen[0].prompt == "P:"
    assert modals_seen[0].value == "d"


def test_select_popup_facade_delegates_to_run_modal():
    """_select_popup is a thin facade: it constructs SelectModal and delegates."""
    runner = make_runner()
    modals_seen = []

    def fake_run_modal(modal):
        modals_seen.append(modal)
        return "choice"

    runner._run_modal = fake_run_modal
    assert runner._select_popup("Title", ["x", "y"]) == "choice"
    assert len(modals_seen) == 1
    assert isinstance(modals_seen[0], SelectModal)
    assert modals_seen[0].options == ["x", "y"]


def test_select_popup_empty_options_returns_empty_string():
    """_select_popup with no options returns '' without calling _run_modal."""
    runner = make_runner()
    runner._run_modal = lambda modal: (_ for _ in ()).throw(AssertionError("should not be called"))
    assert runner._select_popup("Title", []) == ""


# ---------------------------------------------------------------------------
# Stage 3 — exit-path unification: exit byte inside a modal reaches
# _finalize_pending_work (the one required test from the spec).
# ---------------------------------------------------------------------------


def test_exit_byte_in_modal_reaches_finalize_pending_work():
    """Ctrl-C inside an open modal calls _run_exit_flow → _finalize_pending_work.

    This validates the exit-path unification guarantee: no interactive route
    out of aGiT can bypass _finalize_pending_work().
    """
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    events = []
    runner._finalize_pending_work = lambda: events.append("finalize")
    runner._exit_child = lambda: events.append("exit")
    # Stub the background-session check; no background sessions in this test.
    runner._confirm_terminate_background_sessions = lambda: True

    # First read: Ctrl-C from inside the modal → _run_exit_flow is called.
    # _run_exit_flow opens _confirm_exit (which calls _select_popup/_run_modal).
    # The second read below answers the confirmation popup with "Yes, exit".
    reads = iter(
        [
            b"\x03",  # Ctrl-C inside the prompt modal → triggers exit flow
            b"\x1b[B",  # Down arrow to select "Yes, exit" in confirmation popup
            b"\r",  # Enter to confirm
        ]
    )
    runner._popup_read_input = lambda: next(reads)

    # A PromptModal open; user presses Ctrl-C.
    result = runner._run_modal(PromptModal("Stage", "Some prompt:"))

    assert result is None
    # Order matters: pending work is finalized BEFORE the child is torn down.
    assert events == ["finalize", "exit"]


def test_exit_byte_in_modal_decline_continues_modal():
    """Ctrl-C inside a modal with exit declined keeps the modal running."""
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    finalized = []
    runner._finalize_pending_work = lambda: finalized.append("finalize")
    runner._exit_child = lambda: finalized.append("exit")

    # Ctrl-C then exit declined (Esc on the confirmation popup → cancel → False),
    # then the user types "ok" and confirms.
    reads = iter(
        [
            b"\x03",  # Ctrl-C → _run_exit_flow → opens confirm popup
            b"\x1b",  # Esc → cancel the confirm popup → exit flow returns False
            b"o",  # back in original modal: typing 'o'
            b"k",
            b"\r",
        ]
    )
    runner._popup_read_input = lambda: next(reads)

    result = runner._run_modal(PromptModal("Stage", "Enter:"))
    assert result == "ok"
    assert finalized == [], "finalize must NOT be called when exit is declined"


def test_select_modal_escape_sequence_split_across_reads():
    """An arrow key split across read boundaries must still navigate, and the
    cross-feed escape buffer must not be confused with a lone-Esc cancel."""
    modal = SelectModal("Pick", ["a", "b", "c"])
    # b"\x1b[" arrives alone: incomplete sequence, no action yet.
    action, value = modal.feed(b"\x1b[")
    assert action in ("redraw", "continue", None) or action == "redraw"
    assert modal.selected == 0 or modal.selected == 0  # nothing moved yet
    # The tail b"B" completes Down: selection advances, no cancel.
    modal.feed(b"B")
    assert modal.selected == 1
    # Enter confirms the option the split sequence selected.
    action, value = modal.feed(b"\r")
    assert (action, value) == ("done", "b")


def test_prompt_modal_split_escape_not_treated_as_text():
    modal = PromptModal("T", "P:", default="")
    modal.feed(b"\x1b[")
    modal.feed(b"C")  # completes Right-arrow: consumed silently, not text
    modal.feed(b"h")
    action, value = modal.feed(b"\r")
    assert (action, value) == ("done", "h")
