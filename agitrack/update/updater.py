"""Self-updater for aGiTrack.

aGiTrack can update itself in place. Two installation shapes are supported:

* **source-linked** — aGiTrack is importable from a git checkout of its own source
  (the documented ``pip install -e .`` editable install). Updates merge the
  upstream branch into the checkout: a clean checkout fast-forwards, and a
  checkout carrying the user's own commits (aGiTrack runs on session worktree
  branches that accumulate commits) gets a normal merge. Only a genuine content
  conflict, or an uncommitted (dirty) tree, blocks the update — with a message
  instead of leaving the running source half-merged.
* **package** — aGiTrack was installed as a wheel (``pip install agitrack`` or
  ``pipx install agitrack``; there is no Homebrew formula). The latest available version is
  discovered with ``pip index versions``. The upgrade itself is, by preference,
  **package-manager-independent**: it runs the *running interpreter's own* pip
  (``<python> -m pip install --upgrade``), which upgrades a plain pip install, a venv,
  a ``--user`` install, and a pipx venv identically — no need to detect or shell out to
  pipx/brew/apt. It falls back to a ``pip3``/``pip`` on ``PATH`` only when that
  interpreter has no ``pip`` module. The one situation pip can't handle is an
  externally-managed (PEP 668) Python — Homebrew's or a distro's, where pip refuses to
  write. There is no manager to hand off to (no distro and no Homebrew package ships
  aGiTrack), so it reports the ONE command that fits the detected install instead.

The checking logic here is intentionally pure/blocking; callers (the CLI at
startup, the proxy runner's idle loop) run :meth:`Updater.check` in a background
thread so the terminal never stalls on a network ``git fetch``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from agitrack.proc import UTF8_TEXT, console_isolation_kwargs, detach_kwargs, fs_path

import agitrack

# The PyPI distribution name. After the aGiT → aGiTrack rename the distribution,
# the import package, and the installed command are all ``agitrack`` (see
# pyproject.toml / scripts/publish.sh).
DIST_NAME = "agitrack"

# How aGiTrack was installed, as reported by ``UpdateStatus.kind`` / ``Updater.kind``.
KIND_SOURCE = "source"
KIND_PACKAGE = "package"
KIND_UNKNOWN = "unknown"

# How a *package* install can be upgraded (returned by ``Updater._install_method``).
METHOD_PIP = "pip"
METHOD_PIPX = "pipx"
# Installed into a Homebrew-managed PYTHON (…/opt/homebrew/lib/pythonX/site-packages), which is
# what `brew install python` + `pip3 install agitrack` produces — the path the README's macOS
# section leads to. It does NOT mean aGiTrack came from a Homebrew formula: there is no
# `agitrack` formula or cask (checked against formulae.brew.sh), so `brew upgrade agitrack` can
# only ever fail. What this actually identifies is an externally-managed (PEP 668) install,
# where pip refuses to upgrade in place and pipx is the way out.
METHOD_BREW_PYTHON = "brew-python"
# A frozen (PyInstaller) Windows bundle installed by the perMachine MSI. Detected via
# ``sys.frozen`` + the ``HKLM\Software\aGiTrack\InstallDir`` registry key the WiX fragment
# writes. Updated by downloading and re-running the MSI, not pip — see ``_apply_msi``.
METHOD_MSI = "msi"

# The upstream GitHub repo that builds and publishes the Windows MSI release assets. The
# MSI install carries no aGiTrack source checkout, so (unlike a source install) there is no
# local git remote to read — releases always come from here. Overridable via
# ``AGITRACK_GH_REPO`` for forks/testing.
_DEFAULT_GH_REPO = "core-aix/agitrack"
# In-process cache of GitHub Releases API responses: url -> (etag, parsed_json, monotonic_ts).
# The anonymous API allows 60 req/h; our periodic check fires every ~5 min, but the cache
# (plus a conditional ``If-None-Match`` request) keeps even bursty on-demand checks well under.
_GH_CACHE: dict[str, tuple[str, object, float]] = {}
_GH_CACHE_TTL = 300.0
# Cap the MSI download so a wrong/huge asset can't fill the disk; the real MSI is ~25 MB.
_MSI_MAX_BYTES = 200 * 1024 * 1024

# Path fragments that mark where a package install physically lives. ``pipx`` gives each
# app its own venv under ``<PIPX_HOME>/venvs/<app>``; Homebrew formulae (and their bundled
# Python) live under the brew prefix — ``…/Cellar/…`` on every platform, plus the
# ``homebrew``/``linuxbrew`` prefixes. Matched against the *resolved* install path.
_PIPX_MARKER = f"{os.sep}pipx{os.sep}venvs{os.sep}"
_HOMEBREW_MARKERS = (f"{os.sep}Cellar{os.sep}", f"{os.sep}homebrew{os.sep}", f"{os.sep}linuxbrew{os.sep}")

# A short network timeout so a startup check never hangs the terminal. The
# periodic in-session check runs on a worker thread, but keep it bounded anyway.
_NET_TIMEOUT = 20
# The startup check blocks launch, so bound it much tighter: an offline user waits
# at most this long before aGiTrack starts anyway.
STARTUP_NET_TIMEOUT = 6

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
    restart_only: bool = False  # code is already current on disk; only a restart is needed
    # The update was DELIBERATELY not applied for a condition that is the user's own and clears
    # itself — today only a source checkout with uncommitted changes. Distinct from `error`
    # meaning "this went wrong": nothing is broken, nothing needs installing, and the next run
    # over a clean tree just works. Callers must not turn this into a "you must update it
    # yourself" prompt (see selfupdate.STATE_DEFERRED).
    deferred: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def _git(args: list[str], cwd: Path, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    # A network git call (fetch) must never block on a credential prompt — offline/auth
    # failures should fail fast, not hang launch. GIT_TERMINAL_PROMPT=0 covers git's own
    # (HTTP) prompts; SSH remotes ignore it, so also force the ssh transport into batch
    # mode — otherwise a passphrase-protected key (not in the agent) or an unknown host
    # key makes `ssh` wait for input the launch-time check can't answer, and the user is
    # left staring at a hung start until they Ctrl-C. A timeout bounds the rest; on expiry
    # the process is killed and we report a plain non-zero result so callers degrade to
    # "couldn't check" gracefully.
    ssh_cmd = os.environ.get("GIT_SSH_COMMAND") or "ssh"
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": f"{ssh_cmd} -oBatchMode=yes",
    }
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            **UTF8_TEXT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env=env,
            # The background daemon's periodic update check (git fetch + rev-parse) runs from a
            # console-less detached process; without this each git call flashes a console window
            # on Windows (proc.py).
            **console_isolation_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], returncode=124, stdout="", stderr="timed out")


def detect_source_repo() -> Path | None:
    """Return the top level of aGiTrack's own source checkout when the running code
    is imported from one (an editable / source-linked install), else ``None``.

    We trust an install as source-linked only when the directory containing the
    ``agitrack`` package both is a git work tree *and* carries aGiTrack's own
    ``pyproject.toml``. We key off the console-script entry point
    (``agitrack.cli:main``) rather than the distribution ``name`` so the check keeps
    working if the published name changes. That avoids mistaking, say, a wheel
    that happens to sit under some unrelated repository for the real source.
    """
    try:
        module_file = Path(agitrack.__file__).resolve()
    except (AttributeError, TypeError):
        return None
    root = module_file.parent.parent  # .../<repo>/agitrack/__init__.py -> <repo>
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if "agitrack.cli:main" not in text:
        return None
    result = _git(["rev-parse", "--show-toplevel"], root)
    if result.returncode != 0:
        return None
    return fs_path(result.stdout.strip())  # git printed UTF-8; see proc.fs_path


class Updater:
    """Detects how aGiTrack is installed and performs in-place updates."""

    def __init__(self, *, source_repo: Path | None | object = _DETECT) -> None:
        # source_repo is injectable for tests; production auto-detects it. Passing
        # an explicit None forces the package path (no source checkout).
        self._source_repo: Path | None = (
            detect_source_repo() if source_repo is _DETECT else cast("Path | None", source_repo)
        )
        # The source HEAD the running process was loaded from. Snapshotted HERE, at
        # construction (≈ process start), not on the first successful check: a check
        # gated on a network fetch could miss a purely-local update — offline, or the
        # checkout advancing before the first check — and leave the running process
        # unaware it is stale. Reading HEAD needs no network and is independent of the
        # upstream/fetch outcome. After the checkout moves on under a still-running
        # process (a self-update, a manual pull, or, in self-development, the source
        # advancing), this lets us see the running code is older than disk and prompt
        # a restart instead of reporting "up to date" off the checkout alone.
        self._running_rev: str | None = None
        if self._source_repo is not None:
            head = _git(["rev-parse", "HEAD"], self._source_repo)
            if head.returncode == 0:
                self._running_rev = head.stdout.strip() or None
        # MSI-path scratch state, populated by _check_msi/_apply_msi (see METHOD_MSI):
        self._msi_asset_url: str | None = None
        self._msi_asset_name: str | None = None
        self._msi_asset_digest: str | None = None  # "sha256:<hex>" when GitHub provides it
        self._msi_latest: str = ""
        # The downloaded MSI awaiting the elevated install hand-off (the runner reads this).
        self.pending_msi_path: str | None = None
        # Windows package installs only: the ``pip install --upgrade`` command that must run
        # AFTER this process exits (the OS locks the running ``agitrack.exe``, so pip can't
        # replace it in place). Set by _apply_package; callers spawn launch_pip_bootstrapper.
        self.pending_pip_upgrade: list[str] | None = None

    @property
    def kind(self) -> str:
        if self._source_repo is not None:
            return KIND_SOURCE
        return KIND_PACKAGE

    @property
    def source_repo(self) -> Path | None:
        return self._source_repo

    # --- checking --------------------------------------------------------

    def check(self, *, fetch: bool = True, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        """Check whether a newer aGiTrack is available. Blocking (network); run on a
        worker thread from interactive contexts. ``timeout`` bounds each network
        call — pass a short value (``STARTUP_NET_TIMEOUT``) for the launch-time
        check so an offline user isn't made to wait."""
        if self.kind == KIND_SOURCE:
            return self._check_source(fetch=fetch, timeout=timeout)
        if self._install_method() == METHOD_MSI:
            return self._check_msi(timeout=timeout)
        return self._check_package(timeout=timeout)

    def _upstream_ref(self, repo: Path) -> str | None:
        # The current branch's configured upstream (e.g. "origin/main").
        result = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
        if result.returncode != 0:
            return None
        ref = result.stdout.strip()
        return ref or None

    def _remote_target(self, repo: Path) -> str | None:
        # The remote-tracking ref to compare/merge against. Prefer the current
        # branch's configured upstream; when the branch has none — aGiTrack commonly
        # runs on a session worktree branch (``agitrack/...``) that tracks nothing —
        # fall back to the default branch of ``origin`` so the source still
        # follows upstream aGiTrack releases. Returns None only when neither exists.
        upstream = self._upstream_ref(repo)
        if upstream is not None:
            return upstream
        head = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], repo)
        if head.returncode == 0 and head.stdout.strip():
            return head.stdout.strip()
        for candidate in ("origin/main", "origin/master"):
            if _git(["rev-parse", "--verify", "--quiet", candidate], repo).returncode == 0:
                return candidate
        return None

    def _count(self, repo: Path, rev_range: str) -> int:
        # Number of commits in ``rev_range`` (e.g. "HEAD..origin/main"); 0 on error.
        result = _git(["rev-list", "--count", rev_range], repo)
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or "0")

    def _check_source(self, *, fetch: bool, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        repo = self._source_repo
        assert repo is not None
        status = UpdateStatus(kind=KIND_SOURCE)
        # Three commit hashes drive the decision (per the source-install update
        # contract): the RUNNING process's rev, the LOCAL checkout's HEAD, and the
        # REMOTE target's tip. If either the local disk or the remote carries code
        # the running process lacks, an update is available.
        head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        if self._running_rev is None:  # construction snapshot failed: fall back to now
            self._running_rev = head or None
        running = self._running_rev
        head_short = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        status.current = head_short

        # The remote side (needs the network). A missing target or a failed fetch is
        # remembered, not fatal — local staleness below is reported either way.
        target = self._remote_target(repo)
        fetch_error: str | None = None
        remote_head: str | None = None
        if target is not None:
            remote = target.split("/", 1)[0]
            if fetch:
                fetched = _git(["fetch", "--quiet", remote], repo, timeout=timeout)
                if fetched.returncode != 0:
                    fetch_error = (fetched.stderr.strip() or "git fetch failed").splitlines()[-1]
            if fetch_error is None:
                resolved = _git(["rev-parse", "--verify", "--quiet", target], repo)
                remote_head = resolved.stdout.strip() or None

        # Remote ahead of the local checkout: upstream commits the checkout lacks —
        # a real update (fetch + merge) is needed.
        remote_ahead = self._count(repo, f"HEAD..{remote_head}") if remote_head else 0
        # Local checkout ahead of the running process: the disk already carries code
        # this process hasn't loaded (a prior self-update, a manual pull, or a session
        # integration) — only a restart is needed. Detected with NO network.
        local_ahead = 0
        if running and head and running != head:
            local_ahead = self._count(repo, f"{running}..HEAD") or 1  # differ ⇒ at least restart

        if remote_ahead > 0 and remote_head is not None:
            status.available = True
            status.behind = remote_ahead
            status.latest = remote_head[:7]  # short sha of the remote target's tip
            commits = "commit" if remote_ahead == 1 else "commits"
            status.message = f"aGiTrack update available: {remote_ahead} new {commits} on {target} ({status.current} → {status.latest})."
            return status
        if local_ahead > 0:
            # The checkout is already updated but this process still runs the old code.
            # Detected with no network, so an offline local update still prompts a
            # restart. Takes precedence over a fetch error — the user can act on it now.
            assert running is not None
            status.available = True
            status.restart_only = True
            status.current = running[:7]
            status.latest = head_short  # restarting loads the HEAD on disk
            status.message = (
                f"aGiTrack was updated on disk but the running copy is older "
                f"({status.current} → {status.latest}); restart to load it."
            )
            return status
        # Nothing newer locally or on the running side. If the remote couldn't be
        # checked, say why; otherwise we are genuinely current.
        if target is None:
            status.error = "no upstream branch is configured for the aGiTrack source checkout"
        elif fetch_error is not None:
            status.error = fetch_error
        else:
            status.message = "aGiTrack is up to date."
        return status

    def _check_package(self, *, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        installed = self._installed_version()
        status.current = installed
        latest = self._latest_package_version(timeout=timeout)
        if latest is None:
            status.error = "could not determine the latest published aGiTrack version"
            return status
        status.latest = latest
        running = self._running_version()
        index_newer = _version_tuple(latest) > _version_tuple(installed)
        running_stale = _version_tuple(installed) > _version_tuple(running)
        if index_newer:
            status.available = True
            status.message = f"aGiTrack update available: {installed} → {latest}."
        elif running_stale:
            # The package on disk was already upgraded (e.g. `pip install -U`) but
            # this process is still running the old version.
            status.available = True
            status.restart_only = True
            status.current = running
            status.message = f"aGiTrack {installed} is installed but the running copy is {running}; restart to load it."
        else:
            status.message = "aGiTrack is up to date."
        return status

    def _installed_version(self) -> str:
        try:
            from importlib import metadata

            return metadata.version(DIST_NAME)
        except Exception:
            return getattr(agitrack, "__version__", "0")

    def _running_version(self) -> str:
        # The version this process actually imported at startup. ``agitrack.__version__``
        # is read once at import, so after an in-place upgrade it still reflects the
        # OLD version while :meth:`_installed_version` reads the new one from disk.
        return getattr(agitrack, "__version__", "0")

    def _latest_package_version(self, *, timeout: int = _NET_TIMEOUT) -> str | None:
        # `pip index versions` is the most portable way to ask the configured
        # index without a hard dependency on a PyPI JSON client. It is marked
        # experimental but degrades gracefully: any failure (including a network
        # timeout) returns None and the caller reports "could not determine the
        # latest version". `pip index` only READS the index, so it is safe even on
        # an externally-managed (PEP 668) Python where `pip install` is refused.
        pip = self._pip_invocation()
        if pip is None:
            return None
        try:
            result = subprocess.run(
                [*pip, "index", "versions", DIST_NAME],
                **UTF8_TEXT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
                # Keep pip off a console on Windows (the background daemon's update check runs
                # console-less; without this it flashes a window each check). See proc.py.
                **console_isolation_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return None
        if result.returncode != 0:
            return None
        prefix = f"{DIST_NAME.lower()} ("
        for line in result.stdout.splitlines():
            line = line.strip()
            # e.g. "agitrack (1.2.3)" or "LATEST: 1.2.3"
            if line.upper().startswith("LATEST:"):
                return line.split(":", 1)[1].strip() or None
            if line.lower().startswith(prefix) and line.endswith(")"):
                return line[line.index("(") + 1 : -1].strip() or None
        return None

    # --- applying --------------------------------------------------------

    def apply(self) -> UpdateStatus:
        """Install the available update in place. Blocking. Returns a status
        whose ``available`` is False on success and ``error`` set on failure.

        Never raises: any unexpected failure (a subprocess timeout, an OS error,
        a git/pip crash) is caught and returned as an ``error`` status that already
        carries manual-update instructions, so a failed update can never take down
        the running aGiTrack — the user keeps using the current version."""
        try:
            if self.kind == KIND_SOURCE:
                return self._apply_source()
            if self._install_method() == METHOD_MSI:
                return self._apply_msi()
            return self._apply_package()
        except Exception as error:  # an update attempt must not crash aGiTrack
            status = UpdateStatus(kind=self.kind)
            status.error = f"automatic update failed ({error}); {self.manual_update_instructions()}"
            return status

    def manual_update_instructions(self) -> str:
        """How to update aGiTrack by hand — shown when an automatic update is
        impossible or failed, so the user can finish the upgrade themselves while
        continuing to run the current version."""
        if self.kind == KIND_SOURCE and self._source_repo is not None:
            return f"update manually by running `git pull` in the aGiTrack source checkout at {self._source_repo}"
        if self._install_method() == METHOD_MSI:
            repo = self._github_repo()
            return (
                f"download the latest aGiTrack MSI from https://github.com/{repo}/releases/latest and run it "
                "(the installer is not code-signed yet, so if Windows SmartScreen warns, choose "
                '"More info" then "Run anyway")'
            )
        return self._manual_routes()

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
            # DEFERRED, not failed. On the aGiTrack checkout itself this is the normal state for
            # most of the day — the repo is where the work happens, and aGiTrack commits between
            # turns rather than continuously — so reporting it as a failed update made the
            # dashboard say "a newer aGiTrack is available and has to be installed by you" about
            # a checkout that needed nothing but the user's own commit, and flap on and off as
            # the tree went dirty and clean.
            status.deferred = True
            status.error = (
                "the aGiTrack source checkout has uncommitted changes; "
                "update skipped (it will apply once the tree is clean)"
            )
            return status
        target = self._remote_target(repo)
        if target is None:
            status.error = "no upstream branch is configured for the aGiTrack source checkout"
            return status
        # Refresh the target ref so we merge the very latest commit, even if the
        # periodic check ran a while ago.
        remote = target.split("/", 1)[0]
        fetched = _git(["fetch", "--quiet", remote], repo, timeout=_NET_TIMEOUT)
        if fetched.returncode != 0:
            status.error = (fetched.stderr.strip() or "git fetch failed").splitlines()[-1]
            return status
        # Merge the upstream code into the checkout: a clean checkout fast-forwards;
        # a diverged one (the user's own commits — aGiTrack's session branches accumulate
        # them) gets a normal merge. Only a genuine content CONFLICT blocks the update.
        # On conflict we abort so the running source is left clean (no conflict
        # markers, local work intact) rather than half-merged.
        merged = _git(["merge", "--no-edit", target], repo, timeout=_NET_TIMEOUT)
        if merged.returncode != 0:
            _git(["merge", "--abort"], repo)
            status.error = (
                "automatic update is impossible due to merge conflicts between the "
                f"local aGiTrack source and {target}; resolve them manually, then update"
            )
            return status
        status.current = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        status.latest = status.current
        status.message = f"Updated aGiTrack source checkout to {status.current}."
        return status

    def _install_method(self) -> str:
        """Identify the manager that owns this *package* install: ``METHOD_PIPX``,
        ``METHOD_BREW_PYTHON``, or ``METHOD_PIP``. Only consulted for the PEP 668
        fallback (to decide whether ``brew upgrade`` is the right hand-off); the
        primary upgrade path is pip and doesn't need it.

        Decided from where the running code physically lives (the resolved
        ``agitrack`` package path, with ``sys.prefix`` as a backstop), so it works
        without consulting any package metadata. pipx is checked before Homebrew:
        a pipx venv created by a brew-installed pipx still lives under
        ``…/pipx/venvs/…``, not under the brew Cellar, so the pipx marker is the
        more specific signal — and a pipx venv isn't externally managed, so it must
        not be mistaken for a Homebrew install and pushed at ``brew``.

        The MSI bundle is detected first: a frozen (PyInstaller) build whose perMachine
        install root the WiX fragment recorded in the registry. Both conditions are
        required so a portable PyInstaller zip (frozen, but no registry key) still falls
        through to the pip path rather than trying to drive ``msiexec`` against a release
        it didn't come from.
        """
        if sys.platform == "win32" and getattr(sys, "frozen", False) and self._registry_install_dir() is not None:
            return METHOD_MSI
        candidates: list[str] = []
        try:
            candidates.append(str(Path(agitrack.__file__).resolve()))
        except (AttributeError, TypeError):
            pass
        candidates.append(str(Path(sys.prefix).resolve()))
        blob = "\n".join(candidates)
        if _PIPX_MARKER in blob:
            return METHOD_PIPX
        if any(marker in blob for marker in _HOMEBREW_MARKERS):
            return METHOD_BREW_PYTHON
        return METHOD_PIP

    # --- MSI (frozen Windows bundle) -------------------------------------

    def _registry_install_dir(self) -> str | None:
        """The perMachine MSI install root from ``HKLM\\Software\\aGiTrack\\InstallDir``
        (written by ``installer/agitrack.wxs``), or ``None`` when absent / off Windows."""
        if sys.platform != "win32":
            return None
        try:
            import winreg  # type: ignore[import-not-found,unused-ignore]  # Windows-only stdlib
        except ImportError:
            return None
        for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\aGiTrack", 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "InstallDir")
            except OSError:
                continue
            if value:
                return str(value)
        return None

    def msi_install_dir(self) -> str:
        """The directory the MSI installed aGiTrack into — where the bootstrapper and the
        replacement ``agitrack.exe`` live. Prefers the registry value, falling back to the
        running executable's directory so a hand-off still works if the key is missing."""
        registry = self._registry_install_dir()
        if registry and os.path.isdir(registry):
            return registry
        return os.path.dirname(os.path.abspath(sys.executable))

    def _github_repo(self) -> str:
        """The ``owner/name`` slug to query for releases. An MSI install carries no source
        checkout, so unlike the source path there is no meaningful local git remote (the cwd
        is the *user's* project, not aGiTrack's) — default to the known upstream repo, with an
        ``AGITRACK_GH_REPO`` override for forks/testing, and a source remote when available."""
        override = os.environ.get("AGITRACK_GH_REPO")
        if override:
            return override.strip()
        if self._source_repo is not None:
            result = _git(["config", "--get", "remote.origin.url"], self._source_repo)
            slug = _github_slug(result.stdout.strip()) if result.returncode == 0 else None
            if slug:
                return slug
        return _DEFAULT_GH_REPO

    def _github_get_json(self, url: str, *, timeout: int) -> object:
        """GET *url* from the GitHub API as parsed JSON, with a short-TTL + ETag cache so the
        periodic check stays well under the anonymous rate limit. Raises on a hard failure."""
        import json
        import time
        import urllib.error
        import urllib.request

        now = time.monotonic()
        cached = _GH_CACHE.get(url)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agitrack-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if cached is not None:
            etag, payload, ts = cached
            if now - ts < _GH_CACHE_TTL:
                return payload
            if etag:
                headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https GitHub API host
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
                etag = response.headers.get("ETag", "")
                payload = json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 304 and cached is not None:  # Not Modified: reuse the cached body
                _GH_CACHE[url] = (cached[0], cached[1], now)
                return cached[1]
            raise
        _GH_CACHE[url] = (etag, payload, now)
        return payload

    def _latest_msi_release(self, *, timeout: int) -> tuple[str | None, str | None, str | None, str | None]:
        """``(version, download_url, asset_name, digest)`` for the newest release's
        ``agitrack-*-windows-x64.msi`` asset, or all-``None`` when none is published."""
        repo = self._github_repo()
        data = self._github_get_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout=timeout)
        assets = data.get("assets", []) if isinstance(data, dict) else []
        prefix, suffix = "agitrack-", "-windows-x64.msi"
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name", ""))
            if name.startswith(prefix) and name.endswith(suffix):
                version = name[len(prefix) : -len(suffix)]
                digest = asset.get("digest")  # e.g. "sha256:abc…" (newer GitHub API)
                return version, asset.get("browser_download_url"), name, digest
        return None, None, None, None

    def _check_msi(self, *, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        installed = self._installed_version()
        status.current = installed
        try:
            version, url, name, digest = self._latest_msi_release(timeout=timeout)
        except Exception as error:  # network / API / parse failure: report, don't crash
            status.error = f"could not check for aGiTrack MSI updates ({error})"
            return status
        if version is None or not url:
            status.error = "could not find a Windows MSI asset in the latest aGiTrack release"
            return status
        status.latest = version
        self._msi_asset_url = url
        self._msi_asset_name = name
        self._msi_asset_digest = digest
        self._msi_latest = version
        if _version_tuple(version) > _version_tuple(installed):
            status.available = True
            status.message = f"aGiTrack update available: {installed} → {version}."
        else:
            status.message = "aGiTrack is up to date."
        return status

    def _download(self, url: str, dest: Path, *, timeout: int, digest: str | None = None) -> None:
        """Stream *url* to *dest* with a size cap, verifying *digest* (``sha256:<hex>``)
        when GitHub supplies one. Raises on any failure (the partial file is removed)."""
        import hashlib
        import urllib.request

        hasher = hashlib.sha256()
        total = 0
        request = urllib.request.Request(url, headers={"User-Agent": "agitrack-updater"})  # noqa: S310 - GitHub asset URL
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as out:  # noqa: S310
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MSI_MAX_BYTES:
                        raise RuntimeError("MSI download exceeds the size cap")
                    hasher.update(chunk)
                    out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        if digest and digest.lower().startswith("sha256:"):
            expected = digest.split(":", 1)[1].strip().lower()
            if hasher.hexdigest() != expected:
                dest.unlink(missing_ok=True)
                raise RuntimeError("downloaded MSI failed its sha256 checksum")

    def _apply_msi(self) -> UpdateStatus:
        """Download the newest MSI to ``%TEMP%`` and stash its path for the runner's elevated
        hand-off. Does NOT run the installer (the running ``agitrack.exe`` is the very file the
        MSI replaces, so the install must happen after this process exits — see the runner's
        ``_launch_msi_bootstrapper``)."""
        import tempfile

        status = UpdateStatus(kind=KIND_PACKAGE)
        if not self._msi_asset_url:
            # apply() may be called without a preceding check() (e.g. on-demand); resolve now.
            check = self._check_msi()
            if not check.ok:
                status.error = check.error
                return status
        url = self._msi_asset_url
        if not url:
            status.error = f"no aGiTrack MSI asset found to download; {self.manual_update_instructions()}"
            return status
        name = self._msi_asset_name or "agitrack-update.msi"
        dest = Path(tempfile.gettempdir()) / name
        try:
            self._download(url, dest, timeout=600, digest=self._msi_asset_digest)
        except Exception as error:
            status.error = f"failed to download the aGiTrack MSI ({error}); {self.manual_update_instructions()}"
            return status
        self.pending_msi_path = str(dest)
        status.current = self._installed_version()
        status.latest = self._msi_latest or status.current
        status.message = f"Downloaded aGiTrack {status.latest}.".strip()
        return status

    def msi_last_args_path(self) -> str | None:
        """Per-user file (no UAC, survives reboots) recording this launch's argv so the MSI
        relauncher can restart with the same flags. ``None`` when LOCALAPPDATA is unset."""
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        return os.path.join(local, "aGiTrack", "last-args.txt")

    def launch_msi_bootstrapper(self, extra_args: Sequence[str] = ()) -> bool:
        """Windows MSI build only. Hand the MSI downloaded by :meth:`_apply_msi`
        (:attr:`pending_msi_path`) off to the elevated installer and arrange a de-elevated
        relaunch — the running ``agitrack.exe`` is the very file the MSI replaces, so the
        install must happen AFTER this process exits. Shared by the startup path and the
        runner's in-session teardown so both install MSI updates the same way.

        Two cooperating processes keep the updated aGiTrack at the user's NORMAL integrity
        level (not the installer's admin token): an elevated bootstrapper
        (``agitrack-update.cmd`` via UAC ``runas``) waits for this PID, runs ``msiexec``, and
        writes its exit code to a marker file; a non-elevated relauncher (spawned here) waits
        for that marker and, on success, starts the freshly installed ``agitrack.exe`` with the
        recorded args (this launch's argv plus ``extra_args``, e.g. ``--skip-privacy-ack``).

        Returns True when the elevated install was started (the caller MUST exit), False when
        it couldn't be (e.g. UAC declined) so the caller keeps running the current version."""
        from agitrack.proc import detach_kwargs, shell_execute_runas

        msi = self.pending_msi_path
        if not msi or sys.platform != "win32":
            return False
        install_dir = self.msi_install_dir()
        bootstrapper = os.path.join(install_dir, "agitrack-update.cmd")
        exe = os.path.join(install_dir, "agitrack.exe")
        # Record argv (+ extra_args, de-duplicated) so the relauncher restarts with the same
        # flags. last_args stays the PATH: on a write failure the relauncher falls back to
        # whatever is already there (e.g. a launch-time write), or to no args.
        last_args = self.msi_last_args_path() or ""
        if last_args:
            args = list(sys.argv[1:])
            for arg in extra_args:
                if arg not in args:
                    args.append(arg)
            try:
                os.makedirs(os.path.dirname(last_args), exist_ok=True)
                with open(last_args, "w", encoding="utf-8") as handle:
                    handle.write(subprocess.list2cmdline(args))
            except OSError:
                pass
        local = os.environ.get("LOCALAPPDATA", install_dir)
        marker = os.path.join(local, "aGiTrack", "update-result.txt")
        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
        except OSError:
            pass
        # Clear any stale marker so the relauncher only acts on THIS install's result.
        try:
            os.remove(marker)
        except OSError:
            pass
        # Elevated install: wait for our PID, run msiexec, write the result code to the marker.
        # cmd /c quoting rule: wrap the whole command line in one extra pair of quotes when any
        # token is quoted, so the leading "" is stripped and the inner quotes survive.
        inner = f'"{bootstrapper}" "{msi}" {os.getpid()} "{marker}"'
        try:
            shell_execute_runas("cmd.exe", f'/c "{inner}"')
        except Exception:  # UAC declined or launch failed: keep the current version
            return False
        # Non-elevated relauncher: wait for the marker, then start the new build de-elevated.
        relauncher = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$m={_ps_single_quote(marker)};$exe={_ps_single_quote(exe)};$af={_ps_single_quote(last_args)};"
            "$deadline=(Get-Date).AddMinutes(10);"
            "while(-not (Test-Path $m) -and (Get-Date) -lt $deadline){Start-Sleep -Seconds 1};"
            "if(-not (Test-Path $m)){exit};"
            "$rc=((Get-Content $m -TotalCount 1) -join '').Trim();"
            "if($rc -ne '0'){exit};"
            "$a='';if(Test-Path $af){$a=((Get-Content $af -TotalCount 1) -join '').Trim()};"
            "if($a){Start-Process -FilePath $exe -ArgumentList $a}else{Start-Process -FilePath $exe}"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", relauncher],
                **detach_kwargs(),
            )
        except OSError:
            pass  # the install still proceeds; the user just restarts aGiTrack manually
        return True

    def _pip_invocation(self) -> list[str] | None:
        """The command prefix for a pip call, or ``None`` when no pip can upgrade THIS install.

        Prefers the *running* interpreter's own pip (``<python> -m pip``) so the SAME environment
        aGiTrack is imported from is the one upgraded.

        The fallback to a ``pip3``/``pip`` on ``PATH`` is deliberately NOT taken inside a virtual
        environment. It was, on the reasoning that a loose match beats offering no update at all,
        and the result was worse than no update: a uv-created venv has no ``pip`` module, so a
        tracker running from one reached for Homebrew's ``pip3``, which refused under PEP 668 —
        and had it succeeded it would have upgraded HOMEBREW's site-packages while the venv
        aGiTrack actually runs from stayed where it was. The daemon then published "this Python
        is externally managed; run `pip install --upgrade agitrack`" for every dashboard on the
        machine to display: a diagnosis about the wrong interpreter and a command that would not
        have updated the install it was complaining about. Outside a venv a PATH pip plausibly IS
        this interpreter's, so the fallback still stands there."""
        if self._has_module_pip(sys.executable):
            return [sys.executable, "-m", "pip"]
        if _in_virtualenv():
            return None
        for name in ("pip3", "pip"):
            found = shutil.which(name)
            if found:
                return [found]
        return None

    def _has_module_pip(self, python: str) -> bool:
        try:
            result = subprocess.run(
                [python, "-m", "pip", "--version"],
                **UTF8_TEXT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_NET_TIMEOUT,
                # Reached from the daemons' periodic self-update check, which runs console-less;
                # without this each check flashes a console window (proc.py).
                **console_isolation_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _apply_package(self) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        # 1) Primary path — deliberately package-MANAGER-INDEPENDENT. Upgrade with the
        # running interpreter's OWN pip (``<python> -m pip``). This is the one mechanism
        # that works the same regardless of how the wheel was put there: a plain
        # ``pip install``, a venv, a ``--user`` install, and a pipx venv (pipx venvs
        # ship their own pip) all upgrade through it without invoking pipx/brew/apt. The
        # sole case it CAN'T handle is an externally-managed (PEP 668) Python — Homebrew's
        # or a distro's — where pip refuses by design; that falls through to (2).
        pip = self._pip_invocation()
        pep668 = False
        if pip is not None:
            upgrade_cmd = [*pip, "install", "--upgrade", DIST_NAME]
            # Windows: the OS locks the image of the running ``agitrack.exe`` console script,
            # so pip CANNOT replace it in place. Its upgrade uninstalls the old distribution
            # first — it deletes the unlocked package files (the whole ``agitrack/metrics``
            # subpackage among them), then fails on the locked exe with "check the
            # permissions", leaving the install half-removed and the next lazy import
            # crashing. So don't run pip here: record the command and defer it to a detached
            # helper (launch_pip_bootstrapper) that the caller spawns; it waits for this
            # process to exit (releasing the lock), runs the upgrade, then relaunches aGiTrack.
            if sys.platform == "win32":
                self.pending_pip_upgrade = upgrade_cmd
                status.message = "aGiTrack will finish updating after it exits."
                return status
            # Detach the upgrade into its own session (`start_new_session`). `pip
            # install --upgrade` uninstalls the old version before writing the new,
            # so an interruption between the two leaves aGiTrack UNINSTALLED. Running
            # it in a new session means a terminal-close SIGHUP (the user quitting
            # VS Code / closing the window mid-upgrade) is NOT delivered to pip, so it
            # runs to completion and the package is never left half-removed.
            result = subprocess.run(
                upgrade_cmd,
                **UTF8_TEXT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=600,
                **detach_kwargs(),
            )
            if result.returncode == 0:
                return self._package_upgraded(status)
            combined = f"{result.stderr}\n{result.stdout}"
            pep668 = "externally-managed-environment" in combined or "externally managed" in combined
            if not pep668:
                status.error = (result.stderr.strip() or result.stdout.strip() or "upgrade failed").splitlines()[-1]
                return status

        # 2) pip is unavailable or refused (PEP 668). There is NO automatic hand-off from here.
        #
        # This used to run `brew upgrade agitrack` whenever the install looked Homebrew-owned,
        # on the stated belief that "Homebrew is the only such manager that ships aGiTrack". It
        # does not: there is no `agitrack` formula or cask (checked against formulae.brew.sh), so
        # that command could only ever fail — and it fired on the COMMON macOS setup rather than
        # a rare one, because the detection matches a Homebrew-managed PYTHON
        # (…/opt/homebrew/lib/pythonX/site-packages/agitrack), which is exactly what the README's
        # `brew install python` + `pip3 install agitrack` produces. The user was then told to run
        # a command that fails, about a package manager that never owned their install.
        #
        # What that state really means is "pip cannot upgrade this in place", and the answer is
        # pipx — which the guidance below now says, in one line.

        # 3) No automatic path worked — enumerate every supported manual route so the
        # user can finish the upgrade with whichever tool installed aGiTrack.
        status.error = self._manual_upgrade_guidance(pep668=pep668)
        return status

    def _package_upgraded(self, status: UpdateStatus) -> UpdateStatus:
        status.current = self._installed_version()
        status.latest = status.current
        status.message = f"Updated aGiTrack package to {status.current}."
        return status

    def launch_pip_bootstrapper(self, extra_args: Sequence[str] = ()) -> bool:
        """Windows package install only. Spawn a detached helper that finishes the deferred
        pip upgrade recorded in :attr:`pending_pip_upgrade` AFTER this process exits.

        The running ``agitrack.exe`` console script is locked by the OS while we run, so the
        upgrade can't happen in place (see _apply_package). The helper — a hidden, detached
        PowerShell process that outlives us — waits for this PID to exit (releasing the lock),
        runs ``pip install --upgrade``, and on success relaunches aGiTrack with the original
        arguments (plus ``extra_args``, e.g. ``--skip-privacy-ack`` for a menu-triggered
        update). If the process is still alive after the wait, it bails WITHOUT upgrading so a
        stuck parent can't be corrupted the same way an in-place upgrade would.

        Returns True when the helper was spawned (the caller MUST then exit so the upgrade can
        proceed), False when there is nothing to do or the spawn failed (keep the current
        version)."""
        upgrade = self.pending_pip_upgrade
        if not upgrade or sys.platform != "win32":
            return False
        relaunch = _restart_command(extra_args)
        pip_call = " ".join(_ps_single_quote(part) for part in upgrade)
        relaunch_exe = _ps_single_quote(relaunch[0])
        relaunch_args = ",".join(_ps_single_quote(part) for part in relaunch[1:])
        arglist = f" -ArgumentList @({relaunch_args})" if relaunch[1:] else ""
        # Poll up to ~5 min for the parent to exit (600 × 500ms); bail if it never does so a
        # hung aGiTrack is never upgraded out from under itself. Then run pip and, on success,
        # relaunch. $ErrorActionPreference keeps a missing-process probe from being noisy.
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$p={os.getpid()};"
            "for($i=0;$i -lt 600 -and (Get-Process -Id $p -ErrorAction SilentlyContinue);$i++)"
            "{Start-Sleep -Milliseconds 500};"
            "if(Get-Process -Id $p -ErrorAction SilentlyContinue){exit};"
            f"& {pip_call};"
            f"if($LASTEXITCODE -eq 0){{Start-Process -FilePath {relaunch_exe}{arglist}}}"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                **detach_kwargs(),
            )
        except OSError:
            return False
        return True

    def _manual_routes(self) -> str:
        """The ONE command that fits this install, not a menu of every possibility.

        This used to concatenate four routes — pip, pipx, Homebrew, PEP 668 — into a single
        run-on line, on the reasoning that aGiTrack "can't tell for certain which one applies".
        It can: `_install_method` already identifies the install well enough to drive the
        automatic upgrade, so it is good enough to name the manual one. The enumeration was
        also actively misleading, since one of the four (`brew upgrade agitrack`) refers to a
        package that does not exist. A user reading an update notice needs the command to run,
        not a decision to make.
        """
        method = self._install_method()
        if method == METHOD_PIPX:
            return f"update it with `pipx upgrade {DIST_NAME}`"
        if method == METHOD_BREW_PYTHON:
            # Homebrew's Python is externally managed (PEP 668), so pip will not upgrade in
            # place. pipx is the supported way out — NOT `brew upgrade`, which has no formula
            # to act on. The --break-system-packages form is named second because it works but
            # writes into a Python the OS package manager owns.
            return (
                f"this Python is managed by Homebrew, so pip will not upgrade in place — "
                f"reinstall with `pipx install {DIST_NAME}`, or force it with "
                f"`pip install --upgrade --break-system-packages {DIST_NAME}`"
            )
        if _in_virtualenv() and not self._has_module_pip(sys.executable):
            # No pip in here, and a pip from outside would install somewhere else entirely. Only
            # the tool that built the venv can replace what is in it; uv is what builds a venv
            # without pip in the first place, so it is the one worth naming.
            return (
                f"this virtual environment ({sys.prefix}) has no pip, and a pip from outside it "
                f"would install somewhere else — upgrade it with the tool that created it, e.g. "
                f"`uv pip install --upgrade {DIST_NAME}` with that environment active"
            )
        return f"update it with `pip install --upgrade {DIST_NAME}`"

    def _manual_upgrade_guidance(self, *, pep668: bool) -> str:
        lead = (
            "this Python is externally managed (PEP 668), so pip won't upgrade aGiTrack in place"
            if pep668
            else "could not upgrade aGiTrack automatically"
        )
        return f"{lead}; {self._manual_routes()}"


def _in_virtualenv() -> bool:
    """Whether the running interpreter is a virtual environment (venv/virtualenv/uv). Its
    packages are private to it: a pip belonging to another interpreter installs into that other
    interpreter, never in here."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _ps_single_quote(value: str) -> str:
    """Quote *value* as a PowerShell single-quoted string literal (doubling embedded single
    quotes). Used to embed the pip command and relaunch argv in the deferred-upgrade helper."""
    return "'" + value.replace("'", "''") + "'"


def _github_slug(remote_url: str) -> str | None:
    """Parse ``owner/name`` from a GitHub remote URL (https or ssh), or ``None``."""
    url = remote_url.strip()
    if not url:
        return None
    for marker in ("github.com/", "github.com:"):
        if marker in url:
            slug = url.split(marker, 1)[1]
            if slug.endswith(".git"):
                slug = slug[:-4]
            slug = slug.strip("/")
            return slug if slug.count("/") == 1 else None
    return None


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


def _restart_command(extra_args: Sequence[str] = ()) -> list[str]:
    """The argv to re-launch the running aGiTrack with the original CLI arguments.

    A normal (pip/source) install is a Python interpreter, so it is re-run as
    ``python -P -m agitrack …`` — robust to a package upgrade that rewrote the console
    script, and to an ``agitrack`` directory in the working directory it is restarted in
    (``proc.agitrack_invocation``). A **frozen** build (the PyInstaller/MSI ``agitrack.exe``)
    is NOT a Python interpreter: ``-m agitrack`` is not a valid argument there and argparse
    would reject it, so the frozen executable is re-run directly with the saved arguments.
    """
    from agitrack.proc import agitrack_invocation

    args = list(sys.argv[1:])
    for arg in extra_args:
        if arg not in args:
            args.append(arg)
    return [*agitrack_invocation(), *args]


def restart_agitrack(extra_args: Sequence[str] = ()) -> NoReturn:
    """Re-launch aGiTrack so the freshly updated code is loaded.

    Uses ``python -m agitrack`` (or, for a frozen MSI build, the ``agitrack.exe`` directly —
    see :func:`_restart_command`) with the original CLI arguments. Does not return on success.

    ``extra_args`` are appended to the original argv (de-duplicated) — used to
    carry ``--skip-privacy-ack`` through a menu-triggered restart so the
    already-acknowledged privacy warning isn't shown again.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    # A restart means freshly updated code is on disk; the detached daemons (repo/backtrace
    # dashboards, background trackers) are still running the OLD version, so gracefully stop and
    # re-spawn them all now — they reload the new code just like this process is about to. The
    # current process restarts itself via the exec below, so it's excluded. Best-effort.
    try:
        from agitrack import daemons

        daemons.restart_all()
    except Exception:
        pass
    cmd = _restart_command(extra_args)
    if os.name == "nt":
        # Windows has no true in-place exec. os.execv there spawns a new process and exits
        # this one, which (a) fails with "[WinError 6] The handle is invalid" when the CRT
        # tries to inherit this process's now-torn-down console/socket handles, and (b) lets
        # the launching shell return to a prompt while the new TUI is orphaned on the same
        # console. Instead, launch the updated aGiTrack as a child sharing this console and
        # wait for it, propagating its exit code — so the launching shell keeps waiting on us
        # until the new instance finishes. close_fds (the subprocess default on Windows) keeps
        # the bad handles out of the child. Ignore Ctrl-C here so it reaches the child's TUI.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        child = subprocess.Popen(cmd)
        sys.exit(child.wait())
    os.execv(sys.executable, cmd)
