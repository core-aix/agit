"""Tests for agitrack/console.py — the pre-TUI prompt helpers.

The point of these helpers is that a question asked after a long install is answered by a
deliberate keypress, and that a silent installer still looks alive. Nothing here touches a
real terminal: stdin/stdout are stubbed, so the drains are no-ops that must stay harmless.
"""

from __future__ import annotations

import threading

import agitrack.console as console


def test_drain_terminal_input_is_a_noop_without_a_tty(monkeypatch):
    monkeypatch.setattr(console.sys.stdin, "isatty", lambda: False, raising=False)
    console.drain_terminal_input()  # must not raise (pytest's stdin is not a terminal)


def test_drain_terminal_input_never_raises_on_a_broken_stdin(monkeypatch):
    class Broken:
        def isatty(self):
            raise OSError("no tty here")

    monkeypatch.setattr(console.sys, "stdin", Broken())
    console.drain_terminal_input()


def test_ask_drains_before_reading(monkeypatch):
    # A keypress made while an installer ran must not answer the NEXT question: the input
    # queue is discarded first, then the question is read.
    events: list[str] = []
    monkeypatch.setattr(console, "drain_terminal_input", lambda: events.append("drain"))
    answer = console.ask("Pick one: ", input_fn=lambda prompt: events.append(f"input:{prompt}") or "2")
    assert answer == "2"
    assert events == ["drain", "input:Pick one: "]


def test_progress_ticker_disabled_produces_no_output():
    lines: list[str] = []
    with console.progress_ticker("installing", output_fn=lines.append, interval=0.001, enabled=False):
        pass
    assert lines == []


def test_progress_ticker_reports_while_a_slow_step_runs():
    # The reassurance for an installer that prints nothing for minutes.
    import time

    lines: list[str] = []
    with console.progress_ticker("installing Claude Code", output_fn=lines.append, interval=0.01, enabled=True):
        time.sleep(0.15)
    assert lines, "a long step must report progress"
    assert all("still installing Claude Code" not in line or "elapsed" in line for line in lines)
    assert "installing Claude Code" in lines[0]


def test_progress_ticker_stops_ticking_after_the_block():
    import time

    lines: list[str] = []
    with console.progress_ticker("working", output_fn=lines.append, interval=0.01, enabled=True):
        time.sleep(0.05)
    count = len(lines)
    time.sleep(0.05)
    assert len(lines) == count  # the thread stopped with the block


def test_the_ticking_thread_is_gone_by_the_time_the_block_returns():
    """The guarantee behind the test above, asserted directly rather than by waiting and hoping.

    Setting the stop event is not the same as the thread having stopped: a tick whose ``wait``
    had just timed out was already on its way to ``output_fn``, so a line could land under
    whatever the program printed next. The timing-based test caught it only on a loaded CI
    machine; this one cannot miss it."""
    import threading

    with console.progress_ticker("working", output_fn=lambda line: None, interval=0.001, enabled=True):
        assert any(t.name == "agitrack-progress" and t.is_alive() for t in threading.enumerate())

    assert not [t for t in threading.enumerate() if t.name == "agitrack-progress" and t.is_alive()]


def test_a_wedged_output_never_turns_a_progress_message_into_a_hang(monkeypatch):
    # Waiting for the thread must not become a way to block forever: the join is bounded, and a
    # progress line is never worth holding up the program that printed it.
    import time

    monkeypatch.setattr(console, "_TICKER_JOIN_SECONDS", 0.05)
    blocked = threading.Event()

    def wedged(_line: str) -> None:
        blocked.set()
        time.sleep(30)  # never returns within the test

    started = time.monotonic()
    with console.progress_ticker("working", output_fn=wedged, interval=0.001, enabled=True):
        blocked.wait(2.0)
    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------------------------------
# "Is there a human here?" — the guard behind every prompt-or-refuse decision.
# --------------------------------------------------------------------------------------


def test_the_windows_nul_device_is_not_an_interactive_stdin():
    """On Windows ``isatty()`` is ``GetFileType() == FILE_TYPE_CHAR`` and ``NUL`` IS a character
    device, so ``sys.stdin.isatty()`` returns **True** for a process whose stdin is ``NUL`` —
    which is how a detached process, a scheduled task, a service, or ``agitrack … < NUL`` gets
    its stdin. Every "is anyone there to answer?" guard built on the bare call therefore fell
    open on Windows; `--daemons stop` reached ``input()``, got EOF, and reported "Cancelled.
    Nothing was stopped." with exit 0, naming neither `--yes` nor a failure."""
    import os
    import sys

    with open(os.devnull) as devnull:
        if sys.platform == "win32":
            assert devnull.isatty() is True, "the premise: NUL claims to be a terminal"
            assert console._is_windows_nul(devnull) is True
        else:
            assert console._is_windows_nul(devnull) is False  # POSIX /dev/null is not a tty


def test_a_pipe_or_file_stdin_is_left_to_isatty(tmp_path):
    """Only a character device that is NOT a console is rejected. A pipe, a file or a redirected
    fd must fall straight through, or the check would start second-guessing streams it has no
    business overruling."""
    path = tmp_path / "in.txt"
    path.write_text("hello\n", encoding="utf-8")
    with path.open() as handle:
        assert console._is_windows_nul(handle) is False


def test_the_interactive_checks_never_raise_on_a_broken_stream(monkeypatch):
    class Broken:
        def isatty(self):
            raise OSError("no tty here")

    monkeypatch.setattr(console.sys, "stdin", Broken())
    monkeypatch.setattr(console.sys, "stdout", Broken())
    assert console.stdin_is_interactive() is False
    assert console.stdout_is_interactive() is False


def test_the_daemon_log_stamps_every_line_with_the_time(monkeypatch):
    """The background tracker's stdout IS <repo>/.agitrack/background.log, read only after the
    fact. Unstamped, a start that timed out, a restart loop and a tracker up since Tuesday all
    read identically — which is exactly the log that could not answer "why did it not start?"."""
    import io
    import re

    buffer = io.StringIO()
    monkeypatch.setattr(console.sys, "stdout", buffer)
    monkeypatch.setattr(console.sys, "stderr", buffer)
    console.timestamp_output()

    print("aGiTrack is starting...")
    print("half", end="")  # print emits the text and the newline as separate writes…
    print(" a line")  # …and a continued line must not be stamped in the middle

    lines = buffer.getvalue().splitlines()
    assert len(lines) == 2
    assert re.fullmatch(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\] aGiTrack is starting\.\.\.", lines[0])
    assert lines[1].endswith("] half a line")


def test_stamping_output_is_idempotent_and_keeps_the_stream_usable(monkeypatch):
    """Called once per process, but a second call must not double-stamp — and the wrapper has to
    stay a drop-in for sys.stdout: print(flush=True), fileno() and isatty() all keep working."""
    import io

    class _Stream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushed = 0

        def flush(self):
            self.flushed += 1

    stream = _Stream()
    monkeypatch.setattr(console.sys, "stdout", stream)
    console.timestamp_output()
    console.timestamp_output()

    print("once", flush=True)
    assert console.sys.stdout is not stream
    assert stream.flushed >= 1  # flush reached the real stream
    assert stream.getvalue().count("] ") == 1  # one stamp, not two
