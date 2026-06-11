"""Direct unit tests for BackendProcess (#29, P2).

These tests construct BackendProcess directly (no ProxyRunner.__new__) and
exercise spawn / write / drain / terminate / reap and the exec-failure guard.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from agit.proxy.process import BackendProcess


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


def test_drain_reads_all_available():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello ")
        os.write(write_fd, b"world")
        proc = BackendProcess(master_fd=read_fd)
        assert proc.drain() == b"hello world"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_drain_returns_none_on_eof():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # EOF, nothing buffered
    try:
        proc = BackendProcess(master_fd=read_fd)
        assert proc.drain() is None
    finally:
        os.close(read_fd)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def test_write_sends_bytes_to_fd():
    read_fd, write_fd = os.pipe()
    try:
        proc = BackendProcess(master_fd=write_fd)
        proc.write(b"ping")
        assert os.read(read_fd, 16) == b"ping"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_write_propagates_oserror_on_closed_fd():
    # OSError must propagate: call sites have differing error policies (abort
    # the operation, unwind the loop, or swallow) and implement them themselves,
    # exactly as they did around the original bare os.write calls.
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    proc = BackendProcess(master_fd=write_fd)
    with pytest.raises(OSError):
        proc.write(b"must raise")


def test_write_is_noop_when_master_fd_is_none():
    proc = BackendProcess(master_fd=None)
    proc.write(b"noop")  # must not raise


# ---------------------------------------------------------------------------
# Spawn + terminate + reap
# ---------------------------------------------------------------------------


def test_spawn_trivial_child_and_terminate():
    """/bin/cat as a trivial child: spawn, write, drain, terminate."""
    proc = BackendProcess.spawn(["/bin/cat"], cwd="/tmp")
    try:
        assert proc.child_pid is not None and proc.child_pid > 0
        assert proc.master_fd is not None

        proc.write(b"hello\n")
        # Give the child a moment to echo.
        time.sleep(0.1)
        output = proc.drain()
        # Cat echoes input (PTY mode), so we must have received something.
        assert output is not None and len(output) > 0

        # terminate() signals and closes but does NOT reap (matching the
        # original _terminate_child); reap here so the test leaves no zombie.
        pid = proc.child_pid
        proc.terminate()
        assert proc.child_pid is None
        assert proc.master_fd is None
        deadline = time.monotonic() + 3.0
        done = 0
        while time.monotonic() < deadline:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        if not done:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    finally:
        if proc.master_fd is not None:
            os.close(proc.master_fd)


def test_cleanup_terminates_child_via_signal_escalation():
    # Under a non-interactive shell the child inherits SIGINT=SIG_IGN (POSIX
    # background-job semantics), so this exercises the SIGTERM escalation path.
    # cleanup() preserves the original semantics verbatim: it reaps only a child
    # that dies inside the 1s SIGINT window and does NOT waitpid after SIGTERM.
    # The contract under test: after cleanup() returns, the child terminates on
    # its own (the test sends no signal of its own).
    proc = BackendProcess.spawn(["/bin/cat"], cwd="/tmp")
    pid = proc.child_pid
    try:
        proc.cleanup()
        deadline = time.monotonic() + 3.0
        done = 0
        try:
            while time.monotonic() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.05)
        except ChildProcessError:
            done = pid  # cleanup reaped it inside the SIGINT window
        if not done:
            os.kill(pid, signal.SIGKILL)  # don't leave a stray cat behind
            os.waitpid(pid, 0)
        assert done, "cleanup() did not terminate the child (escalation lost)"
    finally:
        if proc.master_fd is not None:
            os.close(proc.master_fd)


def test_spawn_exec_failure_guard_exits_127():
    """A non-existent command must exit with code 127, not leave a duplicate runner."""
    proc = BackendProcess.spawn(["/nonexistent-command-agit-test"], cwd="/tmp")
    pid = proc.child_pid
    # The child should exit almost immediately with code 127.
    deadline = time.monotonic() + 3.0
    exit_code = None
    while time.monotonic() < deadline:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            exit_code = 127  # already reaped elsewhere — treat as success
            break
        if done:
            if hasattr(os, "waitstatus_to_exitcode"):
                exit_code = os.waitstatus_to_exitcode(status)
            else:
                exit_code = (status >> 8) & 0xFF
            break
        time.sleep(0.05)
    if proc.master_fd is not None:
        os.close(proc.master_fd)
    assert exit_code == 127, f"expected exit code 127, got {exit_code}"


def test_teardown_clears_fd_and_pid():
    proc = BackendProcess.spawn(["/bin/cat"], cwd="/tmp")
    assert proc.child_pid is not None
    assert proc.master_fd is not None
    proc.teardown()
    assert proc.child_pid is None
    assert proc.master_fd is None


# ---------------------------------------------------------------------------
# Resize (ioctl)
# ---------------------------------------------------------------------------


def test_resize_is_noop_when_master_fd_is_none():
    proc = BackendProcess(master_fd=None)
    proc.resize(24, 80)  # must not raise


# ---------------------------------------------------------------------------
# Re-export from agit.proxy
# ---------------------------------------------------------------------------


def test_backendprocess_importable_from_agit_proxy():
    from agit.proxy import BackendProcess as BP  # noqa: F401

    assert BP is BackendProcess
