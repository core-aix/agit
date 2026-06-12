from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any


class AgitState:
    def __init__(self, repo: Path, *, default_backend: str = "opencode") -> None:
        self.repo = repo
        self.path = repo / ".agit" / "state.json"
        self.config_path = repo / ".agit" / "config.json"
        self._default_backend = default_backend
        self.data = self._load()
        self.config = self._load_config()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A truncated or invalid state file must not brick startup. Keep the
            # corrupt file aside for debugging and start from defaults.
            self._quarantine_corrupt()
            return self._default()
        if not isinstance(data, dict):
            self._quarantine_corrupt()
            return self._default()
        default = self._default()
        default.update(data)
        return default

    def _quarantine_corrupt(self) -> None:
        try:
            self.path.replace(self.path.with_name(self.path.name + ".bak"))
        except OSError:
            pass

    def _default(self) -> dict[str, Any]:
        return {
            "agit_session_id": f"agit-{uuid.uuid4()}",
            "backend": self._default_backend,
            "model": None,
            "backend_session_id": None,
            "backend_session_repo": None,
            "backend_session_ids": {},
            "backend_sessions": {},
            "session_names": {},
            "last_backend_message_id": None,
            "declined_untracked_files": [],
            "pending_trace": [],
            "pending_token_usage": {
                "context": None,
                "total": 0,
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "subagent_input": 0,
                "subagent_output": 0,
                "subagent_reasoning": 0,
                "subagent_cache_read": 0,
                "subagent_cache_write": 0,
            },
            "session_summary": None,
            "session_summary_commit": None,
        }

    def _default_config(self) -> dict[str, Any]:
        return {"trace_turn_limit": 5, "summarization_model": None, "summarization_enabled": True}

    def _load_config(self) -> dict[str, Any]:
        default = self._default_config()
        if not self.config_path.exists():
            return default
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return default  # user-edited file; don't crash, just use defaults
        default.update(data if isinstance(data, dict) else {})
        return default

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_repo_local_ignore()
        # Atomic write: save() runs on every property setter, so an in-place
        # rewrite interrupted by a crash/SIGKILL/full disk would leave exactly
        # the truncated file that bricks the next startup.
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def _ensure_repo_local_ignore(self) -> None:
        exclude = self._exclude_path()
        if exclude is None:
            return
        if not exclude.exists():
            # Repos created without the default template have no info/exclude;
            # create it (only in an actual git repo) so .agit/ stays unignored
            # nowhere. The worktree case resolves to the shared git dir via git.
            if not (self.repo / ".git").exists():
                return
            try:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text(".agit/\n", encoding="utf-8")
            except OSError:
                pass
            return
        content = exclude.read_text(encoding="utf-8")
        if ".agit/" in content.splitlines():
            return
        with exclude.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(".agit/\n")

    def _exclude_path(self) -> Path | None:
        # Resolve the info/exclude path via git so it works inside a worktree,
        # where ``.git`` is a file pointing at the shared git dir rather than a
        # directory. Fall back to the conventional location when git is not
        # available (e.g. tests with a fabricated .git/info).
        fallback = self.repo / ".git" / "info" / "exclude"
        try:
            process = subprocess.run(
                ["git", "rev-parse", "--git-path", "info/exclude"],
                cwd=self.repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError:
            return fallback
        if process.returncode != 0:
            return fallback
        path = Path(process.stdout.strip())
        return path if path.is_absolute() else self.repo / path

    @property
    def session_id(self) -> str:
        return str(self.data["agit_session_id"])

    def new_agit_session_id(self) -> str:
        self.data["agit_session_id"] = f"agit-{uuid.uuid4()}"
        self.save()
        return self.session_id

    @property
    def backend(self) -> str:
        # Honour the configured default (not a hardcoded backend) when the record
        # has no backend yet, or when the stored value is no longer a known
        # backend, so a missing/stale entry never silently launches the wrong
        # agent (and make_proxy_agent never receives an invalid name).
        from agit.backends.proxy_agents import available_backends

        stored = self.data.get("backend")
        if stored and stored in available_backends():
            return str(stored)
        return self._default_backend

    @backend.setter
    def backend(self, value: str) -> None:
        self.data["backend"] = value
        self.save()

    @property
    def model(self) -> str | None:
        value = self.data.get("model")
        return str(value) if value else None

    @model.setter
    def model(self, value: str | None) -> None:
        self.data["model"] = value
        self.save()

    @property
    def backend_session_id(self) -> str | None:
        value = self.data.get("backend_session_id")
        return str(value) if value else None

    @backend_session_id.setter
    def backend_session_id(self, value: str | None) -> None:
        self.data["backend_session_id"] = value
        self.data["backend_session_repo"] = str(self.repo) if value else None
        self.save()

    def backend_session_matches_repo(self) -> bool:
        return self.data.get("backend_session_repo") == str(self.repo)

    def remember_backend_session(self) -> None:
        """Record the current backend's session id so it can be restored when
        the user switches back to this backend."""
        sessions = dict(self.data.get("backend_session_ids") or {})
        if self.backend_session_id:
            sessions[self.backend] = self.backend_session_id
        else:
            sessions.pop(self.backend, None)
        self.data["backend_session_ids"] = sessions
        self.save()

    def stored_backend_session(self, backend: str) -> str | None:
        value = (self.data.get("backend_session_ids") or {}).get(backend)
        return str(value) if value else None

    def remember_session(
        self,
        backend: str,
        *,
        session_id: str | None,
        worktree: str,
        message_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Record a backend's most recent conversation (its id and the worktree it
        ran in) so it can be resumed after its worktree is removed on exit."""
        sessions = dict(self.data.get("backend_sessions") or {})
        if session_id:
            sessions[backend] = {"id": session_id, "worktree": worktree, "message_id": message_id, "model": model}
        else:
            sessions.pop(backend, None)
        self.data["backend_sessions"] = sessions
        self.save()

    def recall_session(self, backend: str) -> dict | None:
        record = (self.data.get("backend_sessions") or {}).get(backend)
        return dict(record) if isinstance(record, dict) else None

    def session_name_for(self, session_id: str | None) -> str | None:
        """The user-given name for a backend conversation, if one was set."""
        if not session_id:
            return None
        value = (self.data.get("session_names") or {}).get(str(session_id))
        return str(value) if value else None

    def name_session(self, session_id: str | None, name: str | None) -> None:
        """Record (or clear) the user-given name for a backend conversation."""
        if not session_id:
            return
        names = dict(self.data.get("session_names") or {})
        if name:
            names[str(session_id)] = name
        else:
            names.pop(str(session_id), None)
        self.data["session_names"] = names
        self.save()

    @property
    def last_backend_message_id(self) -> str | None:
        value = self.data.get("last_backend_message_id")
        return str(value) if value else None

    @last_backend_message_id.setter
    def last_backend_message_id(self, value: str | None) -> None:
        self.data["last_backend_message_id"] = value
        self.save()

    def declined_untracked(self) -> list[str]:
        return list(self.data.get("declined_untracked_files") or [])

    def add_declined(self, paths: list[str]) -> None:
        current = set(self.declined_untracked())
        current.update(paths)
        self.data["declined_untracked_files"] = sorted(current)
        self.save()

    def remove_declined(self, paths: list[str]) -> None:
        remove = set(paths)
        self.data["declined_untracked_files"] = [path for path in self.declined_untracked() if path not in remove]
        self.save()

    def keep_declined(self, paths: list[str]) -> None:
        keep = set(paths)
        self.data["declined_untracked_files"] = [path for path in self.declined_untracked() if path in keep]
        self.save()

    def pending_trace(self) -> list[dict]:
        return list(self.data.get("pending_trace") or [])

    @property
    def trace_turn_limit(self) -> int:
        value = self.config.get("trace_turn_limit", 5)
        return value if isinstance(value, int) and value > 0 else 5

    @property
    def summarization_model(self) -> str | None:
        value = self.config.get("summarization_model")
        return str(value) if value else None

    @summarization_model.setter
    def summarization_model(self, value: str | None) -> None:
        self.config["summarization_model"] = value
        self._save_config()

    @property
    def summarization_enabled(self) -> bool:
        value = self.config.get("summarization_enabled")
        return True if value is None else bool(value)

    @summarization_enabled.setter
    def summarization_enabled(self, value: bool) -> None:
        self.config["summarization_enabled"] = bool(value)
        self._save_config()

    def _save_config(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.config, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.config_path)

    def append_trace(self, role: str, content: str) -> None:
        trace = self.pending_trace()
        trace.append({"role": role, "content": content})
        self.data["pending_trace"] = trace
        self.save()

    def clear_trace(self) -> None:
        self.data["pending_trace"] = []
        self.data["pending_token_usage"] = self._default()["pending_token_usage"]
        self.save()

    def pending_token_usage(self) -> dict[str, int | None]:
        usage = dict(self._default()["pending_token_usage"])
        usage.update(self.data.get("pending_token_usage") or {})
        return usage

    def add_token_usage(self, usage) -> None:
        current = self.pending_token_usage()
        if usage.context is not None:
            current["context"] = usage.context
        for key in (
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
        ):
            current[key] = int(current.get(key) or 0) + int(getattr(usage, key, 0) or 0)
        self.data["pending_token_usage"] = current
        self.save()

    @property
    def session_summary(self) -> str | None:
        value = self.data.get("session_summary")
        return str(value) if value else None

    @session_summary.setter
    def session_summary(self, value: str | None) -> None:
        self.data["session_summary"] = value
        self.save()

    @property
    def session_summary_commit(self) -> str | None:
        value = self.data.get("session_summary_commit")
        return str(value) if value else None

    @session_summary_commit.setter
    def session_summary_commit(self, value: str | None) -> None:
        self.data["session_summary_commit"] = value
        self.save()
