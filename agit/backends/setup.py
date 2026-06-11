from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from agit.backends.proxy_agents import available_backends, make_proxy_agent

# Where to point users when a backend CLI is missing.
INSTALL_HINTS = {
    "claude": (
        "Install Claude Code: https://docs.claude.com/en/docs/claude-code\n"
        "  e.g. curl -fsSL https://claude.ai/install.sh | bash  (or: npm install -g @anthropic-ai/claude-code)"
    ),
    "opencode": (
        "Install OpenCode: https://opencode.ai\n"
        "  e.g. brew install sst/tap/opencode  (or: npm install -g opencode-ai)"
    ),
}


class BackendUnavailable(RuntimeError):
    """Raised when the selected backend's CLI is not installed and no installed
    alternative was chosen."""


def _executable(name: str) -> str:
    return make_proxy_agent(name).spawn_command(Path("."), session_id=None, resume=False)[0]


def backend_installed(name: str) -> bool:
    return shutil.which(_executable(name)) is not None


def install_hint(name: str) -> str:
    return INSTALL_HINTS.get(name, f"Install the '{name}' CLI and make sure it is on your PATH.")


def select_default_backend(
    config,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    """First-run prompt: let the user pick the default backend (listed
    alphabetically), checking installation and offering to install or choose
    another. Saves and returns the chosen backend."""
    names = available_backends()
    while True:
        output_fn("Welcome to aGiT! Choose your default agent backend:")
        for index, name in enumerate(names, start=1):
            status = "installed" if backend_installed(name) else "not installed"
            output_fn(f"  {index}. {name} ({status})")
        raw = input_fn(f"Enter a number [1-{len(names)}] (default 1): ").strip()
        choice = raw or "1"
        if not choice.isdigit() or not 1 <= int(choice) <= len(names):
            output_fn("Please enter a valid number.")
            continue
        name = names[int(choice) - 1]
        if backend_installed(name) or _wait_for_install(name, input_fn=input_fn, output_fn=output_fn):
            config.default_backend = name
            return name
        # User asked to choose a different backend: show the menu again.


def ensure_installed_backend(
    name: str,
    config,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    """Make sure the backend that is about to run is installed. If not, prompt
    the user to install it or switch to an installed backend (saving the new
    default). Returns the backend to use; raises BackendUnavailable otherwise."""
    if backend_installed(name):
        return name
    if not interactive:
        raise BackendUnavailable(f"Backend '{name}' is not installed.\n{install_hint(name)}")

    names = available_backends()
    while True:
        if backend_installed(name):
            return name
        output_fn(f"\nThe selected backend '{name}' is not installed.")
        output_fn(install_hint(name))
        installed = [other for other in names if backend_installed(other)]
        prompt = "Press Enter after installing to retry"
        if installed:
            prompt += f", type a backend to switch to ({', '.join(installed)})"
        prompt += ", or 'q' to quit: "
        answer = input_fn(prompt).strip().lower()
        if answer in {"q", "quit"}:
            raise BackendUnavailable(f"Backend '{name}' is not installed.")
        if answer in installed:
            config.default_backend = answer
            return answer
        # Otherwise loop and re-check whether `name` is now installed.


def _wait_for_install(
    name: str,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> bool:
    """Return True once `name` is installed, or False if the user wants to
    choose a different backend."""
    while True:
        output_fn(f"\n'{name}' is not installed.")
        output_fn(install_hint(name))
        answer = input_fn("Press Enter after installing to continue, or type 'b' to choose a different backend: ").strip().lower()
        if answer in {"b", "back", "c", "choose"}:
            return False
        if backend_installed(name):
            return True
        output_fn(f"'{name}' was still not found on your PATH.")
