from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from agitrack.proc import _IS_WINDOWS, UTF8_TEXT, console_isolation_kwargs, fs_path


class GitError(RuntimeError):
    pass


# --- short-lived read cache -------------------------------------------------------------
#
# WHY THIS EXISTS: A SINGLE ENTER COSTS 19 GIT SUBPROCESSES, and five of them ask the same
# question. Measured through the real reactor on native Windows, on an empty repo with a clean
# tree: `rev-parse --abbrev-ref HEAD` five times, `diff --quiet` three times, `diff --cached
# --quiet` three times, `ls-files --others` three times — 740 ms, which was 100% of the delay
# between pressing Enter and the prompt reaching the backend. Nothing was slow; there were just
# a lot of process spawns, and a process spawn costs ~38 ms on Windows against ~5 ms on
# macOS/Linux. That is the whole reason "Enter takes a long time" is a Windows report: the same
# path costs about 120 ms there and passes for instant.
#
# The submit path asks its questions through half a dozen collaborating helpers
# (`_pre_agent_commit_if_needed`, `has_pre_agent_user_changes`, `_base_user_edits_pending`,
# `_begin_agent_turn`, `_integrate_committed_turn_before_new_turn`, `_ensure_turn_branch`), and
# threading one answer through all of them would couple every one of them to the caller. So the
# de-duplication lives at the single choke point every one of them goes through instead.
#
# WHAT MAKES IT SAFE:
#   * It is OFF unless a caller explicitly opens a `read_cache()` scope, and those scopes are
#     milliseconds long. Nothing outside one ever sees a cached answer.
#   * Only unambiguously read-only plumbing is cached (below). Anything else — every commit,
#     stage, switch, merge, fetch — DROPS the whole cache as it runs, so a read that follows a
#     write in the same scope always re-runs. That is the invariant the callers depend on:
#     `_offer_pre_agent_user_commit` commits, and everything after it must see the new tree.
#   * It is THREAD-LOCAL, but invalidation is PROCESS-WIDE. The git worker thread commits and
#     merges on the very repos the reactor is reading, and a thread-local clear could never see
#     that. So every write anywhere in the process bumps a counter, and a cached answer is only
#     reused while that counter is unchanged — which makes "the worker committed while the
#     submit path was mid-flight" a cache miss rather than a stale answer.
_read_cache_state = threading.local()

# Bumped by every git command that can write, on any thread, for any repo. Read on every cache
# hit. A single counter (rather than one per repo) because git writes are not confined to the
# repo they run in: a commit in a worktree moves refs the base repo reads, and vice versa.
_write_epoch = 0
_write_epoch_lock = threading.Lock()


def _bump_write_epoch() -> None:
    global _write_epoch
    with _write_epoch_lock:
        _write_epoch += 1


# Read-only git subcommands. Deliberately a short list of ones that CANNOT write, rather than
# everything that happens to be a read today: `branch`, `symbolic-ref`, `stash` and friends all
# mutate depending on their arguments, and a cache is the wrong place to be clever about that.
_CACHEABLE_SUBCOMMANDS = frozenset(
    {"cat-file", "diff", "for-each-ref", "log", "ls-files", "merge-base", "rev-list", "rev-parse", "show-ref", "status"}
)


@contextlib.contextmanager
def read_cache() -> Iterator[None]:
    """De-duplicate repeated read-only git commands for the duration of the block.

    Re-entrant: nested scopes share the outermost one, and the cache is dropped when the
    outermost exits, so nothing survives the block.
    """
    depth = getattr(_read_cache_state, "depth", 0)
    if depth == 0:
        _read_cache_state.entries = {}
    _read_cache_state.depth = depth + 1
    try:
        yield
    finally:
        _read_cache_state.depth -= 1
        if _read_cache_state.depth == 0:
            _read_cache_state.entries = None


# Machine-managed scaffolding directories aGiTrack must never surface as untracked changes to
# stage: its own state (.agitrack) and the backends' local config/state (.claude, .codex,
# .opencode). These belong to the tooling, not the user's source, so they must never appear in
# the "stage these new files?" prompt (the user shouldn't be asked to commit an agent's folder).
_NEVER_STAGE_PREFIXES = (".agitrack/", ".claude/", ".codex/", ".opencode/")

# git's per-repo comment character while aGiTrack manages the repo. See
# GitRepo.ensure_comment_char_preserves_headings for why it is not "#" and not "auto".
_COMMENT_CHAR = ";"


def _PARTIAL_CLONE_KEYS(remote: str) -> list[str]:
    """Every config key a ``git fetch --filter=…`` writes behind the caller's back."""
    return [
        f"remote.{remote}.partialclonefilter",
        f"remote.{remote}.promisor",
        "core.repositoryformatversion",
    ]


def _is_scaffolding(path: str) -> bool:
    return path.startswith(_NEVER_STAGE_PREFIXES)


# git ``log`` flags that emit file CONTENT (line counts / diffs), so the walk needs blob
# objects -- which a blobless partial clone fetches lazily. A pickaxe (``-S``/``-G``) may be
# spelled glued to its term (``-Sneedle``), so those are matched by prefix.
_BLOB_CONTENT_FLAGS = frozenset({"--numstat", "--stat", "--shortstat", "-p", "--patch", "--patch-with-stat"})


def _git_read_needs_blobs(command: list[str]) -> bool:
    """Whether a ``git log``/``rev-list`` reads file content (needs blob objects)."""
    return any(a in _BLOB_CONTENT_FLAGS or a.startswith(("--stat=", "-S", "-G")) for a in command)


class GitRepo:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self._run(["git", "rev-parse", "--show-toplevel"])

    @classmethod
    def discover(cls, path: Path | str) -> "GitRepo":
        # Coerce first: the annotation says Path, but `cwd=` accepted a plain string for years,
        # so callers (and scripts) pass one. The existence checks below are Path methods, and
        # without this they turned a working call into `AttributeError: 'str' object has no
        # attribute 'exists'` — a worse failure than the one they were added to fix.
        path = Path(path)
        # Check the path OURSELVES before handing it to git as `cwd`. A missing directory made
        # subprocess raise FileNotFoundError, and a file made it raise NotADirectoryError (on
        # Windows both arrive as `[WinError 267] The directory name is invalid`) — raw OSErrors
        # that reached the user verbatim, leaking `PosixPath('…')` / a bare WinError with no path,
        # no mention of `--repo`, and no way to tell the two cases apart. One line from the good
        # sibling message that the not-a-repo case already had.
        if not path.exists():
            raise GitError(f"Not a Git repository: {path} (no such directory)")
        if not path.is_dir():
            raise GitError(f"Not a Git repository: {path} (that is a file, not a directory)")
        try:
            process = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path,
                **UTF8_TEXT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                **console_isolation_kwargs(),  # keep git off a console on Windows (proc.py)
            )
        except OSError as error:  # unreadable directory, permissions, a path git cannot chdir into
            raise GitError(f"Cannot read the Git repository at {path}: {error}") from error
        if process.returncode != 0:
            raise GitError(f"Not a Git repository: {path}")
        # fs_path, not Path: git printed UTF-8, and this string is about to be `resolve()`d and
        # handed to the OS as `cwd`. See proc.fs_path.
        return cls(fs_path(process.stdout.strip()))

    @classmethod
    def init(cls, path: Path) -> "GitRepo":
        """Initialize a new Git repository at ``path`` and seed an empty initial
        commit. aGiTrack runs every session in a worktree, which requires a valid HEAD;
        a fresh `git init` leaves an unborn branch, so the seed commit makes the
        repo usable immediately. Any existing files are committed afterwards by
        aGiTrack's normal pre-agent user-commit flow."""
        path = path.expanduser()
        path.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            ["git", "init"],
            cwd=path,
            **UTF8_TEXT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            **console_isolation_kwargs(),  # keep git off a console on Windows (proc.py)
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
        has an unborn branch with no commits, which aGiTrack cannot run on (every
        session is a worktree, and a worktree needs a valid HEAD). Seed an empty
        initial commit so the repo is usable; any pre-existing files are left
        untracked for aGiTrack's normal pre-agent user-commit flow. Returns True if a
        seed commit was created, False if HEAD was already born."""
        if self.has_commits():
            return False
        self._run(["git", "commit", "--allow-empty", "-m", "Initial commit"])
        return True

    def status_short(self) -> str:
        return self._run(["git", "status", "--short"]).stdout

    def status_short_ignored(self) -> str:
        """Porcelain short status that also lists git-ignored entries (``!! path``).

        Used to find worktree files that no commit will ever carry into the base
        directory — not just untracked/unstaged tracked edits, but also ignored
        build output or local data the agent may have created. A wholly-ignored
        directory is reported once as ``dir/`` rather than file-by-file."""
        return self._run(["git", "status", "--short", "--ignored"]).stdout

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
        """Stage every modification to already-tracked files — EXCEPT the agent scaffolding dirs.

        The untracked path has always filtered ``_NEVER_STAGE_PREFIXES``; this one did not, and
        `git add -u` has no such notion. In a repo that TRACKS ``.claude/`` — committing
        ``settings.json`` or a ``commands/`` dir is ordinary team practice — that meant aGiTrack's
        OWN edit to ``.claude/settings.local.json`` (the Stop/SessionStart hooks it installs) was
        swept into the user's history, complete with this machine's absolute venv path, in a file
        the whole team shares. Stopping then left the repo dirty with a change the user never
        made. "Nothing aGiTrack writes is ever staged" has to hold on both sides of tracked."""
        excludes = [f":(exclude){prefix.rstrip('/')}" for prefix in _NEVER_STAGE_PREFIXES]
        result = self._run(["git", "add", "-u", "--", ".", *excludes], check=False)
        if result.returncode != 0:
            # A pathspec excluding a directory that is ALSO git-ignored can error on older git.
            # Falling back to the unfiltered add is still better than failing the commit; the
            # scaffolding is then dropped from the snapshot by comparable_tree() anyway.
            self._run(["git", "add", "-u"])

    def staged_paths(self) -> list[str]:
        """Paths currently in the index (names only). Used to snapshot the index BEFORE a
        prompt stages things on the user's behalf, so backing out of that prompt can put the
        index back exactly as it was."""
        output = self._run(["git", "diff", "--cached", "--name-only"], check=False).stdout
        return [line for line in output.splitlines() if line]

    def unstage(self, paths: list[str]) -> None:
        """Drop ``paths`` from the index, leaving the WORKING TREE untouched — the undo for a
        staging the user then declined. Staging on their behalf and leaving it there is not
        harmless: during a merge conflict `git status` groups staged paths under "changes to be
        committed", which is precisely where a half-resolved file stops being visible as one
        still needing attention."""
        if not paths:
            return
        # `reset` is the portable spelling (`restore --staged` needs git >= 2.23), but it fails
        # on an unborn HEAD — where "unstage" means removing the path from the index entirely.
        result = self._run(["git", "reset", "-q", "HEAD", "--", *paths], check=False)
        if result.returncode != 0:
            self._run(["git", "rm", "--cached", "-q", "--", *paths], check=False)

    def discard_all_changes(self) -> None:
        """Reset the working tree to HEAD, discarding every uncommitted change:
        staged and unstaged tracked edits (``reset --hard``) plus untracked files
        (``clean -fd``). ``clean`` is run without ``-x``, so git-ignored paths —
        aGiTrack's own ``.agitrack/`` among them — are preserved. Destructive and
        unrecoverable; callers must confirm with the user first."""
        self._run(["git", "reset", "--hard", "HEAD"])
        self._run(["git", "clean", "-fd"])

    def stage_paths(self, paths: list[str]) -> None:
        if paths:
            self._run(["git", "add", "--", *paths])

    def untracked_files(self) -> list[str]:
        output = self._run(["git", "ls-files", "--others", "--exclude-standard"]).stdout
        return [line for line in output.splitlines() if line and not _is_scaffolding(line)]

    def untracked_entries(self) -> list[str]:
        """Untracked paths with WHOLLY-untracked directories collapsed to a single ``dir/``
        entry (``--directory``), instead of listing every file under them. Used by the
        intentionally-unstaged ("declined") flow so the user declines a new directory ONCE and
        files later added inside it stay covered — declining the per-file list (``untracked_files``)
        would re-prompt for each new file in an already-declined directory. A partially-tracked
        directory still lists its individual untracked files (git can't collapse it).

        ``--directory`` has a trap: it collapses and reports a directory that holds no TRACKED
        file as untracked even when everything inside it is git-IGNORED (by a nested ``.gitignore``
        like ``.venv/.gitignore`` with ``*``, or a rule that only names the dir's contents) — or
        when the directory is simply empty. Git only ignores a directory outright when a rule
        matches the directory itself, so a dir ignored solely from within slips through as
        "untracked" here even though ``git status`` shows nothing. That yields a phantom
        "stage these new files?" prompt for `.venv/`, build caches, etc. So each collapsed
        ``dir/`` is kept only when it actually contains a genuinely-untracked file — cross-checked
        against the accurate per-file list (which descends and honours every nested ``.gitignore``).

        Agent/tooling scaffolding (``_NEVER_STAGE_PREFIXES``: ``.agitrack/``, ``.claude/``,
        ``.codex/``, ``.opencode/``) is filtered out
        so the user is never asked to stage an agent's own folder."""
        output = self._run(["git", "ls-files", "--others", "--exclude-standard", "--directory"]).stdout
        entries = [line for line in output.splitlines() if line and not _is_scaffolding(line)]
        if not any(entry.endswith("/") for entry in entries):
            return entries  # no collapsed dirs -> nothing to second-guess, skip the extra git call
        # The per-file list honours nested/self-ignoring `.gitignore`s that `--directory` misses.
        files = self.untracked_files()
        return [entry for entry in entries if not entry.endswith("/") or any(path.startswith(entry) for path in files)]

    def ignored_files(self) -> list[str]:
        """Paths git ignores (per .gitignore) in the working tree — build output,
        local data, downloaded deps. A wholly-ignored directory collapses to a single
        ``dir/`` entry (``--directory``) so a caller copying the environment can copy it
        in one shot rather than walking thousands of files. aGiTrack's own ``.agitrack/``
        is never reported (copying it would recurse into the worktrees it holds)."""
        output = self._run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"]).stdout
        return [line for line in output.splitlines() if line and not line.startswith(".agitrack/")]

    def has_staged_changes(self) -> bool:
        return self._diff_has_changes(["git", "diff", "--cached", "--quiet"])

    def staged_changes(self) -> list[str]:
        """Human-readable list of the currently-staged changes, e.g. ``["A  new.py",
        "M  app.py", "D  old.py"]`` — the files a user commit would capture. Used to show the
        change set in the commit prompt."""
        output = self._run(["git", "diff", "--cached", "--name-status"]).stdout
        entries: list[str] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                entries.append(f"{parts[0]:<3}{parts[-1]}")
        return entries

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

    def cover_commit(
        self,
        message: str,
        *,
        first_parent: str,
        second_parent: str,
        include_staged: bool = False,
        tree: str | None = None,
    ) -> str:
        """Create a merge-shaped *cover* commit with parents ``(first_parent,
        second_parent)`` — the same shape as a GitHub PR merge commit. Used to
        attach aGiTrack's message on top of backend-made commits without amending
        them, since an amend changes their hashes and breaks references already
        published elsewhere (#58). The checked-out branch (or detached HEAD)
        moves to the new commit.

        By default the tree is ``second_parent``'s, so the cover is a pure
        metadata commit (working tree untouched). With ``include_staged`` the
        tree is the current index instead, folding any extra staged changes (e.g.
        files aGiTrack staged on top of the backend's commits) into the cover — so the
        cover's first-parent diff shows ALL the covered commits' changes plus the
        staged ones as one unit, instead of a plain commit that shows only the
        extra delta and hides the covered changes behind its single parent.

        An explicit ``tree`` overrides both (e.g. manual-commit mode's cover
        fallback, where the tree must equal ``first_parent``'s — the user's own
        commit already carries the agent's changes and the cover only rides the
        tracking metadata on top with the latent tip as second parent for
        provenance, adding no diff of its own)."""
        if tree is None:
            tree = (
                self._run(["git", "write-tree"]).stdout.strip()
                if include_staged
                else self.rev_parse(f"{second_parent}^{{tree}}")
            )
        sha = self._run(
            ["git", "commit-tree", tree, "-p", first_parent, "-p", second_parent],
            input_text=message,
        ).stdout.strip()
        self._run(["git", "reset", "--soft", sha])
        return self.short_sha(sha)

    def snapshot_worktree_tree(self) -> str:
        """Write the CURRENT working tree to a tree object without touching the real
        index or working tree, and return its SHA. Uses a throwaway index seeded from
        HEAD, so ``git add -A`` records the full working-tree delta the agent produced
        this turn — tracked edits, new untracked files, and deletions — minus the agent
        scaffolding dirs (``_NEVER_STAGE_PREFIXES``). This is how
        manual-commit mode captures a turn as a hidden latent commit while HEAD never
        moves and the user's own index/staging is left completely untouched."""
        scaffolding = [prefix.rstrip("/") for prefix in _NEVER_STAGE_PREFIXES]
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": index}
            # Seed from HEAD so `add -A` records the complete delta vs the branch, not
            # just what happens to be staged in the user's real index. check=False so an
            # unborn branch (no HEAD yet) simply starts from an empty index.
            self._run(["git", "read-tree", "HEAD"], env=env, check=False)
            self._run(["git", "add", "-A"], env=env)
            # Drop the agent scaffolding dirs from the snapshot whether they were tracked
            # or freshly added (``--ignore-unmatch`` so absent ones are a no-op). Done as a
            # separate step rather than an ``:(exclude)`` pathspec, which errors when the
            # dir is also git-ignored (a common setup — ``.agitrack/`` in a global excludes).
            self._run(
                ["git", "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", *scaffolding],
                env=env,
                check=False,
            )
            return self._run(["git", "write-tree"], env=env).stdout.strip()

    def comparable_tree(self, rev: str = "HEAD") -> str:
        """The tree of *rev* with the agent scaffolding dirs stripped, so it is directly
        COMPARABLE with :meth:`snapshot_worktree_tree`.

        Every "has the working tree changed?" test in manual/no-worktree/background mode is a
        comparison between a snapshot and some commit's tree. The snapshot deliberately drops
        every ``_NEVER_STAGE_PREFIXES`` entry; a raw ``rev^{tree}`` does not. So in a repo
        that TRACKS any of those — committing ``.claude/settings.json`` or a ``.claude/commands/``
        dir is ordinary practice — the two could never be equal and every one of those tests
        answered "dirty" forever: the daemon stopped covering the agent's own commits, stale and
        abandoned latent chains were never reset or pruned, a purely human commit made during an
        agent turn was stamped as agent work, and a turn that changed nothing was still recorded.
        Stripping the same paths from both sides is what makes the comparison mean what it says.

        Idempotent, so it is safe on a tree that is already a snapshot (the latent tips it is
        compared against are). Falls back to the raw tree when *rev* has no scaffolding at top
        level — the overwhelmingly common case, and one cheap ``ls-tree`` instead of three
        index writes."""
        scaffolding = [prefix.rstrip("/") for prefix in _NEVER_STAGE_PREFIXES]
        try:
            top = set(self._run(["git", "ls-tree", "--name-only", rev]).stdout.split("\n"))
        except Exception:
            top = set(scaffolding)  # unreadable ⇒ take the thorough path rather than guess
        if not top.intersection(scaffolding):
            return self.rev_parse(f"{rev}^{{tree}}")
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": index}
            self._run(["git", "read-tree", rev], env=env)
            self._run(
                ["git", "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", *scaffolding],
                env=env,
                check=False,
            )
            return self._run(["git", "write-tree"], env=env).stdout.strip()

    def tree_diff_names(self, base: str, tree: str) -> list[str]:
        """The paths that differ between two trees (or revs), in git's own order.

        The counterpart of :meth:`staged_paths` for the commit paths that never touch the
        index: every ``--no-worktree`` mode records its turn as a latent commit built from a
        working-tree snapshot, so "what will this commit carry?" has to be answered by
        comparing trees. Never raises — an unreadable rev answers "nothing known", which
        callers treat as "say less" rather than as an error."""
        try:
            output = self._run(["git", "diff", "--name-only", base, tree], check=False).stdout
        except Exception:
            return []
        return [line for line in output.splitlines() if line]

    def commit_tree(self, tree: str, *, parents: list[str], message: str) -> str:
        """Create a commit object for ``tree`` with the given ``parents`` and return its
        FULL SHA. Unlike ``commit``/``cover_commit`` this moves no ref and touches neither
        HEAD nor the working tree — the caller points a ref at the result (manual-commit
        mode chains latent commits onto ``refs/agitrack/manual/<id>`` this way)."""
        args = ["git", "commit-tree", tree]
        for parent in parents:
            if parent:
                args += ["-p", parent]
        return self._run(args, input_text=message).stdout.strip()

    def parents(self, ref: str = "HEAD") -> list[str]:
        output = self._run(["git", "rev-list", "--parents", "-1", ref]).stdout.split()
        return output[1:]

    def short_sha(self, ref: str = "HEAD") -> str:
        return self._run(["git", "rev-parse", "--short", ref]).stdout.strip()

    def commit_message(self, ref: str = "HEAD") -> str:
        return self._run(["git", "log", "-1", "--format=%B", ref], check=False).stdout

    def commit_timestamp(self, ref: str = "HEAD") -> int | None:
        """Committer date of *ref* as epoch seconds, or None when it can't be read.

        Used to decide whether a commit could possibly belong to a given AI turn: one
        created before the user's prompt even arrived is somebody else's work.
        """
        output = self._run(["git", "log", "-1", "--format=%ct", ref], check=False).stdout.strip()
        try:
            return int(output)
        except ValueError:
            return None

    def diff_range(self, base: str, head: str) -> str:
        return self._run(["git", "diff", f"{base}..{head}"], check=False).stdout

    def show_commit(self, ref: str, *, max_bytes: int = 400_000) -> tuple[str, bool]:
        """The file diffs a commit introduced — a diffstat followed by the unified patch —
        for the dashboard's local diff view (so the dashboard is fully usable off GitHub).

        ``--first-parent`` makes a merge or cover commit show its change against the mainline
        parent (the AI work a cover commit accounts for) instead of the near-empty combined
        diff; it's a no-op for ordinary single-parent commits, and a root commit shows as
        all-additions. All local: it reads blobs already in the clone, never the network.

        Returns ``(patch, truncated)`` — the patch is capped at ``max_bytes`` so one enormous
        commit can't produce an unbounded response; ``truncated`` is True when it was cut."""
        out = self._run(
            ["git", "show", "--no-color", "--first-parent", "--format=", "--stat", "--patch", ref],
            check=False,
        ).stdout
        if len(out) > max_bytes:
            return out[:max_bytes], True
        return out, False

    # --- branches / worktrees / merges (used by concurrent-session support) ---

    def current_branch(self) -> str:
        """The branch name, ``"HEAD"`` when detached, or the branch HEAD *points at* on an
        UNBORN branch — a fresh ``git init`` with no commits, the very first state a new user
        can be in.

        Never raises. ``git rev-parse --abbrev-ref HEAD`` fails outright on an unborn branch
        (``fatal: ambiguous argument 'HEAD'``), and because this ran with ``check=True`` that
        GitError escaped as a raw traceback out of ``agitrack -d text`` — while ``-d html`` was
        worse, reporting success and then serving a page that spun forever while every ``/data``
        request crashed server-side."""
        process = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if process.returncode == 0:
            return process.stdout.strip()
        unborn = self._run(["git", "symbolic-ref", "--short", "HEAD"], check=False)
        return unborn.stdout.strip() if unborn.returncode == 0 else ""

    def rev_parse(self, ref: str) -> str:
        return self._run(["git", "rev-parse", ref]).stdout.strip()

    def hooks_dir(self) -> Path:
        """The shared hooks directory (under the common git dir, so it covers the base
        repo and all its linked worktrees). Used to install the base-commit guard hook."""
        common = self._run(["git", "rev-parse", "--git-common-dir"], check=False).stdout.strip() or ".git"
        base = Path(common)
        if not base.is_absolute():
            base = self.repo / base
        return (base / "hooks").resolve()

    def core_hooks_path(self) -> str | None:
        """The configured ``core.hooksPath`` (which, when set, makes git ignore the
        default hooks dir), or ``None`` if unset."""
        value = self._run(["git", "config", "--get", "core.hooksPath"], check=False).stdout.strip()
        return value or None

    def ensure_comment_char_preserves_headings(self) -> bool:
        """Make git's comment character something other than ``#`` for THIS repo, so editing an
        aGiTrack commit message doesn't silently destroy it. Returns True when it set the config.

        Every aGiTrack commit body is structured with ``#`` headings — ``# Interaction Trace``,
        ``## User`` / ``## Agent``, ``# aGiTrack Metadata``. With git's default
        ``core.commentChar = "#"``, any message the user opens in an EDITOR (``git commit --amend``,
        ``rebase -i`` reword, ``git commit -e``) has every one of those lines stripped as a comment
        on the way out — the trace runs together and the metadata block disappears, so the commit
        reads as untracked to the dashboard/story. It is silent and unrecoverable from the new
        commit alone.

        An EXPLICIT character, not ``auto``. ``auto`` reads better — it picks a character the
        message does not already use at line start — but git 2.54 deprecates it: every subsequent
        `git commit` prints a deprecation warning plus 8-9 hint lines, FOREVER, including long
        after the user has removed aGiTrack entirely, and the hint tells them to run
        `git config unset core.commentChar`, i.e. to undo aGiTrack's own heading guard. It breaks
        outright in Git 3.0. Six independent live-test scenarios found it.

        ``;`` is the replacement: a line-initial ``;`` is rare in prose and in the languages
        aGiTrack's traces quote, where a line-initial ``#`` is near-universal (markdown headings,
        shell, Python, YAML) — which is the whole reason the default is unusable here.

        Set with ``--local``: scoped to the repo aGiTrack manages, never the user's global config.
        Idempotent, and it never overrides a value the user (or another tool) already chose.
        ``agitrack.commentchar`` records that WE set it, so teardown can put the repo back exactly
        as it found it and never unset a value that was the user's."""
        try:
            existing = self._run(["git", "config", "--local", "--get", "core.commentChar"], check=False).stdout.strip()
            ours = self._run(["git", "config", "--local", "--get", "agitrack.commentchar"], check=False).stdout.strip()
            if existing:
                # Migrate the deprecated `auto` we ourselves wrote in an earlier version; anything
                # else in there is the user's and stays untouched.
                if existing == "auto" and ours:
                    self._run(["git", "config", "--local", "core.commentChar", _COMMENT_CHAR], check=False)
                return False
            if self._run(["git", "config", "--get", "core.commentChar"], check=False).stdout.strip():
                return False  # a global/system value the user picked already protects (or is theirs)
            if self._run(["git", "config", "--local", "core.commentChar", _COMMENT_CHAR], check=False).returncode != 0:
                return False
            self._run(["git", "config", "--local", "agitrack.commentchar", _COMMENT_CHAR], check=False)
            return True
        except Exception:
            return False  # never let a config tweak block startup

    def restore_comment_char(self) -> bool:
        """Undo :meth:`ensure_comment_char_preserves_headings`, if aGiTrack was the one that set
        it. Returns True when something was unset.

        Teardown has to include this: the config was written into every tracked repo and never
        removed — not by ``-b stop``, not by ``--remove-hooks`` — so it outlived a full uninstall.
        The ``agitrack.commentchar`` marker is what makes this safe: a value the user chose (or
        one another tool set afterwards) is left alone."""
        try:
            ours = self._run(["git", "config", "--local", "--get", "agitrack.commentchar"], check=False).stdout.strip()
            if not ours:
                return False
            current = self._run(["git", "config", "--local", "--get", "core.commentChar"], check=False).stdout.strip()
            self._run(["git", "config", "--local", "--unset", "agitrack.commentchar"], check=False)
            if current and current != ours:
                return False  # somebody else owns it now
            return self._run(["git", "config", "--local", "--unset", "core.commentChar"], check=False).returncode == 0
        except Exception:
            return False

    def unlanded_commits(self, ref: str) -> list[str]:
        """Commits on *ref* that no branch, tag or remote-tracking ref contains — oldest first.

        aGiTrack's manual mode keeps each turn as a hidden *latent* commit that exists ONLY on
        ``refs/agitrack/manual/<session>``; folding it into the user's commit puts its content on
        a branch and advances the ref there. A plain ``HEAD..<ref>`` walk then mis-reports those
        landed commits as pending again the moment HEAD moves to a branch that doesn't contain
        them (switch branches after a commit and the previous branch's turns fold a SECOND time —
        trace duplicated, tokens double-counted). "Reachable from no branch" is the exact test for
        "not yet folded anywhere", and it costs one rev-list.
        """
        result = self._run(
            ["git", "rev-list", "--reverse", ref, "--not", "--branches", "--tags", "--remotes"],
            check=False,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_refs(self, prefix: str) -> list[str]:
        """Full names of the refs under *prefix* (e.g. ``refs/agitrack/manual/``), or ``[]``."""
        output = self._run(["git", "for-each-ref", "--format=%(refname)", prefix], check=False).stdout
        return [line.strip() for line in output.splitlines() if line.strip()]

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

    def list_remote_branches(self) -> list[str]:
        """Remote-tracking branches, ``remote/branch`` (e.g. ``origin/feature-x``).

        Deliberately NOT part of :meth:`list_branches`: everything that CHECKS OUT, merges or
        deletes a branch means a local one, and handing those callers a read-only remote ref
        would be a new class of bug. Only readers want these — the dashboard, whose branch
        selector must offer a branch the user has fetched but not checked out, because that is
        exactly what "a new branch just landed in this repo" looks like on disk.

        ``origin/HEAD`` is dropped: it is a symbolic alias for the default branch, so listing it
        offers the same branch twice under two names."""
        output = self._run(
            ["git", "for-each-ref", "--format=%(refname:short)%09%(symref)", "refs/remotes/"],
            check=False,
        ).stdout
        names = []
        for line in output.splitlines():
            name, _, symref = line.partition("\t")
            if name and not symref:  # a symref is origin/HEAD -> origin/main; not a branch of its own
                names.append(name)
        return names

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

    def worktree_move(self, old_path: str, new_path: str) -> None:
        # Move a worktree's directory and update git's admin record. The worktree
        # must not be in use (no process with its cwd there) or git refuses.
        self._run(["git", "worktree", "move", old_path, new_path])

    def worktree_remove(self, path: str, *, force: bool = False) -> None:
        command = ["git", "worktree", "remove"]
        if force:
            command.append("--force")
        command.append(path)
        self._run(command)

    def worktree_prune(self) -> None:
        # Drop administrative entries for worktrees whose directories are gone.
        self._run(["git", "worktree", "prune"], check=False)

    def repair_worktrees(self, *paths: str) -> None:
        # Re-link worktree administrative files after the worktrees' directories
        # moved (e.g. the .agit → .agitrack state-dir migration). A moved worktree
        # must be named explicitly — ``git worktree repair`` with no args can't find
        # a worktree whose directory it no longer knows about, so pass the NEW paths.
        # Best-effort; never raises.
        self._run(["git", "worktree", "repair", *paths], check=False)

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
        could drop commits. Lets aGiTrack integrate into the base branch even when the
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

    def arrived_from_elsewhere(self, sha: str) -> bool:
        """Whether *sha* reached this branch from somewhere else rather than being
        committed here.

        Used to decide what an aGiTrack turn may claim as the agent's own work. A
        ``git merge main`` fast-forward, a pull, or a PR merged on GitHub drops commits
        between the cover anchor and HEAD that the agent never made; covering those
        misattributes other people's work (a release PR once landed in covered_commits).

        The reflog answers this exactly: it records HOW the ref moved, so a commit
        created here has a ``commit:`` entry naming it, while one that arrived shows up
        only under ``merge``/``pull``/``reset``. When no reflog is available (a fresh
        clone, an expired entry) fall back to the committer identity — GitHub, a CI bot
        or a teammate cannot be the agent running in this repository.
        """
        try:
            created = self._reflog_created_here()
            if created is not None:
                return sha not in created
            committer = self._run(["git", "log", "-1", "--format=%cn <%ce>", sha], check=False).stdout.strip()
            name = self._run(["git", "config", "user.name"], check=False).stdout.strip()
            email = self._run(["git", "config", "user.email"], check=False).stdout.strip()
            if committer and name and email:
                return committer != f"{name} <{email}>"
        except Exception:
            return False  # never block covering on a failed probe
        return False

    def _reflog_created_here(self) -> set[str] | None:
        """SHAs the CURRENT BRANCH's reflog records as created by a commit on it, or None
        when there is no reflog to read.

        Deliberately the branch's reflog and not HEAD's: HEAD's spans branch switches, so a
        commit made on some other local branch and later merged in would read as "created
        here" and be claimed as the agent's. A branch reflog only ever shows moves of that
        branch, and it is shared with linked worktrees, so it still sees what an agent
        committed in its own worktree.
        """
        ref = self.current_branch() or "HEAD"  # detached: HEAD's log is all there is
        result = self._run(["git", "reflog", "show", "--format=%H %gs", ref], check=False)
        if result.returncode != 0:
            return None
        created: set[str] = set()
        seen_any = False
        for line in result.stdout.splitlines():
            sha, _, subject = line.partition(" ")
            if not sha:
                continue
            seen_any = True
            # "commit:", "commit (amend):", "commit (initial):" — anything else moved the
            # branch to a commit that already existed (merge, pull, reset, checkout).
            if subject.startswith("commit"):
                created.add(sha)
        return created if seen_any else None

    def notes_add(self, commit: str, message: str, *, namespace: str = "agitrack") -> None:
        self._run(["git", "notes", "--ref", namespace, "add", "-f", "-m", message, commit])

    def notes_show(self, commit: str, *, namespace: str = "agitrack") -> str | None:
        result = self._run(["git", "notes", "--ref", namespace, "show", commit], check=False)
        return result.stdout if result.returncode == 0 else None

    def notes_list(self, *, namespace: str = "agitrack") -> list[tuple[str, str]]:
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

    # --- low-level object/ref plumbing (shared-session storage, issue #55) ------
    # These build and move a custom ref (refs/agitrack/shared-sessions) entirely in
    # the object database, never touching the working tree or the real index.

    def ref_exists(self, ref: str) -> bool:
        return self._run(["git", "rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0

    def ref_sha(self, ref: str) -> str | None:
        result = self._run(["git", "rev-parse", "--verify", "--quiet", ref], check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None

    def write_blob(self, content: str) -> str:
        """Write *content* as a blob into the object db; returns its SHA."""
        return self._run(["git", "hash-object", "-w", "--stdin"], input_text=content).stdout.strip()

    def read_tree_paths(self, ref: str) -> dict[str, str]:
        """Map ``path -> blob SHA`` for every file reachable from ``ref`` (a tree
        or commit). Empty when the ref doesn't exist."""
        if not self.ref_exists(ref):
            return {}
        output = self._run(["git", "ls-tree", "-r", "-z", ref], check=False).stdout
        entries: dict[str, str] = {}
        for record in output.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "blob":
                entries[path] = parts[2]
        return entries

    def read_ref_blob(self, ref: str, path: str) -> str | None:
        """Contents of ``path`` within ``ref``'s tree, or None if absent."""
        result = self._run(["git", "cat-file", "-p", f"{ref}:{path}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def write_tree_from(self, entries: dict[str, str]) -> str:
        """Build a tree containing exactly ``entries`` (``path -> blob SHA``) using
        a throwaway index, so the real index and working tree are untouched.
        Returns the tree SHA (the empty tree when ``entries`` is empty)."""
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": index}
            for path, blob in entries.items():
                self._run(
                    ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                    env=env,
                )
            return self._run(["git", "write-tree"], env=env).stdout.strip()

    def commit_tree_orphan(self, tree: str, message: str) -> str:
        """Commit ``tree`` with NO parents — a standalone, history-free snapshot.
        Rewriting a ref to such commits keeps only the latest copy (old objects
        become unreferenced and are GC'd)."""
        return self._run(["git", "commit-tree", tree, "-F", "-"], input_text=message).stdout.strip()

    def update_ref(self, ref: str, sha: str) -> None:
        self._run(["git", "update-ref", ref, sha])

    def delete_ref(self, ref: str) -> None:
        self._run(["git", "update-ref", "-d", ref], check=False)

    def remote_exists(self, name: str = "origin") -> bool:
        return name in self._run(["git", "remote"], check=False).stdout.split()

    def remote_names(self) -> list[str]:
        """Every configured remote, in git's own order (``origin`` first in a normal clone)."""
        return self._run(["git", "remote"], check=False).stdout.split()

    def remote_head_branches(self, remote: str = "origin", *, timeout: float = 20.0) -> set[str] | None:
        """The branch names ``remote`` currently HAS, or None when it could not be asked.

        This is the only way to learn that a branch was DELETED upstream. Git never removes a
        remote-tracking ref on a plain ``git fetch`` — only ``--prune`` does — so a repository
        accumulates ``refs/remotes/origin/*`` for branches that were merged and deleted months
        ago, and anything listing those refs is listing branches that no longer exist.

        None (not an empty set) is the answer for "could not ask": no such remote, offline, a
        credential we do not have, a stalled connection. The caller must treat that as "I know
        nothing about this remote" and leave its branches alone — hiding a branch because the
        network was down would be a worse failure than showing a stale one.

        ``GIT_TERMINAL_PROMPT=0`` so a remote needing credentials fails fast instead of blocking
        on a prompt no one is there to answer, and the timeout bounds a connection that hangs:
        this runs on a background refresh for a page that must stay responsive."""
        try:
            result = self._run(
                ["git", "ls-remote", "--heads", "--quiet", remote],
                check=False,
                env={"GIT_TERMINAL_PROMPT": "0"},
                timeout=timeout,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        names: set[str] = set()
        for line in result.stdout.splitlines():
            _sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                names.add(ref[len("refs/heads/") :].strip())
        return names

    def fetch_ref(
        self,
        refspec: str,
        *,
        remote: str = "origin",
        filter_blobs: str | None = None,
        refetch: bool = False,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> bool:
        """Fetch a single refspec (e.g. ``+refs/agitrack/x:refs/agitrack/x``). Returns
        True on success; False on any failure (offline, no such ref yet, …).

        With ``filter_blobs`` (e.g. ``blob:limit=16k``) the fetch skips large blobs
        — used to pull a shared-session ref's small manifests for listing without
        downloading every transcript; the transcripts are fetched on demand. The
        one-off partial fetch's persisted filter is then dropped so the user's
        normal ``git fetch`` stays full.

        With ``refetch`` git re-downloads every object reachable from the ref as a
        fresh clone would (ignoring what's already local) — used to backfill blobs a
        prior partial fetch omitted, since a plain ref fetch won't (the ref is already
        at the tip, so it transfers nothing).

        ``cancel`` (a ``threading.Event``) stops the fetch the moment it is set —
        the git subprocess is killed, not merely abandoned — so a user who cancels
        (or exits) truly stops the network work rather than leaving it running."""
        cmd = ["git", "fetch"]
        if refetch:
            cmd.append("--refetch")
        if filter_blobs:
            cmd.append(f"--filter={filter_blobs}")
        cmd += [remote, refspec]
        # Never block on an interactive credential prompt — these ref syncs run in
        # the background (and on the exit path), where a prompt would hang with no
        # way to answer. Cached creds / credential helpers still work. The timeout
        # bounds a stalled fetch; cancel kills it immediately on user request.
        # A `--filter` fetch does not just set `partialclonefilter`: git also writes
        # `remote.<r>.promisor=true` and bumps `core.repositoryformatversion` to 1 — and it does
        # NOT write the matching `extensions.partialClone`, so the repo is left in a state git
        # itself would not have produced. Only the filter was ever undone, so a documented
        # READ-ONLY report (`--backtrace text`) permanently mutated the user's git config,
        # reproduced from pristine; in a submodule setup it edited the vendored dependency's own
        # config. Snapshot all three and put them back exactly as they were — including doing
        # nothing at all in a repo that genuinely IS a partial clone.
        restore = self._config_snapshot(_PARTIAL_CLONE_KEYS(remote)) if filter_blobs else None
        ok = self._run_bounded(cmd, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=timeout, cancel=cancel) == 0
        if restore is not None:
            self._restore_config(restore)
        return ok

    def is_partial_clone(self) -> bool:
        """Whether this clone fetches objects lazily from a promisor remote — the state
        ``git clone --filter=blob:none`` (or ``--filter=blob:limit=…``) leaves behind.

        It is the difference between two answers to "how many lines did this commit change?".
        Counting from LOCAL blobs is exact on an ordinary clone and can be remembered forever;
        on a partial clone it is only a floor, because the blobs a big commit touched may still
        live on the remote, so the commits actually being displayed are recounted with fetching
        allowed. Asking git which kind of repo this is lets the ordinary case skip that recount
        entirely (see metrics.collect)."""
        found = self._run(
            ["git", "config", "--get-regexp", r"^(remote\..*\.promisor|extensions\.partialclone)$"],
            check=False,
        )
        return bool(found.stdout.strip())

    def _config_snapshot(self, keys: list[str]) -> dict[str, str | None]:
        """Each key's current local value, or None when unset. Local scope only: this is for
        restoring config aGiTrack is about to disturb, and a global/system value is not ours."""
        snapshot: dict[str, str | None] = {}
        for key in keys:
            result = self._run(["git", "config", "--local", "--get", key], check=False)
            snapshot[key] = result.stdout.strip() if result.returncode == 0 else None
        return snapshot

    def _restore_config(self, snapshot: dict[str, str | None]) -> None:
        """Put every key back to its snapshotted value (unsetting the ones that were absent)."""
        for key, value in snapshot.items():
            if value is None:
                self._run(["git", "config", "--local", "--unset-all", key], check=False)
            else:
                self._run(["git", "config", "--local", key, value], check=False)

    def resolve_blob_oid(self, ref: str, path: str) -> str | None:
        """The blob id at ``ref:path``, read from the (present) tree — so it resolves even when
        the blob CONTENT is a partial-clone placeholder not yet fetched. None if absent."""
        result = self._run(["git", "rev-parse", f"{ref}:{path}"], check=False)
        oid = result.stdout.strip()
        return oid if result.returncode == 0 and oid else None

    def has_object_local(self, oid: str) -> bool:
        """Whether ``oid`` is present in the LOCAL object store, without triggering a
        partial-clone lazy fetch (``GIT_NO_LAZY_FETCH`` keeps it offline). Lets a caller decide
        whether a blob still needs downloading without paying a network round-trip when it
        doesn't. (On git < 2.36 the env is ignored and a missing promised object may lazy-fetch
        here instead — harmless: it just gets fetched a step earlier.)"""
        result = self._run(["git", "cat-file", "-e", oid], check=False, env={"GIT_NO_LAZY_FETCH": "1"})
        return result.returncode == 0

    def fetch_object(
        self,
        oid: str,
        *,
        remote: str = "origin",
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> bool:
        """Fetch a single object by id — used to backfill a transcript blob a partial-clone
        listing fetch omitted (a plain ref fetch won't, as the ref is already at the tip).
        Returns True on success. Bounded + non-interactive like ``fetch_ref``; fails (False)
        on a remote that disallows fetching by object id, so the caller can fall back."""
        cmd = ["git", "fetch", remote, oid]
        return self._run_bounded(cmd, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=timeout, cancel=cancel) == 0

    def _run_bounded(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run *command* as a subprocess that can be stopped early — when *cancel*
        (a ``threading.Event``-like object with ``is_set()``) is set, or *timeout*
        elapses. The process is terminated (then killed) so the network work really
        stops. Returns the exit code, or 124 when cancelled/timed out."""
        _bump_write_epoch()  # fetch/push moves refs; no earlier read can be trusted after it
        # Discard output: callers only use the exit code, and piping a long fetch's
        # progress (stderr) without reading it would fill the pipe buffer and wedge
        # the process — the opposite of "bounded".
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
            # A cancellable network git (fetch/push) run from the console-less background
            # daemon would flash its own console window on Windows without this (proc.py).
            **console_isolation_kwargs(),
        )
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                process.wait(timeout=0.1)
                return process.returncode
            except subprocess.TimeoutExpired:
                pass
            stop = (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline)
            if stop:
                self._terminate_process(process)
                return 124

    @staticmethod
    def _terminate_process(process: "subprocess.Popen") -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def push_ref(
        self,
        refspec: str,
        *,
        remote: str = "origin",
        force_with_lease: str | None = None,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> tuple[bool, str]:
        """Push a refspec. Returns ``(ok, stderr)`` — stderr lets the caller spot a
        non-fast-forward/stale rejection and retry after re-fetching.

        ``timeout`` bounds a stalled push and ``cancel`` (a ``threading.Event``) stops
        it the instant it is set — the git subprocess is killed, not abandoned — so a
        user who cancels a manual share truly stops the upload. When neither is given
        the push is a plain blocking call (the background/exit paths)."""
        command = ["git", "push"]
        if force_with_lease is not None:
            command.append(f"--force-with-lease={force_with_lease}" if force_with_lease else "--force-with-lease")
        command += [remote, refspec]
        # GIT_TERMINAL_PROMPT=0: fail fast on a missing credential rather than
        # blocking on a prompt no one can answer (e.g. the synchronous exit-path
        # share). Cached creds / credential helpers are unaffected.
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if timeout is None and cancel is None:
            result = self._run(command, check=False, env=env)
            return result.returncode == 0, result.stderr
        code, stderr = self._run_bounded_io(command, env=env, timeout=timeout, cancel=cancel)
        return code == 0, stderr

    def _run_bounded_io(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Like :meth:`_run_bounded` but captures stderr for the caller — used for a
        cancellable ``git push``, whose output is small (a few status lines), so the
        pipe can't fill and wedge the process the way an unread fetch progress stream
        would. Returns ``(exit_code, stderr)``; ``(124, partial-stderr)`` when
        cancelled or timed out. Retrying ``communicate`` after a ``TimeoutExpired``
        does not lose output (documented behaviour), so the poll loop is safe."""
        _bump_write_epoch()  # a push moves refs; no earlier read can be trusted after it
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **UTF8_TEXT,
            env={**os.environ, **env} if env else None,
            # A cancellable network git (push) run from the console-less background daemon
            # would flash its own console window on Windows without this (proc.py).
            **console_isolation_kwargs(),
        )
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                _out, err = process.communicate(timeout=0.1)
                return process.returncode, err or ""
            except subprocess.TimeoutExpired:
                pass
            stop = (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline)
            if stop:
                self._terminate_process(process)
                try:
                    _out, err = process.communicate(timeout=2)
                except Exception:
                    err = ""
                return 124, err or ""

    def unreachable_commits(self) -> list[str]:
        """SHAs of commits reachable from no ref (and no reflog) — dangling objects
        git's auto-gc would eventually drop. Used to sweep stale shared-session
        snapshots; the caller filters to genuine sessions before deleting anything."""
        result = self._run(
            ["git", "fsck", "--unreachable", "--no-reflogs", "--connectivity-only", "--no-progress"],
            check=False,
        )
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "unreachable" and parts[1] == "commit":
                commits.append(parts[2])
        return commits

    def delete_orphaned_objects(self, old_sha: str | None) -> int:
        """Immediately delete the loose objects reachable from ``old_sha`` but from
        no current ref — the previous shared-session snapshot's commit/tree/blobs
        after the ref was rewritten off it. Targeted (it only ever removes objects
        exclusive to ``old_sha``, never anything another ref reaches), so it's safe
        to run alongside aGiTrack's other git writes and doesn't wait for git's auto-gc.
        Returns the count removed. Best-effort; never raises.

        Uses an explicit object-set difference: ``git rev-list --objects A --not B``
        does NOT reliably drop trees/blobs A shares with B, which would delete
        objects the current ref still needs — so we diff the two object sets here."""
        if not old_sha:
            return 0
        old = self._run(["git", "rev-list", "--objects", old_sha], check=False)
        if old.returncode != 0:
            return 0
        old_shas = {line.split(" ", 1)[0] for line in old.stdout.splitlines() if line}
        if not old_shas:
            return 0
        kept = self._run(["git", "rev-list", "--objects", "--all"], check=False)
        kept_shas = {line.split(" ", 1)[0] for line in kept.stdout.splitlines() if line}
        orphaned = old_shas - kept_shas  # in old's snapshot, reachable from no ref
        raw = self._run(["git", "rev-parse", "--git-path", "objects"], check=False).stdout.strip()
        if not raw:
            return 0
        objects = fs_path(raw) if os.path.isabs(raw) else (self.repo / raw)
        removed = 0
        for sha in orphaned:
            if len(sha) < 4 or any(ch not in "0123456789abcdef" for ch in sha):
                continue
            loose = objects / sha[:2] / sha[2:]
            try:
                loose.unlink()  # packed objects have no loose file (no-op via OSError)
                removed += 1
            except OSError:
                pass
        return removed

    def root_commit(self) -> str | None:
        """The repo's first (root) commit SHA — a clone-stable repo fingerprint.
        None for an unborn repo. Picks the earliest if history has several roots."""
        output = self._run(["git", "rev-list", "--max-parents=0", "HEAD"], check=False).stdout.split()
        return output[-1] if output else None

    def _run(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_lazy_fetch: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # THE PROGRAM IS ALWAYS `git`, and this is the one place that can promise it. Every
        # caller builds its argv beginning with the literal, and nothing here ever uses a shell
        # (no `shell=True` anywhere in aGiTrack), so a value flowing into `command` is an
        # ARGUMENT to git — never the thing that gets executed, and never shell syntax. Asserting
        # it makes that a checked invariant instead of a convention that happens to hold: a
        # future caller passing a configured or user-supplied program name fails loudly here
        # rather than quietly turning this into a program-execution sink. (What is left to guard
        # after this is git's OWN option parsing — a ref or path that begins with `-` being read
        # as a flag — which callers handle by separating revisions from pathspecs with `--`.)
        if not command or command[0] != "git":
            raise ValueError(f"GitRepo._run only runs git; got {command[:1] or ['(empty)']}")
        # History reads silently truncate on a busy repo, two ways -- both because aGiTrack
        # commits every turn, which constantly fires background ``git gc --auto``:
        #
        #  1. STALE COMMIT-GRAPH. gc writes a commit-graph; a later repack moves the objects
        #     it indexes, so the graph's position lookups no longer match the store and a walk
        #     aborts NON-ZERO -- "commit <sha> exists in commit-graph but not in the object
        #     database" -- though the object is present.
        #  2. LAZY (PROMISOR) FETCH on a partial/blobless clone (``git clone --filter=blob:none``).
        #     When a walk momentarily can't find an object locally -- e.g. mid-repack, when the
        #     pack set is being rewritten -- git tries to LAZY-FETCH it from the promisor remote.
        #     For aGiTrack's own local-only commits (turn branches, cover/shared-session objects)
        #     the remote answers "not our ref" and the read aborts ("Could not read <oid>;
        #     Failed to traverse parents"), even though the object is in the local store. The
        #     count then flaps (e.g. 485 one read, 289 the next) and the dashboard shows a
        #     truncated -- or empty -- log. Small/idle repos never gc enough to hit either.
        #
        # Every read passes check=False, so the aborted/partial output is used as-is. Disabling
        # the commit-graph makes git walk the store directly; disabling lazy fetch makes a walk
        # use the LOCAL objects (all present) instead of attempting a doomed network fetch. Both
        # are correct and negligibly slower at the sizes aGiTrack tracks. Lazy fetch is left ON
        # for reads that need file CONTENT (``--numstat`` line counts, ``-p``/``--stat`` diffs)
        # UNLESS the caller passes ``allow_lazy_fetch=False``: on a blobless clone such a read
        # legitimately fetches absent blobs, so suppressing it would zero the numbers -- but the
        # caller may *want* that. The dashboard's full-history numstat scan opts out, because
        # fetching every historical blob on every poll makes a big blobless clone's dashboard
        # hang for tens of seconds (and the interrupted fetches litter ``.git`` with tmp packs);
        # it instead counts from the LOCAL blobs and fetches only the page actually displayed.
        #
        # Lazy fetch is suppressed via the GIT_NO_LAZY_FETCH=1 ENVIRONMENT VARIABLE, not the
        # ``-c fetch.disableLazyFetch=true`` config: measured on git 2.50.1 (Apple), the config
        # is NOT honoured for a ``git log --numstat`` walk (it still fetches every blob), whereas
        # the env var reliably keeps the walk to local objects. The config is set too, as a
        # harmless second line of defence on git builds where it does take effect.

        # De-duplicate repeated reads, and invalidate on anything that can write. Off unless a
        # caller opened a `read_cache()` scope; see the module header for why the Enter path
        # needs it. Only the plain form is cacheable — a call carrying stdin, a custom env or a
        # timeout is doing something specific enough that it should just run.
        cache_key: tuple | None = None
        cacheable_read = (
            input_text is None
            and not env
            and timeout is None
            and len(command) >= 2
            and command[0] == "git"
            and command[1] in _CACHEABLE_SUBCOMMANDS
        )
        if not cacheable_read:
            # Everything else invalidates. That over-invalidates slightly — a read carrying a
            # custom env or a timeout is not a write — and it is the right direction to err in:
            # the cost is one repeated read, where the cost of the opposite mistake is acting on
            # a tree that has changed.
            _bump_write_epoch()
        elif getattr(_read_cache_state, "depth", 0):
            cache_key = (str(self.repo), tuple(command), check, allow_lazy_fetch)
            epoch, cached = (_read_cache_state.entries or {}).get(cache_key, (None, None))
            if cached is not None and epoch == _write_epoch:
                return cached
        extra_env: dict[str, str] = dict(env) if env else {}
        flags: list[str] = []
        if command and command[0] == "git":
            # PATHS MUST COME BACK VERBATIM. With git's default ``core.quotePath=true`` every
            # listing command (``ls-files``, ``status --short``, ``diff --name-only``) prints a
            # path containing non-ASCII characters C-QUOTED — `"my file \303\251\344\270\255.txt"`,
            # complete with the surrounding double quotes. aGiTrack feeds those names straight
            # back to ``git add --``, which then fails ("pathspec ... did not match any files")
            # and, because staging fails, the whole agent turn is LOST: no commit, no trace, no
            # tokens. Any agent that creates a file with an accented or CJK name hit this. Turning
            # quoting off makes git emit the real UTF-8 bytes, which round-trip.
            flags += ["-c", "core.quotePath=false"]
            if _IS_WINDOWS:
                # WINDOWS LONG PATHS. Without `core.longpaths` — the state every real user is in,
                # since aGiTrack never set it — git refuses any path over MAX_PATH, and aGiTrack
                # read those refusals as "nothing there": `-d text` printed a confident
                # `branch master, 0 commits` / 0% coverage and exit 0 on a 3-commit repo, while
                # git on the same repo said `Filename too long` / `fatal: bad object HEAD`. A
                # silently EMPTY report is the worst possible answer.
                #
                # Passed per-invocation with `-c` rather than written into the user's config:
                # aGiTrack does not get to change how the user's own `git` behaves, only how the
                # git IT runs behaves.
                flags += ["-c", "core.longpaths=true"]
        if len(command) >= 2 and command[0] == "git" and command[1] in ("log", "rev-list", "shortlog"):
            flags += ["-c", "core.commitGraph=false"]
            if not allow_lazy_fetch or not _git_read_needs_blobs(command):
                flags += ["-c", "fetch.disableLazyFetch=true"]
                extra_env["GIT_NO_LAZY_FETCH"] = "1"
        if flags:
            command = [command[0], *flags, *command[1:]]
        # A timeout bounds a network git call (fetch/push over bad internet): on
        # expiry subprocess.run kills the process and raises, which we surface as a
        # non-zero result so the caller treats it as a plain failure (e.g. offline).
        # Keep git off the host console on Windows: a child that inherits our console can leave
        # it out of raw mode (input then echoes as escape codes). When we feed git via input=,
        # subprocess already pipes its stdin, so only detach stdin when we don't. (See proc.py.)
        isolation = console_isolation_kwargs(detach_stdin=input_text is None)
        # ALWAYS encode/decode git I/O as UTF-8 (git's default commit/text encoding), NEVER the
        # platform locale. On Windows ``text=True`` defaults to the ANSI code page (cp1252), which
        # cannot encode the box-drawing, em-dash, curly-quote, and emoji characters that routinely
        # appear in aGiTrack commit messages (the agent interaction trace). Feeding such a message
        # via ``input=`` then raised UnicodeEncodeError before git even ran — so EVERY agent-turn
        # commit failed and aGiTrack silently "stopped committing" on Windows. errors="replace"
        # keeps a stray undecodable byte in git's OUTPUT from ever crashing a read.
        if timeout is not None:
            try:
                return subprocess.run(
                    command,
                    cwd=self.repo,
                    input=input_text,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env={**os.environ, **extra_env} if extra_env else None,
                    timeout=timeout,
                    **isolation,
                )
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(command, returncode=124, stdout="", stderr="timed out")
        process = subprocess.run(
            command,
            cwd=self.repo,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **extra_env} if extra_env else None,
            **isolation,
        )
        if check and process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise GitError(f"Command failed: {' '.join(command)}\n{detail}")
        if cache_key is not None and _read_cache_state.entries is not None:
            # Stamped with the epoch AFTER the read, so a write that landed while git was
            # running invalidates this answer rather than being papered over by it.
            _read_cache_state.entries[cache_key] = (_write_epoch, process)
        return process
