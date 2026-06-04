import subprocess

import pytest

from agit.git import GitRepo
from agit.state import AgitState
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


def _integration_runner(main_repo, worktree_repo, base_branch, name):
    from agit.proxy import ProxyRunner

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = main_repo
    runner.repo = worktree_repo
    runner._base_branch = base_branch
    runner.name = name
    runner.worktree = object()  # non-None marks this as a worktree session
    runner.turn = 0
    runner.merge_ctx = None
    runner.master_fd = None
    runner.agent_in_flight = False
    runner.worktree_manager = WorktreeManager(main_repo)
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    return runner


def test_integrate_clean_merge_advances_base(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("session-1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "agent.txt", "agent work\n", "<agent> work")

    runner = _integration_runner(main, work, base, "session-1")
    runner._integrate_session_turn()

    # Base fast-forwarded to include the agent's work.
    assert (main.repo / "agent.txt").exists()
    # The transient t0 branch is gone and the worktree is left detached at base,
    # so a fully-merged session leaves no branch behind.
    assert main.list_branches("agit/") == []
    assert work.is_detached()
    # A fresh turn branch is created only when the agent next commits.
    runner._ensure_turn_branch()
    assert work.current_branch() == "agit/session-1/t1"
    assert runner.turn == 1


def test_integrate_conflict_starts_agent_merge(tmp_path):
    main = _init_repo(tmp_path)  # f.txt == "base\n"
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")  # conflicting base advance
    base_head = main.rev_parse(base)

    runner = _integration_runner(main, work, base, "s1")
    runner._integrate_session_turn()

    # Base untouched; the merge is left in progress for the agent to resolve.
    assert main.rev_parse(base) == base_head
    assert work.merge_in_progress() is True
    assert work.unmerged_paths()  # conflict present
    assert runner.merge_ctx is not None and runner.merge_ctx["source_branch"] == "agit/s1/t0"


def test_finalize_agent_merge_commits_and_advances(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")

    runner = _integration_runner(main, work, base, "s1")
    runner.state = AgitState(info.path)
    runner.backend = type("B", (), {"name": "claude"})()
    runner._integrate_session_turn()  # -> conflict, merge in progress
    assert runner.merge_ctx is not None

    # Simulate the agent resolving the conflict in the worktree.
    (info.path / "f.txt").write_text("resolved: base + worktree\n")

    assert runner._finalize_agent_merge() is True
    # Base advanced to include the resolved merge; the worktree is left detached
    # at base with its turn branch deleted.
    assert (main.repo / "f.txt").read_text() == "resolved: base + worktree\n"
    assert work.is_detached()
    assert main.list_branches("agit/") == []
    assert runner.merge_ctx is None
    # The merge commit is tagged for an agent-resolved merge.
    assert "<agent-merge>" in main._run(["git", "log", "-1", "--format=%s"]).stdout


def test_turn_from_branch():
    from agit.proxy import ProxyRunner

    runner = ProxyRunner.__new__(ProxyRunner)
    assert runner._turn_from_branch("agit/session-1/t0") == 0
    assert runner._turn_from_branch("agit/feature/t5") == 5
    assert runner._turn_from_branch("main") == 0


def test_open_session_worktree_creates_then_reuses(tmp_path):
    from agit.proxy import ProxyRunner

    main = _init_repo(tmp_path)
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = main
    runner._base_branch = main.current_branch()
    runner.worktree_manager = WorktreeManager(main)

    info1, repo1 = runner._open_session_worktree("s1")
    assert info1.path.is_dir()
    assert repo1.current_branch() == "agit/s1/t0"

    # A second call reuses the same worktree (resume across runs) rather than failing.
    info2, repo2 = runner._open_session_worktree("s1")
    assert info2.path == info1.path
    assert repo2.current_branch() == "agit/s1/t0"


def test_worktree_has_pending_work(tmp_path):
    from agit.proxy import ProxyRunner

    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s", base=base)
    repo = GitRepo.discover(info.path)
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = main
    runner._base_branch = base

    assert runner._worktree_has_pending_work(repo, repo.current_branch()) is False
    _commit(repo, "a.txt", "x\n", "work")
    assert runner._worktree_has_pending_work(repo, repo.current_branch()) is True


def test_reconcile_integrates_and_deletes_stale_worktrees(tmp_path):
    from agit.proxy import ProxyRunner

    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    active = wm.create("session-1", base=base)
    wm.create("merged-one", base=base)  # clean, nothing ahead of base
    pending = wm.create("pending-one", base=base)
    _commit(GitRepo.discover(pending.path), "p.txt", "pending\n", "pending work")

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = main
    runner.repo = GitRepo.discover(active.path)  # the active session's worktree
    runner._base_branch = base
    runner.tracking_enabled = True
    runner.worktree = active
    runner.name = "session-1"
    runner.worktree_manager = wm
    messages = []
    runner._set_message = lambda message, **kw: messages.append(message)
    runner._debug = lambda *a, **k: None

    runner._reconcile_sessions_on_startup()

    names = {info.name for info in wm.list()}
    # Stale worktrees are cleaned up; only the active session's worktree remains.
    assert names == {"session-1"}
    # The pending work was integrated into the base before its worktree went away
    # (its Claude conversation persists and stays resumable).
    assert (main.repo / "p.txt").exists()
    # A clean cleanup needs no user attention.
    assert messages == []


def test_reconcile_flags_conflicting_stale_worktree(tmp_path):
    from agit.proxy import ProxyRunner

    main = _init_repo(tmp_path)  # f.txt == "base\n"
    base = main.current_branch()
    wm = WorktreeManager(main)
    active = wm.create("session-1", base=base)
    stale = wm.create("conflict-one", base=base)
    _commit(GitRepo.discover(stale.path), "f.txt", "stale change\n", "stale edit")
    _commit(main, "f.txt", "base change\n", "base edit")  # diverges -> conflict

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = main
    runner.repo = GitRepo.discover(active.path)
    runner._base_branch = base
    runner.tracking_enabled = True
    runner.worktree = active
    runner.name = "session-1"
    runner.worktree_manager = wm
    messages = []
    runner._set_message = lambda message, **kw: messages.append(message)
    runner._debug = lambda *a, **k: None

    runner._reconcile_sessions_on_startup()

    names = {info.name for info in wm.list()}
    # The conflicting worktree is kept (no clean merge) and surfaced to the user.
    assert "conflict-one" in names
    assert messages and "conflict-one" in messages[0]
    # The stale merge attempt left no merge in progress behind.
    assert GitRepo.discover(stale.path).merge_in_progress() is False


def test_ensure_turn_branch_creates_branch_for_detached_session(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    # Simulate a merged/idle session: detached at base with no turn branch.
    work.switch_detach(base)
    work.delete_branch("agit/s1/t0", force=True)
    assert work.is_detached()
    assert main.list_branches("agit/") == []

    runner = _integration_runner(main, work, base, "s1")
    runner._ensure_turn_branch()  # a new prompt arrives -> its own branch
    assert work.current_branch() == "agit/s1/t1"

    # Already on a turn branch: no extra branch is created.
    runner._ensure_turn_branch()
    assert work.current_branch() == "agit/s1/t1"


def test_integrate_session_on_exit_merges_and_deletes_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "a.txt", "x\n", "<agent> work")  # committed but not integrated

    runner = _integration_runner(main, work, base, "s1")
    runner._exiting = True
    runner._integrate_session_on_exit()

    # Work integrated into base; the worktree is detached and its branch gone.
    assert (main.repo / "a.txt").exists()
    assert work.is_detached()
    assert main.list_branches("agit/") == []


def test_integrate_session_on_exit_drops_empty_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)  # on agit/s1/t0 with nothing ahead of base

    runner = _integration_runner(main, work, base, "s1")
    runner._exiting = True
    runner._integrate_session_on_exit()

    assert work.is_detached()
    assert main.list_branches("agit/") == []


def test_active_has_pending_reflects_unintegrated_commits(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)

    runner = _integration_runner(main, work, base, "s1")
    assert runner._active_has_pending() is False
    _commit(work, "a.txt", "x\n", "<agent> work")
    assert runner._active_has_pending() is True


def test_integrate_active_session_clean_merge(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "a.txt", "x\n", "<agent> work")  # an unintegrated commit

    runner = _integration_runner(main, work, base, "s1")
    runner._select_popup = lambda *a, **k: "Merge manually (you resolve here, then Complete merge)"
    runner._integrate_active_session()

    # The clean commit integrated and the base advanced; nothing left pending and
    # no branch lingers (the worktree is detached at base).
    assert (main.repo / "a.txt").exists()
    assert runner._active_has_pending() is False
    assert work.is_detached()
    assert main.list_branches("agit/") == []


def test_session_unintegrated_detects_pending_commits(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)

    runner = _integration_runner(main, work, base, "s1")
    assert runner._session_unintegrated(work) is False
    _commit(work, "a.txt", "x\n", "<agent> work")
    assert runner._session_unintegrated(work) is True
    # After integration the worktree is detached and merged -> nothing pending.
    runner._integrate_session_turn()
    assert runner._session_unintegrated(work) is False


def test_session_unintegrated_flags_conflict_in_progress(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")

    runner = _integration_runner(main, work, base, "s1")
    # Leave a conflicting merge in progress in the worktree.
    assert work.merge(base) is False
    assert runner._session_unintegrated(work) is True


def test_repoint_current_to_base_detaches_at_new_base(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    # A new base branch that has diverged from the old one.
    main.create_branch("release", base)
    main.switch("release")
    _commit(main, "r.txt", "r\n", "release work")
    release_sha = main.rev_parse("release")

    runner = _integration_runner(main, work, base, "s1")
    runner._base_branch = "release"  # base has already been switched
    runner.turn = 3
    runner._repoint_current_to_base()

    # The session keeps its worktree but now sits (detached) on the new base, so
    # its next turn branches from there.
    assert work.is_detached()
    assert work.rev_parse("HEAD") == release_sha
    assert runner.turn == 0


def test_base_switch_candidates_excludes_agit_and_current(tmp_path):
    from agit.proxy import ProxyRunner

    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    WorktreeManager(main).create("s1", base=base)  # creates an agit/s1/t0 branch

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.base_repo = main
    runner._base_branch = base

    candidates = runner._base_switch_candidates()
    assert "feature" in candidates
    assert base not in candidates
    assert all(not name.startswith("agit/") for name in candidates)


def test_log_range_lists_commits(tmp_path):
    repo = _init_repo(tmp_path)
    base_sha = repo.rev_parse("HEAD")
    _commit(repo, "a.txt", "a\n", "add a")
    out = repo.log_range(base_sha, "HEAD", paths=["a.txt"])
    assert "add a" in out
