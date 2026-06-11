from __future__ import annotations

import sys
from typing import Any
from shutil import get_terminal_size
from dataclasses import dataclass
from pathlib import Path


AGIT_COMMANDS = {
    ":help": "show aGiT commands",
    ":status": "show Git status",
    ":user-commit": "create a user commit",
    ":stage": "review untracked files",
    ":unstaged": "show intentionally unstaged files",
    ":agent-backend": "switch backend",
    ":exit": "exit aGiT",
    ":quit": "exit aGiT",
}

OPENCODE_COMMANDS = {
    "/help": "show OpenCode help",
    "/init": "initialize project context",
    "/model": "switch OpenCode model",
    "/agent": "switch OpenCode agent",
    "/session": "manage sessions",
    "/compact": "compact conversation",
    "/undo": "undo last change",
    "/redo": "redo last undo",
    "/share": "share session",
}


@dataclass
class PromptState:
    repo: Path
    backend: str
    model: str | None
    declined_count: int
    verbose: bool


class AgitPrompt:
    def __init__(self, state_provider) -> None:
        self.state_provider = state_provider
        self.session: Any = None
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                from prompt_toolkit import PromptSession

                self.session = PromptSession(
                    completer=_AgitCompleter(),
                    bottom_toolbar=self._bottom_toolbar,
                    complete_while_typing=True,
                )
            except ImportError:
                self.session = None

    def prompt(self) -> str:
        if self.session is None:
            return input("> ")
        return self.session.prompt("> ")

    def _bottom_toolbar(self):
        state = self.state_provider()
        right = f"unstaged new: {state.declined_count}  :stage" if state.declined_count else ""
        verbose = " | verbose" if state.verbose else ""
        hint = " | type : for aGiT controls, / for OpenCode controls"
        left = f" aGiT {state.backend} | {state.repo.name}{verbose}{hint}"
        if not right:
            return [("class:bottom-toolbar", f"{left} ")]
        width = self._terminal_width()
        padding = " " * max(width - len(left) - len(right) - 2, 1)
        return [
            ("class:bottom-toolbar", left),
            ("class:bottom-toolbar", padding),
            ("class:bottom-toolbar", f"{right} "),
        ]

    def _terminal_width(self) -> int:
        try:
            from prompt_toolkit.application.current import get_app

            return get_app().output.get_size().columns
        except Exception:
            return get_terminal_size(fallback=(100, 24)).columns


try:
    from prompt_toolkit.completion import Completer
except ImportError:  # pragma: no cover - fallback is used when prompt_toolkit is absent
    Completer = object  # type: ignore[misc, assignment]


class _AgitCompleter(Completer):
    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        stripped = text.lstrip()
        if stripped.startswith(":"):
            yield from self._complete_commands(AGIT_COMMANDS, stripped, Completion)
        elif stripped.startswith("/"):
            yield from self._complete_commands(OPENCODE_COMMANDS, stripped, Completion)

    def _complete_commands(self, commands, stripped: str, completion_cls):
        token = stripped.split(maxsplit=1)[0]
        for command, description in commands.items():
            if command.startswith(token):
                yield completion_cls(
                    command,
                    start_position=-len(token),
                    display_meta=description,
                )
