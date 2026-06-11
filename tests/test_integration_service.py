"""Unit tests for IntegrationService and MergeContext / MergePhase (#29 P5).

All tests construct IntegrationService and MergeContext directly; none use
ProxyRunner.__new__ except where testing the delegation from runner stubs.
Real git repos (via tmp_path) are used where git semantics must be verified;
pure-logic tests use lightweight fakes.
"""
import subprocess
import time
import types

import pytest

from agit.git import GitRepo
from agit.proxy.integration import (
    CONFLICT,
    INTEGRATED,
    SKIP,
    IntegrationService,
    MergeContext,
    MergePhase,
)
from agit.git import WorktreeManager
from proxy_helpers import make_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_session(main, name, base, *, backend="test", turn=1):
    wm = WorktreeManager(main)
    info = wm.create(name, base=base)
    work = GitRepo.discover(info.path)
    work.switch(wm.turn_branch(name, turn, backend=backend), create=True)
    return info, work


def _svc(main_repo, base_branch):
    return IntegrationService(main_repo, base_branch)


# ---------------------------------------------------------------------------
# MergeContext / MergePhase
# ---------------------------------------------------------------------------


def test_merge_context_initial_state():
    ctx = MergeContext(source_branch="agit/s1/t1", context="some commits")
    assert ctx.phase is MergePhase.PENDING
    assert ctx.auto_tried is False
    assert ctx.prompt_sent_at is None
    assert ctx.source_branch == "agit/s1/t1"
    assert ctx.context == "some commits"


def test_merge_context_dict_shim_get():
    ctx = MergeContext(source_branch="b", context="c", auto_tried=True)
    assert ctx.get("auto_tried") is True
    assert ctx.get("prompt_sent_at") is None
    assert ctx.get("missing_key", "default") == "default"


def test_merge_context_dict_shim_getitem_setitem():
    ctx = MergeContext(source_branch="b", context="")
    ctx["prompt_sent_at"] = 1.23
    assert ctx.prompt_sent_at == 1.23
    assert ctx["prompt_sent_at"] == 1.23


def test_merge_context_dict_shim_contains():
    ctx = MergeContext(source_branch="b", context="")
    assert "source_branch" in ctx
    assert "nonexistent" not in ctx


def test_merge_phase_values():
    assert MergePhase.PENDING.value == "pending"
    assert MergePhase.RESOLVING.value == "resolving"
    assert MergePhase.MANUAL.value == "manual"


def test_merge_context_manual_phase():
    ctx = MergeContext(source_branch="b", context="", phase=MergePhase.MANUAL, auto_tried=True)
    assert ctx.phase is MergePhase.MANUAL
    assert ctx.auto_tried is True


# ---------------------------------------------------------------------------
# turn_from_branch
# ---------------------------------------------------------------------------


def test_turn_from_branch_parses_number():
    svc = IntegrationService.__new__(IntegrationService)
    assert svc.turn_from_branch("agit/claude/session-1/t3") == 3
    assert svc.turn_from_branch("agit/session-1/t0") == 0
    assert svc.turn_from_branch("main") == 0
    assert svc.turn_from_branch("") == 0


# ---------------------------------------------------------------------------
# ensure_turn_branch
# ---------------------------------------------------------------------------


def test_ensure_turn_branch_creates_branch_on_detached(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    assert work.is_detached()

    svc = _svc(main, base)
    new_turn = svc.ensure_turn_branch(
        repo=work,
        worktree=info,
        turn=0,
        worktree_manager=wm,
        session_name="s1",
        backend_name="test",
    )
    assert new_turn == 1
    assert work.current_branch() == "agit/test/s1/t1"


def test_ensure_turn_branch_skips_if_not_detached(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    work.switch(wm.turn_branch("s1", 1, backend="test"), create=True)
    assert not work.is_detached()

    svc = _svc(main, base)
    new_turn = svc.ensure_turn_branch(work, info, 1, wm, "s1", "test")
    assert new_turn == 1  # unchanged
    assert work.current_branch() == "agit/test/s1/t1"


def test_ensure_turn_branch_skips_occupied_numbers(tmp_path):
    """Never reuse a branch that already exists (holds unintegrated work)."""
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    # Create branches t1 and t2 manually (simulate existing unintegrated work).
    work.switch(wm.turn_branch("s1", 1, backend="test"), create=True)
    _commit(work, "a.txt", "x\n", "work on t1")
    work.switch_detach(base)
    work.switch(wm.turn_branch("s1", 2, backend="test"), create=True, base="HEAD")
    _commit(work, "b.txt", "y\n", "work on t2")
    work.switch_detach(base)

    svc = _svc(main, base)
    new_turn = svc.ensure_turn_branch(work, info, 0, wm, "s1", "test")
    assert new_turn == 3  # skipped t1 and t2
    assert work.current_branch() == "agit/test/s1/t3"


def test_ensure_turn_branch_skips_if_no_worktree(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    svc = _svc(main, base)
    # Use the main repo itself as the "session repo"
    new_turn = svc.ensure_turn_branch(main, None, 5, wm, "s1", "test")
    assert new_turn == 5  # unchanged, no branch created


# ---------------------------------------------------------------------------
# integrate_turn_or_conflict
# ---------------------------------------------------------------------------


def test_integrate_turn_clean_merge(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "<agent> work")

    svc = _svc(main, base)
    result, branch = svc.integrate_turn_or_conflict(
        repo=work,
        name="s1",
        worktree=object(),
        merge_ctx=None,
        integration_paused=False,
    )
    assert result == INTEGRATED
    assert branch == "agit/test/s1/t1"


def test_integrate_turn_conflict(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "f.txt", "session change\n", "wt")
    _commit(main, "f.txt", "base change\n", "base")  # conflicting

    svc = _svc(main, base)
    result, branch = svc.integrate_turn_or_conflict(work, "s1", object(), None, False)
    assert result == CONFLICT
    assert branch == "agit/test/s1/t1"
    assert not work.merge_in_progress()  # aborted cleanly


def test_integrate_turn_skips_when_no_worktree(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    svc = _svc(main, base)
    result, branch = svc.integrate_turn_or_conflict(main, "s", None, None, False)
    assert result == SKIP
    assert branch == ""


def test_integrate_turn_skips_when_paused(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    svc = _svc(main, base)
    result, _ = svc.integrate_turn_or_conflict(work, "s1", object(), None, integration_paused=True)
    assert result == SKIP


def test_integrate_turn_skips_when_merge_ctx_active(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    svc = _svc(main, base)
    ctx = MergeContext(source_branch="agit/test/s1/t1", context="")
    result, _ = svc.integrate_turn_or_conflict(work, "s1", object(), ctx, False)
    assert result == SKIP


def test_integrate_turn_skips_non_agit_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    # main is on 'main' / 'master', not agit/...
    svc = _svc(main, base)
    result, _ = svc.integrate_turn_or_conflict(main, "s", object(), None, False)
    assert result == SKIP


# ---------------------------------------------------------------------------
# advance_base_to
# ---------------------------------------------------------------------------


def test_advance_base_to_fast_forwards_base(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    turn = work.current_branch()
    # merge base into turn first (clean merge)
    assert work.merge(base) is True

    svc = _svc(main, base)
    svc.advance_base_to(work, turn)

    assert (main.repo / "a.txt").exists()
    assert work.is_detached()
    assert turn not in main.list_branches("agit/")


def test_advance_base_refuses_wrong_base_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("other", base)
    main.switch("other")  # base_repo is now on "other"

    svc = _svc(main, base)  # service thinks base is still "main/master"
    with pytest.raises(RuntimeError, match="not the integration branch"):
        svc.advance_base_to(main, "some-branch")


# ---------------------------------------------------------------------------
# align_session_to_base
# ---------------------------------------------------------------------------


def test_align_repoints_clean_worktree(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    # advance base out-of-band
    _commit(main, "new.txt", "x\n", "base advance")

    svc = _svc(main, base)
    svc.align_session_to_base(work)
    assert work.rev_parse("HEAD") == main.rev_parse(base)


def test_align_leaves_dirty_worktree_untouched(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    # dirty working tree
    (info.path / "dirty.txt").write_text("uncommitted\n")
    head_before = work.rev_parse("HEAD")
    _commit(main, "new.txt", "x\n", "base advance")

    svc = _svc(main, base)
    svc.align_session_to_base(work)
    assert work.rev_parse("HEAD") == head_before  # not touched


def test_align_merges_base_into_worktree_with_own_commits(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "session.txt", "session work\n", "session work")
    _commit(main, "base.txt", "base new\n", "base advance")

    svc = _svc(main, base)
    svc.align_session_to_base(work)
    # Base commits merged into session branch
    assert (work.repo / "base.txt").exists()
    assert not work.merge_in_progress()


# ---------------------------------------------------------------------------
# session_unintegrated / active_has_pending
# ---------------------------------------------------------------------------


def test_session_unintegrated_when_ahead_of_base(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    svc = _svc(main, base)
    assert svc.session_unintegrated(work) is True


def test_session_unintegrated_false_when_merged(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    # No commits ahead of base; detached
    svc = _svc(main, base)
    assert svc.session_unintegrated(work) is False


def test_session_unintegrated_true_for_none(tmp_path):
    main = _init_repo(tmp_path)
    svc = _svc(main, main.current_branch())
    assert svc.session_unintegrated(None) is True


def test_active_has_pending_true(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    svc = _svc(main, base)
    assert svc.active_has_pending(work, object()) is True


def test_active_has_pending_false_when_clean(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("s1", base=base)
    work = GitRepo.discover(info.path)
    svc = _svc(main, base)
    assert svc.active_has_pending(work, info) is False


# ---------------------------------------------------------------------------
# worktree_has_pending_work
# ---------------------------------------------------------------------------


def test_worktree_has_pending_work_false_clean(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    svc = _svc(main, base)
    assert svc.worktree_has_pending_work(work, work.current_branch()) is False


def test_worktree_has_pending_work_true_with_commit(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    svc = _svc(main, base)
    assert svc.worktree_has_pending_work(work, work.current_branch()) is True


# ---------------------------------------------------------------------------
# should_auto_complete_merge
# ---------------------------------------------------------------------------


def test_should_auto_complete_merge_all_conditions_met():
    now = time.monotonic()
    ctx = MergeContext(source_branch="b", context="", auto_tried=False, prompt_sent_at=now - 10)
    svc = IntegrationService.__new__(IntegrationService)
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 7, child_idle_seconds=4) is True


def test_should_auto_complete_merge_auto_tried_blocks():
    now = time.monotonic()
    ctx = MergeContext(source_branch="b", context="", auto_tried=True, prompt_sent_at=now - 10)
    svc = IntegrationService.__new__(IntegrationService)
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 7, child_idle_seconds=4) is False


def test_should_auto_complete_merge_no_prompt_sent_at():
    now = time.monotonic()
    ctx = MergeContext(source_branch="b", context="", auto_tried=False, prompt_sent_at=None)
    svc = IntegrationService.__new__(IntegrationService)
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 7, child_idle_seconds=4) is False


def test_should_auto_complete_merge_agent_not_responded():
    now = time.monotonic()
    # prompt_sent_at is AFTER last_child_output → agent hasn't responded
    ctx = MergeContext(source_branch="b", context="", auto_tried=False, prompt_sent_at=now - 3)
    svc = IntegrationService.__new__(IntegrationService)
    # last_child_output = now - 5 < prompt_sent_at ... wait, need output AFTER sent_at
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 10, child_idle_seconds=4) is False


def test_should_auto_complete_merge_not_yet_idle():
    now = time.monotonic()
    ctx = MergeContext(source_branch="b", context="", auto_tried=False, prompt_sent_at=now - 10)
    svc = IntegrationService.__new__(IntegrationService)
    # last_child_output is very recent (agent still working)
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 1, child_idle_seconds=4) is False


# ---------------------------------------------------------------------------
# base_switch_candidates
# ---------------------------------------------------------------------------


def test_base_switch_candidates_excludes_agit_and_current(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    main.create_branch("agit/claude/s1/t1", base)

    svc = _svc(main, base)
    candidates = svc.base_switch_candidates()
    assert "feature" in candidates
    assert base not in candidates
    assert "agit/claude/s1/t1" not in candidates


# ---------------------------------------------------------------------------
# repoint_to_base
# ---------------------------------------------------------------------------


def test_repoint_to_base_detaches_session(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    # switch to new base
    _commit(main, "n.txt", "x\n", "base advance")
    main.create_branch("new-base", base)
    main.switch("new-base")

    svc = _svc(main, "new-base")
    new_turn = svc.repoint_to_base(work, object())
    assert new_turn == 0
    assert work.rev_parse("HEAD") == main.rev_parse("new-base")


def test_repoint_to_base_skips_dirty(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    _, work = _make_session(main, "s1", base)
    (work.repo / "dirty.txt").write_text("uncommitted\n")

    svc = _svc(main, base)
    result = svc.repoint_to_base(work, object())
    assert result is None


# ---------------------------------------------------------------------------
# check_base_drift
# ---------------------------------------------------------------------------


def test_check_base_drift_pauses_when_drifted(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("other", base)
    main.switch("other")  # base_repo now on "other", but svc thinks target is base

    svc = _svc(main, base)
    paused, _, message = svc.check_base_drift(
        base_branch=base,
        integration_paused=False,
        last_check_at=0.0,
        drift_check_seconds=0.0,
    )
    assert paused is True
    assert message is not None
    assert "PAUSED" in message


def test_check_base_drift_resumes_when_restored(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    # base_repo is already on base; was previously paused

    svc = _svc(main, base)
    paused, _, message = svc.check_base_drift(
        base_branch=base,
        integration_paused=True,  # was paused
        last_check_at=0.0,
        drift_check_seconds=0.0,
    )
    assert paused is False
    assert message is not None
    assert "resumed" in message


def test_check_base_drift_throttled():
    svc = IntegrationService.__new__(IntegrationService)
    # last_check_at is very recent → should not run
    paused, check_at, message = svc.check_base_drift(
        base_branch="main",
        integration_paused=False,
        last_check_at=time.monotonic(),
        drift_check_seconds=999.0,
    )
    assert message is None
    assert paused is False


# ---------------------------------------------------------------------------
# poll_base_advanced
# ---------------------------------------------------------------------------


def test_poll_base_advanced_detects_change(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    svc = _svc(main, base)
    old_head = main.rev_parse(base)
    _commit(main, "n.txt", "x\n", "advance")
    new_head = main.rev_parse(base)

    head, _, advanced = svc.poll_base_advanced(
        worktree=object(),
        last_base_head=old_head,
        last_poll_at=0.0,
        base_poll_seconds=0.0,
    )
    assert advanced is True
    assert head == new_head


def test_poll_base_advanced_noop_without_worktree(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    svc = _svc(main, base)
    head, poll_at, advanced = svc.poll_base_advanced(
        worktree=None,
        last_base_head=None,
        last_poll_at=0.0,
        base_poll_seconds=0.0,
    )
    assert advanced is False
    assert head is None


def test_poll_base_advanced_throttled(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    svc = _svc(main, base)
    _, _, advanced = svc.poll_base_advanced(
        worktree=object(),
        last_base_head="some-sha",
        last_poll_at=time.monotonic(),
        base_poll_seconds=999.0,
    )
    assert advanced is False


# ---------------------------------------------------------------------------
# merge_resolution_prompt
# ---------------------------------------------------------------------------


def test_merge_resolution_prompt_contains_base_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    svc = _svc(main, base)
    # Use a fake repo stub (no actual git conflict needed to test the prompt text)
    fake_repo = types.SimpleNamespace(
        unmerged_paths=lambda: ["conflict.txt"],
    )
    prompt = svc.merge_resolution_prompt(fake_repo, "agit/test/s1/t1")
    assert base in prompt
    assert "conflict.txt" in prompt
    assert "<<<<<<< / ======= / >>>>>>>" in prompt
    assert "aGiT" in prompt


# ---------------------------------------------------------------------------
# delete_orphan_merged_branches
# ---------------------------------------------------------------------------


def test_delete_orphan_merged_branches_removes_merged(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    # Create an agit branch, merge it into base, but leave it around
    main.create_branch("agit/test/s1/t1", base)
    main.switch("agit/test/s1/t1")
    _commit(main, "orphan.txt", "x\n", "orphan work")
    branch = main.current_branch()
    main.switch(base)
    main.merge_ff_only(branch)
    # branch is now merged into base; no worktree checks it out

    svc = _svc(main, base)
    svc.delete_orphan_merged_branches()
    assert branch not in main.list_branches("agit/")


def test_delete_orphan_merged_branches_keeps_unmerged(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("agit/test/s1/t1", base)
    main.switch("agit/test/s1/t1")
    _commit(main, "new.txt", "x\n", "unmerged work")
    branch = main.current_branch()
    main.switch(base)
    # NOT merged into base

    svc = _svc(main, base)
    svc.delete_orphan_merged_branches()
    assert branch in main.list_branches("agit/")  # kept


# ---------------------------------------------------------------------------
# MergePhase state-machine pin tests (Finding 4)
# ---------------------------------------------------------------------------
#
# These tests pin the three key gating properties of the MergeContext /
# MergePhase design:
#  1. MANUAL contexts (auto_tried=True at creation) never auto-finalize.
#  2. AUTO contexts auto-finalize exactly ONCE after idle (subsequent calls
#     are blocked by auto_tried=True).
#  3. PENDING contexts (prompt_sent_at=None, Enter not yet sent) never
#     auto-finalize regardless of idle time.
# ---------------------------------------------------------------------------


def test_manual_context_never_auto_finalizes():
    """A MANUAL merge context (user resolves; auto_tried=True) must never
    satisfy should_auto_complete_merge regardless of timing."""
    svc = IntegrationService.__new__(IntegrationService)
    now = time.monotonic()
    # Simulate everything else looking like it should fire: prompt sent,
    # agent responded, plenty of idle time — but it's a MANUAL context.
    ctx = MergeContext(
        source_branch="agit/s/t1",
        context="",
        phase=MergePhase.MANUAL,
        auto_tried=True,  # MANUAL always starts True
        prompt_sent_at=now - 30,
    )
    # Even with last_child_output well before now (long idle), must not fire.
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 20, child_idle_seconds=4) is False


def test_auto_context_finalizes_once_after_idle():
    """An AUTO context should satisfy should_auto_complete_merge exactly once;
    after auto_tried is set to True, subsequent calls return False."""
    svc = IntegrationService.__new__(IntegrationService)
    now = time.monotonic()
    ctx = MergeContext(
        source_branch="agit/s/t1",
        context="",
        phase=MergePhase.RESOLVING,
        auto_tried=False,
        prompt_sent_at=now - 20,
    )
    # Conditions met: agent responded (last_child_output > prompt_sent_at) and idle.
    last_output = now - 10  # responded 10s ago, idle > 4+2=6s
    assert svc.should_auto_complete_merge(ctx, last_child_output=last_output, child_idle_seconds=4) is True

    # Simulate the runner setting auto_tried after the first attempt.
    ctx.auto_tried = True

    # Second call must not fire.
    assert svc.should_auto_complete_merge(ctx, last_child_output=last_output, child_idle_seconds=4) is False


def test_pending_context_never_auto_finalizes():
    """A PENDING context (prompt_sent_at=None; Enter not yet sent) must never
    satisfy should_auto_complete_merge, even if idle time is met."""
    svc = IntegrationService.__new__(IntegrationService)
    now = time.monotonic()
    ctx = MergeContext(
        source_branch="agit/s/t1",
        context="",
        phase=MergePhase.PENDING,
        auto_tried=False,
        prompt_sent_at=None,  # Enter has NOT been sent yet
    )
    # Even with substantial idle time, should not fire (no Enter sent).
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 20, child_idle_seconds=4) is False


def test_auto_context_not_idle_yet_does_not_finalize():
    """An AUTO context with prompt sent and agent responded but not yet
    sufficiently idle must not auto-finalize."""
    svc = IntegrationService.__new__(IntegrationService)
    now = time.monotonic()
    ctx = MergeContext(
        source_branch="agit/s/t1",
        context="",
        phase=MergePhase.RESOLVING,
        auto_tried=False,
        prompt_sent_at=now - 10,
    )
    # Agent responded 2s ago — well within CHILD_IDLE_SECONDS+2 = 6s threshold.
    assert svc.should_auto_complete_merge(ctx, last_child_output=now - 2, child_idle_seconds=4) is False


def _flush_runner(ctx):
    """Minimal runner whose _flush_pending_enter is exercised for real: the
    pending Enter targets a live pipe fd so the write succeeds."""
    import os
    import time

    from proxy_helpers import make_runner

    read_fd, write_fd = os.pipe()
    runner = make_runner(
        master_fd=write_fd,
        merge_ctx=ctx,
        _pending_enter_at=time.monotonic() - 1.0,  # due
        _pending_enter_fd=write_fd,
    )
    return runner, read_fd, write_fd


def test_manual_context_phase_never_promoted_by_flush():
    """The real _flush_pending_enter must not promote a MANUAL context."""
    import os

    ctx = MergeContext(source_branch="agit/s/t1", context="", phase=MergePhase.MANUAL, auto_tried=True)
    runner, read_fd, write_fd = _flush_runner(ctx)
    try:
        runner._flush_pending_enter()
        assert ctx.phase is MergePhase.MANUAL  # unchanged by the flush
        assert ctx.prompt_sent_at is not None  # the Enter itself was recorded
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_pending_context_phase_promoted_to_resolving_by_flush():
    """The real _flush_pending_enter promotes a PENDING (auto) context."""
    import os

    ctx = MergeContext(source_branch="agit/s/t1", context="", phase=MergePhase.PENDING, auto_tried=False)
    runner, read_fd, write_fd = _flush_runner(ctx)
    try:
        runner._flush_pending_enter()
        assert ctx.phase is MergePhase.RESOLVING
        assert ctx.prompt_sent_at is not None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_maybe_complete_agent_merge_spends_the_auto_attempt():
    """runner._maybe_complete_agent_merge must write auto_tried=True BEFORE
    finalizing, so a failed finalize is not retried on every loop tick."""
    import time

    from agit.proxy.integration import IntegrationService

    now = time.monotonic()
    ctx = MergeContext(
        source_branch="agit/s/t1",
        context="",
        phase=MergePhase.RESOLVING,
        auto_tried=False,
        prompt_sent_at=now - 20,
    )
    runner = make_runner(
        merge_ctx=ctx,
        last_child_output=now - 10,  # responded after the prompt, then idle
    )
    runner._integration = IntegrationService.__new__(IntegrationService)
    finalized = []
    runner._finalize_agent_merge = lambda: finalized.append(1) and False

    runner._maybe_complete_agent_merge()
    assert finalized == [1]
    assert ctx.auto_tried is True  # the attempt is spent even though finalize failed

    runner._maybe_complete_agent_merge()
    assert finalized == [1]  # never retried
