from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agit.backends.base import TokenUsage
from agit.session import ExportedSession, SessionTurn, turns_after

__all__ = [
    "ExportedSession",
    "SessionTurn",
    "turns_after",
    "latest_session_id",
    "session_belongs_to_repo",
    "export_session",
    "parse_rows",
]

# User messages whose text is purely a slash-command/tool artifact are not real
# prompts and should be excluded from the interaction trace.
_COMMAND_TAGS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook>",
)


def _projects_root() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "projects"


def _encode_repo(repo: Path) -> str:
    # Claude Code names a project directory by replacing every non-alphanumeric
    # character of the absolute working directory with a dash.
    return re.sub(r"[^a-zA-Z0-9]", "-", str(repo.resolve()))


def _project_dir(repo: Path) -> Path:
    return _projects_root() / _encode_repo(repo)


def _session_path(repo: Path, session_id: str) -> Path:
    return _project_dir(repo) / f"{session_id}.jsonl"


def latest_session_id(repo: Path) -> str | None:
    project_dir = _project_dir(repo)
    if not project_dir.is_dir():
        return None
    transcripts = [path for path in project_dir.glob("*.jsonl") if path.is_file()]
    if not transcripts:
        return None
    latest = max(transcripts, key=lambda path: path.stat().st_mtime)
    return latest.stem


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    return _session_path(repo, session_id).is_file()


def export_session(repo: Path, session_id: str) -> ExportedSession | None:
    path = _session_path(repo, session_id)
    if not path.is_file():
        return None
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return parse_rows(session_id, rows)


def parse_rows(session_id: str, rows: list[dict]) -> ExportedSession:
    turns: list[SessionTurn] = []
    current: dict | None = None
    model: str | None = None
    updated: int | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            turns.append(_finalize_turn(current))
            current = None

    for row in rows:
        row_type = row.get("type")
        if row_type == "user":
            prompt = _user_prompt(row)
            if prompt is None:
                continue
            flush()
            current = {
                "user_id": str(row.get("uuid") or ""),
                "prompt": prompt,
                "final": "",
                "assistant_id": "",
                "model": model,
                "tokens": TokenUsage(),
            }
        elif row_type == "assistant" and current is not None and not row.get("isSidechain"):
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            current["tokens"].add(_message_tokens(message.get("usage")))
            message_model = message.get("model")
            if isinstance(message_model, str) and message_model:
                current["model"] = message_model
                model = message_model
            text = _assistant_text(message)
            if text:
                current["final"] = text
                current["assistant_id"] = str(message.get("id") or "")
    flush()
    return ExportedSession(session_id=session_id, model=model, updated=updated, turns=turns)


def _finalize_turn(turn: dict) -> SessionTurn:
    return SessionTurn(
        user_message_id=turn["user_id"],
        assistant_message_id=turn["assistant_id"],
        user_prompt=turn["prompt"],
        final_response=turn["final"],
        tokens=turn["tokens"],
        model=turn["model"],
    )


def _user_prompt(row: dict) -> str | None:
    if row.get("isMeta") or row.get("isSidechain"):
        return None
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        if not parts:
            return None  # tool_result-only messages are not user prompts
        text = "".join(parts).strip()
    else:
        return None
    if not text or text.startswith(_COMMAND_TAGS):
        return None
    return text


def _assistant_text(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip()
    ]
    return "".join(texts).strip()


def _message_tokens(usage: object) -> TokenUsage:
    if not isinstance(usage, dict):
        return TokenUsage()
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    return TokenUsage(
        context=(input_tokens + cache_read + cache_write) or None,
        total=output_tokens,
        input=input_tokens,
        output=output_tokens,
        reasoning=0,
        cache_read=cache_read,
        cache_write=cache_write,
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0
