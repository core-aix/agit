from __future__ import annotations

import re
from textwrap import shorten, wrap

from agit import __version__

DEFAULT_SUBJECT = "No subject provided"
# GitHub truncates a commit's subject (its first line) at 72 characters in the
# commit list, PR commits, blame, etc. — anything longer is ellipsized. Size the
# whole subject line (prefix included) to that limit so it's never cut off.
MAX_SUBJECT_WIDTH = 72
MAX_BODY_WIDTH = 72
AGENT_SUBJECT_PREFIX = "<agent> "
SECRET_MASK = "[REDACTED]"
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization)\b(\s*[:=]\s*)([^\s,;]+)"
)
SECRET_TOKEN_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
MOUSE_REPORT_RE = re.compile(r"(?:\x1b)?\[<\d+;\d+;\d+[Mm]")
# Full ANSI/terminal escape sequences (CSI/OSC/DCS and lone two-byte escapes).
ANSI_SEQUENCE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[P-_][^\x1b]*\x1b\\"
    r"|\x1b[@-Z\\-_]"
)
# Control characters that should never appear in a commit message, keeping tab,
# newline, and carriage return intact.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def build_agent_commit_message(
    *,
    latest_prompt: str,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agit_session_id: str,
    model: str | None,
    token_usage: dict[str, int | None] | None = None,
    trace_turn_limit: int = 5,
    session_name: str | None = None,
) -> str:
    subject_prompt, full_subject = _subject_parts(_mask_secrets(latest_prompt), width=MAX_SUBJECT_WIDTH - len(AGENT_SUBJECT_PREFIX))
    lines = [f"{AGENT_SUBJECT_PREFIX}{subject_prompt}"]
    if full_subject:
        # The truncated subject flows straight into its full text with no blank
        # line between them, so the extended subject reads as one continued line.
        lines.extend(_body_lines(full_subject))
    lines.append("")
    lines.extend(["# Interaction Trace", ""])
    for item in _limit_trace_turns(trace, trace_turn_limit):
        role = item.get("role", "").strip().lower()
        content = _mask_secrets(item.get("content", ""))
        label = "User" if role == "user" else "Agent"
        lines.extend([f"## {label}", "", *_body_lines(content), ""])

    lines.extend(
        [
            "# aGiT Metadata",
            "commit_type: agent",
            f"backend: {backend}",
            f"model: {model or 'unknown'}",
            f"session_name: {session_name or 'unknown'}",
            f"agit_session_id: {agit_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
        ]
    )
    lines.extend(_token_metadata_lines(token_usage))
    lines.append(f"agit_version: {__version__}")
    return "\n".join(lines).rstrip() + "\n"


AGENT_MERGE_SUBJECT_PREFIX = "<agent-merge> "


def build_agent_merge_message(
    *,
    session_name: str | None,
    base_branch: str,
    source_branch: str,
    agit_session_id: str,
    backend: str,
    backend_session_id: str | None = None,
    conflicting_commits: str | None = None,
) -> str:
    """Commit message for a merge whose conflicts an agent resolved."""
    subject = f"{AGENT_MERGE_SUBJECT_PREFIX}integrate {session_name or source_branch} into {base_branch}"
    lines = [_subject_text(subject, width=MAX_SUBJECT_WIDTH), ""]
    if conflicting_commits and conflicting_commits.strip():
        lines.extend(["# Resolved Against Base Commits", ""])
        lines.extend(_body_lines(_mask_secrets(conflicting_commits)))
        lines.append("")
    lines.extend(
        [
            "# aGiT Metadata",
            "commit_type: agent-merge",
            f"backend: {backend}",
            f"session_name: {session_name or 'unknown'}",
            f"source_branch: {source_branch}",
            f"base_branch: {base_branch}",
            f"agit_session_id: {agit_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
            f"agit_version: {__version__}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_user_commit_message(
    *,
    message: str | None,
    agit_session_id: str,
) -> str:
    user_message = message.strip() if message else ""
    if not user_message:
        raise ValueError("User commit message is required")
    subject, full_subject = _subject_parts(_mask_secrets(user_message), width=MAX_SUBJECT_WIDTH)
    lines = [subject]
    if full_subject:
        # Extended subject continues directly under the subject line (no blank).
        lines.extend(_body_lines(full_subject))
    lines.append("")
    lines.extend(["# aGiT Metadata", "commit_type: user", "backend: agit", f"agit_session_id: {agit_session_id}", f"agit_version: {__version__}"])
    return "\n".join(lines).rstrip() + "\n"


def _subject_text(text: str, *, width: int) -> str:
    return _subject_parts(text, width=width)[0]


def _subject_parts(text: str, *, width: int) -> tuple[str, str | None]:
    one_line = " ".join(text.strip().split()) or DEFAULT_SUBJECT
    subject = shorten(one_line, width=width, placeholder="...")
    return subject, one_line if subject != one_line else None


def _body_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        lines.extend(wrap(raw_line, width=MAX_BODY_WIDTH, replace_whitespace=False, drop_whitespace=False) or [""])
    return lines


def _limit_trace_turns(trace: list[dict], turn_limit: int) -> list[dict]:
    limit = turn_limit if isinstance(turn_limit, int) and turn_limit > 0 else 5
    user_indexes = [index for index, item in enumerate(trace) if str(item.get("role", "")).strip().lower() == "user"]
    if len(user_indexes) <= limit:
        return trace
    return trace[user_indexes[-limit] :]


def _mask_secrets(text: object) -> str:
    value = str(text or "")
    value = MOUSE_REPORT_RE.sub("", value)
    value = ANSI_SEQUENCE_RE.sub("", value)
    value = CONTROL_CHAR_RE.sub("", value)
    value = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET_MASK}", value)
    for pattern in SECRET_TOKEN_RES:
        value = pattern.sub(SECRET_MASK, value)
    return value


def _token_value(token_usage: dict[str, int | None] | None, key: str) -> int | str:
    if not token_usage:
        return "unknown"
    value = token_usage.get(key)
    return value if value is not None else "unknown"


def _append_positive(lines: list[str], key: str, value: object) -> None:
    """Record a token category only when the backend actually reports it."""
    amount = value if isinstance(value, int) else 0
    if amount > 0:
        lines.append(f"{key}: {amount}")


def _token_metadata_lines(token_usage: dict[str, int | None] | None) -> list[str]:
    lines = [f"context_tokens: {_token_value(token_usage, 'context')}"]
    if not token_usage:
        lines.append("tokens_since_last_commit_input: unknown")
        lines.append("tokens_since_last_commit_output: unknown")
        return lines
    # Main-line conversation consumption, broken out by category. Input and
    # output are always recorded; cache and reasoning only when non-zero so the
    # metadata stays compact for backends that do not report them.
    lines.append(f"tokens_since_last_commit_input: {int(token_usage.get('input') or 0)}")
    _append_positive(lines, "tokens_since_last_commit_cache_read", token_usage.get("cache_read"))
    _append_positive(lines, "tokens_since_last_commit_cache_write", token_usage.get("cache_write"))
    lines.append(f"tokens_since_last_commit_output: {int(token_usage.get('output') or 0)}")
    _append_positive(lines, "tokens_since_last_commit_reasoning", token_usage.get("reasoning"))
    # Sub-agent / sidechain consumption, recorded separately and only when the
    # backend exposes it.
    _append_positive(lines, "tokens_since_last_commit_subagent_input", token_usage.get("subagent_input"))
    _append_positive(lines, "tokens_since_last_commit_subagent_cache_read", token_usage.get("subagent_cache_read"))
    _append_positive(lines, "tokens_since_last_commit_subagent_cache_write", token_usage.get("subagent_cache_write"))
    _append_positive(lines, "tokens_since_last_commit_subagent_output", token_usage.get("subagent_output"))
    _append_positive(lines, "tokens_since_last_commit_subagent_reasoning", token_usage.get("subagent_reasoning"))
    return lines
