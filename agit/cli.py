from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from agit.backends.setup import select_default_backend
from agit.backends.proxy_agents import available_backends
from agit.git import GitError, GitRepo
from agit.config import GlobalConfig
from agit.proxy import ProxyRunner
from agit.shell import AgitShell

_BACKEND_COMMANDS = {
    "claude": "claude",
    "opencode": "opencode",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interactive agent + git commit orchestration.",
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="show aGiT help and backend help",
    )
    parser.add_argument("--repo", default=".", help="target Git repository path")
    parser.add_argument("--verbose", action="store_true", help="show aGiT diagnostic messages")
    parser.add_argument("--mode", choices=["proxy", "json"], default="proxy", help="interactive mode")
    parser.add_argument(
        "--backend",
        choices=available_backends(),
        default=None,
        help="agent backend to use; also saved as the global default",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="start a fresh backend conversation instead of resuming the last one",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        help="run the agent against the current branch instead of an isolated worktree "
        "(edits are visible live; no isolation/integration; unsafe with concurrent sessions)",
    )
    parser.epilog = (
        "Unrecognized arguments are forwarded verbatim to the backend CLI "
        "(claude / opencode), e.g. `agit --backend opencode --port 12345`. Use "
        "`--` to forward arguments that aGiT also defines or a bare prompt, e.g. "
        '`agit -- --verbose "fix the bug"`.'
    )
    # parse_known_args so backend-specific flags pass through instead of erroring.
    args, backend_args = parser.parse_known_args(argv)
    # argparse leaves a single leading "--" separator in the remainder; drop it.
    if backend_args and backend_args[0] == "--":
        backend_args = backend_args[1:]

    # First run: ask the user to choose a default backend before launching.
    config = GlobalConfig()

    # Handle help request before any other processing.
    if args.help:
        _show_combined_help(parser, args.backend, config)
        return 0

    # If backend is asked for help, run it directly without TUI.
    if backend_args and any(arg in ("--help", "-h") for arg in backend_args):
        backend = args.backend or config.default_backend
        if not backend:
            print("Error: No backend selected. Use --backend to specify one.")
            return 1
        backend_cmd = _BACKEND_COMMANDS.get(backend)
        if not backend_cmd:
            print(f"Error: Unknown backend '{backend}'.")
            return 1
        if not shutil.which(backend_cmd):
            print(f"Error: Backend '{backend}' not found on PATH.")
            return 1
        result = subprocess.run([backend_cmd] + backend_args, check=False)
        return result.returncode

    if args.backend is None and not config.has_default_backend() and sys.stdin.isatty() and sys.stdout.isatty():
        select_default_backend(config)

    # Worktrees on unless the config opts out or --no-worktree is passed (flag wins).
    use_worktrees = False if args.no_worktree else config.use_worktrees

    if backend_args:
        _warn_reserved_passthrough(args.backend or config.default_backend, backend_args)

    if not _acknowledge_privacy_warning():
        return 1

    try:
        repo = _discover_or_init(Path(args.repo).expanduser())
        if repo is None:
            return 1
        if args.mode == "json":
            AgitShell(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                backend_args=backend_args,
            ).run()
        else:
            return ProxyRunner(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                use_worktrees=use_worktrees,
                backend_args=backend_args,
            ).run()
    except (GitError, RuntimeError) as error:
        print(error)
        return 1
    return 0


# Flags aGiT injects itself to manage session tracking; forwarding a duplicate
# can fight aGiT's own session handling. We warn but still forward — aGiT never
# silently swallows the user's intent.
_RESERVED_PASSTHROUGH = {
    "claude": {"--session-id", "--resume", "-r", "--continue", "-c"},
    "opencode": {"--session", "-s", "--continue", "-c"},
}


def _warn_reserved_passthrough(backend: str, backend_args: list[str]) -> None:
    reserved = _RESERVED_PASSTHROUGH.get(backend, set())
    hit = sorted({arg for arg in backend_args if arg in reserved})
    if hit:
        print(
            f"Warning: forwarding {', '.join(hit)} to {backend}; aGiT manages "
            "session selection itself, so this may interfere with its session tracking."
        )


def _terminal_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except (AttributeError, ValueError):
        return 80


def _show_combined_help(
    parser: argparse.ArgumentParser,
    backend_arg: str | None,
    config: GlobalConfig,
) -> None:
    parser.print_help()
    backend = backend_arg or config.default_backend
    if not backend:
        print("\n(No backend selected yet. Run `agit` to choose one.)")
        return
    backend_cmd = _BACKEND_COMMANDS.get(backend)
    if not backend_cmd:
        print(f"\n(Unknown backend '{backend}'. Cannot show backend help.)")
        return
    if not shutil.which(backend_cmd):
        print(f"\n(Backend '{backend}' not found on PATH. Install it to see its help.)")
        return
    width = _terminal_width()
    print("\n" + "=" * width)
    print(f"Backend help ({backend})")
    print("=" * width + "\n")
    try:
        result = subprocess.run(
            [backend_cmd, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout or result.stderr)
    except Exception as error:
        print(f"(Could not run '{backend_cmd} --help': {error})")


PRIVACY_WARNING = (
    "WARNING: aGiT records the conversation in git commit messages — every\n"
    "message you enter in the chat can become part of the repository history.\n"
    "Do not enter passwords, API keys, or other sensitive information in the\n"
    "chat. (Keeping secrets out of prompts is good practice anyway.)"
)


def _acknowledge_privacy_warning() -> bool:
    """Show the privacy warning at startup; the user must acknowledge it to
    continue. Without a TTY there is no way to acknowledge, so the warning is
    printed and aGiT proceeds — never block automation on an ``input()`` that
    cannot be answered."""
    print(PRIVACY_WARNING)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    try:
        answer = input("Press Enter to acknowledge and continue (q to quit): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\naGiT not started.")
        return False
    if answer in {"q", "quit", "n", "no"}:
        print("aGiT not started.")
        return False
    return True


def _discover_or_init(path: Path) -> GitRepo | None:
    """Find the Git repository for ``path``, or offer to create one. aGiT cannot
    run outside a Git repository, so if the user declines (or we can't prompt),
    return None and let the caller stop."""
    try:
        repo = GitRepo.discover(path)
        # A user who ran `git init` themselves leaves an unborn HEAD (no commits),
        # which aGiT's worktree setup cannot use. Seed an initial commit so an
        # otherwise-empty repository starts cleanly.
        if repo.ensure_born():
            print(f"Seeded an initial commit in empty repository {repo.repo}")
        return repo
    except GitError:
        pass
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"Not a Git repository: {path}\naGiT requires a Git repository to run.")
        return None
    try:
        answer = input(f"{path} is not a Git repository. Initialize one here with `git init`? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in {"y", "yes"}:
        print("aGiT cannot run outside a Git repository. Exiting.")
        return None
    try:
        repo = GitRepo.init(path)
    except GitError as error:
        print(error)
        return None
    print(f"Initialized empty Git repository in {repo.repo}")
    return repo


if __name__ == "__main__":
    raise SystemExit(main())
