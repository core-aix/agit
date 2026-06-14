from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from agit.summaries.prompts import (
    COMMIT_SUMMARY_SYSTEM,
    PRE_COMPACTION_SYSTEM,
    SESSION_UPDATE_SYSTEM,
)

if TYPE_CHECKING:
    from agit.backends.base import AgentBackend
    from agit.transcripts.types import ExportedSession, SessionTurn


class UnusableSummaryError(RuntimeError):
    """The summarizer backend returned an error instead of a summary."""


# Backend error messages that come back through the result text with a zero
# exit code (issue #8: "You've hit your session limit..." ended up as a commit
# subject). Matched against the summary's first line only — the would-be
# subject — with error-shaped phrasings, so a legitimate summary that merely
# *mentions* limits or errors is never rejected.
_UNUSABLE_SUMMARY_RE = re.compile(
    r"(?i)(?:"
    r"you'?\s*(?:ha)?ve hit your"
    r"|hit your (?:session|usage|rate|spending) limit"
    r"|(?:session|usage) limit reached"
    r"|limit will reset"
    r"|credit balance is too low"
    r"|please run /login"
    r"|invalid api key"
    r"|not logged in"
    r"|^\s*api error"
    r"|overloaded_error"
    r"|rate_limit_error"
    r"|authentication_error"
    r")"
)


# Phrases that only ever appear in the summarizer's *prompt* (its system
# instructions and scaffolding), never in a real summary. Some headless backend
# runs return the input prompt verbatim as the "response" with a zero exit code;
# without this guard that echoed prompt became the commit message — the bug where
# a commit subject was "You are a technical summarizer for a coding session…".
# Matched anywhere in the text (an echo restates these), and chosen to be
# distinctive enough that a genuine code-change summary never contains them.
_PROMPT_ECHO_MARKERS = (
    "you are a technical summarizer",
    "you are maintaining a running session summary",
    "you are capturing the full context of a coding session",
    "recent conversation turns:",
)


def summary_is_usable(text: str | None) -> bool:
    """True when *text* looks like an actual summary — not a backend error, and
    not an echo of the summarizer's own prompt."""
    stripped = (text or "").strip()
    first_line = next((line for line in stripped.splitlines() if line.strip()), "")
    if not first_line:
        return False
    if _UNUSABLE_SUMMARY_RE.search(first_line) is not None:
        return False
    lowered = stripped.lower()
    return not any(marker in lowered for marker in _PROMPT_ECHO_MARKERS)


def _looks_like_prompt_echo(prompt: str, response: str) -> bool:
    """The backend returned (a leading chunk of) the prompt we sent instead of a
    summary. Independent of which prompt was used: an echo restates the prompt
    from the top, so the normalised openings match. This backstops
    :data:`_PROMPT_ECHO_MARKERS` if the prompt wording ever changes."""

    def norm(text: str) -> str:
        return " ".join(text.split()).lower()

    prompt_norm, response_norm = norm(prompt), norm(response)
    window = min(len(prompt_norm), len(response_norm), 80)
    return window >= 40 and prompt_norm[:window] == response_norm[:window]


def summary_scratch_dir() -> Path:
    """A stable directory, outside any repository, for summarizer backends.

    Headless summarizer calls (``claude -p`` / ``opencode run``) record a real
    session transcript keyed by their working directory. Running them in the
    session worktree (or the repo root) made the summary conversation that
    directory's newest non-empty session, which the parse worker and the
    exit-time adoption then resumed instead of the user's actual conversation
    (issues #8/#56). Running every summarizer call from this scratch directory
    keeps summary sessions out of every repository's session records, so they
    can never be adopted, listed, or resumed as the previous session.
    """
    config_dir = os.environ.get("AGIT_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agit"
    path = base / "summarizer"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Summarizer:
    def __init__(self, backend: AgentBackend, *, model: str | None = None) -> None:
        self.backend = backend
        self.model = model
        # Tokens this summarizer instance consumed across its LLM calls, so the
        # cost of summarization can be tracked next to the coding session's own
        # usage (issue #8).
        self.tokens_input = 0
        self.tokens_output = 0

    def summarize_commit(
        self,
        *,
        turns: list[SessionTurn],
        diff: str,
        session_summary: str | None = None,
    ) -> str:
        return self._run(self._build_commit_prompt(turns, diff, session_summary))

    def update_session_summary(
        self,
        *,
        current_summary: str | None,
        turns: list[SessionTurn],
        diff: str,
        commit_summary: str,
    ) -> str:
        return self._run(self._build_session_update_prompt(current_summary, turns, diff, commit_summary))

    def summarize_pre_compaction(
        self,
        *,
        exported_session: ExportedSession,
        current_summary: str | None = None,
    ) -> str:
        return self._run(self._build_pre_compaction_prompt(exported_session, current_summary))

    def _run(self, prompt: str) -> str:
        result = self.backend.run(prompt, model=self.model, session_id=None)
        tokens = getattr(result, "tokens", None)
        if tokens is not None:
            self.tokens_input += int(getattr(tokens, "input", 0) or 0)
            self.tokens_output += int(getattr(tokens, "output", 0) or 0)
        text = result.final_response.strip()
        # A failed/echoed run must raise rather than return its text, or the error
        # (or the prompt itself) becomes the commit subject (issue #8). Callers
        # already treat a raising summarizer as "no summary" and keep the
        # prompt-led message.
        if result.exit_code != 0:
            raise UnusableSummaryError(f"summarizer backend exited with {result.exit_code}: {text[:200]}")
        if _looks_like_prompt_echo(prompt, text):
            raise UnusableSummaryError(f"summarizer echoed its prompt instead of summarizing: {text[:200]}")
        if not summary_is_usable(text):
            raise UnusableSummaryError(f"summarizer returned an error message: {text[:200]}")
        return text

    def _build_commit_prompt(
        self,
        turns: list[SessionTurn],
        diff: str,
        session_summary: str | None,
    ) -> str:
        parts = [COMMIT_SUMMARY_SYSTEM, "\n\n"]
        if session_summary:
            parts.extend(["Current session context:\n", session_summary, "\n\n"])
        parts.append("Recent conversation turns:\n")
        for turn in turns:
            if turn.user_prompt:
                parts.extend(["\nUser: ", turn.user_prompt])
            if turn.final_response:
                parts.extend(["\nAgent: ", turn.final_response])
        parts.extend(["\n\nCode changes (diff):\n```\n", diff, "\n```\n\nSummary:"])
        return "".join(parts)

    def _build_session_update_prompt(
        self,
        current_summary: str | None,
        turns: list[SessionTurn],
        diff: str,
        commit_summary: str,
    ) -> str:
        parts = [SESSION_UPDATE_SYSTEM, "\n\n"]
        if current_summary:
            parts.extend(["Current session summary:\n", current_summary, "\n\n"])
        else:
            parts.append("No previous session summary exists. Create an initial summary.\n\n")
        parts.extend(["New commit summary:\n", commit_summary, "\n\n"])
        parts.append("Recent conversation turns:\n")
        for turn in turns:
            if turn.user_prompt:
                parts.extend(["\nUser: ", turn.user_prompt])
            if turn.final_response:
                parts.extend(["\nAgent: ", turn.final_response])
        parts.extend(["\n\nCode changes (diff):\n```\n", diff, "\n```\n\nUpdated session summary:"])
        return "".join(parts)

    def _build_pre_compaction_prompt(
        self,
        exported_session: ExportedSession,
        current_summary: str | None,
    ) -> str:
        parts = [PRE_COMPACTION_SYSTEM, "\n\n"]
        if current_summary:
            parts.extend(["Current session summary:\n", current_summary, "\n\n"])
        parts.append("Full session transcript:\n")
        for turn in exported_session.turns:
            if turn.user_prompt:
                parts.extend(["\nUser: ", turn.user_prompt])
            if turn.final_response:
                parts.extend(["\nAgent: ", turn.final_response])
        parts.append("\n\nComprehensive session summary:")
        return "".join(parts)
