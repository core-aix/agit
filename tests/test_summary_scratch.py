"""Summarizer sessions must never be adopted as the resume session (#56, #8).

A headless summarizer call (``claude -p`` / ``opencode run``) records a real
backend session keyed by its working directory. When the summarizer ran inside
the session worktree, the summary conversation became the worktree's newest
non-empty session — the parse worker and the exit-time adoption then resumed
the *summary* session instead of the user's conversation (entering it on
restart / starting what looked like a brand-new session). These tests pin the
fix: every summarizer backend is constructed with the scratch directory, never
the worktree or the repo.
"""

from agit.config import AgitState
from agit.git import GitRepo
from agit.summaries import summary_scratch_dir

from proxy_helpers import make_runner


def test_summary_scratch_dir_is_under_config_dir_and_outside_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path / "agit-config"))

    scratch = summary_scratch_dir()

    assert scratch == tmp_path / "agit-config" / "summarizer"
    assert scratch.is_dir()  # created so the backend can chdir into it


def test_proxy_summarizer_backend_never_runs_in_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "worktree")
    runner = make_runner(repo=repo, state=AgitState(tmp_path / "worktree"))
    runner.global_config = None
    runner.state.backend = "claude"

    summarizer = runner._make_summarizer()

    assert summarizer is not None
    assert summarizer.backend.repo == summary_scratch_dir()
    assert summarizer.backend.repo != repo.repo  # the leak: cwd = worktree


def test_shell_summarizer_backend_never_runs_in_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path / "agit-config"))
    from agit.shell.runner import AgitShell

    repo = GitRepo.init(tmp_path / "repo")
    shell = AgitShell(repo, backend="claude")

    backend = shell._summarizer_backend()

    assert backend.repo == summary_scratch_dir()
    assert backend.repo != repo.repo
