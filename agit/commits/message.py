from __future__ import annotations

import re
from datetime import datetime, timezone
from textwrap import shorten, wrap

from agit import __version__

DEFAULT_SUBJECT = "No subject provided"
# GitHub truncates a commit's subject (its first line) at 72 characters in the
# commit list, PR commits, blame, etc. — anything longer is ellipsized. Size the
# whole subject line (prefix included) to that limit so it's never cut off.
MAX_SUBJECT_WIDTH = 72
MAX_BODY_WIDTH = 72
# Subject tag for agent commits: every commit aGiT creates from the agent's
# work, including the cover commits placed on top of backend-made commits to
# carry their trace/metadata (issues #35/#58).
AGIT_SUBJECT_PREFIX = "<aGiT> "
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


# Section header that marks a commit as carrying aGiT metadata. Detection of
# backend-made commits (issue #35) checks message bodies for this exact text,
# so keep the builders and the detector on one definition.
METADATA_HEADER = "# aGiT Metadata"


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
    summary: str | None = None,
    summary_metadata: list[str] | None = None,
    covered_commits: list[str] | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
) -> str:
    if summary:
        # The summary leads (issue #8): its first line becomes the subject, the
        # rest of it is the first paragraph of the body (no # Summary section),
        # and the prompts move to a # Prompts section.
        lines = _summary_lead_lines(summary)
        prompts: str | None = latest_prompt
    else:
        subject_prompt, full_subject = _subject_parts(
            _mask_secrets(latest_prompt), width=MAX_SUBJECT_WIDTH - len(AGIT_SUBJECT_PREFIX)
        )
        lines = [f"{AGIT_SUBJECT_PREFIX}{subject_prompt}"]
        if full_subject:
            # The truncated subject flows straight into its full text with no blank
            # line between them, so the extended subject reads as one continued line.
            lines.extend(_body_lines(full_subject))
        prompts = None
    lines.append("")
    lines.extend(
        _trace_and_metadata_lines(
            trace=trace,
            backend=backend,
            backend_session_id=backend_session_id,
            agit_session_id=agit_session_id,
            model=model,
            token_usage=token_usage,
            trace_turn_limit=trace_turn_limit,
            session_name=session_name,
            prompts=prompts,
            summary_metadata=summary_metadata,
            covered_commits=covered_commits,
            started_at=started_at,
            ended_at=ended_at,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def apply_summary_to_message(
    message: str,
    summary: str,
    *,
    summary_metadata: list[str] | None = None,
) -> str:
    """Rewrite an existing agent commit message so the summary leads (#8).

    The summary's first line becomes the subject, the rest of the summary
    becomes the first paragraph of the body (no ``# Summary`` section), and
    the original subject (the collected prompts) is preserved under
    ``# Prompts``. ``summary_metadata`` lines are added to the metadata
    section. Idempotent: a message that already carries a summary (marked by
    its ``# Prompts`` section) is returned unchanged, so a redundant amend can
    never happen.
    """
    if not summary.strip() or "\n# Prompts\n" in message or message.startswith("# Prompts"):
        return message
    lines = message.splitlines()
    try:
        subject_end = lines.index("")
    except ValueError:
        subject_end = len(lines)
    original_subject = "\n".join(lines[:subject_end])
    if original_subject.startswith(AGIT_SUBJECT_PREFIX):
        original_subject = original_subject[len(AGIT_SUBJECT_PREFIX) :]
    rest = lines[subject_end + 1 :]

    new_lines = _summary_lead_lines(summary)
    new_lines.append("")
    if original_subject.strip():
        new_lines.extend(["# Prompts", "", *_body_lines(original_subject), ""])
    new_lines.extend(rest)
    if summary_metadata:
        new_lines = _insert_before_version_line(new_lines, summary_metadata)
    return "\n".join(new_lines).rstrip() + "\n"


def summary_metadata_lines(*, model: str | None, tokens_input: int = 0, tokens_output: int = 0) -> list[str]:
    """Metadata recording what the summarization itself cost (issue #8)."""
    lines = [f"summary_model: {model or 'unknown'}"]
    if tokens_input > 0:
        lines.append(f"summary_tokens_input: {tokens_input}")
    if tokens_output > 0:
        lines.append(f"summary_tokens_output: {tokens_output}")
    return lines


def _is_summary_header(line: str) -> bool:
    """A line that is only the word "summary" plus non-letters — e.g. ``Summary``,
    ``Summary:``, ``## Summary``, ``**Summary**``. Models sometimes emit such a
    header as the first line; left alone it becomes a useless commit subject."""
    return re.sub(r"[^a-zA-Z]", "", line).lower() == "summary"


def _summary_lead_lines(summary: str) -> list[str]:
    """Subject + leading body for a summarized message.

    Mirrors the prompt-led layout: the summary's first line is the subject
    (a truncated subject flows straight into its full text, no blank line),
    and the rest of the summary follows as the first paragraph of the body —
    there is no separate ``# Summary`` section. A leading bare "Summary" header
    line is skipped so the subject is never just the word "summary".
    """
    text_lines = _mask_secrets(summary).strip().splitlines()
    first_index = next(
        (i for i, line in enumerate(text_lines) if line.strip() and not _is_summary_header(line)),
        None,
    )
    first_line = text_lines[first_index] if first_index is not None else DEFAULT_SUBJECT
    remainder = text_lines[first_index + 1 :] if first_index is not None else []
    while remainder and not remainder[0].strip():
        remainder.pop(0)

    subject, full = _subject_parts(first_line, width=MAX_SUBJECT_WIDTH - len(AGIT_SUBJECT_PREFIX))
    lines = [f"{AGIT_SUBJECT_PREFIX}{subject}"]
    if full:
        lines.extend(_body_lines(full))
    if remainder:
        lines.append("")
        lines.extend(_body_lines("\n".join(remainder).rstrip()))
    return lines


def _insert_before_version_line(lines: list[str], extra: list[str]) -> list[str]:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("agit_version:"):
            return lines[:index] + list(extra) + lines[index:]
    return lines + list(extra)


def _trace_and_metadata_lines(
    *,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agit_session_id: str,
    model: str | None,
    token_usage: dict[str, int | None] | None,
    trace_turn_limit: int,
    session_name: str | None,
    covered_commits: list[str] | None,
    prompts: str | None = None,
    summary_metadata: list[str] | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
) -> list[str]:
    lines: list[str] = []
    if prompts and prompts.strip():
        # When the summary takes the subject (#8), the prompts that used to
        # head the message are preserved here.
        lines.extend(["# Prompts", "", *_body_lines(_mask_secrets(prompts)), ""])
    lines.extend(["# Interaction Trace", ""])
    for item in _limit_trace_turns(trace, trace_turn_limit):
        role = item.get("role", "").strip().lower()
        content = _mask_secrets(item.get("content", ""))
        label = "User" if role == "user" else "Agent"
        lines.extend([f"## {label}", "", *_body_lines(content), ""])

    lines.extend(
        [
            METADATA_HEADER,
            "commit_type: agent",
            f"backend: {backend}",
            f"model: {model or 'unknown'}",
            f"session_name: {session_name or 'unknown'}",
            f"agit_session_id: {agit_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
        ]
    )
    if covered_commits:
        # The backend-made commits this trace/metadata accounts for (#35).
        # Those commits are never rewritten, so the hashes stay valid (#58).
        lines.append(f"covered_commits: {' '.join(covered_commits)}")
    # When the AI-driven conversation began and ended (UTC, ISO-8601), so the
    # dashboard can report durations and filter by time.
    if started_at is not None:
        lines.append(f"agent_started_at: {_iso_utc(started_at)}")
    if ended_at is not None:
        lines.append(f"agent_ended_at: {_iso_utc(ended_at)}")
    lines.extend(_token_metadata_lines(token_usage))
    if summary_metadata:
        lines.extend(summary_metadata)
    lines.append(f"agit_version: {__version__}")
    return lines


AGENT_MERGE_SUBJECT_PREFIX = "<aGiT-merge> "


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
    lines.extend(
        [
            "# aGiT Metadata",
            "commit_type: user",
            "backend: agit",
            f"agit_session_id: {agit_session_id}",
            f"agit_version: {__version__}",
        ]
    )
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


def _int_value(token_usage: dict[str, int | None], key: str) -> int:
    value = token_usage.get(key)
    return value if isinstance(value, int) else 0


def _iso_utc(epoch_seconds: int) -> str:
    """Epoch seconds → `YYYY-MM-DDTHH:MM:SSZ` in UTC (second precision)."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_metadata_lines(token_usage: dict[str, int | None] | None) -> list[str]:
    lines = [f"context_tokens: {_token_value(token_usage, 'context')}"]
    if not token_usage:
        lines.append("tokens_since_last_commit_input: unknown")
        lines.append("tokens_since_last_commit_output: unknown")
        return lines
    # Main-line conversation consumption, broken out by category. Input and
    # output are always recorded; cache and reasoning only when non-zero so the
    # metadata stays compact for backends that do not report them.
    #
    # Backends report cache-creation tokens separately from input_tokens, but
    # they ARE fresh input — processed once and written to the cache. Counting
    # only the uncached remainder made a first run's input look near zero next
    # to its cache_write (issue #14), so the input line counts both; the
    # cache_write line below remains the "of which was written to the cache"
    # breakdown. Cache READS stay separate: those tokens were already counted
    # as input when first processed.
    lines.append(
        f"tokens_since_last_commit_input: {_int_value(token_usage, 'input') + _int_value(token_usage, 'cache_write')}"
    )
    _append_positive(lines, "tokens_since_last_commit_cache_read", token_usage.get("cache_read"))
    _append_positive(lines, "tokens_since_last_commit_cache_write", token_usage.get("cache_write"))
    lines.append(f"tokens_since_last_commit_output: {int(token_usage.get('output') or 0)}")
    _append_positive(lines, "tokens_since_last_commit_reasoning", token_usage.get("reasoning"))
    # Sub-agent / sidechain consumption, recorded separately (same input
    # accounting as the main line) and only when the backend exposes it.
    _append_positive(
        lines,
        "tokens_since_last_commit_subagent_input",
        _int_value(token_usage, "subagent_input") + _int_value(token_usage, "subagent_cache_write"),
    )
    _append_positive(lines, "tokens_since_last_commit_subagent_cache_read", token_usage.get("subagent_cache_read"))
    _append_positive(lines, "tokens_since_last_commit_subagent_cache_write", token_usage.get("subagent_cache_write"))
    _append_positive(lines, "tokens_since_last_commit_subagent_output", token_usage.get("subagent_output"))
    _append_positive(lines, "tokens_since_last_commit_subagent_reasoning", token_usage.get("subagent_reasoning"))
    return lines
