"""Branch/merge/integration policy for aGiT worktree sessions (#29, P5).

:class:`IntegrationService` encapsulates the git operations that manage the
lifecycle of session turn branches and their integration into the base branch.
It is deliberately UI-free: every method returns a plain outcome (a string or
dataclass) and the ProxyRunner renders messages / popups in response.

The service takes all its inputs as explicit parameters (``base_repo``,
``base_branch``, per-call ``repo`` / ``name`` / ``worktree``) so it is safe
to call from inside temp-swap windows — it never reads ``runner.active``
implicitly.

Merge-context state machine
---------------------------

The production code uses at most **one** active merge resolution at a time per
session (the ``merge_ctx`` field on a ``Session`` object).  The abandoned
:mod:`agit.merge_queue` module modelled a *queue* of pending merges serialised
through a MergeCoordinator — a design that does not map onto the existing
single-resolution-at-a-time flow:

* MergeCoordinator queues sessions and processes them one at a time, while the
  production code resolves each session's merge independently and concurrently
  (background sessions are serviced via ``_with_session`` from the event loop).
* The ``prompt_sent_at`` timing in the production flow (an Enter keystroke
  arrives asynchronously after the text is injected) has no counterpart in the
  MergeParticipant protocol.
* Adopting the queue model would change *when* merges start and finalize, which
  is an observable behavior change outside the allowed scope (P5 only permits
  formalising the state machine).

Decision: ``agit/merge_queue.py`` and its tests are deleted. Instead, the
ad-hoc ``merge_ctx`` dict is replaced here by a :class:`MergeContext`
dataclass with an explicit :class:`MergePhase` enum, preserving all existing
timing semantics byte-for-byte.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from agit.commits import build_agent_merge_message
from agit.git import GitRepo
from agit.git import WorktreeManager

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Merge-context state machine
# ---------------------------------------------------------------------------


class MergePhase(Enum):
    """Lifecycle of an in-progress agent-assisted merge resolution.

    PENDING   — merge started, conflict injected into the backend, waiting for
                the submit-Enter keystroke to land (``prompt_sent_at`` is None).
    RESOLVING — Enter was sent; waiting for the agent to finish the resolution
                turn (``prompt_sent_at`` is set; agent output is expected).
    MANUAL    — user chose manual resolution: agent NOT involved.  User edits
                files and then triggers "Complete merge" explicitly.
    """

    PENDING = "pending"
    RESOLVING = "resolving"
    MANUAL = "manual"


@dataclass
class MergeContext:
    """In-progress agent-assisted merge resolution.  Stored on ``Session.merge_ctx``.

    ``auto_tried`` encodes the old ``merge_ctx["auto_tried"]`` flag:
    - For AUTO contexts (agent resolves conflicts): starts False; set to True
      after the first auto-finalize attempt so it is never tried twice.
    - For MANUAL contexts (user resolves): set to True at creation so
      ``should_auto_complete_merge`` never fires for them.

    ``phase`` formalises the lifecycle:
    - PENDING  → auto: merge started, prompt injected, waiting for Enter to land.
    - RESOLVING → auto: Enter sent; agent output expected.
    - MANUAL   → user resolves manually; no auto-finalize ever.

    The dict-shim methods (``get``, ``__getitem__``, ``__setitem__``,
    ``__contains__``) are kept because existing runner call sites use
    ``ctx["prompt_sent_at"]`` and ``ctx.get("auto_tried")`` notation;
    production always constructs MergeContext (never plain dicts).
    """

    source_branch: str
    context: str
    started: float = field(default_factory=time.monotonic)
    phase: MergePhase = MergePhase.PENDING
    auto_tried: bool = False
    prompt_sent_at: float | None = None

    # dict-style attribute access shim so existing ``ctx.get(key)`` /
    # ``ctx[key]`` call sites keep working without edits.
    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def __contains__(self, key):
        return hasattr(self, key)


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------

INTEGRATED = "integrated"
CONFLICT = "conflict"
SKIP = "skip"


# ---------------------------------------------------------------------------
# IntegrationService
# ---------------------------------------------------------------------------


class IntegrationService:
    """Stateless (except for ``base_repo`` / ``base_branch``) integration logic.

    The runner creates one instance and keeps it alive; ``base_branch`` is
    updated in-place when the user switches the base (``_perform_base_switch``).

    All git-mutating methods are called with the *session's* ``GitRepo`` passed
    explicitly, never inferred from ``runner.active``, so they are safe inside
    temp-swap windows.
    """

    def __init__(self, base_repo: GitRepo, base_branch: str | None, *, menu_label: str = "Ctrl-G") -> None:
        self.base_repo = base_repo
        self.menu_label = menu_label
        self.base_branch = base_branch  # mutable; updated by runner on base-switch

    @property
    def _base(self) -> str:
        # base_branch is None only before the runner sets it at startup; every
        # branch/merge/integrate method runs after that. Assert-and-return so
        # those call sites type-check and the precondition stays loud.
        assert self.base_branch is not None, "IntegrationService.base_branch is not set"
        return self.base_branch

    # ------------------------------------------------------------------
    # Turn-branch lifecycle
    # ------------------------------------------------------------------

    def turn_from_branch(self, branch: str) -> int:
        """Parse the turn number from an agit turn-branch name."""
        match = re.search(r"/t(\d+)$", branch or "")
        return int(match.group(1)) if match else 0

    def ensure_turn_branch(
        self,
        repo: GitRepo,
        worktree,
        turn: int,
        worktree_manager: WorktreeManager,
        session_name: str,
        backend_name: str,
    ) -> int:
        """Create a new turn branch if the session is detached (between turns).

        Returns the new turn number (unchanged if a branch already existed).
        """
        if worktree is None or not repo.is_detached():
            return turn
        current = (turn or 0) + 1
        next_branch = worktree_manager.turn_branch(session_name, current, backend=backend_name)
        while repo.branch_exists(next_branch):
            current += 1
            next_branch = worktree_manager.turn_branch(session_name, current, backend=backend_name)
        repo.switch(next_branch, create=True)
        return current

    # ------------------------------------------------------------------
    # Base-alignment helpers
    # ------------------------------------------------------------------

    def align_session_to_base(self, repo: GitRepo) -> str:
        """Bring an idle session worktree up to date with the base branch.

        Two cases:
        - No unintegrated commits → re-point (detach) at the current base.
        - Has its own work → merge new base commits in cleanly; skip on conflict.
        A dirty or mid-merge worktree is left untouched.

        Returns a short outcome string for the caller to log:
        - ``"merged:<branch>"``  — base merged cleanly into session branch.
        - ``"conflict:<branch>"`` — base conflicts with session branch; aborted.
        - ``"repointed"``        — session worktree re-pointed to base.
        - ``"noop"``             — nothing to do.

        Raises on unexpected git errors so the caller can log them.
        """
        if self.base_branch is None:
            return "noop"
        if repo.merge_in_progress() or repo.has_changes():
            return "noop"
        branch = repo.current_branch()
        if branch.startswith("agit/") and self.base_repo.log_range(self._base, branch):
            if not self.base_repo.log_range(branch, self.base_branch):
                return "noop"  # already contains the base
            if repo.merge(self.base_branch):
                return f"merged:{branch}"
            else:
                repo.merge_abort()  # conflicts; leave for integration
                return f"conflict:{branch}"
        if repo.rev_parse("HEAD") != self.base_repo.rev_parse(self.base_branch):
            repo.switch_detach(self._base)
            return "repointed"
        return "noop"

    def worktree_has_pending_work(self, repo: GitRepo, branch: str) -> bool:
        """Return True if the worktree has uncommitted changes or unintegrated commits."""
        try:
            if repo.has_changes():
                return True
            return bool(self.base_repo.log_range(self._base, branch))
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Core integration
    # ------------------------------------------------------------------

    def integrate_turn_or_conflict(
        self,
        repo: GitRepo,
        name: str,
        worktree,
        merge_ctx,
        integration_paused: bool,
    ) -> tuple[str, str]:
        """Try to merge the base into the session's turn branch.

        Returns ``(outcome, turn_branch)`` where outcome is one of
        ``INTEGRATED``, ``CONFLICT``, or ``SKIP``.

        On ``INTEGRATED`` the base has been merged into the turn branch (clean
        merge) but the caller is responsible for fast-forwarding the base
        (``_advance_base_to`` / ``advance_base_to``) — this lets tests mock
        that step independently.
        Never touches UI.
        """
        if worktree is None or self.base_branch is None or merge_ctx:
            return SKIP, ""
        if integration_paused:
            return SKIP, ""
        turn_branch = repo.current_branch()
        if not turn_branch.startswith("agit/"):
            return SKIP, ""
        try:
            if not repo.merge(self._base):
                repo.merge_abort()
                return CONFLICT, turn_branch
            return INTEGRATED, turn_branch
        except Exception:
            return SKIP, ""

    def advance_base_to(self, repo: GitRepo, source_branch: str) -> None:
        """Fast-forward the base to ``source_branch``, detach the session, delete the turn branch.

        The session already merged the base into its turn branch, so the turn
        branch is a descendant of the base and this is always a fast-forward. When
        the base branch is the checked-out one in the base repo, advance it with a
        working-tree fast-forward; when the user has ``git checkout``ed a different
        branch in the directory, advance the base ref directly (still a true
        fast-forward, never a force) so integration keeps landing on the original
        base instead of stalling.
        """
        if self.base_repo.current_branch() == self._base:
            self.base_repo.merge_ff_only(source_branch)
        else:
            # Raises GitError if not a real fast-forward — never drops commits.
            self.base_repo.fast_forward_branch(self._base, source_branch)
        repo.switch_detach(self._base)
        if repo.current_branch() != source_branch:
            self.base_repo.delete_branch(source_branch, force=True)

    def _advance_base(self, repo: GitRepo, source_branch: str) -> None:
        """Internal helper that calls advance_base_to without raising to callers."""
        self.advance_base_to(repo, source_branch)

    def integrate_session_on_exit(self, repo: GitRepo, merge_ctx) -> None:
        """Clean up a session's branch on exit: integrate committed work or drop empty branch.

        Conflicts and dirty trees are left intact for the next startup.
        Raises on unexpected git errors so the caller can log them.
        """
        if self.base_branch is None:
            return
        if merge_ctx or repo.merge_in_progress() or repo.has_changes():
            return
        branch = repo.current_branch()
        if not branch.startswith("agit/"):
            return
        if not self.base_repo.log_range(self.base_branch, branch):
            # Nothing ahead of base: just drop the empty turn branch.
            self._advance_base(repo, branch)
            return
        if repo.merge(self.base_branch):
            self._advance_base(repo, branch)
        else:
            repo.merge_abort()

    # ------------------------------------------------------------------
    # Merge-context lifecycle
    # ------------------------------------------------------------------

    def make_merge_context(
        self,
        repo: GitRepo,
        source_branch: str,
        *,
        manual: bool = False,
    ) -> MergeContext:
        """Build a ``MergeContext`` for a newly-started merge resolution.

        Collects conflicting-commit context from the base repo; merges are
        called with the conflict already in progress in ``repo``.
        """
        files = repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self._base, paths=files)
        except Exception:
            context = ""
        phase = MergePhase.MANUAL if manual else MergePhase.PENDING
        return MergeContext(
            source_branch=source_branch,
            context=context,
            phase=phase,
            auto_tried=manual,  # manual = auto was never tried / not desired
        )

    def merge_resolution_prompt(self, repo: GitRepo, source_branch: str) -> str:
        """Build the conflict-resolution prompt text to inject into the agent."""
        files = repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self._base, paths=files)
        except Exception:
            context = ""
        listing = ", ".join(files) if files else "the conflicted files"
        commits = context.replace("\n", "; ") if context else "(none recorded)"
        return (
            f"[aGiT] Merge conflict: the base branch '{self.base_branch}' gained changes from another "
            f"session that conflict with your work in {listing}. The conflicting base commits are: {commits}. "
            "Please open the conflicted files, resolve every <<<<<<< / ======= / >>>>>>> marker keeping both "
            "changes' intent, and save. Do NOT run git or commit — aGiT will create the merge commit once you are done."
        )

    def should_auto_complete_merge(
        self,
        merge_ctx: MergeContext,
        last_child_output: float,
        child_idle_seconds: float,
    ) -> bool:
        """Return True if conditions are met to attempt auto-finalizing a merge.

        The auto-complete fires only after:
        1. The submit-Enter has been sent (``prompt_sent_at`` is set).
        2. The agent has produced output after that Enter.
        3. The agent has been idle for ``child_idle_seconds + 2`` seconds.
        4. Auto-complete has not already been attempted (``auto_tried`` is False).

        MANUAL contexts always have ``auto_tried=True`` and are never auto-finalized.
        """
        if merge_ctx.auto_tried:
            return False
        sent_at = merge_ctx.prompt_sent_at
        if not sent_at:  # the submit Enter has not gone out yet
            return False
        if last_child_output <= sent_at:  # agent has not responded yet
            return False
        if time.monotonic() - last_child_output < child_idle_seconds + 2:
            return False
        return True

    def finalize_agent_merge(
        self,
        repo: GitRepo,
        merge_ctx: MergeContext,
        *,
        session_name: str,
        agit_session_id: str,
        backend_name: str,
        backend_session_id: str | None,
    ) -> tuple[bool, str | None]:
        """Attempt to finalize a pending agent merge.

        Returns ``(success, message)`` where ``message`` is a UI string to
        display (or None when merge is not in progress).  On success the caller
        must clear ``session.merge_ctx`` and clear ``agent_in_flight``.

        Raises on unexpected git errors so the caller can log and keep
        ``merge_ctx`` intact (the user should still be able to retry).
        """
        source_branch = merge_ctx.source_branch
        ctx_context = merge_ctx.context
        if not repo.merge_in_progress():
            return False, None  # already resolved/aborted elsewhere
        repo.add_all()
        if repo.has_conflict_markers() or repo.unmerged_paths():
            return False, (
                "Conflict markers remain. Resolve them (or ask the agent again), "
                f"then {self.menu_label} → session → Complete merge."
            )
        repo.commit(
            build_agent_merge_message(
                session_name=session_name,
                base_branch=self._base,
                source_branch=source_branch,
                agit_session_id=agit_session_id,
                backend=backend_name,
                backend_session_id=backend_session_id,
                conflicting_commits=ctx_context,
            )
        )
        self._advance_base(repo, source_branch)
        return True, f"Merge resolved and committed — integrated '{session_name}' into {self.base_branch}."

    def start_merge(
        self,
        repo: GitRepo,
        name: str,
        worktree,
        *,
        auto: bool,
    ) -> tuple[str, MergeContext | None, str | None]:
        """Begin merging the base into the session's current turn branch.

        Returns ``(outcome, merge_ctx_or_none, message_or_none)``:
        - ``("clean", None, msg)`` — merged without conflicts; base advanced.
        - ``("conflict_auto", ctx, msg)`` — conflict; agent merge started (caller must inject prompt).
        - ``("conflict_manual", ctx, msg)`` — conflict; user resolves manually.
        - ``("error", None, msg)`` — merge could not start.
        - ``("skip", None, None)`` — no worktree or base branch.
        """
        if worktree is None or self.base_branch is None:
            return "skip", None, None
        source_branch = repo.current_branch()
        try:
            clean = repo.merge(self.base_branch)
        except Exception as error:
            return "error", None, f"Could not start merge: {error}"
        if clean:
            self._advance_base(repo, source_branch)
            return "clean", None, f"Integrated '{name}' into {self.base_branch} (no conflicts)."
        # Conflict: build context (files + log) once
        files = repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self._base, paths=files)
        except Exception:
            context = ""
        ctx = MergeContext(
            source_branch=source_branch,
            context=context,
            phase=MergePhase.PENDING if auto else MergePhase.MANUAL,
            auto_tried=not auto,
        )
        if auto:
            return "conflict_auto", ctx, None
        msg = (
            f"Conflicts in {', '.join(files) or 'this session'}. Resolve them here (edit the files or ask the agent), "
            f"then {self.menu_label} → session → Complete merge."
        )
        return "conflict_manual", ctx, msg

    # ------------------------------------------------------------------
    # Cleanup / stale-worktree recovery
    # ------------------------------------------------------------------

    def cleanup_stale_worktree(
        self,
        info,
        worktree_manager: WorktreeManager,
    ) -> bool:
        """Integrate a dormant worktree's pending commits and delete it.

        Returns False (keep + flag) when it has uncommitted changes or its work
        conflicts with the base and needs manual resolution.
        """
        repo = GitRepo(info.path)
        if repo.merge_in_progress() or repo.has_changes():
            return False
        branch = repo.current_branch()
        if branch.startswith("agit/") and self.base_repo.log_range(self._base, branch):
            if not repo.merge(self._base):
                repo.merge_abort()
                return False
            self.base_repo.merge_ff_only(branch)
        worktree_manager.remove(info.name)
        return True

    def delete_orphan_merged_branches(self) -> list[str]:
        """Remove agit/* branches that no worktree checks out and are in the
        base. Returns the deleted branch names so the caller can log each one
        (matching the old per-branch debug line)."""
        deleted: list[str] = []
        checked_out = {entry.get("branch") for entry in self.base_repo.worktree_list()}
        for branch in self.base_repo.list_branches("agit/"):
            if branch in checked_out:
                continue
            if not self.base_repo.log_range(self._base, branch):
                self.base_repo.delete_branch(branch, force=True)
                deleted.append(branch)
        return deleted

    # ------------------------------------------------------------------
    # Session / base predicates
    # ------------------------------------------------------------------

    def session_unintegrated(self, repo: GitRepo | None) -> bool:
        """Return True if a session still has work that did not make it into the base."""
        try:
            if repo is None or repo.merge_in_progress() or repo.has_changes():
                return True
            branch = repo.current_branch()
            return branch.startswith("agit/") and bool(self.base_repo.log_range(self._base, branch))
        except Exception:
            return True

    def active_has_pending(self, repo: GitRepo, worktree) -> bool:
        """Return True if the active session has committed work not yet in the base."""
        if worktree is None or self.base_branch is None:
            return False
        try:
            return bool(self.base_repo.log_range(self.base_branch, repo.current_branch()))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Base-switch helpers
    # ------------------------------------------------------------------

    def base_switch_candidates(self) -> list[str]:
        """Return user branches the base could switch to (never agit/ branches)."""
        try:
            branches = self.base_repo.list_branches()
        except Exception:
            return []
        return [b for b in branches if not b.startswith("agit/") and b != self.base_branch]

    def repoint_to_base(self, repo: GitRepo, worktree) -> int | None:
        """Detach a session worktree at the new base; returns 0 (new turn counter),
        or None for the silent dirty/mid-merge skip. Errors raise so the caller
        can log them (the old code logged the error repr, not a generic skip)."""
        if worktree is None:
            return None
        if repo.has_changes() or repo.merge_in_progress():
            return None
        repo.switch_detach(self._base)
        return 0

    # ------------------------------------------------------------------
    # Base-drift / poll helpers
    # ------------------------------------------------------------------
    #
    # Base-branch drift (the user `git checkout`ed a different branch in the
    # directory) is handled by ProxyRunner._check_base_branch_drift, which
    # prompts the user to switch the base, keep integrating into the original
    # (advance_base_to fast-forwards its ref directly), or pause.

    def poll_base_advanced(
        self,
        worktree,
        last_base_head: str | None,
        last_poll_at: float,
        base_poll_seconds: float,
    ) -> tuple[str | None, float, bool]:
        """Poll the base HEAD and detect out-of-band advances.

        Returns ``(new_head, new_poll_at, advanced)`` where ``advanced`` is True
        when the base moved since the last poll.  Returns ``(None, last_poll_at,
        False)`` when throttled, and ``(None, now, False)`` on failure (the
        caller is responsible for catching exceptions and logging them).
        """
        if worktree is None or self.base_branch is None:
            return None, last_poll_at, False
        now = time.monotonic()
        if now - last_poll_at < base_poll_seconds:
            return None, last_poll_at, False
        try:
            head = self.base_repo.rev_parse(self.base_branch)
        except Exception:
            raise
        advanced = last_base_head is not None and head != last_base_head
        return head, now, advanced
