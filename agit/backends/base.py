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

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        ...
