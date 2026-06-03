import os
import threading
import time

from agit.backends.base import TokenUsage
from agit.opencode_session import SessionTurn
from agit.proxy import ProxyInput, ProxyRunner, _escape_sequence_complete
from agit.state import AgitState


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


def test_proxy_reverse_cells_render_white_on_black():
    runner = ProxyRunner.__new__(ProxyRunner)

    class Cell:
        bold = False
        italics = False
        underscore = False
        reverse = True
        fg = "default"
        bg = "default"

    assert runner._cell_style(Cell()) == "\x1b[37;40m"


def test_proxy_render_row_only_styles_reverse_cells():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols = 3

    class Cell:
        def __init__(self, data, reverse=False, fg="white", bg="default"):
            self.data = data
            self.reverse = reverse
            self.fg = fg
            self.bg = bg

    class Screen:
        buffer = {0: {0: Cell("a"), 1: Cell("b", reverse=True), 2: Cell("c")}}

    runner.screen = Screen()

    assert runner._render_row(0) == "a\x1b[37;40mb\x1b[0mc\x1b[0m"


def test_proxy_render_row_styles_explicit_white_on_black_cells():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols = 3

    class Cell:
        def __init__(self, data, fg="default", bg="default", reverse=False):
            self.data = data
            self.fg = fg
            self.bg = bg
            self.reverse = reverse

    class Screen:
        buffer = {0: {0: Cell("a", fg="white"), 1: Cell("b", fg="white", bg="black"), 2: Cell("c", fg="brightwhite", bg="black")}}

    runner.screen = Screen()

    assert runner._render_row(0) == "a\x1b[37;40mbc\x1b[0m"


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
