from __future__ import annotations

import argparse
from pathlib import Path

from agit.backends.proxy_agents import available_backends
from agit.git import GitError, GitRepo
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
    args = parser.parse_args(argv)

    try:
        repo = GitRepo.discover(Path(args.repo).expanduser())
        if args.mode == "json":
            AgitShell(repo, verbose=args.verbose, backend=args.backend).run()
        else:
            return ProxyRunner(repo, verbose=args.verbose, backend=args.backend).run()
    except (GitError, RuntimeError) as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
