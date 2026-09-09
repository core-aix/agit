"""Manual-commit latent tracking, extracted so any driver — the interactive proxy or the
headless background tracker — can record agent turns as hidden latent commits and fold them
into the user's own commit.

In manual-commit mode the agent edits the current branch directly (no worktree) and each turn
is recorded as a hidden "latent" commit on ``refs/agitrack/manual/<session_id>`` instead of
landing on the branch. HEAD never moves. When the user commits (via aGiTrack or an external
``git commit``), a ``prepare-commit-msg`` hook folds the pending turns' trace + metadata into
that ONE commit and a ``post-commit`` hook resets the latent ref. When a custom
``core.hooksPath`` makes the hooks impossible, a poll-and-cover fallback detects the commit and
adds a metadata-only cover commit instead.

This class owns exactly that machinery over a pair of GitRepo handles (the working ``repo`` and
the ``base_repo`` whose ``.agitrack/`` holds the durable hook files), plus the small amount of
mutable poll state it needs. It performs no I/O with the user; callers supply a ``debug`` sink.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from agitrack.commits.message import apply_summary_to_message, build_manual_squash_trailer, build_pending_trailer
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks
from agitrack.fileio import ensure_state_dir


def write_lf(path: Path, text: str) -> None:
    """Write *text* with LF endings on every platform.

    These files are read by the POSIX ``sh`` commit hooks: ``manual-ref`` a line at a time, and
    ``manual-pending-trailer`` straight into the commit message. ``Path.write_text`` uses
    ``newline=None``, which on Windows rewrites every ``\n`` as ``\r\n`` — the hook then reads a
    ref name with a trailing CR and ``git update-ref`` rejects it, so the latent refs are never
    advanced and the turns fold a SECOND time into the next commit (caught by Windows CI).

    ATOMIC (temp file + rename), because the reader is a separate process whose timing we do not
    control: the ``sh`` hooks read these files during a ``git commit`` that can land at any moment,
    including mid-write. An in-place rewrite can be read half-finished — and while an EMPTY read is
    harmlessly skipped by the hook's ``[ -s ... ]`` guard (the turn is merely lost), a partial but
    non-empty one would fold a truncated metadata block straight into the user's permanent commit
    message. Same guarantee ``state.save()`` already relies on.
    """
    path = Path(path)
    ensure_state_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        # Only reachable with the tmp still present when something above raised; on success
        # os.replace already consumed it.
        try:
            os.unlink(tmp)
        except OSError:
            pass


class ManualCommitTracker:
    def __init__(
        self,
        repo: GitRepo,
        base_repo: GitRepo,
        state: AgitrackState,
        *,
        debug: Callable[[str], None] | None = None,
        in_flight_fn: Callable[[], dict | None] | None = None,
    ) -> None:
        self.repo = repo
        self.base_repo = base_repo
        self.state = state
        self._debug = debug or (lambda _m: None)
        # Supplies the currently-running turn's facts (or None) so a commit the AGENT makes
        # mid-turn still carries attribution — see :meth:`render_trailer`.
        self._in_flight_fn = in_flight_fn
        # "Is a turn still OWED a record even though the tree has not changed?" Set by the caller
        # just before it records, from the same uncovered-commit list the recorded body will name
        # in ``covered_commits``. True when the agent committed its own work mid-turn: that commit
        # carries an in-flight block but not the turn's trace or tokens. Without this both gate()
        # and record() read "unchanged tree" as "nothing happened" and the accounting is dropped.
        self.owed_record = False
        # Cached working-tree snapshot from gate(), reused by record() so it doesn't re-snapshot.
        self._pending_tree: str | None = None
        # Set by gate() when it allowed an unchanged tree; consumed by record(), whose own
        # "nothing new since the tip" guard would otherwise refuse the very record gate approved.
        self._allow_unchanged = False
        # Poll/fallback state: last HEAD we saw, whether the fold hooks are installed, and the
        # last post-commit signal mtime we reacted to.
        self.last_head: str | None = None
        self.hooks_installed = False
        self._signal_mtime: float | None = None
        # Latent chains `setup()` discarded, for the driver to report to the user.
        self.dropped_chains: list[str] = []

    # --- identity / paths ---------------------------------------------------

    def ref(self) -> str:
        """The hidden ref that chains this session's per-turn latent commits."""
        return f"refs/agitrack/manual/{self.state.session_id}"

    def agit_dir(self):
        return self.base_repo.repo / ".agitrack"

    # --- lifecycle ----------------------------------------------------------

    def setup(self) -> None:
        """Startup wiring: install the fold/reset hooks (unless a custom ``core.hooksPath``
        makes that impossible — then the poll+cover fallback runs instead), drop a stale latent
        chain left by a prior run, record the baseline HEAD, and render the initial trailer so
        even a first commit with no agent turns is attributed to the session."""
        self.hooks_installed = False
        try:
            if self.base_repo.core_hooks_path():
                self._debug("manual-commit hooks skipped: core.hooksPath is set (using poll+cover fallback)")
            else:
                self.hooks_installed = git_hooks.install_manual_commit_hooks(
                    self.base_repo.hooks_dir(), debug=self._debug
                )
        except Exception as error:
            self._debug(f"manual-commit hook install failed: {error!r}")
        # ONE snapshot for the two questions below, which are the same question ("is the working
        # tree still what HEAD says?") asked milliseconds apart. Taking it twice doubled the cost
        # of arming a repo, and on a large one that is not a rounding error: 9.4s of a tracker's
        # startup, enough that `agitrack -b` stopped waiting for the handshake and reported a
        # perfectly healthy tracker as one that "did not start".
        try:
            worktree_tree: str | None = self.repo.snapshot_worktree_tree()
        except Exception as error:
            self._debug(f"startup worktree snapshot failed: {error!r}")
            worktree_tree = None  # each caller falls back to taking its own
        self.reset_stale_ref(worktree_tree=worktree_tree)
        # …and drop turns left behind by sessions that are gone, so they never ride into an
        # unrelated commit. Startup is the natural moment: any ref other than ours belongs to a
        # session that is no longer running. See `prune_abandoned_refs` for the rule.
        try:
            # Recorded rather than only logged: the driver surfaces it, because AI attribution
            # going away must never be something the user can only discover with --verbose.
            self.dropped_chains = prune_abandoned_refs(
                self.repo,
                self.ref(),
                self.pending_refs(),
                worktree_tree=worktree_tree,
                debug=self._debug,
            )
        except Exception as error:
            self._debug(f"abandoned-ref prune failed: {error!r}")
        try:
            self.last_head = self.repo.rev_parse("HEAD")
        except Exception:
            self.last_head = None
        self.render_trailer()

    def teardown(self) -> None:
        try:
            if self.base_repo is not None and not self.base_repo.core_hooks_path():
                git_hooks.remove_manual_commit_hooks(self.base_repo.hooks_dir(), debug=self._debug)
        except Exception as error:
            self._debug(f"manual-commit hook removal failed: {error!r}")

    # --- pending turns ------------------------------------------------------

    def pending_refs(self) -> list[str]:
        """EVERY latent ref that still holds turns HEAD does not contain — not just this
        session's.

        A session's turns live on ``refs/agitrack/manual/<agitrack_session_id>``, and that id
        changes when the user starts a new session (Ctrl-G → + New session mints a fresh one) or
        when a backend switch opens one. Folding only ``self.ref()`` therefore DROPPED every turn
        recorded before the switch: the trace between the previous commit and the new one had a
        hole in it, silently. Manual mode is always no-worktree, so all of those sessions edited
        the SAME working tree and the user's commit captures all of their work — the trace must
        cover all of them too. (The dashboard's pending view has always enumerated the refs this
        way; the fold is what lagged behind.)
        """
        try:
            refs = self.repo.list_refs("refs/agitrack/manual/")
        except Exception as error:
            self._debug(f"manual ref enumeration failed: {error!r}")
            refs = []
        current = self.ref()
        if current not in refs:
            refs.append(current)
        return [ref for ref in refs if self.repo.ref_sha(ref)]

    def _pending_shas(self) -> list[str]:
        """The latent commits awaiting a fold across every session's ref, oldest first.

        Ordered by commit time rather than per-ref, so turns from two sessions interleave in the
        order they actually happened; deduplicated because a reset ref can leave two names on one
        chain."""
        seen: set[str] = set()
        stamped: list[tuple[int, str]] = []
        for ref in self.pending_refs():
            try:
                # Commits reachable from NO branch — the ones that are genuinely still latent.
                # `HEAD..ref` is not enough: after a fold the ref points at a real branch commit,
                # so switching to another branch made that (already-folded) work look pending and
                # it folded a second time — duplicating the trace and double-counting its tokens.
                shas = self.repo.unlanded_commits(ref)
            except Exception as error:
                self._debug(f"manual pending walk failed for {ref}: {error!r}")
                continue
            for sha in shas:
                if sha in seen:
                    continue
                seen.add(sha)
                stamped.append((self.repo.commit_timestamp(sha) or 0, sha))
        stamped.sort(key=lambda item: item[0])
        return [sha for _stamp, sha in stamped]

    def pending_count(self) -> int:
        """How many latent turns are recorded but not yet folded into a commit, across every
        session's ref (cheap: no message reads)."""
        return len(self._pending_shas())

    def pending_bodies(self) -> list[str]:
        """Commit-message bodies of the pending latent turns (oldest first): the commits on
        EVERY session's latent ref that HEAD does not yet contain (see :meth:`pending_refs` — a
        session or backend switch moves the ref, and those turns must still be folded). Each body already carries the turn's full
        metadata + interaction trace; the LLM summary (a git note on the latent commit) is folded
        into the body here when it has arrived, so the user's commit gets the summarized message."""
        shas = self._pending_shas()
        bodies: list[str] = []
        for sha in shas:
            body = self.repo.commit_message(sha)
            if not body:
                continue
            try:
                summary = self.repo.notes_show(sha, namespace="agitrack/commit-summary")
            except Exception:
                summary = None
            if summary and summary.strip():
                body = apply_summary_to_message(body, summary, repo_root=self.repo.repo)
            bodies.append(body)
        return bodies

    def in_flight_attribution(self) -> dict | None:
        """The running turn's facts for the trailer, or None when nothing should be attributed.

        Two conditions, both required: the driver reports a turn actually in progress, AND the
        working tree differs from the latent tip so the agent has real changes in this commit.
        The tree check is what keeps the "no AI work ⇒ no footprint" promise — without it, a
        human's own commit made while an agent happened to be thinking would be stamped as
        agent work."""
        if self._in_flight_fn is None:
            return None
        try:
            facts = self._in_flight_fn()
        except Exception as error:
            self._debug(f"in-flight lookup failed: {error!r}")
            return None
        if not facts:
            return None
        try:
            if not self._tree_differs_from_tip(self.repo.snapshot_worktree_tree()):
                return None
        except Exception as error:
            self._debug(f"in-flight snapshot failed: {error!r}")
            return None
        return facts

    def render_trailer(self) -> None:
        """(Re)render ``.agitrack/manual-pending-trailer`` from the durable latent ref, and the
        ``.agitrack/manual-ref`` name file the post-commit hook reads. When pending turns exist the
        trailer carries the ``commit_type: user`` block plus each turn's full trace/metadata; when
        none are pending but the agent is mid-turn with changes in the tree, it carries the
        in-flight attribution block instead; otherwise it is empty, so a purely human commit (no
        AI work) is left untouched."""
        try:
            agit_dir = self.agit_dir()
            ensure_state_dir(agit_dir)
            # EVERY ref whose turns this commit will fold, one per line — the post-commit hook
            # advances each. Writing only the current session's ref left an earlier session's
            # already-folded turns pending, so the next commit folded them a second time.
            refs = self.pending_refs()
            if self.ref() not in refs:
                refs.append(self.ref())
            write_lf(agit_dir / "manual-ref", "\n".join(refs) + "\n")
            trailer = build_pending_trailer(
                agitrack_session_id=self.state.session_id,
                latent_bodies=self.pending_bodies(),
                in_flight=self.in_flight_attribution(),
                repo_root=self.repo.repo,
            )
            write_lf(agit_dir / "manual-pending-trailer", trailer)
        except Exception as error:
            self._debug(f"manual trailer render failed: {error!r}")

    # --- recording turns ----------------------------------------------------

    def _tree_differs_from_tip(self, tree: str) -> bool:
        """Whether *tree* differs from the latent tip's tree (or HEAD's when the chain is
        empty) — i.e. whether there is uncommitted agent work to account for."""
        tip = self.repo.ref_sha(self.ref())
        try:
            # `comparable_tree`, never a raw `^{tree}`: *tree* is a snapshot, which drops the
            # agent scaffolding dirs, so a repo that TRACKS `.claude/` would otherwise read as
            # permanently dirty and nothing here could ever be equal. See GitRepo.comparable_tree.
            base_tree = self.repo.comparable_tree(tip or "HEAD")
        except Exception:
            base_tree = None
        return tree != base_tree

    def gate(self) -> bool:
        """Commit gate for a manual-mode turn: True when the working tree changed since the
        latent tip (or HEAD when the chain is empty). Caches the snapshot so :meth:`record`
        doesn't re-snapshot."""
        try:
            self._pending_tree = self.repo.snapshot_worktree_tree()
        except Exception as error:
            self._debug(f"manual snapshot failed: {error!r}")
            self._pending_tree = None
            self._allow_unchanged = False
            return False
        if self._tree_differs_from_tip(self._pending_tree):
            self._allow_unchanged = False
            return True
        # Unchanged tree, but the agent may have committed the turn's work itself — in which
        # case the record is still owed and must be made against the tree as it stands.
        self._allow_unchanged = bool(self.owed_record)
        return self._allow_unchanged

    def record(self, message: str) -> str | None:
        """Record a manual-mode turn as a hidden latent commit: snapshot the working tree,
        commit-tree it onto the latent tip, and advance ONLY the latent ref — HEAD and the user's
        index are untouched. Returns the short sha, or None if the tree is unchanged."""
        tree = self._pending_tree
        self._pending_tree = None
        if tree is None:
            try:
                tree = self.repo.snapshot_worktree_tree()
            except Exception as error:
                self._debug(f"manual snapshot failed: {error!r}")
                return None
        tip = self.repo.ref_sha(self.ref())
        re_anchored = False
        if tip is not None and not self.repo.has_object_local(tip):
            # The ref names a commit this repo no longer has — `git gc --prune` can collect a
            # latent commit, which is unreachable from any branch by design. Every lookup against
            # it then raises, and because `record()` is called on EVERY turn the whole session
            # would silently stop tracking until someone deleted the ref by hand. Re-anchor at
            # HEAD instead: the chain restarts, and only the already-lost turns are lost.
            self._debug(f"latent tip {tip} is missing from the object store; re-anchoring at HEAD")
            tip, re_anchored = None, True
        parent = tip or self.repo.rev_parse("HEAD")
        allow_unchanged, self._allow_unchanged = self._allow_unchanged, False
        # Defensive: nothing new since the baseline — the latent tip, or HEAD when the chain is
        # EMPTY, which is the same baseline gate() uses and was previously exempt from this guard
        # entirely, so a record() reaching here ungated recorded a phantom first turn against an
        # untouched tree. Skipped in two cases, both of which would otherwise vanish a turn's
        # tokens: gate() explicitly allowed an unchanged tree (the agent committed the turn's work
        # itself), and the re-anchor above, where the tip we would have compared against is the
        # object git collected — HEAD is a fallback parent there, not evidence of no work.
        baseline = None if re_anchored else (tip or "HEAD")
        if not allow_unchanged and baseline is not None and tree == self.repo.comparable_tree(baseline):
            return None
        sha = self.repo.commit_tree(tree, parents=[parent], message=message)
        self.repo.update_ref(self.ref(), sha)
        self.render_trailer()
        return self.repo.short_sha(sha)

    # --- reconciliation with the user's own commits -------------------------

    def reset_stale_ref(self, *, worktree_tree: str | None = None) -> bool:
        """Reset the latent ref to HEAD when its recorded turns are STALE, so they are never
        re-folded into an unrelated future commit. Turns are stale when the tip is an ANCESTOR of
        HEAD (already committed/folded) or the working tree is CLEAN (nothing left to fold). A
        DIRTY tree with a diverged tip means real uncommitted work remains, so the turns are kept.
        Never merges — the ref is only ever reset. Returns True when it reset the ref.

        ``worktree_tree`` is an already-taken snapshot of the working tree to reuse. Taking one
        is the single most expensive thing aGiTrack asks of git — it stages the whole worktree
        into a throwaway index, which has no stat cache, so every tracked file is re-hashed
        (measured: 4.4s on one large repo) — and :meth:`setup` needs the same answer twice."""
        try:
            head = self.repo.rev_parse("HEAD")
            tip = self.repo.ref_sha(self.ref())
            if not tip:
                return False
            snapshot = worktree_tree if worktree_tree is not None else self.repo.snapshot_worktree_tree()
            clean = snapshot == self.repo.comparable_tree("HEAD")
            if clean or self.repo.is_ancestor(tip, head):
                self.repo.update_ref(self.ref(), head)
                return True
        except Exception as error:
            self._debug(f"manual ref reset failed: {error!r}")
        return False

    def service(self) -> None:
        """Per-loop upkeep. With the hooks installed, react to a commit (the post-commit signal
        fired, or HEAD simply moved) by dropping the now-stale latent chain and re-rendering the
        trailer. Without hooks (custom core.hooksPath), fall back to poll+cover."""
        if self.hooks_installed:
            signal_file = self.agit_dir() / "manual-commit-signal"
            try:
                mtime: float | None = signal_file.stat().st_mtime
            except OSError:
                mtime = None
            try:
                head = self.repo.rev_parse("HEAD")
            except Exception:
                return
            signalled = mtime is not None and mtime != self._signal_mtime
            moved = head != self.last_head
            if signalled or moved:
                self._signal_mtime = mtime
                self.last_head = head
                self.reset_stale_ref()
                self.render_trailer()
        else:
            self.reconcile_external_commit()

    def reconcile_external_commit(self) -> None:
        """Poll+cover FALLBACK for when the fold hook can't run (custom core.hooksPath): if HEAD
        moved since we last looked and pending latent turns exist, the user committed outside the
        hook — add a metadata-only cover commit carrying the pending tracking (its tree equals the
        new HEAD's, so it introduces no diff), then reset the latent ref."""
        if self.hooks_installed:
            return
        try:
            head = self.repo.rev_parse("HEAD")
        except Exception:
            return
        if self.last_head is None:
            self.last_head = head
            return
        if head == self.last_head:
            return
        self.last_head = head
        tip = self.repo.ref_sha(self.ref())
        bodies = self.pending_bodies()
        if not tip or not bodies:
            self.render_trailer()
            return
        message = "<aGiTrack> track agent turns\n\n" + build_manual_squash_trailer(
            agitrack_session_id=self.state.session_id, latent_bodies=bodies, repo_root=self.repo.repo
        )
        try:
            head_tree = self.repo.rev_parse("HEAD^{tree}")
            self.repo.cover_commit(message, first_parent=head, second_parent=tip, tree=head_tree)
            self.last_head = self.repo.rev_parse("HEAD")
            self.repo.update_ref(self.ref(), self.repo.rev_parse("HEAD"))
        except Exception as error:
            self._debug(f"manual cover reconcile failed: {error!r}")
        self.render_trailer()


def prune_abandoned_refs(
    repo: GitRepo,
    own_ref: str,
    refs: list[str],
    *,
    worktree_tree: str | None = None,
    debug: Callable[[str], None] | None = None,
) -> list[str]:
    """Drop latent turns from ABANDONED sessions that no longer explain any uncommitted work.

    Manual mode is always no-worktree, so every session edits the SAME working tree, and a user
    commit folds the pending turns of all of them. That is right while those turns still explain
    the code being committed — and wrong once they don't. A session abandoned mid-work (a crash,
    Ctrl-C, a mode switch), or one whose edits the user discarded with ``git checkout --``, leaves
    a ref behind that nothing ever revisits: `reset_stale_ref` only ever looks at the caller's OWN
    ref. Its turns then ride into some later, unrelated commit, permanently attributing AI
    authorship and token counts to code that never contained them.

    The rule, per the maintainer:

        keep a session's turns when it made code changes that are still uncommitted — those get
        committed along with their trace; discard a trailing run of turns that led to no code
        change at all.

    Both halves are decided from git, not from a clock:

    * **Nothing uncommitted anywhere** (the working tree matches HEAD) — no session's changes
      survive, so every abandoned chain is discarded outright. This is the discarded-edit case.
    * **Something uncommitted** — the chain is kept, minus any TRAILING commits whose recorded
      tree matches HEAD's. Those are the conversation-only tail: turns that discussed rather than
      changed anything, so they contribute nothing to the commit about to be made.

    The caller's own ref is never touched — a live session owns its chain, and `reset_stale_ref`
    already governs it. Returns the refs that were changed (pruned or reset), for logging.
    """
    log = debug or (lambda _message: None)
    changed: list[str] = []
    try:
        head = repo.rev_parse("HEAD")
        # Scaffolding-stripped on BOTH sides of every comparison below (the snapshot, and the
        # latent trees the tail-trim walks), or a repo tracking `.claude/` never prunes anything.
        head_tree = repo.comparable_tree("HEAD")
        # Reuse the caller's snapshot when it has one (see reset_stale_ref): startup asks this
        # same question twice, moments apart, and the snapshot is the expensive half of it.
        snapshot = worktree_tree if worktree_tree is not None else repo.snapshot_worktree_tree()
        working_tree_is_clean = snapshot == head_tree
    except Exception as error:
        log(f"abandoned-ref prune skipped: {error!r}")
        return changed

    for ref in refs:
        if ref == own_ref:
            continue
        try:
            tip = repo.ref_sha(ref)
            if not tip or repo.is_ancestor(tip, head):
                continue  # already folded/committed: reset_stale_ref's ordinary case
            if working_tree_is_clean:
                # No uncommitted code anywhere, so nothing this session recorded still explains
                # work about to be committed.
                repo.update_ref(ref, head)
                changed.append(ref)
                log(f"discarded abandoned latent chain {ref}: no uncommitted work remains")
                continue
            # Trim the conversation-only tail: trailing turns that recorded no code beyond HEAD.
            pruned = tip
            while pruned and pruned != head and repo.comparable_tree(pruned) == head_tree:
                parents = repo.parents(pruned)
                pruned = parents[0] if parents else head
            if pruned != tip:
                repo.update_ref(ref, pruned or head)
                changed.append(ref)
                log(f"trimmed conversation-only tail from abandoned chain {ref}")
        except Exception as error:
            log(f"abandoned-ref prune failed for {ref}: {error!r}")
    return changed
