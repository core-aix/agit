from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_BACKEND = "opencode"


def _default_path() -> Path:
    config_dir = os.environ.get("AGIT_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agit"
    return base / "config.json"


class GlobalConfig:
    """User-wide aGiT configuration stored in ``~/.agit/config.json``.

    Holds preferences that should persist across repositories, such as the
    default agent backend used when a repository has no backend recorded yet.
    The location can be overridden with the ``AGIT_CONFIG_DIR`` environment
    variable.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def has_default_backend(self) -> bool:
        return bool(self.data.get("default_backend"))

    @property
    def default_backend(self) -> str:
        value = self.data.get("default_backend")
        return str(value) if value else DEFAULT_BACKEND

    @default_backend.setter
    def default_backend(self, value: str) -> None:
        self.data["default_backend"] = value
        self.save()
