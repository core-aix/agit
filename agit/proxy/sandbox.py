"""Confine the agent's filesystem writes to its session worktree.

aGiT runs each backend agent with its working directory set to a git worktree,
but nothing stops the agent from writing elsewhere through absolute paths — in
particular into the base repository that contains the worktree, which silently
bypasses aGiT's change tracking and worktree isolation.

On macOS we wrap the backend's process in ``sandbox-exec`` with a profile that
allows everything except writing inside the base repo's working tree, re-allowing
only the repo's ``.git`` (so the worktree can still commit) and the session
worktree itself. The agent can still *read* base files, so it keeps full context.

Where no sandbox is available (e.g. Linux), :func:`wrap_command` returns the
command unchanged and the caller falls back to warning when the base working tree
is touched.
"""

from __future__ import annotations

import os
import shutil
import sys

_DISABLE_VALUES = {"0", "off", "false", "no"}


def is_enabled() -> bool:
    """Whether worktree confinement is requested (the ``AGIT_SANDBOX`` env var
    overrides everything; default on)."""
    value = os.environ.get("AGIT_SANDBOX", "").strip().lower()
    if value in _DISABLE_VALUES:
        return False
    return True


def is_available() -> bool:
    """Whether this platform can enforce confinement."""
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _quote(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_profile(base: str, worktree: str) -> str:
    """A ``sandbox-exec`` profile: allow everything, then deny writes inside the
    base repo *and* the worktrees root (so sibling sessions are off-limits too),
    then re-allow the repo's ``.git`` and *this* session's worktree. Later rules
    win, so the worktree allow overrides the worktrees-root deny."""
    base = os.path.realpath(base)
    worktree = os.path.realpath(worktree)
    worktrees_root = os.path.dirname(worktree)  # sibling sessions live alongside
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            f'(deny file-write* (subpath "{_quote(base)}"))',
            f'(deny file-write* (subpath "{_quote(worktrees_root)}"))',
            f'(allow file-write* (subpath "{_quote(os.path.join(base, ".git"))}"))',
            f'(allow file-write* (subpath "{_quote(worktree)}"))',
        ]
    )


def wrap_command(command: list[str], *, base: str, worktree: str) -> list[str]:
    """Wrap ``command`` so its writes are confined to ``worktree`` (plus the
    repo's ``.git``) within ``base``. Returns the command unchanged when
    confinement is disabled, unavailable, or unnecessary (worktree == base)."""
    if not command or not is_enabled() or not is_available():
        return command
    if os.path.realpath(worktree) == os.path.realpath(base):
        return command  # legacy in-place session: nothing to isolate
    return ["sandbox-exec", "-p", build_profile(base, worktree), *command]
