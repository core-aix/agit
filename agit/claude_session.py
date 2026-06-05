from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from agit.backends.base import TokenUsage
from agit.session import ExportedSession, SessionRef, SessionTurn, turns_after

__all__ = [
    "ExportedSession",
    "SessionRef",
    "SessionTurn",
    "turns_after",
    "latest_session_id",
    "list_sessions",
    "list_worktree_sessions",
    "session_belongs_to_repo",
    "export_session",
    "prepare_resume",
    "link_session",
    "session_cwd",
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
    refs = list_sessions(repo)
    if not refs:
        return None
    return max(refs, key=lambda ref: ref.updated).id


def _refs_in_project_dir(project_dir: Path) -> list[SessionRef]:
    if not project_dir.is_dir():
        return []
    refs = []
    for path in project_dir.glob("*.jsonl"):
        if not path.is_file():
            continue
        try:
            updated = path.stat().st_mtime
        except OSError:
            continue
        refs.append(SessionRef(id=path.stem, updated=updated, label=_session_label(path)))
    return refs


def list_sessions(repo: Path) -> list[SessionRef]:
    return _refs_in_project_dir(_project_dir(repo))


def list_worktree_sessions(worktrees_root: Path) -> list[tuple[str, SessionRef]]:
    """Every Claude conversation recorded under any aGiT worktree of this repo,
    newest first, paired with the worktree key needed to recreate it. Includes
    conversations whose worktree has since been deleted (Claude keeps the
    transcript keyed by the worktree path), so they stay resumable."""
    root = _projects_root()
    if not root.is_dir():
        return []
    prefix = _encode_repo(worktrees_root) + "-"
    out: list[tuple[str, SessionRef]] = []
    for project_dir in root.iterdir():
        if not project_dir.is_dir() or not project_dir.name.startswith(prefix):
            continue
        worktree_key = project_dir.name[len(prefix):]
        if not worktree_key:
            continue
        for ref in _refs_in_project_dir(project_dir):
            out.append((worktree_key, ref))
    out.sort(key=lambda item: item[1].updated, reverse=True)
    return out


def _session_label(path: Path, *, line_limit: int = 100) -> str | None:
    # The first real user prompt makes a readable label; it is near the top of
    # the transcript, so reading only the head keeps listing cheap.
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= line_limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "user":
                    prompt = _user_prompt(row)
                    if prompt:
                        return prompt.splitlines()[0]
    except OSError:
        return None
    return None


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    return _session_path(repo, session_id).is_file()


def prepare_resume(worktree: Path, session_id: str) -> bool:
    """Ensure ``claude --resume <session_id>`` works when run in ``worktree``.

    Claude looks up a session's transcript in the project dir of its current
    working directory, so a conversation recorded elsewhere (the repo root before
    aGiT ran, or a different worktree) is invisible from a fresh worktree. Link the
    transcript into the worktree's project dir so the resume finds it. We hardlink
    (one inode, two names) rather than copy, so turns aGiT appends from the worktree
    stay visible to a plain `claude` run in the original directory, and vice-versa
    — the conversation does not fork. Falls back to a copy only across filesystems
    (where hardlinks aren't possible). Returns True if the transcript is in place."""
    if not session_id:
        return False
    worktree = Path(worktree)
    target_dir = _project_dir(worktree)
    target = target_dir / f"{session_id}.jsonl"
    if target.is_file():
        return True
    source = _find_session_file(session_id)
    if source is None:
        return False
    if source.resolve() == target.resolve():
        return True
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        os.link(source, target)  # share one inode so new turns flow both ways
    except FileExistsError:
        return True
    except OSError:
        try:
            shutil.copy2(source, target)  # different filesystem: copy instead
        except OSError:
            return False
    return True


def link_session(session_id: str, src_repo: Path, dst_repo: Path) -> bool:
    """Hardlink a session's transcript from ``src_repo``'s project dir into
    ``dst_repo``'s, so the conversation is also visible/continuable from
    ``dst_repo`` — e.g. surfacing an aGiT worktree session in the repo root so a
    plain ``claude`` run there can resume it. One inode, two names, so later turns
    stay shared. No-op if the source isn't recorded yet or a transcript already
    sits at the destination."""
    if not session_id:
        return False
    src = _session_path(Path(src_repo), session_id)
    if not src.is_file():
        return False
    dst_dir = _project_dir(Path(dst_repo))
    dst = dst_dir / f"{session_id}.jsonl"
    if dst.exists():
        return True
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
    except FileExistsError:
        return True
    except OSError:
        return False
    return True


def session_cwd(session_id: str) -> str | None:
    """The working directory Claude most recently recorded for a session. Claude
    writes its `cwd` into (almost) every transcript line, so this reads the last
    one that has it from the newest transcript file. Used to detect a resume that
    restored the session's old cwd instead of the worktree it was launched in."""
    if not session_id:
        return None
    path = _find_session_file(session_id)
    if path is None:
        return None
    found: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if isinstance(cwd, str) and cwd:
                    found = cwd  # keep the last one
    except OSError:
        return None
    return found


def _find_session_file(session_id: str) -> Path | None:
    # The transcript for a session id may live under any project dir (the repo
    # root, a worktree). Return the most recent match.
    root = _projects_root()
    if not root.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if not candidate.is_file():
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, candidate)
    return newest[1] if newest else None


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
        elif row_type == "assistant" and current is not None and row.get("isSidechain"):
            # Sub-agent (sidechain) turns are not part of the main interaction
            # trace, but their tokens are still consumed — record them under the
            # turn's sub-agent buckets instead of dropping them.
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            current["tokens"].add(_message_tokens(message.get("usage"), sidechain=True))
        elif row_type == "assistant" and current is not None:
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
    # `isCompactSummary` marks the summary Claude injects as a user message when
    # it compacts a conversation (it also sets `isVisibleInTranscriptOnly`). It
    # is not a real prompt, so keep it out of the interaction trace and subject.
    if row.get("isMeta") or row.get("isSidechain") or row.get("isCompactSummary"):
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


def _message_tokens(usage: object, *, sidechain: bool = False) -> TokenUsage:
    if not isinstance(usage, dict):
        return TokenUsage()
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    # Claude folds extended-thinking and tool-call tokens into output_tokens, so
    # there is no separate reasoning figure to record here.
    if sidechain:
        # A sub-agent has its own context window; only its consumption counts,
        # not its context size, so context is left untouched for the main turn.
        return TokenUsage(
            total=output_tokens,
            subagent_input=input_tokens,
            subagent_output=output_tokens,
            subagent_cache_read=cache_read,
            subagent_cache_write=cache_write,
        )
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
