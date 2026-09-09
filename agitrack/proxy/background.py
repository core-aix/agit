"""Background (headless) tracker — run aGiTrack WITHOUT the interactive TUI.

Unlike proxy mode, aGiTrack does not spawn or drive the coding agent here: the user runs the
agent from whatever UI they like (its native CLI, an IDE extension, …), and this tracker watches
the agent's local session transcript and performs the same tracking the TUI would — recording
each completed turn, summarizing it, and installing the commit hooks that fold the interaction
trace and token metadata into commits. It ALWAYS runs without a worktree (it operates on the
current branch), with either manual (user-triggered) or auto (aGiTrack-triggered) commits.

This is the interactive-UI-agnostic tracker of issue #143: because it keys off the on-disk
session transcript rather than a PTY, it tracks a session no matter how the user drives the agent.

Both modes record each turn as a hidden latent commit on ``refs/agitrack/manual/<id>`` and rely
on a ``prepare-commit-msg`` hook to fold the pending turns' trace/metadata into the commit the
agent (or user) makes — a metadata-only cover commit is the fallback when the hook can't run.
The only difference is who triggers the commit: in **manual** mode the user does; in **auto**
mode aGiTrack folds the pending turns into a commit itself once the agent finishes a turn and
hasn't committed its own work. So in auto mode the agent's OWN commits fold via the hook, and the
cover is only a backup — exactly the requested behavior.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, cast

from agitrack import __version__, tracking_gap
from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.commits import ManualCommitTracker
from agitrack.commits.message import build_auto_fold_message, is_fully_tracked_message, summary_metadata_lines
from agitrack.config import AgitrackState, GlobalConfig
from agitrack.events import EventLog, exclude_log_file, resolve_log_path
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks
from agitrack.proc import detach_kwargs, pid_alive, terminate_pid
from agitrack.proxy.commit_engine import CommitEngine, turn_is_finished
from agitrack.proxy.session import Session


# How long the list of installed backends is trusted before it is established again. The
# tracker asks for it on every poll cycle, and answering costs a PATH search plus (for a backend
# that isn't there) an `npm` call — so it is cached; long-running trackers still notice an agent
# installed after they started, within this window.
_INSTALLED_BACKENDS_TTL_SECONDS = 300.0


def background_handshake_path(repo: GitRepo) -> Path:
    """Where a running background tracker records its pid, so `agitrack -b stop`/`status` can
    find it. Separate from the repo lock (which every mode shares) so stop/status target ONLY a
    background tracker, never a foreground TUI holding the same repo."""
    return repo.repo / ".agitrack" / "background.json"


def _read_handshake(repo: GitRepo) -> dict | None:
    try:
        data = json.loads(background_handshake_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _recorded_backend(state) -> str | None:
    """The backend this repo/session is configured to use, or None when none is.

    ``AgitrackState.backend`` deliberately RAISES rather than guessing, which is right for a
    spawn path but wrong inside the commit hook: there the raise was swallowed whole and the
    hook became a silent no-op. This asks the same question in a form that has a "no" answer."""
    try:
        return state.backend
    except Exception:
        return None


def _live_background_pid(repo: GitRepo) -> int | None:
    """The pid of a live background tracker on this repo, or None (also clears a stale file)."""
    info = _read_handshake(repo)
    pid = info.get("pid") if info else None
    if isinstance(pid, int) and pid_alive(pid):
        return pid
    if info is not None:  # stale handshake from a crashed tracker — clean it up
        try:
            background_handshake_path(repo).unlink()
        except OSError:
            pass
    return None


def background_tracker_is_running(repo: GitRepo) -> bool:
    """Whether a background tracker is alive on this repo. Public because the Claude session
    hook asks it on every session start: the note it prints ("aGiTrack commits for you") is
    only TRUE while a tracker is running, so a hook left behind by a daemon that was killed
    rather than stopped must stay silent instead of misleading the agent."""
    return _live_background_pid(repo) is not None


def background_status(repo: GitRepo) -> int:
    """Report whether a background tracker is running on this repo (`agitrack -b status`)."""
    pid = _live_background_pid(repo)
    # NAME THE REPO. `-s`, `-b status` and the `-b` banner never did, which is exactly wrong in a
    # submodule or a multi-repo terminal — the one place the answer needs disambiguating.
    where = _abbreviated_repo(repo)
    if pid is None:
        print(f"No aGiTrack background tracker is running on {where}.")
    else:
        info = _read_handshake(repo) or {}
        mode = info.get("mode", "?")
        print(f"aGiTrack background tracker is running on {where} (PID {pid}, {mode}).")
    print(autostart_status_line(repo))
    from agitrack.update.marker import update_reminder_line

    reminder = update_reminder_line(repo.repo)
    if reminder:
        print(reminder)
    return 0


def stop_background(repo: GitRepo) -> int:
    """Stop the background tracker running on this repo (`agitrack -b stop`). Sends SIGTERM and
    waits briefly for a clean shutdown (it records any final turn and removes its hooks)."""
    pid = _live_background_pid(repo)
    # Read the daemon's record BEFORE terminating it: on POSIX its own teardown deletes the
    # handshake, and the `--log-file` it was started with lives nowhere else.
    running = _read_handshake(repo) or {}
    # Open the untracked stretch from HERE, before the tracker is asked to go: the user has
    # already decided, and marking first means a stop that then fails to reach a wedged daemon
    # still leaves the record that the next start reads. Marking is idempotent within one gap,
    # so `agitrack stop` calling through to here does not move the moment it began.
    tracking_gap.mark_stopped(repo.repo)
    if pid is None:
        print("No aGiTrack background tracker is running on this repo.")
        _disarm_tracking(repo)  # a tracker that died without tearing down may have left it armed
        return 0
    if not _terminate_and_wait(pid):
        print(f"aGiTrack background tracker (PID {pid}) did not stop in time; it may still be shutting down.")
        return 1
    # Also from HERE, not only from the daemon's own teardown: Windows has no SIGTERM
    # delivery, so `-b stop` calls TerminateProcess and the daemon's finally block never runs.
    # The stopping process knows the repo, so it can do the cleanup the killed one could not —
    # otherwise every Windows `-b stop` left a .claude/settings.local.json behind (measured).
    disarmed = _disarm_tracking(repo)
    # Reap the stopped daemon's REGISTRY entry too. `-b stop` reaped the handshake and left the
    # registry file, so dead-pid entries accumulated across kill/restart cycles (1 → 6 over three)
    # and were only ever pruned lazily, on the next read by something else.
    try:
        from agitrack import daemons
        from agitrack.git import RepoLock

        daemons.deregister(pid)
        # ...and the lock file's owner record, for the same reason: on Windows the terminated
        # daemon's own release() never ran, so a clean stop looked exactly like a crash on disk.
        RepoLock(repo.repo / ".agitrack" / "lock").clear_owner_record()
    except Exception:
        pass
    # Also from HERE for the same reason as the disarm above: the daemon emits `daemon-stop` in
    # its own teardown, which never runs when it was TerminateProcess'd. `daemon-start` was
    # therefore the only event a `--log-file` reader ever saw across a full -b lifecycle.
    _emit_stop_event(repo, log_file=running.get("log_file"))
    print("Stopped the aGiTrack background tracker.")
    if disarmed:
        print("Auto-start is off for this repo until you run `agitrack -b` again.")
    print(UNTRACKED_FROM_NOW_NOTE)
    return 0


# Said by every command that switches tracking off, so the guarantee is stated wherever the
# decision is made. The stretch until the next start is the user's own: its conversation is
# never quoted into a commit message (see agitrack.tracking_gap).
UNTRACKED_FROM_NOW_NOTE = "Conversations from now until you start aGiTrack again stay out of commit messages."


def _emit_stop_event(repo: GitRepo, *, log_file: str | None = None) -> None:
    """Append ``daemon-stop`` to the event log the RUNNING daemon was using, best-effort.

    Idempotent enough: the daemon's own teardown emits it too on POSIX, and one duplicate line
    is far better than the silence a Windows stop produced.

    The daemon's log path comes from the handshake first and the config only as a fallback,
    because ``--log-file`` is a command-line flag that is never persisted: a tracker started as
    ``agitrack -b --log-file events.log`` had no ``log_file`` in config, so this read None and
    emitted nothing. On Windows — where `-b stop` is a ``TerminateProcess`` and the daemon's own
    teardown never runs — ``daemon-start`` was therefore the only line a ``--log-file`` reader
    ever saw across a full ``-b`` lifecycle, which is the very thing this function exists to
    prevent."""
    try:
        from agitrack.config import GlobalConfig

        spec = log_file or (_read_handshake(repo) or {}).get("log_file")
        log = EventLog(resolve_log_path(spec or getattr(GlobalConfig(), "log_file", None), repo.repo))
        log.emit("daemon-stop")
    except Exception:
        pass


def _disarm_tracking(repo: GitRepo) -> bool:
    """Everything `stop` has to mean beyond killing the process.

    STOP MEANS STOP. Two things would otherwise keep acting on the user's behalf after they
    asked aGiTrack to stop: the auto-start hooks would bring the tracker back on the next
    commit or the next agent turn, and the Claude session note would keep telling the agent
    that aGiTrack is committing for it — which, with no tracker running, is false and stops
    the agent committing at all.

    The ``autotrack_hook`` PREFERENCE is deliberately left alone: the user's standing choice
    about auto-start is not revoked by one stop, so the next `agitrack -b` re-arms it. Only
    `--remove-hooks` turns the preference itself off. Returns True when auto-start was armed
    and is now disarmed."""
    disarmed = False
    try:
        from agitrack.backends import agent_hooks, claude_settings

        agent_hooks.remove_commit_guidance(repo.repo)
        # BOTH auto-start triggers, on every backend: a stop that left the session-start hook
        # behind would be undone by the user simply opening their agent again, which is not a
        # stop. See agent_hooks for why removal is never scoped to the current backend.
        disarmed |= agent_hooks.remove_agent_autostart(repo.repo)
        disarmed |= claude_settings.remove_autostart_hook(repo.repo)
    except Exception:
        pass
    try:
        hooks_dir = repo.hooks_dir()
        # Ask BEFORE removing: the remover is a no-op on a hook that isn't ours and reports
        # nothing either way, so "was it armed" has to be answered while it is still there.
        was_armed = git_hooks.is_autotrack_hook(hooks_dir / "pre-commit")
        git_hooks.remove_autotrack_precommit_hook(hooks_dir)
        # The manual-commit fold hooks too. `-b stop` removed `pre-commit` and the `.claude`
        # entries and left `prepare-commit-msg` and `post-commit` intercepting every commit, with
        # nothing telling the user two aGiTrack hooks were still installed. On Windows this is the
        # only chance to do it at all: `-b stop` is TerminateProcess, so the daemon's own teardown
        # never runs. Restores whatever project hooks they chained.
        git_hooks.remove_manual_commit_hooks(hooks_dir)
        disarmed = disarmed or was_armed
    except Exception:
        pass
    return disarmed


def handshake_is_manual(info: dict | None) -> bool:
    """Whether a handshake record describes a MANUAL-commit tracker. The field is a human-readable
    label (``"manual commits"`` / ``"auto commits"``), so match on the word."""
    mode = (info or {}).get("mode")
    return isinstance(mode, str) and "manual" in mode


def _running_tracker_is_current(repo: GitRepo, *, owner_pid: int | None = None, manual: bool | None = None) -> bool:
    """True when a LIVE background tracker on this repo is already running the current aGiTrack
    version IN THE REQUESTED COMMIT MODE — so a rerun of ``agitrack -b`` has nothing new to load
    and should leave it in place rather than SIGTERM-and-respawn it. Returns False when no
    background tracker is running, when something else (an interactive TUI) holds the lock
    (``owner_pid`` mismatch), when the running version differs from ours (a genuine update —
    restart to pick it up), or when *manual* asks for a commit mode the running daemon isn't in
    (``agitrack -b -m`` over an auto-commit daemon, or ``-b --auto-commit`` over a manual one):
    that's a deliberate MODE SWITCH, so the daemon must be replaced, not left in place."""
    pid = _live_background_pid(repo)
    if pid is None or (owner_pid is not None and pid != owner_pid):
        return False
    info = _read_handshake(repo) or {}
    if info.get("version") != __version__:
        return False
    return manual is None or handshake_is_manual(info) == manual


def replace_running_tracker(repo: GitRepo, *, owner_pid: int | None) -> bool:
    """``agitrack -b`` invoked while the repo lock is held: if the holder is a LIVE background
    tracker, stop it so this rerun replaces it — like ``-d`` and ``--backtrace``, rerunning
    the command must pick up updated aGiTrack code. Only a background tracker is replaced;
    when anything else holds the lock (an interactive TUI session) this returns False and the
    caller keeps refusing to start. True once the old tracker has shut down cleanly."""
    pid = _live_background_pid(repo)
    if pid is None or (owner_pid is not None and pid != owner_pid):
        return False
    if not _terminate_and_wait(pid):
        print(f"aGiTrack background tracker (PID {pid}) did not stop in time; run `agitrack -b stop` and try again.")
        return False
    print(f"Restarting the aGiTrack background tracker (was PID {pid}).")
    return True


def _terminate_and_wait(pid: int, *, timeout: float = 10.0) -> bool:
    """SIGTERM ``pid`` and wait (bounded) for its clean shutdown — the tracker records any
    final turn and removes its hooks on the way out. True once the process is gone."""
    terminate_pid(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.1)
    return not pid_alive(pid)


def background_log_path(repo: GitRepo) -> Path:
    """Where the detached daemon's stdout/stderr go (startup errors and per-turn notices land
    here — the daemon has no terminal). Mirrors the dashboard's ``dashboard.log``."""
    return repo.repo / ".agitrack" / "background.log"


def _flush_request_path(repo: GitRepo) -> Path:
    return repo.repo / ".agitrack" / "flush-request"


def _flush_done_path(repo: GitRepo) -> Path:
    return repo.repo / ".agitrack" / "flush-done"


def request_daemon_flush(repo: GitRepo, *, timeout: float = 5.0) -> bool:
    """Ask the running background daemon to record any pending COMPLETED turns and (re)render the
    fold trailer RIGHT NOW, then wait (bounded) for it to acknowledge.

    Called from the pre-commit hook (``precommit_sync``) when a daemon holds the repo lock: the
    daemon is the single writer, so the hook can't safely record turns itself, but the commit being
    made needs an up-to-date ``manual-pending-trailer`` for its ``prepare-commit-msg`` fold — not
    one lagging the daemon's poll. We write a unique nonce and spin until the daemon echoes it back
    (it services the request within ~0.1s). Best-effort: on timeout the commit simply proceeds with
    whatever the daemon last rendered. Returns True once acknowledged."""
    nonce = f"{os.getpid()}-{time.time()}"
    try:
        _flush_request_path(repo).write_text(nonce, encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _flush_done_path(repo).read_text(encoding="utf-8").strip() == nonce:
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def proxy_status_path(repo: GitRepo) -> Path:
    """Where the INTERACTIVE proxy records its pid + mode so `agitrack --status` can report it
    (the daemon uses ``background.json`; the shared repo lock only carries a pid, not the mode)."""
    return repo.repo / ".agitrack" / "session.json"


def write_proxy_status(repo: GitRepo, *, commits: str, worktree: bool) -> None:
    """Record the running interactive session's mode for `--status` (best-effort)."""
    try:
        path = proxy_status_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": os.getpid(), "mode": "interactive", "commits": commits, "worktree": bool(worktree)}),
            encoding="utf-8",
        )
    except OSError:
        pass


def clear_proxy_status(repo: GitRepo) -> None:
    try:
        path = proxy_status_path(repo)
        info = None
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = None
        if info is None or info.get("pid") == os.getpid():  # only clear our own record
            path.unlink()
    except OSError:
        pass


def _read_proxy_status(repo: GitRepo) -> dict | None:
    try:
        data = json.loads(proxy_status_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _abbreviated_repo(repo: GitRepo) -> str:
    """The repo path, with ``$HOME`` shortened — for status lines that must say WHICH repo."""
    try:
        from agitrack.metrics.collect import _abbreviate_home

        return _abbreviate_home(str(repo.repo))
    except Exception:
        return str(getattr(repo, "repo", "?"))


def autostart_armed(directory: Path) -> bool:
    """Whether something is in place to start tracking ``directory`` ON ITS OWN — the git
    pre-commit hook, or a backend's session-start hook — with none of the standing refusals in
    the way. A PATH, not a GitRepo: the dashboard asks this for every repository in its switcher
    (see :func:`agitrack.git.hooks.hooks_dir_for_path`).

    This is the difference between a repository that is merely idle and one that is finished
    with. Both have no process running, and for a long time the dashboard called both "off" and
    listed both forever — so a project that starts tracking itself the moment you commit looked
    exactly like a scratch directory nobody will ever open again.

    The refusals are the SAME ones :func:`_autostart_daemon_args` applies when a hook actually
    fires, and in the same order, because a hook that would be turned away is not armed:
    the ``autotrack_hook`` preference being off, and a recorded `agitrack stop` that still
    stands. Anything unreadable answers False — claiming a repo will restart itself when it
    will not is the worse of the two mistakes here.

    Both triggers count. :func:`autostart_status_line` reports the pre-commit one specifically
    (its subject is "will THIS COMMIT be tracked"); this one's subject is "will aGiTrack come
    back here by itself", which a backend's session-start hook also answers yes to."""
    try:
        config = GlobalConfig()
        config.load_repo_overlay(directory)
        if config.autotrack_hook == "off":
            return False
        if tracking_gap.gap_is_open(directory):
            return False
        hooks_dir = git_hooks.hooks_dir_for_path(directory)
        if hooks_dir is not None and git_hooks.is_autotrack_hook(hooks_dir / "pre-commit"):
            return True
        from agitrack.backends import agent_hooks

        return bool(agent_hooks.installed_autostart_backends(directory))
    except Exception:
        return False


def autostart_status_line(repo: GitRepo) -> str:
    """One line saying whether a commit made with aGiTrack down will actually be tracked.

    This used to be derived from the ``autotrack_hook`` PREFERENCE alone and never from the hook
    file, so it lied in four separate situations, all confirmed live:

    * a virgin repo reported ``Auto-start: on`` before any hook existed — the first commit after
      agent work was silently lost and no tracker ever started;
    * ``agitrack -b stop`` printed "Auto-start is off for this repo" and really did remove the
      hooks, and ``-s`` a second later still said ``on``;
    * with ``core.hooksPath`` set, aGiTrack installs nothing at all and ``-s`` asserted the
      opposite — anyone on Husky / pre-commit / lefthook was told their commits were tracked
      when nothing was;
    * every backend was told "…or when an agent turn leaves changes", which only Claude Code
      does; on Codex and OpenCode nothing is picked up until you commit.

    So each clause is now checked rather than assumed, and the mode placeholder ("last-run")
    that leaked onto a fresh repo is gone."""
    try:
        config = GlobalConfig()
        config.load_repo_overlay(repo.repo)
        if config.autotrack_hook == "off":
            return "Auto-start: off (`agitrack -b` or Ctrl-G → settings to enable)."
        if repo.core_hooks_path():
            return (
                "Auto-start: unavailable — this repo sets core.hooksPath, so git ignores the hooks "
                "directory aGiTrack installs into.\n"
                "  Commits made while aGiTrack is down are NOT tracked. Run `agitrack` or "
                "`agitrack -b` while you work, or unset core.hooksPath."
            )
        if not git_hooks.is_autotrack_hook(repo.hooks_dir() / "pre-commit"):
            return (
                "Auto-start: not armed — no aGiTrack hook is installed in this repo yet.\n"
                "  Run `agitrack` or `agitrack -b` once; the hook it leaves behind is what tracks "
                "commits made later."
            )
        last = read_background_mode(repo)
        mode = "manual-commit" if last else "auto-commit"
        triggers = "on a commit"
        # The turn-end half exists only where a backend offers a turn-end hook, and only when
        # that hook is actually on disk for this repo.
        from agitrack.backends import claude_settings

        if claude_settings.hook_is_installed(repo.repo, claude_settings.AUTOSTART_HOOK):
            triggers += ", or when an agent turn leaves changes"
        return f"Auto-start: on ({mode} mode; {triggers})."
    except Exception:
        return "Auto-start: unknown (could not read this repo's hooks)."


def running_mode(repo: GitRepo) -> dict:
    """What aGiTrack is doing for ``repo`` right now, as data.

    The same question ``agitrack -s`` answers in prose, for callers that need it structured: the
    dashboard says it in its header, because a page that looks identical whether or not anything
    is being tracked cannot answer "is this on?", which is the first thing anyone asks of it.

    Returns ``{"running", "armed", "kind", "label", "detail", "pid"}``. ``kind`` is one of
    ``background``, ``interactive``, ``unknown`` (something holds the repo lock but left no mode
    record), ``armed`` (nothing running, but a hook will start one — see :func:`autostart_armed`)
    or ``none``. Never raises: an unreadable repository reports "not tracking" rather than
    breaking the page that asked.
    """
    unknown = {"running": False, "armed": False, "kind": "none", "label": "not tracking", "detail": "", "pid": None}
    try:
        from agitrack.git import RepoLock

        bg_pid = _live_background_pid(repo)
        if bg_pid is not None:
            info = _read_handshake(repo) or {}
            commits = "manual commits" if handshake_is_manual(info) else "auto commits"
            backend = str(info.get("backend") or "?")
            return {
                "running": True,
                "armed": False,  # something is already running; auto-start is not the question
                "kind": "background",
                "label": f"tracking · background · {commits}",
                "detail": (
                    f"aGiTrack is tracking this repository in background mode (PID {bg_pid}): "
                    f"{commits}, no worktree, backend {backend}. Stop it with `agitrack stop`."
                ),
                "pid": bg_pid,
            }
        proxy = _read_proxy_status(repo) or {}
        proxy_pid = proxy.get("pid")
        if isinstance(proxy_pid, int) and pid_alive(proxy_pid):
            commits = "manual commits" if proxy.get("commits") == "manual" else "auto commits"
            worktree = "worktree" if proxy.get("worktree") else "your working tree"
            return {
                "running": True,
                "armed": False,  # something is already running; auto-start is not the question
                "kind": "interactive",
                "label": f"tracking · interactive · {commits}",
                "detail": (
                    f"An interactive aGiTrack session is running here (PID {proxy_pid}): {commits}, {worktree}."
                ),
                "pid": proxy_pid,
            }
        owner = RepoLock(repo.repo / ".agitrack" / "lock").owner_pid()
        if isinstance(owner, int) and pid_alive(owner):
            return {
                "running": True,
                "armed": False,  # something is already running; auto-start is not the question
                "kind": "unknown",
                "label": "tracking",
                "detail": f"aGiTrack holds this repository (PID {owner}); the mode was not recorded.",
                "pid": owner,
            }
        # Nothing is running — but "idle" and "finished with" are different states, and saying
        # "not tracking" to both was wrong in the direction that costs the user work: with the
        # auto-start hook in place, the very next commit (or agent session) starts a tracker and
        # the work IS recorded. The page header and the switcher both read this.
        if autostart_armed(Path(repo.repo)):
            return {
                "running": False,
                "armed": True,
                "kind": "armed",
                "label": "auto-start armed",
                "detail": (
                    "aGiTrack is not running here right now, but auto-start is armed: your next "
                    "commit — or opening an agent in this repository — starts the tracker. Turn "
                    "it off for good with `agitrack --remove-hooks`."
                ),
                "pid": None,
            }
        return {
            "running": False,
            "armed": False,
            "kind": "none",
            "label": "not tracking",
            "detail": (
                "aGiTrack is not running on this repository, so new agent work is not being "
                "recorded. Run `agitrack` in it and pick a mode."
            ),
            "pid": None,
        }
    except Exception:
        return unknown


def running_mode_for(directory: Path) -> dict:
    """:func:`running_mode` for a plain path, without opening a git repository.

    Everything the answer reads lives under ``<dir>/.agitrack/`` (the tracker's handshake, the
    interactive session's status file, the repo lock), so it needs the PATH and nothing else. That
    matters because the dashboard asks this for every repository in its switcher, including ones
    that are not git repositories at all (a directory can be listed for its reconstruction alone),
    and because opening a repo per row would put git on the path of drawing a dropdown."""
    return running_mode(cast(GitRepo, _PathOnlyRepo(Path(directory))))


class _PathOnlyRepo:
    """The one attribute :func:`running_mode` uses. Not a GitRepo, and never used as one."""

    def __init__(self, directory: Path) -> None:
        self.repo = directory


def repo_status(repo: GitRepo) -> int:
    """`agitrack --status` / `-s`: report whether aGiTrack is running for this repo and in which
    mode (interactive vs background, auto vs manual commit, worktree vs no-worktree)."""
    from agitrack.git import RepoLock
    from agitrack.update.marker import update_reminder_line

    def _commit_mode(handshake_mode: object) -> str:
        return "manual-commit" if isinstance(handshake_mode, str) and "manual" in handshake_mode else "auto-commit"

    bg_pid = _live_background_pid(repo)
    if bg_pid is not None:
        info = _read_handshake(repo) or {}
        print(
            f"aGiTrack is running on {_abbreviated_repo(repo)} in BACKGROUND mode (PID {bg_pid}): "
            f"{_commit_mode(info.get('mode'))}, no worktree, backend {info.get('backend', '?')}."
        )
    else:
        proxy = _read_proxy_status(repo)
        proxy_pid = proxy.get("pid") if proxy else None
        if proxy is not None and isinstance(proxy_pid, int) and pid_alive(proxy_pid):
            commits = "manual-commit" if proxy.get("commits") == "manual" else "auto-commit"
            worktree = "worktree" if proxy.get("worktree") else "no worktree"
            print(
                f"aGiTrack is running on {_abbreviated_repo(repo)} in INTERACTIVE mode "
                f"(PID {proxy_pid}): {commits}, {worktree}."
            )
        else:
            owner = RepoLock(repo.repo / ".agitrack" / "lock").owner_pid()
            if isinstance(owner, int) and pid_alive(owner):
                print(f"aGiTrack is running on {_abbreviated_repo(repo)} (PID {owner}); mode details are unavailable.")
            else:
                print(f"aGiTrack is not running on {_abbreviated_repo(repo)}.")
    print(autostart_status_line(repo))
    # WHICH confinement mode is in effect. Nothing anywhere reported it — not the TUI, not `-s`,
    # not the status bar — so a run on a host where the sandbox cannot be created (stock Ubuntu
    # 24.04 denies unprivileged user namespaces) looked exactly like an enforced one, and what
    # looked like enforcement was the agent choosing to comply.
    from agitrack.proxy import sandbox

    print(sandbox.status_line())
    reminder = update_reminder_line(repo.repo)
    if reminder:
        print(reminder)
    return 0


def _background_mode_path(repo: GitRepo) -> Path:
    return repo.repo / ".agitrack" / "background-mode"


def write_background_mode(repo: GitRepo, *, manual: bool) -> None:
    """Persist the commit mode of the LAST run ("manual"/"auto"), so an auto-start on a later commit
    resumes the mode the user last chose. Survives the tracker stopping (unlike the handshake), and
    is written by BOTH the background daemon and the interactive proxy — otherwise switching to
    interactive auto-commit and then committing would auto-start a manual-commit daemon."""
    try:
        path = _background_mode_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("manual" if manual else "auto", encoding="utf-8")
    except OSError:
        pass


def read_background_mode(repo: GitRepo) -> bool | None:
    """The last background run's commit mode as ``manual`` (True) / ``auto`` (False), or None when
    no run has been recorded yet."""
    try:
        text = _background_mode_path(repo).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if text == "manual":
        return True
    if text == "auto":
        return False
    return None


def _open_log(repo: GitRepo) -> Any:
    try:
        path = background_log_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    except OSError:
        return subprocess.DEVNULL


def wait_for_handshake(repo: GitRepo, *, pid: int, timeout: float) -> dict | None:
    """Poll for the handshake the daemon child with ``pid`` writes once it starts tracking.

    Correlating on the child's pid means a stale record from an earlier daemon is never
    mistaken for this launch. Returns the record, or None if the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _read_handshake(repo)
        if info is not None and info.get("pid") == pid:
            return info
        if not pid_alive(pid):  # child died during startup (e.g. lock lost, backend missing)
            return None
        time.sleep(0.05)
    return None


def spawn_background_daemon(repo: GitRepo, *, extra_args: list[str]) -> subprocess.Popen[bytes]:
    """Launch the detached background-tracker child and return its Popen handle.

    The child re-execs ``agitrack --background --background-serve`` in its own session
    (``detach_kwargs``) so it survives the launcher returning and the terminal closing, and
    is not hit by Ctrl-C in the launcher's terminal. Unlike the dashboard daemon there is NO
    owner-pid watchdog — a tracker must outlive whatever launched it (stop it with
    ``agitrack -b stop``). stdout/stderr go to a log file so a startup failure is recoverable.

    The child must load the INSTALLED aGiTrack, never an ``agitrack`` directory that happens to
    sit in the repository being tracked — which is exactly what a repo holding the projects you
    track (aGiTrack's own checkout among them) does hold. See ``proc.agitrack_invocation``: the
    import isolation is the difference between a tracker and a traceback in ``background.log``
    that nobody reads while the launcher says the daemon is live."""
    from agitrack.proc import agitrack_invocation, isolated_env, safe_spawn_cwd

    cmd = [
        *agitrack_invocation(),
        "--repo",
        str(repo.repo),
        "--background",
        "--background-serve",
        "--skip-privacy-ack",  # the interactive launcher already acknowledged it
        *extra_args,
    ]
    log = _open_log(repo)
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(safe_spawn_cwd(repo.repo)),
            env=isolated_env(),
            **detach_kwargs(),
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()  # the child holds its own dup of the fd


def start_background_daemon(repo: GitRepo, *, extra_args: list[str], timeout: float = 8.0) -> int:
    """`agitrack -b`: start the background tracker as a detached daemon and return to the shell.

    Restarts a daemon already running for this repo rather than leaving the old one in place —
    like ``-d`` and ``--backtrace``, re-running the command after an aGiTrack update must run
    the NEW code. The old tracker gets its clean SIGTERM shutdown first (it records any final
    turn and removes its hooks). The daemon keeps running after the terminal closes; stop it
    with ``agitrack -b stop``.

    Exception: a daemon already running the CURRENT version IN THE REQUESTED COMMIT MODE is left
    untouched — restarting it would only churn (stop+respawn) with no new code to load. That
    needless teardown is what made the tracker appear to "quit" whenever aGiTrack was invoked again
    for an unrelated reason. A run asking for the OTHER commit mode still replaces it, so
    ``agitrack -b -m`` over an auto-commit daemon really does switch modes."""
    # THE user start. This is the one background entry point a person types, so it is the one
    # that ends a stretch `agitrack stop` opened (an auto-start hook is not the user starting
    # aGiTrack, and refuses while the stop stands). Before the spawn, so the daemon never runs a
    # poll cycle without the floor in place, and before the already-running early return, so
    # `agitrack -b` over a tracker that somehow survived the stop still means "track from now".
    _resume_tracking_for(repo)
    # The launcher always forwards one of these two, so the requested commit mode is unambiguous.
    manual: bool | None = None
    if "--manual-commits" in extra_args:
        manual = True
    elif "--auto-commit" in extra_args:
        manual = False
    running = _live_background_pid(repo)
    if running is not None and _running_tracker_is_current(repo, owner_pid=running, manual=manual):
        print(f"\naGiTrack background tracker already running (PID {running}, current version) — left in place.")
        return 0
    if running is not None:
        info = _read_handshake(repo) or {}
        if not _terminate_and_wait(running):
            print(
                f"\naGiTrack background tracker (PID {running}) did not stop in time; "
                "run `agitrack -b stop` and try again."
            )
            return 1
        print(f"\nRestarting the aGiTrack background tracker (was PID {running}, {info.get('mode', '?')}).")
    proc = spawn_background_daemon(repo, extra_args=extra_args)
    record = wait_for_handshake(repo, pid=proc.pid, timeout=timeout)
    if record is None and pid_alive(proc.pid):
        # STILL STARTING is not FAILED. The daemon publishes its handshake last, after the git
        # work that arms the repo (hooks, the latent-ref prune, the trailer), and on a big
        # repository that work outruns this wait: a 14 GB repo measured ~9.5s to arm, so a start
        # that was working perfectly was reported as "did not start" — and, because that answer
        # was an error, the dashboard was not opened either. Saying so honestly, and returning
        # success, leaves the tracker that IS coming up alone; a child that has actually died is
        # a different answer, below, and this branch cannot reach it because the process is gone.
        print(
            f"\naGiTrack background tracker (PID {proc.pid}) is still starting — this repository is "
            f"taking longer than {timeout:.0f}s to arm. It keeps coming up in the background; check it "
            f"with `agitrack -b status`, or see {background_log_path(repo)} if it never arrives."
        )
        return 0
    if record is None:
        print(f"\nThe aGiTrack background tracker did not start. See {background_log_path(repo)} for details.")
        return 1
    # Leading blank line separates this from the preceding startup messages (privacy ack, the
    # auto-start hook prompt) so each part of aGiTrack's start-up output reads as its own block.
    print(
        f"\naGiTrack background tracker daemon live (PID {record.get('pid')}, {record.get('mode', '?')}, no worktree).\n"
        "Drive the agent from any UI; it keeps tracking in the background. Stop it with `agitrack -b stop`."
    )
    # The one hook step aGiTrack cannot take for the user: Codex refuses to run a hook nobody has
    # reviewed, and says nothing when it skips one. Silence there would look exactly like a
    # working install, so the daemon's own start is where it gets said. See
    # codex_settings.trust_reminder.
    try:
        from agitrack.backends import agent_hooks

        notice = agent_hooks.startup_notice(repo.repo)
        if notice:
            print(f"\naGiTrack: {notice}")
    except Exception:
        pass
    return 0


# How long the session-start hook waits for the tracker it just spawned to hand back its
# handshake. The agent's session start is BLOCKED for this long, so it is deliberately short:
# the daemon writes the handshake as one of its first acts, and if it is slower than this the
# hook gives up on the note rather than making the user wait for their prompt.
_AGENT_AUTOSTART_HANDSHAKE_SECONDS = 5.0


def _resume_tracking_for(repo: GitRepo) -> None:
    """End a stretch `agitrack stop` opened, best-effort. Called only from a start a PERSON asked
    for; a gap record that cannot be written must never keep a tracker from starting."""
    try:
        if tracking_gap.resume_tracking(repo.repo) is not None:
            print("\nTracking again. Turns from while aGiTrack was stopped stay out of commit messages.")
    except Exception:
        pass


def _stop_still_stands(repo: GitRepo) -> bool:
    """Whether `agitrack stop` is still in force here, so nothing may start tracking on its own.

    `stop` already promises auto-start is off "until you run `agitrack -b` (or `agitrack -i`)
    again", and removing the hooks is not enough on its own to keep it: the removal cannot reach a
    backend that has ALREADY loaded its plugin (an OpenCode server outliving the agent command
    that started it puts a tracker back within seconds of the stop, measured), and a hook file
    the user restores from their dotfiles arms it again. The recorded stop is the authority the
    hook files are not. Errs toward starting: a record that cannot be read must not leave a repo
    untracked forever."""
    try:
        return tracking_gap.gap_is_open(repo.repo)
    except Exception:
        return False


def _installed_backend(preferred: str | None = None) -> str | None:
    """``preferred`` when it names a backend installed on this machine; otherwise any installed
    backend; otherwise None — i.e. "an agent this machine can actually run".

    Used to answer "is there anything here to track at all?", never to OVERRIDE a backend the
    repo has recorded: which one a tracker starts on matters far less than it used to (it
    follows whichever backend the person opens — see
    ``BackgroundRunner._follow_the_driven_backend``), and a daemon handed a recorded backend
    that is not installed moves itself onto one that is, without rewriting the user's global
    default the way an explicit ``--backend`` would."""
    from agitrack.backends.proxy_agents import available_backends
    from agitrack.backends.setup import backend_installed

    names = available_backends()
    ordered = ([preferred] if preferred in names else []) + [name for name in names if name != preferred]
    for name in ordered:
        if backend_installed(name):
            return name
    return None


def _autostart_daemon_args(repo: GitRepo) -> list[str] | None:
    """Every reason NOT to auto-start a tracker for ``repo``, and the arguments to start one
    with when none of them applies. ``None`` means "leave it alone".

    Shared by both auto-start triggers — the turn-end hook (``autostart_on_change``) and the
    session-start hook (``autostart_on_agent_session``) — because the answer must not depend on
    which one fired: the opt-out, an already-running tracker and any other aGiTrack holding the
    single-writer lock are properties of the repository, not of the trigger. Only the extra
    condition each trigger adds (a turn that changed something / an agent session opening) is
    left to the caller.

    The lock is PROBED, not held: an interactive TUI, a `--recover` or a `--backtrace commit` is
    the single writer while it runs, and starting a daemon beside it is exactly the double-writer
    this must never create."""
    config = GlobalConfig()
    config.load_repo_overlay(repo.repo)
    if config.autotrack_hook == "off":
        return None
    if _stop_still_stands(repo):
        return None  # the user stopped aGiTrack here and has not started it again
    if _live_background_pid(repo) is not None:
        return None  # already tracking
    from agitrack.git import RepoLock

    lock = RepoLock(repo.repo / ".agitrack" / "lock")
    if not lock.acquire():
        return None
    lock.release()  # only probing: the daemon we spawn takes it for real
    state = AgitrackState(repo.repo)
    # The recorded backend is taken AS RECORDED — not filtered through "is it installed?". This
    # hook fires when an agent session opens, so an agent is demonstrably running; a probe that
    # cannot see its binary (a CI box, a wrapper on a path the search does not know) must not be
    # what decides the repo goes untracked. Only when nothing at all is recorded does the
    # question become "what could this machine run?", which is still better than the previous
    # answer — nothing, leaving every repo whose FIRST agent session fired this hook untracked.
    backend_name = _recorded_backend(state) or config.default_backend or _installed_backend()
    if not backend_name:
        return None
    manual = read_background_mode(repo)
    if manual is None:
        manual = config.manual_commits
    return ["--manual-commits" if manual else "--auto-commit", "--backend", backend_name]


def autostart_on_agent_hook(cwd: Path | None = None) -> int:
    """The hook entry point: find the repository around ``cwd`` and hand off.

    Split from :func:`autostart_on_agent_session` so the discovery — which fails, loudly and
    normally, in every directory that is not a git repository — is the caller's whole error
    surface, and so the CLI does not have to import ``GitRepo`` into a function that already
    has one by that name."""
    try:
        repo = GitRepo.discover(Path(cwd) if cwd else Path.cwd())
    except Exception:
        return 0  # a directory aGiTrack has never heard of is not an error
    return autostart_on_agent_session(repo)


def autostart_on_agent_session(repo: GitRepo) -> int:
    """Entry point of the backends' SESSION-START hooks (``agitrack --autostart-on-agent``).

    WHY, ON TOP OF THE TURN-END HOOK. Auto-start could previously only react to work that had
    already happened: a commit (the git ``pre-commit`` hook) or a finished turn that left the
    tree changed (``autostart_on_change``). After a reboot — the case auto-start exists for —
    that means the FIRST turn of the session is written with nothing tracking it, and it is
    recovered afterwards rather than recorded as it happens. An agent opening a session in the
    repo is the earliest honest signal that work is about to start here, and every backend
    aGiTrack supports now exposes it.

    It also carries the commit-guidance note for the session it just started, and that is not a
    bonus — it is required. The note hook checks whether a tracker is running and stays silent
    when none is, so the session that STARTS the tracker would otherwise be the one session that
    never learns aGiTrack is committing for it, and would commit its own work all the way to the
    next restart. Printing it here (only when this hook actually started the tracker — otherwise
    the note hook has it covered, and two injections of the same guidance is noise) closes that
    hole.

    Best-effort and always 0, like every other hook body: a hook that fails or blocks breaks the
    user's agent, which is far worse than not starting a tracker."""
    started = False
    try:
        args = _autostart_daemon_args(repo)
        if args is not None:
            proc = spawn_background_daemon(repo, extra_args=args)
            started = wait_for_handshake(repo, pid=proc.pid, timeout=_AGENT_AUTOSTART_HANDSHAKE_SECONDS) is not None
    except Exception:
        return 0
    if started:
        try:
            from agitrack.backends.claude_settings import print_session_note

            print_session_note(cwd=repo.repo, tracker_known_running=True)
        except Exception:
            pass
    return 0


def autostart_on_change(repo: GitRepo) -> int:
    """Entry point of the Claude Code ``Stop`` hook (``agitrack --autostart-on-change``).

    WHY A HOOK AND NOT A WATCHER. Auto-start used to be reachable only through the git
    ``pre-commit`` hook, so tracking resumed when the user COMMITTED — and an agent that edits
    for an hour without committing was exactly the case tracking is for. Nothing else can
    observe a plain file edit without a process already running, which is what we do not have;
    the agent's own turn-end hook is the one moment the editor tells us. So the trigger is
    "a turn finished and left the tree changed", not "a file changed".

    Every reason not to start is checked here rather than left to the daemon, because this
    runs on EVERY turn end in the repo: the opt-out, an already-running tracker, any other
    aGiTrack holding the single-writer lock, and a turn that changed nothing. Best-effort and
    always 0 — a hook that fails or blocks would break the user's agent, which is a far worse
    outcome than not starting a tracker."""
    try:
        extra_args = _autostart_daemon_args(repo)
        if extra_args is None:
            return 0
        if not repo.has_changes():
            return 0  # a question-and-answer turn leaves nothing to track
        spawn_background_daemon(repo, extra_args=extra_args)
    except Exception:
        return 0
    return 0


def precommit_sync(repo: GitRepo, *, backend_command: list[str] | None = None) -> int:
    """Entry point of the persistent auto-track ``pre-commit`` hook. Best-effort; ALWAYS returns 0
    so it can never fail a commit.

    When aGiTrack is not already tracking this repo (its single-writer lock is free) and the AI has
    made changes since the last commit, this records the pending turns and renders the fold trailer
    so the interaction trace + token metadata land in the commit now being made — then, when
    ``background_autostart`` is set, starts the background daemon for future commits (else it prints
    a one-line reminder). A purely human commit (no pending AI turns) is left completely untouched:
    no trailer, no reminder, no daemon."""
    from agitrack.git import RepoLock

    # Remind about an available update on EVERY commit (the marker is written by the background
    # tracker / interactive proxy; installing can't be automated). Visible in the git commit output.
    try:
        from agitrack.update.marker import update_reminder_line

        reminder = update_reminder_line(repo.repo)
        if reminder:
            print(f"aGiTrack: {reminder}", file=sys.stderr)
    except Exception:
        pass
    try:
        lock = RepoLock(repo.repo / ".agitrack" / "lock")
        if not lock.acquire():
            # A live tracker holds the lock — it is the single writer, so we must NOT record turns
            # here (that would race its state/refs and risk double-counting). But the commit being
            # made needs a FRESH fold trailer: if it's the BACKGROUND daemon, nudge it to record any
            # pending completed turns and re-render the trailer synchronously NOW, so this commit's
            # prepare-commit-msg hook folds the trace/metadata in instead of a trailer lagging the
            # daemon's poll (the bug where a commit racing the poll folded nothing). An interactive
            # TUI renders its own trailer as turns complete, so it needs no nudge.
            if _live_background_pid(repo) is not None:
                request_daemon_flush(repo)
            return 0
    except Exception:
        return 0
    config = GlobalConfig()
    config.load_repo_overlay(repo.repo)
    try:
        # manual_commits=True gives fold-into-the-user's-commit semantics: record the pending AI
        # turns latently and let THIS commit's prepare-commit-msg hook fold them in (no auto-commit).
        runner = BackgroundRunner(
            repo, manual_commits=True, backend_command=backend_command, _global_config=config, _lock=lock
        )
        # NOT `runner.state.backend`: that RAISES when no backend has ever been recorded, and the
        # blanket `except` below turned the raise into a silent no-op — no fold hooks installed, no
        # trace folded, rc 0, the commit's AI work lost, while `agitrack -s` still reported
        # "Auto-start on commit: on". Ask a question that can be answered with "don't know".
        # ...and a repo with none recorded still gets tracked: the runner below follows whichever
        # backend the session actually ran on, so any agent this machine has is a fine start.
        backend_name = _recorded_backend(runner.state) or config.default_backend or _installed_backend()
        if not backend_name:
            return 0
        runner.state.ensure_local_ignore()  # git-ignore .agitrack/ before writing the trailer/ref
        # This hook path folds a trace + metadata into a commit the user is writing THEMSELVES, so
        # they are the most likely to re-edit it — protect its '#' headings from git's cleanup.
        repo.ensure_comment_char_preserves_headings()
        runner._manual.setup()  # install the fold hooks (idempotent), reset a stale ref, render
        runner._process_once()  # parse the repo's own backend session, record NEW pending turns
        runner._manual.render_trailer()  # (re)render so the trailer carries the just-recorded turns
    except Exception:
        return 0
    finally:
        lock.release()  # release BEFORE spawning the daemon, which takes its own lock
    # Auto-start is judged on its own, BEFORE the "was there AI work in this commit" question.
    # It used to sit behind `if not synced: return 0`, which made a dead tracker permanent: with
    # nothing running, no agent turn can be recorded, so `synced` is False on every commit that
    # follows, so the one hook that could restart the tracker never even looked. That is a
    # self-sustaining dead state — the tracker stops once and no number of later commits brings
    # it back, which is exactly what was observed. Whether THIS commit carries AI work and
    # whether a tracker should be running from now on are different questions.
    if (
        config.autotrack_hook != "off"
        and not _stop_still_stands(repo)
        and _live_background_pid(repo) is None
        # Spawning a daemon on a machine with no coding agent installed gets a process that
        # exits seconds later having printed "backend ... is not installed" into background.log,
        # and a line in the commit output claiming tracking had started. The turns this hook
        # just recorded are already folded into the commit either way.
        and _installed_backend(backend_name) is not None
    ):
        # Auto-start the background tracker for the turns that FOLLOW (the current commit is already
        # handled by the trailer we just rendered — it stays the author's own manual commit). Use the
        # same commit mode as the last run; the *starting* commit is manual regardless.
        manual = read_background_mode(repo)
        if manual is None:
            manual = config.manual_commits
        extra_args = ["--manual-commits" if manual else "--auto-commit"]
        # Name the backend explicitly. The spawned process resolves `--backend or the GLOBAL
        # default` and never consults this repo's own state, so without this it printed "No coding
        # agent backend is configured." into background.log and exited seconds after the commit
        # told the user tracking had started.
        extra_args += ["--backend", backend_name]
        spawn_background_daemon(repo, extra_args=extra_args)
        mode_label = "manual" if manual else "auto"
        print(
            f"aGiTrack: started automatically in {mode_label}-commit mode (same as last run) to keep tracking — "
            f"this commit stays your own. Stop it with `agitrack -b stop`; disable auto-start with "
            f"`agitrack --remove-hooks`.",
            file=sys.stderr,
        )
    return 0


class BackgroundRunner:
    POLL_SECONDS = 3.0

    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        manual_commits: bool = False,  # background defaults to AUTO commits (like the interactive TUI)
        commit_guidance: bool = True,
        backend_command: list[str] | None = None,
        log_file: str | None = None,
        poll_seconds: float | None = None,
        _global_config: GlobalConfig | None = None,
        _state: AgitrackState | None = None,
        _lock=None,
    ) -> None:
        self.repo = repo
        self.base_repo = repo  # background mode is always no-worktree
        self.verbose = verbose
        self._manual_commits = manual_commits
        self._commit_guidance = commit_guidance
        self._backend_command = list(backend_command or [])
        self.events = EventLog(resolve_log_path(log_file, repo.repo))
        # Keep the log out of the user's tree. Under -b the tracker's own `git add` swept an
        # in-repo events.log into the AGENT'S commit — aGiTrack telemetry about a turn ending
        # up inside the turn, attributed to the AI, in permanent history.
        exclude_log_file(repo.repo, self.events.path)
        self._poll_seconds = poll_seconds if poll_seconds is not None else self.POLL_SECONDS
        self._lock = _lock
        self._stop = threading.Event()
        # Periodic self-update check (never auto-applies — installing a newer aGiTrack can't be
        # fully automated). A found update is recorded to the shared marker so `-b status`, the
        # commit hook, and the dashboard can remind the user.
        self._last_update_check = 0.0
        self._update_thread: threading.Thread | None = None
        # Auto-fold waits (bounded) for the LLM summary to land so the commit's subject/lead is the
        # summary, not the raw prompt. Tracks the latent tip we're waiting on and since when.
        self._fold_wait_tip: str | None = None
        self._fold_wait_since: float | None = None
        # The latent tip we have already complained about being unfoldable, so the warning below
        # is said once per tip rather than every poll for as long as the tracker runs.
        self._fold_unusable_tip: str | None = None
        # Worktree-settle tracking for the fold (see `_worktree_settled`): the latent tip being
        # waited on and since when, plus the last tree seen and when it last changed. A
        # `_settle_changed_at` of 0.0 means "no change ever observed", which reads as settled —
        # a daemon that starts on an already-dirty tree has nothing to wait for.
        self._settle_tip: str | None = None
        self._settle_since = 0.0
        self._settle_tree: str | None = None
        self._settle_changed_at = 0.0
        # Tool-use ids of background tasks the agent started that are still running, refreshed
        # from each export. While any is live the settle wait is not capped (see below).
        self._live_background_tasks: list[str] = []
        # Summary worker threads keyed by the latent commit sha, so the fold can tell a summary
        # that's still computing from one that already FINISHED (and, if it produced no note, fold
        # now instead of waiting out the full summary_wait_seconds — e.g. the summarizer errored).
        self._summary_threads: dict[str, threading.Thread] = {}
        # Nonce of the last pre-commit flush request we serviced, so a repeated request (or a stale
        # request file across a restart) is handled at most once.
        self._last_flush_nonce: str | None = None
        # PERSISTENT tracking watermark: the HEAD up to which this daemon has accounted for AI work.
        # When a turn completes with a clean tree and HEAD has advanced past it, the new untracked
        # commits are the agent's own work (the agent/user committed it) and get COVERED with that
        # turn's trace/metadata. Persisting it (``.agitrack/tracked-head``) is what makes coverage
        # survive a daemon restart or the daemon being down at commit time — the fragile in-flight
        # HEAD snapshot it replaced was lost on every restart, leaving those commits untracked.
        self._tracked_head: str | None = None
        # Summary computed synchronously for the current cover commit's message (the daemon can't
        # amend HEAD to add it later), reused to also write the commit-summary git note.
        self._pending_cover_summary: str | None = None

        # Every backend installed on this machine, and when that was last established. The
        # tracker watches all of them (see `_installed_backends`); the answer is cached because
        # `backend_installed` can shell out to npm looking for a backend that ISN'T there, and
        # refreshed on a slow throttle so installing an agent mid-run is eventually noticed.
        self._installed: list[str] = []
        self._installed_at = 0.0
        # Whether this runner is the live daemon (``run()``), as opposed to the throwaway one the
        # pre-commit hook builds to record pending turns. Both track and both may switch backend;
        # only the daemon may act on that by arming hooks and publishing a handshake — a hook
        # process doing so would leave a handshake for a pid that is already gone, and would
        # promise "aGiTrack commits for you" in a repo where nothing is about to run.
        self._is_live_daemon = False
        self.global_config = _global_config if _global_config is not None else GlobalConfig()
        if getattr(self.global_config, "repo_path", "set") is None:
            self.global_config.load_repo_overlay(repo.repo)
        self.state = (
            _state
            if _state is not None
            else AgitrackState(repo.repo, default_backend=backend or self.global_config.default_backend)
        )
        if backend:
            if backend != self.state.backend:
                self.state.remember_backend_session()
                self.state.backend_session_id = self.state.stored_backend_session(backend)
                self.state.last_backend_message_id = None
            # Record the explicit choice even when it only CONFIRMS what the state resolves to.
            # The state was just seeded with this same value as an in-memory default, so the
            # "differs?" test above is always false on a first run and NOTHING was written:
            # `agitrack -b --backend claude` left both the state file and the global config
            # empty, and every later reader saw no backend at all — the auto-track hook then
            # silently did nothing and the daemon it spawned died on startup.
            self.state.backend = backend
            if self.global_config.default_backend != backend:
                self.global_config.default_backend = backend
        if new_session:
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            self.state.new_agitrack_session_id()
        self.backend = make_proxy_agent(self.state.backend)

        # Both modes record turns as latent commits and fold them via the prepare-commit-msg hook;
        # the tracker owns that machinery (shared with the proxy's manual mode).
        # Facts about the turn the agent is currently running, refreshed from every export (see
        # `_note_in_flight`). Lets the fold trailer attribute a commit the agent makes ITSELF
        # before that turn ends — the pre-commit flush re-exports first, so this is current.
        self._in_flight: dict | None = None
        self._manual = ManualCommitTracker(
            self.repo, self.base_repo, self.state, debug=self._debug, in_flight_fn=lambda: self._in_flight
        )

    # ------------------------------------------------------------------

    def _debug(self, message: str) -> None:
        if self.verbose:
            print(f"[agitrack:bg] {message}", flush=True)

    def _print(self, message: str) -> None:
        print(f"aGiTrack: {message}", flush=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        installed = self._installed_backends()
        if not installed:
            self._print(f"backend '{self.state.backend}' is not installed.")
            return 1
        if self.state.backend not in installed:
            # The recorded backend is gone (uninstalled, or a global default that was never
            # installed here) but another agent IS available. Refusing to start would leave the
            # repo untracked for a backend the person can actually run — and the tracker follows
            # whichever one they open anyway, so start on what exists.
            self._print(f"backend '{self.state.backend}' is not installed; tracking {installed[0]} instead.")
            self._switch_backend(installed[0], announce=False)
        # FIRST, before any of the startup work below: from this point on the process is
        # findable and stoppable by `--daemons`, by a self-update, and by the test suite's
        # sweep. Everything after it can take seconds, and a daemon nobody can see for seconds
        # is a daemon that gets left behind.
        self._register_daemon()
        # Time each startup phase (verbose only). Arming a repo is git work, and on a large one
        # it can take long enough that `agitrack -b` stops waiting for the handshake — at which
        # point the only question worth asking is WHICH phase is slow, and the log could not
        # answer it.
        mark = self._startup_timer()
        self.state.ensure_local_ignore()  # git-ignore .agitrack/ before we write any state there
        # Keep git's comment char off '#', or editing any commit we write (amend, rebase reword)
        # silently strips its '# Interaction Trace' / '# aGiTrack Metadata' headings.
        self.repo.ensure_comment_char_preserves_headings()
        write_background_mode(self.repo, manual=self._manual_commits)  # so an auto-start resumes this mode
        self._report_open_stop()
        mark("repo state")
        self._load_tracked_head()  # persistent coverage watermark (survives restarts)
        self._clear_stale_worktree_guard()
        mark("tracked head")
        self._manual.setup()
        mark("manual-commit setup")
        if getattr(self._manual, "dropped_chains", None):
            count = len(self._manual.dropped_chains)
            chains = "chain" if count == 1 else "chains"
            self._print(
                f"discarded {count} abandoned tracking {chains} from an earlier session — their "
                "changes are no longer uncommitted, so they had nothing left to attribute."
            )
        self._install_autotrack_hook()
        self._install_commit_guidance()
        mark("hooks")
        # LAST, not first. The handshake is what `agitrack -b` waits on before printing "daemon
        # live" and returning the shell, so anything written after it is a race the user can
        # lose: `agitrack -b && agitrack -s` reported "Auto-start: not armed — no aGiTrack hook
        # is installed in this repo yet. Run `agitrack` or `agitrack -b` once" ~0.6s after a
        # successful `agitrack -b`, and a script that started and stopped the tracker left
        # nothing armed at all. Publishing it only once the repo really is in the advertised
        # state costs the launcher that fraction of a second and makes every reading true.
        self._write_handshake()
        mark("handshake")
        self._is_live_daemon = True
        self._install_signal_handlers()
        mode = "manual (user-triggered) commits" if self._manual_commits else "auto commits"
        self.events.emit(
            "daemon-start",
            backend=self.state.backend,
            mode="manual" if self._manual_commits else "auto",
            repo=self.repo.repo,
        )
        watching = ", ".join(self._installed_backends()) or self.state.backend
        self._print(
            f"background tracker running for {self.state.backend} in {self.repo.repo} "
            f"({mode}, no worktree), following whichever agent you open here ({watching}). "
            "Drive the agent from any UI; stop it with `agitrack -b stop`."
        )
        # aGiTrack updated on disk: leave the loop, tear down WITHOUT force-capturing an
        # in-flight turn (the replacement resumes tracking it via the persisted watermark),
        # then replace this process with the new code. When the exec FAILS, the loop below
        # goes back to tracking on the current version — handshake restored, stop commands
        # still honored — and retries on the next detection, until the restart succeeds or
        # the user stops the tracker.
        from agitrack.update import restart as update_restart

        self._restart_cmd: list[str] | None = None
        self._explicit_stop = False

        def _restart_for_update(_version: str) -> None:
            self._restart_cmd = update_restart.restart_command()
            self._stop.set()

        while True:
            # self_update: the tracker CHECKS for a new release itself, not only reacting to
            # someone else installing one. It is the daemon most likely to be the only aGiTrack
            # running on a machine — `agitrack -b` is the default mode, it is meant to be left
            # alone for days, and it has no TUI whose startup check would ever fire. Watching the
            # on-disk fingerprint alone, it would sit on the version it started with forever
            # while a PyPI release came and went. The dashboard daemons already opt in for
            # exactly this reason; this one had been left out.
            update_restart.watch_for_update(self._stop, _restart_for_update, self_update=True)
            try:
                self._loop()
            finally:
                self._teardown(restarting=self._restart_cmd is not None and not self._explicit_stop)
            if self._restart_cmd is None or self._explicit_stop:
                return 0
            # Hand the single-writer lock over BEFORE spawning the successor, and take it back
            # if the successor never comes up. Teardown has already stopped tracking, so we are
            # not using it any more — but we used to hold it until `os._exit(0)`, and on Windows
            # a byte-range lock outlives the process that held it by the time the kernel needs to
            # tear its handles down (measured elsewhere in this codebase at ~100 ms). The
            # successor's first `acquire()` has no retry, so it lost that race, mistook its own
            # dying predecessor for a live tracker, and exited — leaving NOTHING tracking the
            # repo, with exit code 0 and no error anywhere.
            self._release_lock()
            update_restart.exec_replacement(
                self._restart_cmd,
                log=self._print,
                verify=lambda pid: (
                    _read_handshake(self.repo) is not None and (_read_handshake(self.repo) or {}).get("pid") == pid
                ),
            )
            # Only reached when the restart FAILED: resume tracking on the current code. The
            # lock has to come back first — without it this process would keep tracking a repo
            # it no longer holds, and a second writer could start alongside it.
            if not self._reacquire_lock():
                self._print(
                    "update restart failed AND this tracker could not take its own repo lock "
                    "back; another aGiTrack now holds it. Stopping rather than tracking without "
                    "the lock."
                )
                return 1
            self._print(
                "update restart failed; continuing to track on the current version and "
                "retrying. Stop with `agitrack -b stop`."
            )
            self._restart_cmd = None
            self._stop.clear()
            # Undo the teardown: the commit hooks first, then visibility (handshake +
            # --daemons registry, both via _write_handshake) — same order as startup, so the
            # handshake never advertises a state the repo has not reached.
            self._clear_stale_worktree_guard()
            self._manual.setup()
            self._install_autotrack_hook()
            self._write_handshake()

    def _startup_timer(self) -> Callable[[str], None]:
        """A ``mark(phase)`` that logs how long each startup phase took, and the total so far.

        Verbose-only: normal starts stay quiet, but when a start is slow enough to be reported
        as a failure, `agitrack -b -v` says which phase spent the seconds instead of leaving the
        log with a start line, an end line and nothing in between."""
        started = time.monotonic()
        last = started

        def mark(phase: str) -> None:
            nonlocal last
            now = time.monotonic()
            self._debug(f"startup: {phase} took {now - last:.2f}s ({now - started:.2f}s in)")
            last = now

        return mark

    def _report_open_stop(self) -> None:
        """Say so when this tracker is running with the user's stop still standing.

        Ending the stretch is NOT done here: this same entry point serves a daemon the user
        launched and a daemon a hook launched, and only the first is "the user starting aGiTrack
        again". `start_background_daemon` (the `agitrack -b` path) closes it before spawning us,
        so reaching here with it still open means something started tracking on its own. Auto-start
        refuses while a stop stands, so this should be unreachable; if it happens anyway, the
        user's decision still governs (no turn is recorded) and the log says why rather than
        leaving a tracker that silently commits nothing."""
        try:
            if not tracking_gap.gap_is_open(self.repo.repo):
                return
        except Exception as error:  # a gap record must never keep a tracker from starting
            self._debug(f"stop-record lookup failed: {error!r}")
            return
        self._print(
            "aGiTrack was stopped on this repo and has not been started again, so no conversation "
            "is being recorded. Run `agitrack -b` to start tracking."
        )

    def _clear_stale_worktree_guard(self) -> None:
        """Drop a worktree base-commit guard left in the base repo by a crashed WORKTREE run.

        Background mode is always no-worktree, so the guard is never wanted here — and both it and
        our persistent auto-track hook want the same ``pre-commit`` slot. Clearing it first (which
        restores any project hook it chained) means the auto-track install below chains the
        PROJECT hook rather than burying a dead guard under it. Mirrors the interactive proxy's
        "start from a clean hook slate", so switching modes between runs is always safe. Only ever
        touches aGiTrack's own marked hook."""
        try:
            if not self.repo.core_hooks_path():
                git_hooks.remove_base_commit_guard(self.repo.hooks_dir(), debug=self._debug)
        except Exception as error:
            self._debug(f"stale worktree guard removal failed: {error!r}")

    def _teardown(self, *, restarting: bool = False) -> None:
        # Record any final turn (and, in auto mode, fold it) before stopping. `require_complete
        # =False` and `force=True` mirror the proxy's exit finalize: a turn still running when
        # the daemon stops is captured rather than dropped, and the fold does not sit out the
        # summary/settle waits during teardown. NOT on an update restart: the replacement
        # process keeps tracking, so an in-flight turn must wait for its real completion
        # instead of being force-committed half-done at the swap.
        # ...but not into a repo that is GONE (see `_repo_is_gone`): there is nothing left to
        # commit into, and every git call would just raise its way through the handler below,
        # slowly, on the way out.
        if not restarting and not self._repo_is_gone():
            try:
                self._process_once(require_complete=False)
                if not self._manual_commits:
                    self._auto_fold_pending(force=True)
            except Exception as error:
                self._debug(f"final process failed: {error!r}")
        self._manual.teardown()
        # Kept across an update restart: the replacement daemon takes over immediately, and
        # removing then reinstalling would leave a window where a session starting mid-swap
        # gets no note at all.
        if not restarting:
            self._remove_commit_guidance()
        self._remove_handshake()
        from agitrack import daemons

        daemons.deregister()
        self.events.emit("daemon-stop", backend=self.state.backend)
        self._print(
            "background tracker restarting on the updated aGiTrack." if restarting else "background tracker stopped."
        )

    def _release_lock(self) -> None:
        """Give up the repo's single-writer lock, so a successor can take it. Best-effort:
        a restart must not be blocked by a lock object that has already gone."""
        try:
            if self._lock is not None:
                self._lock.release()
        except Exception as error:
            self._debug(f"lock release before restart failed: {error!r}")

    def _reacquire_lock(self) -> bool:
        """Take the repo lock back after a failed restart. The successor may still be dying
        with the lock held, so this retries for a few seconds rather than asking once — the
        same race, and the same answer, as `agitrack -b` replacing a tracker it just stopped."""
        if self._lock is None:
            return True
        try:
            return bool(self._lock.acquire(retry_seconds=5.0))
        except Exception as error:
            self._debug(f"lock re-acquire after failed restart failed: {error!r}")
            return False

    def _write_handshake(self) -> None:
        # Record our pid so `agitrack -b stop`/`status` can target THIS background tracker
        # (as opposed to a foreground TUI, which shares the repo lock but not this file).
        try:
            path = background_handshake_path(self.repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "manual commits" if self._manual_commits else "auto commits"
            # Rewritten mid-run whenever the tracked backend changes, so the start time has to
            # survive: `-b status` and the dashboard report it as "running since", and stamping
            # `now` there would reset this tracker's apparent uptime on every switch.
            started_at = time.time()
            existing = _read_handshake(self.repo) or {}
            if existing.get("pid") == os.getpid() and isinstance(existing.get("started_at"), (int, float)):
                started_at = float(existing["started_at"])
            path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": started_at,
                        "backend": self.state.backend,
                        "mode": mode,
                        # Stamp the running aGiTrack version so a later `agitrack -b` can tell an
                        # already-current daemon (leave it alone) from a stale one (restart to load
                        # new code). Without this, every rerun tore the daemon down and respawned it —
                        # the churn that made the tracker appear to "quit" on unrelated invocations.
                        "version": __version__,
                        # The event log THIS daemon writes to, so `-b stop` can append
                        # `daemon-stop` to it after a Windows TerminateProcess has denied the
                        # daemon its own teardown. `--log-file` is a flag and is never persisted
                        # to config, so the stopping process has no other way to learn it.
                        "log_file": str(self.events.path) if self.events.path else None,
                    }
                ),
                encoding="utf-8",
            )
        except OSError as error:
            self._debug(f"handshake write failed: {error!r}")
        self._register_daemon()

    def _register_daemon(self) -> None:
        """Record this process in the global daemon registry, so `agitrack --daemons` lists it,
        a self-update can restart it, and the test suite's session reaper can find it.

        Deliberately SEPARATE from the handshake, and called much earlier. The two answer
        different questions: the registry says "this process exists, here is how to reach and
        stop it", while the handshake says "I am up, hooks armed, tracking" — which is why the
        handshake has to come last. Registering with it meant a daemon was UNFINDABLE for the
        whole of its startup: nothing could list it, and the suite's per-test sweep (which reads
        this registry) missed any daemon that had not finished starting, so it was never reaped
        and outlived the run. Idempotent; best-effort — never blocks the tracker."""
        try:
            from agitrack import daemons

            daemons.register("background", self.repo.repo)
        except Exception as error:
            self._debug(f"daemon registry write failed: {error!r}")
        self._remember_repo()

    def _remember_repo(self) -> None:
        """Record this repository in the user-wide list the dashboard's switcher offers.

        Tracking a repo is what makes it worth listing, so it is recorded HERE rather than left
        to whoever opens a dashboard. It used to be reachable only through `ensure_hub_for`, so a
        tracker that never opened one — started by the autotrack hook, from a script, over SSH,
        or with `open_dashboard_on_start` off, all of which return early from
        `_open_dashboard_on_start` — ran for days without ever appearing in the switcher.
        Recording it at startup also outlives the daemon: the repo stays listed after tracking
        stops, which is the whole point of a list that is history rather than process state.

        Best-effort: a repo that cannot be remembered is still tracked."""
        try:
            from agitrack import repos as repo_registry

            repo_registry.remember(self.repo.repo)
        except Exception as error:
            self._debug(f"remembering repo for the dashboard switcher failed: {error!r}")

    def _remove_handshake(self) -> None:
        try:
            path = background_handshake_path(self.repo)
            info = _read_handshake(self.repo)
            # Only remove OUR handshake (guard against a race where another tracker rewrote it).
            if info and info.get("pid") == os.getpid():
                path.unlink()
        except OSError:
            pass

    def _maybe_check_update(self) -> None:
        """Periodically check for a newer aGiTrack (throttled to update_check_seconds, on a worker
        thread so the poll never blocks on the network). NEVER auto-applies — it records the result
        to the shared marker so `-b status`, the commit hook, and the dashboard remind the user."""
        if not getattr(self.global_config, "check_for_updates", True):
            return
        if self._update_thread is not None and self._update_thread.is_alive():
            return
        try:
            interval = float(self.global_config.timings.get("update_check_seconds", 300.0))
        except Exception:
            interval = 300.0
        now = time.monotonic()
        if self._last_update_check and (now - self._last_update_check) < interval:
            return
        self._last_update_check = now
        self._update_thread = threading.Thread(target=self._run_update_check, name="agit-bg-update", daemon=True)
        self._update_thread.start()

    def _run_update_check(self) -> None:
        try:
            from agitrack.update.marker import clear_update_marker, write_update_marker
            from agitrack.update.selfupdate import attempt_self_update

            # A daemon has no conversation to interrupt, so it INSTALLS rather than just
            # recording that an update exists — behind the cross-process lock, so it never
            # races the TUI or another daemon doing the same. Whatever it installs is picked
            # up by this daemon's own restart watcher (update/restart.py). The marker below
            # still records anything left for the user, which is what the dashboards read.
            status = None

            def _status(value) -> None:
                nonlocal status
                status = value

            attempt_self_update(debug=self._debug, on_status=_status)
            if status is None or not status.ok:
                return
            if status.available:
                write_update_marker(
                    self.repo.repo, current=status.current, latest=status.latest, message=status.message
                )
                self.events.emit("update-available", current=status.current, latest=status.latest)
                self._print(f"update available: {status.current} → {status.latest}. Run `agitrack` to update.")
            else:
                clear_update_marker(self.repo.repo)  # up to date now — drop a stale marker
        except Exception as error:
            self._debug(f"update check failed: {error!r}")

    def _install_autotrack_hook(self) -> None:
        """Install the PERSISTENT auto-track pre-commit hook so a commit made after this daemon
        stops (e.g. a reboot) still records its AI work. Deliberately NOT removed on teardown — it
        lives on so tracking survives aGiTrack not running (that hook re-installs the fold hooks and
        re-starts the daemon on demand). Gated by the repo-scoped ``autotrack_hook`` setting: when
        the user chose "off" (via the `agitrack -b` prompt) it is NOT installed, and any existing
        one is removed. No-op when a custom core.hooksPath makes install impossible."""
        try:
            hooks_path = self.repo.core_hooks_path()
            if hooks_path:
                # SAY IT. This was `_debug`-only — invisible even with --verbose, since those
                # diagnostics go to a log file whose path is never printed — so the string
                # "core.hooksPath" never reached the user at all, and `-s` then asserted the
                # opposite ("Auto-start: on"). Anyone on Husky / pre-commit / lefthook was told
                # their commits were tracked while nothing was.
                self._print(
                    f"this repo sets core.hooksPath ({hooks_path}), so git ignores the hooks "
                    "directory aGiTrack installs into.\n"
                    "  Tracking works normally while aGiTrack runs, but commits made after it "
                    "stops are NOT tracked.\n"
                    "  Your own hooks are left exactly as they are — aGiTrack will not override "
                    "core.hooksPath to install into it."
                )
                self._debug("autotrack hook skipped: core.hooksPath is set")
                return
            if getattr(self.global_config, "autotrack_hook", "auto") == "off":
                git_hooks.remove_autotrack_precommit_hook(self.repo.hooks_dir(), debug=self._debug)
                self._debug("autotrack hook removed (autotrack_hook=off)")
                return
            from agitrack import __version__
            from agitrack.proc import agitrack_invocation

            # Stamp the running aGiTrack version so a later start can detect a stale hook SCHEMA and,
            # if this version is newer, remove the previously installed hook and re-add the current
            # one (handled inside install_autotrack_precommit_hook).
            git_hooks.install_autotrack_precommit_hook(
                self.repo.hooks_dir(),
                invoke=agitrack_invocation(),
                repo_root=str(self.repo.repo),
                version=__version__,
                debug=self._debug,
            )
            self._install_change_autostart_hook()
        except Exception as error:
            self._debug(f"autotrack hook install failed: {error!r}")

    def _install_change_autostart_hook(self) -> None:
        """The agent-side half of auto-start: resume tracking when a TURN leaves changes, not
        only when the user commits.

        The pre-commit hook above cannot see an agent that edits for an hour without
        committing — which is precisely the work aGiTrack exists to record. Nothing can observe
        a plain file edit without a process already running (the situation auto-start is FOR),
        so the trigger is the agent's own turn-end hook. Claude Code is the only backend that
        offers one; the others keep the commit-time behaviour they have always had.

        Installed under the same ``autotrack_hook`` opt-in as the git hook, and persistent for
        the same reason — its job is to run when aGiTrack is not.

        ARMED FOR EVERY INSTALLED BACKEND, not just the one being tracked. Auto-start has to fire
        for whichever agent the person opens next, and that is not necessarily the one they
        opened last: arming only the current backend meant opening a different agent in the repo
        started nothing, and the session ran untracked until the next commit noticed."""
        from agitrack.backends import agent_hooks, claude_settings

        # The SESSION-START hook is per backend and every backend has one, so it is installed
        # first and unconditionally. The turn-end (Stop) hook below is Claude's alone.
        for name in self._installed_backends() or [self.state.backend]:
            if agent_hooks.install_agent_autostart(self.repo.repo, name, debug=self._debug):
                self._debug(f"{name} session-start autostart hook installed")
        if "claude" not in (self._installed_backends() or [self.state.backend]):
            return
        if claude_settings.install_autostart_hook(self.repo.repo, debug=self._debug):
            self._debug("claude change-autostart hook installed")

    def _install_commit_guidance(self) -> None:
        """Tell the agent that aGiTrack commits for it — the thing proxy mode does with
        ``--append-system-prompt`` and background mode cannot, because it never spawns the
        agent. Claude Code is the only backend with a channel for it that does not touch the
        user's source tree (see backends/claude_settings.py), so the others are simply left
        alone rather than given a half-measure.

        Skipped when the user opted out with ``--no-commit-guidance``: that flag means "do
        not tell the agent what to do about commits", and which mechanism carries the note is
        an implementation detail the flag should not have to know about.

        UNLIKE THE AUTOTRACK HOOK, this one IS removed on teardown. That hook exists to keep
        tracking working while aGiTrack is NOT running; this one would be a lie in the same
        situation — with no tracker watching, nothing is committing for the agent and it
        should behave normally again.

        Installed for every INSTALLED backend, because this tracker commits for whichever one is
        opened here (see :meth:`_follow_the_driven_backend`) — telling only the backend that
        happens to be tracked right now leaves the agent the person actually opens committing
        its own work, which is the duplicate-commit mess the note exists to prevent."""
        if not self._commit_guidance:
            return
        try:
            from agitrack.backends import agent_hooks

            for name in self._installed_backends() or [self.state.backend]:
                if agent_hooks.install_commit_guidance(self.repo.repo, name, debug=self._debug):
                    self._debug(f"{name} commit-guidance hook installed")
        except Exception as error:
            self._debug(f"commit-guidance hook install failed: {error!r}")

    def _remove_commit_guidance(self) -> None:
        try:
            from agitrack.backends import agent_hooks

            if agent_hooks.remove_commit_guidance(self.repo.repo, debug=self._debug):
                self._debug("commit-guidance hook removed")
        except Exception as error:
            self._debug(f"commit-guidance hook removal failed: {error!r}")

    def _install_signal_handlers(self) -> None:
        def handler(_signum, _frame):
            # An EXPLICIT stop (agitrack -b stop / owner teardown): the retry loop in
            # run() must exit even when an update restart is pending or has failed.
            self._explicit_stop = True
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not on the main thread
                pass

    def _repo_is_gone(self) -> bool:
        """Whether the repository this tracker exists to watch has been deleted.

        A tracker outliving its repo has nothing left to do and no way to be told so: `-b stop`
        and `agitrack stop` both work through files under ``<repo>/.agitrack/``, which went with
        the directory, so nothing short of `kill` could reach it. It polls a missing path every
        few seconds for as long as the machine is up, and — because a deleted repo is deleted
        for every reader — it is invisible in the dashboard's switcher too. Temp-directory repos
        make this routine: a test run or a scratch clone under ``/tmp`` is removed while its
        tracker keeps running.

        Only the DIRECTORY disappearing counts. Removing ``.git`` alone leaves a directory the
        user still has and may be about to re-init, and quitting there would throw away the
        pending turn this tracker is holding; the whole path being gone is the one unambiguous
        signal. An error while asking (a permission failure, a stalled network mount) is likewise
        not an answer — a tracker must never stop because a `stat` was slow."""
        try:
            if self.repo.repo.exists():
                return False
        except OSError:
            return False  # cannot tell (a permission error, a stalled mount): keep tracking
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._repo_is_gone():
                # Not `_explicit_stop`: this is the tracker deciding, and the distinction matters
                # for the update-restart path, which must not resurrect a tracker for a repo that
                # no longer exists.
                self._print(f"repository {self.repo.repo} no longer exists; stopping.")
                self._explicit_stop = True
                self._stop.set()
                return
            try:
                self._sample_worktree()  # the daemon's stand-in for the TUI's file watcher
                self._process_once()  # records latently, or covers commits the agent made itself
                self._manual.service()
                if not self._manual_commits:
                    self._auto_fold_pending()
                self._maybe_check_update()
            except Exception as error:  # never let one bad cycle kill the tracker
                self._debug(f"cycle error: {error!r}")
            self._wait_with_flush(self._poll_seconds)

    def _wait_with_flush(self, seconds: float) -> None:
        """Sleep up to ``seconds`` between poll cycles, but stay responsive to a pre-commit flush
        request from the auto-track hook: service it within ~0.1s so a commit isn't held waiting for
        the next full poll. Wakes immediately when stopping."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            self._service_flush_requests()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(0.1, remaining))

    def _service_flush_requests(self) -> None:
        """Handle a pre-commit flush request (see :func:`request_daemon_flush`): record any pending
        completed turns and (re)render the fold trailer synchronously, then echo the nonce back so
        the waiting hook can proceed. Runs on the daemon's own loop thread — the single writer — so
        it never races the poll. At pre-commit time the working tree still holds the about-to-be-
        committed changes, so a just-finished turn records cleanly and folds into THAT commit."""
        try:
            nonce = _flush_request_path(self.repo).read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not nonce or nonce == self._last_flush_nonce:
            return
        try:
            self._process_once()
            self._manual.render_trailer()
        except Exception as error:
            self._debug(f"flush failed: {error!r}")
        self._last_flush_nonce = nonce
        try:
            _flush_done_path(self.repo).write_text(nonce, encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Turn processing (reuses the proxy's CommitEngine so accounting is identical)
    # ------------------------------------------------------------------

    def _bare_session(self) -> Any:
        # Session sets its per-session fields dynamically from FIELDS, so it is used untyped here
        # (as CommitEngine does), letting the attribute assignments below type-check.
        session: Any = Session.bare()
        session.repo = self.repo
        session.state = self.state
        session.backend = self.backend
        session.worktree = None  # background mode never uses a worktree
        session.name = None
        return session

    def _tracked_session_id(self) -> str | None:
        """The conversation to export this cycle: the newest one a HUMAN is driving here, on
        WHICHEVER backend they are driving it on.

        The interactive proxy gets this for free — it spawns the backend itself and pins that
        session (``ProxyRunner._discover_spawned_session``), so it tracks exactly one
        conversation no matter what else runs in the directory. The daemon spawns nothing and
        has to choose, and "newest transcript in the repo dir" is the wrong rule as soon as the
        tracked agent fans work out to SDK workers: each worker writes its own transcript into
        that same directory, so every one of them gets adopted as the user's next turn — one
        commit per worker, each carrying that worker's interaction trace, each snapshotting
        whatever half-finished output files happened to exist at that moment.
        ``list_sessions`` already drops programmatic transcripts, so the newest survivor is the
        real conversation.

        Returns None when nobody is driving an agent here. The caller must then skip the cycle
        rather than let the pinned id stand in: that id may itself be a programmatic session
        adopted before this filter existed, and a still-running one would keep feeding the
        daemon "turns" the user never asked for.
        """
        self._follow_the_driven_backend()
        try:
            return self.backend.latest_session_id(self.repo.repo)
        except Exception as error:
            self._debug(f"tracked session lookup failed: {error!r}")
            return None

    # ------------------------------------------------------------------
    # Following the backend the person actually opened
    # ------------------------------------------------------------------

    def _installed_backends(self) -> list[str]:
        """Every backend installed on this machine, in registry order.

        WHY THE TRACKER WATCHES ALL OF THEM. A background tracker observes work it did not
        start: the person opens whatever agent they feel like in the repo, and the backend
        recorded for the repo is only ever a GUESS about which that will be — seeded from the
        global default the first time anything tracked here. When the guess is wrong the tracker
        polls a store that will never mention the session, and everything about it is lost: no
        per-turn commits, and the user's own commit carries no trace, no tokens, no model.
        Measured, in a repo whose recorded backend was OpenCode while the whole session ran on
        Claude Code: 90 minutes and ~148k output tokens tracked nowhere, ending in a hand-made
        commit. A person is free to choose their agent per session; the tracker follows.

        Cached, and refreshed only every few minutes: ``backend_installed`` searches PATH and
        then the installer directories, running ``npm`` to find them — cheap when the backend is
        there, not cheap when it isn't, and this is asked on every poll cycle."""
        from agitrack.backends.proxy_agents import available_backends
        from agitrack.backends.setup import backend_installed

        now = time.monotonic()
        if self._installed and now - self._installed_at < _INSTALLED_BACKENDS_TTL_SECONDS:
            return list(self._installed)
        found = [name for name in available_backends() if backend_installed(name)]
        if found:
            self._installed = found
            self._installed_at = now
        return list(found)

    def _agent_for(self, name: str) -> Any:
        """The proxy agent for ``name`` — the live one when it is the backend being tracked, so a
        caller that swapped ``self.backend`` (the tests, a future in-process reuse) keeps being
        asked, rather than a fresh replacement that would ignore it."""
        if name == self.state.backend:
            return self.backend
        return make_proxy_agent(name)

    def _backend_activity(self) -> dict[str, float]:
        """When each installed backend last had human activity in this repo, by backend name.

        Backends never used here are ABSENT rather than zero: "nothing recorded" and "recorded
        long ago" are different answers and only the second can win a comparison. A backend whose
        probe fails is treated the same way — an unreadable store must never be able to pull the
        tracker off the backend that is actually working."""
        activity: dict[str, float] = {}
        for name in self._installed_backends():
            try:
                probe = getattr(self._agent_for(name), "repo_activity", None)
                when = probe(self.repo.repo) if callable(probe) else None
            except Exception as error:
                self._debug(f"{name} activity probe failed: {error!r}")
                continue
            if when is not None:
                activity[name] = float(when)
        return activity

    def _follow_the_driven_backend(self) -> None:
        """Point this tracker at the backend the person is actually using, when that is not the
        one it is currently on.

        The comparison is between CONVERSATION timestamps, not "does this backend have a session
        here at all" — otherwise a repo where Claude was used once last month would pull the
        tracker off the OpenCode session running right now. Ties go to the backend already being
        tracked, so a quiet repo never flaps between stores.

        Never switches mid-turn: a turn in flight is one this tracker still owes a commit, and
        going to look at another store would drop its trace and tokens on the floor."""
        if self._in_flight is not None:
            return
        activity = self._backend_activity()
        if not activity:
            return  # nothing recorded anywhere — stay put and let the normal poll run
        driven, driven_at = max(activity.items(), key=lambda item: item[1])
        if driven == self.state.backend:
            return
        current_at = activity.get(self.state.backend)
        if current_at is not None and current_at >= driven_at:
            return
        self._switch_backend(driven)

    def _switch_backend(self, name: str, *, announce: bool = True) -> None:
        """Track ``name`` from now on, keeping everything the outgoing backend had.

        Its conversation is REMEMBERED rather than dropped, so switching back (the person
        returning to their other agent) resumes that conversation instead of adopting whatever
        happens to be newest there. The per-conversation watermark
        (``AgitrackState.backend_message_id_for``) is what keeps every conversation's turns
        counted exactly once across all this, which is why the global mark is deliberately left
        alone: clearing it means "recompute from scratch", and the incoming conversation would
        re-commit turns it had already recorded."""
        previous = self.state.backend
        self.state.remember_backend_session()
        self.state.backend = name
        self.state.backend_session_id = self.state.stored_backend_session(name)
        self.backend = make_proxy_agent(name)
        self.events.emit("backend-switch", backend=name, previous=previous, repo=self.repo.repo)
        if announce:
            self._print(f"the session in this repository is running on {name}; tracking that now (was {previous}).")
        if not self._is_live_daemon:
            return
        # The hooks are per backend: the incoming one needs its commit-guidance note, and its
        # session-start hook is what starts a tracker here again after this one stops.
        self._install_commit_guidance()
        self._install_change_autostart_hook()
        self._write_handshake()  # `-b status` and the dashboard read the tracked backend from here

    def _process_once(self, *, require_complete: bool = True) -> bool:
        """Export the user's active backend session and record any newly completed turns as
        latent commits. Returns True when a turn was recorded this cycle.

        ``require_complete=False`` is the STOP finalize: it keeps a turn that never got a final
        response, so work in progress when the daemon is stopped is still captured. This mirrors
        the interactive proxy's exit finalize — without it, quitting mid-turn silently discarded
        that turn's record in background mode but not in the TUI."""
        session_id = self._tracked_session_id()
        if session_id is None:
            self._debug("no human-driven session in this repo; nothing to export")
            return False
        session = self._bare_session()
        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        # Follow an in-backend session switch (the user starting or resuming a conversation)
        # while ignoring programmatic ones. The per-conversation watermark keeps each
        # conversation's turns counted exactly once.
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: session_id,
            debug_fn=self._debug,
        )
        thread = session.agent_parse_thread
        if thread is not None:
            thread.join(timeout=30)
            if thread.is_alive():
                self._debug("parse worker still running; will retry next cycle")
                return False
        committed, _ = engine.finish_parse_if_ready(
            session=session,
            quiet=True,
            prompt_untracked=True,
            require_complete=require_complete,
            awaited_followups=[],
            agent_is_active_fn=lambda: False,
            debug_fn=self._debug,
            note_session_change_fn=lambda _sid: None,
            mirror_fn=lambda _sid: None,
            commit_fn=self._record_turns,
            note_in_flight_fn=self._note_in_flight,
        )
        # The engine records the session's still-running background tasks onto the session it was
        # given. That object is thrown away each cycle, so carry the answer onto the runner — the
        # fold's settle rule needs it (see `_worktree_settled`).
        self._live_background_tasks = list(getattr(session, "live_background_task_ids", None) or [])
        return bool(committed)

    def _note_in_flight(self, facts: dict | None) -> None:
        """Remember (or clear) the running turn's facts. The pre-commit flush re-renders the
        trailer right after ``_process_once``, so this is current at commit time; re-rendering on
        a change as well keeps attribution working even if that nudge never lands (a removed
        pre-commit hook, a ``core.hooksPath`` that skips it)."""
        changed = self._in_flight != facts
        self._in_flight = facts
        if changed:
            self._manual.render_trailer()

    def _record_turns(
        self,
        *,
        turns,
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        quiet: bool,
        prompt_untracked: bool = True,
    ) -> bool:
        """Record the completed turns. When the agent still has UNCOMMITTED edits in the working tree
        this records a hidden latent commit (HEAD never moves; auto mode folds it via
        :meth:`_auto_fold_pending`). But when the agent (or user) already COMMITTED this turn's work —
        so the tree is clean and HEAD has advanced past our watermark (see
        :meth:`_agent_committed_own_work`) — recording latently would find no delta and silently drop
        the turn. Instead we COVER those commit(s): ONE cover commit carrying the trace/metadata with
        ``covered_commits`` attributing their lines to AI, reusing the exact backend-commit cover path
        the proxy uses (metadata lands in history exactly once).

        A cover (or an in-flight ``covered_commits`` attribution) is made ONLY once the turn's final
        message has been sent. The normal poll already guarantees this — :meth:`finish_parse_if_ready`
        defers while the latest turn is in progress — but the STOP finalize (``require_complete=False``)
        keeps a sole final-less turn to capture in-flight work, and covering THAT would attribute the
        agent's own commit before its turn finished. So when the last turn is still running we take no
        cover and don't advance the watermark: the agent's mid-turn commit keeps its in-flight
        attribution, and the completed turn covers it on a later cycle (after a restart if need be)."""

        def on_commit_fn(sha, trace_text, _is_cover):
            self.events.emit("ai-change-detected", backend=backend, session=backend_session_id, model=model)
            self._start_commit_summary(sha, trace_text)

        # "Final message sent" gate (see docstring): SessionTurn.complete is False only for a turn
        # whose backend response has not finished. The trailing final-less turn survives to here only
        # under the stop finalize; in every other path turns[-1] is already complete. An INTERRUPTED
        # turn is finished too (`turn_is_finished`) — nothing more is coming, so the cover it owes
        # must be taken now rather than waiting for a completion that never arrives.
        turn_complete = bool(turns) and turn_is_finished(turns[-1])
        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        covered = self._agent_committed_own_work(turns) if turn_complete else []
        # …but NEVER in manual-commit mode: there the user decides when to commit, and aGiTrack
        # adding a cover commit of its own breaks that contract outright — the user gets an
        # unrequested commit on their branch. The latent path below records the turn instead and
        # the fold hook combines it into the user's next commit, losing nothing. The interactive
        # proxy already refuses this (`_integrate_agent_made_commits_if_idle`); the daemon's own
        # cover branch was simply never gated on it.
        if covered and self._manual_commits:
            covered = []
        if covered:
            # The cover commit is created immediately and the daemon never amends HEAD, so — unlike
            # the async note flow for other commits — its message must LEAD with the summary already.
            # Summarize synchronously via summarize_fn, and write the same summary as a git note.
            self._pending_cover_summary = None

            def on_commit_cover(sha, _trace_text, _is_cover):
                self.events.emit("ai-change-detected", backend=backend, session=backend_session_id, model=model)
                if self._pending_cover_summary:
                    try:
                        self.repo.notes_add(
                            self.repo.rev_parse(sha), self._pending_cover_summary, namespace="agitrack/commit-summary"
                        )
                    except Exception as error:
                        self._debug(f"cover summary note failed: {error!r}")

            result = engine.commit_turns(
                turns=turns,
                backend=backend,
                backend_session_id=backend_session_id,
                model=model,
                stage_untracked_fn=lambda _repo, _state: None,
                on_commit_fn=on_commit_cover,
                backend_commits=covered,
                summarize_fn=self._summarize_for_message,
            )
            if result:
                self._set_tracked_head(self.repo.rev_parse("HEAD"))  # everything up to the cover is accounted for
            return result
        # Latent path (the agent's edits are still uncommitted, so the tree is dirty). If the agent
        # ALSO made a commit itself earlier this turn — one that carries only an in-flight block, or
        # none — its lines are in this turn's token count, so the recorded body lists it in
        # ``covered_commits``. In manual-record mode commit_turns never makes a cover; the SHAs only
        # feed the body's metadata. Gated on the same "final message sent" rule as the cover above,
        # so a stop-finalize of a still-running turn never attributes the agent's mid-turn commit.
        in_flight_covered = self._uncovered_agent_commits(turns) if turn_complete else []
        # A turn whose work the agent committed ITSELF leaves an unchanged tree, but is still owed
        # its trace and tokens — the tracker's gate/record would otherwise read "unchanged" as
        # "nothing happened" and drop the whole record. Same list the body names in covered_commits.
        self._manual.owed_record = bool(in_flight_covered)
        result = engine.commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=lambda _repo, _state: None,  # latent path never stages
            on_commit_fn=on_commit_fn,
            manual_gate_fn=self._manual.gate,
            manual_record_fn=self._manual.record,
            backend_commits=in_flight_covered,
        )
        if result and in_flight_covered:
            # The recorded body now attributes those agent-made commit(s); advance the watermark
            # past them (HEAD sits at the agent's own commit — the latent record never moved it) so
            # a later turn can't list the same hashes again and _agent_committed_own_work won't
            # re-cover them.
            self._set_tracked_head(self.repo.rev_parse("HEAD"))
        return result

    # ------------------------------------------------------------------
    # Persistent coverage watermark (agent/user committed the turn's own work)
    # ------------------------------------------------------------------

    def _tracked_head_path(self) -> Path:
        return self.repo.repo / ".agitrack" / "tracked-head"

    def _load_tracked_head(self) -> None:
        """Load the persisted coverage watermark, or initialize it to the current HEAD on a repo we
        have never tracked (so pre-existing history is NEVER retroactively attributed to AI). Persisted
        so a restart — or the daemon being down when a commit was made — still covers those commits."""
        try:
            saved = self._tracked_head_path().read_text(encoding="utf-8").strip()
        except OSError:
            saved = ""
        try:
            # The watermark must still describe THIS branch, not merely parse. `rev_parse` echoes
            # a well-formed sha back without checking it exists, and an object can outlive the
            # branch that contained it — any history rewrite (rebase, squash, amend, reset, a
            # force-push) orphans it. `git log <orphan>..HEAD` still succeeds, so the scan would
            # silently run over the wrong range and could re-attribute already-accounted commits.
            # Re-anchoring at HEAD can only under-claim, never claim the user's own history.
            if saved and self.repo.has_object_local(saved) and self.repo.is_ancestor(saved, "HEAD"):
                self._tracked_head = self.repo.rev_parse(saved)
                return
            if saved:
                self._debug(f"tracked-head {saved!r} no longer describes this branch; re-anchoring")
                self._print(
                    "the git history was rewritten since this repo was last tracked, so the coverage "
                    "marker was reset. Agent commits made before the rewrite won't be attributed."
                )
        except Exception:
            pass
        try:
            self._set_tracked_head(self.repo.rev_parse("HEAD"))
        except Exception:
            self._tracked_head = None

    def _set_tracked_head(self, sha: str | None) -> None:
        self._tracked_head = sha
        try:
            if sha:
                self.state.ensure_local_ignore()  # keep .agitrack/ ignored before writing into it
                self._tracked_head_path().write_text(sha + "\n", encoding="utf-8")
        except OSError as error:
            self._debug(f"tracked-head write failed: {error!r}")

    def _is_agitrack_tracked(self, sha: str) -> bool:
        """True when a commit already carries COMPLETE aGiTrack tracking (its own metadata, or an
        aGiTrack cover/merge), so it must not be covered again. Keeps the daemon's own commits and
        hook-folded user commits out of a cover's ``covered_commits``.

        A commit carrying only an IN-FLIGHT block is deliberately NOT tracked: it was stamped
        mid-turn with attribution alone, so the completed turn still owes it a trace and token
        counts and must cover it."""
        try:
            body = self.repo.commit_message(sha) or ""
        except Exception:
            return False
        return is_fully_tracked_message(body) or body.lstrip().startswith("<aGiTrack")

    def _turn_window_start(self, turns) -> int | None:
        """Epoch seconds of the earliest prompt among *turns*, or None when the backend
        transcript carries no timestamps (in which case no time gate can be applied)."""
        stamps = [turn.started_at for turn in turns or [] if getattr(turn, "started_at", None)]
        return min(stamps) if stamps else None

    # Slack on the turn-window gate below. Both clocks are this machine's — the backend transcript's
    # prompt timestamp and git's committer date — so a couple of seconds covers rounding; the slack
    # only ever widens what may be claimed, so it is deliberately kept tight.
    _WINDOW_SLACK_SECONDS = 5

    def _since_turn_start(self, commits: list[str], turns) -> list[str]:
        """*commits* (oldest first) with the leading ones that predate the turn dropped.

        The watermark is persistent and deliberately survives the daemon being down, so commits the
        daemon never watched can sit between it and HEAD — including purely human ones a user made
        in another mode (interactive, or plain ``git commit`` with no aGiTrack running at all). A
        commit created BEFORE the user's prompt arrived cannot be that prompt's work, so it is never
        claimed. Only a leading prefix is dropped: what remains stays a contiguous range ending at
        HEAD, which is what a cover commit's parents describe.

        Commits made after the prompt are still claimed even if the daemon was down when they
        landed — that's the coverage the persistent watermark exists to provide."""
        start = self._turn_window_start(turns)
        if start is None:
            return commits
        cutoff = start - self._WINDOW_SLACK_SECONDS
        for index, sha in enumerate(commits):
            stamp = self.repo.commit_timestamp(sha)
            if stamp is None or stamp >= cutoff:
                return commits[index:]
        return []

    def _agent_committed_own_work(self, turns) -> list[str]:
        """Full SHAs (oldest first) of the commit(s) to cover for a just-completed turn — the
        UNTRACKED commits on ``tracked_head..HEAD`` when the working tree is clean — or ``[]``.

        The persistent watermark (not a per-turn in-flight snapshot) is what makes this survive a
        daemon restart or the daemon being down at commit time: whenever a turn finishes and HEAD has
        advanced past the watermark with the tree clean, the agent/user committed the turn's work
        themselves, so those commits are covered and the watermark advances. Commits that already
        carry aGiTrack tracking are skipped (and the watermark still advances past them), so the
        daemon's own commits and hook-folded user commits are never re-covered. Commits that predate the
        turn's own prompt are dropped by :meth:`_since_turn_start`, so history the daemon never
        watched — a user's own commits made in another mode, or before it ever ran — is not
        retroactively claimed. The one remaining trade-off, deliberately chosen for reliable coverage
        over the old "miss it entirely" behavior: a purely human commit made DURING an AI turn is
        attributed to that turn."""
        if self._tracked_head is None:
            return []
        try:
            head = self.repo.rev_parse("HEAD")
            if head == self._tracked_head:
                return []  # no new commit since we last accounted (e.g. a pure-Q&A turn)
            # `comparable_tree`, not a raw `^{tree}`: the snapshot drops the agent scaffolding
            # dirs, so in a repo that TRACKS `.claude/` this test read "dirty" forever and the
            # daemon covered NO agent self-commit at all. See GitRepo.comparable_tree.
            if self.repo.snapshot_worktree_tree() != self.repo.comparable_tree("HEAD"):
                return []  # dirty tree ⇒ the agent's edits are uncommitted; record latently instead
            commits = self.repo.log_shas(self._tracked_head, head)  # tracked_head..HEAD, oldest first
        except Exception as error:
            self._debug(f"committed-away detection failed: {error!r}")
            return []
        # Commits that arrived from another branch (a merge/pull/PR) are not the agent's,
        # however they came to sit between the watermark and HEAD.
        untracked = [
            sha
            for sha in self._since_turn_start(commits, turns)
            if not self._is_agitrack_tracked(sha) and not self.repo.arrived_from_elsewhere(sha)
        ]
        if not untracked or untracked[-1] != head:
            # Nothing new to cover, or a tracked commit sits at HEAD (can't cover onto it) — just
            # advance the watermark so we don't reconsider these commits every cycle.
            self._set_tracked_head(head)
            return []
        return untracked

    def _uncovered_agent_commits(self, turns) -> list[str]:
        """Not-yet-tracked commit(s) on ``tracked_head..HEAD`` (oldest first): the agent's own
        commits — typically one it ran mid-turn (``git commit``) before this now-complete turn
        finished — whose lines this turn's token count accounts for, so the folded turn body
        lists them in ``covered_commits``.

        Unlike :meth:`_agent_committed_own_work` this does NOT require a clean tree: it runs on
        the latent (dirty-tree) path, where the agent committed once mid-turn and then kept
        editing, so its edits are still uncommitted while a prior self-made commit already sits in
        history. Returns ``[]`` when nothing untracked lies between the watermark and HEAD.

        Same turn-window gate as the cover path: a commit that predates this turn's prompt (a user's
        own commit made while the daemon was down or running in another mode) is never claimed."""
        if self._tracked_head is None:
            return []
        try:
            head = self.repo.rev_parse("HEAD")
            if head == self._tracked_head:
                return []
            commits = self.repo.log_shas(self._tracked_head, head)  # tracked_head..HEAD, oldest first
        except Exception as error:
            self._debug(f"uncovered agent commit check failed: {error!r}")
            return []
        return [
            sha
            for sha in self._since_turn_start(commits, turns)
            if not self._is_agitrack_tracked(sha) and not self.repo.arrived_from_elsewhere(sha)
        ]

    # ------------------------------------------------------------------
    # Auto mode: fold the pending latent turns into a real commit ourselves
    # ------------------------------------------------------------------

    def _auto_fold_pending(self, *, force: bool = False) -> None:
        """Auto mode: aGiTrack commits the pending latent turns itself — folding their full
        trace/metadata — so the user doesn't have to. If the working tree is already clean the
        agent (or user) committed its own work, in which case the prepare-commit-msg fold hook
        folded the tracking into THAT commit (cover being only the fallback), and there is nothing
        for us to do. The tree is snapshotted with the same scaffolding filter the latent path
        uses, so ``.agitrack`` churn never counts as work.

        ``force`` skips the settle and summary waits — set on the STOP finalize, where the commit
        must land NOW because no further poll will run. Same role as the proxy's
        ``_auto_fold_latent_pending(force=True)`` on exit."""
        ref = self._manual.ref()
        tip = self.repo.ref_sha(ref)
        if not tip:
            return
        try:
            # Clean working tree vs HEAD ⇒ the agent (or user) already committed its work, and the
            # prepare-commit-msg fold hook folded the tracking into THAT commit — nothing to do.
            if self.repo.snapshot_worktree_tree() == self.repo.comparable_tree("HEAD"):
                return
        except Exception:
            return
        if self._manual.pending_count() == 0:
            return
        if not force and not self._worktree_settled(tip):
            return  # something is still writing — retry next cycle
        # Let the LLM summary land first so the commit is summarized (subject + lead paragraph),
        # not left with the raw prompt. Bounded — after summary_wait_seconds we fold anyway. Unlike
        # the interactive proxy the daemon never amends HEAD (the user may be committing), so the
        # summary must be in BEFORE the commit, not amended in after.
        if not force and not self._fold_summary_ready(tip):
            return  # retry next cycle
        bodies = self._manual.pending_bodies()  # re-read: any arrived summaries are now applied
        message = build_auto_fold_message(bodies, repo_root=self.repo.repo)
        if not message:
            # Pending turns exist (checked above) yet none of them reads as a recorded turn, so
            # there is nothing to fold and the branch simply stops advancing. That is how a
            # corrupted metadata header once turned into "aGiTrack quietly stopped committing":
            # every poll took this return, and no line anywhere said why. Say it — once per tip,
            # so a stuck chain is a message rather than a silence.
            if self._fold_unusable_tip != tip:
                self._fold_unusable_tip = tip
                self._print(
                    f"{self._manual.pending_count()} recorded turn(s) could not be folded into a commit "
                    "(their metadata is unreadable); the branch is not advancing. "
                    "Please report this at https://github.com/core-aix/agitrack/issues."
                )
            return
        self._fold_unusable_tip = None
        try:
            self.repo.add_tracked()
            declined = set(self.state.declined_untracked())
            self.repo.stage_paths([p for p in self.repo.untracked_entries() if p not in declined])
            if not self.repo.has_staged_changes():
                return
            # The message already carries the folded metadata, so the prepare-commit-msg hook's
            # idempotency check skips re-appending it; the post-commit hook resets the latent ref.
            self.repo.commit(message)
            self._manual.reset_stale_ref()
            self._manual.last_head = self.repo.rev_parse("HEAD")
            self._set_tracked_head(self.repo.rev_parse("HEAD"))  # our own fold commit is accounted for
            self._manual.render_trailer()
            self._fold_wait_tip = None
            self._fold_wait_since = None
            self.events.emit("commit", sha=self.repo.rev_parse("HEAD")[:12], type="agent", backend=self.state.backend)
            self._print("committed agent turn(s).")
        except Exception as error:
            self._debug(f"auto fold failed: {error!r}")

    def _summary_wait_seconds(self) -> float:
        try:
            return float(self.global_config.timings.get("summary_wait_seconds", 45.0))
        except Exception:
            return 45.0

    def _sample_worktree(self) -> None:
        """Note whether the working tree changed since the last poll, so ``_worktree_settled``
        knows when it last moved.

        Sampled every cycle rather than only at fold time, because that is what makes the timing
        match the TUI: there a file watcher stamps ``_last_change_at`` on each write, so by the
        time a turn ends the tree has usually been quiet for a while already and the extra wait
        before committing is near zero. Measuring only from the first fold attempt would instead
        add a full quiet period to every commit. The snapshot is the same one the fold and the
        cover check already take each cycle, so this costs nothing extra in practice."""
        try:
            tree = self.repo.snapshot_worktree_tree()
        except Exception as error:
            self._debug(f"settle sample failed: {error!r}")
            return
        if self._settle_tree is not None and tree != self._settle_tree:
            self._settle_changed_at = time.monotonic()
        self._settle_tree = tree

    def _worktree_settled(self, tip: str) -> bool:
        """True when the working tree has stopped changing, so the fold captures a coherent
        snapshot instead of a half-written one.

        This is the ``file_stable_seconds`` quiet period the interactive TUI applies before an
        auto-commit (``ProxyRunner._maybe_agent_commit``). The daemon used to ignore that setting
        entirely — a user who tuned it saw it honored in the TUI and silently dropped here.

        The wait is capped so a repo with an unexplained continuous writer cannot stop committing
        forever while looking perfectly healthy — the TUI's gate has no such bound. But the cap is
        SUSPENDED while a background task the agent started is still running, because that is the
        one case where a never-settling tree is expected rather than pathological, and it is
        exactly the case the TUI's unbounded wait handles well: a task that streams progress for
        an hour otherwise produces a commit every few minutes whose entire diff is the task's own
        log/output churn and whose turn is the agent saying "still running, nothing to report".
        Waiting collapses that whole run into ONE commit carrying every turn's trace."""
        now = time.monotonic()
        if tip != self._settle_tip:  # a new turn to fold — restart the cap clock
            self._settle_tip = tip
            self._settle_since = now
        try:
            quiet = float(self.global_config.timings.get("file_stable_seconds", 8.0))
        except Exception:
            quiet = 8.0
        if now - self._settle_changed_at >= quiet:
            return True
        if self._live_background_tasks:
            return False  # its output is what keeps the tree moving — wait for it, like the TUI
        return now - self._settle_since >= self._summary_wait_seconds()

    def _fold_summary_ready(self, tip: str) -> bool:
        """True when the auto-fold may proceed: summarization off, the tip's summary note has
        landed, the summary worker already finished WITHOUT one (it errored — don't wait it out), or
        the bounded wait (``summary_wait_seconds``) elapsed. Returns False only while a summary is
        genuinely still computing and within the wait window (so the caller retries next cycle)."""
        if not self._summarization_enabled():
            return True
        try:
            note = self.repo.notes_show(tip, namespace="agitrack/commit-summary")
            if note and note.strip():
                return True
        except Exception:
            return True  # can't read notes ⇒ don't block folding
        worker = self._summary_threads.get(tip)
        if worker is not None and not worker.is_alive():
            # The summary worker for this turn finished but left no note (the summarizer errored or
            # returned nothing). Fold now rather than waiting out the full deadline for a summary
            # that will never arrive — the commit just keeps its raw-prompt subject.
            return True
        now = time.monotonic()
        if self._fold_wait_tip != tip:  # a new tip to wait on — restart the clock
            self._fold_wait_tip = tip
            self._fold_wait_since = now
            return False
        if self._fold_wait_since is None:
            self._fold_wait_since = now
            return False
        return (now - self._fold_wait_since) >= self._summary_wait_seconds()

    # ------------------------------------------------------------------
    # Summaries (best-effort, written as git notes so the fold picks them up)
    # ------------------------------------------------------------------

    def _summarization_enabled(self) -> bool:
        # The GLOBAL config (with repo overlay) is the durable source of truth and wins — matching
        # the proxy (ProxyRunner._summarization_enabled). The per-repo AgitrackState always defaults
        # "on", so it must NOT shadow a global/repo `summarization_enabled: false` or the toggle
        # would never take effect in background mode. Fall back to state only with no global config.
        gc_enabled = getattr(self.global_config, "summarization_enabled", None)
        if gc_enabled is not None:
            return bool(gc_enabled)
        return bool(getattr(self.state, "summarization_enabled", True))

    def _make_summarizer(self):
        if not self._summarization_enabled():
            return None
        from agitrack.backends import backend_class as _backend_class, backend_name as _backend_name
        from agitrack.summaries import Summarizer, summary_scratch_dir
        from agitrack.summaries.model_select import compatible_summarization_model

        backend_name = _backend_name(self.state.backend)
        backend_class = _backend_class(backend_name)
        model = self.state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        # A summarization_model configured for a different backend (e.g. a Claude id under an
        # OpenCode session) is invalid there and fails every summary — drop it for the default.
        model = compatible_summarization_model(backend_name, model)
        if model is None and backend_name != "claude":
            # Same reasoning (and the same fix) as ProxyRunner._make_summarizer: the summarizer
            # runs from a scratch dir outside the repo, so a project-scoped model pin
            # (`opencode.json`, `.codex/config.toml`) cannot apply and the backend falls back to
            # its global default — which failed every summary here too, leaving the daemon's
            # commits with raw-prompt subjects while the interactive path had already been
            # fixed. Two copies, one bug.
            model = compatible_summarization_model(backend_name, self.state.model)
        launch = self._backend_command or None
        return Summarizer(backend_class(summary_scratch_dir(), launch_command=launch), model=model)

    def _summarize_for_message(self, trace_text: str) -> tuple[str, list[str]] | None:
        """Summarize a cover's interaction trace SYNCHRONOUSLY, returning ``(summary, metadata)`` so
        the cover's commit message can LEAD with the summary. The daemon never amends HEAD, so unlike
        the proxy it can't attach the summary after committing — it must be in the message up front
        (the auto-fold path likewise waits for the summary before committing). Best-effort: returns
        None when summaries are off or the summarizer fails, leaving the raw prompt-led message. The
        summary is cached so :meth:`_record_turns` can also write it as the commit-summary note."""
        summarizer = self._make_summarizer()
        if summarizer is None:
            return None
        try:
            summary = summarizer.summarize_commit(trace=trace_text)
        except Exception as error:
            self._debug(f"cover summary failed: {error!r}")
            return None
        if not summary or not summary.strip():
            return None
        self._pending_cover_summary = summary
        meta = summary_metadata_lines(
            model=summarizer.model,
            tokens_input=summarizer.tokens_input,
            tokens_output=summarizer.tokens_output,
            tokens_cache_read=summarizer.tokens_cache_read,
        )
        return summary, meta

    def _start_commit_summary(self, sha: str, trace_text: str) -> None:
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        try:
            full_sha = self.repo.rev_parse(sha)
        except Exception:
            return

        def worker() -> None:
            try:
                summary = summarizer.summarize_commit(trace=trace_text)
            except Exception as error:
                self._debug(f"summary failed for {sha}: {error!r}")
                return
            if not summary or not summary.strip():
                return
            try:
                # Record as a git note; the fold (pending_bodies) and the dashboard both read it.
                # Never amend HEAD here — the tracker runs while the user may be committing, so
                # touching HEAD could race their commit.
                self.repo.notes_add(full_sha, summary, namespace="agitrack/commit-summary")
                self._manual.render_trailer()
            except Exception as error:
                self._debug(f"summary note failed for {sha}: {error!r}")

        thread = threading.Thread(target=worker, name="agit-bg-summary", daemon=True)
        self._summary_threads[full_sha] = thread
        thread.start()
