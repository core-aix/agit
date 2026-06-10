from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

def already_running_message(pid: int | None) -> str:
    """Refusal shown when a second aGiT is started on a repo that already has one
    running. aGiT auto-commits and merges as the agent works, so two instances on
    the same repo would race over commits and branches; we allow only one. Names
    the holding process so the user can find (and stop) it."""
    owner = f"already running on this repo (PID {pid})" if pid else "already running on this repo"
    return (
        f"Another aGiT instance is {owner}.\n"
        "Stop it before starting a new one.\n"
        "\n"
        "aGiT manages your git commits as the agent works. Running two instances on "
        "the same repo would let them fight over commits and branches, so only one is "
        "allowed at a time."
    )


class RepoLock:
    """Advisory single-writer lock for a working tree.

    Only one aGiT process should auto-commit/merge in a given working tree at a
    time. The authority is an OS ``flock`` held on a long-lived fd: the kernel
    releases it the instant the owner dies, so there is no stale-file reclaim
    (and its delete-a-live-lock race) and no PID-liveness guessing that PID
    reuse could fool. The file itself carries no authority — it just records
    the owner's PID for the "already running" message; it persists across
    releases (never unlinked, so two processes can never end up holding flocks
    on two different inodes of the same path) and is truncated on release.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns True on success, False if another live
        process already holds it."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "started_at": time.time()}).encode())
        except OSError:
            pass  # informational only; the flock is what locks
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)  # leave no stale-looking owner info behind
        except OSError:
            pass
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def owner_pid(self) -> int | None:
        pid = self._read_info().get("pid")
        return pid if isinstance(pid, int) else None

    def is_held_by_self(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "RepoLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def _read_info(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
