"""CommitEngine: the agent-commit pipeline extracted from ProxyRunner (#29, P4).

This module owns the stateless core of every agent-commit path:

* :meth:`CommitEngine.commit_turns` — trace-rebuild, token-accounting, commit-
  message assembly and ``git commit``.  Callers inject the two interactive
  behaviour differences as small callables so ONE implementation serves all
  three modes:

  - **proxy interactive** (ProxyRunner, popup UI):
      ``stage_untracked_fn = runner._review_untracked_popup``
      ``pre_commit_fn      = runner._ensure_turn_branch``
      ``on_commit_fn       = lambda sha: runner._set_message(...)``

  - **proxy exit / background** (non-interactive):
      ``stage_untracked_fn`` = auto-stage-non-declined closure
      (no ``pre_commit_fn``, no ``on_commit_fn``)

  - **actions / shell** (``AgitActions.create_agent_commit_from_turns``):
      ``stage_untracked_fn`` = ``actions.review_untracked(...)``
      (no ``pre_commit_fn``, no ``on_commit_fn``) — caller prints its own
      confirmation.

* :meth:`CommitEngine.finish_parse_if_ready` — awaited-followup logic and
  turn-completeness gate, extracted from
  ``ProxyRunner._finish_agent_parse_if_ready``.

* :meth:`CommitEngine.start_parse` — parse-worker launcher extracted from
  ``ProxyRunner._start_agent_parse``.  Results are always written back to the
  *owning* :class:`~agit.proxy.session.Session` (issue #15 fix, preserved
  verbatim).

* :meth:`CommitEngine.record_user_prompt` /
  :meth:`CommitEngine.await_followup` — thin state helpers.

* :meth:`CommitEngine.sanitize_state_trace` /
  :meth:`CommitEngine.initialize_session_baseline` /
  :meth:`CommitEngine.recover_nonempty_session` — session-startup helpers.

Design constraints
------------------
Every method that operates on git/state takes the *session bundle*
``(repo, state)`` (or a full :class:`~agit.proxy.session.Session`) explicitly
rather than touching ``self.active`` on the runner.  That makes it safe to
call from inside temp-swap windows (``_with_session``, ``_pump_background``,
``_stop_session``, ``_finalize_pending_work``) where ``active`` points at a
SERVICED session that may differ from the UI session.

What stays on ProxyRunner (not extracted here)
----------------------------------------------
* Debounce/status bookkeeping inside ``_maybe_agent_commit`` (uses
  runner-level ``file_change_event``, ``last_status``, ``last_poll``, …).
* ``_commit_latest_turn_sync`` and ``_finalize_pending_work`` (orchestrate the
  sync/exit flow over multiple sessions — runner scope).
* ``_commit_available_agent_turns`` (thin two-liner).
* ``_agent_commit_message`` (formats the UI popup text from
  ``_last_agent_commit_id`` stored on the runner).
* ``_ensure_turn_branch`` (worktree-branch management — requires
  ``self._worktrees()`` etc.).
* ``_mirror_session_to_base``, ``_stage_backend_resume``,
  ``_note_backend_session_change``, ``_should_continue_session``,
  ``_integrate_session_turn`` — runner-level coordination.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from agit.commits import build_agent_commit_message
from agit.git import GitRepo
from agit.transcripts.opencode import SessionTurn
from agit.transcripts import turns_after
from agit.config import AgitState


# ---------------------------------------------------------------------------
# Type aliases used in method signatures
# ---------------------------------------------------------------------------

_StageUntrackedFn = Callable[[GitRepo, AgitState], None]
"""Called to review/stage untracked files; receives (repo, state)."""

_PreCommitFn = Callable[[], None]
"""Called immediately before the ``git commit`` (e.g. ``_ensure_turn_branch``)."""

_OnCommitFn = Callable[[str | None], None]
"""Called with the short commit SHA after a successful commit (may be None)."""

_DebugFn = Callable[..., None]
"""Logging sink — ``runner._debug``."""


class CommitEngine:
    """Stateless agent-commit engine bound to a ``(repo, state)`` pair.

    The instance is cheap to construct and short-lived: the runner (or
    ``AgitActions``) creates one per commit call.  Nothing is cached on the
    instance between calls.
    """

    def __init__(
        self,
        repo: GitRepo,
        state: AgitState,
        *,
        debug_fn: _DebugFn | None = None,
    ) -> None:
        self.repo = repo
        self.state = state
        self._debug = debug_fn or (lambda *a, **kw: None)

    # ------------------------------------------------------------------
    # Core commit pipeline
    # ------------------------------------------------------------------

    def commit_turns(
        self,
        *,
        turns: list[SessionTurn],
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        stage_untracked_fn: _StageUntrackedFn,
        pre_commit_fn: _PreCommitFn | None = None,
        on_commit_fn: _OnCommitFn | None = None,
        session_name: str | None = None,
        accumulate_trace_only_on_commit: bool = False,
    ) -> bool:
        """Core of every agent-commit path.

        Rebuilds the pending trace from *turns*, gates on ``has_staged_changes``,
        accumulates tokens, builds and records the commit.  Interactive
        differences (untracked review, branch preparation, message display) are
        fully injected so the same pipeline serves proxy-popup, proxy-quiet and
        actions/shell modes.

        Token usage is accumulated **only** once the commit actually happens
        (i.e. after ``has_staged_changes`` returns True).  A failed attempt
        (nothing staged) reprocesses the same turns on the next parse — the
        trace is rebuilt from scratch each call, but token usage is cumulative
        and would otherwise double-count.  (Bug #14, fixed in d041d10; both
        copies of the logic now share this single implementation.)

        Parameters
        ----------
        accumulate_trace_only_on_commit:
            When ``True`` (actions/shell mode), stage-check happens first and
            trace is only accumulated once the commit will definitely happen —
            leaving state pristine on a failed attempt so the caller can retry
            with fresh staging without a partial trace.  When ``False`` (proxy
            mode, the default), the trace is rebuilt before the stage check so
            the pending-user-merging logic can run even on failed attempts.

        Returns ``True`` if a commit was made, ``False`` otherwise.
        """
        if not turns:
            return False

        if accumulate_trace_only_on_commit:
            # Actions / shell mode: do the staged check first, accumulate only
            # when we know the commit will happen.  Leaves state pristine on
            # a failed attempt (nothing staged → no trace, no tokens written).
            # The pre-commit hook runs first either way (same ordering as the
            # proxy branch) so the parameter contract is mode-independent.
            if pre_commit_fn is not None:
                pre_commit_fn()
            self.repo.add_tracked()
            stage_untracked_fn(self.repo, self.state)
            if not self.repo.has_staged_changes():
                return False
            # Commit will happen: accumulate trace and tokens now.
            for turn in turns:
                if turn.user_prompt:
                    self.state.append_trace("user", turn.user_prompt)
                if turn.final_response:
                    self.state.append_trace("agent", turn.final_response)
                self.state.add_token_usage(turn.tokens)
            prompts = [t.user_prompt for t in turns if t.user_prompt]
            subject_text = " / ".join(prompts) if prompts else f"{backend} changes"
        else:
            # Proxy mode: rebuild trace from scratch, preserving any pending user
            # entries that hadn't yet landed as a turn (e.g. a queued prompt from
            # before this parse cycle).
            pending_users: list[str] = [
                content
                for item in self.state.pending_trace()
                if item.get("role") == "user" and (content := item.get("content"))
            ]
            remaining_pending_users = list(pending_users)
            self.state.data["pending_trace"] = []
            self.state.save()

            subject_prompts: list[str] = []
            for turn in turns:
                if turn.user_prompt:
                    subject_prompts.append(turn.user_prompt)
                    self.state.append_trace("user", turn.user_prompt)
                    if turn.user_prompt in remaining_pending_users:
                        remaining_pending_users.remove(turn.user_prompt)
                if turn.final_response:
                    self.state.append_trace("agent", turn.final_response)

            # Pending user entries that never showed up as a turn's user_prompt
            # (e.g. an incomplete initial turn that has only partial data) are still
            # added to the subject and trace so they appear in the commit body.
            for pending_user in remaining_pending_users:
                subject_prompts.append(pending_user)
                self.state.append_trace("user", pending_user)

            # Hook: proxy mode puts the session on a fresh turn branch here.
            if pre_commit_fn is not None:
                pre_commit_fn()

            self.repo.add_tracked()
            stage_untracked_fn(self.repo, self.state)

            if not self.repo.has_staged_changes():
                return False

            # Accumulate tokens only once we know the commit will happen.
            for turn in turns:
                self.state.add_token_usage(turn.tokens)

            subject_text = " / ".join(subject_prompts) if subject_prompts else f"{backend} changes"

        commit_sha = self.repo.commit(
            build_agent_commit_message(
                latest_prompt=subject_text,
                trace=self.state.pending_trace(),
                backend=backend,
                backend_session_id=backend_session_id,
                agit_session_id=self.state.session_id,
                model=model or self.state.model,
                token_usage=self.state.pending_token_usage(),
                trace_turn_limit=self.state.trace_turn_limit,
                session_name=session_name,
            )
        )
        self.state.clear_trace()

        if on_commit_fn is not None:
            on_commit_fn(commit_sha)

        return True

    # ------------------------------------------------------------------
    # Parse-result consumption (extracted from _finish_agent_parse_if_ready)
    # ------------------------------------------------------------------

    def finish_parse_if_ready(
        self,
        *,
        session,  # agit.proxy.session.Session
        quiet: bool,
        prompt_untracked: bool,
        require_complete: bool,
        awaited_followups: list[str],
        agent_is_active_fn: Callable[[], bool],
        debug_fn: _DebugFn,
        note_session_change_fn: Callable[[str], None],
        mirror_fn: Callable[[str | None], None],
        commit_fn: Callable,
    ) -> tuple[bool | None, list[str]]:
        """Consume a ready parse result and (conditionally) commit.

        Extracted from ``ProxyRunner._finish_agent_parse_if_ready``.  The
        caller retains ownership of the ``awaited_followups`` list; the
        updated list is returned as the second element of the tuple.

        ``commit_fn`` is called with ``(turns=..., backend=...,
        backend_session_id=..., model=..., quiet=..., prompt_untracked=...)``
        keyword arguments when a commit should happen.  The runner passes
        ``self._create_agent_commit_from_turns_popup`` so test mocks stay
        effective; ``AgitActions`` passes its own pipeline wrapper.

        Returns
        -------
        (result, new_awaited)
            *result* is ``True`` (committed), ``False`` (consumed, no commit),
            or ``None`` (deferred / no result ready).
        """
        parse_thread = session.agent_parse_thread
        if parse_thread and parse_thread.is_alive():
            return None, awaited_followups

        parse_result = session.agent_parse_result
        if parse_result is None:
            return None, awaited_followups

        session.agent_parse_result = None
        session_id, exported_session, last_message_id, owner_state = parse_result

        if owner_state is not None and owner_state is not self.state:
            debug_fn("discarding agent parse result owned by another session")
            return None, awaited_followups

        if not exported_session:
            debug_fn(f"agent parse consumed without session session_id={session_id}")
            return False, awaited_followups

        new_session_id = exported_session.session_id or session_id
        note_session_change_fn(new_session_id)
        self.state.backend_session_id = new_session_id
        mirror_fn(new_session_id)

        if exported_session.model:
            self.state.model = exported_session.model

        all_turns = turns_after(exported_session, last_message_id)

        # Awaited-followup logic: a prompt queued while the agent was busy
        # belongs in the same commit as the turn it triggered.
        awaited = list(awaited_followups)
        if awaited and any(getattr(t, "interrupted", False) for t in all_turns):
            awaited = []
        if awaited:
            seen = {" ".join((t.user_prompt or "").split()) for t in exported_session.turns}
            awaited = [p for p in awaited if p not in seen]
            if require_complete and awaited and agent_is_active_fn():
                debug_fn(f"deferring agent commit: {len(awaited)} queued follow-up(s) not yet in transcript")
                return None, awaited
            awaited = []  # committing now — drop cancelled queue entries

        if require_complete and all_turns and not all_turns[-1].complete:
            debug_fn(f"deferring agent commit: latest turn still in progress session_id={new_session_id}")
            return None, awaited

        complete_turns = [t for t in all_turns if t.final_response]
        if not complete_turns:
            debug_fn(
                f"agent parse consumed without final response "
                f"session_id={self.state.backend_session_id} turns={len(all_turns)}"
            )
            return False, awaited

        committed = commit_fn(
            turns=all_turns,
            backend=session.backend.name,
            backend_session_id=self.state.backend_session_id,
            model=exported_session.model or self.state.model,
            quiet=quiet,
            prompt_untracked=prompt_untracked,
        )
        if committed:
            # Advance the watermark so the next parse cycle only exports new turns.
            self.state.last_backend_message_id = complete_turns[-1].assistant_message_id
            debug_fn(
                f"agent commit created session_id={self.state.backend_session_id} "
                f"assistant_id={self.state.last_backend_message_id}"
            )
        return committed, awaited

    # ------------------------------------------------------------------
    # Parse-worker launcher (extracted from _start_agent_parse)
    # ------------------------------------------------------------------

    def start_parse(
        self,
        *,
        session,  # agit.proxy.session.Session
        discover_session_id_fn: Callable[[], str | None],
        debug_fn: _DebugFn,
    ) -> bool:
        """Launch the background session-export worker for *session*.

        Verbatim semantics from ``ProxyRunner._start_agent_parse``:

        * Results are written back to the *owning* Session object (issue #15).
        * ``agent_parse_lock`` prevents double-launch.
        * Returns ``False`` if a worker is already running or a result is
          already pending.

        Parameters
        ----------
        session:
            The session whose backend will be exported.  All reads and writes
            happen on this object; the caller may swap ``active`` freely while
            the worker runs.
        discover_session_id_fn:
            Called only when ``session.worktree is None`` to discover the
            spawned backend session (``runner._discover_spawned_session``).
        debug_fn:
            Logging sink.
        """
        parse_lock = session.agent_parse_lock
        if parse_lock is None:
            parse_lock = threading.Lock()
            session.agent_parse_lock = parse_lock

        with parse_lock:
            if session.agent_parse_active:
                return False
            if session.agent_parse_thread and session.agent_parse_thread.is_alive():
                return False
            if session.agent_parse_result is not None:
                return False
            session.agent_parse_active = True

        last_message_id = session.state.last_backend_message_id
        owner = session
        backend = owner.backend
        repo = owner.repo
        state = owner.state
        worktree = owner.worktree

        def worker() -> None:
            result = None
            try:
                debug_fn("agent parse worker started")
                if worktree is not None:
                    # A worktree's directory is unique to this aGiT session, so
                    # the newest backend session there is always this session's
                    # current conversation — track it even if the user started a
                    # new conversation from inside the backend.
                    session_id = backend.latest_session_id(repo.repo) or state.backend_session_id
                else:
                    # No worktree isolation: stay pinned to the owned session.
                    session_id = state.backend_session_id or discover_session_id_fn()
                exported = backend.export_session(repo.repo, session_id) if session_id else None
                turn_count = len(exported.turns) if exported else 0
                final_count = len([t for t in exported.turns if t.final_response]) if exported else 0
                debug_fn(f"agent parse worker finished session_id={session_id} turns={turn_count} finals={final_count}")
                result = (session_id, exported, last_message_id, state)
            finally:
                with parse_lock:
                    owner.last_parse_finish = time.monotonic()
                    if result is not None:
                        owner.agent_parse_result = result
                    owner.agent_parse_active = False

        session.last_parse_start = time.monotonic()
        debug_fn(f"agent parse started last_message_id={last_message_id}")
        session.agent_parse_thread = threading.Thread(target=worker, name="agit-session-parse", daemon=True)
        session.agent_parse_thread.start()
        return True

    # ------------------------------------------------------------------
    # Simple state helpers
    # ------------------------------------------------------------------

    def record_user_prompt(self, prompt_text: str) -> None:
        """Append a user prompt to the pending trace (no-op if empty)."""
        if prompt_text:
            self.state.append_trace("user", prompt_text)

    def await_followup(self, prompt_text: str, awaited: list[str]) -> list[str]:
        """Return a new awaited list with *prompt_text* appended if appropriate.

        Slash commands (/model, /compact, …) are skipped because they are
        filtered from the transcript and would defer commits indefinitely.
        The updated list must be stored by the caller (on the runner or
        wherever ``_awaited_followups`` lives).
        """
        norm = " ".join((prompt_text or "").split())
        if norm and not norm.startswith("/"):
            return awaited + [norm]
        return awaited

    # ------------------------------------------------------------------
    # Session-baseline helpers (extracted from runner startup paths)
    # ------------------------------------------------------------------

    def sanitize_state_trace(self, backend) -> None:
        """Drop raw backend event blobs from the pending trace.

        Certain backends (e.g. OpenCode) persist large JSON event objects in
        the trace.  They are not human-readable and bloat commit messages.
        This strips them in-place and saves state.
        """
        changed = False
        clean = []
        for item in self.state.pending_trace():
            role = item.get("role")
            content = item.get("content")
            if role == "agent" and isinstance(content, str) and backend.is_event_blob(content):
                changed = True
                continue
            clean.append(item)
        if changed:
            self.state.data["pending_trace"] = clean
            self.state.save()
            self._debug("removed raw backend event blob from pending trace")

    def recover_nonempty_session(self, backend, repo, stage_backend_resume_fn):
        """Find the most recent non-empty conversation for this worktree.

        Called when the recorded session turns out empty.  Returns
        ``(session_id, ExportedSession)`` or ``None``.
        """
        try:
            candidate = backend.latest_session_id(repo.repo)
        except Exception as error:
            self._debug(f"recover non-empty session failed: {error!r}")
            return None
        if not candidate or candidate == self.state.backend_session_id:
            return None
        stage_backend_resume_fn(candidate)
        session = backend.export_session(repo.repo, candidate)
        if session and session.turns:
            return candidate, session
        return None

    def initialize_session_baseline(
        self,
        backend,
        repo,
        *,
        should_continue_fn: Callable[[], bool],
        stage_backend_resume_fn: Callable[[str | None], None],
        debug_fn: _DebugFn | None = None,
    ) -> None:
        """Compute the resume baseline for a newly-spawned session.

        Mirrors ``ProxyRunner._initialize_session_baseline``.  The caller's
        ``stage_backend_resume_fn`` is invoked to copy the transcript into the
        right directory before export.
        """
        _dbg = debug_fn or self._debug
        if not should_continue_fn():
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            return
        stage_backend_resume_fn(self.state.backend_session_id)
        session = backend.export_session(repo.repo, self.state.backend_session_id)
        if not session or not session.turns:
            recovered = self.recover_nonempty_session(backend, repo, stage_backend_resume_fn)
            session = recovered[1] if recovered else None
            if recovered:
                _dbg(f"recorded session empty; recovered non-empty {recovered[0]}")
                self.state.backend_session_id = recovered[0]
            else:
                self.state.backend_session_id = None
                self.state.last_backend_message_id = None
                return
        if session.model:
            self.state.model = session.model
        complete = [t for t in session.turns if t.assistant_message_id]
        self.state.last_backend_message_id = complete[-1].assistant_message_id if complete else None
        self.state.clear_trace()
