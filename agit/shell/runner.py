from __future__ import annotations

import sys

from agit.commits import AgitActions
from agit.backends.setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agit.backends.claude import ClaudeBackend
from agit.backends.opencode import OpenCodeBackend
from agit.git import GitRepo
from agit.config import GlobalConfig
from agit.git import RepoLock, already_running_message
from agit.config import AgitState
from agit.shell.ui import AgitPrompt, PromptState


AGIT_PREFIX = ":"

BACKENDS = {
    OpenCodeBackend.name: OpenCodeBackend,
    ClaudeBackend.name: ClaudeBackend,
}


class AgitShell:
    def __init__(self, repo: GitRepo, *, verbose: bool = False, backend: str | None = None, new_session: bool = False) -> None:
        self.repo = repo
        self.global_config = GlobalConfig()
        self.state = AgitState(repo.repo, default_backend=self.global_config.default_backend)
        if backend and backend in BACKENDS and backend != self.state.backend:
            self.state.remember_backend_session()
            self.state.backend = backend
            self.global_config.default_backend = backend
            self.state.backend_session_id = self.state.stored_backend_session(backend)
            self.state.last_backend_message_id = None
        if new_session:
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            self.state.new_agit_session_id()
        self.verbose = verbose
        self.prompt = AgitPrompt(self._prompt_state)
        self.actions = AgitActions(repo, self.state, verbose=verbose)
        self.management_lock = RepoLock(repo.repo / ".agit" / "lock")

    def run(self) -> None:
        try:
            resolved = ensure_installed_backend(self.state.backend, self.global_config, interactive=sys.stdin.isatty())
        except BackendUnavailable as error:
            print(error)
            return
        if resolved != self.state.backend:
            self.state.backend = resolved
        if not self.management_lock.acquire():
            print(already_running_message(self.management_lock.owner_pid()))
            return
        self.state.save()
        if self.verbose:
            print(f"aGiT session {self.state.session_id}")
            print(f"Repository: {self.repo.repo}")
            print(f"Backend: {self.state.backend}")
            print("Type :help for aGiT commands. Backend / commands are passed through.")
        try:
            while True:
                try:
                    text = self.prompt.prompt().strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if not text:
                    continue
                if text.startswith(AGIT_PREFIX):
                    if self._handle_command(text):
                        return
                else:
                    self._handle_agent_prompt(text)
        finally:
            self.management_lock.release()

    def _handle_command(self, text: str) -> bool:
        command, _, arg = text.partition(" ")
        if command in {":exit", ":quit"}:
            return True
        if command == ":help":
            self._print_help()
        elif command == ":status":
            print(self.repo.status_short() or "Working tree clean")
        elif command == ":agent-backend":
            agent = arg.strip()
            if agent not in BACKENDS:
                print(f"Unknown backend: {agent or '(none)'}. Available: {', '.join(sorted(BACKENDS))}")
            elif not backend_installed(agent):
                print(f"'{agent}' is not installed.")
                print(install_hint(agent))
            else:
                self.state.remember_backend_session()
                self.state.backend = agent
                self.global_config.default_backend = agent
                self.state.backend_session_id = self.state.stored_backend_session(agent)
                self.state.last_backend_message_id = None
                print(f"Backend set to {agent}")
        elif command == ":user-commit":
            self.actions.create_user_commit()
        elif command == ":unstaged":
            declined = self.state.declined_untracked()
            if declined:
                print("Intentionally unstaged files:")
                for path in declined:
                    print(f"  {path}")
            else:
                print("No intentionally unstaged files.")
        elif command == ":stage":
            self.actions.review_untracked(include_declined=True)
        else:
            print(f"Unknown command: {command}")
        return False

    def _handle_agent_prompt(self, prompt: str) -> None:
        if self.actions.has_pre_agent_user_changes():
            print("User changes detected before agent runs.")
            self.actions.create_user_commit()

        backend = self._backend()
        self.state.append_trace("user", prompt)
        result = backend.run(prompt, model=self.state.model, session_id=self.state.backend_session_id)
        if result.session_id:
            self.state.backend_session_id = result.session_id
        if result.model and result.model != self.state.model:
            self.state.model = result.model
        if result.exit_code != 0:
            self.state.append_trace("agent", result.final_response or f"Backend exited with code {result.exit_code}")
            self.state.add_token_usage(result.tokens)
            if self.verbose:
                print(f"Backend exited with code {result.exit_code}; no automatic agent commit was made.")
            return

        self.state.append_trace("agent", result.final_response)
        self.state.add_token_usage(result.tokens)
        self.repo.add_tracked()
        self.actions.review_untracked(include_declined=False)
        if self.repo.has_staged_changes():
            from agit.commits import build_agent_commit_message

            self.repo.commit(
                build_agent_commit_message(
                    latest_prompt=prompt,
                    trace=self.state.pending_trace(),
                    backend=result.backend,
                    backend_session_id=self.state.backend_session_id,
                    agit_session_id=self.state.session_id,
                    model=self.state.model,
                    token_usage=self.state.pending_token_usage(),
                    trace_turn_limit=self.state.trace_turn_limit,
                )
            )
            self.state.clear_trace()
            print("Created <agent> commit.")
        else:
            if self.verbose:
                print("No code changes detected; interaction trace remains pending.")

    def _backend(self):
        backend_class = BACKENDS.get(self.state.backend)
        if backend_class is None:
            raise RuntimeError(f"Unsupported backend: {self.state.backend}")
        return backend_class(self.repo.repo, verbose=self.verbose)

    def _print_help(self) -> None:
        print("Commands:")
        print("  :help              show this help")
        print("  :status            show git status")
        print("  :user-commit       create a user commit")
        print("  :stage             review and stage untracked files")
        print("  :unstaged          show intentionally unstaged files")
        print(f"  :agent-backend <{'|'.join(BACKENDS)}> select the agent backend")
        print("  :exit              exit")
        print("Backend / commands are not reserved by aGiT and are sent to the backend.")

    def _prompt_state(self) -> PromptState:
        existing = [path for path in self.state.declined_untracked() if (self.repo.repo / path).exists()]
        return PromptState(
            repo=self.repo.repo,
            backend=self.state.backend,
            model=self.state.model,
            declined_count=len(existing),
            verbose=self.verbose,
        )
