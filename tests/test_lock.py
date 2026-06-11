import json
import os
import subprocess
import sys

from agit.git import RepoLock, already_running_message


def test_already_running_message_names_the_pid():
    assert "PID 1234" in already_running_message(1234)
    # Falls back gracefully when the holder's pid is unknown.
    assert "PID" not in already_running_message(None)
    assert "already running on this repo" in already_running_message(None)


def test_acquire_and_release(tmp_path):
    lock = RepoLock(tmp_path / "lock")
    assert lock.acquire() is True
    assert lock.is_held_by_self() is True
    assert (tmp_path / "lock").exists()
    assert lock.owner_pid() == os.getpid()
    lock.release()
    assert lock.is_held_by_self() is False
    # The file persists (it carries no authority — the flock does) but the
    # owner info is cleared, so nothing looks held.
    assert lock.owner_pid() is None


def test_second_holder_is_blocked_by_live_owner(tmp_path):
    path = tmp_path / "lock"
    first = RepoLock(path)
    assert first.acquire() is True
    # A live owner blocks a second acquirer (flock conflicts across fds).
    second = RepoLock(path)
    assert second.acquire() is False
    assert second.is_held_by_self() is False
    first.release()
    # Once released, the second can take it.
    assert second.acquire() is True
    second.release()


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


def test_stale_lock_file_from_dead_pid_does_not_wedge(tmp_path):
    # Issue #23: a leftover file naming a dead (or recycled) pid must never
    # refuse startup — nobody holds the flock, so the lock is simply free.
    path = tmp_path / "lock"
    path.write_text(json.dumps({"pid": 2_147_400_000, "started_at": 0}))
    lock = RepoLock(path)
    assert lock.acquire() is True
    assert lock.owner_pid() == os.getpid()
    lock.release()


def test_leftover_file_naming_a_live_pid_does_not_wedge(tmp_path):
    # Issue #23 (PID reuse): even a file naming a LIVE process is not a lock
    # unless that process actually holds the flock. Before, os.kill(pid, 0)
    # succeeding made a recycled pid hold the repo hostage forever.
    path = tmp_path / "lock"
    path.write_text(json.dumps({"pid": os.getppid(), "started_at": 0}))
    lock = RepoLock(path)
    assert lock.acquire() is True
    lock.release()


def test_corrupt_lock_file_is_ignored(tmp_path):
    path = tmp_path / "lock"
    path.write_text("not json")
    lock = RepoLock(path)
    assert lock.acquire() is True
    lock.release()


def test_release_without_ownership_is_a_noop(tmp_path):
    path = tmp_path / "lock"
    first = RepoLock(path)
    assert first.acquire() is True
    second = RepoLock(path)
    assert second.acquire() is False
    second.release()  # no-op; never owned it
    # The first holder is unaffected: a third acquirer is still refused.
    assert RepoLock(path).acquire() is False
    assert first.owner_pid() == os.getpid()
    first.release()


def test_context_manager(tmp_path):
    path = tmp_path / "lock"
    with RepoLock(path) as lock:
        assert lock.is_held_by_self() is True
        assert path.exists()
    assert lock.is_held_by_self() is False
    assert RepoLock(path).acquire() is True  # free again


def test_lock_released_by_os_when_owner_dies(tmp_path):
    # Issue #23: the kernel must free the lock the instant the holder dies —
    # no stale file, no reclaim step, no second-writer race.
    path = tmp_path / "lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, sys, time\n"
                "from agit.git import RepoLock\n"
                "lock = RepoLock(pathlib.Path(sys.argv[1]))\n"
                "assert lock.acquire()\n"
                "print('held', flush=True)\n"
                "time.sleep(30)\n"
            ),
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        lock = RepoLock(path)
        assert lock.acquire() is False  # blocked by the live holder...
        assert lock.owner_pid() == child.pid  # ...which the message can name
    finally:
        child.kill()
        child.wait()
    assert lock.acquire() is True  # freed by the OS on death, instantly
    lock.release()
