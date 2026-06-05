import json
import os

from agit.lock import RepoLock


def test_acquire_and_release(tmp_path):
    lock = RepoLock(tmp_path / "lock")
    assert lock.acquire() is True
    assert lock.is_held_by_self() is True
    assert (tmp_path / "lock").exists()
    assert lock.owner_pid() == os.getpid()
    lock.release()
    assert not (tmp_path / "lock").exists()
    assert lock.is_held_by_self() is False


def test_second_holder_is_blocked_by_live_owner(tmp_path):
    path = tmp_path / "lock"
    first = RepoLock(path)
    assert first.acquire() is True
    # A live owner (this very process) blocks a second acquirer.
    second = RepoLock(path)
    assert second.acquire() is False
    assert second.is_held_by_self() is False
    first.release()
    # Once released, the second can take it.
    assert second.acquire() is True


def test_different_repos_get_independent_locks(tmp_path):
    # The lock is per-repo (its path lives under the repo's .agit/), so aGiT on
    # one repo never blocks aGiT on another — only a second instance on the SAME
    # repo is refused.
    repo_a = RepoLock(tmp_path / "a" / ".agit" / "lock")
    repo_b = RepoLock(tmp_path / "b" / ".agit" / "lock")
    assert repo_a.acquire() is True
    assert repo_b.acquire() is True  # a different repo is unaffected
    repo_a.release()
    repo_b.release()


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path):
    path = tmp_path / "lock"
    # Write a lock owned by a almost-certainly-dead pid.
    path.write_text(json.dumps({"pid": 2_147_400_000, "started_at": 0}))
    lock = RepoLock(path)
    assert lock.acquire() is True
    assert lock.owner_pid() == os.getpid()


def test_corrupt_lock_file_is_reclaimed(tmp_path):
    path = tmp_path / "lock"
    path.write_text("not json")
    lock = RepoLock(path)
    assert lock.acquire() is True


def test_release_only_removes_own_lock(tmp_path):
    path = tmp_path / "lock"
    # Lock owned by another (live) pid — our process must not delete it.
    path.write_text(json.dumps({"pid": os.getppid(), "started_at": 0}))
    lock = RepoLock(path)
    assert lock.acquire() is False
    lock.release()  # no-op; we never owned it
    assert path.exists()


def test_context_manager(tmp_path):
    path = tmp_path / "lock"
    with RepoLock(path) as lock:
        assert lock.is_held_by_self() is True
        assert path.exists()
    assert not path.exists()
