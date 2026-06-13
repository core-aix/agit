"""Self-updater for aGiT.

aGiT can update itself in place. Two installation shapes are supported:

* **source-linked** — aGiT is importable from a git checkout of its own source
  (the documented ``pip install -e .`` editable install). Updates are pulled
  from the checkout's upstream branch with a fast-forward-only merge, so an
  update can never clobber local development: a dirty or diverged source tree
  blocks the update with a message instead.
* **package** — aGiT was installed as a wheel (e.g. ``pip install agit``).
  Updates run ``pip install --upgrade`` and the latest available version is
  discovered with ``pip index versions``.

The checking logic here is intentionally pure/blocking; callers (the CLI at
startup, the proxy runner's idle loop) run :meth:`Updater.check` in a background
thread so the terminal never stalls on a network ``git fetch``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import agit

# The PyPI distribution name. The import package and the installed command are
# both ``agit``; the plain ``agit`` name on PyPI belongs to an unrelated project,
# so aGiT is published as ``agit-cli`` (see pyproject.toml / scripts/publish.sh).
DIST_NAME = "agit-cli"

# How aGiT was installed, as reported by ``UpdateStatus.kind`` / ``Updater.kind``.
KIND_SOURCE = "source"
KIND_PACKAGE = "package"
KIND_UNKNOWN = "unknown"

# A short network timeout so a startup check never hangs the terminal. The
# periodic in-session check runs on a worker thread, but keep it bounded anyway.
_NET_TIMEOUT = 20

# Sentinel so callers can inject ``source_repo=None`` to mean "no source-linked
# install" (force the package path), distinct from "not provided -> auto-detect".
_DETECT = object()


@dataclass
class UpdateStatus:
    """Result of an update check.

    ``available`` is the only field callers must consult to decide whether to
    offer an update; the rest drive the user-facing message.
    """

    kind: str  # KIND_SOURCE | KIND_PACKAGE | KIND_UNKNOWN
    available: bool = False
    current: str = ""  # current revision (short sha) or version
    latest: str = ""  # target revision (short sha) or version
    behind: int = 0  # commits behind upstream (source installs only)
    message: str = ""  # human-readable summary for the UI
    error: str | None = None  # set when the check could not complete

    @property
    def ok(self) -> bool:
        return self.error is None


def _git(args: list[str], cwd: Path, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def detect_source_repo() -> Path | None:
    """Return the top level of aGiT's own source checkout when the running code
    is imported from one (an editable / source-linked install), else ``None``.

    We trust an install as source-linked only when the directory containing the
    ``agit`` package both is a git work tree *and* carries aGiT's own
    ``pyproject.toml``. We key off the console-script entry point
    (``agit.cli:main``) rather than the distribution ``name`` so the check keeps
    working if the published name changes. That avoids mistaking, say, a wheel
    that happens to sit under some unrelated repository for the real source.
    """
    try:
        module_file = Path(agit.__file__).resolve()
    except (AttributeError, TypeError):
        return None
    root = module_file.parent.parent  # .../<repo>/agit/__init__.py -> <repo>
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if "agit.cli:main" not in text:
        return None
    result = _git(["rev-parse", "--show-toplevel"], root)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


class Updater:
    """Detects how aGiT is installed and performs in-place updates."""

    def __init__(self, *, source_repo: Path | None | object = _DETECT) -> None:
        # source_repo is injectable for tests; production auto-detects it. Passing
        # an explicit None forces the package path (no source checkout).
        self._source_repo: Path | None = (
            detect_source_repo() if source_repo is _DETECT else cast("Path | None", source_repo)
        )

    @property
    def kind(self) -> str:
        if self._source_repo is not None:
            return KIND_SOURCE
        return KIND_PACKAGE

    @property
    def source_repo(self) -> Path | None:
        return self._source_repo

    # --- checking --------------------------------------------------------

    def check(self, *, fetch: bool = True) -> UpdateStatus:
        """Check whether a newer aGiT is available. Blocking (network); run on a
        worker thread from interactive contexts."""
        if self.kind == KIND_SOURCE:
            return self._check_source(fetch=fetch)
        return self._check_package()

    def _upstream_ref(self, repo: Path) -> str | None:
        # The current branch's configured upstream (e.g. "origin/main"). Without
        # one we cannot tell which remote branch to compare against, so the
        # source updater simply stays quiet.
        result = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
        if result.returncode != 0:
            return None
        ref = result.stdout.strip()
        return ref or None

    def _check_source(self, *, fetch: bool) -> UpdateStatus:
        repo = self._source_repo
        assert repo is not None
        status = UpdateStatus(kind=KIND_SOURCE)
        upstream = self._upstream_ref(repo)
        if upstream is None:
            status.error = "no upstream branch is configured for the aGiT source checkout"
            return status
        remote = upstream.split("/", 1)[0]
        if fetch:
            fetched = _git(["fetch", "--quiet", remote], repo, timeout=_NET_TIMEOUT)
            if fetched.returncode != 0:
                status.error = (fetched.stderr.strip() or "git fetch failed").splitlines()[-1]
                return status
        behind = _git(["rev-list", "--count", f"HEAD..{upstream}"], repo)
        if behind.returncode != 0:
            status.error = behind.stderr.strip() or "could not compare against upstream"
            return status
        status.behind = int(behind.stdout.strip() or "0")
        status.current = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        status.latest = _git(["rev-parse", "--short", upstream], repo).stdout.strip()
        status.available = status.behind > 0
        if status.available:
            commits = "commit" if status.behind == 1 else "commits"
            status.message = (
                f"aGiT update available: {status.behind} new {commits} on {upstream} "
                f"({status.current} → {status.latest})."
            )
        else:
            status.message = "aGiT is up to date."
        return status

    def _check_package(self) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        status.current = self._installed_version()
        latest = self._latest_package_version()
        if latest is None:
            status.error = "could not determine the latest published aGiT version"
            return status
        status.latest = latest
        status.available = _version_tuple(latest) > _version_tuple(status.current)
        if status.available:
            status.message = f"aGiT update available: {status.current} → {latest}."
        else:
            status.message = "aGiT is up to date."
        return status

    def _installed_version(self) -> str:
        try:
            from importlib import metadata

            return metadata.version(DIST_NAME)
        except Exception:
            return getattr(agit, "__version__", "0")

    def _latest_package_version(self) -> str | None:
        # `pip index versions` is the most portable way to ask the configured
        # index without a hard dependency on a PyPI JSON client. It is marked
        # experimental but degrades gracefully: any failure returns None and the
        # caller reports "could not determine the latest version".
        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", DIST_NAME],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_NET_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        prefix = f"{DIST_NAME.lower()} ("
        for line in result.stdout.splitlines():
            line = line.strip()
            # e.g. "agit-cli (1.2.3)" or "LATEST: 1.2.3"
            if line.upper().startswith("LATEST:"):
                return line.split(":", 1)[1].strip() or None
            if line.lower().startswith(prefix) and line.endswith(")"):
                return line[line.index("(") + 1 : -1].strip() or None
        return None

    # --- applying --------------------------------------------------------

    def apply(self) -> UpdateStatus:
        """Install the available update in place. Blocking. Returns a status
        whose ``available`` is False on success and ``error`` set on failure."""
        if self.kind == KIND_SOURCE:
            return self._apply_source()
        return self._apply_package()

    def _apply_source(self) -> UpdateStatus:
        repo = self._source_repo
        assert repo is not None
        status = UpdateStatus(kind=KIND_SOURCE)
        # Never update over local development: a dirty work tree means the user
        # has uncommitted changes a fast-forward would not touch but which make
        # an automatic update surprising. Bail with a clear message instead.
        dirty = _git(["status", "--porcelain"], repo)
        if dirty.returncode != 0:
            status.error = dirty.stderr.strip() or "could not inspect the source checkout"
            return status
        if dirty.stdout.strip():
            status.error = (
                "the aGiT source checkout has uncommitted changes; "
                "update skipped (commit or stash them, or update manually)"
            )
            return status
        upstream = self._upstream_ref(repo)
        if upstream is None:
            status.error = "no upstream branch is configured for the aGiT source checkout"
            return status
        # Refresh the upstream ref so we fast-forward onto the very latest commit,
        # even if the periodic check ran a while ago.
        remote = upstream.split("/", 1)[0]
        fetched = _git(["fetch", "--quiet", remote], repo, timeout=_NET_TIMEOUT)
        if fetched.returncode != 0:
            status.error = (fetched.stderr.strip() or "git fetch failed").splitlines()[-1]
            return status
        # --ff-only: refuse to create a merge commit. A diverged local branch
        # (the user committed their own work) blocks the auto-update rather than
        # rewriting their history.
        merged = _git(["merge", "--ff-only", upstream], repo, timeout=_NET_TIMEOUT)
        if merged.returncode != 0:
            status.error = (
                "could not fast-forward the aGiT source checkout "
                f"(local branch has diverged from {upstream}); update manually"
            )
            return status
        status.current = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        status.latest = status.current
        status.message = f"Updated aGiT source checkout to {status.current}."
        return status

    def _apply_package(self) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", DIST_NAME],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=600,
        )
        if result.returncode != 0:
            status.error = (result.stderr.strip() or result.stdout.strip() or "pip upgrade failed").splitlines()[-1]
            return status
        status.current = self._installed_version()
        status.latest = status.current
        status.message = f"Updated aGiT package to {status.current}."
        return status


def _version_tuple(version: str) -> tuple:
    # Lenient numeric-prefix comparison: "1.2.3" -> (1, 2, 3). Non-numeric
    # trailers (e.g. "1.2.3rc1") compare by their leading integer only, which is
    # good enough to answer "is the index version higher than mine?".
    parts: list[int] = []
    for chunk in version.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def restart_agit() -> NoReturn:
    """Re-exec aGiT in place so the freshly updated code is loaded.

    Uses ``python -m agit`` with the original CLI arguments so the entry point
    survives a package upgrade that may have rewritten the console script. This
    does not return on success.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "agit", *sys.argv[1:]])
