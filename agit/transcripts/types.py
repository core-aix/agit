from __future__ import annotations

from dataclasses import dataclass

from agit.backends.base import TokenUsage


@dataclass
class SessionTurn:
    user_message_id: str
    assistant_message_id: str
    user_prompt: str
    final_response: str
    tokens: TokenUsage
    model: str | None
    # Whether the agent's response to this prompt has finished. False while the
    # turn is still mid-flight (the backend's last message was a tool call, not a
    # final answer), so aGiT can defer committing until the prompt is fully
    # answered and never split one prompt across several commits. Defaults True so
    # backends that don't report a finish reason keep committing on idle as before.
    complete: bool = True
    # True when the user interrupted this turn (Esc): it will never receive more
    # messages, and any prompts the user had queued behind it were discarded by
    # the backend — so nothing should keep waiting for them.
    interrupted: bool = False


@dataclass
class ExportedSession:
    session_id: str
    model: str | None
    updated: int | None
    turns: list[SessionTurn]


@dataclass
class SessionRef:
    """A lightweight reference to one of a repository's backend sessions, used
    to list, discover, and switch the session aGiT tracks."""

    id: str
    updated: float  # epoch seconds; newest wins
    label: str | None = None


def turns_after(session: ExportedSession, last_message_id: str | None) -> list[SessionTurn]:
    if not last_message_id:
        return session.turns
    for index, turn in enumerate(session.turns):
        if turn.assistant_message_id == last_message_id or turn.user_message_id == last_message_id:
            return session.turns[index + 1 :]
    return session.turns
