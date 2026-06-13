from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self._run(["git", "rev-parse", "--show-toplevel"])

    @classmethod
    def discover(cls, path: Path) -> "GitRepo":
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise GitError(f"Not a Git repository: {path}")
        return cls(Path(process.stdout.strip()))

    @classmethod
    def init(cls, path: Path) -> "GitRepo":
        """Initialize a new Git repository at ``path`` and seed an empty initial
        commit. aGiT runs every session in a worktree, which requires a valid HEAD;
        a fresh `git init` leaves an unborn branch, so the seed commit makes the
        repo usable immediately. Any existing files are committed afterwards by
        aGiT's normal pre-agent user-commit flow."""
        path = path.expanduser()
        path.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            ["git", "init"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise GitError(f"git init failed in {path}:\n{process.stderr.strip()}")
        repo = cls.discover(path)
        repo.ensure_born()
        return repo

    def has_commits(self) -> bool:
        """True once the repository has at least one commit (a born HEAD)."""
        return self._run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], check=False).returncode == 0

    def ensure_born(self) -> bool:
        """Make sure HEAD points at a commit. A freshly `git init`-ed repository
        has an unborn branch with no commits, which aGiT cannot run on (every
        session is a worktree, and a worktree needs a valid HEAD). Seed an empty
        initial commit so the repo is usable; any pre-existing files are left
        untracked for aGiT's normal pre-agent user-commit flow. Returns True if a
        seed commit was created, False if HEAD was already born."""
        if self.has_commits():
            return False
        self._run(["git", "commit", "--allow-empty", "-m", "Initial commit"])
        return True

    def status_short(self) -> str:
        return self._run(["git", "status", "--short"]).stdout

    def status(self) -> str:
        # Full (long-format) `git status`, for the user-facing status command.
        return self._run(["git", "status"]).stdout

    def has_changes(self) -> bool:
        return bool(self.status_short().strip())

    def has_tracked_changes(self) -> bool:
        return self._diff_has_changes(["git", "diff", "--quiet"]) or self.has_staged_changes()

    def diff_head(self) -> str:
        # Content of all tracked changes (staged + unstaged) relative to HEAD.
        # Used as a fingerprint: `status --short` alone cannot tell new edits to
        # an already-modified file apart from the state a user declined before.
        return self._run(["git", "diff", "HEAD"], check=False).stdout

    def add_tracked(self) -> None:
        self._run(["git", "add", "-u"])

    def stage_paths(self, paths: list[str]) -> None:
        if paths:
            self._run(["git", "add", "--", *paths])

    def untracked_files(self) -> list[str]:
        output = self._run(["git", "ls-files", "--others", "--exclude-standard"]).stdout
        return [line for line in output.splitlines() if line and not line.startswith(".agit/")]

    def has_staged_changes(self) -> bool:
        return self._diff_has_changes(["git", "diff", "--cached", "--quiet"])

    def _diff_has_changes(self, command: list[str]) -> bool:
        process = self._run(command, check=False)
        if process.returncode == 0:
            return False
        if process.returncode == 1:
            return True
        raise GitError(process.stderr.strip() or "Unable to inspect changes")

    def commit(self, message: str) -> str:
        self._run(["git", "commit", "-F", "-"], input_text=message)
        return self.short_sha("HEAD")

    def amend_commit(self, message: str) -> str:
        """Rewrite HEAD's message (tree untouched); returns the new short SHA."""
        self._run(["git", "commit", "--amend", "-F", "-"], input_text=message)
        return self.short_sha("HEAD")

    def cover_commit(self, message: str, *, first_parent: str, second_parent: str) -> str:
        """Create a merge-shaped *cover* commit: the tree of ``second_parent``
        with parents ``(first_parent, second_parent)`` — the same shape as a
        GitHub PR merge commit. Used to attach aGiT's message on top of
        backend-made commits without amending them, since an amend changes
        their hashes and breaks references already published elsewhere (#58).
        The checked-out branch (or detached HEAD) moves to the new commit; the
        working tree is untouched because the tree is identical to HEAD's."""
        tree = self.rev_parse(f"{second_parent}^{{tree}}")
        sha = self._run(
            ["git", "commit-tree", tree, "-p", first_parent, "-p", second_parent],
            input_text=message,
        ).stdout.strip()
        self._run(["git", "reset", "--soft", sha])
        return self.short_sha(sha)

    def parents(self, ref: str = "HEAD") -> list[str]:
        output = self._run(["git", "rev-list", "--parents", "-1", ref]).stdout.split()
        return output[1:]

    def short_sha(self, ref: str = "HEAD") -> str:
        return self._run(["git", "rev-parse", "--short", ref]).stdout.strip()

    def commit_message(self, ref: str = "HEAD") -> str:
        return self._run(["git", "log", "-1", "--format=%B", ref], check=False).stdout

    def diff_range(self, base: str, head: str) -> str:
        return self._run(["git", "diff", f"{base}..{head}"], check=False).stdout

    # --- branches / worktrees / merges (used by concurrent-session support) ---

    def current_branch(self) -> str:
        # Returns the branch name, or "HEAD" when detached.
        return self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def rev_parse(self, ref: str) -> str:
        return self._run(["git", "rev-parse", ref]).stdout.strip()

    def branch_exists(self, name: str) -> bool:
        return self._run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False).returncode == 0

    def create_branch(self, name: str, base: str) -> None:
        self._run(["git", "branch", name, base])

    def delete_branch(self, name: str, *, force: bool = False) -> None:
        self._run(["git", "branch", "-D" if force else "-d", name])

    def list_branches(self, prefix: str = "") -> list[str]:
        output = self._run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).stdout
        names = [line for line in output.splitlines() if line]
        return [name for name in names if name.startswith(prefix)] if prefix else names

    def switch(self, branch: str, *, create: bool = False, base: str | None = None) -> None:
        command = ["git", "switch"]
        if create:
            # `-c`, never `-C`: create must not silently reset an existing
            # branch — a leftover turn branch can still hold unintegrated
            # commits. Callers pick a free name (or handle the GitError).
            command.append("-c")
        command.append(branch)
        if create and base:
            command.append(base)
        self._run(command)

    def switch_detach(self, ref: str) -> None:
        # Detach HEAD at ``ref`` (keeping any working-tree changes), leaving no
        # branch checked out — so a now-empty turn branch can be deleted.
        self._run(["git", "switch", "--detach", ref])

    def is_detached(self) -> bool:
        return self.current_branch() == "HEAD"

    def worktree_add_detached(self, path: str, *, base: str) -> None:
        # Create a worktree detached at ``base`` with no branch of its own; a turn
        # branch is created lazily on the first commit (see _ensure_turn_branch).
        self._run(["git", "worktree", "add", "--detach", path, base])

    def worktree_remove(self, path: str, *, force: bool = False) -> None:
        command = ["git", "worktree", "remove"]
        if force:
            command.append("--force")
        command.append(path)
        self._run(command)

    def worktree_prune(self) -> None:
        # Drop administrative entries for worktrees whose directories are gone.
        self._run(["git", "worktree", "prune"], check=False)

    def worktree_list(self) -> list[dict[str, str]]:
        output = self._run(["git", "worktree", "list", "--porcelain"]).stdout
        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if not line.strip():
                if current:
                    worktrees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["path"] = value
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key == "detached":
                current["branch"] = ""
        if current:
            worktrees.append(current)
        return worktrees

    def merge(self, ref: str) -> bool:
        """Merge ``ref`` into the current branch. Returns True on a clean merge,
        False if there are conflicts (the merge is left in progress for
        resolution). Raises GitError on any other failure."""
        process = self._run(["git", "merge", "--no-edit", ref], check=False)
        if process.returncode == 0:
            return True
        if self.unmerged_paths():
            return False
        raise GitError(process.stderr.strip() or process.stdout.strip() or "merge failed")

    def merge_ff_only(self, ref: str) -> None:
        self._run(["git", "merge", "--ff-only", ref])

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True if ``ancestor`` is reachable from ``descendant`` — i.e. moving a
        branch from ``ancestor`` to ``descendant`` would be a fast-forward."""
        return (
            self._run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                check=False,
            ).returncode
            == 0
        )

    def fast_forward_branch(self, branch: str, target: str) -> None:
        """Fast-forward ``branch`` (which need NOT be checked out) to ``target``.

        Refuses unless ``target`` is a descendant of ``branch``, so this can only
        ever advance the branch along its own history — never a force-move that
        could drop commits. Lets aGiT integrate into the base branch even when the
        user has `git checkout`ed a different branch in the directory."""
        if not self.is_ancestor(branch, target):
            raise GitError(f"'{target}' is not a fast-forward of '{branch}'")
        # -f updates the ref in place; git itself still refuses if `branch` is
        # checked out in any worktree, so this only runs for a non-checked-out base.
        self._run(["git", "branch", "-f", branch, target])

    def merge_abort(self) -> None:
        self._run(["git", "merge", "--abort"], check=False)

    def unmerged_paths(self) -> list[str]:
        output = self._run(["git", "diff", "--name-only", "--diff-filter=U"], check=False).stdout
        return [line for line in output.splitlines() if line]

    def merge_in_progress(self) -> bool:
        return self._run(["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], check=False).returncode == 0

    def has_conflict_markers(self) -> bool:
        # `git diff --check` reports leftover conflict markers, but only in the
        # worktree vs the index — once `add_all()` stages the files it sees
        # nothing. `--cached` checks the staged content against HEAD, so markers
        # are still caught right before they would be committed.
        for command in (["git", "diff", "--check"], ["git", "diff", "--cached", "--check"]):
            output = self._run(command, check=False).stdout
            if "conflict marker" in output.lower():
                return True
        return False

    def add_all(self) -> None:
        self._run(["git", "add", "-A"])

    def log_range(self, base: str, head: str, *, paths: list[str] | None = None) -> str:
        command = ["git", "log", "--no-color", "--format=%h %s", f"{base}..{head}"]
        if paths:
            command.extend(["--", *paths])
        return self._run(command, check=False).stdout.strip()

    def log_shas(self, base: str, head: str) -> list[str]:
        """Full SHAs of commits in ``base..head``, oldest first."""
        output = self._run(["git", "log", "--format=%H", "--reverse", f"{base}..{head}"], check=False).stdout
        return [line for line in output.split() if line]

    def notes_add(self, commit: str, message: str, *, namespace: str = "agit") -> None:
        self._run(["git", "notes", "--ref", namespace, "add", "-f", "-m", message, commit])

    def notes_show(self, commit: str, *, namespace: str = "agit") -> str | None:
        result = self._run(["git", "notes", "--ref", namespace, "show", commit], check=False)
        return result.stdout if result.returncode == 0 else None

    def notes_list(self, *, namespace: str = "agit") -> list[tuple[str, str]]:
        output = self._run(["git", "notes", "--ref", namespace, "list"], check=False).stdout
        if not output.strip():
            return []
        entries = []
        for line in output.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                commit_sha = parts[1]
                note = self.notes_show(commit_sha, namespace=namespace)
                first_line = (note or "").strip().split("\n")[0] if note else ""
                entries.append((commit_sha, first_line))
        return entries

    def _run(
        self, command: list[str], *, input_text: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            command,
            cwd=self.repo,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise GitError(f"Command failed: {' '.join(command)}\n{detail}")
        return process
