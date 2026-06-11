from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TokenUsage:
    context: int | None = None
    total: int = 0
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Tokens consumed by sub-agents / sidechain turns the backend spawns. Kept
    # separate from the main-line counters so they can be reported as their own
    # category rather than silently dropped or conflated with the main session.
    subagent_input: int = 0
    subagent_output: int = 0
    subagent_reasoning: int = 0
    subagent_cache_read: int = 0
    subagent_cache_write: int = 0

    _SUM_FIELDS = (
        "total",
        "input",
        "output",
        "reasoning",
        "cache_read",
        "cache_write",
        "subagent_input",
        "subagent_output",
        "subagent_reasoning",
        "subagent_cache_read",
        "subagent_cache_write",
    )

    def to_dict(self) -> dict[str, int | None]:
        data: dict[str, int | None] = {"context": self.context}
        for field in self._SUM_FIELDS:
            data[field] = getattr(self, field)
        return data

    def add(self, other: "TokenUsage") -> None:
        if other.context is not None:
            self.context = other.context
        for field in self._SUM_FIELDS:
            setattr(self, field, getattr(self, field) + getattr(other, field))


@dataclass
class AgentResult:
    backend: str
    session_id: str | None
    model: str | None
    final_response: str
    exit_code: int
    tokens: TokenUsage


class AgentBackend(Protocol):
    name: str

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult: ...
