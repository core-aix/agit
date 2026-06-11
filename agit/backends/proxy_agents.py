from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol

from agit.transcripts import claude as claude_session, opencode as opencode_session
from agit.transcripts import ExportedSession, SessionRef


class ProxyAgent(Protocol):
    """A coding-agent CLI driven through aGiT's proxy (native TUI) mode.

    Each backend launches its own interactive TUI and records its transcript in
    a backend-specific place; these methods give the proxy a uniform way to
    spawn the CLI and recover the turns/responses/tokens aGiT commits.
    """

    name: str

    def new_session_id(self) -> str | None:
        """A session id to start a fresh session with, or None to let the
        backend choose one that aGiT will discover afterwards."""

    def spawn_command(self, repo: Path, *, session_id: str | None, resume: bool) -> list[str]: ...

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool: ...

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        """Make sure spawning the resume command in ``repo`` (as cwd) will find
        this conversation, staging its transcript there if the backend stores
        transcripts per directory. Returns True if it can be resumed."""

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        """Make a conversation running in ``worktree`` also visible/continuable
        from ``base_repo`` (e.g. a plain CLI run in the repo root). Returns True
        if mirrored. No-op for backends without per-directory transcript files."""

    def recorded_working_dir(self, session_id: str) -> str | None:
        """The working directory the backend most recently recorded for a session,
        or None if it doesn't record one. Used to detect a resume that drifted the
        cwd away from the worktree it was launched in."""

    def latest_session_id(self, repo: Path) -> str | None: ...

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        """Every session recorded for this repository, for listing/switching."""

    def export_session(self, repo: Path, session_id: str) -> ExportedSession | None: ...

    def is_event_blob(self, content: str) -> bool:
        """Whether a trace entry is a raw backend event dump that should be
        dropped rather than committed."""


class OpenCodeProxyAgent:
    name = "opencode"

    def new_session_id(self) -> str | None:
        # OpenCode assigns its own session id; aGiT discovers it after the run.
        return None

    def spawn_command(self, repo: Path, *, session_id: str | None, resume: bool) -> list[str]:
        command = ["opencode"]
        if resume and session_id:
            command.extend(["--session", session_id])
        command.append(str(repo))
        return command

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool:
        return opencode_session.session_belongs_to_repo(repo, session_id)

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        # OpenCode resumes by id from its own global store, regardless of cwd.
        return bool(session_id)

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        # OpenCode keeps sessions in a global store keyed by id (resumable from
        # anywhere); there's no per-directory transcript file to link.
        return False

    def recorded_working_dir(self, session_id: str) -> str | None:
        return None  # not tracked for OpenCode

    def latest_session_id(self, repo: Path) -> str | None:
        return opencode_session.latest_session_id(repo)

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        return opencode_session.list_sessions(repo)

    def list_worktree_sessions(self, worktrees_root: Path) -> list[tuple[str, SessionRef]]:
        return opencode_session.list_worktree_sessions(worktrees_root)

    def export_session(self, repo: Path, session_id: str) -> ExportedSession | None:
        return opencode_session.export_session(repo, session_id)

    def is_event_blob(self, content: str) -> bool:
        return opencode_session.looks_like_event_blob(content)


class ClaudeProxyAgent:
    name = "claude"

    def new_session_id(self) -> str | None:
        # Claude accepts an explicit session id, so aGiT picks one up front and
        # knows exactly which transcript to read.
        return str(uuid.uuid4())

    def spawn_command(self, repo: Path, *, session_id: str | None, resume: bool) -> list[str]:
        if resume and session_id:
            return ["claude", "--resume", session_id]
        if session_id:
            return ["claude", "--session-id", session_id]
        return ["claude"]

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool:
        return claude_session.session_belongs_to_repo(repo, session_id)

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        return claude_session.prepare_resume(repo, session_id)

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        return claude_session.link_session(session_id, worktree, base_repo)

    def recorded_working_dir(self, session_id: str) -> str | None:
        return claude_session.session_cwd(session_id)

    def latest_session_id(self, repo: Path) -> str | None:
        return claude_session.latest_session_id(repo)

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        return claude_session.list_sessions(repo)

    def list_worktree_sessions(self, worktrees_root: Path) -> list[tuple[str, SessionRef]]:
        return claude_session.list_worktree_sessions(worktrees_root)

    def export_session(self, repo: Path, session_id: str) -> ExportedSession | None:
        return claude_session.export_session(repo, session_id)

    def is_event_blob(self, content: str) -> bool:
        return False


_AGENTS: dict[str, type] = {
    OpenCodeProxyAgent.name: OpenCodeProxyAgent,
    ClaudeProxyAgent.name: ClaudeProxyAgent,
}


def available_backends() -> list[str]:
    return sorted(_AGENTS)


def make_proxy_agent(name: str) -> ProxyAgent:
    # Raise on an unknown backend rather than silently substituting one: a stale
    # or mistyped backend name must surface, not quietly launch the wrong agent.
    try:
        agent_class = _AGENTS[name]
    except KeyError:
        raise ValueError(f"Unknown backend {name!r}; choose one of {', '.join(available_backends())}.") from None
    return agent_class()
