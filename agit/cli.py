from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agit.backends.setup import select_default_backend
from agit.backends.proxy_agents import available_backends
from agit.git import GitError, GitRepo
from agit.config import GlobalConfig
from agit.proxy import ProxyRunner
from agit.shell import AgitShell


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive agent + git commit orchestration.")
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
    args = parser.parse_args(argv)

    # First run: ask the user to choose a default backend before launching.
    config = GlobalConfig()
    if args.backend is None and not config.has_default_backend() and sys.stdin.isatty() and sys.stdout.isatty():
        select_default_backend(config)

    try:
        repo = _discover_or_init(Path(args.repo).expanduser())
        if repo is None:
            return 1
        if args.mode == "json":
            AgitShell(repo, verbose=args.verbose, backend=args.backend, new_session=args.new_session).run()
        else:
            return ProxyRunner(repo, verbose=args.verbose, backend=args.backend, new_session=args.new_session).run()
    except (GitError, RuntimeError) as error:
        print(error)
        return 1
    return 0


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
