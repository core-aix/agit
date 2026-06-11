import os
import subprocess

import agit.shell.runner as shell_mod
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
