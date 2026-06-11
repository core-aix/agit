from __future__ import annotations

from typing import List

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agit.git.repo import GitRepo

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
    main working tree. A worktree is created detached at the base; per-turn
    branches ``agit/<backend>/<name>/tN`` are created lazily on the first commit,
    so an unused session never leaves a branch behind."""

    def __init__(self, main_repo: GitRepo) -> None:
        self.main_repo = main_repo
        self.root = main_repo.repo / ".agit" / WORKTREES_DIRNAME

    def worktree_path(self, name: str) -> Path:
        return self.root / _sanitize_name(name)

    def turn_branch(self, name: str, turn: int, *, backend: str) -> str:
        # Turn branches live under refs/heads/agit/<backend>/<name>/ ; there is
        # deliberately no bare parent ref (it would D/F-conflict with these).
        return f"{BRANCH_PREFIX}{_sanitize_name(backend)}/{_sanitize_name(name)}/t{turn}"

    def is_agit_branch(self, branch: str) -> bool:
        return branch.startswith(BRANCH_PREFIX)

    def create(self, name: str, *, base: str) -> WorktreeInfo:
        path = self.worktree_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Drop any stale admin entry first: if a previous worktree directory was
        # removed out-of-band, git keeps a "prunable" registration that would make
        # `worktree add` fail with "already registered". Pruning clears only
        # entries whose directories are gone, so live worktrees are untouched.
        self.main_repo.worktree_prune()
        # Detached at base: no branch exists until the session actually commits.
        self.main_repo.worktree_add_detached(str(path), base=base)
        return WorktreeInfo(name=_sanitize_name(name), path=path, branch="")

    def remove(self, name: str, *, force: bool = True) -> None:
        path = self.worktree_path(name)
        try:
            self.main_repo.worktree_remove(str(path), force=force)
        except Exception:
            # git refused (e.g. locked/modified). Force-clean the directory and
            # prune the administrative entry so we never leave a worktree behind.
            shutil.rmtree(path, ignore_errors=True)
        self.main_repo.worktree_prune()
        if path.exists():
            # The directory still survives; keep its branches too so a worktree and
            # its branch never drift out of sync (the user can retry the removal).
            return
        # Directory is gone: delete this session's turn branches under any backend
        # (agit/<backend>/<name>/tN, and legacy agit/<name>/tN). The session name
        # is the second-to-last path segment in both layouts.
        sanitized = _sanitize_name(name)
        for branch in self.main_repo.list_branches(BRANCH_PREFIX):
            parts = branch.split("/")
            if len(parts) >= 3 and parts[-2] == sanitized:
                self.main_repo.delete_branch(branch, force=True)

    def list(self) -> List[WorktreeInfo]:
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

    def stale(self) -> List[WorktreeInfo]:
        """aGiT worktrees left behind by a previous run (used at startup for
        recovery). With no live manager, every aGiT worktree is stale."""
        return self.list()
