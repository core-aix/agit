"""Cross-platform process primitives (agitrack/proc.py).

The POSIX behaviour is exercised for real on the test host; the Windows branches are
exercised by flipping the platform flag and stubbing the Win32 helpers (so they're
covered on a Linux CI too, without a Windows box)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import agitrack.proc as proc


def test_detach_kwargs_posix_uses_new_session():
    if proc._IS_WINDOWS:  # pragma: no cover - this assertion is for the POSIX test host
        return
    assert proc.detach_kwargs() == {"start_new_session": True}


def test_shell_execute_runas_off_windows_raises(monkeypatch):
    # Off Windows the elevation helper is unavailable — must fail loudly, not silently no-op,
    # so callers don't think an elevated install was launched when it wasn't.
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    try:
        proc.shell_execute_runas("cmd.exe", "/c echo hi")
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError off Windows")


def test_detach_kwargs_windows_uses_creationflags(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    kwargs = proc.detach_kwargs()
    assert set(kwargs) == {"creationflags"}  # no start_new_session on Windows
    assert "start_new_session" not in kwargs


def test_pid_alive_posix_real():
    if proc._IS_WINDOWS:  # pragma: no cover
        return
    assert proc.pid_alive(os.getpid()) is True
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert proc.pid_alive(done.pid) is False


def test_pid_alive_dispatches_to_windows_helper(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc, "_windows_pid_alive", lambda pid: pid == 123)
    assert proc.pid_alive(123) is True
    assert proc.pid_alive(999) is False


def test_terminate_pid_posix_is_best_effort_for_a_dead_pid():
    if proc._IS_WINDOWS:  # pragma: no cover
        return
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    proc.terminate_pid(done.pid)  # must not raise even though the process is gone


def test_terminate_pid_dispatches_to_windows_helper(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc, "_windows_terminate", lambda pid: seen.append(pid))
    proc.terminate_pid(555)
    assert seen == [555]


# --- console_isolation_kwargs (keep child subprocesses off the host console) --------


def test_console_isolation_kwargs_windows_detaches_stdin_and_hides_console(monkeypatch):
    # On Windows a captured child must not inherit our console (it would reset raw mode and
    # make input echo as escape codes) — give it its own hidden console and a detached stdin.
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    kwargs = proc.console_isolation_kwargs()
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "creationflags" in kwargs  # CREATE_NO_WINDOW → child gets its own console, not ours


def test_console_isolation_kwargs_windows_keeps_stdin_when_feeding_input(monkeypatch):
    # When the caller feeds the child via input=, subprocess already pipes stdin; passing our
    # own stdin= too would be a conflict, so detach_stdin=False omits it (creationflags stay).
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    kwargs = proc.console_isolation_kwargs(detach_stdin=False)
    assert "stdin" not in kwargs
    assert "creationflags" in kwargs


def test_console_isolation_kwargs_posix_detaches_stdin_and_starts_new_session(monkeypatch):
    # POSIX has no console coupling (no creationflags), but the child runs in its OWN session
    # (start_new_session) so a short git subprocess doesn't sit in the terminal's foreground
    # group and flicker the tab title to "git"; the stdin detach also stops a TTY-probing CLI
    # from hanging the menu thread.
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    assert proc.console_isolation_kwargs() == {"stdin": subprocess.DEVNULL, "start_new_session": True}
    assert proc.console_isolation_kwargs(detach_stdin=False) == {"start_new_session": True}


# --- resolve_subprocess_command (Windows .cmd/.exe resolution, #118) ----------------


def test_resolve_subprocess_command_posix_passthrough(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    assert proc.resolve_subprocess_command(["claude", "-p", "x"]) == ["claude", "-p", "x"]


def test_resolve_subprocess_command_empty_is_unchanged(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    assert proc.resolve_subprocess_command([]) == []


def test_resolve_subprocess_command_windows_wraps_cmd_shim(monkeypatch):
    # npm installs `claude.cmd`; CreateProcess can't run a batch file, so it must go through
    # cmd.exe /c — otherwise summarization raised FileNotFoundError on Windows. The .cmd case
    # returns a fully-quoted command-LINE string (subprocess.run accepts it on Windows).
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\Users\me\AppData\npm\claude.cmd")
    cmd = proc.resolve_subprocess_command(["claude", "-p", "summarize this"])
    assert isinstance(cmd, str)
    assert "cmd.exe" in cmd.lower() and "/c" in cmd
    assert r"C:\Users\me\AppData\npm\claude.cmd" in cmd
    assert "summarize this" in cmd  # args preserved after the shim


def test_resolve_subprocess_command_windows_cmd_shim_under_spaced_path(monkeypatch):
    # The reported failure: a .cmd shim under a path with a space (e.g. a Windows username
    # "First Last") plus a spaced argument. The shim path and the arg are each quoted, and the
    # whole inner command is wrapped in an extra quote pair so cmd.exe's /c strip leaves them
    # intact (verified empirically — without this cmd splits the path at the space).
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    spaced = r"C:\Users\First Last\AppData\Roaming\npm\claude.cmd"
    monkeypatch.setattr(proc.shutil, "which", lambda name: spaced)
    cmd = proc.resolve_subprocess_command(["claude", "--append-system-prompt", "a note with spaces"])
    assert isinstance(cmd, str)
    assert cmd.startswith('"') and cmd.endswith('"')  # protective outer quotes
    assert f'"{spaced}"' in cmd  # the spaced shim path is quoted
    assert '"a note with spaces"' in cmd  # the spaced arg is quoted


def test_resolve_subprocess_command_windows_exe_no_shell(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\bin\opencode.exe")
    cmd = proc.resolve_subprocess_command(["opencode", "models"])
    assert cmd == [r"C:\bin\opencode.exe", "models"]  # resolved path, run directly


def test_resolve_subprocess_command_windows_unresolved_falls_back(monkeypatch):
    # which() found nothing (backend not on PATH): pass the name through unchanged so the
    # caller still gets its usual FileNotFoundError rather than a surprise.
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)
    assert proc.resolve_subprocess_command(["claude", "-p", "x"]) == ["claude", "-p", "x"]


# --- which_executable: Windows-correct executable lookup (#half-installed npm shims) -------


def test_which_executable_posix_is_plain_which(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    monkeypatch.setattr(proc.shutil, "which", lambda name: "/usr/bin/" + name)
    assert proc.which_executable("claude") == "/usr/bin/claude"


def test_which_executable_windows_finds_cmd_shim(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    # Only claude.cmd exists (the proper npm shim); .exe does not.
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\npm\claude.cmd" if name == "claude.cmd" else None)
    assert proc.which_executable("claude") == r"C:\npm\claude.cmd"


def test_which_executable_windows_rejects_extensionless_and_ps1(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    # A half-installed npm package: bare 'claude' (shell script) and claude.ps1 exist, but
    # no .exe/.cmd/.bat — raw shutil.which would return the bare file, which_executable must not.
    present = {"claude": r"C:\npm\claude", "claude.ps1": r"C:\npm\claude.ps1"}
    monkeypatch.setattr(proc.shutil, "which", lambda name: present.get(name))
    assert proc.which_executable("claude") is None


def test_which_executable_windows_honours_explicit_extension(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\bin\opencode.exe" if name == "opencode.exe" else None)
    assert proc.which_executable("opencode.exe") == r"C:\bin\opencode.exe"


def test_fs_path_is_the_identity_on_a_utf8_filesystem():
    """Which is every Windows and macOS box, and Linux unless someone opts out.

    Compared as a Path, not as a string: `str(Path("/home/u/x"))` is `\\home\\u\\x` on Windows,
    because pathlib renders separators for the host. That is pathlib's business and says nothing
    about fs_path, but asserting on the raw string made this fail on every Windows run — the
    identity that matters is that the CHARACTERS are untouched, which is checked explicitly."""
    printed = "/home/u/tëst_репо"

    assert proc.fs_path(printed) == Path(printed)
    assert "tëst_репо" in proc.fs_path(printed).name  # the non-ASCII survived intact


def _encodes_back_to(text: str, encoding: str) -> bytes:
    """The bytes the filesystem layer would send to the kernel for ``text``.

    This is the operation that failed: ``Path(...).resolve()`` reaches ``posixpath.realpath``,
    which encodes the string with the filesystem codec. Raises exactly where aGiTrack did.
    """
    return text.encode(encoding, "surrogateescape")


def test_fs_path_survives_an_ascii_filesystem_encoding(monkeypatch):
    """UTF8_TEXT decodes git's output correctly, but the resulting str is only half the round
    trip: Python encodes str paths back to bytes for every syscall with the FILESYSTEM
    encoding, which under `PYTHONUTF8=0` in a C locale is ASCII. `Path(git_output).resolve()`
    then raised UnicodeEncodeError from inside posixpath — a raw traceback out of every command
    on any repo with a non-ASCII path.
    """
    if proc._IS_WINDOWS:  # pragma: no cover - the filesystem encoding is always UTF-8 there
        return
    printed = "/home/u/tëst"  # git printed UTF-8 and UTF8_TEXT decoded it
    monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "ascii")

    with pytest.raises(UnicodeEncodeError):
        _encodes_back_to(printed, "ascii")  # the bug, if the string is passed on unconverted

    assert _encodes_back_to(str(proc.fs_path(printed)), "ascii") == printed.encode("utf-8")


def test_fs_path_round_trips_a_real_non_ascii_directory(tmp_path, monkeypatch):
    """The same property against bytes that really are on this disk, rather than a literal."""
    if proc._IS_WINDOWS:  # pragma: no cover
        return
    target = tmp_path / "ünïcode_репо"
    target.mkdir()
    printed = str(target)  # what `git rev-parse --show-toplevel` hands back through UTF8_TEXT
    monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "latin-1")

    # The bytes the kernel would be handed are the bytes the directory actually has. (Only the
    # ENCODE side can be checked on a UTF-8 host: `is_dir()` would use this host's real codec,
    # not the latin-1 one being simulated.)
    assert _encodes_back_to(str(proc.fs_path(printed)), "latin-1") == os.fsencode(target)


# --------------------------------------------------------------------------- import isolation
#
# Run aGiTrack in the folder that HOLDS your projects — the one whose subfolders you have been
# tracking, aGiTrack's own source checkout among them — and every `python -m agitrack` child
# used to import `./agitrack` instead of the installed package. Because a stray directory has
# no `__init__.py` it arrives as a NAMESPACE package, so the child died on `ImportError: cannot
# import name '__version__' from 'agitrack' (unknown location)` inside a log file, while the
# launcher that spawned it reported the daemon live.


def test_agitrack_invocation_keeps_the_working_directory_off_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    command = proc.agitrack_invocation()
    assert command[0] == "/usr/bin/python3"
    assert command[-2:] == ["-m", "agitrack"]
    if sys.version_info >= (3, 11):
        assert "-P" in command
    else:  # pragma: no cover - 3.10 rejects the flag outright, so it must not be passed
        assert "-P" not in command


def test_agitrack_invocation_frozen_build_gets_no_interpreter_flags(monkeypatch):
    # `-P` is as invalid an argument to agitrack.exe as `-m agitrack` is.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\PF\aGiTrack\agitrack.exe")
    assert proc.agitrack_invocation() == [r"C:\PF\aGiTrack\agitrack.exe"]


def test_isolated_env_sets_the_safe_path_and_keeps_the_rest(monkeypatch):
    monkeypatch.setenv("AGITRACK_SOMETHING", "kept")
    env = proc.isolated_env()
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["AGITRACK_SOMETHING"] == "kept"
    assert proc.isolated_env({"ONLY": "this"}) == {"ONLY": "this", "PYTHONSAFEPATH": "1"}


def test_shadowing_agitrack_package_finds_a_stray_directory(tmp_path):
    (tmp_path / "agitrack").mkdir()
    assert proc.shadowing_agitrack_package(tmp_path) == tmp_path / "agitrack"


def test_shadowing_agitrack_package_finds_a_stray_module(tmp_path):
    (tmp_path / "agitrack.py").write_text("", encoding="utf-8")
    assert proc.shadowing_agitrack_package(tmp_path) == tmp_path / "agitrack.py"


def test_shadowing_agitrack_package_ignores_an_ordinary_directory(tmp_path):
    (tmp_path / "notes").mkdir()
    assert proc.shadowing_agitrack_package(tmp_path) is None


def test_agitrack_own_checkout_does_not_shadow_itself():
    # The `agitrack/` directory in aGiTrack's own repo IS the package this code was imported
    # from, so a daemon started there loads exactly what its launcher was running.
    checkout = Path(proc.__file__).resolve().parent.parent
    assert proc.shadowing_agitrack_package(checkout) is None


def test_safe_spawn_cwd_keeps_a_clean_directory(tmp_path):
    assert proc.safe_spawn_cwd(tmp_path) == tmp_path


def test_safe_spawn_cwd_moves_out_of_a_shadowed_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "agitrack").mkdir()
    chosen = proc.safe_spawn_cwd(tmp_path)
    assert chosen == tmp_path / "config"
    assert chosen.is_dir()  # a cwd a child cannot enter is no better than a shadowed one


def test_a_child_started_in_a_shadowed_directory_still_imports_agitrack(tmp_path):
    """The end-to-end property, run for real: a `python -m agitrack` child launched the way
    aGiTrack launches its daemons imports the installed package even when the directory it
    starts in holds one named the same."""
    (tmp_path / "agitrack").mkdir()  # the shadow: a namespace package with no __init__.py
    command = [*proc.agitrack_invocation(), "--version"]
    done = subprocess.run(
        command,
        cwd=proc.safe_spawn_cwd(tmp_path),
        env=proc.isolated_env(),
        capture_output=True,
        **proc.UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    assert "unknown location" not in done.stderr
