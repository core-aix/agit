import subprocess
import types
import sys

import pytest

from agitrack import cli
from agitrack.git import GitError, GitRepo


def _has_git() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


def test_git_init_seeds_usable_repo(tmp_path):
    (tmp_path / "file.txt").write_text("hello\n", encoding="utf-8")
    repo = GitRepo.init(tmp_path)

    # Valid HEAD (the seed commit) so worktree setup won't choke on an unborn branch.
    assert repo.current_branch() not in ("", "HEAD")
    # The user's pre-existing file is left untracked for aGiTrack's user-commit flow.
    assert "file.txt" in repo.status_short()


def test_git_init_repo_has_born_head(tmp_path):
    repo = GitRepo.init(tmp_path)
    assert repo.has_commits()


def test_ensure_born_seeds_unborn_repo_and_is_idempotent(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    repo = GitRepo.discover(tmp_path)
    assert not repo.has_commits()  # fresh `git init`: unborn HEAD

    assert repo.ensure_born() is True  # seeds an initial commit
    assert repo.has_commits()
    assert repo.current_branch() not in ("", "HEAD")  # worktree-usable HEAD

    assert repo.ensure_born() is False  # already born: no-op


def test_discover_or_init_seeds_empty_initialized_repo(tmp_path, capsys):
    # A user who ran `git init` themselves (unborn HEAD) must start cleanly,
    # leaving their own files untracked for aGiTrack's user-commit flow.
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "existing.txt").write_text("mine\n", encoding="utf-8")

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None
    assert repo.has_commits()
    assert repo.current_branch() not in ("", "HEAD")
    assert "existing.txt" in repo.untracked_files()
    assert "Seeded an initial commit" in capsys.readouterr().out


def test_discover_or_init_returns_existing_repo(tmp_path, monkeypatch):
    GitRepo.init(tmp_path)
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "n")
    repo = cli._discover_or_init(tmp_path)
    assert repo is not None
    assert asked == []  # an existing repo is never prompted about


def _force_tty(monkeypatch, stdin: bool, stdout: bool = True):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: stdin, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout, raising=False)


# --- startup gh-availability check ------------------------------------------------


def _stub_gh(monkeypatch, *, status: str, has_remote: bool = True):
    """Stub the gh status + GitHub-remote probes the startup check reads."""
    import agitrack.metrics.github as ghmod

    monkeypatch.setattr(ghmod, "gh_status", lambda: status)
    monkeypatch.setattr(ghmod, "commit_url_base", lambda repo: "https://x/commit/" if has_remote else "")
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: None)


_FAKE_REPO = object()


def test_gh_check_silent_when_authenticated(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="ok")
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, False)


def test_gh_check_silent_without_a_github_remote(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated", has_remote=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, False)


def test_gh_check_non_interactive_does_not_prompt(monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, False)


def test_gh_check_unauthenticated_defaults_to_login(monkeypatch):
    # Signing in is the recommended action, so a bare Enter runs `gh auth login`.
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    ran = []
    monkeypatch.setattr(cli, "_run_gh_login", lambda: ran.append(True))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)
    assert ran == [True]


def test_gh_check_unauthenticated_skip_continues_without_login(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "s")
    monkeypatch.setattr(cli, "_run_gh_login", lambda: (_ for _ in ()).throw(AssertionError("skipped")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)


def test_gh_check_quit_aborts_startup(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    assert cli._check_gh_availability(_FAKE_REPO) == (False, True)


def test_gh_check_login_runs_gh_auth_login(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "l")
    ran = []
    monkeypatch.setattr(cli, "_run_gh_login", lambda: ran.append(True))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)
    assert ran == [True]


def test_gh_check_missing_does_not_offer_login(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="missing")
    monkeypatch.setattr("builtins.input", lambda *a: "l")  # 'l' is meaningless when gh isn't installed
    ran = []
    monkeypatch.setattr(cli, "_run_gh_login", lambda: ran.append(True))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)
    assert ran == []  # no login attempted — gh isn't installed


# --- startup menu-key conflict check ----------------------------------------------


def _menu_config(tmp_path, **data):
    from agitrack.config import GlobalConfig

    config = GlobalConfig(tmp_path / "config.json")
    config.data.update(data)
    return config


def test_menu_key_check_silent_without_conflict(tmp_path, monkeypatch):
    # No known host conflict (not VS Code) → never prompts.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "iTerm.app"})
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._verify_menu_key(_menu_config(tmp_path)) is True


def test_menu_key_check_non_interactive_does_not_prompt(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._verify_menu_key(_menu_config(tmp_path)) is True


def test_menu_key_check_keep_records_acknowledgement(tmp_path, monkeypatch):
    # VS Code + Ctrl-G conflicts; pressing Enter keeps it and records the ack so the next
    # launch stays quiet.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    monkeypatch.setattr("builtins.input", lambda *a: "")  # keep
    config = _menu_config(tmp_path)
    assert cli._verify_menu_key(config) is True
    assert config._raw("menu_key_acknowledged") == "ctrl-g"
    # Second launch: already acknowledged → no prompt.
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._verify_menu_key(config) is True


def test_menu_key_check_quit_aborts(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    assert cli._verify_menu_key(_menu_config(tmp_path)) is False


def test_menu_key_check_test_then_keep(tmp_path, monkeypatch):
    # 't' runs the key test (stubbed), then Enter keeps the key.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    answers = iter(["t", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    tested = []
    monkeypatch.setattr(cli, "_run_menu_key_test", lambda key: tested.append(key) or True)
    assert cli._verify_menu_key(_menu_config(tmp_path)) is True
    assert tested == ["ctrl-g"]


def _fake_msvcrt(monkeypatch, presses):
    # A stand-in msvcrt for the native-Windows menu-key probe so it's testable on POSIX
    # (the real path has no termios; #118). getch pops the queued keypresses in order.
    import sys

    queue = list(presses)
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(kbhit=lambda: bool(queue), getch=lambda: queue.pop(0))
    )


def test_read_menu_key_press_windows_detects_expected(monkeypatch):
    # Ctrl-G arriving as 0x07 means the key reached aGiTrack and would open the TUI menu.
    _fake_msvcrt(monkeypatch, [b"\x07"])
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=1.0) is True


def test_read_menu_key_press_windows_times_out_when_intercepted(monkeypatch):
    # The host (VS Code) swallowed the key — nothing ever arrives, so the probe reports False.
    _fake_msvcrt(monkeypatch, [])
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=0.05) is False


def test_read_menu_key_press_windows_ctrl_c_cancels(monkeypatch):
    _fake_msvcrt(monkeypatch, [b"\x03"])  # Ctrl-C
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=1.0) is None


def test_read_menu_key_press_windows_skips_function_key_scancodes(monkeypatch):
    # A function/arrow key (lead byte 0xe0 + scancode) is consumed, not matched; a real
    # Ctrl-G after it still registers.
    _fake_msvcrt(monkeypatch, [b"\xe0", b"H", b"\x07"])
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=1.0) is True


def test_menu_key_check_change_persists_new_key(tmp_path, monkeypatch):
    # 'c' to change → enter a non-conflicting key → it's persisted as menu_key.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    answers = iter(["c", "ctrl-o", "n"])  # choose; new key; skip the test
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    config = _menu_config(tmp_path)
    assert cli._verify_menu_key(config) is True
    assert config.menu_key == "ctrl-o"
    assert config._raw("menu_key_acknowledged") == "ctrl-o"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows msvcrt path returns False on timeout, not None")
def test_read_menu_key_press_returns_none_without_tty():
    # No real tty under pytest → the raw-mode test can't run and reports "unavailable".
    assert cli._read_menu_key_press(b"\x07", shift=False) is None


def test_discover_or_init_initializes_when_user_agrees(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None
    assert repo.current_branch() not in ("", "HEAD")  # initialized + seeded


def test_discover_or_init_stops_when_user_declines(tmp_path, monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")  # an EXPLICIT no

    repo = cli._discover_or_init(tmp_path)

    assert repo is None  # caller exits; aGiTrack can't run outside a git repo
    assert "cannot run outside a Git repository" in capsys.readouterr().out
    assert not (tmp_path / ".git").exists()  # nothing was created


def test_discover_or_init_defaults_to_creating_the_repo(tmp_path, monkeypatch):
    """Enter accepts. aGiTrack cannot track anything without a repo, so declining ends the run —
    the default must be the answer that lets the user get started, not the one that quits."""
    _force_tty(monkeypatch, stdin=True)
    prompts: list = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "")  # bare Enter

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None and (tmp_path / ".git").exists()
    assert "[Y/n]" in prompts[0]  # ...and the prompt says so


def test_discover_or_init_never_creates_a_repo_without_an_answer(tmp_path, monkeypatch):
    """A default of YES must not become "create a repo" when there is nobody to ask — an
    interrupted or closed stdin is not consent to write into the user's directory."""
    _force_tty(monkeypatch, stdin=True)

    def interrupted(*_a, **_k):
        raise EOFError

    monkeypatch.setattr("builtins.input", interrupted)

    assert cli._discover_or_init(tmp_path) is None
    assert not (tmp_path / ".git").exists()


def test_discover_or_init_non_interactive_does_not_prompt(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._discover_or_init(tmp_path) is None


# --- a folder that HOLDS repositories ---------------------------------------
#
# Running aGiTrack one directory too high — in `~/Code` rather than in `~/Code/my-project` —
# lands in a folder full of repositories that is not one itself. "Not a Git repository: ." is
# an opaque answer there, and `git init` (the offer's default everywhere else) is an actively
# bad one: it makes a single project out of the whole folder and still tracks nothing inside it.


def _folder_of_repos(tmp_path, count: int = 3):
    for index in range(count):
        (tmp_path / f"project-{index}" / ".git").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()
    return tmp_path


def test_repositories_inside_lists_child_repos_only(tmp_path):
    _folder_of_repos(tmp_path, count=2)
    # A linked worktree / submodule records `.git` as a FILE, and is still a repository.
    (tmp_path / "worktree").mkdir()
    (tmp_path / "worktree" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

    assert cli._repositories_inside(tmp_path) == ["project-0", "project-1", "worktree"]


def test_repositories_inside_is_empty_for_an_ordinary_folder(tmp_path):
    (tmp_path / "notes").mkdir()
    assert cli._repositories_inside(tmp_path) == []


def test_discover_or_init_explains_a_folder_of_repositories_instead_of_prompting(tmp_path, monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=False)
    _folder_of_repos(tmp_path)

    assert cli._discover_or_init(tmp_path) is None

    out = capsys.readouterr().out
    assert "3 folders inside it are" in out
    assert "project-0" in out and "not-a-repo" not in out
    assert "`cd` into the project you want" in out


def test_discover_or_init_defaults_to_NOT_creating_a_repo_around_a_folder_of_repositories(
    tmp_path, monkeypatch, capsys
):
    _force_tty(monkeypatch, stdin=True)
    prompts: list = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "")  # bare Enter

    _folder_of_repos(tmp_path)

    assert cli._discover_or_init(tmp_path) is None
    assert not (tmp_path / ".git").exists()  # Enter must not git-init a folder of projects
    assert "[y/N]" in prompts[0]  # ...and the prompt says which way it leans
    assert "would make one project out of the whole folder" in capsys.readouterr().out


def test_discover_or_init_still_initializes_a_folder_of_repositories_when_asked(tmp_path, monkeypatch):
    """The offer is de-emphasised, not removed: a vendored checkout inside a new project is a
    real (if rarer) thing to want."""
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    _folder_of_repos(tmp_path)

    repo = cli._discover_or_init(tmp_path)
    assert repo is not None and (tmp_path / ".git").exists()


def test_no_repo_message_explains_a_folder_of_repositories(tmp_path):
    """`agitrack status` / `stop` one directory too high get the same explanation, without the
    `git init` half — those commands create nothing."""
    _folder_of_repos(tmp_path)
    message = cli._no_repo_message(tmp_path, GitError("Not a Git repository: ."))

    assert message.startswith("Not a Git repository: .")
    assert "3 folders inside it are" in message
    assert "would make one project out of the whole folder" not in message


def test_no_repo_message_leaves_an_ordinary_failure_alone(tmp_path):
    assert cli._no_repo_message(tmp_path, GitError("Not a Git repository: .")) == "Not a Git repository: ."


# --- backend passthrough args (#32) -----------------------------------------


def _stub_repo_and_free_lock(monkeypatch):
    """Stub repo discovery to a lightweight object with a ``.repo`` path and the
    single-instance pre-check to "free", so cli.main reaches the launch surface."""
    import pathlib
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/agit-test")))

    class _FreeLock:
        def __init__(self, _path):
            pass

        def acquire(self, **kw):  # **kw: the takeover path passes retry_seconds (see RepoLock.acquire)
            return True  # nobody else holds it — we take it

        def release(self):
            pass

        def owner_pid(self):
            return None

        def probe_owner(self):
            return None

    monkeypatch.setattr(cli, "RepoLock", _FreeLock)


def _stub_launch(monkeypatch, *, use_worktrees: bool = True, commit_guidance: bool = True):
    """Stub the launch surface so cli.main only exercises arg routing.
    Returns the dict the fake runner/shell records its kwargs into."""
    captured: dict = {}

    class Fake:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "BackgroundRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)
    # These tests exercise main()'s arg routing, not the pre-TUI startup checks; neutralize
    # them so the minimal stub Config/repo below need no extra surface — and so the checks
    # don't behave differently by environment (e.g. the menu-key check firing because the
    # suite runs inside VS Code, or the gh check shelling out to `gh` on the stub repo,
    # which has no `_run` and broke CI where gh is unauthenticated).
    monkeypatch.setattr(cli, "_verify_menu_key", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_check_gh_availability", lambda *a, **k: (True, False))
    _stub_repo_and_free_lock(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    Config.use_worktrees = use_worktrees
    Config.commit_guidance = commit_guidance
    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())
    return captured


def test_already_running_refused_before_privacy_prompt(monkeypatch, capsys):
    # A second instance must be turned away BEFORE the privacy acknowledgement, so
    # the user isn't asked to acknowledge anything only to be refused.
    import pathlib
    from types import SimpleNamespace

    events: list[str] = []
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))

    class _HeldLock:
        def __init__(self, _path):
            pass

        def acquire(self, **kw):  # **kw: the takeover path passes retry_seconds (see RepoLock.acquire)
            return False  # another instance holds it — refuse

        def owner_pid(self):
            return 4321

        def release(self):
            pass

        def probe_owner(self):
            return 4321  # another instance holds it

    monkeypatch.setattr(cli, "RepoLock", _HeldLock)
    monkeypatch.setattr(
        cli, "already_running_message", lambda pid, **_kwargs: events.append(f"refused:{pid}") or "running"
    )
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: events.append("privacy") or True)

    class Config:
        check_for_updates = False
        use_worktrees = True

        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "opencode"])

    assert rc == 1
    assert events == ["refused:4321"]  # refused, and the privacy prompt never ran


def test_background_refused_when_another_instance_holds_the_repo(monkeypatch):
    # Only ONE aGiTrack per repo: a background tracker must be refused (never launched) when the
    # single-writer repo lock is already held — by ANY mode — so two never race over commits.
    import pathlib
    from types import SimpleNamespace

    launched: list = []
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "BackgroundRunner", lambda *a, **k: launched.append(k))

    class _HeldLock:
        def __init__(self, _path):
            pass

        def acquire(self, **kw):  # **kw: the takeover path passes retry_seconds (see RepoLock.acquire)
            return False  # another instance already holds the repo

        def owner_pid(self):
            return 999

        def release(self):
            pass

    monkeypatch.setattr(cli, "RepoLock", _HeldLock)
    monkeypatch.setattr(cli, "already_running_message", lambda pid, **_kwargs: "running")

    class Config:
        check_for_updates = False
        background = False

        def has_default_backend(self):
            return True

        default_backend = "claude"

        def load_repo_overlay(self, _root):
            pass

        def seed_defaults(self):
            return False

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--background", "--backend", "claude"])

    assert rc == 1
    assert launched == []  # the tracker was never constructed


def test_background_rerun_replaces_a_running_background_tracker(monkeypatch):
    # Re-running `agitrack -b` while a background tracker holds the repo lock REPLACES it
    # (like `-d`/`--backtrace`), so a rerun after an aGiTrack update always runs new code.
    import pathlib
    from types import SimpleNamespace

    from agitrack.proxy import background as bg

    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "BackgroundRunner", object())  # platform supports background mode
    monkeypatch.setattr(cli, "_maybe_prompt_background_hook", lambda *a, **k: None)

    class _HeldOnceLock:
        calls = 0

        def __init__(self, _path):
            pass

        def acquire(self, **kw):  # **kw: the takeover path passes retry_seconds (see RepoLock.acquire)
            type(self).calls += 1
            return type(self).calls > 1  # held by the old daemon; free once it stops

        def owner_pid(self):
            return 999

        def release(self):
            pass

    monkeypatch.setattr(cli, "RepoLock", _HeldOnceLock)
    replaced: list = []
    monkeypatch.setattr(bg, "replace_running_tracker", lambda repo, *, owner_pid: replaced.append(owner_pid) or True)
    started: list = []
    monkeypatch.setattr(bg, "start_background_daemon", lambda repo, *, extra_args: started.append(extra_args) or 0)

    class Config:
        check_for_updates = False
        background = False

        def has_default_backend(self):
            return True

        default_backend = "claude"

        def load_repo_overlay(self, _root):
            pass

        def seed_defaults(self):
            return False

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--background", "--backend", "claude"])

    assert rc == 0
    assert replaced == [999]  # the old tracker was stopped, not refused
    assert len(started) == 1  # and a fresh daemon was spawned


def _bg_rerun_over_live_tracker(monkeypatch, argv: list[str]):
    """Run `agitrack -b …` with the repo lock held by a live, current-version background tracker.
    Returns ``(rc, replaced_pids, spawn_args)``."""
    import pathlib
    from types import SimpleNamespace

    from agitrack.proxy import background as bg

    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "BackgroundRunner", object())
    monkeypatch.setattr(cli, "_maybe_prompt_background_hook", lambda *a, **k: None)

    class _HeldOnceLock:
        calls = 0

        def __init__(self, _path):
            pass

        def acquire(self, **kw):  # **kw: the takeover path passes retry_seconds (see RepoLock.acquire)
            type(self).calls += 1
            return type(self).calls > 1

        def owner_pid(self):
            return 999

        def release(self):
            pass

    monkeypatch.setattr(cli, "RepoLock", _HeldOnceLock)
    replaced: list = []
    started: list = []
    monkeypatch.setattr(bg, "replace_running_tracker", lambda repo, *, owner_pid: replaced.append(owner_pid) or True)
    monkeypatch.setattr(bg, "start_background_daemon", lambda repo, *, extra_args: started.append(extra_args) or 0)
    # A live tracker on the current version, running AUTO commits.
    monkeypatch.setattr(
        bg, "_running_tracker_is_current", lambda repo, *, owner_pid=None, manual=None: manual in (None, False)
    )

    class Config:
        check_for_updates = False
        background = False
        manual_commits = False

        def has_default_backend(self):
            return True

        default_backend = "claude"

        def load_repo_overlay(self, _root):
            pass

        def seed_defaults(self):
            return False

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())
    return cli.main(argv), replaced, started


def test_background_rerun_in_the_same_commit_mode_leaves_the_tracker_in_place(monkeypatch, capsys):
    # No new code to load and no mode change ⇒ don't churn (stop+respawn is what made the tracker
    # look like it "quit" on unrelated invocations).
    rc, replaced, started = _bg_rerun_over_live_tracker(monkeypatch, ["--background", "--backend", "claude"])

    assert rc == 0
    assert replaced == [] and started == []
    assert "left in place" in capsys.readouterr().out


def test_background_rerun_in_the_other_commit_mode_replaces_the_tracker(monkeypatch):
    # `agitrack -b -m` over an auto-commit daemon is a MODE SWITCH: it must replace the daemon, not
    # be told "already running" and silently keep the old mode.
    rc, replaced, started = _bg_rerun_over_live_tracker(
        monkeypatch, ["--background", "--manual-commits", "--backend", "claude"]
    )

    assert rc == 0
    assert replaced == [999]
    assert started and "--manual-commits" in started[0]  # the new mode is forwarded to the fresh daemon
    assert "--auto-commit" not in started[0]


def test_no_backend_configured_non_interactive_errors(monkeypatch, capsys):
    # No --backend and no configured default, run non-interactively: aGiTrack must
    # fail clearly rather than silently fall back to a hardcoded backend (the old
    # behaviour that produced surprise OpenCode sessions).
    _force_tty(monkeypatch, stdin=False)
    _stub_repo_and_free_lock(monkeypatch)
    launched: list = []

    class Fake:
        def __init__(self, repo, **kw):
            launched.append(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)

    class Config:
        use_worktrees = True

        def has_default_backend(self):
            return False

        default_backend = None

        def load_repo_overlay(self, _root):
            pass

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main([])

    assert rc == 1
    assert launched == []  # never launched a backend
    assert "No coding agent backend is configured" in capsys.readouterr().out


def test_explicit_backend_flag_launches_without_configured_default(monkeypatch):
    # An explicit --backend works even with no configured default (no fallback needed).
    _force_tty(monkeypatch, stdin=False)
    _stub_repo_and_free_lock(monkeypatch)
    captured: dict = {}

    class Fake:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)

    class Config:
        use_worktrees = True

        def has_default_backend(self):
            return False

        default_backend = None

        def load_repo_overlay(self, _root):
            pass

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "claude"])

    assert rc == 0
    assert captured.get("backend") == "claude"


# --- --no-worktree (#9) -----------------------------------------------------


def test_no_worktree_flag_disables_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--no-worktree"])
    assert captured["use_worktrees"] is False


def test_default_uses_config_use_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch, use_worktrees=False)  # config opt-out, no flag
    cli.main([])
    assert captured["use_worktrees"] is False


def test_manual_commits_flag_forces_no_worktree(monkeypatch):
    # --manual-commits always runs without a worktree, even though config has worktrees on.
    captured = _stub_launch(monkeypatch, use_worktrees=True)
    cli.main(["--manual-commits"])
    assert captured["manual_commits"] is True
    assert captured["use_worktrees"] is False


def test_manual_commits_short_flag(monkeypatch):
    # -m is the short alias and behaves identically.
    captured = _stub_launch(monkeypatch, use_worktrees=True)
    cli.main(["-m"])
    assert captured["manual_commits"] is True
    assert captured["use_worktrees"] is False


def test_manual_commits_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["manual_commits"] is False
    assert captured["use_worktrees"] is True  # worktrees stay on when manual mode is off


# --- --background / -b -------------------------------------------------------


def _autostart_config(tmp_path):
    from agitrack.config.settings import GlobalConfig

    cfg = GlobalConfig(path=tmp_path / "global.json")
    cfg.load_repo_overlay(tmp_path)  # repo overlay at tmp_path/.agitrack/config.json
    return cfg


def test_the_autostart_prompt_describes_what_this_backend_will_actually_install(tmp_path, monkeypatch, capsys):
    """The prompt is the consent moment, so it has to match what gets written.

    Answering yes installs a git pre-commit hook and a session-start hook on every backend —
    each in that backend's OWN file, so the prompt names the file the user can go and look at.
    Claude Code additionally has a turn-end hook, which no other backend exposes. Codex adds the
    one step aGiTrack cannot take for them: it will not run a hook nobody has reviewed, and says
    nothing when it skips one, so an install that looks complete does nothing until they trust
    it.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    cli._maybe_prompt_background_hook(_autostart_config(tmp_path / "claude"), scripted=False, backend="claude")
    claude_text = capsys.readouterr().out
    cli._maybe_prompt_background_hook(_autostart_config(tmp_path / "codex"), scripted=False, backend="codex")
    codex_text = capsys.readouterr().out

    assert ".claude/settings.local.json" in claude_text
    assert "without waiting for a commit" in claude_text  # Claude's extra, turn-end hook
    assert ".claude" not in codex_text
    assert ".codex/hooks.json" in codex_text
    assert "`/hooks` in Codex" in codex_text  # the step only Codex needs, said plainly
    for text in (claude_text, codex_text):
        assert "pre-commit hook" in text  # the parts every backend gets
        assert "session-start hook" in text
        assert "--remove-hooks" in text


def test_the_prompt_resolves_the_backend_the_daemon_will_use(tmp_path, monkeypatch):
    # --backend wins; otherwise the repo's own recorded backend; otherwise the global default.
    # Getting this wrong shows the wrong description to the user, which is the whole point.
    import json as _json

    from agitrack.git import GitRepo

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    repo = GitRepo.discover(tmp_path)
    config = _autostart_config(tmp_path)

    assert cli._autostart_backend(repo, "opencode", config) == "opencode"  # the flag wins

    (tmp_path / ".agitrack").mkdir(exist_ok=True)
    (tmp_path / ".agitrack" / "state.json").write_text(_json.dumps({"backend": "codex"}), encoding="utf-8")
    assert cli._autostart_backend(repo, None, config) == "codex"  # then what this repo ran


def test_background_hook_prompt_enable_off_and_reask_when_off(tmp_path, monkeypatch, capsys):
    # `agitrack -b` explains the auto-start hook and records the repo-scoped choice.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def run(answer):
        cfg = _autostart_config(tmp_path / (answer or "default"))  # a fresh repo per case
        monkeypatch.setattr("builtins.input", lambda *a: answer)
        cli._maybe_prompt_background_hook(cfg, scripted=False)
        return cfg

    assert run("").autotrack_hook == "auto"  # default (Enter) enables auto-start
    assert run("y").autotrack_hook == "auto"
    assert run("n").autotrack_hook == "off"
    assert "--remove-hooks" in capsys.readouterr().out  # tells the user how to cancel it

    # Once ENABLED for the repo, a later `-b` does not re-prompt.
    keep = run("y")
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("re-prompted")))
    cli._maybe_prompt_background_hook(keep, scripted=False)

    # But when it's OFF (e.g. after `agitrack --remove-hooks`), `-b` MUST ask again so the user
    # can turn it back on.
    keep.set("autotrack_hook", "off", scope="repo")
    reasked = []
    monkeypatch.setattr("builtins.input", lambda *a: reasked.append(1) or "y")
    cli._maybe_prompt_background_hook(keep, scripted=False)
    assert reasked and keep.autotrack_hook == "auto"  # re-asked and re-enabled


def test_background_hook_prompt_skipped_when_scripted(tmp_path, monkeypatch):
    cfg = _autostart_config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    cli._maybe_prompt_background_hook(cfg, scripted=True)
    assert cfg.source("autotrack_hook") != "repo"  # nothing recorded (default 'auto' stays implicit)


def _stub_bg_daemon(monkeypatch):
    """`agitrack -b` now spawns a DETACHED daemon: the launcher calls start_background_daemon
    with the flags forwarded to the child. Capture those to assert the resolved commit mode."""
    captured: dict = {}

    def fake_start(repo, *, extra_args, **kw):
        captured["extra_args"] = extra_args
        return 0

    monkeypatch.setattr("agitrack.proxy.background.start_background_daemon", fake_start)
    return captured


def test_background_flag_forces_no_worktree_and_auto_default(monkeypatch):
    # --background always runs without a worktree and defaults to AUTO commits (like the TUI).
    _stub_launch(monkeypatch, use_worktrees=True)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    captured = _stub_bg_daemon(monkeypatch)
    cli.main(["--background"])
    # The launcher forwards the resolved commit mode to the detached daemon child.
    assert "--auto-commit" in captured["extra_args"]  # auto by default
    assert "--manual-commits" not in captured["extra_args"]


def test_background_short_flag(monkeypatch):
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    captured = _stub_bg_daemon(monkeypatch)
    cli.main(["-b"])
    assert "--auto-commit" in captured["extra_args"]  # auto by default


def test_background_manual_commits_opts_into_manual(monkeypatch):
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    captured = _stub_bg_daemon(monkeypatch)
    cli.main(["-b", "-m"])
    assert "--manual-commits" in captured["extra_args"]  # -m opts into user-triggered commits


def test_background_stop_and_status_do_not_launch(monkeypatch):
    # `-b stop` / `-b status` are handled early and never construct a runner.
    calls: dict = {}
    monkeypatch.setattr(cli, "GitRepo", type("R", (), {"discover": staticmethod(lambda p: object())}))

    def _stop(repo):
        calls["stop"] = True
        return 0

    def _status(repo):
        calls["status"] = True
        return 0

    monkeypatch.setattr("agitrack.proxy.background.stop_background", _stop)
    monkeypatch.setattr("agitrack.proxy.background.background_status", _status)

    assert cli.main(["-b", "stop"]) == 0
    assert cli.main(["-b", "status"]) == 0
    assert calls == {"stop": True, "status": True}


def test_background_off_by_default(monkeypatch):
    # No --background ⇒ the normal proxy path runs (captures use_worktrees), background inert.
    captured = _stub_launch(monkeypatch, use_worktrees=True)
    cli.main([])
    assert captured["use_worktrees"] is True


# --- --no-sandbox / --allowed-edit-paths ------------------------------------


def test_sandbox_on_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    assert captured["sandbox"] is True


def test_no_sandbox_flag_disables_sandbox(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode", "--no-sandbox"])
    assert captured["sandbox"] is False


def test_allowed_edit_paths_flag_splits_on_pathsep(monkeypatch):
    import os

    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode", "--allowed-edit-paths", os.pathsep.join(["/data", "/srv/x"])])
    assert captured["allowed_edit_paths"] == ["/data", "/srv/x"]


def test_allowed_edit_paths_default_empty(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    assert captured["allowed_edit_paths"] == []


# --- --no-commit-guidance ---------------------------------------------------


def test_commit_guidance_on_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["commit_guidance"] is True


def test_no_commit_guidance_flag_disables_it(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--no-commit-guidance"])
    assert captured["commit_guidance"] is False


def test_default_uses_config_commit_guidance(monkeypatch):
    captured = _stub_launch(monkeypatch, commit_guidance=False)  # config opt-out, no flag
    cli.main([])
    assert captured["commit_guidance"] is False


def test_delay_merge_flag_passed_to_runner(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--delay-merge"])
    assert captured["delay_merge"] is True


def test_delay_merge_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["delay_merge"] is False


def _stub_source_repo(monkeypatch, *, rev: str, status: str) -> None:
    """Stand in for aGiTrack's own source checkout in the --version tests."""

    class _Source:
        def __init__(self, root):
            self.repo = root

        def short_sha(self, ref="HEAD"):
            return rev

        def status_short(self):
            return status

    # Patched where the version line RESOLVES it: `agitrack.versioning` imports GitRepo lazily
    # from `agitrack.git`, so that is the name to stand in for — `cli.GitRepo` is a different
    # binding and stubbing it left the real git running against a tmp dir that is not a repo.
    monkeypatch.setattr("agitrack.git.GitRepo", _Source)


def test_version_flag_prints_version_and_exits(monkeypatch, capsys):
    # `agitrack --version` is cheap and side-effect-free: no repo discovery, no
    # privacy prompt. The VSCode extension reads it to detect a self-updated CLI.
    import agitrack

    called = {"discover": False}
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: called.__setitem__("discover", True))
    monkeypatch.setattr("agitrack.update.updater.detect_source_repo", lambda: None)  # a package install
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == agitrack.__version__
    assert called["discover"] is False  # exits before touching the repo


def test_version_flag_names_the_commit_on_a_source_checkout(monkeypatch, capsys, tmp_path):
    # On a checkout the release version is ambiguous — every commit between two releases reports
    # the same one — so it cannot answer "which code am I running?", which is what --version is
    # asked. The release version stays FIRST and unadorned so `--version | cut -d" " -f1` is
    # unchanged for anything parsing it.
    import agitrack

    monkeypatch.setattr("agitrack.update.updater.detect_source_repo", lambda: tmp_path)
    _stub_source_repo(monkeypatch, rev="c3eba7d7", status="")  # a CLEAN checkout

    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out.strip()

    assert out == f"{agitrack.__version__} (source c3eba7d7)"
    assert out.split()[0] == agitrack.__version__


def test_version_flag_marks_a_dirty_source_checkout(monkeypatch, capsys, tmp_path):
    # "which code is this?" is only half-answered by a sha when the tree carries uncommitted
    # work — which, on the aGiTrack checkout itself, is most of the time.
    monkeypatch.setattr("agitrack.update.updater.detect_source_repo", lambda: tmp_path)
    _stub_source_repo(monkeypatch, rev="c3eba7d7", status=" M agitrack/cli.py\n")

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip().endswith("(source c3eba7d7-dirty)")


def test_version_flag_falls_back_to_the_bare_version_when_git_fails(monkeypatch, capsys, tmp_path):
    # Reporting a version must not be something that can fail.
    import agitrack

    monkeypatch.setattr("agitrack.update.updater.detect_source_repo", lambda: tmp_path)
    monkeypatch.setattr(cli, "GitRepo", lambda root: (_ for _ in ()).throw(OSError("no git")))

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == agitrack.__version__


def test_startup_message_printed_for_interactive_proxy(monkeypatch, capsys):
    # Entering aGiTrack prints immediate feedback so the terminal isn't silent while the
    # TUI comes up — shown however it was launched (terminal or VSCode).
    _stub_launch(monkeypatch)
    cli.main([])
    assert "aGiTrack is starting..." in capsys.readouterr().out


def test_startup_message_suppressed_in_json_mode(monkeypatch, capsys):
    # json/bridge output is machine-readable; the human "starting" line must not leak in.
    _stub_launch(monkeypatch)
    cli.main(["--prompt", ":status"])
    assert "aGiTrack is starting..." not in capsys.readouterr().out


def test_recover_flag_finalizes_and_exits(tmp_path, monkeypatch, capsys):
    # `agitrack --recover` runs headless recovery and exits — no privacy prompt,
    # no TUI, no "starting" line. With no session worktrees there is nothing to do.
    from agitrack.git import GitRepo

    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "cfg"))
    GitRepo.init(tmp_path / "repo")
    rc = cli.main(["--repo", str(tmp_path / "repo"), "--recover"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to recover." in out
    assert "aGiTrack is starting" not in out  # recovery is not an interactive launch


def test_update_check_runs_under_a_tty(monkeypatch):
    # The startup self-update offer is gated only on a TTY (+ config) — NOT on any
    # editor/environment signal — so it runs inside VSCode's integrated terminal,
    # which is a real PTY, exactly as in a standalone terminal.
    captured = _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    ran = {"checked": False}
    monkeypatch.setattr(cli, "_check_for_update_at_startup", lambda config: ran.__setitem__("checked", True))

    cli.main(["-i"])  # plain interactive proxy launch (bare `agitrack` on a tty shows the menu)

    assert ran["checked"] is True
    assert captured  # launch still proceeded


def test_update_check_skipped_without_a_tty(monkeypatch):
    _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    ran = {"checked": False}
    monkeypatch.setattr(cli, "_check_for_update_at_startup", lambda config: ran.__setitem__("checked", True))

    cli.main([])

    assert ran["checked"] is False  # no way to answer a prompt without a TTY


def test_json_flag_selects_the_json_prompt_loop(monkeypatch):
    # --json is the documented flag for the JSON prompt-loop; it must route to AgitrackShell
    # (json mode), exactly like the deprecated `--mode json` alias the other tests use.
    captured = _stub_launch(monkeypatch)
    cli.main(["--json", "--prompt", "hi"])
    assert "json_events" in captured  # a shell-only kwarg ⇒ the json shell (not the proxy) launched


def test_ui_bridge_flag_passed_to_shell_and_forces_json_mode(monkeypatch):
    # --ui-bridge is a json-mode transport: it must reach the shell and select json
    # mode even without an explicit --json (the VSCode extension relies on this).
    captured = _stub_launch(monkeypatch)
    cli.main(["--ui-bridge"])
    assert captured["ui_bridge"] is True


def test_ui_bridge_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--mode", "json", "--prompt", "hi"])
    assert captured["ui_bridge"] is False


def test_json_events_flag_passed_to_shell(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--mode", "json", "--json-events", "--prompt", "hi"])
    assert captured["json_events"] is True


def test_json_events_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--mode", "json", "--prompt", "hi"])
    assert captured["json_events"] is False


def test_full_agent_messages_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["full_agent_messages"] is False


def test_full_agent_messages_flag_enables_it(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--full-agent-messages"])
    assert captured["full_agent_messages"] is True


def test_full_agent_messages_flag_not_forwarded_to_backend(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--full-agent-messages"])
    assert "--full-agent-messages" not in captured["backend_args"]


def test_unknown_args_forwarded_to_backend(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend", "opencode", "--port", "12345"])
    assert rc == 0
    assert captured["backend_args"] == ["--port", "12345"]


def test_double_dash_forwards_agitrack_defined_flags_and_prompt(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--", "--verbose", "fix the bug"])
    # everything after -- goes to the backend, including a flag aGiTrack also owns
    assert captured["backend_args"] == ["--verbose", "fix the bug"]


def test_agitrack_flags_still_bind_before_separator(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--verbose", "--backend", "claude", "--", "--model", "opus"])
    # --verbose before -- is aGiTrack's; only post-separator args pass through
    assert captured["backend_args"] == ["--model", "opus"]


def test_no_passthrough_args_is_empty_list(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    assert captured["backend_args"] == []


def test_reserved_passthrough_flag_warns_but_forwards(monkeypatch, capsys):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--resume", "abc123"])
    out = capsys.readouterr().out
    assert "--resume" in out and "session" in out.lower()
    assert captured["backend_args"] == ["--resume", "abc123"]  # still forwarded


def test_backend_command_flag_passed_to_runner(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend-command", "somewrapper claude"])
    assert rc == 0
    assert captured["backend_command"] == ["somewrapper", "claude"]


def test_backend_command_absent_resolves_from_config(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    # No flag and the stub config has no backend_command ⇒ launch the binary directly.
    assert captured["backend_command"] == []


def test_backend_command_invalid_value_fails_fast(monkeypatch, capsys):
    _stub_launch(monkeypatch)
    rc = cli.main(["--backend-command", 'wrap "unbalanced'])
    assert rc == 1
    assert "backend-command" in capsys.readouterr().out.lower()


def test_backend_command_mismatch_warns(monkeypatch, capsys):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["-i", "--backend", "claude", "--backend-command", "wrap opencode"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Warning" in out and "opencode" in out and "claude" in out
    # The launch still goes through with exactly what the user asked for.
    assert captured["backend_command"] == ["wrap", "opencode"]


def test_backend_command_mismatch_aborts_when_declined(monkeypatch, capsys):
    # Interactive run: a mismatch must be explicitly confirmed; declining (anything but
    # y) aborts before the backend is ever launched.
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    rc = cli.main(["-i", "--backend", "claude", "--backend-command", "wrap opencode"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Warning" in out and "not started" in out
    assert captured == {}  # the runner was never constructed


def test_backend_command_mismatch_proceeds_when_confirmed(monkeypatch):
    # Entering y proceeds with exactly the command the user asked for.
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    rc = cli.main(["-i", "--backend", "claude", "--backend-command", "wrap opencode"])
    assert rc == 0
    assert captured["backend_command"] == ["wrap", "opencode"]


def test_backend_command_naming_selected_backend_does_not_warn(monkeypatch, capsys):
    _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--backend-command", "somewrapper claude"])
    assert "Warning" not in capsys.readouterr().out


def test_backend_command_opaque_wrapper_does_not_warn(monkeypatch, capsys):
    # A wrapper that doesn't name any known backend is left alone (no guessing).
    _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--backend-command", "mylauncher --flag"])
    assert "Warning" not in capsys.readouterr().out


def test_proxy_runner_stores_backend_command(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend="opencode", backend_command=["somewrapper", "opencode"])
    assert runner._backend_command == ["somewrapper", "opencode"]
    # The launch command flows into the spawned command's executable head.
    assert runner._launch_command() == ["somewrapper", "opencode"]


def test_proxy_runner_stores_backend_args(tmp_path):
    # Build a runner through the real __init__ (with a tmp git repo) and confirm
    # passthrough args are stored for _spawn to append.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend="opencode", backend_args=["--port", "9999"])
    assert runner._backend_args == ["--port", "9999"]
    # _spawn appends them after spawn_command; verify that composition directly.
    base = ["opencode", str(tmp_path)]
    assert base + runner._backend_args == ["opencode", str(tmp_path), "--port", "9999"]


def test_json_backends_append_backend_args():
    from agitrack.backends.claude import ClaudeBackend
    from agitrack.backends.opencode import OpenCodeBackend

    claude = ClaudeBackend("/repo", backend_args=["--max-budget-usd", "5"])
    assert claude.backend_args == ["--max-budget-usd", "5"]

    oc = OpenCodeBackend("/repo", backend_args=["--port", "0"])
    assert oc.backend_args == ["--port", "0"]


# --- combined help (#32) ----------------------------------------------------


def _no_backend_spawn(monkeypatch):
    """Record any backend-CLI invocation so a help test can assert none happened."""
    calls: list = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: calls.append(args) or None)
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    return calls


def test_help_shows_only_agitrack_options(monkeypatch, capsys):
    # `agitrack --help` shows aGiTrack's OWN options only — never the backend's help.
    calls = _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "--backend" in out and "--no-commit-guidance" in out  # aGiTrack's own options
    assert "Backend help" not in out  # NOT the backend's help section
    assert calls == []  # the backend CLI was never invoked for help


def test_help_survives_a_legacy_code_page_console(monkeypatch, capsys):
    """`agitrack --help` must be printable on a cp1252 console (#233).

    It was not: one help string described the "user↔agent trace", and U+2194 has no cp1252
    representation, so `--help` — the first command anyone runs — died with a
    UnicodeEncodeError on a default Windows console. Asserting on the whole rendered help
    rather than on that one string is the point: any future arrow, box-drawing character or
    emoji added to an option's help fails here instead of on a user's terminal.
    """
    _no_backend_spawn(monkeypatch)

    assert cli.main(["--help"]) == 0

    help_text = capsys.readouterr().out
    help_text.encode("cp1252")  # raises UnicodeEncodeError if an unrepresentable char is back


def test_console_output_degrades_instead_of_crashing(monkeypatch):
    """The second line of defence: paths, branch names and backend errors are not ours to
    sanitize, and any of them can carry a character the console's code page lacks."""
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(cli.sys, "stdout", stream)

    cli._make_console_output_lossy()

    stream.write("user↔agent")  # would raise UnicodeEncodeError under errors="strict"
    stream.flush()
    assert stream.errors == "replace"


def test_help_short_flag_shows_only_agitrack_options(monkeypatch, capsys):
    calls = _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "claude"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["-h"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "Backend help" not in out
    assert calls == []


def test_help_with_explicit_backend_still_omits_backend_help(monkeypatch, capsys):
    # Even with --backend, `--help` shows only aGiTrack's options.
    calls = _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "opencode", "--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "--backend" in out
    assert "Backend help" not in out
    assert calls == []


def test_help_works_with_no_backend_selected(monkeypatch, capsys):
    # Help is backend-independent now: it shows even when no backend is chosen yet, and
    # no longer prints the old "No backend selected yet" combined-help note.
    _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "No backend selected yet" not in out


def test_backend_help_via_double_dash_runs_directly(monkeypatch):
    """Test that --backend X -- --help runs backend help directly, not combined help."""
    monkeypatch.setattr(
        cli, "_discover_or_init", lambda p: (_ for _ in ()).throw(AssertionError("TUI should not launch"))
    )
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    class FakeResult:
        returncode = 0

    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append(args[0])
        return FakeResult()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(["--backend", "opencode", "--", "--help"])
    assert rc == 0
    assert run_calls == [["opencode", "--help"]]


def test_backend_help_runs_directly_without_tui(monkeypatch):
    monkeypatch.setattr(
        cli, "_discover_or_init", lambda p: (_ for _ in ()).throw(AssertionError("TUI should not launch"))
    )
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: FakeResult())

    rc = cli.main(["--backend", "opencode", "--", "--help"])
    assert rc == 0


def test_backend_help_no_backend_selected(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--", "--help"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "No backend selected" in out


def test_backend_other_args_still_launch_tui(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend", "opencode", "--", "--port", "12345"])
    assert rc == 0
    assert captured["backend_args"] == ["--port", "12345"]


# --- scripted prompts: --prompt (#53) -----------------------------------------


def test_prompt_flag_implies_json_mode_and_passes_prompts(monkeypatch):
    captured: dict = {}

    class FakeShell:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0  # the real shell returns a process exit code, which main() propagates

    monkeypatch.setattr(cli, "AgitrackShell", FakeShell)
    monkeypatch.setattr(
        cli, "ProxyRunner", lambda *a, **k: (_ for _ in ()).throw(AssertionError("proxy must not launch"))
    )
    _stub_repo_and_free_lock(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "claude"
        use_worktrees = True

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--prompt", "build it", "--prompt", ":status"])

    assert rc == 0
    assert captured["prompts"] == ["build it", ":status"]


def test_prompt_flag_never_blocks_on_input_even_with_a_tty(monkeypatch):
    # A scripted run must sail past the privacy acknowledgment and the
    # first-run backend selection — both would otherwise input() on a TTY.
    captured: dict = {}

    class FakeShell:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0  # the real shell returns a process exit code, which main() propagates

    monkeypatch.setattr(cli, "AgitrackShell", FakeShell)
    _stub_repo_and_free_lock(monkeypatch)

    class Config:
        def has_default_backend(self):
            return False  # would trigger the interactive first-run selection

        default_backend = None
        use_worktrees = True

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("scripted run must not prompt"))
    )

    rc = cli.main(["--backend", "claude", "--prompt", "build it"])

    assert rc == 0
    assert captured["prompts"] == ["build it"]


# --- startup privacy warning --------------------------------------------------


def test_privacy_warning_acknowledged_with_enter(monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    assert cli._acknowledge_privacy_warning() is True
    out = capsys.readouterr().out
    # The warning explains what is logged and what not to enter.
    assert "git commit" in out
    assert "passwords, API keys" in out


def test_privacy_warning_drains_stdin_before_reading(monkeypatch):
    # A stray newline injected into the terminal (e.g. by an editor's shell integration)
    # must not auto-acknowledge: pending input is flushed BEFORE the prompt reads, so the
    # acknowledgment stays a deliberate keypress.
    events: list[str] = []
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: events.append("drain"))
    monkeypatch.setattr("builtins.input", lambda *a: events.append("input") or "")

    assert cli._acknowledge_privacy_warning() is True
    assert events == ["drain", "input"]  # drained first, then read


def test_drain_terminal_input_never_raises():
    cli._drain_terminal_input()  # no real tty under pytest; must be a safe no-op


def test_privacy_warning_wraps_to_terminal_width():
    # A narrow terminal must wrap the warning tighter (different break points) so it never
    # overflows, while a wide terminal keeps the authored wrapping.
    wide = cli._privacy_warning(100)
    narrow = cli._privacy_warning(34)
    for text in (wide, narrow):
        assert text.startswith("\n")  # leading blank line preserved
        assert "passwords, API keys" in text  # key phrase never split mid-wrap
    # Narrow re-wraps: more lines, and every line fits the width.
    narrow_lines = [line for line in narrow.splitlines() if line]
    assert len(narrow_lines) > len([line for line in wide.splitlines() if line])
    assert all(len(line) <= 34 for line in narrow_lines)


def test_privacy_warning_never_exceeds_authored_width_on_wide_terminal():
    # On a very wide terminal we cap at the authored width rather than stretching the text
    # across the whole screen.
    lines = [line for line in cli._privacy_warning(500).splitlines() if line]
    assert lines and all(len(line) <= cli._PRIVACY_WARNING_WIDTH for line in lines)


def test_privacy_warning_quit_aborts(monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "q")

    assert cli._acknowledge_privacy_warning() is False
    assert "not started" in capsys.readouterr().out


def test_privacy_warning_interrupt_aborts(monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert cli._acknowledge_privacy_warning() is False


def test_privacy_warning_non_interactive_prints_and_continues(monkeypatch, capsys):
    # No TTY = no way to acknowledge: print the warning, never block automation.
    _force_tty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._acknowledge_privacy_warning() is True
    assert "passwords, API keys" in capsys.readouterr().out


def test_privacy_warning_skipped_does_not_print_or_prompt(monkeypatch, capsys):
    # A menu-update restart passes skip=True: no warning, no prompt, just continue.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._acknowledge_privacy_warning(skip=True) is True
    assert capsys.readouterr().out == ""


def test_main_stops_when_privacy_warning_declined(monkeypatch):
    # json/scripted startup acknowledges the privacy warning in cli.main (no pre-TUI config
    # steps precede it there). The interactive TUI path acks it inside the runner, AFTER the
    # gh-login / menu-key / backend-install steps — see test_proxy.test_run_*privacy*.
    captured = _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "q")

    rc = cli.main(["--mode", "json"])

    assert rc == 1
    assert captured == {}  # the shell was not launched


def test_auto_commit_overrides_manual_in_interactive_mode(monkeypatch):
    """C4: the --auto-commit override sat INSIDE the `if background:` block, so it could never
    fire for a TUI run. The flag was accepted, printed nothing, and did nothing: the turn stayed
    latent on refs/agitrack/manual/…, the branch never advanced (+0 commits after 420 s), and
    --help did not say it was background-only."""
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)

    assert cli.main(["--manual-commits", "--auto-commit"]) == 0

    assert captured["manual_commits"] is False


def test_manual_commits_alone_still_means_manual(monkeypatch):
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)

    assert cli.main(["--manual-commits"]) == 0

    assert captured["manual_commits"] is True


def test_overwrite_shared_alone_is_an_error_not_an_agent_session(monkeypatch, capsys):
    """C34: `--overwrite-shared` is a MODIFIER and was read nowhere except next to
    --share-sessions, so on its own it fell straight through to launching a full agent session.
    On a machine with a configured backend, someone who typed it meaning to fix a rejected share
    got a live agent instead."""
    monkeypatch.setattr(
        cli, "ProxyRunner", lambda *a, **k: pytest.fail("--overwrite-shared alone must not start a session")
    )

    assert cli.main(["--overwrite-shared"]) == 2

    out = capsys.readouterr().out
    assert "--share-sessions" in out


def test_flag_prefixes_are_not_accepted(monkeypatch, capsys):
    """The refusal message named `--overwrite`, a flag that does not exist in --help. It only
    ever resolved because argparse prefix-matches, and it would have broken silently the moment
    a second `--overwrite*` option was added. With allow_abbrev off, `--overwrite` is an unknown
    flag — it is passed through to the backend like any other, not silently promoted to
    `--overwrite-shared`."""
    monkeypatch.setattr(
        cli, "_run_share_sessions", lambda *a, **k: pytest.fail("a flag PREFIX must not trigger a share")
    )
    monkeypatch.setattr(cli, "ProxyRunner", lambda *a, **k: pytest.fail("--overwrite must not start a session either"))
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    _stub_launch(monkeypatch)

    # Reaching the launcher at all (rather than the share path or the overwrite-shared error)
    # is the proof: the prefix was not resolved to the real flag.
    assert cli.main(["--overwrite", "-s"]) == 0


def test_the_usage_line_does_not_leak_the_interpreter_path(capsys):
    """`--help`'s usage line was `usage: python.exe C:\\Users\\dev\\AppData\\Local\\...\\agitrack`
    — Python 3.14 on Windows derives prog from sys.argv[0] — leaking the user's home layout into
    any pasted help output, and the only line over 80 columns."""
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: agitrack" in out


def _no_configured_backend(monkeypatch):
    """A machine that has never chosen a default — the state the backend gate is written for."""

    class Config:
        default_backend = None
        use_worktrees = False
        commit_guidance = True

        def has_default_backend(self):
            return False

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())


def test_the_only_installed_backend_is_used_without_a_tty(monkeypatch, capsys):
    """Windows/no-TTY: bare `agitrack` and `agitrack -b` both stopped at the backend gate
    IDENTICALLY whether one or two backends were installed, so an explicitly headless mode
    required a terminal for its first run and the entire documented prompt chain was
    unreachable. Five separate live-test scenarios dead-ended here."""
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "backend_installed", lambda name: name == "claude")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _no_configured_backend(monkeypatch)

    assert cli.main([]) == 0

    assert captured["backend"] == "claude"
    out = capsys.readouterr().out
    assert "only coding agent backend installed" in out


def test_the_gate_offers_only_backends_that_are_actually_installed(monkeypatch, capsys):
    """The no-backend message offered `--backend <claude|codex|opencode>` unconditionally, so on
    a box where codex is not installed two of its three suggestions would have failed."""
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "backend_installed", lambda name: name in {"claude", "opencode"})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _no_configured_backend(monkeypatch)

    assert cli.main([]) == 1

    out = capsys.readouterr().out
    assert "claude|opencode" in out
    assert "codex" not in out


def test_the_gate_says_so_when_nothing_is_installed(monkeypatch, capsys):
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "backend_installed", lambda name: False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _no_configured_backend(monkeypatch)

    assert cli.main([]) == 1
    assert "None of the supported backends are installed" in capsys.readouterr().out


def test_help_does_not_promise_things_that_do_not_exist(capsys):
    """A message naming a flag, file, or command that does not exist is filed as a bug in its own
    right by this project's own live-test plan. These four were found live."""
    assert cli.main(["--help"]) == 0
    # argparse hard-wraps help text, so compare on a whitespace-normalised copy.
    out = " ".join(capsys.readouterr().out.split())

    # `--skip-privacy-ack` is the only way to get a clean machine-readable stream, and a driver
    # author cannot find a flag that is not in --help (it was argparse.SUPPRESS'd).
    assert "--skip-privacy-ack" in out
    # `--ui-bridge` is newline-delimited JSON, not JSON-RPC 2.0; developers who believed the help
    # wrote JSON-RPC clients and got silence.
    assert "JSON-RPC 2.0" in out and "NEWLINE-DELIMITED JSON" in out
    # `--log-file` claimed a "merge integrated" event that no code ever emits.
    assert "merge integrated" not in out
    # Confinement protects the REPOSITORY, not the rest of the disk — true on both platforms by
    # construction (`--dev-bind / /` on Linux, `(allow default)` on macOS).
    assert "protects the REPOSITORY, not the rest of the disk" in out
    # `--auto-commit` is not background-only.
    assert "in EVERY mode" in out


def test_a_failed_takeover_does_not_name_the_pid_it_just_killed(monkeypatch, capsys):
    """The `-b -m` zero-writer bug's user-facing half: on losing the lock race it printed
    "already running (PID N)" naming the PID it had just terminated, and told the user to
    `agitrack -b stop` a process that no longer existed."""
    import pathlib
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))
    monkeypatch.setattr(cli, "_refuse_during_merge_conflict", lambda repo: False)
    monkeypatch.setattr("agitrack.config.migrate.migrate_repo_state", lambda repo: None)

    class _Lock:
        def __init__(self, _path):
            pass

        def acquire(self, **kw):
            return False  # never obtainable, even with the retry

        def owner_pid(self):
            return 4242

        def release(self):
            pass

    monkeypatch.setattr(cli, "RepoLock", _Lock)
    monkeypatch.setattr("agitrack.proxy.background._running_tracker_is_current", lambda *a, **k: False)
    monkeypatch.setattr("agitrack.proxy.background.replace_running_tracker", lambda *a, **k: True)

    class Config:
        default_backend = "claude"
        use_worktrees = False
        commit_guidance = True

        def has_default_backend(self):
            return True

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    assert cli.main(["-b", "-m"]) == 1

    out = capsys.readouterr().out
    assert "Nothing is tracking it now" in out
    assert "4242" not in out  # never name the process we just stopped


def test_an_optional_tool_install_does_not_default_to_yes(monkeypatch):
    """`gh` is explicitly OPTIONAL — aGiTrack degrades to git author names without it — yet a
    bare Enter shelled straight into `brew install gh`: a package install nobody asked for, on
    the most reflexive keypress there is. `git` is genuinely required, so it keeps its Y
    default."""
    monkeypatch.setattr(cli, "_installed_via_msi", lambda: False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr("agitrack.system_tools.can_install_tool", lambda name: True)
    monkeypatch.setattr(
        "agitrack.system_tools.install_system_tool", lambda name: pytest.fail(f"must not install {name}")
    )

    prompts: list[str] = []

    def _bare_enter(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(cli, "_ask", _bare_enter)

    assert cli._maybe_install_tool("gh", required=False) is False
    assert "[y/N]" in prompts[0]


def test_a_required_tool_install_still_defaults_to_yes(monkeypatch):
    installed: list[str] = []
    monkeypatch.setattr(cli, "_installed_via_msi", lambda: False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr("agitrack.system_tools.can_install_tool", lambda name: True)
    monkeypatch.setattr("agitrack.system_tools.install_system_tool", lambda name: installed.append(name) or True)
    monkeypatch.setattr(cli, "_ask", lambda prompt: "")

    assert cli._maybe_install_tool("git", required=True) is True
    assert installed == ["git"]


def test_yes_makes_the_startup_prompts_non_interactive(monkeypatch):
    """`agitrack -b` on a fresh config blocked on three startup questions whenever it ran ON a
    terminal — despite -b being the documented "no TUI, returns to your shell" path — so a
    scripted or CI use inside a terminal simply hung. There was no --yes at all, and `-b status`
    skipping the wizard made the two inconsistent."""
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    _no_configured_backend(monkeypatch)
    monkeypatch.setattr(cli, "backend_installed", lambda name: name == "claude")
    monkeypatch.setattr(
        cli, "select_default_backend", lambda *a, **k: pytest.fail("--yes must not open the backend chooser")
    )
    monkeypatch.setattr(
        cli,
        "select_default_summarizer_model",
        lambda *a, **k: pytest.fail("--yes must not open the summarizer chooser"),
    )

    assert cli.main(["--yes"]) == 0
    assert captured["backend"] == "claude"


def test_without_yes_a_terminal_still_gets_the_first_run_prompts(monkeypatch):
    """--yes is opt-in: an ordinary interactive first run must still be walked through setup."""
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    _no_configured_backend(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(cli, "select_default_backend", lambda *a, **k: asked.append("backend") or "claude")
    monkeypatch.setattr(cli, "select_default_summarizer_model", lambda *a, **k: asked.append("model"))
    monkeypatch.setattr(cli, "_installed_via_msi", lambda: False)

    cli.main(["-i"])  # a mode is named, so the bare-`agitrack` menu stays out of the way

    assert asked == ["backend", "model"]


def _fake_repo(tmp_path):
    return type("R", (), {"repo": tmp_path})()


def test_share_sessions_without_a_terminal_refuses_and_says_so_in_its_exit_code(tmp_path, monkeypatch, capsys):
    """A CI job that asked to publish its conversations and got exit 0 back would report
    success having published nothing. `--daemons stop` already makes this split; the refusal
    for want of a terminal is an error, the user answering "no" at the prompt is not."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)

    assert cli._run_share_sessions(_fake_repo(tmp_path)) == 1

    out = capsys.readouterr().out
    assert "--yes" in out  # and it names the way to do it deliberately


def test_share_sessions_answered_no_at_the_prompt_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: "n")

    assert cli._run_share_sessions(_fake_repo(tmp_path)) == 0


# --------------------------------------------------------------------------------------
# A git failure is never a traceback.
# --------------------------------------------------------------------------------------


def test_an_unhandled_git_failure_is_a_message_not_a_traceback(monkeypatch, capsys):
    """Every command guards the ``GitRepo.discover()`` that OPENS the repository, but nothing
    guarded the git commands that follow — and git can fail long after discovery succeeds. On a
    Windows box with neither ``core.longpaths`` nor the OS long-path opt-in, a repository whose
    path passes MAX_PATH opens fine and then fails on the first read of ``.git/packed-refs``:
    ``agitrack --repo <deep path> -d text`` printed a raw traceback ending in
    ``agitrack.git.repo.GitError: … fatal: couldn't read .git/packed-refs: Filename too long``."""
    from agitrack.git import GitError

    def _explode(argv=None):
        raise GitError("Command failed: git for-each-ref\nfatal: couldn't read .git/packed-refs: Filename too long")

    monkeypatch.setattr(cli, "_dispatch", _explode)

    assert cli.main(["-d", "text"]) == 1

    out = capsys.readouterr().out
    assert "Filename too long" in out
    assert "Traceback" not in out
    # ...and on Windows, where this has a cause aGiTrack can name, it names the two things that
    # actually fix it. `core.longpaths` is a Windows-only git setting and ENAMETOOLONG elsewhere
    # is the filesystem's own far larger limit, so the advice is deliberately not offered there.
    assert ("core.longpaths" in out and "MAX_PATH" in out) is (sys.platform == "win32")


def test_an_ordinary_git_failure_gets_no_long_path_advice(monkeypatch, capsys):
    """The hint is only right for one cause. Offering `core.longpaths` for an unrelated git
    error would send the user to change a setting that has nothing to do with it."""
    from agitrack.git import GitError

    monkeypatch.setattr(cli, "_dispatch", lambda argv=None: (_ for _ in ()).throw(GitError("fatal: bad object HEAD")))

    assert cli.main(["-s"]) == 1

    out = capsys.readouterr().out
    assert "bad object HEAD" in out
    assert "core.longpaths" not in out
