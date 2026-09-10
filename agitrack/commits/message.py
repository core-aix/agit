from __future__ import annotations

import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from textwrap import wrap

from agitrack import __version__
from agitrack.commits.foreign import FOREIGN_REPO_MASK, redact_foreign_repos
from agitrack.transcripts.capabilities import FIELDS as CAPABILITY_FIELDS


def _system_info() -> str:
    """Host OS and version for the commit metadata. Tool availability differs across
    macOS / Linux / Windows, so recording the platform helps interpret a session
    (and any tool-specific behaviour) after the fact."""
    system = platform.system()
    info = ""
    try:
        if system == "Darwin":
            info = f"macOS {platform.mac_ver()[0]}"
        elif system == "Windows":
            info = f"Windows {platform.release()}"
        elif system == "Linux":
            release = platform.freedesktop_os_release()
            version = release.get("VERSION_ID") or release.get("VERSION", "")
            info = f"{release.get('NAME', 'Linux')} {version}"
    except (OSError, AttributeError, KeyError):
        info = ""
    if not info.strip():
        info = f"{system} {platform.release()}"
    # Keep the metadata line within the 72-char commit-body width even for an
    # unusually long distro string.
    return info.strip()[:60] or "unknown"


def _finish(lines: list[str], repo_root: str | Path | None) -> str:
    """Join a built message and take OTHER projects' names out of it.

    The last step of every builder, and deliberately a step over the FINISHED text rather than
    one more thing each of the dozen places that mask a fragment has to remember. Foreign-repo
    redaction is the only masking that needs to know which repository is being committed to, and
    threading that through `_summary_lead_lines`, `_trace_role_lines`, `_note_block` and the rest
    would put the same argument in eight private signatures for one pass that reads the same
    answer each time. ``repo_root=None`` (a caller that has not said which repo it is) leaves the
    text exactly as built — see :func:`agitrack.commits.foreign.redact_foreign_repos`."""
    return redact_foreign_repos("\n".join(lines).rstrip() + "\n", repo_root)


DEFAULT_SUBJECT = "No subject provided"
# GitHub truncates a commit's subject (its first line) at 72 characters in the
# commit list, PR commits, blame, etc. — anything longer is ellipsized. Size the
# whole subject line (prefix included) to that limit so it's never cut off.
MAX_SUBJECT_WIDTH = 72
MAX_BODY_WIDTH = 72
# Subject tag for agent commits: every commit aGiTrack creates from the agent's
# work, including the cover commits placed on top of backend-made commits to
# carry their trace/metadata (issues #35/#58).
AGITRACK_SUBJECT_PREFIX = "<aGiTrack> "
SECRET_MASK = "[REDACTED]"
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization)\b(\s*[:=]\s*)([^\s,;]+)"
)
# High-confidence secret token shapes — the set GitHub's secret-scanning push protection
# blocks. Redaction must cover at least these, or a transcript carrying one is refused by
# origin ("push declined") instead of sharing. Each has a distinctive prefix, so the
# false-positive risk is low. (Generic ``name = value`` secrets are caught separately by
# SECRET_ASSIGNMENT_RE above.)
SECRET_TOKEN_RES = [
    # OpenAI / Anthropic and other "sk-" API keys (covers sk-proj-…, sk-ant-…, incl. the
    # sk-ant-oat…/sk-ant-ort… OAuth access/refresh tokens Claude login stores).
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    # Claude Code / Anthropic OAuth login "reply key": after approving in the browser, the login
    # page shows an authorization code and state joined by '#' (``code#state``) to paste back
    # into the terminal — so it lands in the transcript as a pasted user message and, from there,
    # in the commit trace. One-time use, but a secret that never belongs in a commit or a shared
    # transcript. Two long URL-safe tokens joined by '#' is a distinctive shape; the lookarounds
    # keep it from firing inside a longer run, so ordinary trace text — a bare SHA, a UUID, a URL
    # with a short ``#fragment`` — is left untouched.
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    # GitHub personal-access / OAuth / user / server / refresh tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    # GitHub fine-grained personal-access token.
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # GitLab personal-access / pipeline-trigger tokens.
    re.compile(r"\bgl(?:pat|ptt|rt|soat)-[A-Za-z0-9_-]{16,}\b"),
    # Slack bot / user / app / refresh / config tokens.
    re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}\b"),
    # AWS access key id (long-term AKIA + temporary/role/user ASIA, AROA, AIDA, …).
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|A3T[A-Z0-9])[0-9A-Z]{16}\b"),
    # Google API key and OAuth access token.
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    # Stripe live / restricted secret keys.
    re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"),
    # npm automation token.
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    # PyPI / Test-PyPI upload token.
    re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}\b"),
    # SendGrid API key.
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    # Twilio API key SID.
    re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    # Doppler service / personal token.
    re.compile(r"\bdop_v1_[A-Za-z0-9]{32,}\b"),
    # PEM private-key block (RSA/EC/OpenSSH/PGP/…); in a JSONL transcript the whole block
    # sits on one physical line, so grab it end-to-end when the END marker is present too.
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----)?"),
]
# --- filesystem-path masking -------------------------------------------------------------
# Commit messages are shared, pushed and read by other people; an absolute path in one leaks
# the account name and the machine's layout (``/home/alice/Code/client-work/...``) for no
# benefit to the reader. Every ABSOLUTE path is replaced with PATH_MASK. Relative paths are
# deliberately kept: ``paper/main.tex`` says which file changed and reveals nothing about the
# machine.
PATH_MASK = "[PATH]"
# A URL is not a filesystem path — matched FIRST and passed through unchanged, so the "//" in
# ``https://github.com/core-aix/agitrack`` is never mistaken for a path.
_URL_RE = r"(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+:)[^\s<>\"']+"
# POSIX absolute path. Two things keep this off ordinary prose:
#   * the lookbehind requires the leading "/" to start a word, so "and/or" and
#     "input/output/buffer" are untouched (their slashes follow a word character);
#   * at least TWO segments are required ("/a/b"), so a backend SLASH COMMAND — /compact,
#     /model, /clear, which routinely appear in prompts and in commit subjects — survives,
#     while /etc/hosts and /usr/bin/python3 are masked.
_POSIX_PATH_RE = r"(?<![\w.~-])/(?:[^\s/\\<>\"'`|,;]+/)+[^\s/\\<>\"'`|,;]*"
# "~" / "~user" home paths. Unambiguous, so one segment is enough (``~/notes``). The user part
# must be empty or start with a letter/underscore, so "~123/second" — a tilde meaning
# "approximately", as in "(~123/second)" — is not read as a home path.
_TILDE_PATH_RE = r"(?<![\w./-])~(?:[A-Za-z_][A-Za-z0-9_.-]*)?/[^\s<>\"'`|,;]*"
# Windows drive-letter and UNC paths: C:\Users\me\x, C:/Users/me/x, \\server\share\x.
_WINDOWS_PATH_RE = r"(?<![\w.-])(?:[A-Za-z]:[\\/]|\\\\[^\s\\<>\"'`]+\\)[^\s<>\"'`|,;]*"
PATH_RE = re.compile(f"(?P<url>{_URL_RE})|(?:{_WINDOWS_PATH_RE}|{_TILDE_PATH_RE}|{_POSIX_PATH_RE})")
# Punctuation that ends the SENTENCE rather than the path ("see ~/notes." keeps its full
# stop). Stripped off the match and re-appended after the mask.
_PATH_TRAILING_PUNCT = ".,;:!?)]}\"'"

# A well-formed SGR mouse report, and the defense-in-depth net under it: a MALFORMED one,
# cut off or corrupted before its M/m terminator ever arrived (the proxy's own capture aborts
# these now — see _escape_sequence_complete — but a trace item can reach here from elsewhere,
# and this must not depend on capture being perfect). No real prompt starts "[<35;124;48": the
# body is exactly digits and ';', so a run of that shape is stripped whether or not it happens
# to end in M/m.
MOUSE_REPORT_RE = re.compile(r"(?:\x1b)?\[<\d+;\d+;\d+[Mm]")
MOUSE_REPORT_FRAGMENT_RE = re.compile(r"(?:\x1b)?\[<\d+(?:;\d+){1,2}[Mm]?")
# Full ANSI/terminal escape sequences (CSI/OSC/DCS and lone two-byte escapes).
#
# The `[P-_]` and `[@-Z\-_]` ranges look like typos and are not: they are CODE-POINT ranges from
# ECMA-48, not letter ranges. `P-_` is 0x50-0x5F — exactly the string-introducer half of the C1
# set (DCS `P`, SOS `X`, PM `^`, APC `_`), which is what a string-terminated escape begins with;
# `@-Z` plus `\-_` is the rest of the Fe range for the two-byte forms. CodeQL's `py/overly-large-
# range` flags any class spanning letters into punctuation as suspicious, so this one is reported
# and dismissed rather than narrowed: writing out the members instead would be the same set,
# longer, and would stop saying WHY these characters belong together.
ANSI_SEQUENCE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[P-_][^\x1b]*\x1b\\"
    r"|\x1b[@-Z\\-_]"
)
# Control characters that should never appear in a commit message, keeping tab,
# newline, and carriage return intact.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# An ATX Markdown heading: 1–6 leading '#' followed by whitespace (matches the
# dashboard's own md() parser, so a line is a heading here iff it renders as one
# there). Used to nest a trace message's own headings under its role heading.
ATX_HEADING_RE = re.compile(r"^(#{1,6})(\s.*)$")
# A fenced code block delimiter; a leading '#' inside such a fence is a comment,
# not a heading, so heading-nesting must skip fenced regions.
CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# The role heading for each trace turn ("## User" / "## Agent") is level 2.
TRACE_ROLE_HEADING_LEVEL = 2
# Every heading inside a trace message moves down by exactly this much: `#` → `###`,
# `##` → `####`. A FIXED shift, not a normalization of the shallowest heading to some floor —
# the message's own levels are its author's, and re-basing them changes what the author wrote
# about their own structure. Two levels because one nested correctly but did not READ
# correctly: at level 3 a message's own sections sat a single `#` from the `## User`/`## Agent`
# scaffolding, and in plain-text `git log` telling the trace's structure from the
# conversation's content meant counting hashes. Levels are capped at the Markdown maximum of
# 6, so a message already nesting 5 deep loses some depth at the bottom — the rare case, and
# preferable to re-basing every message.
TRACE_HEADING_SHIFT = 2


# Section header that marks a commit as carrying aGiTrack metadata. Detection of
# backend-made commits (issue #35) checks message bodies for this exact text,
# so keep the builders and the detector on one definition.
METADATA_HEADER = "# aGiTrack Metadata"
# The section every aGiTrack agent commit body opens with, and therefore the boundary
# between a message's lead (subject + body paragraphs) and its recorded sections.
TRACE_HEADER = "# Interaction Trace"
# Marks a metadata block as the PARTIAL record of a turn that was still running when the
# commit was made (see `build_in_flight_trailer`). Callers that ask "is this commit already
# accounted for?" must answer NO for such a commit: it carries attribution but not the turn's
# trace or tokens, so the completed turn still has to cover it.
IN_FLIGHT_MARKER = "in_flight: true"


def is_in_flight_only_message(body: str) -> bool:
    """Whether *body* carries an aGiTrack record that is ONLY an in-flight block — the agent
    committed while its turn was still running, so the commit names who made the change but
    still owes the turn's trace and tokens.

    Deliberately narrower than ``not is_fully_tracked_message(...)``, which is also true of a
    commit with no aGiTrack metadata at all. The two need opposite handling: a commit made
    outside aGiTrack can never be completed (there is no pending record to attach), so the
    latent chain is redundant and must be dropped; an in-flight one is mid-record, so the chain
    is the only thing holding the accounting until it lands."""
    if METADATA_HEADER not in body:
        return False
    return not is_fully_tracked_message(body)


def is_fully_tracked_message(body: str) -> bool:
    """Whether *body* carries a COMPLETE aGiTrack record — metadata that accounts for a
    finished turn. False for a message with no metadata at all, and false for one whose only
    metadata is an in-flight block, because that turn's trace and token usage are still to
    come and must land in a commit that covers this one."""
    if METADATA_HEADER not in body:
        return False
    blocks = body.split(METADATA_HEADER)[1:]
    return any(IN_FLIGHT_MARKER not in block for block in blocks)


# A token line that records real usage. `: [1-9]` excludes an explicit zero, so a block that
# accounts for a turn which burned nothing does not read as AI work.
_NONZERO_TOKENS_RE = re.compile(r"^tokens_since_last_commit_[a-z_]+: [1-9]", re.MULTILINE)


def _metadata_blocks(body: str) -> list[str]:
    """The metadata blocks in *body*, each cut at its first blank line.

    A block is a run of contiguous ``key: value`` lines, so the blank line after it ends it. The
    cut matters: without it a block would swallow the NEXT turn's interaction trace, and a trace
    that happens to quote a metadata line (aGiTrack's own repo does this constantly) would then be
    read as that block's own record.
    """
    return [chunk.split("\n\n", 1)[0] for chunk in body.split(METADATA_HEADER)[1:]]


def carries_ai_history(body: str) -> bool:
    """Whether *body* records actual AI WORK — not merely that aGiTrack was present.

    ``METADATA_HEADER in body`` answers the weaker question, and conflating the two is a real
    source of confusion: a repo whose only aGiTrack commit is a plain USER commit looks fully
    tracked (100% coverage) while its dashboard is empty, and `--backtrace commit` refuses to
    annotate anything — "every agent-made commit here is already tracked" — on the strength of an
    attribution block holding no turn, no trace and no tokens.

    A block qualifies when it is an agent/agent-merge record, or carries a non-zero token count.
    A user commit that FOLDED turns in (manual mode) carries their `commit_type: agent` blocks and
    still qualifies, so this never demotes a genuinely tracked commit. In-flight blocks do not
    qualify — their trace and tokens are still to come.
    """
    for block in _metadata_blocks(body):
        if IN_FLIGHT_MARKER in block:
            continue
        commit_type = ""
        for line in block.splitlines():
            if line.startswith("commit_type:"):
                commit_type = line.split(":", 1)[1].strip()
                break
        # No commit_type at all is agent work, exactly as the dashboard's classifier reads it.
        if commit_type in ("", "agent", "agent-merge") or _NONZERO_TOKENS_RE.search(block):
            return True
    return False


def build_agent_commit_message(
    *,
    latest_prompt: str,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agitrack_session_id: str,
    model: str | None,
    backend_version: str | None = None,
    reasoning_effort: str | None = None,
    conversation_anchor: str | None = None,
    token_usage: dict[str, int | None] | None = None,
    trace_turn_limit: int = 5,
    session_name: str | None = None,
    summary: str | None = None,
    summary_metadata: list[str] | None = None,
    covered_commits: list[str] | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
    compactions: int = 0,
    origin_event: dict | None = None,
    capabilities: dict[str, list[str]] | None = None,
    interrupted: bool = False,
    changed_paths: list[str] | None = None,
    repo_root: str | Path | None = None,
) -> str:
    if summary:
        # The summary leads (issue #8): its first line becomes the subject, the
        # rest of it is the first paragraph of the body (no # Summary section).
        # The prompts are not duplicated into the message — the interaction trace
        # below already carries them verbatim.
        lines = _summary_lead_lines(summary, interrupted=interrupted, repo_root=repo_root)
    else:
        subject_prompt, full_subject = _subject_parts(
            _mask_secrets(latest_prompt, repo_root), width=MAX_SUBJECT_WIDTH - len(AGITRACK_SUBJECT_PREFIX)
        )
        lines = [f"{AGITRACK_SUBJECT_PREFIX}{_interrupted_subject(subject_prompt, interrupted)}"]
        if full_subject:
            # A BLANK line first: git's subject is the whole first paragraph, newlines
            # folded to spaces, so a continuation glued under the first sentence put the
            # entire paragraph back into `git log --oneline` (a real 286-character entry).
            # The blank line is what makes the split at the first sentence actually hold.
            lines.append("")
            lines.extend(_body_lines(full_subject))
    lines.append("")
    lines.extend(
        _trace_and_metadata_lines(
            trace=trace,
            backend=backend,
            backend_session_id=backend_session_id,
            agitrack_session_id=agitrack_session_id,
            model=model,
            backend_version=backend_version,
            reasoning_effort=reasoning_effort,
            conversation_anchor=conversation_anchor,
            token_usage=token_usage,
            trace_turn_limit=trace_turn_limit,
            session_name=session_name,
            summary_metadata=summary_metadata,
            covered_commits=covered_commits,
            started_at=started_at,
            ended_at=ended_at,
            compactions=compactions,
            origin_event=origin_event,
            capabilities=capabilities,
            interrupted=interrupted,
            changed_paths=changed_paths,
            repo_root=repo_root,
        )
    )
    return _finish(lines, repo_root)


def apply_summary_to_message(
    message: str,
    summary: str,
    *,
    summary_metadata: list[str] | None = None,
    repo_root: str | Path | None = None,
) -> str:
    """Rewrite an existing agent commit message so the summary leads (#8).

    The summary's first line becomes the subject and the rest of the summary
    becomes the first paragraph of the body (no ``# Summary`` section). The
    original prompt-led subject is dropped, not preserved in a section — the
    ``# Interaction Trace`` below already carries the prompts verbatim, so a
    separate ``# Prompts`` block only duplicated them. ``summary_metadata`` lines
    are added to the metadata section. Idempotent: a message that already carries
    a summary (marked by its ``summary_model:`` metadata) is returned unchanged,
    so a redundant amend can never happen.
    """
    if not summary.strip() or "\nsummary_model:" in message:
        return message
    lines = message.splitlines()
    # Everything from the trace header on is kept; the prompt-led lead above it is replaced
    # by the summary. Anchoring on the header rather than the first blank line matters now
    # that a long prompt's continuation is its own paragraph — the blank line no longer
    # marks the end of the lead, and the old prompt text would have survived into the body.
    try:
        rest_start = next(index for index, line in enumerate(lines) if line.startswith(TRACE_HEADER))
    except StopIteration:
        try:
            rest_start = lines.index("") + 1
        except ValueError:
            rest_start = len(lines)
    rest = lines[rest_start:]

    # Read the fact back off the message being amended (the async path amends a commit built
    # elsewhere, so no caller has to thread the flag through): the summary-led subject must
    # carry the same mark the originally built subject did.
    new_lines = _summary_lead_lines(summary, interrupted="\ninterrupted: true" in message, repo_root=repo_root)
    new_lines.append("")
    new_lines.extend(rest)
    if summary_metadata:
        new_lines = _insert_before_version_line(new_lines, summary_metadata)
    return _finish(new_lines, repo_root)


def summary_metadata_lines(
    *, model: str | None, tokens_input: int = 0, tokens_output: int = 0, tokens_cache_read: int = 0
) -> list[str]:
    """Metadata recording what the summarization itself cost (issue #8).

    ``tokens_input`` is fresh input (uncached input + cache-creation), matching the main
    commit's token accounting; ``tokens_cache_read`` reports cache hits separately and is
    emitted only when non-zero."""
    lines = [f"summary_model: {model or 'unknown'}"]
    if tokens_input > 0:
        lines.append(f"summary_tokens_input: {tokens_input}")
    if tokens_cache_read > 0:
        lines.append(f"summary_tokens_cache_read: {tokens_cache_read}")
    if tokens_output > 0:
        lines.append(f"summary_tokens_output: {tokens_output}")
    return lines


def _is_summary_header(line: str) -> bool:
    """A line that is only the word "summary" plus non-letters — e.g. ``Summary``,
    ``Summary:``, ``## Summary``, ``**Summary**``. Models sometimes emit such a
    header as the first line; left alone it becomes a useless commit subject."""
    return re.sub(r"[^a-zA-Z]", "", line).lower() == "summary"


INTERRUPTED_SUBJECT_MARK = "(interrupted) "


def _interrupted_subject(subject: str, interrupted: bool) -> str:
    """Mark a cancelled turn's subject as such.

    The body note says the turn was interrupted, and the summarizer is given that note —
    but the subject is written by a model, and it still described the whole REQUEST:
    "Session created placeholder text files r1.txt through r10.txt" over a two-file diff,
    with an accurate body underneath. `git log --oneline` shows only the subject, so the
    one line most people read was the one that overclaimed. The mark is applied here, by
    aGiTrack, because accuracy about what happened cannot be delegated to the summarizer."""
    if not interrupted or subject.startswith(INTERRUPTED_SUBJECT_MARK):
        return subject
    return f"{INTERRUPTED_SUBJECT_MARK}{subject}"


def _summary_lead_lines(summary: str, *, interrupted: bool = False, repo_root: str | Path | None = None) -> list[str]:
    """Subject + leading body for a summarized message.

    Mirrors the prompt-led layout: the summary's first line is the subject
    (a truncated subject flows straight into its full text, no blank line),
    and the rest of the summary follows as the first paragraph of the body —
    there is no separate ``# Summary`` section. A leading bare "Summary" header
    line is skipped so the subject is never just the word "summary".
    """
    text_lines = _mask_secrets(summary, repo_root).strip().splitlines()
    first_index = next(
        (i for i, line in enumerate(text_lines) if line.strip() and not _is_summary_header(line)),
        None,
    )
    first_line = text_lines[first_index] if first_index is not None else DEFAULT_SUBJECT
    remainder = text_lines[first_index + 1 :] if first_index is not None else []
    while remainder and not remainder[0].strip():
        remainder.pop(0)

    subject, full = _subject_parts(first_line, width=MAX_SUBJECT_WIDTH - len(AGITRACK_SUBJECT_PREFIX))
    lines = [f"{AGITRACK_SUBJECT_PREFIX}{_interrupted_subject(subject, interrupted)}"]
    # Everything after the first sentence is BODY, separated by a blank line: git reads the
    # first paragraph as the subject, so a summary whose lead paragraph ran four sentences
    # became a four-sentence `git log --oneline` entry.
    paragraphs = [text for text in (full, "\n".join(remainder).rstrip()) if text]
    if paragraphs:
        lines.append("")
        lines.extend(_body_lines("\n\n".join(paragraphs)))
    return lines


def _insert_before_version_line(lines: list[str], extra: list[str]) -> list[str]:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("agitrack_version:"):
            return lines[:index] + list(extra) + lines[index:]
    return lines + list(extra)


TRACE_EVENT_ROLE = "event"
"""Trace role for something that HAPPENED rather than something someone said.

The harness wakes the agent when a task it backgrounded reports back, and the transcript
parser labels the resulting turn with a synthetic prompt (``BACKGROUND_PROMPT_LABELS`` in
transcripts/claude.py). Recording that label under the ``user`` role put words in the user's
mouth: every such commit's trace carried a ``## User`` / ``(background task completed)`` block
indistinguishable from something they typed — and that trace is the summarizer's sole input, so
the summary read the wake-up as a request. It is an event; it renders as one."""


def _trace_role_lines(item: dict, repo_root: str | Path | None = None) -> list[str]:
    """The rendered lines for one trace entry — its "## User"/"## Agent" role heading plus the
    masked, heading-nested body — or an EMPTY list when the entry has no textual content. A
    blank/whitespace entry (e.g. a garbled or empty proxy capture of a follow-up typed while the
    agent was busy) must never produce a bare "## User" heading with nothing under it.

    An :data:`TRACE_EVENT_ROLE` entry is NOT a speaker and gets no role heading: it renders as a
    note (the same blockquote form the lead-in notes use) naming the event, so a reader — and the
    summarizer — can see the agent continued on its own rather than answering anybody."""
    content = _nest_headings_under_role(_mask_secrets(item.get("content", ""), repo_root))
    if not content.strip():
        return []
    role = item.get("role", "").strip().lower()
    if role == TRACE_EVENT_ROLE:
        # Phrased generically over the label rather than mapping each known one, so a new
        # harness event kind renders correctly the day it appears instead of silently
        # reverting to "the user said this".
        note = (
            f"The agent was woken here by a background event {' '.join(content.split())}, not by "
            "a user message. What follows is its response to that event."
        )
        return [*_note_block(note), ""]
    label = "User" if role == "user" else "Agent"
    return [f"## {label}", "", *_body_lines(content), ""]


def render_interaction_trace(
    trace: list[dict],
    trace_turn_limit: int,
    *,
    interrupted: bool = False,
    changed_paths: list[str] | None = None,
    repo_root: str | Path | None = None,
) -> str:
    """The interaction-trace body exactly as it is appended to an aGiTrack commit:
    role headings plus masked, heading-nested content (same as the commit's
    ``# Interaction Trace`` section, without the header). This is the *sole* input
    given to the summarizer, so the summary reflects the committed trace and
    nothing else — no diff, no out-of-band context.

    ``interrupted`` carries the one fact the trace cannot show on its own: that the user
    cancelled the turn. Without it the summarizer reads an agent's "I'll do X now" as X
    having been done. ``changed_paths`` carries the second one — what the commit actually
    contains — because a cancelled turn's messages stop before its last edits (see
    ``_interrupted_changes_sentence``). Both are ignored unless the turn was interrupted."""
    lines: list[str] = list(_interrupted_note_lines(interrupted, changed_paths))
    for item in _limit_trace_turns(trace, trace_turn_limit):
        lines.extend(_trace_role_lines(item, repo_root))
    return "\n".join(lines).strip()


def _trace_and_metadata_lines(
    *,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agitrack_session_id: str,
    model: str | None,
    token_usage: dict[str, int | None] | None,
    trace_turn_limit: int,
    backend_version: str | None = None,
    reasoning_effort: str | None = None,
    conversation_anchor: str | None = None,
    session_name: str | None,
    covered_commits: list[str] | None,
    summary_metadata: list[str] | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
    compactions: int = 0,
    origin_event: dict | None = None,
    capabilities: dict[str, list[str]] | None = None,
    interrupted: bool = False,
    changed_paths: list[str] | None = None,
    repo_root: str | Path | None = None,
) -> list[str]:
    lines: list[str] = [TRACE_HEADER, ""]
    # The interruption leads every other note: it changes how the whole trace below
    # should be read (a request stated is not a request carried out).
    lines.extend(_interrupted_note_lines(interrupted, changed_paths))
    # Session-level events (a fork/copy this session began from, any context
    # compactions in these turns) lead the trace as a note, so the conversation log
    # itself shows when the context — and the token counts riding on it — changed.
    lines.extend(_session_event_note_lines(compactions=compactions, origin_event=origin_event))
    # When this commit accounts for commit(s) the agent made ITSELF (their hashes listed
    # in ``covered_commits`` below), lead the trace with a note explaining the relationship,
    # so a reader of the covering commit understands where those hashes came from and why
    # this commit's token counts span work already in history — the human-readable
    # counterpart to the ``covered_commits`` metadata line (issues #35/#58).
    lines.extend(_covered_commits_note_lines(covered_commits))
    for item in _limit_trace_turns(trace, trace_turn_limit):
        # Nest each message's own headings under its "## User"/"## Agent" role heading (so a
        # message's "# Title" can't outrank the role) and skip empty entries (see _trace_role_lines).
        lines.extend(_trace_role_lines(item, repo_root))

    lines.extend(
        [
            METADATA_HEADER,
            "commit_type: agent",
            # Machine-readable counterpart of the note above, so the dashboard and any
            # later reader can tell a cancelled turn from a completed one without
            # parsing prose. Emitted only when true: an ordinary commit is unchanged.
            *(["interrupted: true"] if interrupted else []),
            f"backend: {backend}",
            # The agent HARNESS version, beside the backend it names. The harness shapes a change
            # as much as the model does — it owns the tool set, the system prompt and the
            # editing style — and it updates itself far more often than the model changes.
            # Without it, a shift in how commits were produced is invisible in the history:
            # this repository's shell-to-editing-tool ratio tripled over six weeks of CLI
            # releases with the model and permission mode constant, and that could only be
            # established by re-reading raw transcripts that a later cleanup could have removed.
            *([f"backend_version: {backend_version}"] if backend_version else []),
            f"model: {model or 'unknown'}",
        ]
    )
    # The reasoning effort / thinking level the conversation ran at, only when the
    # backend transcript revealed it (see SessionTurn.reasoning_effort).
    if reasoning_effort:
        lines.append(f"reasoning_effort: {reasoning_effort}")
    # Capabilities beyond the backend's built-in toolset that these turns USED — MCP servers and
    # their tools, skills, sub-agents. A repo can hand the agent extra powers via MCP servers or
    # plugins, and those shape the change as much as the model does, so the provenance is
    # incomplete without them. Omitted entirely when a turn used none, so an ordinary commit's
    # metadata is unchanged.
    lines.extend(_capability_lines(capabilities))
    lines.extend(
        [
            f"session_name: {session_name or 'unknown'}",
            f"agitrack_session_id: {agitrack_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
        ]
    )
    # A pointer to the exact place in the backend conversation this commit
    # accounts for: the backend message id of the last turn it covers. Combined
    # with backend/backend_session_id it pinpoints where in the transcript to
    # resume reading, so a shared or locally-kept session can be backtracked to
    # the moment these changes were made. Recorded only when the transcript
    # exposes a message id for the turn.
    if conversation_anchor:
        lines.append(f"conversation_anchor: {conversation_anchor}")
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
    lines.extend(_session_event_metadata_lines(compactions=compactions, origin_event=origin_event))
    if summary_metadata:
        lines.extend(summary_metadata)
    lines.append(f"system: {_system_info()}")
    lines.append(f"agitrack_version: {__version__}")
    return lines


# A commit trailer must stay readable: a turn that fans out over many tools would otherwise
# bury the rest of the metadata. Beyond this many entries the list is truncated with a
# "+N more" marker, so the field still reports the true breadth without dominating the message.
_CAPABILITY_LIST_LIMIT = 12


def _capability_line(key: str, values: list[str]) -> str:
    shown = values[:_CAPABILITY_LIST_LIMIT]
    extra = len(values) - len(shown)
    joined = " ".join(shown) + (f" +{extra} more" if extra > 0 else "")
    return f"{key}: {joined}"


def _capability_lines(capabilities: dict[str, list[str]] | None) -> list[str]:
    """One ``key: a b c`` line per non-empty capability list, in a fixed field order."""
    if not capabilities:
        return []
    lines: list[str] = []
    for key in CAPABILITY_FIELDS:
        values = [str(v).strip() for v in (capabilities.get(key) or []) if str(v).strip()]
        if values:
            lines.append(_capability_line(key, values))
    return lines


# How many paths the interrupted-turn note spells out before it starts counting. Long
# enough to be useful, short enough that a hundred-file turn does not bury the trace.
_INTERRUPTED_NOTE_PATH_LIMIT = 12


def _interrupted_changes_sentence(changed_paths: list[str] | None) -> str:
    """The one thing the trace of an interrupted turn cannot be trusted about: what landed.

    The trace holds the agent's MESSAGES, and a cancelled turn's last message is typically
    its opening "I'll create the ten files one at a time" — the tool calls that followed are
    not in it. Reading that trace under the note above, the summarizer concluded the opposite
    of the truth: `<aGiTrack> (interrupted) Requested creation of f1.txt–f10.txt was
    interrupted before any writes`, on a commit whose own diff adds f1.txt through f8.txt.
    A subject that contradicts its diff is worse than a vague one, and neither the human nor
    the summarizer can tell without being told, so the file list goes in the note."""
    if not changed_paths:
        return ""
    shown = changed_paths[:_INTERRUPTED_NOTE_PATH_LIMIT]
    listed = ", ".join(shown)
    if len(changed_paths) > len(shown):
        listed += f", and {len(changed_paths) - len(shown)} more"
    return (
        " The agent's messages below stop where it was cancelled and may not mention its last"
        f" edits, so take the changes from this commit itself: it changes {listed}."
    )


def _interrupted_note_lines(interrupted: bool, changed_paths: list[str] | None = None) -> list[str]:
    """Lead-in note for a turn the user CANCELLED (Esc) before the agent finished.

    Such a turn still commits — the partial edits are real work and dropping them would
    strand them — but nothing in the message used to say so, and the reader has no other
    way to tell: an agent that answers "I'll create the ten files now" and is stopped after
    two leaves a trace that reads exactly like a completed turn. Worse, that trace is the
    summarizer's SOLE input, so the generated subject asserted the whole request as done
    ("Ten sequential text files r1.txt through r10.txt were created") over a two-file diff.
    The note states the fact for both readers at once: the human, and the summarizer."""
    if not interrupted:
        return []
    note = (
        "The user interrupted this turn before the agent finished it, so the request as "
        "stated in the trace below was NOT completed."
    ) + _interrupted_changes_sentence(changed_paths)
    return [*_note_block(_mask_secrets(note)), ""]


def _session_event_note_lines(*, compactions: int, origin_event: dict | None) -> list[str]:
    """Lead-in note for the interaction trace describing fork/copy lineage and any
    context compactions — the human-readable counterpart to the metadata lines."""
    notes: list[str] = []
    if origin_event:
        notes.append(_origin_event_sentence(origin_event))
    if isinstance(compactions, int) and compactions > 0:
        times = "once" if compactions == 1 else f"{compactions} times"
        notes.append(
            f"The conversation context was compacted {times} here — earlier history "
            "was summarized to fit the model's window, so the token counts below run "
            "against a reset (smaller) context."
        )
    lines: list[str] = []
    for note in notes:
        lines.extend(_note_block(_mask_secrets(note)))
        lines.append("")
    return lines


def _covered_commits_note_lines(covered_commits: list[str] | None) -> list[str]:
    """Lead-in note for a commit that COVERS commit(s) the agent made itself — the
    human-readable counterpart to the ``covered_commits`` metadata line. Empty when the
    commit covers nothing (an ordinary aGiTrack commit), so a normal commit is unchanged.

    The note applies uniformly wherever a commit accounts for the agent's own commits: the
    merge-shaped cover commit worktree mode places on top of them (#35), and the folded
    turn body that attributes a commit the agent made mid-turn in a no-worktree mode. In
    both cases the covered commits keep their own hashes (never rewritten, #58) and this
    commit carries the trace and token usage for the work that produced them."""
    if not covered_commits:
        return []
    plural = "s" if len(covered_commits) > 1 else ""
    note = (
        f"This commit accounts for {len(covered_commits)} commit{plural} the agent made "
        "itself — their hashes are listed under `covered_commits` in the metadata below. "
        "Those commits keep their own hashes and are never rewritten; the interaction "
        "trace and the token usage for the work that produced them are recorded here, so "
        "this commit's token counts span changes already present in history."
    )
    return [*_note_block(_mask_secrets(note)), ""]


def _origin_event_sentence(origin_event: dict) -> str:
    kind = origin_event.get("kind")
    source_name = origin_event.get("source_name") or origin_event.get("source") or "another session"
    collaborator = origin_event.get("collaborator")
    if kind == "copy":
        whose = f"{collaborator}'s shared session" if collaborator else "a shared session"
        return (
            f"This session was copied from {whose} '{source_name}'. It resumes that "
            "conversation, so its starting context — and the token usage inherited with "
            "it — originated there, not in this session."
        )
    return (
        f"This session was forked from '{source_name}'. It resumes a copy of that "
        "conversation, so its starting context and the token usage inherited with it "
        "came from the original session."
    )


def _note_block(text: str) -> list[str]:
    """Wrap *text* as a Markdown blockquote (``>`` on every line) within the body width.
    The note is plain prose (no code/indentation to preserve), so collapse whitespace and
    drop the wrap padding for clean, evenly-filled quote lines."""
    wrapped = wrap(" ".join(text.split()), width=MAX_BODY_WIDTH - 2) or [""]
    return [f"> {line}" for line in wrapped]


def _session_event_metadata_lines(*, compactions: int, origin_event: dict | None) -> list[str]:
    """Machine-readable metadata for the session events: a compaction count and the
    fork/copy lineage, so the dashboard can flag turns whose token counts span a
    context reset or were inherited from another conversation."""
    lines: list[str] = []
    if isinstance(compactions, int) and compactions > 0:
        lines.append(f"context_compactions: {compactions}")
    if origin_event:
        kind = origin_event.get("kind") or "fork"
        source = origin_event.get("source") or "unknown"
        source_name = origin_event.get("source_name")
        detail = f"{source} ({source_name})" if source_name else str(source)
        key = "copied_from" if kind == "copy" else "forked_from"
        lines.append(f"{key}: {detail}")
        collaborator = origin_event.get("collaborator")
        if kind == "copy" and collaborator:
            lines.append(f"copied_from_contributor: {collaborator}")
    return lines


AGENT_MERGE_SUBJECT_PREFIX = "<aGiTrack-merge> "


def build_agent_merge_message(
    *,
    session_name: str | None,
    base_branch: str,
    source_branch: str,
    agitrack_session_id: str,
    backend: str,
    backend_session_id: str | None = None,
    conflicting_commits: str | None = None,
    repo_root: str | Path | None = None,
) -> str:
    """Commit message for a merge whose conflicts an agent resolved."""
    subject = f"{AGENT_MERGE_SUBJECT_PREFIX}integrate {session_name or source_branch} into {base_branch}"
    lines = [_subject_text(subject, width=MAX_SUBJECT_WIDTH), ""]
    if conflicting_commits and conflicting_commits.strip():
        lines.extend(["# Resolved Against Base Commits", ""])
        lines.extend(_body_lines(_mask_secrets(conflicting_commits, repo_root)))
        lines.append("")
    lines.extend(
        [
            "# aGiTrack Metadata",
            "commit_type: agent-merge",
            f"backend: {backend}",
            f"session_name: {session_name or 'unknown'}",
            f"source_branch: {source_branch}",
            f"base_branch: {base_branch}",
            f"agitrack_session_id: {agitrack_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    return _finish(lines, repo_root)


def build_user_commit_message(
    *,
    message: str | None,
    agitrack_session_id: str,
    repo_root: str | Path | None = None,
) -> str:
    user_message = message.strip() if message else ""
    if not user_message:
        raise ValueError("User commit message is required")
    subject, full_subject = _subject_parts(_mask_secrets(user_message, repo_root), width=MAX_SUBJECT_WIDTH)
    lines = [subject]
    if full_subject:
        # Blank line first, so git's subject is the first sentence and not the whole
        # paragraph (see build_agent_commit_message).
        lines.append("")
        lines.extend(_body_lines(full_subject))
    lines.append("")
    lines.extend(
        [
            "# aGiTrack Metadata",
            "commit_type: user",
            "backend: agit",
            f"agitrack_session_id: {agitrack_session_id}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    return _finish(lines, repo_root)


def restore_own_markers(body: str) -> str:
    """Put aGiTrack's own name back into a latent body that an over-eager redaction took it out of.

    A repository named ``agitrack`` in the registry used to be redacted like any other foreign
    project, which rewrote the two markers aGiTrack stamps on its OWN messages — the
    ``<aGiTrack> `` subject prefix and the ``# aGiTrack Metadata`` header — into the mask. The
    name is exempt now (:data:`agitrack.commits.foreign.OWN_NAME`), but the turns recorded while
    it was not are still sitting on the latent refs, and to every reader that keys on the header
    they look like commits holding no agent work at all: the fold builders below drop them, so
    those turns would never reach a commit and their trace and tokens would be lost outright.

    Deliberately narrow — only the two STRUCTURAL forms, anchored as aGiTrack writes them. A bare
    ``[OTHER_REPO]`` in the prose of an interaction trace is a real redaction of a real other
    project, and unmasking that would undo the very thing the redaction is for.
    """
    if FOREIGN_REPO_MASK not in body:
        return body
    return body.replace(f"# {FOREIGN_REPO_MASK} Metadata", METADATA_HEADER).replace(
        f"<{FOREIGN_REPO_MASK}> ", AGITRACK_SUBJECT_PREFIX
    )


def build_manual_squash_trailer(
    *, agitrack_session_id: str, latent_bodies: list[str], repo_root: str | Path | None = None
) -> str:
    """The tracking text a manual-commit-mode git hook appends to the user's own commit
    message, so the single commit carries the whole session's interaction record.

    It is a *squashed* message: a leading ``commit_type: user`` metadata block followed by the
    full message body of each pending latent turn in **chronological (oldest-first) order** — the
    same order a normal squash merge lists its commits, so a manual-mode commit reads like any
    other squash. Each latent body already carries its own ``# Interaction Trace`` +
    ``# aGiTrack Metadata``, so the concatenation has one metadata block per turn — exactly the
    aggregate shape the dashboard already parses into one constituent per turn (see
    ``agitrack.metrics.collect._parse_constituents``), which sums the tokens and classifies the
    commit as agent-tracked when any turn is present. No new ``commit_type`` is needed. (The
    dashboard *displays* the constituents newest-first, but the message itself stays chronological
    — the reorder is display-only, in ``agitrack.metrics.web``.)

    **No footprint on a non-AI commit.** When there are no pending agent turns — i.e. the commit
    holds only the user's own hand-written code, with no AI work to track — this returns the empty
    string, so the ``prepare-commit-msg`` hook appends nothing and the commit is left completely
    untouched. aGiTrack never attributes (or covers) a commit that contains no AI-written code.

    ``latent_bodies`` are the commit-message bodies read from the latent ref
    (``refs/agitrack/manual/<id>``), the durable source of truth, so the trailer is always
    reproducible after a restart. Returns a string ending in a single newline, or ``""`` when
    there are no pending turns."""
    # Chronological (oldest-first), like any squash merge; keep only real turn bodies.
    turn_blocks = [
        text
        for text in (restore_own_markers((body or "").strip()) for body in latent_bodies)
        if text and METADATA_HEADER in text
    ]
    if not turn_blocks:
        return ""  # no AI work in this commit ⇒ no trailer, no attribution, no footprint
    header = "\n".join(
        [
            METADATA_HEADER,
            "commit_type: user",
            "backend: agit",
            f"agitrack_session_id: {agitrack_session_id}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    return redact_foreign_repos("\n\n".join([header, *turn_blocks]).rstrip() + "\n", repo_root)


def build_in_flight_trailer(
    *,
    agitrack_session_id: str,
    backend: str,
    backend_session_id: str | None,
    model: str | None,
    prompt: str | None = None,
    repo_root: str | Path | None = None,
) -> str:
    """The tracking text for a commit the AGENT makes ITSELF, in the middle of a turn.

    A turn becomes a pending latent turn only once it COMPLETES, so an agent that runs
    ``git commit`` while it is still working leaves
    :func:`build_manual_squash_trailer` with nothing to fold — the commit lands with no
    metadata at all and its lines are attributed to nobody. This block attributes it:
    which agent, in which conversation, working on what.

    Deliberately carries **no token counts**. The turn is still running; when it finishes it
    is recorded as a normal latent turn and that commit carries the full interaction trace
    and the usage. Counting tokens here too would double-count them, so ``in_flight: true``
    marks the block as the partial record it is — the same turn's complete record follows in
    a later commit.

    The caller is responsible for only asking for this when the agent has actually changed
    the working tree; a commit with no AI work in it still gets no trailer at all.
    """
    lines = [METADATA_HEADER, "commit_type: agent", IN_FLIGHT_MARKER, f"backend: {backend}"]
    lines.append(f"model: {model or 'unknown'}")
    lines.extend(
        [
            f"agitrack_session_id: {agitrack_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    note = _note_block(
        _mask_secrets(
            "The agent committed this itself while its turn was still running, so this records "
            "who made the change; the turn's full interaction trace and token usage land in a "
            "later commit."
        )
    )
    # The note leads the interaction trace as a blockquote — INSIDE the section, above the
    # running prompt — the same placement as the session-event / covered-commits lead-in notes,
    # not floating above the header. The section header is always present so the note has a home
    # even when the running turn has no prompt text yet.
    body: list[str] = [TRACE_HEADER, "", *note, ""]
    if prompt and prompt.strip():
        # _trace_role_lines masks and heading-nests the prompt, exactly as a real trace does.
        body.extend(_trace_role_lines({"role": "user", "content": prompt}, repo_root))
    body.extend(lines)
    return redact_foreign_repos("\n".join(body).strip() + "\n", repo_root)


def build_pending_trailer(
    *,
    agitrack_session_id: str,
    latent_bodies: list[str],
    in_flight: dict | None = None,
    repo_root: str | Path | None = None,
) -> str:
    """The trailer the ``prepare-commit-msg`` hook folds into the commit being made — the one
    entry point every mode renders through, so they all attribute a commit identically.

    Completed turns win: they carry the full trace and the token usage. The in-flight fallback
    applies only when NONE are pending, which is exactly the case that used to lose attribution
    entirely — the agent ran ``git commit`` itself partway through a turn, and a turn does not
    become a pending latent turn until it completes.

    ``in_flight`` must be None whenever no agent turn is running OR the agent has not changed
    the working tree, which preserves the promise that a purely human commit is left untouched.
    """
    trailer = build_manual_squash_trailer(
        agitrack_session_id=agitrack_session_id, latent_bodies=latent_bodies, repo_root=repo_root
    )
    if trailer or not in_flight:
        return trailer
    return build_in_flight_trailer(agitrack_session_id=agitrack_session_id, repo_root=repo_root, **in_flight)


def build_auto_fold_message(latent_bodies: list[str], *, repo_root: str | Path | None = None) -> str:
    """Commit message for an **AUTO-mode fold**: aGiTrack commits the pending agent turn(s) *itself*,
    so the message must read like a clean AGENT commit — NOT the manual "squash into a user commit"
    format (which leads with a generic ``<aGiTrack> commit agent turns`` subject and a spurious
    ``commit_type: user`` block for work no user committed).

    One pending turn ⇒ its own single-agent-commit body **verbatim**: the subject is the turn's
    prompt (or, when summarization has run, the LLM summary that ``pending_bodies`` folds into the
    body — so the subject and lead paragraph are the summary), with one ``# aGiTrack Metadata``
    block. Several turns ⇒ a squash led by the newest turn's subject with each turn's full body,
    still with no ``commit_type: user`` header. Returns ``""`` when there are no real turns.

    ``latent_bodies`` come from ``ManualCommitTracker.pending_bodies`` (summaries already applied),
    the durable source of truth, so the message is reproducible and carries the folded metadata the
    ``prepare-commit-msg`` hook's idempotency check looks for."""
    bodies = [
        text
        for text in (restore_own_markers((body or "").strip()) for body in latent_bodies)
        if text and METADATA_HEADER in text
    ]
    if not bodies:
        return ""
    if len(bodies) == 1:
        # A clean single agent commit (summary leads when present).
        return redact_foreign_repos(bodies[0].rstrip() + "\n", repo_root)
    # Several turns folded at once: lead with the newest turn's subject, then every turn's full body
    # (oldest-first, like any squash) — each already carries its own commit_type: agent metadata.
    newest_subject = bodies[-1].splitlines()[0].strip() or f"{AGITRACK_SUBJECT_PREFIX}agent turns"
    return redact_foreign_repos(newest_subject + "\n\n" + "\n\n".join(bodies).rstrip() + "\n", repo_root)


def _subject_text(text: str, *, width: int) -> str:
    return _subject_parts(text, width=width)[0]


def _subject_parts(text: str, *, width: int) -> tuple[str, str | None]:
    # The subject is the first sentence: text up to the first period that ends one
    # (followed by whitespace or the end of the string). The remainder, if any, flows
    # onto the next line (the body). aGiTrack does NOT truncate with "…" — a long
    # subject is left intact and Git shortens its DISPLAY when needed. ``width`` is
    # accepted for caller compatibility but no longer used to truncate.
    one_line = " ".join(text.strip().split()) or DEFAULT_SUBJECT
    match = re.search(r"\.(?=\s|$)", one_line)
    if match is None:
        return one_line, None
    end = match.start() + 1  # include the period that ends the first sentence
    subject = one_line[:end].rstrip()
    remainder = one_line[end:].strip()
    return subject, remainder or None


def _body_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        lines.extend(wrap(raw_line, width=MAX_BODY_WIDTH, replace_whitespace=False, drop_whitespace=False) or [""])
    return lines


def _nest_headings_under_role(content: str) -> str:
    """Push every Markdown heading inside one trace message down by
    :data:`TRACE_HEADING_SHIFT` levels: ``#`` becomes ``###``, ``##`` becomes ``####``.

    A uniform shift, so the message's own hierarchy survives exactly as its author wrote it
    and nothing it wrote can outrank the ``## User`` / ``## Agent`` role heading it belongs
    to (the shallowest possible result is level 3). Two levels rather than one keeps the
    trace's scaffolding distinguishable at a glance in plain-text ``git log``.

    Fenced code blocks are skipped, since a leading ``#`` there is content being QUOTED — a
    shell comment, or a Markdown example — and rewriting it would falsify what the message
    showed. Levels are capped at the Markdown maximum of 6."""
    lines = content.splitlines()
    in_fence = False
    fence_marker = ""
    headings: list[tuple[int, int, str]] = []  # (line index, level, trailing text)
    for index, line in enumerate(lines):
        fence = CODE_FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            continue
        if in_fence:
            continue
        heading = ATX_HEADING_RE.match(line)
        if heading:
            headings.append((index, len(heading.group(1)), heading.group(2)))
    if not headings:
        return content
    for index, level, rest in headings:
        lines[index] = "#" * min(6, level + TRACE_HEADING_SHIFT) + rest
    return "\n".join(lines)


def _limit_trace_turns(trace: list[dict], turn_limit: int) -> list[dict]:
    """Keep the most recent ``turn_limit`` turns of ``trace``.

    A turn is an EXCHANGE — something said to the agent, and the agent answering it — so it is
    the agent's reply that closes one and makes the next thing said a new turn. Consecutive
    messages with no reply between them are one turn however many there are: the user can keep
    typing while the agent works, and none of that is a new exchange.

    Counting each `## User` block instead cut the trace INSIDE a turn. Measured on a real
    session: one turn carried eleven messages sent while the agent worked, so a limit of 5 kept
    the last three and dropped the opening prompt with eight follow-ups — and because that turn's
    work was committed there, the words that asked for it survived in no commit at all.

    PROMPTS AND WAKE-UPS ARE BUDGETED SEPARATELY, and this is the whole point of the function:

        the unit of the limit is a user-agent PAIR. A turn nobody asked for — the harness woke
        the agent with a background event (:data:`TRACE_EVENT_ROLE`) and it answered — is not a
        pair, and can never push a prompt out of the trace.

    Both kinds are still bounded, each to `turn_limit` of its own, because they run away for
    different reasons and neither bound can do the other's job:

      * Counting wake-ups as turns loses the prompt. A real commit here traced seven turns —
        an opening instruction, five wake-ups from timers the agent had queued while waiting on
        CI, and a second instruction. Five one-sentence replies saying "still waiting" spent the
        entire budget of 5, so the instruction that motivated the commit was the one thing
        evicted, while five notes about a stale timer were kept.
      * Not counting them at all leaves the trace unbounded: a monitor that ticks for an hour
        wakes the agent every few minutes, those turns are deferred rather than committed (they
        change nothing), and the pending trace keeps growing. A wake-up may evict an OLDER
        wake-up — that is the one thing a burst of them should cost.

    A wake-up older than the oldest surviving prompt goes with it: an event note is context for
    the exchange it sits in, and reading one stranded above the first prompt tells nobody
    anything. `starts_turn=False`, recorded for a message known to have been queued into a turn
    already under way, keeps such a message out of the count even where a reply happens to
    precede it; it is absent on entries written by older installs, so its default preserves
    their meaning."""
    limit = turn_limit if isinstance(turn_limit, int) and turn_limit > 0 else 5
    starts: list[tuple[int, str]] = []
    answered = True  # nothing is in flight yet, so the first thing said opens a turn
    for index, item in enumerate(trace):
        role = str(item.get("role", "")).strip().lower()
        if role not in ("user", TRACE_EVENT_ROLE):
            answered = True  # the agent replied: whatever is said next begins a new exchange
            continue
        if answered and item.get("starts_turn", True) is not False:
            starts.append((index, role))
        answered = False
    prompts = [index for index, role in starts if role == "user"]
    events = [index for index, role in starts if role != "user"]
    if len(prompts) <= limit and len(events) <= limit:
        return trace
    kept_prompts = prompts[-limit:]
    kept_events = events[-limit:]
    if kept_prompts:
        # Never strand a wake-up above the oldest prompt still in the trace.
        kept_events = [index for index in kept_events if index > kept_prompts[0]]
    kept = set(kept_prompts) | set(kept_events)
    opens = {index for index, _ in starts}
    limited: list[dict] = []
    keeping = False  # entries ahead of the first turn belong to none of them
    for index, item in enumerate(trace):
        if index in opens:
            keeping = index in kept
        if keeping:
            limited.append(item)
    return limited


def mask_paths(text: str) -> str:
    """Replace every ABSOLUTE filesystem path with :data:`PATH_MASK`, leaving URLs and
    relative paths intact. See the PATH_RE block for what counts as a path and why."""

    def replace(match: re.Match[str]) -> str:
        if match.group("url"):
            return match.group(0)  # a URL is not a filesystem path
        path = match.group(0)
        trailing = ""
        while path and path[-1] in _PATH_TRAILING_PUNCT:
            trailing = path[-1] + trailing
            path = path[:-1]
        if not path:
            return match.group(0)  # punctuation only — nothing path-like to mask
        return PATH_MASK + trailing

    return PATH_RE.sub(replace, text)


def _mask_secrets(text: object, repo_root: str | Path | None = None) -> str:
    """Make arbitrary agent/user text safe to publish in a commit message: strip terminal
    escapes and control characters, redact secrets, mask absolute filesystem paths, and — when
    the caller says which repository this is — take out the names of the user's OTHER projects.

    The foreign-repo pass happens HERE, on the raw text, and not only in :func:`_finish` on the
    finished message, because the message is hard-wrapped to 72 columns in between: a real commit
    came out carrying ``acme-\nbilling/lib/retry.py``, split at the hyphen by ``textwrap`` and so
    invisible to any pattern working on the wrapped result. Redaction has to see the text as the
    agent wrote it. `_finish` stays as the backstop for what does not pass through here."""
    value = str(text or "")
    value = MOUSE_REPORT_RE.sub("", value)
    value = MOUSE_REPORT_FRAGMENT_RE.sub("", value)
    value = ANSI_SEQUENCE_RE.sub("", value)
    value = CONTROL_CHAR_RE.sub("", value)
    value = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET_MASK}", value)
    for pattern in SECRET_TOKEN_RES:
        value = pattern.sub(SECRET_MASK, value)
    # Path masking must not re-scan the "[REDACTED]" markers the steps above inserted; the
    # foreign-repo pass runs last for the same reason, and because an absolute path into another
    # project is already gone by then.
    return redact_foreign_repos(mask_paths(value), repo_root)


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
