"""How far along a dashboard build is, so the loading page can say so.

The dashboard's first load on a repository it has never indexed is dominated by diffing every
commit in the history: the line counts in :mod:`agitrack.metrics.collect` and the per-file index
in :mod:`agitrack.metrics.files`. Both are cached per commit afterwards (so this only happens
once, and a new commit costs one diff), but that first pass is measured in minutes on a large
repository — and until now the page showed a spinner and the words "a large repo can take a few
seconds" for the whole of it, which is indistinguishable from a hang.

So the build reports what it is doing, and the page asks. The build runs inside whichever request
thread is serving ``/data``; ``/progress`` is answered on ANOTHER thread of the same threading
server, which is why the numbers live in a small registry keyed by repository rather than being
returned by the call that is still running.

Nothing here is required for a build to work: reporting with nobody listening is a no-op, and a
page that never asks (a static export, an older client) is unaffected.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class Build:
    """One in-flight dashboard build, as the page should hear about it."""

    stage: str = ""
    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.monotonic)

    def payload(self) -> dict:
        return {
            "active": True,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "elapsed": round(time.monotonic() - self.started, 1),
        }


_local = threading.local()  # the build THIS thread is running, if any
_lock = threading.Lock()
_active: dict[str, Build] = {}  # repo root -> its in-flight build, for other threads to read


@contextmanager
def publishing(root: Path | str) -> Iterator[None]:
    """Publish this thread's build progress for the repository at *root*.

    Re-entrant on purpose: ``/data`` builds the dashboard and then the file index, and each of
    those wraps itself. The outermost scope owns the registry entry, so an inner one finishing
    never takes the reading page's answer away mid-build."""
    if getattr(_local, "build", None) is not None:
        yield  # already inside a published build on this thread
        return
    key = str(root)
    build = Build()
    _local.build = build
    with _lock:
        _active[key] = build
    try:
        yield
    finally:
        _local.build = None
        with _lock:
            if _active.get(key) is build:  # never remove a newer build another thread published
                del _active[key]


def step(stage: str, done: int, total: int) -> None:
    """Report progress from inside a build. A no-op when nothing is publishing, so the hot paths
    can call it unconditionally."""
    build = getattr(_local, "build", None)
    if build is None:
        return
    build.stage, build.done, build.total = stage, done, total


def snapshot(root: Path | str) -> dict:
    """What ``/progress`` answers for *root*: the live build, or "nothing running"."""
    with _lock:
        build = _active.get(str(root))
    return build.payload() if build is not None else {"active": False}
