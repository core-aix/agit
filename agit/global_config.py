from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_BACKEND = "opencode"

# The key that opens aGiT's command menu in proxy mode. Configurable as
# "menu_key" in config.json ("ctrl-<letter>"); a few control keys are excluded
# because the terminal or aGiT already gives them a meaning: Ctrl-C (exit
# flow), Ctrl-H (Backspace), Ctrl-I (Tab), Ctrl-J/Ctrl-M (Enter).
DEFAULT_MENU_KEY = "ctrl-g"
_MENU_KEY_RE = re.compile(r"^ctrl[-+]([a-bd-gk-ln-z])$")

# Tunable timings (all in seconds) governing aGiT's polling / debounce behaviour.
# Stored under the "timings" key in config.json; any subset may be overridden, and
# anything missing or invalid falls back to the default below.
DEFAULT_TIMINGS: dict[str, float] = {
    "base_poll_seconds": 3.0,  # how often to re-check the base branch HEAD for out-of-band commits
    "background_poll_seconds": 2.0,  # how often an idle background session is serviced
    "file_stable_seconds": 8.0,  # quiet period after a file change before an auto-commit
    "child_idle_seconds": 4.0,  # no backend output for this long counts as idle
    "parse_cooldown_seconds": 10.0,  # minimum gap between agent-turn parses
    "base_edit_check_seconds": 3.0,  # how often to warn about un-sandboxed base-repo edits
    "cwd_check_seconds": 3.0,  # how often to check for the resume-cwd drift bug
    "base_drift_check_seconds": 2.0,  # how often to check the base repo's checked-out branch
}


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

    @property
    def sandbox(self) -> bool:
        # Confine the agent's writes to its session worktree (on by default).
        value = self.data.get("sandbox")
        return True if value is None else bool(value)

    @sandbox.setter
    def sandbox(self, value: bool) -> None:
        self.data["sandbox"] = bool(value)
        self.save()

    @property
    def menu_key(self) -> str:
        # Normalized "ctrl-<letter>" spec for the aGiT menu key. Invalid or
        # conflicting values fall back to the default so a config typo can
        # never lock the user out of the menu.
        value = self.data.get("menu_key")
        if isinstance(value, str):
            match = _MENU_KEY_RE.match(value.strip().lower())
            if match:
                return f"ctrl-{match.group(1)}"
        return DEFAULT_MENU_KEY

    @property
    def menu_key_byte(self) -> bytes:
        return bytes([ord(self.menu_key[-1]) - 96])  # ctrl-a..ctrl-z → 0x01..0x1a

    @property
    def menu_key_label(self) -> str:
        return f"Ctrl-{self.menu_key[-1].upper()}"

    @property
    def timings(self) -> dict[str, float]:
        # Defaults overlaid with any valid user overrides from the "timings" object.
        # An override must be a positive number; bad values (wrong type, <= 0, bool)
        # are ignored so a typo can never stall or busy-spin a poll loop.
        stored = self.data.get("timings")
        stored = stored if isinstance(stored, dict) else {}
        result = dict(DEFAULT_TIMINGS)
        for key in DEFAULT_TIMINGS:
            value = stored.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                result[key] = float(value)
        return result
