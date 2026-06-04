from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agit.git import GitRepo

# All aGiT-managed branches live under this prefix so they can be recognised
# (for cleanup / stale recovery) and never collide with the user's branches.
BRANCH_PREFIX = "agit/"
WORKTREES_DIRNAME = "worktrees"


@dataclass
class WorktreeInfo:
    name: str
    path: Path
    branch: str


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return cleaned or "session"


class WorktreeManager:
    """Creates and tracks aGiT session worktrees under ``.agit/worktrees`` of the
    main working tree. Each session worktree is checked out on its own
    ``agit/<name>`` branch; per-turn branches are derived from it."""

    def __init__(self, main_repo: GitRepo) -> None:
        self.main_repo = main_repo
        self.root = main_repo.repo / ".agit" / WORKTREES_DIRNAME

    def worktree_path(self, name: str) -> Path:
        return self.root / _sanitize_name(name)

    def turn_branch(self, name: str, turn: int) -> str:
        # Turn branches live under refs/heads/agit/<name>/ ; there is deliberately
        # no bare ``agit/<name>`` ref (it would D/F-conflict with these).
        return f"{BRANCH_PREFIX}{_sanitize_name(name)}/t{turn}"

    def branch_prefix(self, name: str) -> str:
        return f"{BRANCH_PREFIX}{_sanitize_name(name)}/"

    def is_agit_branch(self, branch: str) -> bool:
        return branch.startswith(BRANCH_PREFIX)

    def create(self, name: str, *, base: str, initial_turn: int = 0) -> WorktreeInfo:
        path = self.worktree_path(name)
        branch = self.turn_branch(name, initial_turn)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.main_repo.worktree_add(str(path), branch=branch, base=base)
        return WorktreeInfo(name=_sanitize_name(name), path=path, branch=branch)

    def remove(self, name: str, *, force: bool = True) -> None:
        path = self.worktree_path(name)
        try:
            self.main_repo.worktree_remove(str(path), force=force)
        except Exception:
            # Worktree may already be gone; ignore so cleanup is idempotent.
            pass
        for branch in self.main_repo.list_branches(self.branch_prefix(name)):
            self.main_repo.delete_branch(branch, force=True)

    def list(self) -> list[WorktreeInfo]:
        infos: list[WorktreeInfo] = []
        root = self.root.resolve()
        for entry in self.main_repo.worktree_list():
            path = Path(entry.get("path", ""))
            try:
                inside = path.resolve().parent == root
            except OSError:
                inside = False
            if inside:
                infos.append(WorktreeInfo(name=path.name, path=path, branch=entry.get("branch", "")))
        return infos

    def stale(self) -> list[WorktreeInfo]:
        """aGiT worktrees left behind by a previous run (used at startup for
        recovery). With no live manager, every aGiT worktree is stale."""
        return self.list()
