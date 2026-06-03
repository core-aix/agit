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


@dataclass
class ExportedSession:
    session_id: str
    model: str | None
    updated: int | None
    turns: list[SessionTurn]


def turns_after(session: ExportedSession, last_message_id: str | None) -> list[SessionTurn]:
    if not last_message_id:
        return session.turns
    for index, turn in enumerate(session.turns):
        if turn.assistant_message_id == last_message_id or turn.user_message_id == last_message_id:
            return session.turns[index + 1 :]
    return session.turns
