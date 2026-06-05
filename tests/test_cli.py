import subprocess
from pathlib import Path

import pytest

from agit import cli
from agit.git import GitError, GitRepo


def _has_git() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


def test_git_init_seeds_usable_repo(tmp_path):
    (tmp_path / "file.txt").write_text("hello\n", encoding="utf-8")
    repo = GitRepo.init(tmp_path)

    # Valid HEAD (the seed commit) so worktree setup won't choke on an unborn branch.
    assert repo.current_branch() not in ("", "HEAD")
    # The user's pre-existing file is left untracked for aGiT's user-commit flow.
    assert "file.txt" in repo.status_short()


def test_discover_or_init_returns_existing_repo(tmp_path, monkeypatch):
    GitRepo.init(tmp_path)
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "n")
    repo = cli._discover_or_init(tmp_path)
    assert repo is not None
    assert asked == []  # an existing repo is never prompted about


def _force_tty(monkeypatch, stdin: bool, stdout: bool = True):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: stdin, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout, raising=False)


def test_discover_or_init_initializes_when_user_agrees(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None
    assert repo.current_branch() not in ("", "HEAD")  # initialized + seeded


def test_discover_or_init_stops_when_user_declines(tmp_path, monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # default = no

    repo = cli._discover_or_init(tmp_path)

    assert repo is None  # caller exits; aGiT can't run outside a git repo
    assert "cannot run outside a Git repository" in capsys.readouterr().out
    assert not (tmp_path / ".git").exists()  # nothing was created


def test_discover_or_init_non_interactive_does_not_prompt(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._discover_or_init(tmp_path) is None
