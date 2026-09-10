"""Cross-platform process primitives (POSIX + native Windows).

aGiTrack spawns detached helpers (the dashboard daemon, the self-update) and
probes/stops processes by pid. The POSIX idioms — ``start_new_session`` to detach,
``os.kill(pid, 0)`` to probe liveness, ``os.kill(pid, SIGTERM)`` to stop — don't all
exist on native Windows, so these helpers select the right mechanism per platform and
keep every call site identical and platform-agnostic.
"""

from __future__ import annotations

import codecs
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


# The ONLY correct way to ask subprocess for text I/O in this codebase. Spread it instead of
# writing ``text=True``:
#
#     subprocess.run(cmd, **UTF8_TEXT, stdout=subprocess.PIPE, ...)
#
# Bare ``text=True`` decodes with ``locale.getencoding()`` — cp1252 on a Western Windows box,
# ASCII under ``LC_ALL=C`` or ``PYTHONUTF8=0`` on Linux. Every tool aGiTrack shells out to (git
# above all) speaks UTF-8, so the locale codec is simply the wrong one, and the failure is not
# cosmetic: ``git rev-parse --show-toplevel`` in ``C:\Users\Müller\repo`` came back as mojibake,
# which was then passed as ``cwd`` to the next git call and surfaced as ``[WinError 267] The
# directory name is invalid`` — aGiTrack was unusable for anyone whose profile directory is not
# pure ASCII. On Linux the same call raised ``UnicodeDecodeError`` outright.
#
# ``errors="replace"`` is deliberate: a stray undecodable byte in a tool's OUTPUT must degrade
# one character, never crash a command.
# Annotated ``dict[str, Any]``: inferred as ``dict[str, object]`` it cannot satisfy any
# ``subprocess.run`` overload when spread, and every call site fails to type-check.
UTF8_TEXT: dict[str, Any] = {"text": True, "encoding": "utf-8", "errors": "replace"}


def fs_path(printed: str) -> Path:
    """The other half of ``UTF8_TEXT``: a path a tool PRINTED, made usable as a path.

    ``UTF8_TEXT`` gets the wire format right — git speaks UTF-8 — but decoding is only half
    the round trip. Python encodes ``str`` paths back to bytes for every syscall using
    ``sys.getfilesystemencoding()``, and that is **not** always UTF-8 on Linux: under
    ``PYTHONUTF8=0`` in a non-UTF-8 locale (``LC_ALL=C``, a bare cron or systemd unit, a slim
    container image with no locales installed) it is ASCII. So in ``/home/u/tëst`` the ``ë``
    that came back correctly from ``git rev-parse --show-toplevel`` then blew up on the very
    next line — ``Path(printed).resolve()`` raised ``UnicodeEncodeError`` from inside
    ``posixpath.realpath``, before any of aGiTrack's own error handling could run, and every
    command (``-s``, ``-d text``, the TUI) died on a raw traceback.

    Re-decoding those UTF-8 bytes with ``os.fsdecode`` yields the surrogate-escaped form that
    encodes back to *the same bytes on disk*, which is what the filesystem layer wants. Where
    the filesystem encoding already is UTF-8 — always on Windows and macOS, and on Linux
    unless someone has explicitly opted out — this is the identity.
    """
    encoding = sys.getfilesystemencoding()
    if codecs.lookup(encoding).name == "utf-8":
        return Path(printed)
    # ``os.fsdecode`` would be the obvious call, but it binds its codec at import time, so it
    # cannot be reasoned about (or tested) against the encoding in force now. This is the same
    # conversion, spelled out: git's bytes, decoded the way the filesystem layer will re-encode
    # them. ``surrogateescape`` is what makes that re-encoding lossless.
    return Path(printed.encode("utf-8", "surrogateescape").decode(encoding, "surrogateescape"))


# ``python -m agitrack`` puts the CURRENT DIRECTORY at the front of ``sys.path``, where a
# directory named ``agitrack`` beats the installed package. That is not hypothetical: it is
# what happens the moment aGiTrack is run in the folder that HOLDS your projects — the folder
# whose subfolders you have been tracking, aGiTrack's own source checkout among them.
#
# The failure is worse than "an older copy was imported". A stray ``agitrack/`` directory has
# no ``__init__.py``, so it is picked up as a NAMESPACE package: submodules still resolve (via
# the installed package's own finder) but the package itself has no attributes, and every child
# process dies on ``ImportError: cannot import name '__version__' from 'agitrack' (unknown
# location)`` — inside a log file, while the launcher that spawned it reports success. Passing
# ``-P`` is what keeps the current directory off ``sys.path``.
#
# ``-P`` (and its environment twin ``PYTHONSAFEPATH``, see :func:`isolated_env`) landed in
# Python 3.11 and would be rejected outright by the 3.10 this project still supports, so on
# 3.10 the flag is omitted and :func:`safe_spawn_cwd` carries the protection alone.
_SAFE_PATH_ARGS = ["-P"] if sys.version_info >= (3, 11) else []


def agitrack_invocation() -> list[str]:
    """The argv PREFIX that invokes THIS aGiTrack (append the aGiTrack args after it).

    A normal (pip/source/pipx) install is a Python interpreter, so it runs as
    ``<python> -P -m agitrack …`` — which loads whatever version is installed for that
    interpreter, so after a self-update it automatically runs the NEW code (the interpreter path
    is stable), and never the stray ``agitrack`` directory that may sit in whatever working
    directory the command is run from (see ``_SAFE_PATH_ARGS``). A **frozen** build (the
    PyInstaller/MSI ``agitrack.exe``) is not a Python interpreter — ``-m agitrack`` is invalid
    there, and so is ``-P`` — so the executable is run directly; the MSI reinstalls it at the
    same path, so it too resolves to the updated version."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, *_SAFE_PATH_ARGS, "-m", "agitrack"]


def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (the current environment by default) with the safe import path turned on.

    The environment twin of the ``-P`` in :func:`agitrack_invocation`, for children whose
    command line aGiTrack does not fully own — and a second belt for the ones it does, since a
    daemon re-spawned from a command line RECORDED by an older aGiTrack carries no ``-P``."""
    env = dict(os.environ if base is None else base)
    env["PYTHONSAFEPATH"] = "1"
    return env


def shadowing_agitrack_package(directory: str | os.PathLike[str]) -> Path | None:
    """The stray ``agitrack`` module in ``directory`` that a child started there would import
    instead of the installed one, or None when there is none.

    aGiTrack's OWN source checkout is not one: its ``agitrack/`` directory *is* the package this
    code was imported from, so a daemon started in it loads exactly what its launcher was
    running."""
    package = Path(__file__).resolve().parent
    for name in ("agitrack", "agitrack.py"):
        candidate = Path(directory) / name
        try:
            if not candidate.exists() or candidate.resolve() == package:
                continue
        except OSError:  # pragma: no cover - an unreadable candidate cannot shadow anything
            continue
        return candidate
    return None


def safe_spawn_cwd(directory: str | os.PathLike[str]) -> Path:
    """``directory``, unless a child started there would import a stray ``agitrack`` package —
    then aGiTrack's own config directory, which by construction holds no such thing.

    Every daemon addresses its repository by an absolute ``--repo`` path and runs git with an
    explicit ``cwd``, so where the process itself sits is free to move; being able to import
    aGiTrack is not. This is what protects Python 3.10, where ``-P``/``PYTHONSAFEPATH`` do not
    exist (see ``_SAFE_PATH_ARGS``)."""
    directory = Path(directory)
    if shadowing_agitrack_package(directory) is None:
        return directory
    from agitrack.env import getenv_compat

    config_dir = getenv_compat("CONFIG_DIR")
    neutral = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    try:
        neutral.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - a home directory that cannot be written to
        return directory
    return neutral if shadowing_agitrack_package(neutral) is None else directory


# The one canonical Windows flag for the package. Prefer a plain ``os.name == "nt"`` inline
# for a runtime branch; import THIS constant instead in code whose Windows branch is unit-
# tested on a POSIX host (backends, this module) — a test flips ``_IS_WINDOWS`` directly,
# whereas monkeypatching the global ``os.name`` would also switch ``pathlib`` to WindowsPath
# semantics and break unrelated path handling on the test host.
_IS_WINDOWS = os.name == "nt"

# Windows: a process that is still running reports this as its exit code.
_STILL_ACTIVE = 259

# The PATH aGiTrack INHERITED from the shell that launched it, captured at import — before
# any install prepends a freshly-created bin directory to ``os.environ["PATH"]`` for this
# process. That distinction is what lets aGiTrack tell "the user's own shells already find
# this command" from "only aGiTrack finds it, because it just installed it" (see
# agitrack/path_setup.py). This module is imported by the CLI at startup and by the installer
# itself, so it is always read before an install can mutate the value.
INHERITED_PATH = os.environ.get("PATH", "")


def which_executable(name: str) -> str | None:
    """Like :func:`shutil.which`, but on Windows only returns a path the OS can actually
    execute — a ``.exe``/``.cmd``/``.bat``/``.com`` — never a bare extensionless file or a
    ``.ps1``.

    ``shutil.which`` on Windows treats EVERY existing file as executable (there is no
    ``X_OK`` bit), so for a half-installed npm package that left only the extensionless Unix
    shell script and a PowerShell ``.ps1`` shim — e.g. a ``claude`` with ``claude.ps1`` but
    no ``claude.cmd`` — it returns the non-runnable shell script. aGiTrack would then think
    the backend is installed, skip offering to install it, and fail to launch it through
    ConPTY/``CreateProcess`` (which can't run a shell script or a ``.ps1``), surfacing as
    "backend exited repeatedly on launch". Restricting to real Windows-executable shims makes
    detection match what can actually be launched.
    """
    if not _IS_WINDOWS:
        return shutil.which(name)
    if os.path.splitext(name)[1]:
        return shutil.which(name)  # an explicit extension was given — trust it
    for ext in (".exe", ".cmd", ".bat", ".com"):
        found = shutil.which(name + ext)
        if found:
            return found
    return None


def resolve_subprocess_command(command: list[str]) -> list[str] | str:
    """Resolve *command* for ``subprocess`` on the current OS.

    On Windows, look the executable up via PATH/PATHEXT (so a bare ``claude`` finds the npm
    ``claude.cmd`` shim — which ``CreateProcess`` would otherwise miss, since it only tries
    ``.exe``) and run a ``.cmd``/``.bat`` through ``cmd.exe /c``, the only way
    ``CreateProcess`` (what ``subprocess`` uses with ``shell=False``) executes a batch file.
    On POSIX, returns *command* unchanged.

    For the ``.cmd``/``.bat`` case the result is a fully-quoted command-LINE STRING, not an
    arg list (``subprocess.run`` accepts either on Windows). ``cmd.exe`` strips ONE layer of
    quotes from the text after ``/c``; if we returned the list ``[cmd, "/c", exe, *args]``,
    ``subprocess`` would render ``cmd /c "exe" "arg with space"`` and that strip would split a
    spaced exe path (e.g. a npm shim under ``C:\\Users\\First Last\\…``) or a spaced argument.
    Wrapping the whole inner command in an EXTRA pair of quotes — ``cmd /c "<inner>"`` — makes
    ``cmd`` strip exactly that pair and run the inner command (with its own quoting) intact.

    This is the ``subprocess`` counterpart of the ConPTY launch path's
    ``_resolve_windows_command``: without it, every non-TUI backend invocation (summarizer,
    model listing) raised ``FileNotFoundError`` on Windows, so summarization silently fell
    back to the prompt-led commit message.
    """
    if not _IS_WINDOWS or not command:
        return command
    exe = which_executable(command[0]) or command[0]
    if exe.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        inner = subprocess.list2cmdline([exe, *command[1:]])
        return f'"{comspec}" /c "{inner}"'
    return [exe, *command[1:]]


def detach_kwargs() -> dict:
    """``subprocess`` keyword args that fully detach a child so it outlives the parent
    and the parent's terminal closing.

    POSIX starts a new session (``setsid``), so a terminal-close ``SIGHUP`` isn't
    delivered to the child. Windows has no sessions; the equivalent is a new process
    group that is detached from the console with no window of its own.
    """
    if _IS_WINDOWS:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def console_isolation_kwargs(*, detach_stdin: bool = True) -> dict:
    """``subprocess`` kwargs for a SYNCHRONOUS, output-captured child that must NOT disturb the
    parent's console.

    aGiTrack keeps the host console in raw mode on Windows. A child process that INHERITS that
    console can call ``SetConsoleMode`` on it (the backend CLIs do, to talk to a TTY — e.g.
    ``opencode session list`` run while building the sessions menu) and never restore it, which
    drops the host out of raw mode: the user then sees keystrokes echo as visible escape codes
    and input stops reaching aGiTrack until raw mode is reasserted. Give the child its OWN hidden
    console (``CREATE_NO_WINDOW``) so its console calls can't touch ours, and detach its stdin
    from our console (``DEVNULL``) so it neither reads our input nor probes it as a TTY (which
    also stops a TTY-expecting CLI from hanging the menu thread).

    Pass ``detach_stdin=False`` for a call that already feeds the child via ``input=`` (the two
    are mutually exclusive in ``subprocess``); the ``input=`` pipe already keeps it off the
    console.

    On POSIX, run the child in its OWN session (``start_new_session`` → ``setsid``). aGiTrack fires
    many short git subprocesses (per-turn commits, status/branch polls); left in aGiTrack's process
    group they sit in the controlling terminal's FOREGROUND group, so the terminal keeps retitling
    its tab to "git" and back — a visible flicker during the startup burst. A new session takes the
    child out of the foreground group so the title stays put. It's safe here because these children
    are output-captured with stdin detached (``DEVNULL`` or an ``input=`` pipe): they never need the
    controlling terminal — no pager, no credential prompt on the tty.
    """
    kwargs: dict = {}
    if detach_stdin:
        kwargs["stdin"] = subprocess.DEVNULL
    if _IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process.

    POSIX sends signal 0, which probes without delivering anything. Windows opens the
    process and checks it hasn't exited (an absent process can't be opened).
    """
    if _IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def terminate_pid(pid: int) -> None:
    """Ask process ``pid`` to stop — best effort, never raises.

    POSIX delivers ``SIGTERM`` (aGiTrack's helpers handle it for a clean shutdown).
    Windows has no ``SIGTERM`` delivery, so it calls ``TerminateProcess``.
    """
    if _IS_WINDOWS:
        _windows_terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def shell_execute_runas(file: str, params: str = "") -> None:
    """Launch *file* elevated via UAC (the shell's ``runas`` verb). Windows-only.

    This is the only Windows-native way for an UN-elevated process to obtain an
    elevated child without a separate always-admin helper — the same pattern VS Code,
    Slack, and GitHub Desktop use to hand a perMachine installer off to ``msiexec``.
    ``ShellExecuteExW`` shows the UAC consent dialog; the elevated process is launched
    only if the user accepts.

    Raises :class:`NotImplementedError` off Windows, and :class:`OSError` when the
    launch fails — including when the user declines the UAC prompt
    (``ERROR_CANCELLED`` = 1223) — so the caller can fall back (e.g. keep running the
    current version and remind the user to update manually).
    """
    if not _IS_WINDOWS:
        raise NotImplementedError("shell_execute_runas is Windows-only")
    import ctypes
    from ctypes import wintypes

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100  # complete the operation before returning
    SW_SHOWNORMAL = 1

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]  # Windows-only
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = params
    info.nShow = SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()  # type: ignore[attr-defined]  # Windows-only
        raise OSError(f"ShellExecuteExW(runas) failed for {file!r} (Windows error {err})")


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_terminate(pid: int) -> None:
    import ctypes

    process_terminate = 0x0001
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
