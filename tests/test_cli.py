import subprocess

import pytest

from agit import cli
from agit.git import GitRepo


def _has_git() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


def test_git_init_seeds_usable_repo(tmp_path):
    (tmp_path / "file.txt").write_text("hello\n", encoding="utf-8")
    repo = GitRepo.init(tmp_path)

    # Valid HEAD (the seed commit) so worktree setup won't choke on an unborn branch.
    assert repo.current_branch() not in ("", "HEAD")
    # The user's pre-existing file is left untracked for aGiT's user-commit flow.
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
    # leaving their own files untracked for aGiT's user-commit flow.
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


def test_discover_or_init_initializes_when_user_agrees(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None
    assert repo.current_branch() not in ("", "HEAD")  # initialized + seeded


def test_discover_or_init_stops_when_user_declines(tmp_path, monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # default = no

    repo = cli._discover_or_init(tmp_path)

    assert repo is None  # caller exits; aGiT can't run outside a git repo
    assert "cannot run outside a Git repository" in capsys.readouterr().out
    assert not (tmp_path / ".git").exists()  # nothing was created


def test_discover_or_init_non_interactive_does_not_prompt(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._discover_or_init(tmp_path) is None


# --- backend passthrough args (#32) -----------------------------------------


def _stub_launch(monkeypatch, *, use_worktrees: bool = True):
    """Stub the launch surface so cli.main only exercises arg routing.
    Returns the dict the fake runner/shell records its kwargs into."""
    captured: dict = {}

    class Fake:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "AgitShell", Fake)
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    Config.use_worktrees = use_worktrees
    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())
    return captured


# --- --no-worktree (#9) -----------------------------------------------------


def test_no_worktree_flag_disables_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--no-worktree"])
    assert captured["use_worktrees"] is False


def test_default_uses_config_use_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch, use_worktrees=False)  # config opt-out, no flag
    cli.main([])
    assert captured["use_worktrees"] is False


def test_unknown_args_forwarded_to_backend(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend", "opencode", "--port", "12345"])
    assert rc == 0
    assert captured["backend_args"] == ["--port", "12345"]


def test_double_dash_forwards_agit_defined_flags_and_prompt(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--", "--verbose", "fix the bug"])
    # everything after -- goes to the backend, including a flag aGiT also owns
    assert captured["backend_args"] == ["--verbose", "fix the bug"]


def test_agit_flags_still_bind_before_separator(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--verbose", "--backend", "claude", "--", "--model", "opus"])
    # --verbose before -- is aGiT's; only post-separator args pass through
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


def test_proxy_runner_stores_backend_args(tmp_path):
    # Build a runner through the real __init__ (with a tmp git repo) and confirm
    # passthrough args are stored for _spawn to append.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agit.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend_args=["--port", "9999"])
    assert runner._backend_args == ["--port", "9999"]
    # _spawn appends them after spawn_command; verify that composition directly.
    base = ["opencode", str(tmp_path)]
    assert base + runner._backend_args == ["opencode", str(tmp_path), "--port", "9999"]


def test_json_backends_append_backend_args():
    from agit.backends.claude import ClaudeBackend
    from agit.backends.opencode import OpenCodeBackend

    claude = ClaudeBackend("/repo", backend_args=["--max-budget-usd", "5"])
    assert claude.backend_args == ["--max-budget-usd", "5"]

    oc = OpenCodeBackend("/repo", backend_args=["--port", "0"])
    assert oc.backend_args == ["--port", "0"]


# --- combined help (#32) ----------------------------------------------------


def test_help_shows_combined_help(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class FakeResult:
        stdout = "opencode help output"
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: FakeResult())

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "--backend" in out
    assert "Backend help (opencode)" in out
    assert "opencode help output" in out


def test_help_short_flag(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class FakeResult:
        stdout = "claude help output"
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: FakeResult())

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "claude"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["-h"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Backend help (claude)" in out
    assert "claude help output" in out


def test_help_with_explicit_backend(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class FakeResult:
        stdout = "opencode help output"
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: FakeResult())

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "opencode", "--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Backend help (opencode)" in out
    assert "opencode help output" in out


def test_help_no_backend_selected(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "No backend selected yet" in out


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
