"""BackendProcess: child-process / PTY lifecycle for the proxy backend (#29, P2).

Owns the fork/exec mechanics, PTY drain, window-size ioctl, signal-based
teardown, and all writes to the child's PTY.  Policy decisions (command
construction, sandbox wrapping, session selection) stay in ProxyRunner.

Ownership note (P3)
-------------------
Each proxy :class:`~agit.proxy.session.Session` owns one BackendProcess
instance (``session.process``) for its whole lifetime.  ``child_pid`` and
``master_fd`` remain addressable as plain fields — on the Session via
properties over its process, and on the runner via the P3 compat properties
that delegate to the active session — so tests built via
``ProxyRunner.__new__`` that set/read these fields directly keep working.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import time


class BackendProcess:
    """Owns PTY/child-process mechanics for one backend session.

    Parameters
    ----------
    master_fd:
        The master end of the PTY (returned by ``pty.fork``).  ``None`` means
        the process has not been spawned yet (or has been torn down).
    child_pid:
        PID of the child process.  ``None`` when not running.
    """

    def __init__(self, master_fd: int | None = None, child_pid: int | None = None) -> None:
        self.master_fd = master_fd
        self.child_pid = child_pid

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    @classmethod
    def spawn(cls, command: list[str], cwd: str) -> "BackendProcess":
        """Fork a PTY child, exec *command* inside it, and return a new instance.

        The child changes to *cwd* before exec.  If exec fails the child exits
        with code 127 (the exec-failure guard from issue #20) so the fork never
        silently propagates as a duplicate runner.
        """
        pid, fd = pty.fork()
        if pid == 0:
            # The child must never survive a failed exec (backend uninstalled
            # mid-session, PATH change, worktree deleted): the exception would
            # otherwise propagate and leave a duplicate aGiT running from the
            # fork point, sharing state files, locks, and the terminal.
            try:
                os.chdir(cwd)
                os.execvp(command[0], command)
            except BaseException:
                os._exit(127)
        return cls(master_fd=fd, child_pid=pid)

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def drain(self) -> bytes | None:
        """Read all currently-available output from the PTY (bounded).

        Returns the concatenated bytes, or ``None`` on EOF / read error with
        nothing buffered (signals the caller that the child is gone).

        Read all currently-available output in one go (capped) and render once,
        instead of re-rendering after every 4 KB. During heavy output (e.g.
        fast scrolling in OpenCode) this keeps the PTY drained so the backend's
        writes never block, which otherwise stalls/kills the backend.
        """
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

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Write *data* to the child's PTY master fd.

        ``OSError`` propagates: call sites have different error semantics (some
        abort the surrounding operation, some let it unwind the loop), so the
        policy of swallowing or handling belongs to the caller, exactly as it
        did when they called ``os.write`` directly.
        """
        if self.master_fd is None:
            return
        os.write(self.master_fd, data)

    # ------------------------------------------------------------------
    # Resize (PTY ioctl only)
    # ------------------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """Send ``TIOCSWINSZ`` to the child's PTY master fd.

        The caller is responsible for updating its own screen model; this method
        only performs the kernel ioctl. ``OSError`` propagates so the caller can
        skip follow-up work (e.g. a repaint) when the ioctl failed, matching the
        original inline behavior.
        """
        if self.master_fd is None:
            return
        import fcntl
        import struct
        import termios

        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def terminate(self) -> None:
        """Send SIGINT to the child and close the master fd.

        Callers that need to wait for the child to exit should pass the pid to
        ``_note_pid_for_reaping`` / ``_reap_stopped_children`` on the runner
        (those are host-level, not session-level, so they live on the runner).
        This method fires the signal, closes the fd, and nulls the local
        references; the runner clears its own ``child_pid`` / ``master_fd``
        after calling this.
        """
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.child_pid = None

    def cleanup(self) -> None:
        """SIGINT -> wait up to 1 s -> SIGTERM the child process.

        This is the graceful shutdown used by ``_cleanup_child`` on exit/signal.
        Does nothing if no child is running.  The waitpid reaping logic (issue
        #21) is preserved verbatim.
        """
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

    def teardown(self) -> None:
        """``cleanup()`` then close the master fd and null out both fields.

        Equivalent to the old ``_teardown_child``: suitable for callers that
        want to reuse the runner for a fresh spawn afterwards.
        """
        self.cleanup()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.child_pid = None

    def signal_exit(self) -> None:
        """Send SIGINT to the child without waiting (used by ``_exit_child``).

        Does not close the master fd -- the caller handles that in the ``run``
        ``finally`` block so it always runs even if the child is already gone.
        """
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
