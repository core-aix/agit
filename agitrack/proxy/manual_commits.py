"""Manual-commit mode and latent tracking, as a mixin on ``ProxyRunner``.

``--manual-commits`` inverts aGiTrack's usual bargain: the agent's work is recorded as
HIDDEN latent commits on a side ref, the working tree is left deliberately dirty, and the
user commits on their own schedule — at which point a hook folds the accumulated tracking
into their commit message. The same machinery backs every no-worktree mode, where the fold
happens automatically.

It is a strict addition: none of it runs unless the mode is on, which is why it reads as a
self-contained layer rather than a branch inside the normal commit path.

See ``sharing.py``'s header for why these are mixins rather than collaborator objects.
"""

from __future__ import annotations

import time

from agitrack.commits import (
    apply_summary_to_message,
    build_auto_fold_message,
    build_manual_squash_trailer,
    build_pending_trailer,
    is_in_flight_only_message,
    write_lf,
)
from agitrack.git import hooks as git_hooks

from agitrack.proxy.runner_host import RunnerHost
from agitrack.fileio import ensure_state_dir


class ManualCommitsMixin(RunnerHost):
    """``ProxyRunner``'s manual-commit/latent-tracking half. Mixed in, never instantiated."""

    # --- manual-commit mode (--manual-commits): hidden latent commits + a hook that ----
    # --- folds their tracking into the user's own commit (a strict addition, off by ----
    # --- default; none of this runs unless self._manual_commits is True). --------------

    def _manual_ref(self) -> str:
        """The hidden ref that chains this session's per-turn latent commits."""
        return f"refs/agitrack/manual/{self.state.session_id}"

    def _manual_agit_dir(self):
        return self.base_repo.repo / ".agitrack"

    @property
    def _latent_tracking(self) -> bool:
        """Whether this session records turns as hidden latent commits and folds them via the
        prepare-commit-msg hook — true for ALL no-worktree sessions (manual OR auto). Worktree
        mode commits per-branch and integrates as before (false). Derived (not stored) so it is
        always consistent with ``_use_worktrees`` even in tests that build the runner directly."""
        return not self._use_worktrees

    @property
    def _noworktree_auto(self) -> bool:
        """No-worktree mode WITHOUT manual commits: aGiTrack folds the pending latent turns into a
        commit itself, and the fold hook folds the agent's OWN commits (cover is only the backup)."""
        return (not self._use_worktrees) and (not self._manual_commits)

    def _setup_manual_commit_mode(self) -> None:
        """Startup wiring for manual-commit mode: install the fold/reset hooks (unless a
        custom ``core.hooksPath`` makes that impossible — then the poll+cover fallback runs
        instead), point the latent chain at the current HEAD, and render the initial trailer
        so even a first commit with no agent turns is attributed to the session.

        Also used by no-worktree AUTO mode (``_latent_tracking``): the same hooks fold the agent's
        own commits, and aGiTrack folds any remaining pending turns itself (see _auto_fold_latent)."""
        if not self._latent_tracking:
            return
        self._manual_hooks_installed = False
        try:
            if self.base_repo.core_hooks_path():
                self._debug("manual-commit hooks skipped: core.hooksPath is set (using poll+cover fallback)")
            else:
                self._manual_hooks_installed = git_hooks.install_manual_commit_hooks(
                    self.base_repo.hooks_dir(), debug=self._debug
                )
        except Exception as error:
            self._debug(f"manual-commit hook install failed: {error!r}")
        # Also install the PERSISTENT auto-track pre-commit hook so this run's own commits fold
        # their AI work. Whether it STAYS after exit is decided by whether it was there before:
        # the hook auto-starts a background tracker on the next commit, so leaving one behind in a
        # repo that had none would mean an interactive session quietly turned auto-start ON — a
        # standing preference, decided on the user's behalf, from a command that says nothing about
        # it. Same rule the backends' session-start hooks already follow
        # (`_displace_background_autostart`): restore what was there, and only that. Recorded here
        # rather than in teardown because by then the hook is always installed, so "was it there?"
        # can no longer be asked. Tri-state: None means no setup has run, so the teardown that
        # CLEARS stale hooks at startup (called before this) never acts on it.
        try:
            self._autotrack_hook_preexisting = git_hooks.is_autotrack_hook(self.base_repo.hooks_dir() / "pre-commit")
        except Exception as error:
            self._debug(f"autotrack hook state read failed: {error!r}")
            self._autotrack_hook_preexisting = True  # unknown: err toward leaving it alone
        self._install_autotrack_precommit_hook()
        # Recovery: drop a stale latent chain left by a prior run (e.g. the user committed
        # outside aGiTrack after exiting) so its turns aren't re-folded into a later commit.
        # ONE snapshot for both recovery steps below: they ask the same question ("does the
        # working tree still match HEAD?") a moment apart, and taking it twice doubled the cost
        # of starting on a large repo (measured at 4.4s per snapshot on one).
        try:
            worktree_tree: str | None = self.repo.snapshot_worktree_tree()
        except Exception as error:
            self._debug(f"startup worktree snapshot failed: {error!r}")
            worktree_tree = None  # each step falls back to taking its own
        self._reset_stale_manual_ref(worktree_tree=worktree_tree)
        # …and the same for chains left by sessions that are GONE — a crash, a Ctrl-C, a mode
        # switch, or edits the user discarded. `_reset_stale_manual_ref` only ever looks at our
        # own ref, so nothing revisited those and their turns rode into an unrelated later
        # commit, attributing AI authorship to code that never contained it.
        try:
            from agitrack.commits.manual import prune_abandoned_refs

            dropped = prune_abandoned_refs(
                self.repo,
                self._manual_ref(),
                self._manual_pending_refs(),
                worktree_tree=worktree_tree,
                debug=self._debug,
            )
            if dropped:
                # Say so. Discarding a chain is the right call (its work is no longer uncommitted),
                # but it is still AI attribution going away, and the user should hear about it
                # rather than find out from a history that is quietly missing a turn.
                count = len(dropped)
                chains = "chain" if count == 1 else "chains"
                self._set_message(
                    f"aGiTrack: discarded {count} abandoned tracking {chains} from an earlier session — "
                    "their changes are no longer uncommitted, so they had nothing left to attribute.",
                    seconds=10.0,
                )
        except Exception as error:
            self._debug(f"abandoned-ref prune failed: {error!r}")
        # Baseline HEAD for the poll fallback (detect a user/external commit that the hook
        # didn't fold), and the durable trailer/ref files the hook reads.
        try:
            self._manual_last_head = self.repo.rev_parse("HEAD")
        except Exception:
            self._manual_last_head = None
        self._render_manual_trailer()

    def _teardown_manual_commit_mode(self) -> None:
        # Unconditional (NOT gated on _latent_tracking): removal must run even in worktree mode so
        # a stale manual fold-hook left by a prior crashed no-worktree run is cleared. It only
        # removes aGiTrack's OWN marked hooks (restoring any chained user hook) and is a no-op when
        # none are installed, so calling it in any mode is safe.
        # getattr: several tests build a ProxyRunner via __new__, so the attribute may not exist.
        preexisting = getattr(self, "_autotrack_hook_preexisting", None)
        self._autotrack_hook_preexisting = None
        try:
            if self.base_repo is not None and not self.base_repo.core_hooks_path():
                git_hooks.remove_manual_commit_hooks(self.base_repo.hooks_dir(), debug=self._debug)
                # Give the pre-commit slot back exactly as this run found it. `preexisting is
                # False` and nothing weaker: None means no setup ran (the startup call that clears
                # stale hooks), and True means the user already had auto-start armed, which an
                # interactive session must never be what uninstalls.
                if preexisting is False:
                    git_hooks.remove_autotrack_precommit_hook(self.base_repo.hooks_dir(), debug=self._debug)
                    self._debug("auto-track pre-commit hook removed: this repo did not have it before the session")
        except Exception as error:
            self._debug(f"manual-commit hook removal failed: {error!r}")

    def _manual_pending_count(self) -> int:
        """How many latent turns are recorded but not yet folded into a commit (cheap: no
        message reads). Used by the exit reminder."""
        if not self._latent_tracking:
            return 0
        tip = self.repo.ref_sha(self._manual_ref())
        if not tip:
            return 0
        try:
            return len(self.repo.log_shas("HEAD", tip))
        except Exception:
            return 0

    def _manual_pending_refs(self) -> list[str]:
        """Every latent ref still holding turns that no branch contains — not just this session's.

        Mirrors :meth:`ManualCommitTracker.pending_refs` (the proxy keeps its own copy of the
        latent machinery): the ref name embeds the agitrack session id, so a new session or a
        backend switch moves it and folding only the current ref would drop the turns recorded
        before the switch."""
        try:
            refs = self.base_repo.list_refs("refs/agitrack/manual/")
        except Exception as error:
            self._debug(f"manual ref enumeration failed: {error!r}")
            refs = []
        current = self._manual_ref()
        if current not in refs:
            refs.append(current)
        return [ref for ref in refs if self.repo.ref_sha(ref)]

    def _manual_pending_shas(self) -> list[str]:
        """The latent commits awaiting a fold across every session's ref, oldest first.

        "Reachable from no branch" — not ``HEAD..ref`` — is what makes this correct after a fold:
        the post-commit hook advances the ref to a real branch commit, so a plain range walk
        reported that already-folded work as pending again the moment HEAD moved to another
        branch, and it folded (and billed) a second time."""
        seen: set[str] = set()
        stamped: list[tuple[int, str]] = []
        for ref in self._manual_pending_refs():
            try:
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

    def _manual_pending_bodies(self) -> list[str]:
        """Commit-message bodies of the pending latent turns (oldest first): the commits on
        the latent ref that HEAD does not yet contain.

        Each body already carries the turn's full metadata + interaction trace (written
        synchronously when the latent commit was recorded — the fold never waits on it). The
        LLM summary is computed asynchronously and lands as a git NOTE on the latent commit
        (which is never HEAD, so it can't be amended into the message); fold it into the body
        here when it has arrived, so the user's commit gets the summarized message. If the user
        commits before a summary finishes, it is simply omitted — the metadata/trace are still
        there."""
        bodies: list[str] = []
        for sha in self._manual_pending_shas():
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

    def _note_in_flight(self, facts: dict | None) -> None:
        """Remember (or clear) the running turn's facts, refreshed from every session export.

        Re-renders the fold trailer on a change so it is already current if the agent commits
        mid-turn. The proxy needs this because — unlike the background daemon, which the
        pre-commit hook nudges synchronously — it otherwise renders only as turns COMPLETE, and
        a turn that has merely STARTED is exactly the case this attribution exists for."""
        changed = getattr(self, "_in_flight", None) != facts
        self._in_flight = facts
        if changed:
            self._render_manual_trailer()

    def _in_flight_attribution(self) -> dict | None:
        """The running turn's facts for the fold trailer, or None when nothing should be
        attributed. Mirrors ``ManualCommitTracker.in_flight_attribution``: a turn must actually
        be in progress AND the working tree must differ from the latent tip, so a commit with no
        AI work in it still gets no trailer."""
        facts = getattr(self, "_in_flight", None)
        if not facts:
            return None
        try:
            if not self._manual_tree_differs_from_tip(self.repo.snapshot_worktree_tree()):
                return None
        except Exception as error:
            self._debug(f"in-flight snapshot failed: {error!r}")
            return None
        return facts

    def _render_manual_trailer(self) -> None:
        """(Re)render ``.agitrack/manual-pending-trailer`` from the durable latent ref, and
        the ``.agitrack/manual-ref`` name file the post-commit hook reads. When pending turns
        exist the trailer carries the ``commit_type: user`` block plus each turn's full
        trace/metadata; when none are pending but the agent is mid-turn with changes in the tree
        it carries the in-flight attribution block instead (so an agent that commits its own work
        before the turn ends is still tracked); otherwise it is empty and a purely human commit
        is untouched."""
        if not self._latent_tracking:
            return
        try:
            agit_dir = self._manual_agit_dir()
            ensure_state_dir(agit_dir)
            # Every ref this commit will fold, one per line, LF-terminated (see commits.write_lf:
            # a CRLF name makes `git update-ref` refuse and the refs never advance on Windows).
            refs = self._manual_pending_refs()
            if self._manual_ref() not in refs:
                refs.append(self._manual_ref())
            write_lf(agit_dir / "manual-ref", "\n".join(refs) + "\n")
            trailer = build_pending_trailer(
                agitrack_session_id=self.state.session_id,
                latent_bodies=self._manual_pending_bodies(),
                in_flight=self._in_flight_attribution(),
                repo_root=self.repo.repo,
            )
            write_lf(agit_dir / "manual-pending-trailer", trailer)
        except Exception as error:
            self._debug(f"manual trailer render failed: {error!r}")

    def _manual_tree_differs_from_tip(self, tree: str) -> bool:
        """Whether *tree* differs from the latent tip's tree (or HEAD's when the chain is
        empty) — i.e. whether there is uncommitted agent work to account for."""
        tip = self.repo.ref_sha(self._manual_ref())
        try:
            # `comparable_tree`, never a raw `^{tree}` — the snapshot on the other side of this
            # comparison drops the agent scaffolding dirs. See GitRepo.comparable_tree.
            base_tree = self.repo.comparable_tree(tip or "HEAD")
        except Exception:
            base_tree = None
        return tree != base_tree

    def _manual_gate(self) -> bool:
        """Commit gate for a manual-mode turn: True when the working tree changed since the
        latent tip (or HEAD when the chain is empty). Caches the snapshot tree so
        :meth:`_manual_record` doesn't re-snapshot."""
        try:
            self._manual_pending_tree = self.repo.snapshot_worktree_tree()
        except Exception as error:
            self._debug(f"manual snapshot failed: {error!r}")
            self._manual_pending_tree = None
            self._manual_allow_unchanged = False
            return False
        if self._manual_tree_differs_from_tip(self._manual_pending_tree):
            self._manual_allow_unchanged = False
            return True
        # An unchanged tree normally means the turn left nothing to record. It does NOT when the
        # agent committed its own work mid-turn: that commit carries an in-flight block only, so
        # the turn's trace and tokens are still owed, and declining here loses them outright.
        # Record with the tree as it stands — the latent commit is metadata, not a diff.
        self._manual_allow_unchanged = bool(self._uncovered_backend_commits())
        return self._manual_allow_unchanged

    def _manual_changed_paths(self) -> list[str]:
        """The paths this turn's latent record will carry.

        Read against the same baseline the gate uses — the latent tip, or HEAD when the chain
        is empty — so it is this TURN's contribution and not everything since HEAD. Runs after
        ``_manual_gate`` has cached the snapshot, which is the tree ``_manual_record`` commits.

        Used only for the interrupted-turn note (see ``message._interrupted_changes_sentence``):
        a cancelled turn's trace stops at the agent's "I'll do X" and cannot account for the
        diff, so the commit has to state its own contents. Best-effort — an empty list simply
        leaves the note without its file list."""
        tree = getattr(self, "_manual_pending_tree", None)
        if tree is None:
            return []
        tip = self.repo.ref_sha(self._manual_ref())
        try:
            base = self.repo.comparable_tree(tip or "HEAD")
        except Exception as error:
            self._debug(f"latent baseline tree failed: {error!r}")
            return []
        return self.repo.tree_diff_names(base, tree)

    def _manual_record(self, message: str) -> str | None:
        """Record a manual-mode turn as a hidden latent commit: snapshot the working tree,
        commit-tree it onto the latent tip, and advance ONLY the latent ref — HEAD and the
        user's index are untouched. Returns the short sha, or None if the tree is unchanged."""
        tree = getattr(self, "_manual_pending_tree", None)
        self._manual_pending_tree = None
        if tree is None:
            try:
                tree = self.repo.snapshot_worktree_tree()
            except Exception as error:
                self._debug(f"manual snapshot failed: {error!r}")
                return None
        tip = self.repo.ref_sha(self._manual_ref())
        re_anchored = False
        if tip is not None and not self.repo.has_object_local(tip):
            # Kept in lockstep with `ManualCommitTracker.record`: `git gc --prune` can collect a
            # latent commit (unreachable from any branch by design), after which every lookup
            # against the tip raises — and record() runs on EVERY turn, so interactive `-m` would
            # silently stop tracking for the rest of the session. Re-anchor at HEAD: the chain
            # restarts and only the already-lost turns are lost.
            self._debug(f"latent tip {tip} is missing from the object store; re-anchoring at HEAD")
            tip, re_anchored = None, True
        parent = tip or self.repo.rev_parse("HEAD")
        allow_unchanged = getattr(self, "_manual_allow_unchanged", False)
        self._manual_allow_unchanged = False
        # Defensive: nothing new since the baseline — the latent tip, or HEAD when the chain is
        # EMPTY (the same baseline the gate uses, and previously exempt from this guard, so an
        # ungated record() wrote a phantom first turn against an untouched tree). Skipped when the
        # gate allowed an unchanged tree (the agent committed the turn's work itself) and after a
        # re-anchor, where HEAD is a fallback parent rather than evidence that nothing happened —
        # either way the turn's tokens would vanish.
        baseline = None if re_anchored else (tip or "HEAD")
        if not allow_unchanged and baseline is not None and tree == self.repo.comparable_tree(baseline):
            return None
        sha = self.repo.commit_tree(tree, parents=[parent], message=message)
        self.repo.update_ref(self._manual_ref(), sha)
        self._render_manual_trailer()
        return self.repo.short_sha(sha)

    def _reset_stale_manual_ref(self, *, worktree_tree: str | None = None) -> bool:
        """Reset the latent ref to HEAD when its recorded turns are STALE, so they are never
        re-folded into an unrelated future commit. The one rule, applied both at startup and
        on every poll: turns are stale when either

          * the tip is an ANCESTOR of HEAD — they are already committed/folded (a normal
            in-aGiTrack commit resets the ref via the post-commit hook; this catches a bypassed
            or interrupted one); or
          * the working tree is CLEAN (its snapshot equals HEAD's tree) — nothing is left to
            fold, so any pending turns are already reflected in HEAD. This is the case when a
            commit was made OUTSIDE aGiTrack: while aGiTrack runs, the fold hook already
            combined the turns INTO that commit (any terminal, even ``--no-verify``); after it
            has exited, the hook is gone so the trace is unavoidably lost for that commit — but
            either way the pending chain is now redundant and dropping it prevents its trace
            from re-attaching to a later commit.

        A DIRTY tree with a diverged tip means real uncommitted work remains, so the turns are
        kept and fold on the next commit. Never merges — the ref is only ever reset, so there
        is no git conflict. Returns True when it reset the ref.

        ``worktree_tree`` is an already-taken snapshot to reuse, for the one caller that needs
        this answer twice in a row (see :meth:`_setup_manual_commit_mode`). A snapshot stages the
        whole working tree into a throwaway index with no stat cache, so it re-hashes every
        tracked file — seconds on a large repo, and the most expensive thing aGiTrack asks of
        git."""
        try:
            head = self.repo.rev_parse("HEAD")
            tip = self.repo.ref_sha(self._manual_ref())
            if not tip:
                return False
            snapshot = worktree_tree if worktree_tree is not None else self.repo.snapshot_worktree_tree()
            clean = snapshot == self.repo.comparable_tree("HEAD")
            if clean and is_in_flight_only_message(self.repo.commit_message(head)):
                # A clean tree normally means the fold hook already combined the pending turns
                # INTO HEAD, so they are redundant. It does NOT when HEAD carries only an
                # IN-FLIGHT block: the agent committed while its turn was still running, so that
                # commit records who made the change but not the turn's trace or tokens — which
                # are still owed. Dropping the chain here is what loses them for good, and in
                # manual mode (where aGiTrack must not commit) the chain is the ONLY thing
                # holding them until the user's next commit folds them in.
                # Same rule as `_uncovered_backend_commits`: in-flight ≠ accounted for.
                #
                # Narrow on purpose: a commit with NO aGiTrack metadata (made outside aGiTrack,
                # where the hook never ran) still drops the chain as before — that trace is
                # unavoidably lost, and keeping the chain would re-attach it to some later,
                # unrelated commit.
                return False
            if clean or self.repo.is_ancestor(tip, head):
                self.repo.update_ref(self._manual_ref(), head)
                return True
        except Exception as error:
            self._debug(f"manual ref reset failed: {error!r}")
        return False

    def _service_manual_commit_mode(self) -> None:
        """Per-loop upkeep for manual-commit mode (throttled). With the hooks installed, react
        to a commit (the post-commit signal fired, or HEAD simply moved) by dropping the now-
        stale latent chain and re-rendering the trailer — so a commit made outside aGiTrack,
        which the fold hook already combined the pending turns into, also resets the ref.
        Without hooks (custom core.hooksPath), fall back to detecting the commit by polling
        HEAD and adding a cover commit. Runs for all no-worktree modes (``_latent_tracking``):
        in no-worktree AUTO mode this is also how the agent's own commit resets the chain after
        the fold hook folds tracking into it."""
        if not self._latent_tracking:
            return
        now = time.monotonic()
        if now - getattr(self, "_manual_poll_at", 0.0) < self.BASE_POLL_SECONDS:
            return
        self._manual_poll_at = now
        # No-worktree AUTO mode: aGiTrack folds any pending turns into a commit itself (so the
        # user doesn't have to). A clean tree means the agent already committed — the fold hook
        # folded the tracking into that commit — so this is a no-op then.
        if self._noworktree_auto:
            self._auto_fold_latent_pending()
        if getattr(self, "_manual_hooks_installed", False):
            signal_file = self._manual_agit_dir() / "manual-commit-signal"
            try:
                mtime = signal_file.stat().st_mtime
            except OSError:
                mtime = None
            try:
                head = self.repo.rev_parse("HEAD")
            except Exception:
                return
            signalled = mtime is not None and mtime != getattr(self, "_manual_signal_mtime", None)
            moved = head != self._manual_last_head
            if signalled or moved:
                self._manual_signal_mtime = mtime
                self._manual_last_head = head
                # A commit happened: if it absorbed the pending turns (tree now clean, or the
                # tip is already in HEAD), drop the stale chain. The post-commit hook normally
                # did this already; doing it here too makes it robust to a bypassed hook.
                self._reset_stale_manual_ref()
                self._render_manual_trailer()
        else:
            self._reconcile_manual_external_commit()

    def _reconcile_manual_external_commit(self) -> None:
        """Poll+cover FALLBACK for when the fold hook can't run (custom core.hooksPath): if
        HEAD moved since we last looked and pending latent turns exist, the user committed
        outside the hook — add a cover commit carrying the pending tracking (its tree equals
        the new HEAD's, so it introduces no diff), then reset the latent ref. A no-op when
        the hook is installed (it already folded the tracking and reset the ref)."""
        if not self._latent_tracking or getattr(self, "_manual_hooks_installed", False):
            return
        try:
            head = self.repo.rev_parse("HEAD")
        except Exception:
            return
        last = getattr(self, "_manual_last_head", None)
        if last is None:
            self._manual_last_head = head
            return
        if head == last:
            return
        self._manual_last_head = head
        tip = self.repo.ref_sha(self._manual_ref())
        bodies = self._manual_pending_bodies()
        if not tip or not bodies:
            self._render_manual_trailer()
            return
        message = "<aGiTrack> track agent turns\n\n" + build_manual_squash_trailer(
            agitrack_session_id=self.state.session_id, latent_bodies=bodies, repo_root=self.repo.repo
        )
        try:
            head_tree = self.repo.rev_parse("HEAD^{tree}")
            self.repo.cover_commit(message, first_parent=head, second_parent=tip, tree=head_tree)
            self._manual_last_head = self.repo.rev_parse("HEAD")
            self.repo.update_ref(self._manual_ref(), self.repo.rev_parse("HEAD"))
        except Exception as error:
            self._debug(f"manual cover reconcile failed: {error!r}")
        self._render_manual_trailer()

    def _auto_fold_latent_pending(self, *, force: bool = False) -> None:
        """No-worktree AUTO mode: fold the pending latent turns into a real commit ourselves, so
        the branch advances per turn (like normal auto mode) but the interaction trace/metadata
        rides the SAME commit as the code — no separate cover. A clean working tree means the agent
        (or user) already committed its work, in which case the prepare-commit-msg fold hook folded
        the tracking into THAT commit (cover being only the backup), so there is nothing to do.

        ``force`` skips the summary-defer wait below: it is set on the EXIT finalize, where the
        summary has already been joined and serviced separately, and where we must land the commit
        NOW — the throttled poll that normally folds does not run during teardown, so without this
        the turn's changes would stay recorded only as a latent commit and never reach HEAD (the
        reported 'commit not made, not even on exit' bug in no-worktree auto mode)."""
        if not self._noworktree_auto:
            return
        tip = self.repo.ref_sha(self._manual_ref())
        if not tip:
            return
        try:
            if self.repo.snapshot_worktree_tree() == self.repo.comparable_tree("HEAD"):
                return  # clean vs HEAD ⇒ agent/user committed; the fold hook handled it
        except Exception:
            return
        # Hold the fold briefly while this turn's summary is still in flight: the summary lands
        # as a note on the (never-HEAD) latent commit, which _manual_pending_bodies folds into the
        # message below — but only if it has arrived first. Without this wait the poll folds within
        # ~3s, long before the multi-second LLM summary returns, so the folded commit keeps its
        # prompt-based subject and the summary is orphaned as a note (the reported no-worktree bug).
        # Bounded by SUMMARY_WAIT_SECONDS: past the deadline we fold as-is, summary becomes notes-only.
        if not force and self._summary_blocks_integration(time.monotonic()):
            return
        bodies = self._manual_pending_bodies()
        message = build_auto_fold_message(bodies, repo_root=self.repo.repo)
        if not message:
            return
        try:
            folded = list(self.repo.log_shas("HEAD", tip))  # the latent turns this fold absorbs
        except Exception:
            folded = []
        try:
            self.repo.add_tracked()
            declined = set(self.state.declined_untracked())
            # The user's own untracked files (see _begin_agent_turn) are NOT this turn's output:
            # staging them here put a file the user never agreed to commit into an agent commit
            # that says nothing about it. They stay untracked until the user stages them.
            theirs = self.user_untracked_since_fold or frozenset()
            self.repo.stage_paths([p for p in self.repo.untracked_entries() if p not in declined and p not in theirs])
            if not self.repo.has_staged_changes():
                return
            # The message already carries the folded metadata, so the prepare-commit-msg hook's
            # idempotency check skips re-appending it; the post-commit hook resets the latent ref.
            sha = self.repo.commit(message)
            self._reset_stale_manual_ref()
            self._manual_last_head = self.repo.rev_parse("HEAD")
            self._render_manual_trailer()
            self._last_agent_commit_id = sha
            self._sessions_with_activity.add(self.state.session_id)
            # Remember which latent turns this fold absorbed, so a summary that lands AFTER the
            # deadline can still be amended into the real commit instead of being stranded on the
            # latent one (see _folded_head_for). Only the newest fold is tracked: once HEAD moves
            # on, amending is no longer safe anyway.
            self._last_auto_fold = {
                "head": self._manual_last_head,
                "latent": folded,
                "single": len(bodies) == 1,
            }
            # This fold is closed: the next turn re-establishes which untracked files are the
            # user's (they may have staged, deleted or added some in the meantime).
            self.user_untracked_since_fold = None
        except Exception as error:
            self._debug(f"auto fold failed: {error!r}")
