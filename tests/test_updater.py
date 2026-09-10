"""Tests for agitrack/update/updater.py.

All subprocess and filesystem calls are mocked — no real git, pip, or network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agitrack.update.updater import (
    KIND_PACKAGE,
    KIND_SOURCE,
    METHOD_BREW_PYTHON,
    METHOD_MSI,
    Updater,
    _github_slug,
    _restart_command,
    _version_tuple,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_updater_package() -> Updater:
    """Return an Updater forced into package mode (no source repo)."""
    return Updater(source_repo=None)


def _make_updater_source(tmp_path: Path) -> Updater:
    """Return an Updater forced into source mode with a mock git HEAD."""
    with patch("agitrack.update.updater._git") as mock_git:
        mock_git.return_value = _completed(0, stdout="abc1234\n")
        updater = Updater(source_repo=tmp_path)
    return updater


# ---------------------------------------------------------------------------
# kind / source_repo properties
# ---------------------------------------------------------------------------


def test_kind_is_package_when_no_source():
    assert _make_updater_package().kind == KIND_PACKAGE


def test_kind_is_source_when_source_repo_provided(tmp_path):
    updater = _make_updater_source(tmp_path)
    assert updater.kind == KIND_SOURCE


def test_source_repo_returns_injected_path(tmp_path):
    updater = _make_updater_source(tmp_path)
    assert updater.source_repo == tmp_path


def test_source_repo_is_none_for_package():
    assert _make_updater_package().source_repo is None


# ---------------------------------------------------------------------------
# __init__: HEAD snapshot
# ---------------------------------------------------------------------------


def test_init_source_snapshots_running_rev(tmp_path):
    with patch("agitrack.update.updater._git") as mock_git:
        mock_git.return_value = _completed(0, stdout="deadbeef\n")
        updater = Updater(source_repo=tmp_path)
    assert updater._running_rev == "deadbeef"


def test_init_source_head_fails_running_rev_none(tmp_path):
    with patch("agitrack.update.updater._git") as mock_git:
        mock_git.return_value = _completed(1)
        updater = Updater(source_repo=tmp_path)
    assert updater._running_rev is None


# ---------------------------------------------------------------------------
# check() dispatch
# ---------------------------------------------------------------------------


def test_check_dispatches_to_check_package_when_no_source():
    updater = _make_updater_package()
    with patch.object(updater, "_check_package", return_value=MagicMock()) as mock_check:
        updater.check()
        mock_check.assert_called_once()


# ---------------------------------------------------------------------------
# _has_module_pip
# ---------------------------------------------------------------------------


def test_has_module_pip_returns_true_on_returncode_zero():
    updater = _make_updater_package()
    with patch("agitrack.update.updater.subprocess.run") as mock_run:
        mock_run.return_value = _completed(0)
        assert updater._has_module_pip("python") is True


def test_has_module_pip_returns_false_on_nonzero():
    updater = _make_updater_package()
    with patch("agitrack.update.updater.subprocess.run") as mock_run:
        mock_run.return_value = _completed(1)
        assert updater._has_module_pip("python") is False


def test_has_module_pip_oserror_returns_false():
    updater = _make_updater_package()
    with patch("agitrack.update.updater.subprocess.run", side_effect=OSError):
        assert updater._has_module_pip("python") is False


def test_has_module_pip_subprocess_error_returns_false():
    updater = _make_updater_package()
    with patch(
        "agitrack.update.updater.subprocess.run",
        side_effect=subprocess.SubprocessError,
    ):
        assert updater._has_module_pip("python") is False


# ---------------------------------------------------------------------------
# _pip_invocation
# ---------------------------------------------------------------------------


def test_pip_invocation_uses_current_python_when_pip_available():
    import sys

    updater = _make_updater_package()
    with patch.object(updater, "_has_module_pip", return_value=True):
        result = updater._pip_invocation()
    assert result == [sys.executable, "-m", "pip"]


def test_pip_invocation_falls_back_to_pip3():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        # NOT in a venv: there a PATH pip plausibly belongs to this same interpreter. (The suite
        # itself runs in a venv, so this has to be said rather than assumed.)
        patch("agitrack.update.updater._in_virtualenv", return_value=False),
        patch("agitrack.update.updater.shutil.which", side_effect=lambda n: "/usr/bin/pip3" if n == "pip3" else None),
    ):
        result = updater._pip_invocation()
    assert result == ["/usr/bin/pip3"]


def test_pip_invocation_falls_back_to_pip_when_no_pip3():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        patch("agitrack.update.updater._in_virtualenv", return_value=False),
        patch(
            "agitrack.update.updater.shutil.which",
            side_effect=lambda n: "/usr/bin/pip" if n == "pip" else None,
        ),
    ):
        result = updater._pip_invocation()
    assert result == ["/usr/bin/pip"]


def test_a_venv_with_no_pip_is_never_upgraded_through_another_interpreters_pip():
    """A uv-created venv has no `pip` module, and a pip from outside it installs into whatever
    environment owns THAT pip — not into this one.

    Measured on a developer machine: a background tracker running from such a venv fell back to
    Homebrew's pip3, which refused under PEP 668, and the daemon then published "this Python is
    externally managed; run `pip install --upgrade agitrack`" into the global self-update record
    for every dashboard to show. Both halves were wrong — the diagnosis named an interpreter the
    tracker does not run from, and the command would not have touched the install it described.
    Better to say there is no automatic route than to take one that leads elsewhere."""
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        patch("agitrack.update.updater._in_virtualenv", return_value=True),
        patch("agitrack.update.updater.shutil.which", return_value="/opt/homebrew/bin/pip3"),
    ):
        assert updater._pip_invocation() is None
        # …and the user is pointed at the tool that CAN replace it.
        guidance = updater._manual_routes()
    assert "no pip" in guidance and "uv pip install --upgrade" in guidance


def test_pip_invocation_returns_none_when_nothing_found():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        patch("agitrack.update.updater.shutil.which", return_value=None),
    ):
        assert updater._pip_invocation() is None


# ---------------------------------------------------------------------------
# _installed_version
# ---------------------------------------------------------------------------


def test_installed_version_returns_string():
    # The function either returns from importlib.metadata or the __version__ fallback;
    # either way it must return a non-empty string without raising.
    updater = _make_updater_package()
    result = updater._installed_version()
    assert isinstance(result, str) and result


def test_installed_version_fallback_on_metadata_error():
    updater = _make_updater_package()
    with patch("importlib.metadata.version", side_effect=Exception("not found")):
        result = updater._installed_version()
    # Must return something string-like without raising
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _latest_package_version
# ---------------------------------------------------------------------------


def test_latest_package_version_parses_latest_line():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(0, stdout="LATEST: 1.5.0\nagitrack (1.4.0)\n")
        result = updater._latest_package_version()
    assert result == "1.5.0"


def test_latest_package_version_parses_parenthetical_line():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(0, stdout="agitrack (2.0.1)\n")
        result = updater._latest_package_version()
    assert result == "2.0.1"


def test_latest_package_version_nonzero_returncode_returns_none():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(1)
        assert updater._latest_package_version() is None


def test_latest_package_version_timeout_returns_none():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch(
            "agitrack.update.updater.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["pip"], 5),
        ),
    ):
        assert updater._latest_package_version() is None


def test_latest_package_version_no_pip_returns_none():
    updater = _make_updater_package()
    with patch.object(updater, "_pip_invocation", return_value=None):
        assert updater._latest_package_version() is None


def test_latest_package_version_unrecognised_output_returns_none():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(0, stdout="nothing useful here\n")
        assert updater._latest_package_version() is None


# ---------------------------------------------------------------------------
# _apply_package: pip failure (non-PEP 668)
# ---------------------------------------------------------------------------


def test_apply_package_pip_non_pep668_failure_sets_error(monkeypatch):
    # The in-process pip path is POSIX-only (Windows defers); pin the platform so this runs
    # the same on any host.
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "linux", raising=False)
    updater = _make_updater_package()
    pip_result = _completed(1, stdout="", stderr="some unrelated error\nfailed to upgrade")
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run", return_value=pip_result),
    ):
        status = updater._apply_package()
    assert status.error is not None
    assert "upgrade" in status.error or "error" in status.error.lower() or status.error


# ---------------------------------------------------------------------------
# _apply_package: Windows defers the upgrade to a post-exit helper
# ---------------------------------------------------------------------------


def test_apply_package_windows_defers_without_running_pip(monkeypatch):
    # On Windows the running agitrack.exe is locked, so pip CANNOT replace it in place — an
    # in-process upgrade deletes package files then fails on the exe, corrupting the install.
    # apply() must instead record the command and return success WITHOUT running pip.
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["python", "-m", "pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        status = updater._apply_package()
    mock_run.assert_not_called()  # the corrupting in-place upgrade never runs
    assert status.error is None
    assert updater.pending_pip_upgrade == ["python", "-m", "pip", "install", "--upgrade", "agitrack"]


def test_launch_pip_bootstrapper_spawns_detached_helper(monkeypatch):
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    updater = _make_updater_package()
    updater.pending_pip_upgrade = ["python", "-m", "pip", "install", "--upgrade", "agitrack"]
    with (
        patch("agitrack.update.updater._restart_command", return_value=["python", "-m", "agitrack", "--verbose"]),
        patch("agitrack.update.updater.subprocess.Popen") as mock_popen,
    ):
        assert updater.launch_pip_bootstrapper(["--skip-privacy-ack"]) is True
    mock_popen.assert_called_once()
    argv, kwargs = mock_popen.call_args
    cmd = argv[0]
    assert cmd[0] == "powershell"
    script = cmd[-1]
    # The helper waits for THIS pid, runs the recorded pip upgrade, then relaunches aGiTrack.
    assert str(os.getpid()) in script
    assert "install" in script and "--upgrade" in script and "agitrack" in script
    assert "Start-Process" in script and "--verbose" in script
    # Detached so it outlives this process; detach_kwargs() keys off the real host OS (Windows
    # creationflags vs. POSIX start_new_session), not the patched platform, so assert per host.
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs.get("start_new_session") is True


def test_launch_pip_bootstrapper_noop_without_pending():
    updater = _make_updater_package()
    updater.pending_pip_upgrade = None
    assert updater.launch_pip_bootstrapper() is False


def test_launch_pip_bootstrapper_noop_off_windows(monkeypatch):
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "linux", raising=False)
    updater = _make_updater_package()
    updater.pending_pip_upgrade = ["python", "-m", "pip", "install", "--upgrade", "agitrack"]
    assert updater.launch_pip_bootstrapper() is False


def test_launch_pip_bootstrapper_returns_false_on_spawn_error(monkeypatch):
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    updater = _make_updater_package()
    updater.pending_pip_upgrade = ["python", "-m", "pip", "install", "--upgrade", "agitrack"]
    with (
        patch("agitrack.update.updater._restart_command", return_value=["python", "-m", "agitrack"]),
        patch("agitrack.update.updater.subprocess.Popen", side_effect=OSError("no powershell")),
    ):
        assert updater.launch_pip_bootstrapper() is False


# ---------------------------------------------------------------------------
# launch_msi_bootstrapper: shared elevated MSI hand-off (startup + in-session)
# ---------------------------------------------------------------------------


def test_launch_msi_bootstrapper_starts_elevated_install_and_relaunch(monkeypatch, tmp_path):
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    updater = _make_updater_package()
    updater.pending_msi_path = str(tmp_path / "agitrack-9.9.9-windows-x64.msi")
    runas: list = []
    popen: list = []
    monkeypatch.setattr("agitrack.proc.shell_execute_runas", lambda file, params="": runas.append((file, params)))
    monkeypatch.setattr("agitrack.update.updater.subprocess.Popen", lambda cmd, **kw: popen.append((cmd, kw)))
    with patch.object(updater, "msi_install_dir", return_value=str(tmp_path / "install")):
        assert updater.launch_msi_bootstrapper(["--skip-privacy-ack"]) is True
    # Elevated install started via runas on cmd.exe → the bootstrapper .cmd.
    assert runas and runas[0][0] == "cmd.exe" and "agitrack-update.cmd" in runas[0][1]
    # A detached PowerShell relauncher was spawned to restart after the install.
    assert popen and popen[0][0][0] == "powershell"
    # The relaunch args were recorded with the extra flag for the relauncher to read back.
    last = (tmp_path / "aGiTrack" / "last-args.txt").read_text(encoding="utf-8")
    assert "--skip-privacy-ack" in last


def test_launch_msi_bootstrapper_returns_false_when_uac_declined(monkeypatch, tmp_path):
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    updater = _make_updater_package()
    updater.pending_msi_path = str(tmp_path / "x.msi")
    popen: list = []

    def _deny(file, params=""):
        raise OSError("user declined UAC")

    monkeypatch.setattr("agitrack.proc.shell_execute_runas", _deny)
    monkeypatch.setattr("agitrack.update.updater.subprocess.Popen", lambda *a, **k: popen.append(a))
    with patch.object(updater, "msi_install_dir", return_value=str(tmp_path)):
        assert updater.launch_msi_bootstrapper() is False
    assert popen == []  # the relauncher must NOT spawn when the elevated install didn't start


def test_launch_msi_bootstrapper_noop_without_pending():
    updater = _make_updater_package()
    updater.pending_msi_path = None
    assert updater.launch_msi_bootstrapper() is False


def test_launch_msi_bootstrapper_noop_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "linux", raising=False)
    updater = _make_updater_package()
    updater.pending_msi_path = str(tmp_path / "x.msi")
    assert updater.launch_msi_bootstrapper() is False


# ---------------------------------------------------------------------------
# _apply_package: brew upgrade path
# ---------------------------------------------------------------------------


def test_apply_package_never_shells_out_to_brew_on_a_homebrew_python(monkeypatch):
    # THE BUG this replaces: on PEP 668 the updater ran `brew upgrade agitrack`, on the stated
    # belief that Homebrew "ships aGiTrack". It does not — there is no agitrack formula or cask —
    # so the command could only fail. And it fired on the COMMON macOS setup, because the
    # detection matches a Homebrew-managed PYTHON (the README's `brew install python` +
    # `pip3 install agitrack`), not a Homebrew-installed aGiTrack. The old test asserted the brew
    # call was made, so it pinned the bug in place.
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "linux", raising=False)
    updater = _make_updater_package()
    ran: list[list[str]] = []

    def record(cmd, *a, **k):
        ran.append(list(cmd))
        return _completed(1, stderr="externally-managed-environment error")

    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch.object(updater, "_install_method", return_value=METHOD_BREW_PYTHON),
        patch("agitrack.update.updater.shutil.which", return_value="/usr/local/bin/brew"),
        patch("agitrack.update.updater.subprocess.run", side_effect=record),
    ):
        status = updater._apply_package()

    assert not any("brew" in part for cmd in ran for part in cmd), ran
    # It reports instead, naming the one route that actually works for this install.
    assert "pipx install agitrack" in (status.error or "")
    assert "brew upgrade" not in (status.error or "")


# ---------------------------------------------------------------------------
# _version_tuple helper
# ---------------------------------------------------------------------------


def test_version_tuple_basic():
    assert _version_tuple("1.2.3") == (1, 2, 3)


def test_version_tuple_single():
    assert _version_tuple("5") == (5,)


def test_version_tuple_with_suffix():
    # Non-numeric suffix is truncated at the first non-digit character
    t = _version_tuple("1.2.3rc1")
    assert t[0] == 1
    assert t[1] == 2


def test_version_tuple_empty_string_returns_zero():
    # "".split(".") is [""], so the single empty chunk maps to 0.
    assert _version_tuple("") == (0,)


# ---------------------------------------------------------------------------
# MSI self-update (frozen Windows bundle). All monkey-patched: no real registry,
# network, download, or msiexec — so these run on POSIX CI too.
# ---------------------------------------------------------------------------


def _release_json(version: str = "9.9.9", *, with_msi: bool = True, digest=None) -> dict:
    assets = [{"name": "agitrack-extension.vsix", "browser_download_url": "https://example/vsix"}]
    if with_msi:
        assets.append(
            {
                "name": f"agitrack-{version}-windows-x64.msi",
                "browser_download_url": f"https://example/agitrack-{version}-windows-x64.msi",
                "digest": digest,
            }
        )
    return {"tag_name": f"v{version}", "assets": assets}


class _FakeResp:
    """Minimal urlopen()-style response usable as a context manager."""

    def __init__(self, data: bytes, headers: dict | None = None) -> None:
        self._data = data
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size >= len(self._data):
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _win_msi(monkeypatch) -> None:
    """Make _install_method() report METHOD_MSI on any host (frozen + registry + win32)."""
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    monkeypatch.setattr("agitrack.update.updater.sys.frozen", True, raising=False)


def test_install_method_msi_when_frozen_and_registry(monkeypatch):
    updater = _make_updater_package()
    _win_msi(monkeypatch)
    with patch.object(updater, "_registry_install_dir", return_value=r"C:\Program Files\aGiTrack"):
        assert updater._install_method() == METHOD_MSI


def test_install_method_not_msi_when_not_frozen(monkeypatch):
    updater = _make_updater_package()
    monkeypatch.setattr("agitrack.update.updater.sys.platform", "win32", raising=False)
    monkeypatch.setattr("agitrack.update.updater.sys.frozen", False, raising=False)
    with patch.object(updater, "_registry_install_dir", return_value=r"C:\Program Files\aGiTrack"):
        assert updater._install_method() != METHOD_MSI


def test_install_method_not_msi_without_registry_key(monkeypatch):
    updater = _make_updater_package()
    _win_msi(monkeypatch)
    with patch.object(updater, "_registry_install_dir", return_value=None):
        assert updater._install_method() != METHOD_MSI


def test_check_msi_newer_version_available():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_github_get_json", return_value=_release_json("9.9.9")),
        patch.object(updater, "_installed_version", return_value="0.1.0"),
    ):
        status = updater._check_msi()
    assert status.ok and status.available
    assert status.latest == "9.9.9"
    assert updater._msi_asset_url.endswith("agitrack-9.9.9-windows-x64.msi")
    assert updater._msi_asset_name == "agitrack-9.9.9-windows-x64.msi"


def test_check_msi_up_to_date():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_github_get_json", return_value=_release_json("1.0.0")),
        patch.object(updater, "_installed_version", return_value="1.0.0"),
    ):
        status = updater._check_msi()
    assert status.ok and not status.available


def test_check_msi_api_error_is_reported():
    updater = _make_updater_package()
    with patch.object(updater, "_github_get_json", side_effect=RuntimeError("boom")):
        status = updater._check_msi()
    assert not status.ok and "MSI" in (status.error or "")


def test_check_msi_no_msi_asset_is_reported():
    updater = _make_updater_package()
    with patch.object(updater, "_github_get_json", return_value=_release_json(with_msi=False)):
        status = updater._check_msi()
    assert not status.ok


def test_apply_msi_downloads_and_stores_path(tmp_path, monkeypatch):
    updater = _make_updater_package()
    updater._msi_asset_url = "https://example/agitrack-9.9.9-windows-x64.msi"
    updater._msi_asset_name = "agitrack-9.9.9-windows-x64.msi"
    updater._msi_latest = "9.9.9"
    captured: dict = {}

    def fake_download(url, dest, *, timeout, digest=None):
        Path(dest).write_bytes(b"msi-bytes")
        captured["dest"] = str(dest)

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    with (
        patch.object(updater, "_download", side_effect=fake_download),
        patch.object(updater, "_installed_version", return_value="0.1.0"),
    ):
        status = updater._apply_msi()
    assert status.ok
    assert updater.pending_msi_path == captured["dest"]
    assert status.latest == "9.9.9"
    assert Path(captured["dest"]).name == "agitrack-9.9.9-windows-x64.msi"


def test_apply_msi_download_failure_surfaces_manual_instructions(monkeypatch):
    updater = _make_updater_package()
    updater._msi_asset_url = "https://example/agitrack-9.9.9-windows-x64.msi"
    updater._msi_asset_name = "agitrack-9.9.9-windows-x64.msi"
    monkeypatch.setattr("tempfile.gettempdir", lambda: ".")
    with (
        patch.object(updater, "_download", side_effect=RuntimeError("net down")),
        patch.object(updater, "_install_method", return_value=METHOD_MSI),
        patch.object(updater, "_github_repo", return_value="core-aix/agitrack"),
    ):
        status = updater._apply_msi()
    assert not status.ok
    assert "releases/latest" in (status.error or "")
    assert updater.pending_msi_path is None


def test_manual_instructions_msi_route():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_install_method", return_value=METHOD_MSI),
        patch.object(updater, "_github_repo", return_value="core-aix/agitrack"),
    ):
        text = updater.manual_update_instructions()
    assert "releases/latest" in text
    assert "SmartScreen" in text


def test_download_checksum_mismatch_raises_and_cleans_up(tmp_path):
    updater = _make_updater_package()
    dest = tmp_path / "x.msi"
    with patch("urllib.request.urlopen", return_value=_FakeResp(b"hello")):
        with pytest.raises(RuntimeError):
            updater._download("https://example/x", dest, timeout=10, digest="sha256:deadbeef")
    assert not dest.exists()  # partial download removed


def test_download_ok_without_digest(tmp_path):
    updater = _make_updater_package()
    dest = tmp_path / "x.msi"
    with patch("urllib.request.urlopen", return_value=_FakeResp(b"payload")):
        updater._download("https://example/x", dest, timeout=10, digest=None)
    assert dest.read_bytes() == b"payload"


def test_check_routes_to_msi(monkeypatch):
    updater = _make_updater_package()
    _win_msi(monkeypatch)
    with (
        patch.object(updater, "_registry_install_dir", return_value=r"C:\PF\aGiTrack"),
        patch.object(updater, "_check_msi", return_value=MagicMock(name="msi-status")) as msi_check,
    ):
        updater.check()
    msi_check.assert_called_once()


def test_github_repo_defaults_to_upstream_for_msi(monkeypatch):
    updater = _make_updater_package()  # no source repo -> no local remote
    monkeypatch.delenv("AGITRACK_GH_REPO", raising=False)
    assert updater._github_repo() == "core-aix/agitrack"


def test_github_repo_env_override(monkeypatch):
    updater = _make_updater_package()
    monkeypatch.setenv("AGITRACK_GH_REPO", "me/fork")
    assert updater._github_repo() == "me/fork"


# ---------------------------------------------------------------------------
# _github_slug helper
# ---------------------------------------------------------------------------


def test_github_slug_https():
    assert _github_slug("https://github.com/core-aix/agitrack.git") == "core-aix/agitrack"


def test_github_slug_ssh():
    assert _github_slug("git@github.com:core-aix/agitrack.git") == "core-aix/agitrack"


def test_github_slug_non_github_is_none():
    assert _github_slug("https://example.com/foo/bar") is None
    assert _github_slug("") is None


# ---------------------------------------------------------------------------
# _restart_command: frozen MSI build vs normal interpreter
# ---------------------------------------------------------------------------


def test_restart_command_frozen_runs_exe_directly(monkeypatch):
    monkeypatch.setattr("agitrack.update.updater.sys.frozen", True, raising=False)
    monkeypatch.setattr("agitrack.update.updater.sys.argv", ["agitrack.exe", "--repo", "x"])
    monkeypatch.setattr("agitrack.update.updater.sys.executable", r"C:\PF\aGiTrack\agitrack.exe")
    cmd = _restart_command(["--skip-privacy-ack"])
    assert cmd == [r"C:\PF\aGiTrack\agitrack.exe", "--repo", "x", "--skip-privacy-ack"]
    assert "-m" not in cmd  # the invalid frozen-app argument is gone


def test_restart_command_non_frozen_uses_module(monkeypatch):
    monkeypatch.setattr("agitrack.update.updater.sys.frozen", False, raising=False)
    monkeypatch.setattr("agitrack.update.updater.sys.argv", ["agitrack", "--repo", "x"])
    monkeypatch.setattr("agitrack.update.updater.sys.executable", "/usr/bin/python3")
    cmd = _restart_command()
    # `-P` (3.11+) keeps a stray `agitrack` directory in the restart's working directory off
    # `sys.path`; see agitrack.proc.agitrack_invocation.
    flags = ["-P"] if sys.version_info >= (3, 11) else []
    assert cmd == ["/usr/bin/python3", *flags, "-m", "agitrack", "--repo", "x"]


def test_restart_command_dedupes_extra_args(monkeypatch):
    monkeypatch.setattr("agitrack.update.updater.sys.frozen", False, raising=False)
    monkeypatch.setattr("agitrack.update.updater.sys.argv", ["agitrack", "--skip-privacy-ack"])
    monkeypatch.setattr("agitrack.update.updater.sys.executable", "/usr/bin/python3")
    cmd = _restart_command(["--skip-privacy-ack"])
    assert cmd.count("--skip-privacy-ack") == 1
