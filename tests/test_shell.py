import os
import subprocess
from pathlib import Path

import agit.shell.runner as shell_mod
from agit.backends.base import AgentResult, TokenUsage
from agit.commits import AgitActions
from agit.git import GitRepo
from agit.git import RepoLock
from agit.shell import AgitShell
from agit.config import AgitState


def test_declined_untracked_files_do_not_count_as_pre_agent_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "declined.txt").write_text("declined", encoding="utf-8")
    repo = GitRepo.discover(tmp_path)
    state = AgitState(repo.repo)
    state.add_declined(["declined.txt"])

    shell = AgitShell(repo)

    assert shell.actions.has_pre_agent_user_changes() is False


def test_new_promptable_untracked_files_count_as_pre_agent_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")

    shell = AgitShell(GitRepo.discover(tmp_path))

    assert shell.actions.has_pre_agent_user_changes() is True


def test_second_instance_is_refused(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    repo = GitRepo.discover(tmp_path)
    shell = AgitShell(repo)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda *a, **k: shell.state.backend)
    # Another live aGiT already holds this repo's management lock.
    holder = RepoLock(repo.repo / ".agit" / "lock")
    assert holder.acquire() is True
    # The prompt loop must never start when the repo is already taken.
    shell.prompt = type("P", (), {"prompt": lambda self: (_ for _ in ()).throw(AssertionError("should not prompt"))})()

    shell.run()

    out = capsys.readouterr().out
    assert "already running" in out
    assert str(os.getpid()) in out  # names the holding process's PID
    holder.release()


# --- scripted prompts: `agit --prompt ...` (#53) ------------------------------


class FakeBackend:
    """Headless backend stand-in: every prompt writes one file and succeeds."""

    name = "claude"
    runs: list[str] = []

    def __init__(self, repo, *, verbose=False, backend_args=None):
        self.repo = Path(repo)

    def run(self, prompt, *, model, session_id):
        FakeBackend.runs.append(prompt)
        (self.repo / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        return AgentResult(
            backend=self.name,
            session_id="ses-1",
            model="m",
            final_response="created hello.py",
            exit_code=0,
            tokens=TokenUsage(),
        )


def _no_input(monkeypatch):
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("scripted mode must not prompt"))
    )


def _scripted_shell(tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path / "agit-home"))
    FakeBackend.runs = []
    monkeypatch.setitem(shell_mod.BACKENDS, "claude", FakeBackend)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    _no_input(monkeypatch)
    repo = GitRepo.init(tmp_path / "demo")
    shell = AgitShell(repo, backend="claude", prompts=prompts)
    shell.state.summarization_enabled = False
    return shell, repo


def test_scripted_prompts_commit_without_any_input(tmp_path, monkeypatch, capsys):
    shell, repo = _scripted_shell(tmp_path, monkeypatch, ["write hello.py", ":status"])

    shell.run()

    out = capsys.readouterr().out
    # The new file was auto-staged (no interactive review) and committed.
    assert "Staged untracked files: hello.py" in out
    assert "Created <aGiT> commit." in out
    log = subprocess.run(
        ["git", "-C", str(repo.repo), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert log.startswith("<aGiT> write hello.py")
    assert FakeBackend.runs == ["write hello.py"]  # ':status' is aGiT's, not the agent's


def test_scripted_exit_command_stops_the_script(tmp_path, monkeypatch):
    shell, _ = _scripted_shell(tmp_path, monkeypatch, [":exit", "never reaches the agent"])

    shell.run()

    assert FakeBackend.runs == []


def test_scripted_run_releases_the_repo_lock(tmp_path, monkeypatch):
    shell, repo = _scripted_shell(tmp_path, monkeypatch, ["write hello.py"])

    shell.run()

    assert RepoLock(repo.repo / ".agit" / "lock").acquire() is True


# --- non-interactive actions fall back to safe defaults (#53) -----------------


def test_non_interactive_untracked_review_stages_everything(tmp_path, monkeypatch):
    repo = GitRepo.init(tmp_path)
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    _no_input(monkeypatch)

    actions = AgitActions(repo, AgitState(repo.repo), interactive=False)
    actions.review_untracked(include_declined=False)

    assert repo.has_staged_changes()


def test_non_interactive_user_commit_uses_default_message(tmp_path, monkeypatch):
    repo = GitRepo.init(tmp_path)
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    _no_input(monkeypatch)

    actions = AgitActions(repo, AgitState(repo.repo), interactive=False)
    assert actions.create_user_commit() is True

    log = subprocess.run(
        ["git", "-C", str(repo.repo), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert log.startswith("Save user changes")
