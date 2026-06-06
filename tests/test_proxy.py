import os
import threading
import time

import pytest

import types

from agit.backends.base import TokenUsage
from agit.opencode_session import SessionTurn
from agit.backends.proxy_agents import make_proxy_agent
from agit.proxy import ProxyInput, ProxyRunner, _escape_sequence_complete, _humanize_age, _short_session, detect_color_mode
from agit.session import ExportedSession, SessionRef
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
        return "abc1234"  # mirror GitRepo.commit returning the new short SHA


def test_proxy_ctrl_g_enters_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07git-status\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-status"
    assert should_exit is False


def test_proxy_s_jumps_to_session():
    # Only "session" starts with "s", so s+Enter selects it directly.
    parser = ProxyInput()
    _f, _e, command, _x = parser.feed(b"\x07s\r")
    assert command == "session"


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

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07git-stat\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-status"
    assert should_exit is False


def test_proxy_arrow_selection_runs_selected_command():
    parser = ProxyInput()

    # Down-arrow from the first command (session) selects the second.
    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "agent-backend"
    assert should_exit is False


def test_proxy_tab_completes_selected_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "agent-backend"


def test_proxy_enter_runs_selected_partial_match_without_tab():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07git-stat\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-status"
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

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[<35;88;11Mgit-status\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-status"
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
    # The subject lists every prompt that led to the commit, joined by " / ".
    assert message.startswith("<agent> fix it / also handle errors")
    assert message.index("## User\n\nfix it") < message.index("## User\n\nalso handle errors")
    assert message.index("## User\n\nalso handle errors") < message.index("## Agent\n\ndone")


def test_agent_commit_subject_joins_all_prompts_with_slash(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = FakeCommitRepo()
    runner.state = AgitState(tmp_path)
    runner.verbose = False
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn("u1", "a1", "add the parser", "done one", TokenUsage(total=1, output=1), None),
            SessionTurn("u2", "a2", "now add tests", "done two", TokenUsage(total=1, output=1), None),
            SessionTurn("u3", "a3", "and fix the lint", "done three", TokenUsage(total=1, output=1), None),
        ],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        quiet=True,
    )

    assert committed is True
    subject = runner.repo.message.splitlines()[0]
    # Every prompt that led to the commit, in order, joined by " / ".
    assert subject == "<agent> add the parser / now add tests / and fix the lint"


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
    assert message.index("## User\n\nexplain only") < message.index("## Agent\n\nno code changed")
    assert message.index("## Agent\n\nno code changed") < message.index("## User\n\nnow edit")
    assert message.count("## User\n\nexplain only") == 1


def _parse_ready_runner(tmp_path, session, *, last_message_id=None):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.state = AgitState(tmp_path)
    runner.worktree = None
    runner.agent_parse_thread = None
    runner.backend = types.SimpleNamespace(name="claude")
    runner._debug = lambda *a, **k: None
    runner._note_backend_session_change = lambda sid: None
    runner._mirror_session_to_base = lambda sid: None
    runner._integrate_session_turn = lambda: None
    runner.commits = []
    runner._create_agent_commit_from_turns_popup = lambda **k: (runner.commits.append(k), True)[1]
    runner.agent_parse_result = (session.session_id, session, last_message_id)
    return runner


def test_finish_agent_parse_defers_commit_while_turn_in_progress(tmp_path):
    # The latest prompt is still being answered (last message was a tool call), so
    # the idle/file-stable debounce must NOT commit — otherwise one prompt gets
    # split into several commits (code now, tests later).
    in_progress = ExportedSession(
        session_id="ses-9",
        model="claude-opus-4-8",
        updated=None,
        turns=[SessionTurn("u1", "a1", "fix it and add tests", "Let me add a sanitizer.", TokenUsage(), None, complete=False)],
    )
    runner = _parse_ready_runner(tmp_path, in_progress)

    result = runner._finish_agent_parse_if_ready(quiet=True)

    assert result is None  # deferred, not committed
    assert runner.commits == []
    # The conversation id is still tracked while we wait, so resume stays correct.
    assert runner.state.backend_session_id == "ses-9"


def test_finish_agent_parse_commits_once_turn_is_complete(tmp_path):
    finished = ExportedSession(
        session_id="ses-9",
        model="claude-opus-4-8",
        updated=None,
        turns=[SessionTurn("u1", "a1", "fix it and add tests", "Done — code and tests are in.", TokenUsage(total=1, output=1), None, complete=True)],
    )
    runner = _parse_ready_runner(tmp_path, finished)

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert len(runner.commits) == 1


def test_finish_agent_parse_forces_in_progress_commit_on_exit(tmp_path):
    # On exit the worktree is torn down, so an unfinished turn must still be
    # committed (require_complete=False) rather than lost.
    in_progress = ExportedSession(
        session_id="ses-9",
        model="claude-opus-4-8",
        updated=None,
        turns=[SessionTurn("u1", "a1", "fix it and add tests", "Let me add a sanitizer.", TokenUsage(total=1, output=1), None, complete=False)],
    )
    runner = _parse_ready_runner(tmp_path, in_progress)

    forced = runner._finish_agent_parse_if_ready(quiet=True, integrate=False, require_complete=False)

    assert forced is True
    assert len(runner.commits) == 1


def test_finish_agent_parse_defers_for_queued_followup_not_in_transcript(tmp_path):
    # Turn 1 is complete, but the user queued a follow-up mid-turn that the backend
    # hasn't processed yet (absent from the transcript). While the agent is still
    # active, the commit must wait so the follow-up shares this commit — otherwise
    # it would land as a separate second commit.
    session = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "a1", "first prompt", "done one", TokenUsage(total=1, output=1), None, complete=True),
    ])
    runner = _parse_ready_runner(tmp_path, session)
    runner._awaited_followups = ["second prompt"]
    runner._agent_is_active = lambda: True

    assert runner._finish_agent_parse_if_ready(quiet=True) is None  # deferred
    assert runner.commits == []


def test_finish_agent_parse_commits_both_turns_once_followup_lands(tmp_path):
    session = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "a1", "first prompt", "done one", TokenUsage(total=1, output=1), None, complete=True),
        SessionTurn("u2", "a2", "second prompt", "done two", TokenUsage(total=1, output=1), None, complete=True),
    ])
    runner = _parse_ready_runner(tmp_path, session)
    runner._awaited_followups = ["second prompt"]
    runner._agent_is_active = lambda: True

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert len(runner.commits) == 1  # ONE commit covering both queued turns
    # The committed batch carried both prompts; the queue is cleared.
    assert [t.user_prompt for t in runner.commits[0]["turns"]] == ["first prompt", "second prompt"]
    assert runner._awaited_followups == []


def test_finish_agent_parse_does_not_block_on_cancelled_followup(tmp_path):
    # The queued prompt never landed and the agent has gone idle (user cancelled
    # it): commit turn 1 rather than block commits forever.
    session = ExportedSession("ses-1", "m", None, [
        SessionTurn("u1", "a1", "first prompt", "done one", TokenUsage(total=1, output=1), None, complete=True),
    ])
    runner = _parse_ready_runner(tmp_path, session)
    runner._awaited_followups = ["cancelled prompt"]
    runner._agent_is_active = lambda: False

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert runner._awaited_followups == []


def test_agent_commit_popup_includes_commit_id(tmp_path):
    # The auto-commit confirmation names the short SHA so the user can find the
    # commit aGiT just made.
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = FakeCommitRepo()
    runner.state = AgitState(tmp_path)
    runner.verbose = False
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "do the thing", "done", TokenUsage(total=1, output=1), None)],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=False,
    )

    assert committed is True
    assert runner.message == "Created <agent> commit abc1234."


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


def test_screen_erase_does_not_carry_glyph_attributes():
    # A backend that clears the screen while underline is still active (Claude's
    # session-choice picker) must not leave underlined blank cells behind — those
    # render as stray horizontal lines that linger after the view is dismissed.
    import pyte

    from agit.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 3, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Underline on, draw text, then clear the whole display — all while the
    # cursor still carries the underline + a real background colour.
    stream.feed(b"\x1b[4m\x1b[44mhi\x1b[2J")

    # The drawn cells (which carried underline) are erased to clean blanks that
    # keep only the background colour, not the underline.
    for x in (0, 1):
        cell = screen.buffer[0][x]
        assert cell.data == " "
        assert cell.underscore is False  # glyph attribute dropped on erase
        assert cell.bg == "blue"  # background-colour-erase preserved


def test_screen_erase_in_line_does_not_carry_underline():
    import pyte

    from agit.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Underline on, then erase to end of line: the blanked cells must be clean.
    stream.feed(b"\x1b[4mx\x1b[K")

    assert screen.buffer[0][0].underscore is True  # the drawn char keeps it
    for x in range(1, 6):
        assert screen.buffer[0][x].underscore is False


def test_screen_survives_private_device_status_query():
    # Claude/Ink emits \x1b[?6n (DEC-private cursor-position query) mid-redraw —
    # e.g. while collapsing an option menu after a selection. pyte's
    # report_device_status() doesn't accept the private flag and would raise
    # TypeError, aborting the feed and dropping the rest of the chunk (which
    # truncated the collapse redraw and left stale menu rows on screen). The screen
    # must absorb the query and keep applying the bytes that follow it.
    import pyte

    from agit.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[1;1HAA\x1b[?6nBB")  # private DSR sandwiched between writes

    row0 = "".join((screen.buffer[0][x].data or " ") for x in range(6)).rstrip()
    assert row0 == "AABB"  # the text after the query was NOT dropped


def test_feed_child_output_strips_xtmodkeys_mistaken_for_underline():
    # Claude toggles the xterm modifyOtherKeys keyboard mode (CSI > 4 m) when it
    # enters/leaves the session-choice picker. pyte mis-tokenises the >-private
    # sequence as the SGR \x1b[4m (underline on), which then sticks to everything
    # drawn afterwards — the stray horizontal lines. The private sequence must be
    # stripped from what is fed to pyte so no underline leaks.
    import pyte

    from agit.proxy import _BackgroundColorEraseScreen

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.screen = _BackgroundColorEraseScreen(10, 2, history=10, ratio=0.5)
    runner.stream = pyte.ByteStream(runner.screen)

    runner._feed_child_output(b"\x1b[>4mhello")

    row = runner.screen.buffer[0]
    assert "".join((row[x].data or " ") for x in range(5)) == "hello"  # not "4mhel.."
    for x in range(10):
        assert row[x].underscore is False


def test_feed_child_output_preserves_dec_private_modes_and_real_sgr():
    # The strip must be surgical: real SGR (incl. genuine underline) and DEC
    # private (?) sequences pass through untouched.
    import pyte

    from agit.proxy import _BackgroundColorEraseScreen

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.screen = _BackgroundColorEraseScreen(10, 2, history=10, ratio=0.5)
    runner.stream = pyte.ByteStream(runner.screen)

    runner._feed_child_output(b"\x1b[?25l\x1b[4mU\x1b[24mP")

    assert runner.screen.buffer[0][0].underscore is True  # genuine underline kept
    assert runner.screen.buffer[0][1].underscore is False
    assert runner.screen.cursor.hidden is True  # ?25l honoured


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


def _paint_runner():
    import types

    import pyte

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols, runner.rows = 8, 4  # 3 visible content rows + 1 status row
    runner.scroll_back = 0
    runner.sel_active = False
    runner.sel_anchor = runner.sel_point = None
    runner.screen = pyte.HistoryScreen(8, 3, history=50, ratio=0.5)
    runner.stream = pyte.ByteStream(runner.screen)
    runner._in_sync_update = False
    runner._sync_since = 0.0
    runner.message = None
    runner.message_until = 0.0
    runner.input = types.SimpleNamespace(capturing=False)
    runner._status_line = lambda: "STATUS".ljust(8)
    return runner


def test_render_wraps_frame_in_synchronized_update(monkeypatch):
    runner = _paint_runner()
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or len(data))

    runner.stream.feed(b"\x1b[Hr0\r\nr1\r\nr2")
    runner._render()
    assert len(writes) == 1
    out = writes[0].decode()
    # The whole repaint is one atomic synchronized update (no tearing/flicker).
    assert out.startswith("\x1b[?2026h") and out.endswith("\x1b[?2026l")
    assert out.count("\x1b[?2026h") == 1 and out.count("\x1b[?2026l") == 1
    # It is a full repaint: every visible row plus the status line is present.
    assert "r0" in out and "r1" in out and "r2" in out and "STATUS" in out


def test_sticky_message_renders_after_timeout(monkeypatch):
    runner = _paint_runner()
    runner.cols = 60  # wide enough that the popup text isn't truncated
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or len(data))

    runner._set_message("Created <agent> commit.", sticky=True)
    runner.message_until = time.monotonic() - 100  # the timeout passed long ago

    runner._render()

    # A sticky message stays up past its timeout (until the next keypress).
    assert "Created <agent> commit." in writes[0].decode()


def test_nonsticky_message_hidden_after_timeout(monkeypatch):
    runner = _paint_runner()
    runner.cols = 60
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or len(data))

    runner.message = "transient note"
    runner.message_until = time.monotonic() - 100
    runner._message_sticky = False

    runner._render()

    assert "transient note" not in writes[0].decode()


def test_keypress_dismisses_sticky_message():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._set_message("Created <agent> commit.", sticky=True)
    assert runner._message_sticky is True

    assert runner._clear_sticky_message_on_input() is True
    assert runner.message is None
    assert runner._message_sticky is False
    # A following keypress has nothing sticky to clear.
    assert runner._clear_sticky_message_on_input() is False


def test_keypress_leaves_nonsticky_message_intact():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._set_message("transient note")  # default: not sticky

    assert runner._clear_sticky_message_on_input() is False
    assert runner.message == "transient note"


def test_set_message_requests_a_render():
    # The render loop only paints when _render_pending is set (or on child
    # output). A message set from the background idle loop — e.g. the auto-commit
    # confirmation, when the agent is quiet — must therefore request a repaint, or
    # the popup is never drawn.
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._render_pending = False

    runner._set_message("Created <agent> commit.", sticky=True)

    assert runner._render_pending is True


def test_track_sync_update_defers_then_releases_render():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._in_sync_update = False
    runner._sync_since = 0.0
    runner._render_pending = False
    runner._last_render = 0.0
    rendered = []
    runner._render = lambda: rendered.append(1)

    # Begin-sync with no matching end: aGiT is mid-update and must defer.
    runner._track_sync_update(b"\x1b[?2026h")
    assert runner._in_sync_update is True
    runner._render_output()
    assert rendered == [] and runner._render_pending is True

    # End-sync releases the hold; the next output paints.
    runner._track_sync_update(b"\x1b[?2026l")
    assert runner._in_sync_update is False
    runner._render_output()
    assert rendered == [1]

    # A begin+end inside one chunk ends not-in-update.
    runner._track_sync_update(b"\x1b[?2026hABC\x1b[?2026l")
    assert runner._in_sync_update is False


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


def test_apply_timings_overrides_constants():
    from agit.global_config import DEFAULT_TIMINGS

    runner = ProxyRunner.__new__(ProxyRunner)
    # Defaults are the class constants until config is applied.
    assert runner.BASE_POLL_SECONDS == DEFAULT_TIMINGS["base_poll_seconds"]

    custom = dict(DEFAULT_TIMINGS, base_poll_seconds=30.0, child_idle_seconds=1.5)
    runner._apply_timings(custom)

    assert runner.BASE_POLL_SECONDS == 30.0
    assert runner.CHILD_IDLE_SECONDS == 1.5
    assert runner.POLL_SECONDS == DEFAULT_TIMINGS["background_poll_seconds"]


def test_proxy_refuses_second_instance(monkeypatch, capsys):
    import sys

    runner = ProxyRunner.__new__(ProxyRunner)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    runner._ensure_backend_available = lambda: True
    # A live aGiT (PID 4321) already holds the lock: acquire fails.
    runner.management_lock = type("L", (), {"acquire": lambda self: False, "owner_pid": lambda self: 4321})()

    assert runner.run() == 1
    out = capsys.readouterr().out
    assert "already running" in out and "4321" in out  # names the holding process


def _mux_runner():
    import agit.session_runtime as sr
    from agit.session_runtime import capture_session

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols, runner.rows = 20, 5
    runner.color_mode = "truecolor"
    runner.host_fg_value = runner.host_bg_value = runner.host_da = None
    runner.host_palette = {}
    runner._render = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None
    runner._set_message = lambda *a, **k: None
    runner._stop_file_watcher = lambda: None
    for field, value in sr.default_session_fields().items():
        setattr(runner, field, value)
    runner.name, runner.worktree = "A", None
    runner.repo, runner.state, runner.backend, runner.actions = "repoA", "stateA", "bA", "actA"
    runner.sessions = [capture_session(runner)]
    runner.active_index = 0
    return runner


def _bg_session(name):
    import agit.session_runtime as sr

    return sr.Session(**{**sr.default_session_fields(), "name": name, "repo": f"repo{name}",
                         "state": f"state{name}", "backend": f"b{name}", "actions": f"act{name}"})


def test_switch_active_swaps_session_state():
    runner = _mux_runner()
    runner.sessions.append(_bg_session("B"))

    runner._switch_active(1)
    assert runner.active_index == 1 and runner.repo == "repoB"
    assert runner.sessions[0].repo == "repoA"  # A's state was preserved

    runner._switch_active(0)
    assert runner.repo == "repoA"
    assert runner.sessions[1].repo == "repoB"


def test_background_fds_excludes_active_session():
    runner = _mux_runner()
    b = _bg_session("B")
    b.master_fd = 20
    runner.sessions.append(b)
    runner.sessions[0].master_fd = 10  # active snapshot fd (handled separately)
    assert runner._background_fds() == {20: b}


def test_pump_background_feeds_screen_without_disturbing_active():
    import os
    import pyte

    runner = _mux_runner()
    b = _bg_session("B")
    read_fd, write_fd = os.pipe()
    b.master_fd = read_fd
    b.screen = pyte.HistoryScreen(20, 4, history=10, ratio=0.5)
    b.stream = pyte.ByteStream(b.screen)
    runner.sessions.append(b)
    try:
        os.write(write_fd, b"hello-bg")
        runner._pump_background(b)
        row0 = "".join((b.screen.buffer[0].get(x).data if b.screen.buffer[0].get(x) else " ") for x in range(8))
        assert row0 == "hello-bg"
        assert runner.repo == "repoA"  # active session untouched
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_baseline_drops_session_with_no_conversation(tmp_path):
    from types import SimpleNamespace

    from agit.session import ExportedSession

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "ses-empty"
    runner.repo = SimpleNamespace(repo=tmp_path)
    runner._should_continue_session = lambda: True
    # A session that exists but has no turns must not be resumed.
    runner.backend = SimpleNamespace(export_session=lambda repo, sid: ExportedSession(sid, None, None, []))

    runner._initialize_session_baseline()

    assert runner.state.backend_session_id is None
    assert runner.state.last_backend_message_id is None


def test_new_session_flag_clears_backend_session_and_mints_agit_id(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._force_new_session = True
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "old-session"
    old_agit = runner.state.session_id

    runner._apply_new_session_if_requested()

    assert runner.state.backend_session_id is None
    assert runner.state.session_id != old_agit


def test_status_line_shows_base_branch(tmp_path):
    import subprocess

    from agit.git import GitRepo
    from agit.state import AgitState

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = GitRepo(tmp_path)
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "abcdef123456"
    runner.name = "session-1"
    runner.backend = type("B", (), {"name": "claude"})()
    runner._base_branch = "main"
    runner.worktree = object()
    runner.scroll_back = 0
    runner.cols = 120

    line = runner._status_line()
    assert "session-1" in line
    assert "→ main" in line  # the branch this session's work merges into


def test_inject_prompt_defers_enter_until_text_settles():
    import os
    import time

    read_fd, write_fd = os.pipe()
    try:
        runner = ProxyRunner.__new__(ProxyRunner)
        runner.master_fd = write_fd
        runner.merge_ctx = {"prompt_sent_at": None}
        runner._pending_enter_at = None

        runner._inject_prompt("resolve the\nconflict   now")
        # The text is typed immediately, collapsed to a single line, with NO
        # trailing carriage return (that would submit mid-paste).
        typed = os.read(read_fd, 4096)
        assert typed == b"resolve the conflict now"
        assert runner._pending_enter_at is not None
        assert runner.merge_ctx["prompt_sent_at"] is None  # not submitted yet

        # Too early: the Enter is still pending.
        runner._flush_pending_enter()
        assert runner._pending_enter_at is not None

        # Once the settle delay elapses, the Enter is sent as its own keystroke.
        runner._pending_enter_at = time.monotonic() - 0.01
        runner._flush_pending_enter()
        assert os.read(read_fd, 16) == b"\r"
        assert runner._pending_enter_at is None
        assert runner.merge_ctx["prompt_sent_at"] is not None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_backend_session_change_warns_once(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.worktree = object()  # a worktree session
    runner._warned_backend_session = False
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "old"
    messages = []
    runner._set_message = lambda message, **kw: messages.append(message)

    runner._note_backend_session_change("new")
    assert messages and "separate branch" in messages[0].lower()
    assert runner._warned_backend_session is True

    # It only warns once, not on every subsequent change.
    messages.clear()
    runner.state.backend_session_id = "new"
    runner._note_backend_session_change("newer")
    assert messages == []


def test_new_session_not_applied_without_flag(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._force_new_session = False
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "keep-this"
    runner._apply_new_session_if_requested()
    assert runner.state.backend_session_id == "keep-this"


def test_finalize_pending_work_commits_non_interactively():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.agent_parse_thread = None
    runner.sessions = []
    runner.active_index = 0
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner._start_agent_parse = lambda: False
    calls = []
    runner._finish_agent_parse_if_ready = lambda **k: (calls.append(k), False)[1]

    runner._finalize_pending_work()

    assert calls, "finalize should attempt to commit the latest turn"
    # Exit must never block on the untracked-files prompt.
    assert all(call.get("prompt_untracked") is False for call in calls)


def test_confirm_exit_prompts_when_managing(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
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


def test_proxy_pending_prompt_forwards_when_agent_mid_turn(tmp_path):
    # The parse deferred (latest turn still in progress) so its result was
    # consumed and _finish_agent_parse_if_ready now returns None with no live
    # thread. The follow-up must be forwarded (queued by the backend), not held
    # behind the "checking" message forever.
    runner = ProxyRunner.__new__(ProxyRunner)
    read_fd, write_fd = os.pipe()
    try:
        runner.master_fd = write_fd
        runner.pending_forwarded = [b"\r"]
        runner.pending_prompt_text = "and also rename it"
        runner.passthrough_prompt = bytearray(b"and also rename it")
        runner.state = AgitState(tmp_path)
        runner.agent_parse_thread = None
        runner.agent_in_flight = False
        runner.message = None
        runner.message_until = 0.0
        runner._finish_agent_parse_if_ready = lambda quiet: None
        runner._ensure_turn_branch = lambda: None

        runner._resume_pending_prompt_if_ready()

        assert os.read(read_fd, 1) == b"\r"
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is True
        assert runner.state.pending_trace() == [{"role": "user", "content": "and also rename it"}]
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


def _integration_runner(merge_ok):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.worktree = object()
    runner._base_branch = "main"
    runner.merge_ctx = None
    runner.name = "session-1"
    runner._exiting = False
    runner._debug = lambda *a, **k: None

    class FakeRepo:
        def __init__(self):
            self.aborted = False

        def current_branch(self):
            return "agit/session-1/t1"

        def merge(self, ref):
            return merge_ok

        def merge_abort(self):
            self.aborted = True

    runner.repo = FakeRepo()
    return runner


def test_integrate_conflict_aborts_and_prompts_resolve_options():
    runner = _integration_runner(merge_ok=False)
    calls = []
    runner._prompt_resolve_conflict = lambda src: calls.append(src)
    runner._advance_base_to = lambda src: calls.append(("advance", src))

    runner._integrate_session_turn()

    assert runner.repo.aborted is True  # conflicted merge is backed out
    assert calls == ["agit/session-1/t1"]  # options box is surfaced


def test_integrate_clean_merge_advances_base_without_prompt():
    runner = _integration_runner(merge_ok=True)
    calls = []
    runner._prompt_resolve_conflict = lambda src: calls.append(("prompt", src))
    runner._advance_base_to = lambda src: calls.append(("advance", src))

    runner._integrate_session_turn()

    assert runner.repo.aborted is False
    assert calls == [("advance", "agit/session-1/t1")]


def test_integrate_conflict_on_exit_leaves_for_startup():
    runner = _integration_runner(merge_ok=False)
    runner._exiting = True
    prompted = []
    runner._prompt_resolve_conflict = lambda src: prompted.append(src)
    runner._advance_base_to = lambda src: None

    runner._integrate_session_turn()

    assert runner.repo.aborted is True  # merge is aborted
    assert prompted == []  # but no UI on exit


def _resolve_runner(choice_index):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.name = "session-1"
    runner._base_branch = "main"
    runner.backend = types.SimpleNamespace(name="opencode")
    runner._render = lambda: None
    msgs = []
    runner._set_message = lambda *a, **k: msgs.append(a[0] if a else "")
    runner._messages = msgs
    runner._select_popup = lambda title, options: options[choice_index]
    runner._dispatched = []
    runner._start_merge_for_active = lambda *, auto: runner._dispatched.append(auto)
    return runner


def test_prompt_resolve_conflict_dispatches_auto():
    runner = _resolve_runner(choice_index=0)
    runner._prompt_resolve_conflict("agit/session-1/t1")
    assert runner._dispatched == [True]


def test_prompt_resolve_conflict_dispatches_manual():
    runner = _resolve_runner(choice_index=1)
    runner._prompt_resolve_conflict("agit/session-1/t1")
    assert runner._dispatched == [False]


def test_prompt_resolve_conflict_leave_does_not_merge():
    runner = _resolve_runner(choice_index=2)
    runner._prompt_resolve_conflict("agit/session-1/t1")
    assert runner._dispatched == []  # nothing merged
    assert runner._messages and "unintegrated" in runner._messages[0]


# --- cross-backend sessions (Part B) ---

def test_live_session_for_backend_finds_active_and_background():
    import types

    runner = _mux_runner()
    runner.backend = types.SimpleNamespace(name="claude")  # active
    b = _bg_session("B")
    b.backend = types.SimpleNamespace(name="opencode")
    runner.sessions.append(b)

    assert runner._live_session_for_backend("claude") == 0
    assert runner._live_session_for_backend("opencode") == 1
    assert runner._live_session_for_backend("gemini") is None


def _backend_switch_runner(monkeypatch):
    import types

    runner = _mux_runner()
    runner.backend = types.SimpleNamespace(name="claude")
    runner.worktree = object()
    runner.global_config = types.SimpleNamespace(default_backend="claude")
    monkeypatch.setattr("agit.proxy.backend_installed", lambda n: True)
    return runner


def test_switch_backend_switches_to_live_session_without_teardown(monkeypatch):
    import types

    runner = _backend_switch_runner(monkeypatch)
    b = _bg_session("B")
    b.backend = types.SimpleNamespace(name="opencode")
    runner.sessions.append(b)
    calls = []
    runner._switch_active = lambda i: calls.append(("switch", i))
    runner._new_session = lambda *a, **k: calls.append(("new", a, k))

    runner._switch_backend("opencode")

    assert calls == [("switch", 1)]  # resumed the live opencode session, no respawn


def test_switch_backend_creates_per_backend_session_when_none_live(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._next_session_name = lambda: "session-2"
    prompts = []

    def fake_prompt(title, prompt, *, default=""):
        prompts.append((title, default))
        return default  # user accepts the prefilled name

    runner._prompt_popup = fake_prompt
    calls = []
    runner._new_session = lambda name, **k: calls.append((name, k))
    runner._switch_active = lambda i: calls.append(("switch", i))

    runner._switch_backend("opencode")

    # The name popup is prefilled with the next session-N; the backend is pinned.
    assert prompts == [("New opencode session", "session-2")]
    assert calls == [("session-2", {"backend": "opencode"})]


def test_switch_backend_uses_edited_session_name(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._next_session_name = lambda: "session-2"
    runner._prompt_popup = lambda title, prompt, *, default="": "my-feature"  # user renames
    calls = []
    runner._new_session = lambda name, **k: calls.append((name, k))
    runner._switch_active = lambda i: calls.append(("switch", i))

    runner._switch_backend("opencode")

    assert calls == [("my-feature", {"backend": "opencode"})]


def test_switch_backend_cancelled_name_does_not_create(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._next_session_name = lambda: "session-2"
    runner._prompt_popup = lambda *a, **k: None  # user cancels the name popup
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    created = []
    runner._new_session = lambda *a, **k: created.append((a, k))

    runner._switch_backend("opencode")

    assert created == []


def test_switch_backend_noop_when_same_backend(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    msgs = []
    runner._set_message = lambda *a, **k: msgs.append(a[0])
    runner._render = lambda: None
    called = []
    runner._switch_active = lambda i: called.append(i)
    runner._new_session = lambda *a, **k: called.append("new")

    runner._switch_backend("claude")

    assert called == [] and any("Already using" in m for m in msgs)


def test_service_background_integrates_idle_session_cleanly():
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    b = _bg_session("B")
    b.agent_in_flight = True
    b.last_child_output = 0.0  # long idle
    b.last_poll = 0.0
    runner.sessions.append(b)
    calls = []
    runner._with_session = lambda session, fn: calls.append(session.name) or "integrated"
    runner._switch_active = lambda i: calls.append(("switch", i))
    runner._prompt_resolve_conflict = lambda src: calls.append(("prompt", src))

    runner._service_background_sessions()

    assert calls == ["B"]  # serviced once, clean integration, no switch/prompt
    assert b.last_poll > 0.0  # throttle timestamp advanced


def test_service_background_integrates_even_when_not_in_flight():
    # Regression: a finished background session whose in-flight flag was already
    # cleared (e.g. it committed but didn't integrate) must still be serviced —
    # otherwise its commits sit unintegrated until the user switches to it.
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    b = _bg_session("B")
    b.agent_in_flight = False  # no longer flagged, but still has work to integrate
    b.last_child_output = 0.0
    b.last_poll = 0.0
    runner.sessions.append(b)
    serviced = []
    runner._with_session = lambda session, fn: serviced.append(session.name) or "integrated"
    runner._switch_active = lambda i: serviced.append(("switch", i))
    runner._prompt_resolve_conflict = lambda src: serviced.append(("prompt", src))

    runner._service_background_sessions()

    assert serviced == ["B"]


def test_service_background_conflict_switches_and_prompts():
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    b = _bg_session("B")
    b.agent_in_flight = True
    b.last_child_output = 0.0
    b.last_poll = 0.0
    runner.sessions.append(b)
    runner._with_session = lambda session, fn: "conflict"
    switched, prompted = [], []

    class _Repo:
        def current_branch(self):
            return "agit/B/t1"

    def _switch(i):
        switched.append(i)
        runner.repo = _Repo()

    runner._switch_active = _switch
    runner._prompt_resolve_conflict = lambda src: prompted.append(src)

    runner._service_background_sessions()

    assert switched == [1] and prompted == ["agit/B/t1"]


def test_service_background_finalizes_pending_merge():
    runner = _mux_runner()
    runner.merge_ctx = None
    b = _bg_session("B")
    b.merge_ctx = {"source_branch": "agit/B/t1"}
    runner.sessions.append(b)
    called = []
    runner._with_session = lambda session, fn: called.append((session.name, fn.__name__))

    runner._service_background_sessions()

    assert called == [("B", "_maybe_complete_agent_merge")]


def test_service_background_skips_while_active_merge_in_progress():
    runner = _mux_runner()
    runner.merge_ctx = {"busy": 1}
    b = _bg_session("B")
    b.agent_in_flight = True
    runner.sessions.append(b)
    called = []
    runner._with_session = lambda *a, **k: called.append(1)

    runner._service_background_sessions()

    assert called == []


def test_with_session_swaps_in_and_restores_active():
    runner = _mux_runner()  # active session name "A"
    b = _bg_session("B")
    runner.sessions.append(b)
    seen = []

    def fn():
        seen.append(runner.name)  # "B" while swapped in
        runner.last_status = "touched"  # mutation must persist to the snapshot
        return "ok"

    result = runner._with_session(b, fn)

    assert result == "ok"
    assert seen == ["B"]
    assert runner.name == "A"  # active restored
    assert b.last_status == "touched"  # background snapshot updated


def test_next_session_name_skips_existing_worktrees_and_sessions():
    import types

    runner = _mux_runner()  # one active session named "A"
    runner.worktree_manager = types.SimpleNamespace(
        list=lambda: [types.SimpleNamespace(name="session-1"), types.SimpleNamespace(name="session-2")]
    )
    # session-1 and session-2 are taken (plus active "A"), so the next is session-3.
    assert runner._next_session_name() == "session-3"


# --- injected-prompt targeting (cross-backend safety) ---

def test_inject_prompt_records_target_fd(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.master_fd = 5
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append((fd, data)) or len(data))

    runner._inject_prompt("resolve the conflict")

    assert writes == [(5, b"resolve the conflict")]
    assert runner._pending_enter_fd == 5
    assert runner._pending_enter_at is not None


def test_flush_pending_enter_targets_injected_fd_not_active(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._pending_enter_at = 0.0  # already due
    runner._pending_enter_fd = 7  # the backend the prompt was typed into
    runner.master_fd = 99  # a *different* session became active in the meantime
    runner.merge_ctx = None
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append((fd, data)) or len(data))

    runner._flush_pending_enter()

    # The submit Enter goes to the injected backend (7), never the active one (99).
    assert writes == [(7, b"\r")]
    assert runner._pending_enter_fd is None


def test_flush_pending_enter_marks_sent_only_when_still_active(monkeypatch):
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))

    # Same session still active -> prompt_sent_at is recorded.
    active = ProxyRunner.__new__(ProxyRunner)
    active._pending_enter_at, active._pending_enter_fd, active.master_fd = 0.0, 7, 7
    active.merge_ctx = {"prompt_sent_at": None}
    active._flush_pending_enter()
    assert active.merge_ctx["prompt_sent_at"] is not None

    # Switched away -> the active session's merge_ctx is NOT marked.
    switched = ProxyRunner.__new__(ProxyRunner)
    switched._pending_enter_at, switched._pending_enter_fd, switched.master_fd = 0.0, 7, 99
    switched.merge_ctx = {"prompt_sent_at": None}
    switched._flush_pending_enter()
    assert switched.merge_ctx["prompt_sent_at"] is None


# --- session name uniqueness + per-backend resume ---

def test_state_remember_and_recall_session(tmp_path):
    s = AgitState(tmp_path)
    assert s.recall_session("opencode") is None
    s.remember_session("opencode", session_id="abc", worktree="session-2", message_id="m1", model="o4")
    assert s.recall_session("opencode") == {"id": "abc", "worktree": "session-2", "message_id": "m1", "model": "o4"}
    # Survives a reload from disk.
    assert AgitState(tmp_path).recall_session("opencode")["id"] == "abc"
    # Clearing (no id) forgets it.
    s.remember_session("opencode", session_id=None, worktree="session-2")
    assert s.recall_session("opencode") is None


def test_session_name_taken_detects_live_and_on_disk():
    import types

    runner = _mux_runner()  # one live session named "A"
    runner.worktree_manager = types.SimpleNamespace(list=lambda: [types.SimpleNamespace(name="session-1")])
    assert runner._session_name_taken("A") is True
    assert runner._session_name_taken("session-1") is True
    assert runner._session_name_taken("fresh") is False


def test_prompt_session_name_rejects_duplicates_until_unique():
    import types

    runner = _mux_runner()  # live session "A"
    runner.worktree_manager = types.SimpleNamespace(list=lambda: [])
    runner._next_session_name = lambda: "session-9"
    answers = iter(["A", "my-session"])  # first a duplicate, then a fresh name
    runner._prompt_popup = lambda title, prompt, *, default="": next(answers)
    assert runner._prompt_session_name("New Session", default="session-2") == "my-session"


def test_prompt_session_name_cancel_returns_none():
    import types

    runner = _mux_runner()
    runner.worktree_manager = types.SimpleNamespace(list=lambda: [])
    runner._prompt_popup = lambda *a, **k: None
    assert runner._prompt_session_name("New Session", default="session-2") is None


def test_switch_backend_resumes_stored_session(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._recall_backend_session = lambda name: {"id": "sess-9", "worktree": "session-2"}
    calls = []
    runner._new_session = lambda name, **k: calls.append((name, k))
    runner._switch_active = lambda i: calls.append(("switch", i))

    runner._switch_backend("opencode")

    # Resumes the remembered conversation in its original worktree, no name prompt.
    assert calls == [("session-2", {"backend": "opencode", "resume_session_id": "sess-9"})]


# --- resume-past-conversation naming ---

def _resume_runner():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.name = "session-1"
    runner.active_index = 0
    s0 = types.SimpleNamespace(name="session-1", state=types.SimpleNamespace(backend_session_id="live-1"))
    runner.sessions = [s0]
    runner._switch_active = lambda i: runner.__dict__.setdefault("_switched", []).append(i)
    runner._new_session = lambda name, **k: runner.__dict__.setdefault("_created", []).append((name, k))
    return runner


def test_resume_uses_original_worktree_when_name_is_free():
    runner = _resume_runner()
    runner._next_session_name = lambda: "session-9"

    runner._resume_conversation("session-2", "past-xyz")

    # "session-2" is not a live session, so resume in its original worktree.
    assert runner.__dict__.get("_created") == [("session-2", {"resume_session_id": "past-xyz"})]


def test_resume_uses_fresh_name_when_colliding_with_live_session():
    runner = _resume_runner()
    runner._next_session_name = lambda: "session-3"

    # A past conversation that ran in "session-1" — but session-1 is live now.
    runner._resume_conversation("session-1", "past-xyz")

    assert runner.__dict__.get("_created") == [("session-3", {"resume_session_id": "past-xyz"})]


def test_resume_switches_to_already_live_conversation():
    runner = _resume_runner()

    runner._resume_conversation("session-1", "live-1")  # same id as the live session

    assert runner.__dict__.get("_switched") == [0]
    assert "_created" not in runner.__dict__


# --- base branch switched out-of-band ---

def _base_drift_runner(current_branch):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner._base_branch = "dev"
    runner.tracking_enabled = True
    runner._integration_paused = False
    runner._base_drift_check_at = 0.0
    runner.base_repo = types.SimpleNamespace(current_branch=lambda: current_branch)
    runner._debug = lambda *a, **k: None
    runner.messages = []
    runner._set_message = lambda m, **k: runner.messages.append(m)
    runner._render = lambda: None
    return runner


def test_base_branch_drift_pauses_then_resumes():
    runner = _base_drift_runner("feature-x")
    runner._check_base_branch_drift()
    assert runner._integration_paused is True
    assert any("PAUSED" in m and "feature-x" in m for m in runner.messages)

    runner.base_repo.current_branch = lambda: "dev"  # user switches back
    runner._base_drift_check_at = 0.0                 # bypass the 2s throttle
    runner.messages.clear()
    runner._check_base_branch_drift()
    assert runner._integration_paused is False
    assert any("resumed" in m for m in runner.messages)


def test_integrate_turn_skips_while_paused():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.worktree = types.SimpleNamespace()
    runner._base_branch = "dev"
    runner.merge_ctx = None
    runner._integration_paused = True
    assert runner._integrate_turn_or_conflict() == "skip"


def test_advance_base_refuses_when_base_switched_out_of_band():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner._base_branch = "dev"
    merged = []
    runner.base_repo = types.SimpleNamespace(
        current_branch=lambda: "feature-x",            # drifted off the base branch
        merge_ff_only=lambda ref: merged.append(ref),
    )
    with pytest.raises(RuntimeError):
        runner._advance_base_to("agit/claude/session-1/t1")
    assert merged == []  # never fast-forwarded the wrong branch


def _exit_removal_runner(*, log_range_result="", rev_parse_raises=False):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner._base_branch = "dev"
    runner.merge_ctx = None
    runner._primary_worktree_name = None
    runner.worktree = types.SimpleNamespace(name="session-1")
    runner.repo = types.SimpleNamespace(
        current_branch=lambda: "agit/claude/session-1/t1",
        merge_in_progress=lambda: False,
        has_changes=lambda: False,
    )

    def _rev_parse(ref):
        if rev_parse_raises:
            raise RuntimeError("unknown revision")
        return "abc123"

    runner.base_repo = types.SimpleNamespace(rev_parse=_rev_parse,
                                             log_range=lambda base, head: log_range_result)
    runner.removed = []
    runner._worktrees = lambda: types.SimpleNamespace(remove=lambda name: runner.removed.append(name))
    runner._remember_session_for_backend = lambda: None
    runner._persist_last_session_record = lambda: None
    runner._terminate_child = lambda: None
    runner._debug = lambda *a, **k: None
    return runner


def test_exit_keeps_worktree_with_unintegrated_commits():
    runner = _exit_removal_runner(log_range_result="deadbeef a commit")
    runner._remove_worktree_on_exit()
    assert runner.removed == []  # branch ahead of base (e.g. merging was paused) → preserved


def test_exit_keeps_worktree_when_base_ref_unresolvable():
    runner = _exit_removal_runner(rev_parse_raises=True)
    runner._remove_worktree_on_exit()
    assert runner.removed == []  # base branch deleted/renamed → can't confirm merged → preserved


def test_exit_removes_fully_merged_worktree():
    runner = _exit_removal_runner(log_range_result="")  # nothing ahead of base
    runner._remove_worktree_on_exit()
    assert runner.removed == ["session-1"]  # normal cleanup of a merged session still happens


def test_exit_persists_resume_pointer_even_when_worktree_kept():
    # A primary session that exits with unintegrated work keeps its worktree, but
    # its resume pointer (which adopts a backend-native session switch) must still
    # be persisted. Gating that behind worktree removal caused the off-by-one:
    # next start resumes a stale conversation, and only the start after that lands
    # on the one the user switched to.
    runner = _exit_removal_runner(log_range_result="deadbeef still ahead")
    runner._primary_worktree_name = "session-1"
    persisted = []
    runner._persist_last_session_record = lambda: persisted.append(True)

    runner._remove_worktree_on_exit()

    assert runner.removed == []  # unintegrated → worktree still kept
    assert persisted == [True]   # ...but the resume pointer was persisted anyway


def test_exit_does_not_persist_resume_pointer_for_background_session():
    # Only the primary session owns the durable resume pointer; a non-primary
    # (background) session must not overwrite it on exit.
    runner = _exit_removal_runner(log_range_result="deadbeef still ahead")
    runner._primary_worktree_name = "session-2"  # this session ("session-1") is not primary
    persisted = []
    runner._persist_last_session_record = lambda: persisted.append(True)

    runner._remove_worktree_on_exit()

    assert persisted == []


def _bg_confirm_runner(statuses, active_index=0):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.sessions = [object()] * len(statuses)
    runner.active_index = active_index
    runner._session_status = lambda i: statuses[i]
    runner._session_name = lambda i: f"session-{i}"
    return runner


def test_confirm_terminate_background_sessions_no_prompt_when_all_idle():
    runner = _bg_confirm_runner(["running", "idle", "idle"])  # active(0) running, bg idle
    popups = []
    runner._select_popup = lambda *a, **k: popups.append(a) or "unused"
    # The active session running in the foreground never triggers the prompt.
    assert runner._confirm_terminate_background_sessions() is True
    assert popups == []


def test_confirm_terminate_background_sessions_prompts_and_names_them():
    runner = _bg_confirm_runner(["idle", "running", "idle"])  # background session-1 running
    captured = {}

    def fake_popup(title, options):
        captured["title"], captured["options"] = title, options
        return "Yes, terminate them and exit"

    runner._select_popup = fake_popup
    assert runner._confirm_terminate_background_sessions() is True
    assert "still running" in captured["title"]
    assert "session-1" in captured["title"]

    # Declining the second confirmation keeps working (cancels exit).
    runner._select_popup = lambda *a, **k: "No, keep working"
    assert runner._confirm_terminate_background_sessions() is False


def test_sync_idle_worktrees_skipped_while_paused():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._integration_paused = True
    runner.sessions = ["would-explode-if-iterated"]  # not a real session; must not be touched
    runner.active_index = 0
    runner._sync_idle_worktrees_to_base()  # returns early; no AttributeError


# --- corrupted-worktree reuse / diagnostics ---

def test_cleanup_stale_state_removes_orphaned_worktree_dirs(tmp_path):
    import types

    root = tmp_path / ".agit" / "worktrees"
    registered = root / "session-1"
    registered.mkdir(parents=True)
    orphan = root / "session-2"
    (orphan / ".agit").mkdir(parents=True)         # only .agit/ → not a valid worktree
    (root / "stray-file").write_text("x")           # a file, not a dir → ignored

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.tracking_enabled = True
    runner._debug = lambda *a, **k: None
    prunes = []
    runner.base_repo = types.SimpleNamespace(worktree_prune=lambda: prunes.append(1))
    runner.worktree_manager = types.SimpleNamespace(
        root=root,
        list=lambda: [types.SimpleNamespace(path=registered)],  # session-1 is registered
    )
    runner._worktrees = lambda: runner.worktree_manager

    runner._cleanup_stale_state_on_startup()

    assert registered.exists()         # a real registered worktree is kept
    assert not orphan.exists()         # the orphaned .agit/-only dir is swept
    assert (root / "stray-file").exists()
    assert prunes                      # pruned stale git registrations


def test_cleanup_stale_state_noop_when_read_only():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.tracking_enabled = False
    pruned = []
    runner.base_repo = types.SimpleNamespace(worktree_prune=lambda: pruned.append(1))
    runner._cleanup_stale_state_on_startup()
    assert pruned == []  # a read-only observer touches nothing


def test_is_valid_worktree_rejects_leftover_without_git(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    leftover = tmp_path / "session-1"
    (leftover / ".agit").mkdir(parents=True)  # only .agit/, no .git → invalid
    assert runner._is_valid_worktree(leftover) is False


def test_open_session_worktree_recreates_corrupted_leftover(tmp_path):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner._debug = lambda *a, **k: None
    runner._base_branch = "dev"
    leftover = tmp_path / "session-1"
    (leftover / ".agit").mkdir(parents=True)  # corrupted leftover
    created = {}

    def _create(name, *, base):
        created["called"] = (name, base)
        (tmp_path / name / ".git").parent.mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(name=name, path=tmp_path / name, branch="")

    runner.worktree_manager = types.SimpleNamespace(
        worktree_path=lambda name: tmp_path / name, create=_create)
    runner._worktrees = lambda: runner.worktree_manager
    import agit.proxy as proxymod
    orig = proxymod.GitRepo
    proxymod.GitRepo = lambda path: types.SimpleNamespace(current_branch=lambda: "")
    try:
        runner._open_session_worktree("session-1")
    finally:
        proxymod.GitRepo = orig

    assert created["called"] == ("session-1", "dev")  # recreated, not reused
    assert not (leftover / ".agit").exists()           # corrupted leftover was cleared first


def test_diag_path_uses_base_repo(tmp_path):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = types.SimpleNamespace(repo=tmp_path / "base")
    runner.repo = types.SimpleNamespace(repo=tmp_path / "base" / ".agit" / "worktrees" / "session-1")
    runner._diag_run = "20260101-000000"
    path = runner._diag_path("proxy-raw")
    # Lands in the *base* .agit/, not the ephemeral worktree's.
    assert path == tmp_path / "base" / ".agit" / "proxy-raw-20260101-000000.log"


# --- resume cwd drift guard ---

def _drift_runner(recorded_cwd, worktree_path):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.worktree = types.SimpleNamespace(name="session-1")
    runner.repo = types.SimpleNamespace(repo=worktree_path)
    runner.state = types.SimpleNamespace(backend_session_id="sess-1")
    runner.backend = types.SimpleNamespace(recorded_working_dir=lambda sid: recorded_cwd)
    runner._debug = lambda *a, **k: None
    runner._cwd_check_at = 0.0
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    runner._render = lambda: None
    return runner


def test_cwd_drift_warns_when_backend_left_the_worktree():
    runner = _drift_runner("/somewhere/else", "/repo/.agit/worktrees/session-1")
    runner._warn_if_cwd_drifted()
    assert runner.messages and "#58591" in runner.messages[0]
    assert runner._cwd_drift_checked is True
    # Warns once, then stops.
    runner.messages.clear()
    runner._warn_if_cwd_drifted()
    assert runner.messages == []


def test_cwd_drift_silent_when_on_the_worktree():
    runner = _drift_runner("/repo/.agit/worktrees/session-1", "/repo/.agit/worktrees/session-1")
    runner._warn_if_cwd_drifted()
    assert runner.messages == []
    assert runner._cwd_drift_checked is True


def test_cwd_drift_waits_when_no_cwd_recorded_yet():
    runner = _drift_runner(None, "/repo/.agit/worktrees/session-1")
    runner._warn_if_cwd_drifted()
    assert runner.messages == []
    assert getattr(runner, "_cwd_drift_checked", False) is False  # will re-check next tick


# --- worktree confinement ---

def test_confine_to_worktree_wraps_when_enabled(monkeypatch):
    import types
    from agit import sandbox

    monkeypatch.setattr(sandbox, "is_available", lambda: True)
    monkeypatch.delenv("AGIT_SANDBOX", raising=False)
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.worktree = types.SimpleNamespace(name="session-1")
    runner.global_config = types.SimpleNamespace(sandbox=True)
    runner.base_repo = types.SimpleNamespace(repo="/repo")
    runner.repo = types.SimpleNamespace(repo="/repo/.agit/worktrees/session-1")

    wrapped = runner._confine_to_worktree(["claude"])

    assert wrapped[0] == "sandbox-exec" and wrapped[-1] == "claude"


def test_confine_to_worktree_noop_without_worktree_or_when_disabled(monkeypatch):
    import types
    from agit import sandbox

    monkeypatch.setattr(sandbox, "is_available", lambda: True)
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.worktree = None  # no worktree (legacy): nothing to confine
    runner.global_config = types.SimpleNamespace(sandbox=True)
    assert runner._confine_to_worktree(["claude"]) == ["claude"]

    runner.worktree = types.SimpleNamespace(name="session-1")
    runner.global_config = types.SimpleNamespace(sandbox=False)  # user opted out
    runner.base_repo = types.SimpleNamespace(repo="/repo")
    runner.repo = types.SimpleNamespace(repo="/repo/.agit/worktrees/session-1")
    assert runner._confine_to_worktree(["claude"]) == ["claude"]


# --- backend-exit / native session switch ---

def test_adopt_latest_backend_session_repoints_after_native_switch():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = types.SimpleNamespace(repo="/wt")
    runner.backend = types.SimpleNamespace(latest_session_id=lambda repo: "switched-id")
    runner.state = types.SimpleNamespace(backend_session_id="pinned-id", last_backend_message_id="m9")
    runner._debug = lambda *a, **k: None

    runner._adopt_latest_backend_session()

    # The worktree's newest conversation (what the user switched to) wins.
    assert runner.state.backend_session_id == "switched-id"
    assert runner.state.last_backend_message_id is None


def test_adopt_latest_backend_session_keeps_id_when_unchanged():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = types.SimpleNamespace(repo="/wt")
    runner.backend = types.SimpleNamespace(latest_session_id=lambda repo: "same")
    runner.state = types.SimpleNamespace(backend_session_id="same", last_backend_message_id="m1")
    runner._debug = lambda *a, **k: None

    runner._adopt_latest_backend_session()

    assert runner.state.backend_session_id == "same"
    assert runner.state.last_backend_message_id == "m1"  # untouched


def test_recover_nonempty_session_returns_latest_with_content(tmp_path):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "empty-id"
    runner.repo = types.SimpleNamespace(repo="/wt")
    runner._debug = lambda *a, **k: None
    runner._stage_backend_resume = lambda sid: None
    real = ExportedSession("real-id", "claude-opus-4-8", None, [SessionTurn("u", "a", "p", "r", TokenUsage(), None)])
    runner.backend = types.SimpleNamespace(
        latest_session_id=lambda repo: "real-id",
        export_session=lambda repo, sid: real if sid == "real-id" else ExportedSession(sid, None, None, []),
    )

    assert runner._recover_nonempty_session() == ("real-id", real)


def test_recover_nonempty_session_none_when_latest_also_empty(tmp_path):
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.state = AgitState(tmp_path)
    runner.state.backend_session_id = "empty-id"
    runner.repo = types.SimpleNamespace(repo="/wt")
    runner._debug = lambda *a, **k: None
    runner._stage_backend_resume = lambda sid: None
    runner.backend = types.SimpleNamespace(
        latest_session_id=lambda repo: "other-empty",
        export_session=lambda repo, sid: ExportedSession(sid, None, None, []),
    )

    assert runner._recover_nonempty_session() is None


def test_relaunch_backend_resumes_then_gives_up_on_crash_loop(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._debug = lambda *a, **k: None
    calls = []
    runner._restart_agent = lambda msg: calls.append("relaunch")
    runner._finalize_on_backend_exit = lambda: calls.append("finalize")

    t = [1000.0]
    monkeypatch.setattr("agit.proxy.time.monotonic", lambda: t[0])

    # Backend keeps dying quickly: first 3 relaunch, the 4th gives up and exits.
    assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is False

    assert calls == ["relaunch", "relaunch", "relaunch", "finalize"]


def test_relaunch_backend_resets_loop_guard_after_quiet_period(monkeypatch):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._debug = lambda *a, **k: None
    relaunches = []
    runner._restart_agent = lambda msg: relaunches.append(1)
    runner._finalize_on_backend_exit = lambda: relaunches.append("finalize")

    t = [1000.0]
    monkeypatch.setattr("agit.proxy.time.monotonic", lambda: t[0])
    for _ in range(3):
        runner._relaunch_backend_or_exit()
    t[0] += 60.0  # a minute later the old exits no longer count
    assert runner._relaunch_backend_or_exit() is True
    assert relaunches.count("finalize") == 0  # never gave up


def test_finalize_on_backend_exit_finalizes_once_and_clears_pid():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.child_pid = 4321
    calls = []

    def fake_finalize():  # mirror the real guard inside _finalize_pending_work
        if runner.__dict__.get("_finalized_on_exit"):
            return
        runner.__dict__["_finalized_on_exit"] = True
        calls.append("finalized")

    runner._finalize_pending_work = fake_finalize
    runner._debug = lambda *a, **k: None

    runner._finalize_on_backend_exit()
    runner._finalize_on_backend_exit()  # idempotent (guarded inside _finalize_pending_work)

    assert runner.child_pid is None
    assert calls == ["finalized"]


# --- startup resume + naming ---

def test_resumable_sessions_come_from_backend_repo_record():
    import types

    refs = [SessionRef(id="a", updated=1.0, label="old"),
            SessionRef(id="b", updated=3.0, label="new"),
            SessionRef(id="c", updated=2.0, label="mid")]
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = types.SimpleNamespace(repo="/repo-root")
    asked = {}

    def _list(repo):
        asked["repo"] = repo
        return list(refs)

    runner.backend = types.SimpleNamespace(list_sessions=_list)

    result = runner._resumable_sessions()

    # Sourced from the repo aGiT launched in (not worktrees), newest first.
    assert asked["repo"] == "/repo-root"
    assert [ref.id for ref in result] == ["b", "c", "a"]


def _startup_runner():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner._prompt_startup_name = lambda continuing: "prompted-name"
    runner.root = types.SimpleNamespace(
        _names={},
        session_name_for=lambda sid: runner.root._names.get(sid),
        name_session=lambda sid, name: runner.root._names.__setitem__(sid, name),
    )
    return runner


def test_startup_name_keeps_stored_name_without_prompting():
    runner = _startup_runner()
    runner.root._names["sess-1"] = "alpha"

    assert runner._resolve_startup_session_name(runner.root, "sess-1", None) == "alpha"


def test_startup_name_uses_user_given_prior_worktree():
    runner = _startup_runner()

    # A non-auto prior worktree name counts as a name; an auto one does not.
    assert runner._resolve_startup_session_name(runner.root, "sess-1", "my-feature") == "my-feature"


def test_startup_name_prompts_when_unnamed_and_records_it():
    runner = _startup_runner()

    name = runner._resolve_startup_session_name(runner.root, "sess-1", "session-3")

    assert name == "prompted-name"
    assert runner.root._names["sess-1"] == "prompted-name"  # remembered for next time


# --- idle worktree base-sync ---

def test_sync_idle_worktrees_aligns_idle_skips_in_flight():
    import types

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.active_index = 0
    runner.repo = "repoA"
    runner.agent_in_flight = False  # active session is idle
    busy = types.SimpleNamespace(repo="repoB", agent_in_flight=True)   # working -> skip
    idle = types.SimpleNamespace(repo="repoC", agent_in_flight=False)  # idle -> sync
    runner.sessions = [types.SimpleNamespace(repo="repoA", agent_in_flight=False), busy, idle]
    aligned = []
    runner._align_session_to_base = lambda repo: aligned.append(repo)

    runner._sync_idle_worktrees_to_base()

    # Active (idle) + idle background are re-pointed; the in-flight one is left alone.
    assert aligned == ["repoA", "repoC"]


# --- user-facing git commands operate on the base tree, not the worktree -------
#
# A session runs in a worktree that only contains tracked files, but the user's
# own untracked / intentionally-unstaged files live in the base working tree.
# These commands (git-stage / git-unstaged / git-user-commit) must therefore read
# and write the base repo + base state, or the user's files are invisible.


def _user_git_runner(tmp_path, answers):
    from agit.git import GitRepo

    repo = GitRepo.init(tmp_path)  # seeds an initial commit; user files stay untracked
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = repo
    runner.repo = repo
    runner.global_config = type("GC", (), {"default_backend": "claude"})()
    runner._base_branch = repo.current_branch()
    runner._user_declined = []
    runner.prompts = []  # (title, body) of each popup shown
    scripted = list(answers)

    def prompt(title, body):
        runner.prompts.append((title, body))
        return scripted.pop(0) if scripted else None

    runner._prompt_popup = prompt
    return runner, repo


def test_stage_files_groups_new_and_declined_and_stages_selection(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=["1", "add new"])
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "local.txt").write_text("y\n", encoding="utf-8")
    runner._user_state().add_declined(["local.txt"])  # recorded by the pre-agent flow

    message = runner._stage_files_popup()

    body = runner.prompts[0][1]
    assert "New files:" in body and "new.py" in body
    assert "Intentionally unstaged:" in body and "local.txt" in body
    # #1 is new.py (new files listed first); it is staged+committed, local.txt left.
    assert "Committed 1 file" in message
    assert "new.py" not in repo.untracked_files()
    assert "local.txt" in repo.untracked_files()


def test_stage_files_all_stages_every_candidate(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=["a", "add all"])
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "local.txt").write_text("y\n", encoding="utf-8")
    runner._user_state().add_declined(["local.txt"])

    message = runner._stage_files_popup()

    assert "Committed 2 file" in message
    assert repo.untracked_files() == []
    assert runner._user_state().declined_untracked() == []  # declined cleared too


def test_stage_files_reads_declined_from_base_not_worktree_state(tmp_path):
    # Regression: declines are recorded in BASE state by the pre-agent prompt; the
    # menu must surface them from there, not from the (empty) worktree state.
    runner, repo = _user_git_runner(tmp_path, answers=[""])  # view, then cancel
    (tmp_path / "local.txt").write_text("y\n", encoding="utf-8")
    runner._user_state().add_declined(["local.txt"])
    runner.state = AgitState(tmp_path / "worktree")  # worktree state: nothing declined

    message = runner._stage_files_popup()

    assert "local.txt" in runner.prompts[0][1]
    assert message == "Nothing staged."


def test_stage_files_empty_when_nothing_to_stage(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=[])
    assert runner._stage_files_popup() == "No files to stage."
    assert runner.prompts == []  # nothing to ask about


def test_stage_files_invalid_selection_stages_nothing(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=["9"])  # out of range
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")

    message = runner._stage_files_popup()

    assert "nothing staged" in message.lower()
    assert "new.py" in repo.untracked_files()


def test_git_status_returns_full_long_format(tmp_path):
    from agit.git import GitRepo

    repo = GitRepo.init(tmp_path)
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")

    output = repo.status()

    assert "Untracked files" in output  # long format, not --short
    assert "new.py" in output


def test_status_line_unstaged_count_reflects_base_declined(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=[])
    runner.name = "s1"
    runner.backend = type("B", (), {"name": "claude"})()
    runner.state = type("S", (), {"backend_session_id": None})()
    runner.worktree = object()
    runner.scroll_back = 0
    runner.cols = 120
    runner._user_declined = ["a.txt", "b.txt"]

    assert "unstaged:2" in runner._status_line()
