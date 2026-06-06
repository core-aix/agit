from __future__ import annotations

from agit.backends.base import TokenUsage
from agit.commit_message import build_agent_commit_message, build_user_commit_message
from agit.git import GitRepo
from agit.opencode_session import SessionTurn
from agit.state import AgitState


class AgitActions:
    def __init__(self, repo: GitRepo, state: AgitState, *, verbose: bool = False) -> None:
        self.repo = repo
        self.state = state
        self.verbose = verbose

    def create_user_commit(self) -> bool:
        self.repo.add_tracked()
        self.review_untracked(include_declined=False)
        if not self.repo.has_staged_changes():
            if self.verbose:
                print("No staged user changes to commit.")
            return False
        message = ""
        while not message.strip():
            message = input("User commit message: ")
            if not message.strip():
                print("User commit message is required.")
        self.repo.commit(build_user_commit_message(message=message, agit_session_id=self.state.session_id))
        self.state.clear_trace()
        print("Created user commit.")
        return True

    def create_agent_commit_from_turns(
        self,
        *,
        turns: list[SessionTurn],
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        quiet: bool = False,
    ) -> bool:
        if not turns:
            return False
        for turn in turns:
            if turn.user_prompt:
                self.state.append_trace("user", turn.user_prompt)
            if turn.final_response:
                self.state.append_trace("agent", turn.final_response)
            self.state.add_token_usage(turn.tokens)

        self.repo.add_tracked()
        self.review_untracked(include_declined=False)
        if not self.repo.has_staged_changes():
            return False

        # Subject lists every prompt that led to this commit, joined by " / ".
        prompts = [turn.user_prompt for turn in turns if turn.user_prompt]
        subject_text = " / ".join(prompts) if prompts else f"{backend} changes"
        message = build_agent_commit_message(
            latest_prompt=subject_text,
            trace=self.state.pending_trace(),
            backend=backend,
            backend_session_id=backend_session_id,
            agit_session_id=self.state.session_id,
            model=model or self.state.model,
            token_usage=self.state.pending_token_usage(),
            trace_turn_limit=self.state.trace_turn_limit,
        )
        self.repo.commit(message)
        self.state.clear_trace()
        if not quiet:
            print("Created <agent> commit.")
        return True

    def review_untracked(self, *, include_declined: bool) -> None:
        untracked = self.repo.untracked_files()
        declined = set(self.state.declined_untracked())
        candidates = untracked if include_declined else [path for path in untracked if path not in declined]
        if not candidates:
            return

        print("Untracked files:")
        for index, path in enumerate(candidates, start=1):
            print(f"  {index}. {path}")
        answer = input("Stage untracked files? [y/N/select]: ").strip().lower()
        if answer in {"y", "yes"}:
            self.repo.stage_paths(candidates)
            self.state.remove_declined(candidates)
            return
        if answer in {"s", "select"}:
            selected = self._select_paths(candidates)
            if selected:
                self.repo.stage_paths(selected)
                self.state.remove_declined(selected)
            declined_now = [path for path in candidates if path not in selected]
            if declined_now:
                self.state.add_declined(declined_now)
            return
        self.state.add_declined(candidates)

    def has_pre_agent_user_changes(self) -> bool:
        declined = set(self.state.declined_untracked())
        untracked = self.repo.untracked_files()
        self.state.keep_declined(untracked)
        promptable_untracked = [path for path in untracked if path not in declined]
        return self.repo.has_tracked_changes() or bool(promptable_untracked)

    def _select_paths(self, candidates: list[str]) -> list[str]:
        raw = input("Enter numbers to stage, separated by spaces: ").strip()
        selected: list[str] = []
        for item in raw.split():
            if item.isdigit() and 1 <= int(item) <= len(candidates):
                selected.append(candidates[int(item) - 1])
        return selected
