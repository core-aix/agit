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
    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        backend_args: list[str] | None = None,
        prompts: list[str] | None = None,
    ) -> None:
        self.repo = repo
        self.backend_args = list(backend_args or [])  # forwarded to the backend CLI (#32)
        # Scripted mode (#53): run these prompts in order, then exit. No
        # question can be answered in a scripted or piped run, so everything
        # that would ask one falls back to a safe non-interactive default.
        self.prompts = list(prompts) if prompts is not None else None
        self.interactive = self.prompts is None and sys.stdin.isatty()
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
        self.actions = AgitActions(repo, self.state, verbose=verbose, interactive=self.interactive)
        self.management_lock = RepoLock(repo.repo / ".agit" / "lock")

    def run(self) -> None:
        try:
            resolved = ensure_installed_backend(self.state.backend, self.global_config, interactive=self.interactive)
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
            if self.prompts is not None:
                self._run_scripted(self.prompts)
                return
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

    def _run_scripted(self, prompts: list[str]) -> None:
        """`agit --prompt ...` (#53): run the prompts in order, then exit.
        ':' commands work exactly as at the interactive prompt; each prompt is
        echoed so the output reads like a session transcript."""
        for text in prompts:
            text = text.strip()
            if not text:
                continue
            print(f"> {text}")
            if text.startswith(AGIT_PREFIX):
                if self._handle_command(text):
                    return
            else:
                self._handle_agent_prompt(text)

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
        elif command == ":summarizer":
            self._handle_summarizer_command(arg.strip())
        else:
            print(f"Unknown command: {command}")
        return False

    def _handle_summarizer_command(self, arg: str) -> None:
        sub = arg.lower()
        if sub in ("on", "off"):
            enabled = sub == "on"
            self.state.summarization_enabled = enabled
            print(f"Summarizer {'enabled' if enabled else 'disabled'}.")
        elif sub == "model":
            current = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            print(f"Current summarizer model: {current}")
            new_model = input("Enter model (empty to clear): ").strip()
            self.state.summarization_model = new_model or None
            print(f"Summarizer model set to: {self.state.summarization_model or '(same as session)'}")
        elif sub == "" or sub == "status":
            enabled = self._summarization_enabled()
            model = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            print(f"Summarizer: {'ON' if enabled else 'OFF'}")
            print(f"Model: {model}")
        else:
            print(f"Unknown summarizer command: {arg}")
            print("Usage: :summarizer [on|off|model|status]")

    def _summarization_enabled(self) -> bool:
        state_enabled = getattr(self.state, "summarization_enabled", None)
        if state_enabled is not None:
            return state_enabled
        if self.global_config is not None:
            gc_enabled = getattr(self.global_config, "summarization_enabled", None)
            if gc_enabled is not None:
                return gc_enabled
        return True

    def _handle_agent_prompt(self, prompt: str) -> None:
        if prompt.startswith("/compact"):
            self._handle_pre_compaction()

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
            from agit.commits import build_agent_commit_message, summary_metadata_lines
            from agit.summaries import Summarizer

            diff_before_commit = self.repo.diff_head()
            commit_summary = None
            summary_metadata = None
            summarizer_model = self.state.summarization_model or self.global_config.summarization_model
            if self._summarization_enabled():
                # Shell mode is synchronous per prompt, so summarizing inline is
                # fine — but say so, since the LLM call can take a while.
                print("aGiT is summarizing the changes before committing...")
                try:
                    summarizer = Summarizer(self._summarizer_backend(), model=summarizer_model)
                    from agit.transcripts.types import SessionTurn

                    turns = [
                        SessionTurn(
                            user_message_id="",
                            assistant_message_id="",
                            user_prompt=prompt,
                            final_response=result.final_response,
                            tokens=result.tokens,
                            model=result.model,
                            complete=True,
                            interrupted=False,
                        )
                    ]
                    commit_summary = summarizer.summarize_commit(
                        turns=turns,
                        diff=diff_before_commit,
                        session_summary=self.state.session_summary,
                    )
                    summary_metadata = summary_metadata_lines(
                        model=summarizer.model or self.state.model,
                        tokens_input=summarizer.tokens_input,
                        tokens_output=summarizer.tokens_output,
                    )
                except Exception as error:
                    if self.verbose:
                        print(f"Summarization failed: {error}")

            commit_sha = self.repo.commit(
                build_agent_commit_message(
                    latest_prompt=prompt,
                    trace=self.state.pending_trace(),
                    backend=result.backend,
                    backend_session_id=self.state.backend_session_id,
                    agit_session_id=self.state.session_id,
                    model=self.state.model,
                    token_usage=self.state.pending_token_usage(),
                    trace_turn_limit=self.state.trace_turn_limit,
                    summary=commit_summary,
                    summary_metadata=summary_metadata,
                )
            )
            self.state.clear_trace()

            if commit_summary and commit_sha:
                try:
                    self.repo.notes_add(commit_sha, commit_summary, namespace="agit/commit-summary")
                    new_session_summary = summarizer.update_session_summary(
                        current_summary=self.state.session_summary,
                        turns=turns,
                        diff=diff_before_commit,
                        commit_summary=commit_summary,
                    )
                    self.state.session_summary = new_session_summary
                    self.state.session_summary_commit = commit_sha
                    self.repo.notes_add(commit_sha, new_session_summary, namespace="agit/session-summary")
                except Exception as error:
                    if self.verbose:
                        print(f"Session summary update failed: {error}")

            print("Created <aGiT> commit.")
        else:
            if self.verbose:
                print("No code changes detected; interaction trace remains pending.")

    def _backend(self):
        backend_class = BACKENDS.get(self.state.backend)
        if backend_class is None:
            raise RuntimeError(f"Unsupported backend: {self.state.backend}")
        return backend_class(self.repo.repo, verbose=self.verbose, backend_args=self.backend_args)

    def _summarizer_backend(self):
        # Summarizer calls run from a scratch cwd, never the repo: a headless
        # run records a real backend session keyed by its working directory,
        # which would otherwise pollute the repo's session list and get picked
        # up as "the previous session" on resume (issues #8/#56).
        from agit.summaries import summary_scratch_dir

        backend_class = BACKENDS.get(self.state.backend)
        if backend_class is None:
            raise RuntimeError(f"Unsupported backend: {self.state.backend}")
        return backend_class(summary_scratch_dir(), verbose=self.verbose)

    def _handle_pre_compaction(self) -> None:
        if self.verbose:
            print("aGiT: Capturing session summary before compaction...")
        try:
            from agit.summaries import Summarizer

            model = self.state.summarization_model or self.global_config.summarization_model
            summarizer = Summarizer(self._summarizer_backend(), model=model)
            session_id = self.state.backend_session_id
            if not session_id:
                return
            from agit.backends.proxy_agents import make_proxy_agent

            proxy_agent = make_proxy_agent(self.state.backend)
            exported = proxy_agent.export_session(self.repo.repo, session_id)
            if not exported or not exported.turns:
                return
            summary = summarizer.summarize_pre_compaction(
                exported_session=exported,
                current_summary=self.state.session_summary,
            )
            self.state.session_summary = summary
            head_sha = self.repo.rev_parse("HEAD")
            if head_sha:
                self.state.session_summary_commit = head_sha
                self.repo.notes_add(head_sha, summary, namespace="agit/session-summary")
            if self.verbose:
                print("aGiT: Session summary captured.")
        except Exception as error:
            if self.verbose:
                print(f"aGiT: Pre-compaction summary failed: {error}")

    def _print_help(self) -> None:
        print("Commands:")
        print("  :help              show this help")
        print("  :status            show git status")
        print("  :user-commit       create a user commit")
        print("  :stage             review and stage untracked files")
        print("  :unstaged          show intentionally unstaged files")
        print(f"  :agent-backend <{'|'.join(BACKENDS)}> select the agent backend")
        print("  :summarizer [on|off|model|status]")
        print("                     manage summarization (on/off, set model, show status)")
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
