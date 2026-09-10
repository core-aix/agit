"""Tests for the global daemon registry (`agitrack --daemons` and restart-on-update)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from agitrack import daemons
from agitrack.daemons import _scan_daemon_processes as _real_scan  # bound pre-guard (conftest stubs the attr)


def test_register_list_deregister(monkeypatch, tmp_path):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "daemons")
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    daemons.register("dashboard", "/home/me/myrepo", url="http://127.0.0.1:8765/")
    infos = [i for i in daemons.list_running() if i.pid == os.getpid()]
    assert len(infos) == 1
    assert infos[0].function == "repo dashboard"
    assert infos[0].repo_name == "myrepo"
    assert infos[0].url == "http://127.0.0.1:8765/"

    daemons.deregister()
    assert not any(i.pid == os.getpid() for i in daemons.list_running())


def test_running_repos_reports_tracked_repos_without_a_process_scan(monkeypatch, tmp_path):
    # The dashboard's switcher unions this in while drawing a dropdown the page header polls, so
    # it must NOT cost a `ps` subprocess. Every daemon registers itself, so the registry alone is
    # the complete answer in practice; the scan stays available for the rare caller that needs it.
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "daemons")

    def fail_scan():
        raise AssertionError("running_repos must not scan the process table by default")

    monkeypatch.setattr(daemons, "_scan_daemon_processes", fail_scan)
    (tmp_path / "myrepo").mkdir()
    daemons.register("background", tmp_path / "myrepo")

    assert daemons.running_repos() == [str((tmp_path / "myrepo").resolve())]


def test_running_repos_excludes_the_hub_which_belongs_to_no_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "daemons")
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])
    daemons.register("hub", "", url="http://127.0.0.1:8765/")

    assert daemons.running_repos() == []


def test_list_running_prunes_dead_entries(monkeypatch, tmp_path):
    directory = tmp_path / "daemons"
    monkeypatch.setattr(daemons, "_registry_dir", lambda: directory)
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    directory.mkdir()
    dead_pid = 2**31 - 1  # a pid that cannot be running
    entry = directory / f"{dead_pid}.json"
    entry.write_text(json.dumps({"pid": dead_pid, "kind": "background", "repo": "/r"}), encoding="utf-8")

    assert daemons.list_running() == []  # the dead entry is filtered out...
    assert not entry.exists()  # ...and pruned from disk


def test_restart_all_terminates_and_respawns(monkeypatch, tmp_path):
    directory = tmp_path / "daemons"
    monkeypatch.setattr(daemons, "_registry_dir", lambda: directory)
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    directory.mkdir()
    marker = tmp_path / "respawned.txt"

    # A real, alive process stands in for a running daemon; its recorded command touches a marker.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (directory / f"{sleeper.pid}.json").write_text(
        json.dumps(
            {
                "pid": sleeper.pid,
                "kind": "dashboard",
                "repo": "/r/one",
                "cmd": [sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"],
            }
        ),
        encoding="utf-8",
    )

    # Terminate = actually kill + reap the sleeper, so the wait loop sees it exit promptly.
    def fake_terminate(pid):
        if pid == sleeper.pid:
            sleeper.kill()
            sleeper.wait()

    monkeypatch.setattr(daemons, "terminate_pid", fake_terminate)
    # restart_all now requires proof each target really is an aGiTrack daemon before signalling
    # it (a registry pid can be a REUSED pid after a reboot), so the stand-in must look like one.
    monkeypatch.setattr(
        daemons, "_process_command_lines", lambda: [f"{sleeper.pid} python -m agitrack --dashboard-serve"]
    )

    restarted = daemons.restart_all(exclude_pid=os.getpid())
    assert restarted == 1
    assert not (directory / f"{sleeper.pid}.json").exists()  # old entry removed

    for _ in range(100):  # the re-spawn is detached; wait for its marker
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()


def test_restart_all_skips_current_process(monkeypatch, tmp_path):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "daemons")
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    daemons.register("background", "/r/self")  # this process registers itself
    # Excluding self (the default) must not terminate this very test process.
    assert daemons.restart_all() == 0
    daemons.deregister()


def test_scan_daemon_processes_parses_ps(monkeypatch):
    monkeypatch.setattr(daemons, "_custom_config_dir", lambda: "")  # exercise the scan itself
    canned = (
        "  501 /usr/bin/python3 -m agitrack --repo /home/me/proj --dashboard-serve --dashboard-owner-pid 42\n"
        "  777 /usr/bin/python3 -m agitrack --repo /home/me/other --backtrace-serve --dashboard-owner-pid 9\n"
        "  888 /usr/bin/python3 -m agitrack --repo /home/me/bg --background --background-serve\n"
        "  999 -zsh\n"
        " 1000 /usr/bin/python3 -m agitrack --dashboard\n"  # interactive dashboard launcher, NOT a daemon
    )

    class _R:
        stdout = canned

    monkeypatch.setattr(daemons.subprocess, "run", lambda *a, **k: _R())
    by_kind = {i.kind: i for i in _real_scan()}
    assert set(by_kind) == {"dashboard", "backtrace", "background"}  # the no-serve-flag lines are ignored
    assert by_kind["dashboard"].pid == 501 and by_kind["dashboard"].repo == "/home/me/proj"
    assert by_kind["backtrace"].repo == "/home/me/other"
    assert by_kind["background"].repo == "/home/me/bg"
    assert "--dashboard-serve" in by_kind["dashboard"].cmd  # its argv can re-launch it


def test_list_running_finds_unregistered_daemon_via_ps(monkeypatch, tmp_path):
    """A daemon with NO registry entry (e.g. started before the registry existed) is still found."""
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "empty")  # no registry entries

    class _R:
        stdout = "  501 /usr/bin/python3 -m agitrack --repo /r/one --dashboard-serve --dashboard-owner-pid 1\n"

    # This test exercises the REAL scan (parsing a mocked `_process_command_lines`), so restore it
    # over the conftest guard that stubs it out for every other test.
    monkeypatch.setattr(daemons, "_scan_daemon_processes", _real_scan)
    monkeypatch.setattr(daemons, "_custom_config_dir", lambda: "")  # exercise the scan itself
    monkeypatch.setattr(daemons, "_process_command_lines", lambda: _R().stdout.splitlines())
    infos = daemons.list_running()
    assert any(i.pid == 501 and i.kind == "dashboard" and i.repo == "/r/one" for i in infos)


def test_ps_scan_failure_is_graceful(monkeypatch):
    def boom(*a, **k):
        raise OSError("ps not available")

    monkeypatch.setattr(daemons.subprocess, "run", boom)
    assert daemons._scan_daemon_processes() == []  # never raises where ps is missing (e.g. Windows)


def test_daemons_cli_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    from agitrack.cli import main

    assert main(["--daemons"]) == 0
    assert "No aGiTrack daemons are currently running." in capsys.readouterr().out


def test_registry_dir_honors_config_dir_isolation(monkeypatch, tmp_path):
    # The registry must follow AGITRACK_CONFIG_DIR like all other global state: the test
    # suite isolates that env var, and a registry that ignored it let update-restart
    # tests run restart_all() against the DEVELOPER'S real registry — killing their
    # live daemons on every full test run.
    from agitrack import daemons

    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    assert daemons._registry_dir() == tmp_path / "daemons"


# --- every daemon updates itself, and every daemon comes back ----------------------------------


def test_every_long_lived_daemon_checks_for_updates_itself():
    """Watching the on-disk fingerprint only reacts to SOMEONE ELSE installing a new version.

    On a machine whose only aGiTrack is a daemon — which is the normal case for `agitrack -b`,
    the default mode, left running for days with no TUI whose startup check would ever fire —
    nobody else ever installs anything, so a watcher alone sits on its starting version while
    release after release goes by on PyPI. The background tracker had been left out of this.

    A source-level check on purpose: the next daemon someone adds must opt in too, and a runtime
    test would only cover the daemons that already exist."""
    import re
    from pathlib import Path

    calls = []
    for path in Path("agitrack").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"watch_for_update\(([^)]*)\)", source, re.S):
            if source[: match.start()].rstrip().endswith("def"):
                continue  # the definition itself, not a call
            calls.append((path.as_posix(), match.group(1)))

    assert calls, "no daemon watches for updates at all"
    missing = [path for path, args in calls if "self_update=True" not in args]
    assert not missing, f"these daemons never check for an update themselves: {missing}"


def test_every_view_daemon_restarts_only_after_the_background_trackers():
    """Scanned from the source for the same reason as the check above: a NEW view daemon added
    later would otherwise silently race the trackers again.

    The ordering is one-directional. The background tracker goes FIRST and must not wait for
    anything (waiting on itself would deadlock the whole update), so it is the one caller that
    must NOT pass the gate; every daemon that only shows the user what the trackers recorded
    passes it.
    """
    import re
    from pathlib import Path

    views: list[str] = []
    trackers: list[str] = []
    for path in Path("agitrack").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"watch_for_update\(([^)]*)\)", source, re.S):
            if source[: match.start()].rstrip().endswith("def"):
                continue  # the definition itself, not a call
            posix = path.as_posix()
            (views if posix.startswith("agitrack/metrics/") else trackers).append(
                posix if "defer_while" in match.group(1) else f"MISSING:{posix}"
            )

    assert views, "no view daemon watches for updates at all"
    assert not [v for v in views if v.startswith("MISSING:")], (
        f"these view daemons would restart before the trackers: {views}"
    )
    assert not [t for t in trackers if not t.startswith("MISSING:")], (
        f"the background tracker must not wait for itself: {trackers}"
    )


def test_the_dashboard_hub_is_recognised_as_a_daemon_to_restart():
    """`restart_all` demands proof a pid really is an aGiTrack daemon before signalling it (a
    registry pid can be a reused pid after a reboot). A daemon whose serve flag is not listed
    would be silently skipped by every update restart."""
    flags = {flag: kind for flag, kind in daemons._SERVE_FLAGS}

    assert flags.get("--hub-serve") == "hub"
    # ...and it is in the set the update restart acts on at all.
    assert "hub" in daemons._STOPPABLE_KINDS


def test_a_live_session_is_never_restarted_from_under_the_user():
    # The one kind deliberately left out: restarting someone's conversation is not an update.
    assert "session" in daemons.KIND_LABELS
    assert "session" not in daemons._STOPPABLE_KINDS


def test_every_kind_aGiTrack_can_leave_running_is_either_restarted_or_deliberately_not():
    # A new daemon kind that is neither restarted nor consciously exempt would quietly keep
    # running old code after every update.
    deliberate_exemptions = {"session"}

    assert set(daemons.KIND_LABELS) - daemons._STOPPABLE_KINDS == deliberate_exemptions


# --- scoping a daemon to a repository, with another repository inside it -----
#
# `agitrack stop` and `--daemons stop <repo>` narrow the registry to the daemons serving one
# repository. "Serving" has to include a daemon recorded against one of aGiTrack's session
# worktrees (they live at `<repo>/.agitrack/worktrees/<name>`) and must NOT include a daemon
# for a repository that merely sits inside this one — a vendored checkout, a submodule, or the
# folder of projects someone has since made a repository of its own. Stopping the outer one
# was ending live sessions in the inner one.


def _info(repo: str) -> daemons.DaemonInfo:
    return daemons.DaemonInfo(pid=1, kind="session", repo=repo)


def test_serves_repo_matches_the_repo_itself(tmp_path):
    assert daemons._serves_repo(_info(str(tmp_path)), tmp_path) is True


def test_serves_repo_matches_one_of_this_repos_session_worktrees(tmp_path):
    worktree = tmp_path / ".agitrack" / "worktrees" / "coral"
    worktree.mkdir(parents=True)
    assert daemons._serves_repo(_info(str(worktree)), tmp_path) is True


def test_serves_repo_ignores_a_repository_nested_inside_this_one(tmp_path):
    nested = tmp_path / "vendor" / "other-project"
    nested.mkdir(parents=True)
    assert daemons._serves_repo(_info(str(nested)), tmp_path) is False


def test_serves_repo_ignores_the_enclosing_folder(tmp_path):
    child = tmp_path / "project"
    child.mkdir()
    # The daemon is the inner repo's; asking about it from the outer folder is not a match, and
    # neither is the reverse.
    assert daemons._serves_repo(_info(str(tmp_path)), child) is False
