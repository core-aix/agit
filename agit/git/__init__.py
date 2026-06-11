"""Git and working-tree mechanics: the GitRepo CLI facade, per-session
worktrees/branches, and the single-writer lock. Public names are re-exported
here so call sites import ``from agit.git import GitRepo`` etc."""

from agit.git.lock import RepoLock, already_running_message
from agit.git.repo import GitError, GitRepo
from agit.git.worktree import (
    BRANCH_PREFIX,
    WORKTREES_DIRNAME,
    WorktreeInfo,
    WorktreeManager,
    _sanitize_name,
)

__all__ = [
    "GitError",
    "GitRepo",
    "WorktreeInfo",
    "WorktreeManager",
    "BRANCH_PREFIX",
    "WORKTREES_DIRNAME",
    "_sanitize_name",
    "RepoLock",
    "already_running_message",
]
