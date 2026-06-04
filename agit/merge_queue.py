from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class MergeParticipant(Protocol):
    """A session that can integrate its completed turn into the base branch.

    Integration runs inside the session's own worktree (merge the base into the
    turn branch) so the owning agent can resolve any conflict in place; the
    coordinator then advances the base.
    """

    name: str

    def merge_base_into_turn(self) -> bool:
        """Merge the base branch into this session's turn branch (in its
        worktree). Return True if clean, False if there are conflicts."""

    def has_unmerged(self) -> bool:
        """Whether unresolved conflicts remain in the worktree."""

    def conflict_prompt(self) -> str:
        """Prompt text describing the conflict (files + conflicting commits) to
        hand to the owning agent."""

    def inject_prompt(self, text: str) -> None:
        """Send a prompt to the backend session to resolve the conflict."""

    def turn_just_completed(self) -> bool:
        """Whether the agent produced a new final message since resolution
        began (i.e. it finished the resolution turn)."""

    def finalize(self) -> None:
        """On a clean/resolved merge: fast-forward the base to the turn branch,
        delete the old turn branch, and start the next turn branch."""

    def abort_merge(self) -> None:
        """Abandon the in-progress merge, leaving the session as it was."""


class MergeState(Enum):
    IDLE = "idle"
    RESOLVING = "resolving"
    PAUSED = "paused"


@dataclass
class MergeTask:
    session_name: str
    completed_at: float


class MergeCoordinator:
    """Serialized, completion-ordered integration of session turn branches.

    ``tick()`` is called repeatedly from the event loop and never blocks: it
    advances one integration at a time. Conflicts that the owning agent cannot
    resolve leave the coordinator PAUSED for the user to act on.
    """

    def __init__(self, participants: dict[str, MergeParticipant], *, clock=time.monotonic) -> None:
        self.participants = participants
        self._clock = clock
        self.queue: list[MergeTask] = []
        self.state = MergeState.IDLE
        self.current: MergeTask | None = None
        self.paused_reason: str | None = None

    def enqueue(self, session_name: str) -> None:
        if session_name == (self.current.session_name if self.current else None):
            return
        if any(task.session_name == session_name for task in self.queue):
            return
        self.queue.append(MergeTask(session_name, self._clock()))

    def pending(self) -> list[str]:
        names = [task.session_name for task in self.queue]
        if self.current and self.state in {MergeState.RESOLVING, MergeState.PAUSED}:
            names.insert(0, self.current.session_name)
        return names

    def is_busy(self) -> bool:
        return self.state is not MergeState.IDLE or bool(self.queue)

    def tick(self) -> None:
        if self.state is MergeState.PAUSED:
            return
        if self.state is MergeState.IDLE:
            self._start_next()
        elif self.state is MergeState.RESOLVING:
            self._check_resolution()

    def resolve_paused(self) -> None:
        """User has finished resolving by hand; re-check and finalize if clean."""
        if self.state is not MergeState.PAUSED or self.current is None:
            return
        participant = self.participants.get(self.current.session_name)
        if participant is None or not participant.has_unmerged():
            if participant is not None:
                participant.finalize()
            self._clear_current()
        # else: still conflicted; remain paused.

    def skip_paused(self) -> None:
        """Abandon the paused integration (its branch is left for stale recovery)."""
        if self.state is not MergeState.PAUSED or self.current is None:
            return
        participant = self.participants.get(self.current.session_name)
        if participant is not None:
            participant.abort_merge()
        self._clear_current()

    def drop(self, session_name: str) -> None:
        """Remove a session from the queue (e.g. when it is stopped)."""
        self.queue = [task for task in self.queue if task.session_name != session_name]
        if self.current and self.current.session_name == session_name and self.state is not MergeState.IDLE:
            participant = self.participants.get(session_name)
            if participant is not None:
                participant.abort_merge()
            self._clear_current()

    def _start_next(self) -> None:
        if not self.queue:
            return
        self.current = self.queue.pop(0)
        participant = self.participants.get(self.current.session_name)
        if participant is None:
            self.current = None
            return
        if participant.merge_base_into_turn():
            participant.finalize()
            self._clear_current()
        else:
            participant.inject_prompt(participant.conflict_prompt())
            self.state = MergeState.RESOLVING

    def _check_resolution(self) -> None:
        assert self.current is not None
        participant = self.participants.get(self.current.session_name)
        if participant is None:
            self._clear_current()
            return
        if not participant.turn_just_completed():
            return
        if participant.has_unmerged():
            self.state = MergeState.PAUSED
            self.paused_reason = f"{self.current.session_name}: unresolved merge conflict"
        else:
            participant.finalize()
            self._clear_current()

    def _clear_current(self) -> None:
        self.current = None
        self.state = MergeState.IDLE
        self.paused_reason = None
