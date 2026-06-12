from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from agit.summaries.prompts import MODEL_SELECTION_SYSTEM

if TYPE_CHECKING:
    from agit.backends.base import AgentBackend


def detect_cheapest_model(backend: AgentBackend) -> str | None:
    models = _list_available_models(backend.name)
    if not models:
        return None
    if len(models) == 1:
        return models[0]
    prompt = f"{MODEL_SELECTION_SYSTEM}\n\nAvailable models:\n" + "\n".join(models)
    result = backend.run(prompt, model=None, session_id=None)
    selected = result.final_response.strip()
    if selected in models:
        return selected
    for model in models:
        if selected in model or model in selected:
            return model
    return None


def _list_available_models(backend_name: str) -> list[str]:
    if backend_name == "opencode":
        return _list_opencode_models()
    if backend_name == "claude":
        return _list_claude_models()
    return []


def _list_opencode_models() -> list[str]:
    try:
        result = subprocess.run(
            ["opencode", "models"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                models.append(line.split()[0] if " " in line else line)
        return models
    except (subprocess.TimeoutExpired, OSError):
        return []


def _list_claude_models() -> list[str]:
    try:
        result = subprocess.run(
            ["claude", "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        return ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]
    except (subprocess.TimeoutExpired, OSError):
        return []
