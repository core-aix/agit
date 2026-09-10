"""Global registry of the long-running aGiTrack daemons, across every repository.

aGiTrack can leave three kinds of detached process running: the **repo dashboard** (`-d`), the
**backtrace dashboard** (`--backtrace`), and the **background tracker** (`-b`). Each lives in its
own repo/temp dir, so there was no single place to see (or stop) them all. Every daemon now writes
a tiny ``<pid>.json`` into a shared directory when it starts serving and removes it when it exits;
this module reads that directory to:

* list every running daemon with its function, repo, and PID (`agitrack --daemons`), so a user can
  kill a stray one by hand; and
* after a self-update, gracefully stop and re-spawn them all so they reload the new version
  (:func:`restart_all`, called from the update restart path).

Entries whose process is gone are pruned on read, so a crashed daemon never lingers in the list.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from agitrack import __version__
from agitrack.paths import contains_segments
from agitrack.proc import UTF8_TEXT, console_isolation_kwargs, detach_kwargs, pid_alive, terminate_pid

# Human-readable name for each daemon kind, shown in `--daemons`.
KIND_LABELS = {
    "hub": "dashboard (all repos)",
    "dashboard": "repo dashboard",
    "backtrace": "backtrace dashboard",
    "background": "background mode",
    "session": "interactive session",
}

# Kinds `--daemons stop` and the update-restart may act on. An INTERACTIVE SESSION is listed but
# never signalled: `--daemons list` showed only detached daemons, so someone asking "what is
# aGiTrack running?" — usually because something holds a repo lock — got half the answer and no
# mention of the very session holding it. Listing it is the point; terminating someone's live
# conversation from a bulk sweep is not.
_STOPPABLE_KINDS = frozenset({"hub", "dashboard", "backtrace", "background"})


def _stoppable(infos: "list[DaemonInfo]") -> "list[DaemonInfo]":
    return [info for info in infos if info.kind in _STOPPABLE_KINDS]


# The internal serve flag unique to each detached daemon child — the fingerprint used to find a
# daemon directly in the OS process table (so one that never registered, e.g. started by a version
# before this registry existed, is still discovered).
_SERVE_FLAGS = (
    ("--hub-serve", "hub"),
    ("--dashboard-serve", "dashboard"),
    ("--backtrace-serve", "backtrace"),
    ("--background-serve", "background"),
)


def _custom_config_dir() -> str:
    """``AGITRACK_CONFIG_DIR`` if the user set one, else ``""``. A set value means "this is a
    private aGiTrack universe" — the process-table scan cannot honour that, so it stands down."""
    from agitrack.env import getenv_compat

    return (getenv_compat("CONFIG_DIR") or "").strip()


def _registry_dir() -> Path:
    # Honors AGITRACK_CONFIG_DIR like every other global-state path (settings, learn,
    # summarizer). CRITICAL for the test suite, whose conftest isolates that env var:
    # the registry previously ignored it, so tests exercising the update-restart path
    # ran daemons.restart_all() against the DEVELOPER'S real registry — silently
    # SIGTERM-ing their live dashboards on every full test run.
    from agitrack.env import getenv_compat

    config_dir = getenv_compat("CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    return base / "daemons"


def _entry_path(pid: int) -> Path:
    return _registry_dir() / f"{pid}.json"


def _daemon_command() -> list[str]:
    """The command that would re-launch THIS daemon process — its own argv. A frozen build runs
    the exe directly; a normal install re-invokes ``python -m agitrack`` with the safe import
    path, so a re-launch from a directory holding a stray ``agitrack`` package still loads the
    installed one (see ``proc.agitrack_invocation``)."""
    from agitrack.proc import agitrack_invocation

    return [*agitrack_invocation(), *sys.argv[1:]]


@dataclass
class DaemonInfo:
    pid: int
    kind: str
    repo: str
    url: str = ""
    version: str = ""
    cmd: list[str] = field(default_factory=list)
    started: int = 0

    @property
    def function(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def repo_name(self) -> str:
        name = Path(self.repo).name if self.repo else ""
        return name or (self.repo or "?")


def register(kind: str, repo: str | os.PathLike[str], *, url: str = "", cmd: list[str] | None = None) -> None:
    """Record THIS process as a running daemon of ``kind`` for ``repo``. Best-effort — a failure
    to write the registry entry never breaks the daemon itself."""
    try:
        directory = _registry_dir()
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": os.getpid(),
            "kind": kind,
            "repo": str(repo),
            "url": url,
            "version": __version__,
            "cmd": cmd or _daemon_command(),
            "started": int(time.time()),
        }
        from agitrack.fileio import atomic_write_text

        atomic_write_text(_entry_path(os.getpid()), json.dumps(record))
    except OSError:
        pass


def deregister(pid: int | None = None) -> None:
    """Remove a daemon's registry entry (defaults to this process). Best-effort."""
    try:
        _entry_path(pid if pid is not None else os.getpid()).unlink()
    except OSError:
        pass


def list_running(*, repo: str | os.PathLike[str] | None = None) -> list[DaemonInfo]:
    """Every aGiTrack daemon currently alive, optionally only those serving ``repo``.

    Combines two sources so nothing is missed: the registry (rich — carries the URL/version and is
    cross-platform) AND a scan of the OS process table (authoritative — finds a daemon even if it
    never wrote a registry entry, e.g. one started before this feature existed). Deduped by pid,
    with the registry entry preferred where both have it.

    ``repo`` narrows the result to daemons whose repo is that path (or inside it), which is what
    makes a scoped stop possible: the unscoped one reaches across every repository the user has."""
    by_pid: dict[int, DaemonInfo] = {info.pid: info for info in _registry_entries()}
    for info in _scan_daemon_processes():
        by_pid.setdefault(info.pid, info)  # a registered entry (URL/version) wins over a bare scan
    out = list(by_pid.values())
    if repo is not None:
        out = [info for info in out if _serves_repo(info, repo)]
    out.sort(key=lambda info: (info.kind, info.repo))
    return out


def running_repos(*, scan: bool = False) -> list[str]:
    """Resolved paths of every repository an aGiTrack daemon is working on RIGHT NOW.

    The dashboard's repo switcher unions this with the remembered list, so a repository aGiTrack
    is demonstrably tracking is offered even when nothing ever recorded it in ``repos.json`` —
    which used to happen for every headless start (see ``repos.remember``'s callers) and can
    still happen to an entry lost by that file's deliberately unlocked write.

    Registry-only by default, NOT the process-table scan: this is read while drawing a dropdown
    that the page header polls, and the scan costs a ``ps`` subprocess. Every daemon registers
    itself at startup, so the cheap source is also the complete one in practice; ``scan=True`` is
    for the rare caller that must find a daemon which never registered.

    The hub itself is excluded — it serves every repository and belongs to none."""
    seen: dict[str, None] = {}
    infos = list_running() if scan else _registry_entries()
    for info in infos:
        if info.kind == "hub" or not info.repo:
            continue
        try:
            resolved = str(Path(info.repo).expanduser().resolve())
        except OSError:
            resolved = str(Path(info.repo).expanduser())
        seen.setdefault(resolved, None)
    return list(seen)


# An aGiTrack session worktree lives at ``<base>/.agitrack/worktrees/<name>``, and a daemon
# recorded against one IS a daemon for the base repo.
_WORKTREE_SEGMENTS = "/.agitrack/worktrees/"


def _serves_repo(info: DaemonInfo, repo: str | os.PathLike[str]) -> bool:
    """Whether ``info`` is a daemon for ``repo``. Paths are resolved so a symlinked or
    relatively-recorded repo still matches; a daemon with no recorded repo never does.

    A daemon recorded on a path INSIDE ``repo`` counts only when that path is one of aGiTrack's
    own session worktrees. Any descendant used to count, which is wrong the moment a repository
    contains another one — a vendored checkout, a submodule, or simply the folder of projects
    that someone has since turned into a repository of its own. ``agitrack stop`` in the outer
    repo then reached into the inner one and ended a live session that had nothing to do with
    it, and ``--daemons stop`` scoped to the outer repo did the same."""
    if not info.repo:
        return False
    try:
        want = Path(repo).expanduser().resolve()
        have = Path(info.repo).expanduser().resolve()
    except OSError:
        return str(info.repo) == str(repo)
    if have == want:
        return True
    return want in have.parents and contains_segments(str(have), _WORKTREE_SEGMENTS)


def _registry_entries() -> list[DaemonInfo]:
    """Daemons that recorded a registry entry, pruning entries whose process has exited."""
    out: list[DaemonInfo] = []
    try:
        entries = sorted(_registry_dir().glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _safe_unlink(path)
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            _safe_unlink(path)  # stale entry from a crashed/killed daemon
            continue
        out.append(
            DaemonInfo(
                pid=pid,
                kind=str(record.get("kind", "?")),
                repo=str(record.get("repo", "")),
                url=str(record.get("url", "")),
                version=str(record.get("version", "?")),
                cmd=list(record.get("cmd") or []),
                started=int(record.get("started", 0)),
            )
        )
    return out


def _scan_daemon_processes() -> list[DaemonInfo]:
    """aGiTrack daemons found directly in the OS process table — matched by their unique internal
    ``--*-serve`` flag, so a daemon that never registered is still listed and can be restarted (its
    command line is its own re-launch command). Cross-platform (``ps`` on POSIX, PowerShell/CIM on
    Windows); best-effort, empty if neither is available (the registry alone is then used).

    Skipped entirely under a custom ``AGITRACK_CONFIG_DIR``. The scan has no way to tell which
    config dir a process belongs to, so under isolation it reported — and ``--daemons stop`` then
    killed — every daemon on the machine, including other test slots' and the developer's own live
    session. Isolation that the registry honours but the scan ignores is not isolation; the
    registry is the authority whenever the caller has asked for a private one."""
    if _custom_config_dir():
        return []
    out: list[DaemonInfo] = []
    mine = os.getpid()
    for line in _process_command_lines():
        pid_str, _, command = line.strip().partition(" ")
        if not pid_str.isdigit() or not command:
            continue
        pid = int(pid_str)
        if pid == mine:
            continue
        kind = next((k for flag, k in _SERVE_FLAGS if flag in command), None)
        if kind is None:
            continue  # the serve flags are unique to aGiTrack daemon children
        argv = _split_command(command)
        out.append(DaemonInfo(pid=pid, kind=kind, repo=_repo_from_argv(argv), cmd=argv))
    return out


def _process_command_lines() -> list[str]:
    """One ``"<pid> <full command line>"`` string per running process. POSIX uses ``ps``; Windows
    uses PowerShell's ``Win32_Process`` (``ps`` doesn't exist there). ``[]`` if the query fails."""
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId) $($_.CommandLine)" }',
        ]
    else:
        # `-x` without `-a`: THIS user's processes only. A shared machine's other users have
        # their own registries and their own daemons, and we must never list (let alone signal)
        # someone else's — signalling would fail anyway, but naming them in a stop listing is
        # already a leak of what else is running on the box.
        command = ["ps", "-xww", "-o", "pid=,args="]
    try:
        # On Windows this scan IS a PowerShell process, and it is called from console-less
        # daemons (the self-update path restarts the others). Without isolation it puts a
        # PowerShell window on the user's desktop for as long as the scan takes.
        result = subprocess.run(
            command, capture_output=True, **UTF8_TEXT, timeout=15, check=False, **console_isolation_kwargs()
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return result.stdout.splitlines()


def _split_command(command: str) -> list[str]:
    """Split a command line into argv. Windows paths use backslashes, so shell-splitting there must
    NOT treat ``\\`` as an escape (``posix=False``)."""
    try:
        return shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return command.split()


def _repo_from_argv(argv: list[str]) -> str:
    """The ``--repo`` path from a daemon's argv (empty if absent). Surrounding quotes — which
    ``posix=False`` splitting keeps on a Windows path with spaces — are stripped."""
    for index, token in enumerate(argv):
        bare = token.strip('"')
        if bare == "--repo" and index + 1 < len(argv):
            return argv[index + 1].strip('"')
        if bare.startswith("--repo="):
            return bare[len("--repo=") :]
    return ""


def restart_all(*, exclude_pid: int | None = None, log=lambda message: None) -> int:
    """Gracefully stop and re-spawn every running daemon, so they reload freshly updated code.

    Called from the update restart path. The current process (``exclude_pid``, this pid by default)
    is skipped — it restarts itself via the caller's own re-exec. Each daemon is SIGTERM'd (its
    handler shuts it down and deregisters), then re-launched from its recorded command. Re-spawned
    from a neutral directory with the safe import path, so ``python -m agitrack`` can never pick up
    a stray ``agitrack`` package in some directory — the RECORDED command line may predate the
    ``-P`` that ``proc.agitrack_invocation`` now bakes in. Best-effort and independent per
    daemon."""
    from agitrack.proc import isolated_env, safe_spawn_cwd

    skip = exclude_pid if exclude_pid is not None else os.getpid()
    env = isolated_env()
    home = str(safe_spawn_cwd(Path.home()))
    restarted = 0
    for info in _signal_targets(_stoppable([i for i in list_running() if i.pid != skip and i.cmd])):
        if info.pid == skip or not info.cmd:
            continue
        try:
            terminate_pid(info.pid)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and pid_alive(info.pid):
                time.sleep(0.05)
            deregister(info.pid)
            subprocess.Popen(
                info.cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=home,
                env=env,
                **detach_kwargs(),
            )
            restarted += 1
            log(f"restarted {info.function} for {info.repo_name} (was pid {info.pid})")
        except Exception:
            pass
    return restarted


def stop_all(
    *, exclude_pid: int | None = None, repo: str | os.PathLike[str] | None = None, log=lambda message: None
) -> tuple[int, list[str]]:
    """Stop every running aGiTrack daemon, across all repositories — or only ``repo``'s.

    Returns ``(stopped, still_running)`` — the count that went away and a description of any that
    would not, so the caller can report a partial result honestly rather than claiming success.
    Each is asked to exit and waited for; a daemon that ignores it is left alone and named rather
    than escalated, since a stuck dashboard is far less bad than one killed mid-write of its
    handshake or state. On POSIX that ask is SIGTERM and the daemon's own handler runs; on Windows
    ``terminate_pid`` is ``TerminateProcess``, so NO handler runs and the daemon is stopped
    abruptly — its registry entry is reaped here instead of by itself.

    The CURRENT process is skipped by default: `agitrack --daemons stop` run from inside a live
    session must not terminate that session.
    """
    skip = exclude_pid if exclude_pid is not None else os.getpid()
    stopped = 0
    survivors: list[str] = []
    for info in _signal_targets(_stoppable([i for i in list_running(repo=repo) if i.pid != skip])):
        if info.pid == skip:
            continue
        try:
            terminate_pid(info.pid)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and pid_alive(info.pid):
                time.sleep(0.05)
            if pid_alive(info.pid):
                survivors.append(f"{info.function} for {info.repo_name} (pid {info.pid})")
                continue
            deregister(info.pid)
            stopped += 1
            log(f"stopped {info.function} for {info.repo_name} (pid {info.pid})")
        except Exception as error:  # a daemon we cannot signal (gone, or not ours) is not a failure
            survivors.append(f"{info.function} for {info.repo_name} (pid {info.pid}): {error}")
    return stopped, survivors


def _live_agitrack_pids() -> set[int] | None:
    """PIDs on this machine whose command line is an aGiTrack daemon, or ``None`` if the process
    table could not be read.

    ``pid_alive()`` alone is not identity: a registry entry survives a crash, and after a reboot
    the OS happily reassigns that number to something else entirely — an editor, a browser, the
    user's build. Signalling it because a stale ``<pid>.json`` says "daemon" is how a stop-all
    reaches a process aGiTrack never started. One scan, taken only on the paths that actually
    signal, is the cheap way to require proof."""
    lines = _process_command_lines()
    if not lines:
        return None  # cannot verify — callers fall back to trusting the registry
    pids: set[int] = set()
    for line in lines:
        pid_str, _, command = line.strip().partition(" ")
        if pid_str.isdigit() and any(flag in command for flag, _kind in _SERVE_FLAGS):
            pids.add(int(pid_str))
    return pids


def _signal_targets(candidates: list[DaemonInfo]) -> list[DaemonInfo]:
    """``candidates`` minus any whose PID is demonstrably not an aGiTrack daemon (PID reuse).
    Stale registry entries for those PIDs are reaped on the way out."""
    live = _live_agitrack_pids()
    if live is None:
        return candidates
    out: list[DaemonInfo] = []
    for info in candidates:
        if info.pid in live:
            out.append(info)
        else:
            deregister(info.pid)
    return out


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
