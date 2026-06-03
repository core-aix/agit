from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any


class AgitState:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.path = repo / ".agit" / "state.json"
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        default = self._default()
        default.update(data)
        return default

    def _default(self) -> dict[str, Any]:
        return {
            "agit_session_id": f"agit-{uuid.uuid4()}",
            "backend": "opencode",
            "model": None,
            "backend_session_id": None,
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
            },
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_repo_local_ignore()
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def _ensure_repo_local_ignore(self) -> None:
        exclude = self.repo / ".git" / "info" / "exclude"
        if not exclude.exists():
            return
        content = exclude.read_text(encoding="utf-8")
        if ".agit/" in content.splitlines():
            return
        with exclude.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(".agit/\n")

    @property
    def session_id(self) -> str:
        return str(self.data["agit_session_id"])

    @property
    def backend(self) -> str:
        return str(self.data.get("backend") or "opencode")

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

    def pending_trace(self) -> list[dict]:
        return list(self.data.get("pending_trace") or [])

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
        for key in ("total", "input", "output", "reasoning", "cache_read", "cache_write"):
            current[key] = int(current.get(key) or 0) + int(getattr(usage, key, 0) or 0)
        self.data["pending_token_usage"] = current
        self.save()
