"""Every aGiTrack daemon child must import the INSTALLED aGiTrack.

The bug this file pins down: run aGiTrack in the folder that HOLDS your projects — the folder
whose subfolders you have been tracking, aGiTrack's own source checkout among them — and the
directory it starts its children in contains something called ``agitrack``. ``python -m
agitrack`` puts that directory first on ``sys.path``, so the child imported ``./agitrack``: a
directory with no ``__init__.py``, therefore a NAMESPACE package, therefore

    ImportError: cannot import name '__version__' from 'agitrack' (unknown location)

— in a log file, while the launcher that spawned it printed "daemon live". aGiTrack looked like
it had started and nothing was tracking anything.

The protection is threefold and every daemon gets all three: ``-P`` on the command line,
``PYTHONSAFEPATH`` in the environment (for a command line recorded before ``-P`` existed, and
for the interpreters that read only the variable), and a working directory that holds no stray
package at all — the last being what covers Python 3.10, which has neither of the first two.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agitrack.git import GitRepo
from agitrack.metrics import backtrace as backtrace_mod
from agitrack.metrics import daemon as dashboard_mod
from agitrack.metrics import hub as hub_mod
from agitrack.proxy import background as background_mod


def _has_git() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


class _Child:
    pid = 4242


# Captured at import, BEFORE the suite-wide fixture that stubs the hub spawn out (conftest's
# `_never_spawn_the_real_dashboard`) can replace it: this file is one of the tests that means to
# exercise the real spawn, so it binds the real function itself.
_REAL_SPAWN_HUB = hub_mod._spawn_hub


def _capture_popen(monkeypatch) -> dict:
    """Record the one spawn that follows instead of making it.

    Patched on ``subprocess`` itself rather than on each module: two of the four spawn helpers
    import subprocess inside the function, where a module attribute would never be consulted."""
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen.update(kwargs)
        return _Child()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return seen


def _assert_isolated(seen: dict, shadowed: Path) -> None:
    assert seen["env"]["PYTHONSAFEPATH"] == "1"
    assert Path(seen["cwd"]) != shadowed  # the stray package is not on the child's doorstep
    assert "agitrack" in seen["cmd"][-1] or "agitrack" in seen["cmd"]


def test_background_tracker_is_spawned_with_import_isolation(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (tmp_path / "agitrack").mkdir()  # the folder-of-projects case, in one directory
    seen = _capture_popen(monkeypatch)

    background_mod.spawn_background_daemon(repo, extra_args=[])

    _assert_isolated(seen, tmp_path)


def test_dashboard_daemon_is_spawned_with_import_isolation(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (tmp_path / "agitrack").mkdir()
    seen = _capture_popen(monkeypatch)

    dashboard_mod.spawn_dashboard_daemon(repo)

    _assert_isolated(seen, tmp_path)


def test_backtrace_daemon_is_spawned_with_import_isolation(tmp_path, monkeypatch):
    (tmp_path / "agitrack").mkdir()
    seen = _capture_popen(monkeypatch)

    backtrace_mod._spawn_backtrace_child(tmp_path)

    _assert_isolated(seen, tmp_path)


def test_hub_is_spawned_with_import_isolation(tmp_path, monkeypatch):
    # The hub, unlike the per-repo daemons, inherits the LAUNCHER's working directory — which is
    # whatever folder the user typed `agitrack -d` in.
    (tmp_path / "agitrack").mkdir()
    monkeypatch.chdir(tmp_path)
    seen = _capture_popen(monkeypatch)

    _REAL_SPAWN_HUB()

    _assert_isolated(seen, tmp_path)
    assert "--hub-serve" in seen["cmd"]


def test_a_daemon_child_really_imports_the_installed_agitrack(tmp_path):
    """The property itself, with a real interpreter: the exact command and environment a daemon
    is launched with survives a directory named ``agitrack`` sitting where it starts."""
    from agitrack.proc import agitrack_invocation, isolated_env, safe_spawn_cwd

    (tmp_path / "agitrack").mkdir()
    done = subprocess.run(
        [*agitrack_invocation(), "--version"],
        cwd=safe_spawn_cwd(tmp_path),
        env=isolated_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert done.returncode == 0, done.stderr
    assert "unknown location" not in done.stderr  # the namespace-package import error
