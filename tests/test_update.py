import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agit import cli
from agit.config import DEFAULT_TIMINGS, GlobalConfig
from agit.update import KIND_PACKAGE, KIND_SOURCE, UpdateStatus, Updater
from agit.update.updater import _version_tuple
from proxy_helpers import make_runner


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)


def _commit(path: Path, name: str, content: str, message: str) -> None:
    (path / name).write_text(content)
    _git(["add", name], path)
    _git(["commit", "-qm", message], path)


@pytest.fixture
def source_clone(tmp_path: Path):
    """A 'remote' repo and a clone of it whose `main` tracks `origin/main` —
    the shape of a source-linked aGiT install. Returns (remote, clone)."""
    remote = tmp_path / "remote"
    _init_repo(remote)
    _commit(remote, "agit.py", "v1\n", "first")
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(clone)], tmp_path)
    _git(["config", "user.email", "t@t"], clone)
    _git(["config", "user.name", "t"], clone)
    return remote, clone


# --- version comparison (pure) ---------------------------------------------


def test_version_tuple_orders_numerically():
    assert _version_tuple("1.2.3") == (1, 2, 3)
    assert _version_tuple("10.0") > _version_tuple("9.9")
    # Non-numeric trailers fall back to the leading integer.
    assert _version_tuple("1.2.3rc1") == (1, 2, 3)
    assert _version_tuple("2.0.0") > _version_tuple("1.9.9")


# --- install-kind detection -------------------------------------------------


def test_kind_is_source_when_source_repo_present(source_clone):
    _, clone = source_clone
    assert Updater(source_repo=clone).kind == KIND_SOURCE


def test_kind_is_package_without_source_repo():
    assert Updater(source_repo=None).kind == KIND_PACKAGE


# --- source check -----------------------------------------------------------


def test_source_check_reports_no_update_when_in_sync(source_clone):
    _, clone = source_clone
    status = Updater(source_repo=clone).check()
    assert status.ok
    assert status.available is False
    assert status.behind == 0


def test_source_check_detects_upstream_commits(source_clone):
    remote, clone = source_clone
    # Advance the remote by two commits; the clone is now behind.
    _commit(remote, "agit.py", "v2\n", "second")
    _commit(remote, "agit.py", "v3\n", "third")
    status = Updater(source_repo=clone).check()
    assert status.ok
    assert status.available is True
    assert status.behind == 2
    assert "2 new commits" in status.message


def test_source_check_errors_without_upstream(tmp_path: Path):
    repo = tmp_path / "solo"
    _init_repo(repo)
    _commit(repo, "agit.py", "v1\n", "first")  # main has no upstream
    status = Updater(source_repo=repo).check()
    assert not status.ok
    assert status.available is False
    assert "upstream" in (status.error or "")


# --- source apply -----------------------------------------------------------


def test_source_apply_fast_forwards(source_clone):
    remote, clone = source_clone
    _commit(remote, "agit.py", "v2\n", "second")
    updater = Updater(source_repo=clone)
    assert updater.check().available
    result = updater.apply()
    assert result.ok, result.error
    # The clone now carries the remote's content and is back in sync.
    assert (clone / "agit.py").read_text() == "v2\n"
    assert updater.check().available is False


def test_source_apply_refuses_dirty_tree(source_clone):
    remote, clone = source_clone
    _commit(remote, "agit.py", "v2\n", "second")
    (clone / "agit.py").write_text("local edit\n")  # uncommitted local change
    result = Updater(source_repo=clone).apply()
    assert not result.ok
    assert "uncommitted" in (result.error or "")
    assert (clone / "agit.py").read_text() == "local edit\n"  # untouched


def test_source_apply_refuses_diverged_branch(source_clone):
    remote, clone = source_clone
    _commit(remote, "agit.py", "v2\n", "remote change")
    _commit(clone, "local.py", "x\n", "local change")  # clone diverged, clean tree
    result = Updater(source_repo=clone).apply()
    assert not result.ok
    assert "diverged" in (result.error or "")


# --- package path (mocked index) -------------------------------------------


def test_package_check_available(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "_latest_package_version", lambda: "1.2.0")
    status = updater.check()
    assert status.kind == KIND_PACKAGE
    assert status.available is True
    assert status.current == "1.0.0" and status.latest == "1.2.0"


def test_package_check_up_to_date(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.2.0")
    monkeypatch.setattr(updater, "_latest_package_version", lambda: "1.2.0")
    assert updater.check().available is False


def test_package_check_errors_when_index_unreachable(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "_latest_package_version", lambda: None)
    status = updater.check()
    assert not status.ok


# --- config -----------------------------------------------------------------


def test_check_for_updates_defaults_on_and_persists(tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "config.json")
    assert config.check_for_updates is True
    config.check_for_updates = False
    assert GlobalConfig(path=tmp_path / "config.json").check_for_updates is False


def test_update_check_seconds_timing_default():
    assert DEFAULT_TIMINGS["update_check_seconds"] == 300.0


# --- runner integration -----------------------------------------------------


class _FakeUpdater:
    def __init__(self, status: UpdateStatus):
        self._status = status
        self.applied = False

    def check(self) -> UpdateStatus:
        return self._status

    def apply(self) -> UpdateStatus:
        self.applied = True
        return UpdateStatus(kind=self._status.kind, message="updated", current="new")


def _available_status() -> UpdateStatus:
    return UpdateStatus(
        kind=KIND_SOURCE,
        available=True,
        current="aaaaaaa",
        latest="bbbbbbb",
        behind=1,
        message="aGiT update available: 1 new commit on origin/main (aaaaaaa → bbbbbbb).",
    )


def test_consume_update_result_notifies_once():
    runner = make_runner()
    runner._update_status = None
    runner._update_check_thread = None
    runner._update_worker_result = _available_status()

    class _DoneThread:
        def is_alive(self):
            return False

    runner._update_check_thread = _DoneThread()
    runner._consume_update_check_result()
    assert runner._update_status is not None and runner._update_status.available
    assert runner._update_offered is True
    assert "update available" in (runner.message or "")
    # A second consume with the same (already offered) status does not re-notify.
    runner.message = None
    runner._consume_update_check_result()
    assert runner.message is None


def test_ready_for_update_blocked_by_in_flight_agent():
    runner = make_runner()
    assert runner._ready_for_update() is True
    runner.active.agent_in_flight = True
    assert runner._ready_for_update() is False


def test_ready_for_update_blocked_by_active_merge():
    runner = make_runner()
    runner.merge_ctx = object()  # a conflict resolution is in progress
    assert runner._ready_for_update() is False


def test_ready_for_update_blocked_by_background_merge():
    runner = make_runner()
    runner.merge_ctx = None
    runner.sessions = [SimpleNamespace(merge_ctx=object())]  # a background session is merging
    assert runner._merge_session_active() is True
    assert runner._ready_for_update() is False


def test_no_update_prompt_during_merge_session():
    runner = make_runner()
    runner.merge_ctx = object()  # merge in progress
    runner._update_status = None
    runner._update_offered = False
    runner._update_worker_result = _available_status()
    runner._update_check_thread = SimpleNamespace(is_alive=lambda: False)

    runner._maybe_check_for_update()

    # The finished result is NOT consumed or surfaced while merging.
    assert runner._update_offered is False
    assert runner.message is None
    assert runner._update_status is None

    # Once the merge ends, the pending result surfaces on the next pass.
    runner.merge_ctx = None
    runner._maybe_check_for_update()
    assert runner._update_offered is True
    assert "update available" in (runner.message or "")


def test_handle_update_command_marks_pending_when_busy(monkeypatch):
    runner = make_runner()
    runner._update_status = _available_status()
    runner.active.agent_in_flight = True  # not ready -> should defer
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "Update when sessions finish")
    applied = []
    monkeypatch.setattr(runner, "_apply_update_and_restart", lambda: applied.append(True))
    runner._handle_update_command()
    assert runner._update_pending is True
    assert applied == []  # deferred, not applied while the agent is busy


def test_handle_update_command_applies_when_ready(monkeypatch):
    runner = make_runner()
    runner._update_status = _available_status()
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "Update when sessions finish")
    applied = []
    monkeypatch.setattr(runner, "_apply_update_and_restart", lambda: applied.append(True))
    runner._handle_update_command()
    assert runner._update_pending is True
    assert applied == [True]


def test_maybe_apply_pending_update_triggers_when_ready(monkeypatch):
    runner = make_runner()
    runner._update_pending = True
    runner._update_applying = False
    applied = []
    monkeypatch.setattr(runner, "_apply_update_and_restart", lambda: applied.append(True))
    runner._maybe_apply_pending_update()
    assert applied == [True]


# --- CLI startup prompt -----------------------------------------------------


class _StartupUpdater:
    def __init__(self, status: UpdateStatus):
        self._status = status
        self.checked = False
        self.applied = False

    def check(self) -> UpdateStatus:
        self.checked = True
        return self._status

    def apply(self) -> UpdateStatus:
        self.applied = True
        return UpdateStatus(kind=self._status.kind, message="updated", current="new")


def test_startup_prompt_applies_and_restarts(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(_available_status())
    restarted = []
    monkeypatch.setattr("agit.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("agit.update.restart_agit", lambda: restarted.append(True))
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    cli._check_for_update_at_startup(config)
    assert updater.applied is True
    assert restarted == [True]


def test_startup_prompt_never_disables_future_checks(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(_available_status())
    monkeypatch.setattr("agit.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("builtins.input", lambda *a: "never")
    cli._check_for_update_at_startup(config)
    assert config.check_for_updates is False
    assert updater.applied is False


def test_startup_prompt_skipped_when_up_to_date(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(UpdateStatus(kind=KIND_SOURCE, available=False))
    prompted = []
    monkeypatch.setattr("agit.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("builtins.input", lambda *a: prompted.append(True) or "y")
    cli._check_for_update_at_startup(config)
    assert prompted == []  # no prompt when there is nothing to install


def test_startup_check_skipped_when_disabled(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    config.check_for_updates = False

    def boom(*a, **k):
        raise AssertionError("update check must not run when disabled")

    monkeypatch.setattr("agit.update.Updater", boom)
    cli._check_for_update_at_startup(config)  # returns without checking
