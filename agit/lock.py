from __future__ import annotations

import json
import os
import time
from pathlib import Path


class RepoLock:
    """Advisory single-writer lock for a working tree.

    Only one aGiT process should auto-commit/merge in a given working tree at a
    time. The lock is a small file holding the owner's PID; a lock whose owner
    process is no longer alive is treated as stale and reclaimed. This is an
    application-level lock (not a git feature) and is best-effort: the atomic
    primitive is the ``O_CREAT | O_EXCL`` create.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._acquired = False

    def acquire(self) -> bool:
        """Try to take the lock. Returns True on success, False if another live
        process already holds it."""
        if self._acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._try_create():
            return True
        # Someone holds it (or a stale file remains). Reclaim if stale, retry once.
        if self._reclaim_if_stale() and self._try_create():
            return True
        return False

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if self._read_info().get("pid") == os.getpid():
                self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        finally:
            self._acquired = False

    def owner_pid(self) -> int | None:
        pid = self._read_info().get("pid")
        return pid if isinstance(pid, int) else None

    def is_held_by_self(self) -> bool:
        return self._acquired

    def __enter__(self) -> "RepoLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            payload = json.dumps({"pid": os.getpid(), "started_at": time.time()})
            os.write(fd, payload.encode())
        finally:
            os.close(fd)
        self._acquired = True
        return True

    def _reclaim_if_stale(self) -> bool:
        """Remove the lock file if its recorded owner is not alive. Returns True
        if the lock is now free to be re-created."""
        info = self._read_info()
        pid = info.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            return False
        # Dead owner, or an unreadable/corrupt lock file: reclaim it.
        try:
            self.path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _read_info(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user.
        return True
    except OSError:
        return False
    return True
