import subprocess

import pytest

from agit.git import GitRepo
from agit.worktree import WorktreeManager, _sanitize_name


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _commit(repo, name, content, message):
    (repo.repo / name).write_text(content)
    repo.stage_paths([name])
    repo.commit(message)


# --- naming (pure) ---

def test_naming_helpers(tmp_path):
    repo = _init_repo(tmp_path)
    wm = WorktreeManager(repo)
    assert wm.worktree_path("feat x").name == "feat-x"
    assert wm.turn_branch("feat x", 2) == "agit/feat-x/t2"
    assert wm.branch_prefix("feat x") == "agit/feat-x/"
    assert wm.is_agit_branch("agit/feat-x/t0") is True
    assert wm.is_agit_branch("main") is False
    assert _sanitize_name("  ") == "session"


# --- worktree lifecycle against real git ---

def test_create_list_remove_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    wm = WorktreeManager(repo)
    info = wm.create("feat", base="HEAD")
    assert info.path.is_dir()
    assert info.branch == "agit/feat/t0"
    listed = wm.list()
    assert [w.name for w in listed] == ["feat"]
    assert listed[0].branch == "agit/feat/t0"

    wm.remove("feat")
    assert not info.path.exists()
    assert "agit/feat/t0" not in repo.list_branches("agit/")


def test_turn_branches_coexist_without_df_conflict(tmp_path):
    repo = _init_repo(tmp_path)
    wm = WorktreeManager(repo)
    info = wm.create("feat", base="HEAD")
    work = GitRepo.discover(info.path)
    # A second turn branch must be creatable alongside the first.
    work.switch("agit/feat/t1", create=True, base="HEAD")
    assert "agit/feat/t1" in repo.list_branches("agit/")


# --- merge behaviour against real git ---

def test_merge_clean(tmp_path):
    repo = _init_repo(tmp_path)
    base = repo.current_branch()
    repo.create_branch("topic", base)
    repo.switch("topic")
    _commit(repo, "new.txt", "hello\n", "add new")
    repo.switch(base)
    assert repo.merge("topic") is True
    assert (repo.repo / "new.txt").exists()


def test_merge_conflict_reports_and_aborts(tmp_path):
    repo = _init_repo(tmp_path)
    base = repo.current_branch()
    repo.create_branch("topic", base)
    repo.switch("topic")
    _commit(repo, "f.txt", "topic change\n", "topic edit")
    repo.switch(base)
    _commit(repo, "f.txt", "base change\n", "base edit")
    assert repo.merge("topic") is False
    assert "f.txt" in repo.unmerged_paths()
    repo.merge_abort()
    assert repo.unmerged_paths() == []


def test_log_range_lists_commits(tmp_path):
    repo = _init_repo(tmp_path)
    base_sha = repo.rev_parse("HEAD")
    _commit(repo, "a.txt", "a\n", "add a")
    out = repo.log_range(base_sha, "HEAD", paths=["a.txt"])
    assert "add a" in out
