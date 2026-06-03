from __future__ import annotations

import json
import os
import pty
import subprocess
import time
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
    "parse_exported_session",
    "looks_like_event_blob",
]


def latest_session_id(repo: Path) -> str | None:
    _debug(repo, "opencode session list starting")
    process = subprocess.run(
        ["opencode", "session", "list", "--format", "json", "--max-count", "10"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    _debug(repo, f"opencode session list finished returncode={process.returncode} stdout_bytes={len(process.stdout)} stderr_bytes={len(process.stderr)}")
    if process.returncode != 0:
        return None
    try:
        sessions = json.loads(process.stdout)
    except json.JSONDecodeError:
        return None
    repo = repo.resolve()
    matching = [session for session in sessions if _same_repo(session.get("directory"), repo) and session.get("id")]
    candidates = matching or [session for session in sessions if session.get("id")]
    if not candidates:
        return None
    latest = max(candidates, key=lambda session: session.get("updated") or session.get("created") or 0)
    return str(latest["id"])


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    _debug(repo, f"opencode session belongs check starting session_id={session_id}")
    process = subprocess.run(
        ["opencode", "session", "list", "--format", "json", "--max-count", "50"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    _debug(repo, f"opencode session belongs check finished session_id={session_id} returncode={process.returncode}")
    if process.returncode != 0:
        return False
    try:
        sessions = json.loads(process.stdout)
    except json.JSONDecodeError:
        return False
    resolved = repo.resolve()
    return any(session.get("id") == session_id and _same_repo(session.get("directory"), resolved) for session in sessions)


def _same_repo(directory: object, repo: Path) -> bool:
    if not isinstance(directory, str) or not directory:
        return False
    try:
        return Path(directory).resolve() == repo
    except OSError:
        return directory == str(repo)


def export_session(repo: Path, session_id: str) -> ExportedSession | None:
    _debug(repo, f"opencode export starting session_id={session_id}")
    output, returncode = _run_export_pty(repo, session_id)
    _debug(repo, f"opencode export finished session_id={session_id} returncode={returncode} output_bytes={len(output.encode(errors='replace'))}")
    if returncode != 0:
        return None
    json_text = _extract_json_object(output)
    if not json_text:
        return None
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return parse_exported_session(data)


def _debug(repo: Path, message: str) -> None:
    if os.environ.get("AGIT_DEBUG_PROXY", "").strip().lower() not in {"1", "true", "yes"}:
        return
    try:
        path = repo / ".agit" / "proxy-debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _run_export_pty(repo: Path, session_id: str) -> tuple[str, int]:
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(repo)
        os.execvp("opencode", ["opencode", "export", session_id])

    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    os.close(fd)
    _done, status = os.waitpid(pid, 0)
    return b"".join(chunks).decode(errors="replace"), os.waitstatus_to_exitcode(status)


def parse_exported_session(data: dict) -> ExportedSession:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    session_id = str(info.get("id") or "")
    updated = (info.get("time") or {}).get("updated") if isinstance(info.get("time"), dict) else None
    model = _model_name(info)
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    current_user: dict | None = None
    assistant_group: list[dict] = []
    turns: list[SessionTurn] = []

    def flush() -> None:
        if current_user is None or not assistant_group:
            return
        turn = _build_turn(current_user, assistant_group, model)
        if turn:
            turns.append(turn)

    for message in messages:
        msg_info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = msg_info.get("role")
        if role == "user":
            flush()
            current_user = message
            assistant_group = []
        elif role == "assistant" and current_user is not None:
            assistant_group.append(message)
    flush()
    return ExportedSession(session_id=session_id, model=model, updated=updated, turns=turns)


def _build_turn(user_message: dict, assistants: list[dict], session_model: str | None) -> SessionTurn | None:
    user_info = user_message.get("info") if isinstance(user_message.get("info"), dict) else {}
    user_id = str(user_info.get("id") or "")
    if not user_id:
        return None

    final_response = ""
    final_assistant: dict | None = None
    tokens = TokenUsage()
    model = session_model
    last_assistant = assistants[-1] if assistants else None
    for assistant in assistants:
        assistant_info = assistant.get("info") if isinstance(assistant.get("info"), dict) else {}
        tokens.add(_tokens(assistant_info, assistant.get("parts")))
        model = _model_name(assistant_info) or model
        response = _final_response(assistant.get("parts"), finish=assistant_info.get("finish"))
        if response:
            final_response = response
            final_assistant = assistant

    final_info = (final_assistant or last_assistant or {}).get("info", {})
    assistant_id = str(final_info.get("id") or "")
    return SessionTurn(
        user_message_id=user_id,
        assistant_message_id=assistant_id,
        user_prompt=_parts_text(user_message.get("parts")),
        final_response=final_response,
        tokens=tokens,
        model=model,
    )


def _extract_json_object(output: str) -> str | None:
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return output[start : end + 1]


def _parts_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            text = part["text"]
            if _looks_like_event_blob(text):
                continue
            texts.append(text)
    return "".join(texts).strip()


def _final_response(parts: object, *, finish: object = None) -> str:
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
            continue
        metadata = part.get("metadata")
        phase = _find_value(metadata, {"phase"}) if isinstance(metadata, dict) else None
        if phase == "final_answer" or (finish == "stop" and part.get("type") == "text"):
            text = part["text"]
            texts.append(_final_text_from_event_blob(text) if _looks_like_event_blob(text) else text)
    return "".join(texts).strip()


def looks_like_event_blob(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    event_lines = 0
    for line in lines[:5]:
        if line.startswith("{") and '"type"' in line and ('"sessionID"' in line or '"part"' in line):
            event_lines += 1
    return event_lines >= min(len(lines), 2)


_looks_like_event_blob = looks_like_event_blob


def _final_text_from_event_blob(text: str) -> str:
    final_parts: list[str] = []
    fallback_parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_text = part.get("text") if isinstance(part.get("text"), str) else event.get("text")
        if not isinstance(part_text, str) or not part_text.strip():
            continue
        metadata = part.get("metadata")
        phase = _find_value(metadata, {"phase"}) if isinstance(metadata, dict) else None
        if phase == "final_answer" or str(event.get("type", "")).lower() in {"final", "complete", "done"}:
            final_parts.append(part_text)
        elif str(event.get("type", "")).lower() == "text" or str(part.get("type", "")).lower() == "text":
            fallback_parts.append(part_text)
    return "".join(final_parts or fallback_parts).strip()


def _tokens(info: dict, parts: object) -> TokenUsage:
    usage = TokenUsage()
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                usage.add(_token_usage(part.get("tokens")))
    return usage if usage.total else _token_usage(info.get("tokens"))


def _token_usage(tokens: object) -> TokenUsage:
    if not isinstance(tokens, dict):
        return TokenUsage()
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    input_tokens = _int(tokens.get("input"))
    output_tokens = _int(tokens.get("output"))
    reasoning_tokens = _int(tokens.get("reasoning"))
    return TokenUsage(
        context=input_tokens or None,
        total=output_tokens + reasoning_tokens,
        input=input_tokens,
        output=output_tokens,
        reasoning=reasoning_tokens,
        cache_read=_int(cache.get("read")),
        cache_write=_int(cache.get("write")),
    )


def _model_name(info: dict) -> str | None:
    model = info.get("model")
    if isinstance(model, dict):
        provider = model.get("providerID")
        model_id = model.get("modelID") or model.get("id")
        if provider and model_id:
            return f"{provider}/{model_id}"
        return str(model_id) if model_id else None
    provider = info.get("providerID")
    model_id = info.get("modelID")
    if provider and model_id:
        return f"{provider}/{model_id}"
    return str(model_id) if model_id else None


def _find_value(value: object, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                return item.strip()
            found = _find_value(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, keys)
            if found:
                return found
    return None


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0
