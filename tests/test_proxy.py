import os
import threading
import time

from agit.backends.base import TokenUsage
from agit.opencode_session import SessionTurn
from agit.backends.proxy_agents import make_proxy_agent
from agit.proxy import ProxyInput, ProxyRunner, _escape_sequence_complete, _humanize_age, _short_session, detect_color_mode
from agit.session import SessionRef
from agit.state import AgitState


class _FakeBackend:
    name = "fake"

    def __init__(self, refs):
        self._refs = refs

    def list_sessions(self, repo):
        return list(self._refs)


def _runner_with_sessions(refs):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.backend = _FakeBackend(refs)
    runner.repo = type("Repo", (), {"repo": "/repo"})()
    return runner


def test_discover_spawned_session_picks_the_new_session():
    refs = [SessionRef("old", 100.0), SessionRef("new", 200.0)]
    runner = _runner_with_sessions(refs)
    runner._pre_spawn_session_ids = {"old"}
    assert runner._discover_spawned_session() == "new"


def test_discover_spawned_session_returns_none_when_nothing_new():
    refs = [SessionRef("old", 100.0), SessionRef("older", 50.0)]
    runner = _runner_with_sessions(refs)
    runner._pre_spawn_session_ids = {"old", "older"}
    assert runner._discover_spawned_session() is None


def test_discover_spawned_session_without_snapshot_uses_newest():
    refs = [SessionRef("a", 100.0), SessionRef("b", 300.0), SessionRef("c", 200.0)]
    runner = _runner_with_sessions(refs)
    runner._pre_spawn_session_ids = None
    assert runner._discover_spawned_session() == "b"


def test_resolve_session_id_matches_exact_and_unique_prefix():
    refs = [SessionRef("abc123", 1.0), SessionRef("abd999", 2.0)]
    runner = _runner_with_sessions(refs)
    assert runner._resolve_session_id("abc123") == "abc123"
    assert runner._resolve_session_id("abc") == "abc123"
    assert runner._resolve_session_id("ab") is None  # ambiguous prefix
    assert runner._resolve_session_id("zzz") is None


def test_short_session_and_humanize_age():
    assert _short_session("35e076c5-8653-439c") == "35e076c5"
    assert _short_session(None) == "(none)"
    import time

    assert _humanize_age(time.time() - 30).endswith("s ago")
    assert _humanize_age(time.time() - 3700).endswith("h ago")
    assert _humanize_age(0) == ""


class FakeCommitRepo:
    def __init__(self):
        self.message = ""

    def add_tracked(self):
        pass

    def has_staged_changes(self):
        return True

    def commit(self, message: str):
        self.message = message


def test_proxy_ctrl_g_enters_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07status\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


def test_proxy_forwards_colon_at_line_start():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b":status\r")

    assert b"".join(forwarded) == b":status\r"
    assert local_echo == b""
    assert command is None
    assert should_exit is False


def test_proxy_forwards_colon_inside_prompt():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"fix this: bug\r")

    assert b"".join(forwarded) == b"fix this: bug\r"
    assert local_echo == b""
    assert command is None
    assert should_exit is False


def test_proxy_forwards_slash_commands():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"/help\r")

    assert b"".join(forwarded) == b"/help\r"
    assert local_echo == b""
    assert command is None
    assert should_exit is False


def test_proxy_ctrl_c_exits_in_command_capture():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\x03")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is True


def test_proxy_escape_cancels_command_capture():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\x1b")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is False
    assert parser.capturing is False


def test_proxy_escape_clears_command_buffer():
    parser = ProxyInput()

    parser.feed(b"\x07sta\x1b")

    assert parser.text() == ""


def test_proxy_tab_completes_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


def test_proxy_arrow_selection_runs_selected_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "stage"
    assert should_exit is False


def test_proxy_tab_completes_selected_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "stage"


def test_proxy_enter_runs_selected_partial_match_without_tab():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


def test_proxy_agent_backend_command_name():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07agent-b\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "agent-backend"
    assert should_exit is False


def test_proxy_ignores_sgr_mouse_sequences_in_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[<35;88;11Mstatus\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


def test_popup_escape_sequence_consumer_waits_for_mouse_terminator():
    assert _escape_sequence_complete(b"\x1b[<35;88;11") is False
    assert _escape_sequence_complete(b"\x1b[<35;88;11M") is True
    assert _escape_sequence_complete(b"\x1b[<35;88;11m") is True
    assert _escape_sequence_complete(b"\x1b[35;88;11") is False
    assert _escape_sequence_complete(b"\x1b[35;88;11M") is True


def test_proxy_ctrl_c_exits_in_passthrough_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x03")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is True


def test_proxy_agent_commit_preserves_incomplete_initial_user_turn(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = FakeCommitRepo()
    runner.state = AgitState(tmp_path)
    runner.verbose = False
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "also handle errors")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn("u1", "a1", "fix it", "", TokenUsage(), None),
            SessionTurn("u2", "a2", "also handle errors", "done", TokenUsage(total=1, output=1), None),
        ],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    assert message.startswith("<agent> fix it / also handle errors")
    assert message.index("User:\nfix it") < message.index("User:\nalso handle errors")
    assert message.index("User:\nalso handle errors") < message.index("Agent:\ndone")


def test_proxy_agent_commit_preserves_previous_no_change_trace(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = FakeCommitRepo()
    runner.state = AgitState(tmp_path)
    runner.verbose = False
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "explain only")
    runner.state.append_trace("agent", "no code changed")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn("u1", "a1", "explain only", "no code changed", TokenUsage(), None),
            SessionTurn("u2", "a2", "now edit", "edited", TokenUsage(total=1, output=1), None),
        ],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    assert message.index("User:\nexplain only") < message.index("Agent:\nno code changed")
    assert message.index("Agent:\nno code changed") < message.index("User:\nnow edit")
    assert message.count("User:\nexplain only") == 1


def test_proxy_plain_row_handles_empty_pyte_cell_data():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols = 2

    class Cell:
        data = ""

    class Screen:
        buffer = {0: {0: Cell()}}

    runner.screen = Screen()

    assert runner._plain_row(0) == "  "


def _make_cell(data=" ", **attrs):
    class Cell:
        pass

    cell = Cell()
    cell.data = data
    cell.fg = "default"
    cell.bg = "default"
    cell.bold = cell.italics = cell.underscore = cell.blink = cell.reverse = cell.strikethrough = False
    for key, value in attrs.items():
        setattr(cell, key, value)
    return cell


def test_proxy_cell_sgr_reproduces_attributes():
    runner = ProxyRunner.__new__(ProxyRunner)

    # Default cell carries no styling.
    assert runner._cell_sgr(_make_cell()) == ""
    # Reverse video is forwarded verbatim, not flattened to white-on-black.
    assert runner._cell_sgr(_make_cell(reverse=True)) == "7"
    # Truecolor and 256-color (which pyte stores as hex) become 24-bit SGR.
    assert runner._cell_sgr(_make_cell(fg="ff8000")) == "38;2;255;128;0"
    # Named ANSI colors plus attributes round-trip to their SGR codes.
    assert runner._cell_sgr(_make_cell(bold=True, fg="red", bg="black")) == "1;31;40"
    assert runner._cell_sgr(_make_cell(italics=True, fg="brightcyan")) == "3;96"


def test_proxy_render_row_preserves_colors():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols = 3

    class Screen:
        buffer = {
            0: {
                0: _make_cell("a", fg="ff8000"),
                1: _make_cell("b", fg="ff8000"),
                2: _make_cell("c"),
            }
        }

    runner.screen = Screen()

    # The style is emitted once for the run of matching cells and reset before
    # the default-styled cell, so OpenCode's colors survive the round-trip.
    assert runner._render_row(0) == "\x1b[38;2;255;128;0mab\x1b[0mc"


def test_detect_color_mode_from_environment():
    assert detect_color_mode({"COLORTERM": "truecolor"}) == "truecolor"
    assert detect_color_mode({"COLORTERM": "24bit", "TERM": "xterm"}) == "truecolor"
    # OpenCode's default macOS env: no COLORTERM, a 256-colour TERM.
    assert detect_color_mode({"TERM": "xterm-256color"}) == "256"
    assert detect_color_mode({"TERM": "screen-256color"}) == "256"
    assert detect_color_mode({"TERM": "xterm"}) == "16"
    assert detect_color_mode({}) == "16"


def test_proxy_hex_color_preserves_256_encoding():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.color_mode = "256"
    # These hexes are exact xterm-256 palette entries OpenCode emits via 38;5;N.
    # They must round-trip back to the same palette index so the host terminal
    # renders them with its own palette, exactly like a native session.
    assert runner._hex_color_code("080808", foreground=False) == "48;5;232"
    assert runner._hex_color_code("eeeeee", foreground=True) == "38;5;255"
    assert runner._hex_color_code("7f7f7f", foreground=True) == "38;5;8"
    assert runner._hex_color_code("5fafff", foreground=True) == "38;5;75"


def test_proxy_hex_color_preserves_truecolor_encoding():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.color_mode = "truecolor"
    assert runner._hex_color_code("ff8000", foreground=True) == "38;2;255;128;0"
    assert runner._hex_color_code("0a0a0a", foreground=False) == "48;2;10;10;10"


def test_proxy_render_row_emits_256_colors_in_256_mode():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.color_mode = "256"
    runner.cols = 3

    class Screen:
        buffer = {
            0: {
                0: _make_cell("a", fg="eeeeee", bg="080808"),
                1: _make_cell("b", fg="eeeeee", bg="080808"),
                2: _make_cell("c"),
            }
        }

    runner.screen = Screen()

    out = runner._render_row(0)
    assert "38;5;255" in out and "48;5;232" in out
    assert "38;2;" not in out and "48;2;" not in out  # no truecolor leakage


def test_proxy_render_row_emits_reverse_video():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols = 3

    class Screen:
        buffer = {
            0: {
                0: _make_cell("a"),
                1: _make_cell("b", reverse=True),
                2: _make_cell("c"),
            }
        }

    runner.screen = Screen()

    assert runner._render_row(0) == "a\x1b[7mb\x1b[0mc"


def test_drain_child_output_reads_all_available():
    runner = ProxyRunner.__new__(ProxyRunner)
    read_fd, write_fd = os.pipe()
    try:
        runner.master_fd = read_fd
        os.write(write_fd, b"hello ")
        os.write(write_fd, b"world")
        assert runner._drain_child_output() == b"hello world"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_drain_child_output_returns_none_on_eof():
    runner = ProxyRunner.__new__(ProxyRunner)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # EOF, nothing buffered
    try:
        runner.master_fd = read_fd
        assert runner._drain_child_output() is None
    finally:
        os.close(read_fd)


def _history_runner():
    import pyte

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols, runner.rows = 12, 5  # 4 visible rows
    runner.child_mouse = False
    runner.scroll_back = 0
    runner.screen = pyte.HistoryScreen(12, 4, history=100, ratio=0.5)
    stream = pyte.ByteStream(runner.screen)
    for i in range(20):
        stream.feed(f"line{i:02d}\r\n".encode())
    runner._render = lambda: None
    return runner


def test_wheel_scrolls_history_and_strips_mouse_when_backend_has_no_mouse():
    runner = _history_runner()
    # Wheel-up consumes the event (returns empty) and scrolls back.
    assert runner._intercept_scroll(b"\x1b[<64;5;5M") == b""
    assert runner.scroll_back == 3
    runner._intercept_scroll(b"\x1b[<64;5;5M")
    assert runner.scroll_back == 6
    # Wheel-down moves toward the live view.
    runner._intercept_scroll(b"\x1b[<65;5;5M")
    assert runner.scroll_back == 3


def test_scrolled_view_shows_history_lines():
    runner = _history_runner()
    runner.scroll_back = 9

    def text(lines):
        return ["".join((c.get(x).data if c.get(x) else " ") for x in range(7)).rstrip() for c in lines]

    assert text(runner._visible_lines()) == ["line08", "line09", "line10", "line11"]
    runner.scroll_back = 0
    assert text(runner._visible_lines())[-2] == "line19"


def test_hold_incomplete_tail_buffers_split_escape_sequence():
    runner = ProxyRunner.__new__(ProxyRunner)
    # A mouse report split across reads must be held back, not leaked as bytes.
    head, tail = runner._hold_incomplete_tail(b"abc\x1b[<35;10;")
    assert head == b"abc"
    assert tail == b"\x1b[<35;10;"
    head2, tail2 = runner._hold_incomplete_tail(tail + b"5M")
    assert head2 == b"\x1b[<35;10;5M"
    assert tail2 == b""
    # A complete buffer leaves no tail.
    assert runner._hold_incomplete_tail(b"plain text") == (b"plain text", b"")


def test_pageup_pagedown_scroll_history():
    runner = _history_runner()
    runner._intercept_scroll(b"\x1b[5~")  # PageUp
    assert runner.scroll_back == max(runner.rows - 2, 1)
    runner._intercept_scroll(b"\x1b[6~")  # PageDown
    assert runner.scroll_back == 0


def test_mouse_drag_selects_and_copies():
    runner = _history_runner()
    import pyte

    runner.screen = pyte.HistoryScreen(20, 4, history=50, ratio=0.5)
    pyte.ByteStream(runner.screen).feed(b"hello world\r\n")
    runner.sel_active = False
    runner.sel_anchor = runner.sel_point = None
    copied = []
    runner._copy_to_clipboard = lambda text: copied.append(text)
    runner._set_message = lambda *a, **k: None

    runner._intercept_scroll(b"\x1b[<0;1;1M")   # press at col 1, row 1
    runner._intercept_scroll(b"\x1b[<32;5;1M")  # drag to col 5
    assert runner._selection_ranges() == {0: (0, 4)}
    runner._intercept_scroll(b"\x1b[<0;5;1m")   # release
    assert copied == ["hello"]
    assert runner.sel_active is False


def test_mouse_press_release_copies_without_motion_events():
    # With only button tracking (no 1002 motion), the release must still capture
    # the end point so a drag copies correctly.
    runner = _history_runner()
    import pyte

    runner.screen = pyte.HistoryScreen(20, 4, history=50, ratio=0.5)
    pyte.ByteStream(runner.screen).feed(b"hello world\r\n")
    runner.sel_active = False
    runner.sel_anchor = runner.sel_point = None
    copied = []
    runner._copy_to_clipboard = lambda text: copied.append(text)
    runner._set_message = lambda *a, **k: None

    runner._intercept_scroll(b"\x1b[<0;1;1M")  # press
    runner._intercept_scroll(b"\x1b[<0;5;1m")  # release (no motion in between)
    assert copied == ["hello"]


def test_mouse_events_are_stripped_from_forwarded_input():
    runner = _history_runner()
    runner.sel_active = False
    runner.sel_anchor = runner.sel_point = None
    runner._copy_to_clipboard = lambda text: None
    runner._set_message = lambda *a, **k: None
    assert runner._intercept_scroll(b"X\x1b[<0;3;2MY") == b"XY"


def test_reset_agent_tracking_reenables_scrollback_for_new_backend():
    # Switching OpenCode -> Claude must clear child_mouse so aGiT reclaims the
    # wheel for scrollback instead of forwarding it to a backend that ignores it.
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.child_mouse = True
    runner.scroll_back = 7
    runner.passthrough_prompt = bytearray(b"abc")
    runner.passthrough_escape = None

    runner._reset_agent_tracking()

    assert runner.child_mouse is False
    assert runner.scroll_back == 0


def test_wheel_forwarded_when_backend_manages_mouse():
    runner = _history_runner()
    runner.child_mouse = True
    event = b"\x1b[<64;5;5M"
    assert runner._intercept_scroll(event) == event  # passed through to backend
    assert runner.scroll_back == 0


def test_read_only_observer_skips_auto_commit():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.tracking_enabled = False
    # Should return immediately without touching repo/state/watcher attributes.
    assert runner._maybe_agent_commit() is None


def test_read_only_observer_skips_pre_agent_commit():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.tracking_enabled = False
    # Returns True (forward the prompt) without inspecting the repo.
    assert runner._pre_agent_commit_if_needed("do it") is True


def test_confirm_exit_read_only_exits_without_prompt():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.tracking_enabled = False
    assert runner._confirm_exit() is True


def test_confirm_exit_prompts_when_managing(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.tracking_enabled = True
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "Yes, exit")
    assert runner._confirm_exit() is True
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "No, keep working")
    assert runner._confirm_exit() is False
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: None)  # cancelled
    assert runner._confirm_exit() is False


def test_proxy_passthrough_prompt_drops_escape_sequences():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.passthrough_prompt = bytearray()
    runner.passthrough_escape = None

    # "fix" + down-arrow (ESC [ B) + " bug" must capture only the typed text.
    runner._update_passthrough_prompt([b"f", b"i", b"x", b"\x1b", b"[", b"B", b" ", b"b", b"u", b"g"])
    assert runner.passthrough_prompt.decode() == "fix bug"


def test_proxy_passthrough_prompt_handles_escape_split_across_reads():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.passthrough_prompt = bytearray()
    runner.passthrough_escape = None

    runner._update_passthrough_prompt([b"a", b"\x1b"])
    runner._update_passthrough_prompt([b"[", b"A", b"b"])  # up-arrow split, then 'b'
    assert runner.passthrough_prompt.decode() == "ab"


def test_proxy_parses_host_terminal_responses():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.host_fg_value = runner.host_bg_value = runner.host_da = None
    runner.host_palette = {}
    runner.debug_proxy = False

    runner._parse_host_terminal_responses(
        b"\x1b]10;rgb:1a1a/1a1a/1a1a\x07"
        b"\x1b]11;rgb:fafa/fafa/fafa\x07"
        b"\x1b]4;1;rgb:cccc/0000/0000\x07"
        b"\x1b[?62;c"
    )

    assert runner.host_fg_value == b"rgb:1a1a/1a1a/1a1a"
    assert runner.host_bg_value == b"rgb:fafa/fafa/fafa"
    assert runner.host_palette == {b"1": b"rgb:cccc/0000/0000"}
    assert runner.host_da == b"\x1b[?62;c"


def test_proxy_answers_terminal_queries_from_host_cache(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.master_fd = 99
    runner.rows, runner.cols = 30, 100
    runner.host_fg_value = b"rgb:1a1a/1a1a/1a1a"
    runner.host_bg_value = b"rgb:fafa/fafa/fafa"
    runner.host_palette = {b"1": b"rgb:cccc/0000/0000"}
    runner.host_da = b"\x1b[?62;c"

    class Cursor:
        x = 4
        y = 2

    class Screen:
        cursor = Cursor()

    runner.screen = Screen()

    written = []
    real_write = os.write

    def fake_write(fd, data):
        if fd == 99:
            written.append(data)
            return len(data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake_write)
    runner._answer_terminal_queries(
        b"\x1b]10;?\x07\x1b]11;?\x07\x1b]4;1;?\x07\x1b[6n\x1b[0c"
    )

    reply = b"".join(written)
    # OpenCode learns the real terminal colors, so it picks the matching theme.
    assert b"\x1b]10;rgb:1a1a/1a1a/1a1a\x07" in reply
    assert b"\x1b]11;rgb:fafa/fafa/fafa\x07" in reply
    assert b"\x1b]4;1;rgb:cccc/0000/0000\x07" in reply
    assert b"\x1b[3;5R" in reply  # cursor position report (1-based)
    assert b"\x1b[?62;c" in reply  # device attributes


def test_proxy_answers_nothing_without_host_values(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.master_fd = 99
    runner.rows, runner.cols = 30, 100
    runner.host_fg_value = runner.host_bg_value = runner.host_da = None
    runner.host_palette = {}
    runner.screen = None

    written = []
    real_write = os.write

    def fake_write(fd, data):
        if fd == 99:
            written.append(data)
            return len(data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake_write)
    runner._answer_terminal_queries(b"\x1b]10;?\x07\x1b]11;?\x07")

    assert written == []


def test_proxy_status_check_runs_after_file_event_only():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.file_change_event = threading.Event()
    runner.status_check_pending = False
    runner.last_poll = 0.0
    runner.agent_in_flight = False
    runner.agent_parse_thread = None
    runner.agent_parse_result = None
    runner.last_child_output = 0.0
    runner.last_status = ""
    runner.last_status_change = 0.0
    runner.verbose = False
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.FILE_STABLE_SECONDS = 8.0
    runner._prune_declined_untracked = lambda: None
    runner._commit_available_agent_turns = lambda quiet: False

    class Repo:
        calls = 0

        def status_short(self):
            self.calls += 1
            return " M file.txt\n"

    runner.repo = Repo()
    runner.file_change_event.set()

    runner._maybe_agent_commit()
    runner._maybe_agent_commit()

    assert runner.repo.calls == 1


def test_proxy_parse_starts_only_after_cooldown_between_file_events():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.file_change_event = threading.Event()
    runner.status_check_pending = False
    runner.parse_pending = False
    runner.last_poll = 0.0
    runner.agent_in_flight = False
    runner.agent_parse_thread = None
    runner.agent_parse_result = None
    runner.agent_parse_active = False
    runner.last_child_output = 0.0
    runner.last_status = ""
    runner.last_status_change = 0.0
    runner.last_parse_start = 0.0
    runner.last_parse_finish = 0.0
    runner.last_parse_attempt_status = ""
    runner.verbose = False
    runner.CHILD_IDLE_SECONDS = 0.0
    runner.FILE_STABLE_SECONDS = 0.0
    runner.PARSE_COOLDOWN_SECONDS = 60.0
    runner._prune_declined_untracked = lambda: None

    class Repo:
        def status_short(self):
            return " M file.txt\n"

    starts = []
    runner.repo = Repo()

    def start_parse():
        runner.last_parse_start = time.monotonic()
        runner.last_parse_finish = time.monotonic()
        starts.append(True)
        return True

    runner._start_agent_parse = start_parse

    runner.file_change_event.set()
    runner._maybe_agent_commit()
    runner.file_change_event.set()
    runner._maybe_agent_commit()

    assert len(starts) == 1


def test_proxy_parse_cooldown_starts_after_parse_finish():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.file_change_event = threading.Event()
    runner.status_check_pending = False
    runner.parse_pending = False
    runner.last_poll = 0.0
    runner.agent_in_flight = False
    runner.agent_parse_thread = None
    runner.agent_parse_result = None
    runner.agent_parse_active = False
    runner.last_child_output = 0.0
    runner.last_status = ""
    runner.last_status_change = 0.0
    runner.last_parse_start = 0.0
    runner.last_parse_finish = time.monotonic()
    runner.last_parse_attempt_status = ""
    runner.verbose = False
    runner.CHILD_IDLE_SECONDS = 0.0
    runner.FILE_STABLE_SECONDS = 0.0
    runner.PARSE_COOLDOWN_SECONDS = 60.0
    runner._prune_declined_untracked = lambda: None

    class Repo:
        def status_short(self):
            return " M file.txt\n"

    starts = []
    runner.repo = Repo()
    runner._start_agent_parse = lambda: starts.append(True) or True

    runner.file_change_event.set()
    runner._maybe_agent_commit()

    assert starts == []


def test_proxy_start_agent_parse_rejects_active_parse(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = type("Repo", (), {"repo": tmp_path})()
    runner.state = AgitState(tmp_path)
    runner.agent_parse_thread = None
    runner.agent_parse_result = None
    runner.agent_parse_active = True

    assert runner._start_agent_parse() is False


def test_proxy_sanitizes_raw_opencode_event_agent_trace(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = type("Repo", (), {"repo": tmp_path})()
    runner.state = AgitState(tmp_path)
    runner.backend = make_proxy_agent("opencode")
    runner.debug_proxy = False
    runner.state.append_trace("user", "hi")
    runner.state.append_trace(
        "agent",
        "\n".join(
            [
                '{"type":"step_start","sessionID":"ses-1","part":{"type":"step-start"}}',
                '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"Hi."}}',
            ]
        ),
    )

    runner._sanitize_state_trace()

    assert runner.state.pending_trace() == [{"role": "user", "content": "hi"}]


def test_proxy_pending_prompt_forwards_after_agent_parse_commit(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    read_fd, write_fd = os.pipe()
    try:
        runner.master_fd = write_fd
        runner.pending_forwarded = [b"\r"]
        runner.pending_prompt_text = "fix it"
        runner.passthrough_prompt = bytearray(b"fix it")
        runner.state = AgitState(tmp_path)
        runner.agent_parse_thread = None
        runner.agent_in_flight = False
        runner.message = "waiting"
        runner.message_until = 1.0
        runner._finish_agent_parse_if_ready = lambda quiet: True

        runner._resume_pending_prompt_if_ready()

        assert os.read(read_fd, 1) == b"\r"
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is True
        assert runner.state.pending_trace() == [{"role": "user", "content": "fix it"}]
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_pending_prompt_user_commit_then_forwards(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    read_fd, write_fd = os.pipe()
    try:
        runner.master_fd = write_fd
        runner.pending_forwarded = [b"\r"]
        runner.pending_prompt_text = "fix it"
        runner.passthrough_prompt = bytearray(b"fix it")
        runner.state = AgitState(tmp_path)
        runner.agent_parse_thread = None
        runner.agent_in_flight = False
        runner.screen = None
        runner.message = None
        runner.message_until = 0.0
        runner._finish_agent_parse_if_ready = lambda quiet: False
        runner._create_user_commit_popup = lambda: True

        class Actions:
            def has_pre_agent_user_changes(self):
                return True

        runner.actions = Actions()

        runner._resume_pending_prompt_if_ready()

        assert os.read(read_fd, 1) == b"\r"
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_pending_prompt_cancelled_user_commit_does_not_forward(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(read_fd, False)
        runner.master_fd = write_fd
        runner.pending_forwarded = [b"\r"]
        runner.pending_prompt_text = "fix it"
        runner.passthrough_prompt = bytearray(b"fix it")
        runner.state = AgitState(tmp_path)
        runner.agent_parse_thread = None
        runner.agent_in_flight = False
        runner.screen = None
        runner.message = None
        runner.message_until = 0.0
        runner._finish_agent_parse_if_ready = lambda quiet: False
        runner._create_user_commit_popup = lambda: False

        class Actions:
            def has_pre_agent_user_changes(self):
                return True

        runner.actions = Actions()

        runner._resume_pending_prompt_if_ready()

        try:
            written = os.read(read_fd, 1)
        except BlockingIOError:
            written = b""
        assert written == b""
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is False
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_agent_active_does_not_depend_on_recent_output():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.agent_in_flight = False
    runner.agent_parse_thread = None
    runner.last_child_output = 999999999.0

    assert runner._agent_is_active() is False

    runner.agent_in_flight = True
    assert runner._agent_is_active() is True


def test_proxy_clears_stale_agent_in_flight_when_idle():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.agent_in_flight = True
    runner.last_child_output = 0.0
    runner.CHILD_IDLE_SECONDS = 4.0

    runner._clear_agent_in_flight_if_idle()

    assert runner.agent_in_flight is False
