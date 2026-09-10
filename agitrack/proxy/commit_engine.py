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

  - **actions / shell** (``AgitrackActions.create_agent_commit_from_turns``):
      ``stage_untracked_fn`` = ``actions.review_untracked(...)``
      (no ``pre_commit_fn``, no ``on_commit_fn``) — caller prints its own
      confirmation.

* :meth:`CommitEngine.finish_parse_if_ready` — awaited-followup logic and
  turn-completeness gate, extracted from
  ``ProxyRunner._finish_agent_parse_if_ready``.

* :meth:`CommitEngine.start_parse` — parse-worker launcher extracted from
  ``ProxyRunner._start_agent_parse``.  Results are always written back to the
  *owning* :class:`~agitrack.proxy.session.Session` (issue #15 fix, preserved
  verbatim).

* :meth:`CommitEngine.record_user_prompt` /
  :meth:`CommitEngine.await_followup` — thin state helpers.

* :meth:`CommitEngine.sanitize_state_trace` /
  :meth:`CommitEngine.initialize_session_baseline` /
  :meth:`CommitEngine.recover_nonempty_session` — session-startup helpers.

Design constraints
------------------
Every method that operates on git/state takes the *session bundle*
``(repo, state)`` (or a full :class:`~agitrack.proxy.session.Session`) explicitly
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

import re
import threading
import time
from pathlib import Path
from typing import Callable

from agitrack import tracking_gap
from agitrack.commits import build_agent_commit_message, render_interaction_trace
from agitrack.commits.message import TRACE_EVENT_ROLE
from agitrack.git import GitRepo
from agitrack.transcripts.opencode import SessionTurn
from agitrack.transcripts import capabilities
from agitrack.transcripts import turns_after
from agitrack.config import AgitrackState


# ---------------------------------------------------------------------------
# Type aliases used in method signatures
# ---------------------------------------------------------------------------

_StageUntrackedFn = Callable[[GitRepo, AgitrackState], None]
"""Called to review/stage untracked files; receives (repo, state)."""

_PreCommitFn = Callable[[], None]
"""Called immediately before the ``git commit`` (e.g. ``_ensure_turn_branch``)."""

_OnCommitFn = Callable[[str | None, str, bool], None]
"""Called after a successful commit with: the short commit SHA (may be None), the
rendered interaction trace, and whether the commit is a *cover* (a merge-shaped
commit aGiTrack placed on top of the backend agent's own commits, #35)."""

_DebugFn = Callable[..., None]
"""Logging sink — ``runner._debug``."""


def _norm(text: str | None) -> str:
    """Whitespace-normalized form used to match recorded prompts against
    transcript turns (the transcript normalizes the user's raw typing)."""
    return " ".join((text or "").split())


def _is_slash_command(text: str | None) -> bool:
    """True when a typed prompt is purely a backend/TUI slash command
    (``/compact``, ``/comp``, ``/model``, ``/clear`` …).

    These are directives to the backend, not conversational prompts: the
    transcript parser already excludes them and :meth:`await_followup` skips
    them, so they must not be recorded into the pending trace either — otherwise
    they surface in the commit's interaction trace as a stray ``## User`` /
    ``/comp`` entry. A ``/compact`` in particular is redundant there: the trace's
    compaction lead-in note already records that the context was compacted."""
    return _norm(text).startswith("/")


_TOKEN_RE = re.compile(r"[a-z0-9]+")


# A monitor-tick acknowledgment is TRIVIAL when the agent barely responded: a short
# reply and little output. A turn that did real work off a monitor event (tool calls,
# file edits, a substantive final report) blows past both bounds and must commit
# immediately, monitor-triggered or not. (The tick acks that flooded a real repo were
# ~14 output tokens / ~26 characters; real reports run far larger on both axes.)
_TRIVIAL_UPDATE_MAX_OUTPUT_TOKENS = 100
_TRIVIAL_UPDATE_MAX_RESPONSE_CHARS = 240


def _only_trivial_monitor_updates(turns) -> bool:
    """Whether every one of these completed turns is a TRIVIAL intermediate monitor tick:
    triggered purely by the ``(background monitor update)`` synthetic prompt (no real user
    text anywhere) AND barely answered. Only such batches does finish_parse_if_ready defer
    rather than committing one history entry per tick. Any real prompt (typed or queued),
    a terminal ``(background task completed)`` turn, or a monitor turn with a substantive
    response (a normal final message, real work) makes the batch commit-worthy."""
    from agitrack.transcripts.claude import MONITOR_UPDATE_LABEL

    for turn in turns:
        prompts = [turn.user_prompt or "", *getattr(turn, "queued_followups", [])]
        texts = [p.strip() for p in prompts if p and p.strip()]
        if not texts or any(text != MONITOR_UPDATE_LABEL for text in texts):
            return False
        output_tokens = int(getattr(getattr(turn, "tokens", None), "output", 0) or 0)
        if output_tokens > _TRIVIAL_UPDATE_MAX_OUTPUT_TOKENS:
            return False
        if len(turn.final_response or "") > _TRIVIAL_UPDATE_MAX_RESPONSE_CHARS:
            return False
    return True


def _same_prompt(a: str, b: str) -> bool:
    """True when two recordings are the same user prompt.

    The prompt recorded at submit reconstructs the raw typed bytes, so line
    editing while typing (cursor moves, deletions) garbles it relative to the
    transcript's clean version — same words, joined or reordered differently.
    Equality (even whitespace-normalized) misses those, leaving the garbled
    copy to be re-added to the trace as if it were a separate prompt. Near-
    duplicates are therefore detected by word overlap: editing artifacts only
    shuffle words around, while genuinely different prompts share few.
    """
    na, nb = _norm(a).lower(), _norm(b).lower()
    if na == nb:
        return True
    tokens_a, tokens_b = set(_TOKEN_RE.findall(na)), set(_TOKEN_RE.findall(nb))
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return overlap >= 0.6


# Answers to a BACKEND'S OWN dialog, which the backend records as a user message.
#
# Claude and Codex both ask a trust question ("Do you trust the files in this folder?") whose
# answer is a single keystroke, and that keystroke lands in the transcript as a turn whose user
# prompt is `1`. aGiTrack then copied it into the commit's interaction trace, so every
# --no-worktree commit carried a stray `## User` / `1` — permanently, in history, seen across
# three independent live scenarios. The dialog also overlaps aGiTrack's own startup popup, so
# whatever is typed in that window is committed forever.
#
# Deliberately narrow: ONE character, from the set a menu answer can be, and only when the turn
# produced no agent reply and no edits. A real prompt that short does not exist, and a turn that
# the agent actually answered is never discarded whatever it says.
_DIALOG_KEYSTROKES = frozenset("0123456789yYnN")


def _is_background_event(prompt: str | None) -> bool:
    """Whether *prompt* is the harness's synthetic label for a turn the agent ran off a
    BACKGROUND EVENT — a task it had started reporting back — rather than off anything the
    user said (``BACKGROUND_PROMPT_LABELS`` in transcripts/claude.py).

    Such a turn has no author. Recorded as a ``user`` trace entry it became a ``## User`` /
    ``(background task completed)`` block that reads exactly like something the user typed,
    and it reached the commit subject and the dashboard's per-commit prompt the same way.
    It is instead recorded under ``TRACE_EVENT_ROLE`` and rendered as an event note."""
    from agitrack.transcripts.claude import BACKGROUND_PROMPT_LABELS

    return (prompt or "").strip() in BACKGROUND_PROMPT_LABELS


def _is_dialog_keystroke(turn) -> bool:
    """Whether *turn* is a backend dialog answer rather than something the user asked for."""
    prompt = (getattr(turn, "user_prompt", "") or "").strip()
    if len(prompt) != 1 or prompt not in _DIALOG_KEYSTROKES:
        return False
    if (getattr(turn, "final_response", "") or "").strip():
        return False
    if getattr(turn, "agent_messages", None) or getattr(turn, "edits", None):
        return False
    return True


def turn_is_finished(turn) -> bool:
    """Whether *turn* can still receive more messages from the backend.

    ``complete`` alone is not that question. A turn the user INTERRUPTED is over — the backend
    stopped, and nothing will ever be added to it — but the transcripts do not agree on how to
    say so: Claude resolves an interrupt to ``complete=True`` (``_finalize_turn``: "a turn the
    user interrupted can never receive more messages, so treating it as in-progress would stall
    the commit loop forever"), while Codex closes the aborted turn with ``complete=False``
    (``turn_aborted``/``task_aborted``/``turn_interrupted`` in ``transcripts/codex.py``).

    Reading ``complete`` on its own therefore made Esc behave differently per backend: on Codex
    the "latest turn still in progress" gate below deferred the commit of a turn that would never
    finish, so the agent's partial edits were never committed and the interactive
    keep/commit/discard offer — which lives past that gate — was never even reached. The work sat
    in the tree until the exit finalize (``require_complete=False``) swept it up, or another turn
    happened to carry it. Interruption is terminal on every backend, so it is treated as terminal
    here."""
    return bool(getattr(turn, "complete", True)) or bool(getattr(turn, "interrupted", False))


def _prompt_covered_by(pending: str, prompt: str) -> bool:
    """True when *pending* is already represented WITHIN *prompt*. A single turn's ``user_prompt``
    can aggregate several messages — a base prompt plus the follow-ups the user QUEUED while the
    agent worked (see ``transcripts/claude._queued_human_prompt``) — so the separately-recorded
    base prompt is a small fraction of the combined text and ``_same_prompt``'s symmetric overlap
    misses it, re-adding it to the trace as a duplicate. Detect containment instead: (almost) all of
    *pending*'s words appear in *prompt*. Requires ≥3 words so a short leftover can't coincidentally
    match an unrelated turn."""
    pt = set(_TOKEN_RE.findall(_norm(pending).lower()))
    tt = set(_TOKEN_RE.findall(_norm(prompt).lower()))
    if len(pt) < 3 or not tt:
        return False
    return len(pt & tt) / len(pt) >= 0.9


def _garbled_short_duplicate(pending: str, prompt: str) -> bool:
    """True when a SHORT *pending* leftover looks like a garbled capture of *prompt* already in the
    trace. The proxy reconstructs the raw typed bytes, so a follow-up typed while the agent was busy
    can be captured with a dropped leading/trailing character (e.g. "ontinue" for "Continue"). Such a
    one-word near-copy is too short for the word-overlap / containment checks above (which need ≥3
    words), so it would leak in as a spurious extra ## User heading. For short prompts (≤2 words),
    treat them as the same when one's normalized text is a substring of the other's AND covers most
    of it (≥0.7) — which catches a dropped edge character without matching unrelated short words."""
    a, b = _norm(pending).lower(), _norm(prompt).lower()
    if not a or not b or len(_TOKEN_RE.findall(a)) > 2 or len(_TOKEN_RE.findall(b)) > 2:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 3 and short in long and len(short) / len(long) >= 0.7


class CommitEngine:
    """Stateless agent-commit engine bound to a ``(repo, state)`` pair.

    The instance is cheap to construct and short-lived: the runner (or
    ``AgitrackActions``) creates one per commit call.  Nothing is cached on the
    instance between calls.
    """

    def __init__(
        self,
        repo: GitRepo,
        state: AgitrackState,
        *,
        debug_fn: _DebugFn | None = None,
        full_agent_messages: bool | None = None,
    ) -> None:
        self.repo = repo
        self.state = state
        self._debug = debug_fn or (lambda *a, **kw: None)
        # Per-run override for the "include all agent messages" behaviour (e.g. the
        # --full-agent-messages CLI flag). None defers to the per-repo config
        # (``state.full_agent_messages``); True/False forces it for this run only,
        # without touching the persisted config.
        self._full_agent_messages = full_agent_messages

    # ------------------------------------------------------------------
    # The untracked stretch (`agitrack stop` → next start)
    # ------------------------------------------------------------------

    def _tracking_floor(self) -> float | None:
        """When tracking last resumed after a stop, or None if it never was stopped.

        Read from the repo rather than carried on the engine because the engine is constructed
        fresh per commit call and the record is written by a DIFFERENT process (the `agitrack
        stop` the user typed). Defensive to the last: an engine built over a repo stand-in with
        no path, or a record that will not parse, simply has no floor and changes nothing."""
        root = getattr(self.repo, "repo", None)
        if root is None:
            return None
        try:
            return tracking_gap.tracking_floor(Path(root))
        except Exception as error:
            self._debug(f"tracking floor lookup failed: {error!r}")
            return None

    def _skip_untracked_turns(self, turns: list[SessionTurn]) -> None:
        """Advance this conversation's watermark past turns from the untracked stretch, so they
        are not re-exported and re-dropped on every poll for the rest of the conversation's life.

        Anchoring is deliberately blunt compared with the commit path's: these turns will never
        be recorded, so there is no force-capture to continue and nothing to count. Whichever id
        the turn carries is enough to move past it, and ``marked_at`` keeps the time fallback
        honest if a later compaction reshapes the boundary the id names."""
        last = turns[-1]
        mark = last.assistant_message_id or last.user_message_id
        if not mark:
            return
        self.state.set_backend_message_id(
            self.state.backend_session_id,
            mark,
            marked_at=(last.ended_at or last.started_at or time.time()),
        )

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
        backend_commits: list[str] | None = None,
        manual_gate_fn: Callable[[], bool] | None = None,
        manual_record_fn: Callable[[str], str | None] | None = None,
        changed_paths_fn: Callable[[], list[str]] | None = None,
        summarize_fn: Callable[[str], tuple[str, list[str]] | None] | None = None,
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
        backend_commits:
            Unintegrated commits the backend made itself this turn (full SHAs,
            oldest first), issue #35.  With nothing staged, a merge-shaped
            *cover commit* carrying the trace/metadata is added on top of them
            (their hashes never change — an amend would break references the
            agent already published, #58); with staged changes, the normal
            commit lists them in its ``covered_commits`` metadata.

        Commits are created immediately, without any LLM call: summarization
        runs in the background afterwards and is attached by amending the
        commit message (issue #8) — blocking the commit on a summary froze the
        UI and delayed integration past the next turn.

        Returns ``True`` if a commit was made, ``False`` otherwise.
        """
        # The last gate before turns become commit-message text, so the "nothing from a stopped
        # stretch, ever" rule is enforced where it is actually spent rather than only where the
        # live parse selects turns. Costs one small file read per commit and normally drops
        # nothing: the floor is unset until a `agitrack stop` has actually happened.
        _untracked, turns = tracking_gap.split_untracked_turns(turns, self._tracking_floor())
        if _untracked:
            self._debug(f"dropping {len(_untracked)} turn(s) prompted while aGiTrack was stopped")
        if not turns:
            return False
        backend_commits = list(backend_commits or [])

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
            cover_backend_head = False
            cover_with_staged = False
            if not self.repo.has_staged_changes():
                if not self._head_is_coverable(backend_commits):
                    return False
                cover_backend_head = True
            elif self._head_is_coverable(backend_commits):
                # Staged changes on top of coverable backend commits: cover them
                # together so the covered changes aren't hidden behind a plain
                # commit's single parent (#35).
                cover_with_staged = True
            # Commit (or cover) will happen: accumulate trace and tokens now.
            for turn in turns:
                if turn.user_prompt and not _is_dialog_keystroke(turn):
                    # A background wake-up is an EVENT, not something the user said.
                    role = TRACE_EVENT_ROLE if _is_background_event(turn.user_prompt) else "user"
                    self.state.append_trace(role, turn.user_prompt)
                # Each message the user queued mid-turn gets its OWN ## User heading (it was sent
                # after the agent had already said something), not merged into the base prompt.
                for followup in turn.queued_followups:
                    if followup.strip():
                        # Its own heading, but NOT a new turn — see append_trace(starts_turn).
                        self.state.append_trace("user", followup, starts_turn=False)
                for message in self._agent_messages_for(turn):
                    self.state.append_trace("agent", message)
                self._add_turn_usage(turn)
            # The subject describes what was ASKED FOR, so a synthetic wake-up label is not a
            # candidate: a commit whose turns were all background-driven falls back to
            # "<backend> changes" rather than claiming the user asked for "(background task
            # completed)".
            prompts = [
                p
                for turn in turns
                for p in ([turn.user_prompt, *turn.queued_followups])
                if p and not _is_background_event(p)
            ]
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
            self.state.data["pending_trace"] = []
            self.state.save()

            subject_prompts: list[str] = []
            entries: list[tuple[str, str, bool]] = []
            for turn in turns:
                if turn.user_prompt and not _is_dialog_keystroke(turn):
                    if _is_background_event(turn.user_prompt):
                        # An EVENT woke the agent; nobody asked for anything. It belongs in the
                        # trace (it explains a turn with no prompt) but never under the user's
                        # name, and never as the commit's subject.
                        entries.append((TRACE_EVENT_ROLE, turn.user_prompt, True))
                    else:
                        subject_prompts.append(turn.user_prompt)
                        entries.append(("user", turn.user_prompt, True))
                # A mid-turn queued message gets its own ## User heading (sent after the agent
                # already responded), rather than being merged into the base prompt.
                for followup in turn.queued_followups:
                    if not followup.strip():
                        continue
                    subject_prompts.append(followup)
                    entries.append(("user", followup, False))
                for message in self._agent_messages_for(turn):
                    entries.append(("agent", message, True))

            # Pending user entries that never showed up as a turn's user_prompt
            # (e.g. a follow-up note typed mid-turn) are still added to the
            # subject and trace so they appear in the commit body. Matching uses
            # word overlap (_same_prompt), not equality: the recording keeps the
            # user's raw typing, which line editing garbles relative to the
            # transcript's clean version — equality re-added the same prompt as
            # if it were new (issue #8). Duplicate recordings also collapse.
            turn_prompts = [p for t in turns for p in ([t.user_prompt, *t.queued_followups]) if p]
            # A message the user QUEUES mid-turn and the backend actually receives is recorded by
            # the transcript (Claude writes it as an attachment row → `queued_followups`). So when
            # these turns carry queued follow-ups, this backend is DEMONSTRABLY recording them, and
            # a submit-time capture matching none of them never reached the agent: a message the
            # user typed, queued, and then DELETED before it was sent. The user retracted it, so it
            # must not be attributed to them in the trace — a stray prompt meant for another window
            # once landed in a commit that way. (Without that evidence we keep the old behavior and
            # carry the capture through, so a prompt from an earlier incomplete parse is never lost.)
            transcript_records_queued = any(turn.queued_followups for turn in turns)
            leftovers: list[str] = []
            for pending_user in pending_users:
                if not _norm(pending_user):
                    continue
                if _is_slash_command(pending_user):
                    continue  # a backend directive (e.g. /compact) recorded earlier — not a prompt
                # Skip when the turn already carries this prompt — either as a near-duplicate
                # (_same_prompt) OR because the turn aggregated it as a queued follow-up so the
                # base is CONTAINED in the (longer) combined turn prompt (_prompt_covered_by).
                if any(
                    _same_prompt(pending_user, prompt)
                    or _prompt_covered_by(pending_user, prompt)
                    or _garbled_short_duplicate(pending_user, prompt)
                    for prompt in turn_prompts
                ):
                    continue
                if transcript_records_queued:
                    continue  # never delivered (see above) — the user deleted it before it was sent
                if any(_same_prompt(pending_user, prompt) for prompt in leftovers):
                    continue
                leftovers.append(pending_user)
            subject_prompts.extend(leftovers)

            # Leftovers were typed while the LAST turn was running, so they belong with
            # that turn's prompt — right after it and BEFORE the turn's agent response(s).
            # A turn is laid out as [user_prompt, agent_msg, agent_msg, …], so insert the
            # leftovers immediately after the last user prompt. The previous logic inserted
            # before the last AGENT message, which — when a turn emitted several agent
            # messages — dropped the leftovers in between (or after) the agent's replies,
            # so a message the user sent mid-turn read as if it came after the agent's
            # final response (issue #8; the agent's answer actually covers all of them).
            # The anchor is whatever STARTED the last turn — a user prompt, or the background
            # event that woke the agent with nobody saying anything. Anchoring on "user" alone
            # dropped a leftover typed during a background-driven turn past all of that turn's
            # agent replies, reading as if it arrived after the agent's final answer.
            insert_at = len(entries)
            for index in range(len(entries) - 1, -1, -1):
                if entries[index][0] in ("user", TRACE_EVENT_ROLE) and entries[index][2]:
                    insert_at = index + 1
                    break
            # Leftovers were typed while that turn was running, so they continue it too.
            entries[insert_at:insert_at] = [("user", leftover, False) for leftover in leftovers]
            for role, content, starts_turn in entries:
                self.state.append_trace(role, content, starts_turn=starts_turn)

            cover_backend_head = False
            cover_with_staged = False
            if manual_record_fn is not None:
                # Manual-commit mode: never stage into the user's index, run pre_commit_fn
                # (no turn branch), or move HEAD. The turn is recorded as a hidden latent
                # commit by manual_record_fn instead. Gate on whether the working tree
                # actually changed since the latent tip, so a no-op turn records nothing —
                # and, per bug #14, tokens are still accumulated only once past this gate.
                if manual_gate_fn is not None and not manual_gate_fn():
                    return False
            else:
                # Hook: proxy mode puts the session on a fresh turn branch here.
                if pre_commit_fn is not None:
                    pre_commit_fn()

                self.repo.add_tracked()
                stage_untracked_fn(self.repo, self.state)

                if not self.repo.has_staged_changes():
                    if not self._head_is_coverable(backend_commits):
                        return False
                    cover_backend_head = True
                elif self._head_is_coverable(backend_commits):
                    # Staged changes on top of coverable backend commits: cover them
                    # together so the covered changes aren't hidden behind a plain
                    # commit's single parent (#35).
                    cover_with_staged = True

            # Accumulate tokens only once we know the commit (or cover) will happen.
            for turn in turns:
                self._add_turn_usage(turn)

            subject_text = " / ".join(subject_prompts) if subject_prompts else f"{backend} changes"

        # The metadata lists covered hashes in short form (the full SHAs stay
        # internal — _head_is_coverable compares them against rev-parse HEAD).
        # An aGiTrack commit accounts for itself; it lists only the backend-made
        # commits it additionally covers (#35).
        covered_display = [self._short_sha(sha) for sha in backend_commits]
        # The AI-driven conversation's span: earliest prompt to latest response
        # across the turns this commit accounts for (None when the backend's
        # transcript carries no timestamps).
        starts = [turn.started_at for turn in turns if turn.started_at]
        ends = [turn.ended_at for turn in turns if turn.ended_at]
        # Context compactions across the turns this commit accounts for, and a one-shot
        # fork/copy origin event for this session — both recorded because they reshape
        # the context the token counts run against (issue: track compaction & fork/copy).
        compactions = sum(int(getattr(turn, "compaction_count", 0) or 0) for turn in turns)
        # Reasoning effort / thinking level for this commit's turns: the most recent
        # turn that recorded one wins, so the metadata reflects the level in effect
        # at the end of the span (None when no turn revealed it).
        # The harness version these turns ran under. Newest-first like the model and effort:
        # a CLI that updated itself mid-span produced the later turns, and that is the version
        # the commit describes.
        backend_version = next(
            (turn.backend_version for turn in reversed(turns) if getattr(turn, "backend_version", None)),
            None,
        )
        reasoning_effort = next(
            (turn.reasoning_effort for turn in reversed(turns) if getattr(turn, "reasoning_effort", None)),
            None,
        )
        # Anchor into the backend conversation: the message id of the last turn this
        # commit covers, so the commit links back to the exact place in the (locally
        # kept or shared) transcript. The last turn with a recorded assistant message
        # id wins; None when the backend exposes none.
        conversation_anchor = next(
            (turn.assistant_message_id for turn in reversed(turns) if getattr(turn, "assistant_message_id", None)),
            None,
        )
        # Capabilities beyond the backend's built-ins that these turns used — MCP servers/tools,
        # skills, sub-agents. Unioned across the span (each is a set of things that were in play,
        # not a "latest wins" setting), in first-seen order so identical spans render identically.
        capability_metadata = capabilities.merge_turns(turns)
        origin_event = self.state.session_origin_event()
        # Render the interaction trace once — it is the summarizer's sole input and what
        # on_commit_fn hands off below. When a summarize_fn is supplied (the background daemon's
        # cover path, which can't amend HEAD to attach a summary afterwards), summarize it
        # SYNCHRONOUSLY here so the commit message LEADS with the summary, just like every other
        # aGiTrack commit. Other callers (proxy, shell) pass None and keep the async amend flow.
        # Did the user cancel (Esc) the turn this commit ENDS on? A turn that was interrupted
        # after the agent had said what it was ABOUT to do still commits (its partial edits are
        # real), but the trace alone reads like a finished turn — and the trace is all the
        # summarizer gets, so it described the whole request as done. Both the message and the
        # summarizer input carry the fact.
        #
        # THE LAST TURN, NOT ANY TURN. A commit routinely covers several turns, and `any` made
        # one Esc anywhere in that span stamp "(interrupted)" on a commit whose closing turn ran
        # to completion — `git log --oneline` then said the work was cut short when it was
        # finished and pushed. (Live example: a span of four turns where the user had stopped a
        # single tool call early on.) What the mark has to answer is whether the work this
        # commit DELIVERS was cut short, and that is decided by the turn it ends on; an earlier
        # interrupted turn is still visible as itself in the interaction trace.
        interrupted = bool(turns) and bool(getattr(turns[-1], "interrupted", False))
        # What the commit actually carries, read off the index that is about to become it.
        # Only for an interrupted turn: that is the one case where the trace demonstrably
        # cannot account for the diff (see message._interrupted_changes_sentence), and every
        # other commit keeps the trace as the summarizer's sole input, by design.
        changed_paths = self._changed_paths(changed_paths_fn) if interrupted else None
        trace_text = render_interaction_trace(
            self.state.pending_trace(),
            self.state.trace_turn_limit,
            interrupted=interrupted,
            changed_paths=changed_paths,
            # The summarizer's SOLE input, so it is redacted before the LLM ever sees it: a
            # summary written from text that still named another project would put the name back
            # into the subject line in the summarizer's own words, where no pattern can find it.
            repo_root=getattr(self.repo, "repo", None),
        )
        summary_text: str | None = None
        summary_metadata: list[str] | None = None
        if summarize_fn is not None:
            try:
                summarized = summarize_fn(trace_text)
            except Exception as error:
                self._debug(f"synchronous summary failed: {error!r}")
                summarized = None
            if summarized:
                summary_text, summary_metadata = summarized
        message = build_agent_commit_message(
            latest_prompt=subject_text,
            trace=self.state.pending_trace(),
            backend=backend,
            backend_session_id=backend_session_id,
            agitrack_session_id=self.state.session_id,
            model=model or self.state.model,
            backend_version=backend_version,
            reasoning_effort=reasoning_effort,
            conversation_anchor=conversation_anchor,
            token_usage=self.state.pending_token_usage(),
            trace_turn_limit=self.state.trace_turn_limit,
            session_name=session_name,
            summary=summary_text,
            summary_metadata=summary_metadata,
            covered_commits=covered_display or None,
            started_at=min(starts) if starts else None,
            ended_at=max(ends) if ends else None,
            compactions=compactions,
            origin_event=origin_event,
            capabilities=capability_metadata,
            interrupted=interrupted,
            changed_paths=changed_paths,
            # Which repository this is, so the names of the user's OTHER projects can be taken
            # out of the prompt, the reply and the summary. In worktree mode this is the session
            # worktree rather than the base repo, which is the same answer for this purpose: the
            # base is one of its parents, and `foreign` treats the whole line as "not another
            # project" (agitrack/commits/foreign.py).
            repo_root=getattr(self.repo, "repo", None),
        )
        if manual_record_fn is not None:
            # Manual-commit mode: record the turn as a hidden latent commit on the side
            # ref (snapshot the working tree, commit-tree onto the latent tip, move only
            # that ref). HEAD and the user's index are untouched — the user's own commit
            # later folds these in. Returns the latent sha (or None if, defensively, the
            # tree turned out unchanged after the gate).
            commit_sha = manual_record_fn(message)
            if commit_sha is None:
                return False
        elif cover_backend_head or cover_with_staged:
            # The backend committed its own work (#35). Its commits keep their
            # hashes — amending them broke references the agent had already
            # published in PRs/issues (#58). Instead the trace/metadata ride a
            # GitHub-PR-style merge-shaped cover commit on top, parents (turn
            # start, backend head), so `git log --first-parent` reads turn-by-turn
            # and the cover's diff shows every covered change. When aGiTrack also has
            # extra staged changes on top (e.g. it staged the agent's new files),
            # `include_staged` folds them into the cover's tree so they're tracked
            # alongside the covered commits rather than hidden behind a plain
            # commit's single parent.
            commit_sha = self.repo.cover_commit(
                message,
                first_parent=f"{backend_commits[0]}^",
                second_parent=backend_commits[-1],
                include_staged=cover_with_staged,
            )
        else:
            commit_sha = self.repo.commit(message)
        # The fork/copy origin event is a one-shot: it has now been surfaced in this
        # commit, so clear it (a no-op when there was none) — later commits in the same
        # session shouldn't keep re-announcing the lineage.
        if origin_event is not None:
            self.state.clear_session_origin_event()
        # ``trace_text`` was rendered above (before the message build) so a synchronous
        # summarize_fn could see it; it is the summarizer's sole input and what on_commit_fn
        # hands off. Clear the pending trace now that the commit is recorded.
        self.state.clear_trace()

        if on_commit_fn is not None:
            on_commit_fn(commit_sha, trace_text, cover_backend_head or cover_with_staged)

        return True

    def _agent_messages_for(self, turn: SessionTurn) -> list[str]:
        """The agent's user-facing message(s) to record in the trace for *turn*.

        By default just the final response — the substantive reply, and the long-
        standing behaviour. When the ``full_agent_messages`` option is on, every
        user-facing message the agent sent during the turn, in order (each becomes
        its own ``## Agent`` block); tool calls, tool results, and file edits are
        never included either way. Falls back to the final response when the backend
        didn't recover the full list, and is empty when the turn has no agent text.
        """
        full = self._full_agent_messages
        if full is None:
            full = self.state.full_agent_messages
        if full and turn.agent_messages:
            return [message for message in turn.agent_messages if message]
        return [turn.final_response] if turn.final_response else []

    def _changed_paths(self, changed_paths_fn: Callable[[], list[str]] | None) -> list[str]:
        """The paths the pending commit will carry.

        Two sources, because the two commit paths differ. The classic path stages into the
        index and commits it, so the index is the answer — read AFTER staging, so it is the
        commit's own content and not a guess. Every ``--no-worktree`` mode instead records a
        hidden LATENT commit from a working-tree snapshot and never touches the index at all,
        which is why reading only the index came back empty for the mode most people run (and
        left the interrupted note without its file list where it was needed most); those
        callers pass their own ``changed_paths_fn``.

        Never fatal: any failure leaves the note as it was before, without a file list."""
        try:
            return changed_paths_fn() if changed_paths_fn is not None else self.repo.staged_paths()
        except Exception as error:
            self._debug(f"changed path listing failed: {error!r}")
            return []

    def _short_sha(self, sha: str) -> str:
        """Short display form of *sha* (falls back to a 7-char prefix when the
        repo cannot resolve it, e.g. fake repos in tests)."""
        try:
            return self.repo.short_sha(sha)
        except Exception:
            return sha[:7]

    def _head_is_coverable(self, backend_commits: list[str]) -> bool:
        """True when HEAD is the latest of the backend's own unintegrated
        commits, so a cover commit on top attaches the trace to the commits
        that actually made the change (#35). Never true for commits aGiTrack
        created (they carry their own metadata) or for anything already
        integrated into base (``backend_commits`` only ever lists commits
        ahead of base)."""
        if not backend_commits:
            return False
        try:
            return self.repo.rev_parse("HEAD") == backend_commits[-1]
        except Exception as error:
            self._debug(f"cover check failed: {error!r}")
            return False

    # ------------------------------------------------------------------
    # Parse-result consumption (extracted from _finish_agent_parse_if_ready)
    # ------------------------------------------------------------------

    def finish_parse_if_ready(
        self,
        *,
        session,  # agitrack.proxy.session.Session
        quiet: bool,
        prompt_untracked: bool,
        require_complete: bool,
        awaited_followups: list[str],
        agent_is_active_fn: Callable[[], bool],
        debug_fn: _DebugFn,
        note_session_change_fn: Callable[[str], None],
        mirror_fn: Callable[[str | None], None],
        commit_fn: Callable,
        on_cancelled_fn: Callable[[list[SessionTurn]], bool] | None = None,
        note_in_flight_fn: Callable[[dict | None], None] | None = None,
    ) -> tuple[bool | None, list[str]]:
        """Consume a ready parse result and (conditionally) commit.

        Extracted from ``ProxyRunner._finish_agent_parse_if_ready``.  The
        caller retains ownership of the ``awaited_followups`` list; the
        updated list is returned as the second element of the tuple.

        ``commit_fn`` is called with ``(turns=..., backend=...,
        backend_session_id=..., model=..., quiet=..., prompt_untracked=...)``
        keyword arguments when a commit should happen.  The runner passes
        ``self._create_agent_commit_from_turns_popup`` so test mocks stay
        effective; ``AgitrackActions`` passes its own pipeline wrapper.

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
            # Deliberately does NOT move the pin. An empty transcript is not evidence of a
            # switch: Claude mints a fresh EMPTY session id on every resume/picker action, and
            # adopting one drops the user into a blank session on the next start (the same trap
            # `claude.latest_session_id` and `_sync_tracked_session` avoid by preferring a ref
            # with a label). A real switch is picked up by `_service_native_session_switch` once
            # the conversation has content, which is also when its name can be asked for.
            debug_fn(f"agent parse consumed without session session_id={session_id}")
            return False, awaited_followups

        new_session_id = exported_session.session_id or session_id
        note_session_change_fn(new_session_id)
        self.state.backend_session_id = new_session_id
        mirror_fn(new_session_id)

        if exported_session.model:
            self.state.model = exported_session.model

        # Remember which of the agent's background work is still running so the pre-agent
        # user-commit dialog knows the tree may have a concurrent writer (see the runner's
        # _offer_pre_agent_user_commit) and the auto-fold keeps waiting for the tree to settle.
        # Backgrounded tasks and async SUB-AGENTS both qualify: each keeps editing this tree
        # after the launching turn ended, which is exactly what makes ownership of an
        # uncommitted change unknowable. Best-effort: sessions without the field skip it.
        try:
            session.live_background_task_ids = [
                *(getattr(exported_session, "live_background_task_ids", []) or []),
                *(getattr(exported_session, "live_subagent_ids", []) or []),
            ]
        except AttributeError:
            pass

        all_turns = turns_after(
            exported_session,
            last_message_id,
            marked_at=self.state.backend_message_marked_at_for(self.state.backend_session_id),
        )

        # Turns the user had switched tracking OFF for (`agitrack stop`, resumed later) are not
        # ours to record. The watermark alone cannot express that: it is persistent by design, so
        # everything after it looks committable however long aGiTrack was down, and a conversation
        # STARTED during the gap has no watermark at all and would export whole. Dropped here,
        # before anything reads `all_turns` — the in-flight trailer, the trace, the token
        # accounting and the cover path all take their input from this list. See
        # agitrack.tracking_gap for what the floor does and does not cover.
        untracked_turns, all_turns = tracking_gap.split_untracked_turns(all_turns, self._tracking_floor())
        if untracked_turns:
            debug_fn(
                f"skipping {len(untracked_turns)} turn(s) prompted while aGiTrack was stopped "
                f"session_id={new_session_id}"
            )
            self._skip_untracked_turns(untracked_turns)

        # Tell the driver whether the agent is MID-TURN right now, so a commit the agent makes
        # ITSELF before its turn ends still gets attributed (see `build_in_flight_trailer`) —
        # otherwise the fold hook has no pending turn to fold and the commit lands with no
        # metadata at all. Computed here, the one place that already holds both the export and
        # the watermark, so every mode gets the same answer from the same evidence.
        if note_in_flight_fn is not None:
            running = all_turns[-1] if all_turns and not turn_is_finished(all_turns[-1]) else None
            facts = None
            if running is not None:
                try:
                    backend_name = self.state.backend
                except Exception:
                    backend_name = "unknown"  # no backend configured (tests / partial state)
                facts = {
                    "backend": backend_name,
                    "backend_session_id": new_session_id,
                    "model": exported_session.model or self.state.model,
                    "prompt": running.user_prompt,
                }
            note_in_flight_fn(facts)

        # Awaited-followup logic: a prompt queued while the agent was busy
        # belongs in the same commit as the turn it triggered.
        awaited = list(awaited_followups)
        if awaited and any(getattr(t, "interrupted", False) for t in all_turns):
            awaited = []
        if awaited:
            # Word-overlap matching, not equality: line editing garbles the
            # recorded prompt relative to the transcript's clean version, and
            # an unmatchable awaited entry would defer commits indefinitely.
            turn_prompts = [t.user_prompt or "" for t in exported_session.turns]
            awaited = [p for p in awaited if not any(_same_prompt(p, prompt) for prompt in turn_prompts)]
            if require_complete and awaited and agent_is_active_fn():
                debug_fn(f"deferring agent commit: {len(awaited)} queued follow-up(s) not yet in transcript")
                return None, awaited
            awaited = []  # committing now — drop cancelled queue entries

        if require_complete and all_turns and not turn_is_finished(all_turns[-1]):
            debug_fn(f"deferring agent commit: latest turn still in progress session_id={new_session_id}")
            return None, awaited

        # ASYNC SUB-AGENTS still working. The main agent can spawn sub-agents that outlive the
        # turn that launched them, then post what reads as a final answer while they are doing
        # the actual job. Committing there records a trace whose closing message says the work
        # is done over a tree the sub-agents are still writing to — and the summarizer, whose
        # only input is that trace, describes the request as finished. So the launching turn
        # WAITS: nothing is lost, the watermark stays put, and once the sub-agents report back
        # their work and the agent's follow-up land in the same commit. The exit/stop finalize
        # (require_complete=False) still captures everything, and the liveness signal itself
        # expires (see SUBAGENT_LIVE_HORIZON_SECONDS) so a sub-agent that died without
        # reporting cannot hold commits forever.
        live_subagents = list(getattr(exported_session, "live_subagent_ids", []) or [])
        if require_complete and all_turns and live_subagents:
            debug_fn(
                f"deferring agent commit: {len(live_subagents)} async sub-agent(s) still running "
                f"session_id={new_session_id}"
            )
            return None, awaited

        # ENFORCED: a commit's trace never ends with an unanswered user message. A
        # trailing turn with no final response (the user's next prompt caught by a
        # flush/force commit before the agent replied) is trimmed: its changes, if any,
        # still land in this commit's snapshot, but its trace and the watermark wait for
        # the reply, so it commits properly once answered — the same ordering interactive
        # auto mode gets by committing the prior turn before forwarding a new prompt.
        # Exception: a SOLE final-less turn under a force commit (require_complete=False,
        # the exit finalize) is kept, so exit still captures in-flight work; turns_after
        # re-exports it if the conversation later continues.
        while len(all_turns) > 1 and not all_turns[-1].final_response:
            dropped = all_turns.pop()
            debug_fn(
                f"trimming unanswered trailing turn from commit "
                f"user_id={dropped.user_message_id} session_id={new_session_id}"
            )

        complete_turns = [t for t in all_turns if t.final_response]
        # Intermediate background-monitor ticks: a Monitor streaming events from a
        # still-running job wakes the agent for each event, and every wake becomes its own
        # turn (synthetic prompt, usually a one-line acknowledgment plus whatever files the
        # monitored job just touched). Committing each tick floods the history — a real
        # overnight session produced 100+ such commits — so while the live loop is running
        # (``require_complete``) these turns are DEFERRED: the watermark stays put and they
        # fold into the next substantive commit (a real prompt, a terminal task
        # notification, or the exit finalize, which passes require_complete=False).
        if require_complete and complete_turns and _only_trivial_monitor_updates(complete_turns):
            debug_fn(
                f"deferring agent commit: {len(complete_turns)} trivial background monitor update turn(s) only "
                f"session_id={new_session_id}"
            )
            return None, awaited
        last_turn = all_turns[-1] if all_turns else None
        if not complete_turns:
            # A user-cancelled turn (Esc) that produced no committable response
            # would normally leave the agent's partial edits sitting uncommitted in
            # the worktree forever. When a cancellation handler is supplied (the
            # live interactive path), let it decide commit-vs-discard; if it acted,
            # advance the watermark past the cancelled turn so it isn't reconsidered.
            if (
                on_cancelled_fn is not None
                and last_turn is not None
                and getattr(last_turn, "interrupted", False)
                and on_cancelled_fn(all_turns)
            ):
                self.state.set_backend_message_id(
                    self.state.backend_session_id,
                    last_turn.assistant_message_id
                    or last_turn.user_message_id
                    or self.state.backend_message_id_for(self.state.backend_session_id),
                    # With the mark's TIME, like every other advance: `set_backend_message_id`
                    # only writes the time when it is given one, so omitting it left this
                    # session's previous (older) mark in place. turns_after falls back to that
                    # time whenever a compaction stops the id matching a turn boundary, and an
                    # older time there re-exports turns already committed.
                    marked_at=(last_turn.ended_at or last_turn.started_at or time.time()),
                )
                debug_fn(
                    f"agent parse consumed without final response (cancelled) "
                    f"session_id={self.state.backend_session_id} turns={len(all_turns)}"
                )
                return False, awaited
            if last_turn is None or getattr(last_turn, "interrupted", False):
                # Interrupted with no handler, or genuinely nothing to act on.
                debug_fn(
                    f"agent parse consumed without final response "
                    f"session_id={self.state.backend_session_id} turns={len(all_turns)}"
                )
                return False, awaited
            # A turn that FINISHED but emitted no final TEXT response — e.g. its last action was
            # a tool call / file edit and the agent stopped without a closing message. It may
            # still have changed files, and dropping it here would leave that work uncommitted
            # forever: the live loop keeps re-parsing and the exit finalize hits this same gate,
            # so neither ever commits it. Fall through to a normal commit attempt — commit_turns
            # gates on has_staged_changes and no-ops (returns False) if nothing is actually
            # staged, so a truly empty turn is still skipped, but real edits are captured.
            debug_fn(
                f"committing finished turn with no final text response "
                f"session_id={self.state.backend_session_id} turns={len(all_turns)}"
            )

        committed = commit_fn(
            turns=all_turns,
            backend=session.backend.name,
            backend_session_id=self.state.backend_session_id,
            model=exported_session.model or self.state.model,
            quiet=quiet,
            prompt_untracked=prompt_untracked,
        )
        if committed:
            # Advance the watermark for THIS conversation so the next parse cycle only
            # exports its new turns — keyed per conversation so a later switch back to a
            # different conversation reads its own mark, never this one's. Prefer the last turn
            # that actually carried a response; fall back to the last turn for a no-text
            # finished turn. Which of the turn's two ids anchors the mark is decided below.
            watermark = complete_turns[-1] if complete_turns else last_turn
            # A turn captured MID-FLIGHT is anchored on its USER id — never on whatever
            # assistant id it happens to carry at this instant. A backend mints a NEW message
            # id for each assistant response within a turn, so the id a force capture (the
            # exit/stop finalize, `require_complete=False`) reads is TRANSIENT: when the turn
            # continues, its `assistant_message_id` becomes a later one, the stored mark then
            # matches no turn boundary, and turns_after's marked_at fallback drops the turn
            # for having STARTED before the mark. What survives is then only the PARTIAL
            # snapshot the capture took: everything the turn did afterwards — its real final
            # response, its remaining tokens — is recorded nowhere, and its later edits reach
            # history only as unattributed diff in whatever commit next sweeps the tree. (The
            # prompt itself does survive, in that partial record; the watermark only advances
            # when the capture actually committed. Losing the prompt outright is the SEPARATE
            # inherited-watermark bug, see AgitrackState.backend_message_id_for.) The user id
            # is stable for the turn's whole life and is exactly what turns_after re-exports
            # INCLUSIVELY once the turn finishes.
            #
            # This used to key off `not watermark.assistant_message_id`, which caught only a
            # turn captured before it had said ANYTHING — every mid-flight turn that had
            # already spoken (the common case; a daemon restart mid-turn) fell through to the
            # transient id. Easy to hit: `uv run` reinstalls the editable package, the tracker
            # reads that as "aGiTrack updated on disk" and restarts. Reproduced by replaying a
            # real transcript truncated at 25/50/75% through a turn — the turn vanished from
            # every later commit each time (2026-08-15).
            # An INTERRUPTED turn is finished, not mid-flight: anchoring it on its user id would
            # re-export it inclusively on the next parse (see turns_after) and commit its trace a
            # second time. Only a turn that can still grow needs the user-id anchor.
            anchor_on_user = (
                watermark is not None
                and bool(watermark.user_message_id)
                and (not watermark.assistant_message_id or not turn_is_finished(watermark))
            )
            if watermark is None:
                mark_id = None
            elif anchor_on_user:
                mark_id = watermark.user_message_id
            else:
                mark_id = watermark.assistant_message_id or watermark.user_message_id
            if watermark is not None and mark_id:
                self.state.set_backend_message_id(
                    self.state.backend_session_id,
                    mark_id,
                    marked_at=(watermark.ended_at or watermark.started_at or time.time()),
                )
            if watermark is not None and anchor_on_user:
                # Force-captured mid-turn: the next parse re-exports this turn INCLUSIVELY
                # once it finishes. Remember the usage counted SO FAR (cumulative, so a
                # second force-capture of the same still-running turn updates it) and
                # _add_turn_usage adds only the delta on the re-commit.
                self.state.set_partial_turn_usage(
                    self.state.backend_session_id, watermark.user_message_id, watermark.tokens.to_dict()
                )
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
        session,  # agitrack.proxy.session.Session
        discover_session_id_fn: Callable[[], str | None],
        debug_fn: _DebugFn,
        on_finish: Callable[[], None] | None = None,
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
        on_finish:
            Called (from the worker thread) the moment the result is stored. The reactor
            passes its self-wake here: a submitted prompt is HELD while this worker runs,
            and without the wake it was released only on the next poll tick, so a parse
            that finished in 80 ms still sat behind "checking existing git changes..." for
            up to a full ACTIVE_POLL_SECONDS before reaching the backend.
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
                    # A worktree's directory is unique to this aGiTrack session, so
                    # the newest backend session there is always this session's
                    # current conversation — track it even if the user started a
                    # new conversation from inside the backend.
                    session_id = backend.latest_session_id(repo.repo) or state.backend_session_id
                else:
                    # No worktree isolation: track the session aGiTrack spawned, and follow an
                    # in-backend switch (Claude /resume or a new conversation started inside the
                    # backend) too, so ALL modes support session switching. discover_session_id_fn
                    # is snapshot-based: it only ever returns a session that appeared AFTER launch,
                    # never a pre-existing unrelated one, so preferring it can't grab the wrong
                    # session — and it returns None (⇒ fall back to the pinned id) when no switch
                    # has happened or no reliable snapshot exists. The per-conversation watermark
                    # (backend_message_id_for) keeps each conversation counted exactly once.
                    session_id = discover_session_id_fn() or state.backend_session_id
                # Read the watermark for the conversation actually being exported (not a
                # single global one), so switching between conversations never replays or
                # double-counts a conversation's already-committed turns.
                last_message_id = state.backend_message_id_for(session_id)
                exported = backend.export_session(repo.repo, session_id) if session_id else None
                turn_count = len(exported.turns) if exported else 0
                final_count = len([t for t in exported.turns if t.final_response]) if exported else 0
                debug_fn(
                    f"agent parse worker finished session_id={session_id} turns={turn_count} "
                    f"finals={final_count} watermark={last_message_id}"
                )
                result = (session_id, exported, last_message_id, state)
            except Exception as error:
                # Unhandled, this printed a traceback to STDERR - which, while the TUI owns
                # the screen, is painted straight over the user's session. The prompt this
                # parse is holding is released by the caller either way.
                debug_fn(f"agent parse worker failed: {error!r}")
            finally:
                with parse_lock:
                    owner.last_parse_finish = time.monotonic()
                    if result is not None:
                        owner.agent_parse_result = result
                    owner.agent_parse_active = False
                if on_finish is not None:
                    try:
                        on_finish()  # wake the reactor: a held prompt is waiting on this
                    except Exception:
                        pass

        session.last_parse_start = time.monotonic()
        debug_fn("agent parse started")
        session.agent_parse_thread = threading.Thread(target=worker, name="agit-session-parse", daemon=True)
        session.agent_parse_thread.start()
        return True

    # ------------------------------------------------------------------
    # Simple state helpers
    # ------------------------------------------------------------------

    def _add_turn_usage(self, turn) -> None:
        """Accumulate a turn's tokens EXACTLY ONCE across commits.

        A turn can legitimately appear in TWO commits: force-captured while still
        running (exit/teardown with ``require_complete=False`` — the watermark then
        falls back to its user id), and again once finished, because ``turns_after``
        re-exports the watermark turn INCLUSIVELY when it has since gained a final
        response (the crash-recovery continuation). Adding ``turn.tokens`` blindly on
        the re-commit counted the whole — by then much larger — turn a second time;
        with several restarts inside one long turn this inflated the recorded tokens
        13-26x (2026-07-25). The first commit records what it counted
        (``partial_turn_tokens``); the re-commit adds only the delta."""
        from agitrack.backends.base import TokenUsage

        usage = turn.tokens
        recorded = self.state.partial_turn_usage()
        is_partial_continuation = (
            recorded
            and recorded.get("user_id") == turn.user_message_id
            and recorded.get("session_id") == self.state.backend_session_id
        )
        # Second, independent guard against re-counting: a turn that ENDED at or before
        # the committed frontier (the watermark's recorded time) was already accounted
        # for — whatever export path resurfaced it. The one legitimate back-reach, a
        # force-captured turn finishing later, ends AFTER the frontier and is handled
        # by the partial-delta below.
        if not is_partial_continuation:
            marked_at = self.state.backend_message_marked_at_for(self.state.backend_session_id)
            turn_end = turn.ended_at or turn.started_at
            if marked_at and turn_end and turn_end <= marked_at:
                return
        if recorded is not None and is_partial_continuation:
            prior = recorded.get("usage") or {}
            delta = TokenUsage(
                **{
                    field: max(0, getattr(usage, field) - int(prior.get(field) or 0))
                    for field in TokenUsage._SUM_FIELDS
                }
            )
            delta.context = usage.context
            usage = delta
            self.state.clear_partial_turn_usage()
        self.state.add_token_usage(usage)

    def record_user_prompt(self, prompt_text: str) -> None:
        """Append a user prompt to the pending trace (no-op if empty or a bare
        slash command). Slash commands (``/compact``, ``/model``, …) are backend
        directives, not prompts, and are kept out of the trace — see
        :func:`_is_slash_command`."""
        if prompt_text and prompt_text.strip() and not _is_slash_command(prompt_text):
            self.state.append_trace("user", prompt_text)

    def await_followup(self, prompt_text: str, awaited: list[str]) -> list[str]:
        """Return a new awaited list with *prompt_text* appended if appropriate.

        Slash commands (/model, /compact, …) are skipped because they are
        filtered from the transcript and would defer commits indefinitely.
        The updated list must be stored by the caller (on the runner or
        wherever ``_awaited_followups`` lives).
        """
        norm = _norm(prompt_text)
        if norm and not _is_slash_command(prompt_text):
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
