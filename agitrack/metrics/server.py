"""HTTP server for the live aGiTrack dashboard (#54).

`agitrack --dashboard` serves the HTML dashboard and opens it in the browser.
The page polls ``/data`` on an interval and re-renders, so the dashboard
reflects new commits as they land — useful for watching an agent work.
Everything derives from ``git log``: read-only, no state, identical on every clone. The
built view is cached per REF STATE (branch tip plus the latent manual refs), so a poll, a
log page or a return from /story or /learn reuses it instead of walking the history again;
a moved ref rebuilds it.

Where it binds depends on where aGiTrack is running. On your own machine it
stays on loopback; in a **remote shell** (SSH/Mosh) loopback would be reachable
only from the remote box itself, so it binds all interfaces instead and
advertises the remote's own IP — open it directly if the firewall allows, or
copy-paste the printed `ssh -L` command if it doesn't. Set
``AGITRACK_DASHBOARD_HOST`` to pin a bind address (``127.0.0.1`` opts back out).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Callable, TypeVar

from agitrack.git import GitRepo
from agitrack.git.repo import read_cache
from agitrack.proc import UTF8_TEXT
from agitrack.metrics import learn as learn_page
from agitrack.metrics import story as story_page
from agitrack.metrics.collect import Dashboard, build_dashboard, viewable_branches
from agitrack.metrics.files import FileBrowser, git_browser
from agitrack.metrics.insights import build_insights, context_from_browser
from agitrack.metrics import progress
from agitrack.metrics.github import cached_logins
from agitrack.metrics.routing import Response, html_response, json_response
from agitrack.metrics.web import (
    _filter_stats,
    parse_meta_filters,
    aggregates_payload,
    commit_diff,
    log_page,
    shared_sessions_for,
    shell_html,
)

DEFAULT_HOST = "127.0.0.1"
# Bind address used when aGiTrack runs in a remote shell: loopback there is reachable
# only from the remote box, which is never where the user's browser is.
ALL_INTERFACES = "0.0.0.0"
# Overrides the automatic choice, e.g. AGITRACK_DASHBOARD_HOST=127.0.0.1 to keep the
# dashboard off the network even over SSH, or a single NIC address to narrow it.
BIND_HOST_ENV = "AGITRACK_DASHBOARD_HOST"

DEFAULT_PORT = 8765
# Ports are handed out CONSECUTIVELY from the preferred one (8765, 8766, 8767, …) rather
# than falling straight to an OS-assigned ephemeral port: with several dashboards/backtraces
# up at once the URLs stay predictable and adjacent, so one `ssh -L` block covers them.
PORT_SCAN_SPAN = 32

_ServerT = TypeVar("_ServerT", bound=http.server.HTTPServer)


# Below this, compressing costs more than the bytes it saves on any link worth the name.
_GZIP_MIN_BYTES = 1024


def maybe_gzip(body: bytes, accept_encoding: str) -> tuple[bytes, str]:
    """``(body, content_encoding)`` — gzipped when the client accepts it and it is worth doing.

    The page and its JSON are highly compressible text (the ~90 KB shell shrinks about 5×), and
    over a remote or SSH-forwarded connection that transfer time is exactly the blank-screen wait
    the user sees before the dashboard appears. Level 6 is the usual size/CPU balance; the work
    happens on a background daemon, not in the TUI."""
    if len(body) < _GZIP_MIN_BYTES or "gzip" not in accept_encoding.lower():
        return body, ""
    import gzip

    return gzip.compress(body, 6), "gzip"


def _str(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _str(query, key)
    return int(raw) if raw.lstrip("-").isdigit() else default


class RepoScope:
    """One repository's LIVE dashboard: every route it answers, and the caches behind them.

    This used to be a ``BaseHTTPRequestHandler`` subclass with the repo and its caches bolted on
    as class attributes, one subclass per server. That worked exactly as long as there was one
    repository per process. Serving every repository from one hub means the same logic has to be
    selectable per request, so it lives here instead: a plain object that turns a path and a query
    into a :class:`Response`, with no socket anywhere in sight.

    The caches are per SCOPE (so two repositories never share a built dashboard) and keyed by
    what they were built from, so a poll that finds no new commits recomputes nothing.
    """

    def __init__(self, repo: GitRepo, *, email_logins: dict[str, str] | None = None, pending_source=None) -> None:
        self.repo = repo
        # How this scope answers "what does the backtrace know that I do not?" (see
        # agitrack/metrics/pending.py). The hub replaces it with one that reads an already-built
        # reconstruction when there is one, for an exact count instead of the cheap estimate.
        self._pending_source = pending_source
        self._pending_cache: tuple[float, dict] | None = None
        # Lowercased author email → GitHub login, supplementing `gh` for commits not yet pushed.
        self.email_logins: dict[str, str] = {k.lower(): v for k, v in (email_logins or {}).items()}
        # The file browser, keyed by (ref, head sha): building it scans `git log --numstat`, so it
        # is rebuilt only when the branch's tip moves, not per poll.
        self._browser_cache: dict[tuple[str, str], FileBrowser] = {}
        # Efficiency insights are scoped to the CURRENT FILTER (so a narrowed time range re-asks
        # the question for that slice), hence keyed by the filter too, not just the branch tip.
        # Bounded: cleared whenever the tip moves, and capped while a tip is current.
        self._insights_cache: dict[tuple, list[dict]] = {}
        # The built Dashboard itself, keyed by the refs it was built from (see _dashboard).
        self._dash_cache: dict[tuple, Dashboard] = {}
        # One build at a time per repository. The server is threaded, so a page fetching /data
        # while another tab (or the hub) asks for the same thing had every thread indexing the
        # SAME history in parallel: the full first-load cost paid twice over, and both writing
        # the same rows into the cache. Whoever arrives second waits for the first and then
        # finds the answer in the cache above. Re-entrant because the file browser's build asks
        # for the dashboard it belongs to.
        self._build_lock = threading.RLock()

    @property
    def root(self) -> "Path":
        return self.repo.repo

    @property
    def repo_name(self) -> str:
        return str(self.repo.repo).rstrip("/").rsplit("/", 1)[-1]

    # ----------------------------------------------------------------- routes

    def get(self, path: str, query: dict[str, list[str]]) -> "Response | None":
        """Answer a GET, or None when this scope has no such route (a 404 at the edge)."""
        with read_cache():
            return self._get(path, query)

    def _get(self, path: str, query: dict[str, list[str]]) -> "Response | None":
        """The routes themselves, under one read-cache scope (see :meth:`get`).

        A single request asks git the same cheap questions several times over — the branch list
        backs the selector, the ref validator AND the cache fingerprint, and the shown ref's tip
        is read by more than one of them. The scope is per REQUEST (and thread-local, which is
        what makes it safe under the threading server) and the dashboard only ever reads, so
        de-duplicating within it cannot serve a value the request itself invalidated."""
        author, backend, model = _str(query, "author"), _str(query, "backend"), _str(query, "model")
        # The advanced filter: repeated `meta=<key>:<op>:<value>` conditions over any recorded
        # metadata field (see web.parse_meta_filters).
        meta = parse_meta_filters(query.get("meta") or [])
        frm, to = _int(query, "from", 0), _int(query, "to", 0)
        ref = self._ref(_str(query, "branch"))
        if path in ("", "/", "/index.html"):
            # Paint the page chrome instantly with no aggregates/log embedded, then let the
            # browser fetch /data and /log behind a loading animation — so a repo with a huge
            # history doesn't block the first paint on the git-log crunch. Warming the login
            # cache here (a background refresh) means the resolved GitHub IDs are likely ready by
            # the time the first /data poll lands, so committers show as their IDs almost at once.
            cached_logins(self.repo, ref)
            return html_response(shell_html(self.repo))
        if path == "/progress":
            # How far along a build running on ANOTHER thread is (metrics.progress). Answered
            # without touching git or building anything: the whole point is that it stays
            # instant while the request it describes is still crunching the history.
            return json_response(progress.snapshot(self.root))
        if path == "/data":
            payload = aggregates_payload(
                self._dashboard(ref),
                author=author,
                backend=backend,
                model=model,
                meta=meta,
                frm=frm,
                to=to,
                granularity=_str(query, "granularity"),
            )
            payload["shared_sessions"] = shared_sessions_for(self.repo)
            payload["insights"] = self._insights(
                ref, author=author, backend=backend, model=model, meta=meta, frm=frm, to=to
            )
            payload["pending"] = self._pending()
            payload["empty_state"] = self._empty_state(self._dashboard(ref))
            return json_response(payload)
        if path == "/log":
            # Also published: on a repository being indexed for the first time this page's exact
            # line counts are fetched here, after /data has done the history, and the loading
            # screen is still up in front of it.
            with progress.publishing(self.root):
                return json_response(
                    log_page(
                        self._dashboard(ref),
                        repo=self.repo,
                        author=author,
                        backend=backend,
                        model=model,
                        meta=meta,
                        frm=frm,
                        to=to,
                        offset=_int(query, "offset", 0),
                        limit=_int(query, "limit", 50),
                        sort=_str(query, "sort"),
                    )
                )
        if path == "/diff":
            # This commit's file diffs, straight from the local clone — so the dashboard shows
            # changes without GitHub. The sha is validated as a hex id in commit_diff.
            return json_response(commit_diff(self.repo, _str(query, "sha")))
        if path == "/files":
            # The file browser: every changed file with its per-file change history and the
            # conversation/tokens behind each change (same view as --backtrace, real commits).
            return json_response({"files": self._browser(ref).files_payload()})
        if path == "/filelog":
            return json_response(self._browser(ref).file_log_payload(_str(query, "path")))
        if path == "/filediff":
            return json_response(self._browser(ref).file_diff(_str(query, "path"), _str(query, "sha")))
        if path == "/story":
            # The storyline: the backend agent tells this branch's history as chapters
            # (agitrack/metrics/story.py). Chrome only; the page fetches /story/state.
            return html_response(story_page.story_html(self.repo.repo))
        if path == "/story/state":
            stats, sha_paths = self._story_view(ref if ref != "HEAD" else "")
            return json_response(
                story_page.story_state(
                    self.repo.repo,
                    stats,
                    sha_paths,
                    branch=ref if ref != "HEAD" else self.repo.current_branch(),
                    branches=self._dashboard(ref).branches or viewable_branches(self.repo),
                    repo_name=self.repo_name,
                )
            )
        if path == "/learn":
            # The learning page: the backend agent coaches the user from their own interaction
            # traces (agitrack/metrics/learn.py). Chrome only; the page fetches /learn/state
            # after paint, like the dashboard shell.
            return html_response(learn_page.learn_html(self.repo.repo))
        if path == "/learn/state":
            # ``ref`` honours a ?branch= param (validated in _ref): the trace lives in commits, so
            # the committer list and trace count are branch-dependent.
            payload = learn_page.learn_state(self.repo.repo, self.repo)
            dash = self._dashboard(ref)
            payload["committers"] = sorted(dash.committers_with_lines())
            payload["branches"] = dash.branches or viewable_branches(self.repo)
            payload["branch"] = ref if ref != "HEAD" else self.repo.current_branch()
            payload["trace_turns"] = sum(1 for stat in dash.stats if stat.kind in learn_page._AI_KINDS)
            return json_response(payload)
        if path == "/learn/models":
            return json_response(learn_page.model_options(_str(query, "backend")))
        if path == "/state":
            # Whether aGiTrack is running on THIS repository, and in which mode. Read fresh on
            # every request (a handshake file and a pid check, no git), because the whole point
            # of the answer is that it changes while the page is open.
            from agitrack.proxy.background import running_mode
            from agitrack.versioning import version_line

            state = dict(running_mode(self.repo))
            # The version SERVING this request. The page compares it with the one it loaded
            # under and reloads on a change: aGiTrack self-updates and restarts its daemons
            # underneath an open dashboard, so a tab could sit for hours rendering with the old
            # page's JS against a new server — which is exactly how a fixed parsing bug appeared
            # to persist until the tab was reloaded by hand.
            # The same string `agitrack --version` prints (`versioning.version_line`), so a
            # source checkout's commit is part of the identity being compared: between two
            # releases every commit reports the same version, and it is precisely those
            # restarts a developer needs the open page to notice.
            state["agitrack_version"] = version_line()
            # The update notices, so a page that has been open for hours shows what is true NOW.
            # They are rendered into the HTML as well, for the first paint, but a marker can be
            # cleared or an update installed while the tab sits there: the banner is the one part
            # of the page whose whole job is to be current.
            from agitrack.metrics.web import update_notices

            state["update_notices"] = update_notices(self.repo)
            return json_response(state)
        return None

    def post(self, path: str, body: dict) -> "Response | None":
        """Answer a POST. All POST endpoints belong to the learning and story pages; agent
        failures come back as ``{"error": …}`` rather than a 500, so the page can show them
        in place."""
        if path.startswith("/story/"):
            payload = story_page.handle_story_post(
                path, body, root=self.repo.repo, view=self._story_view, repo_name=self.repo_name
            )
        else:
            payload = learn_page.handle_learn_post(
                path, body, root=self.repo.repo, repo=self.repo, view=self._learn_view
            )
        return None if payload is None else json_response(payload)

    # How long a pending-work answer is reused. The probe is a directory listing plus one
    # `git log`, which is cheap but not free, and this rides on every dashboard poll; the answer
    # only changes when someone commits or the agent runs, so a minute is imperceptible.
    _PENDING_TTL_SECONDS = 60.0

    def _pending(self) -> dict:
        """Agent work the backtrace can see and the tracked history cannot (see metrics.pending)."""
        import time as _time

        from agitrack.metrics.pending import notice_text, pending_work

        now = _time.monotonic()
        if self._pending_cache is not None and now - self._pending_cache[0] < self._PENDING_TTL_SECONDS:
            return self._pending_cache[1]
        work = self._pending_source() if self._pending_source is not None else pending_work(self.repo.repo)
        payload = {**work.to_dict(), "notice": notice_text(work)}
        self._pending_cache = (now, payload)
        return payload

    def _empty_state(self, dash: Dashboard) -> dict:
        """What to say when this dashboard has no agent work in it (see metrics.pending)."""
        from agitrack.metrics.pending import empty_state

        # "Tracked" here means the same thing the automatic view choice means by it: a commit
        # carrying a NON-ZERO token count. A repo whose only metadata is user-commit attribution
        # has a full coverage bar over an empty story, which is no better than an empty page.
        tracked = any(any(v for v in (stat.tokens or {}).values()) for stat in dash.stats)
        return empty_state(self.repo.repo, commits=len(dash.stats), tracked=tracked)

    # ----------------------------------------------------------------- views

    def _story_view(self, branch: str) -> tuple[list, dict]:
        """The commits (and which files each touched) the storyline is told from: this branch's
        whole history, unfiltered. The story is about the project, not about a dashboard filter,
        so no author/period narrowing applies here."""
        ref = self._ref(branch)
        dash = self._dashboard(ref)
        _files, sha_paths = context_from_browser(self._browser(ref), dash.stats)
        return dash.stats, sha_paths

    def _learn_view(self, author: str, frm: int, to: int, branch: str) -> tuple[list, list[dict], list[dict]]:
        """The filtered stats + insights + file rows the learning agent's digest is built from:
        exactly the same slice the dashboard would show for this filter. ``branch`` picks the ref
        the trace is read from (validated like the dashboard's selector)."""
        ref = self._ref(branch)
        dash = self._dashboard(ref)
        stats = _filter_stats(dash, author=author, backend="", model="", frm=frm, to=to)
        insights = self._insights(ref, author=author, frm=frm, to=to)
        return stats, insights, self._browser(ref).files_payload()

    def _ref(self, branch: str) -> str:
        # Only a branch this repo actually has may be viewed: an unchecked value would be
        # interpolated straight into ``git log <ref>``, so anything not in the branch list (an
        # option string, a bogus name, "") falls back to HEAD. `viewable_branches` is the SAME
        # list the selector is built from — including remote-tracking branches, which is how a
        # branch the user has fetched but not checked out becomes viewable — so the selector can
        # never offer something this refuses.
        return branch if branch and branch in viewable_branches(self.repo) else "HEAD"

    def _dashboard(self, ref: str = "HEAD") -> Dashboard:
        # cached_logins never blocks: it returns the cached GitHub identities (or {} when cold)
        # and refreshes them in the background, so polls stay fast. Resolved logins appear on a
        # later poll. {} when gh is absent. The initial / paint warms this cache so the IDs are
        # usually ready by the first /data poll.
        logins = cached_logins(self.repo, ref)
        # Reading the whole history is THE expensive thing this server does, and every request
        # used to do it again: a poll, a log page, the story page, and every return from /learn
        # or /story. It only changes when a ref moves, so key it on the tips we actually read
        # (the branch, and the manual-mode latent refs the pending rows come from) plus how many
        # identities are resolved so far.
        key = (ref, self._ref_state(ref), len(logins), len(self.email_logins))
        hit = self._dash_cache.get(key)
        if hit is not None:
            return hit
        with self._build_lock:
            hit = self._dash_cache.get(key)
            if hit is not None:
                return hit  # another thread built it while this one waited
            # The one place a request can block for minutes (a repository whose commits have
            # never been diffed), so it is the one place that says how far along it is.
            with progress.publishing(self.root):
                dash = build_dashboard(self.repo, ref, sha_logins=logins, email_logins=self.email_logins)
            self._dash_cache.clear()  # only the current state is worth keeping
            self._dash_cache[key] = dash
            return dash

    def _ref_state(self, ref: str) -> str:
        """A cheap fingerprint of everything the dashboard reads: the shown branch's tip, the
        latent (manual-mode) refs, and the NAMES of every branch. Two git plumbing calls instead
        of a full history walk.

        The branch names are part of it because the dashboard carries the branch SELECTOR, and a
        branch arriving does not move the branch being shown: fingerprinting the shown tip alone
        served the stale selector for as long as the current branch sat still, so a freshly
        pulled branch stayed invisible until the user happened to commit. Names, not tips — a
        branch merely MOVING changes nothing the selector shows, and rebuilding the whole
        history for it would be pure churn."""
        try:
            head = self.repo.rev_parse(ref)
        except Exception:
            return ""  # unreadable: fall through to a rebuild rather than serve a guess
        try:
            latent = self.repo._run(
                ["git", "for-each-ref", "--format=%(objectname)", "refs/agitrack/manual"], check=False
            ).stdout
        except Exception:
            latent = ""
        try:
            names = "\n".join(viewable_branches(self.repo))
        except Exception:
            names = ""
        return head + "|" + latent.strip() + "|" + names

    def _browser(self, ref: str = "HEAD") -> FileBrowser:
        # Build (and cache) the file browser for this ref. Keyed by the branch tip so a poll that
        # finds no new commits reuses it; only a new commit rebuilds the numstat index.
        dash = self._dashboard(ref)
        head = dash.stats[-1].sha if dash.stats else ""
        key = (ref, head)
        hit = self._browser_cache.get(key)
        if hit is None:
            with self._build_lock:
                hit = self._browser_cache.get(key)  # another thread may have just indexed it
                if hit is None:
                    with progress.publishing(self.root):
                        hit = git_browser(self.repo, dash.stats, ref)
                    self._browser_cache.clear()  # keep only the latest tip's — bounded memory
                    self._browser_cache[key] = hit
        return hit

    _INSIGHTS_CACHE_MAX = 16

    def _insights(
        self,
        ref: str = "HEAD",
        *,
        author: str = "",
        backend: str = "",
        model: str = "",
        meta: list[tuple[str, str, str]] | None = None,
        frm: int = 0,
        to: int = 0,
    ) -> list[dict]:
        # Insights for the FILTERED view: the same commits the rest of the page is showing.
        # Cached per (tip, filter) — a poll with unchanged filters reuses the result, and a new
        # commit invalidates every entry.
        dash = self._dashboard(ref)
        head = dash.stats[-1].sha if dash.stats else ""
        key = (ref, head, author, backend, model, tuple(meta or ()), frm, to)
        cache = self._insights_cache
        hit = cache.get(key)
        if hit is None:
            if cache and next(iter(cache))[:2] != (ref, head):
                cache.clear()  # the tip moved: every cached slice is stale
            elif len(cache) >= self._INSIGHTS_CACHE_MAX:
                cache.pop(next(iter(cache)))  # bound the per-tip filter variants
            stats = _filter_stats(dash, author=author, backend=backend, model=model, frm=frm, to=to, meta=meta)
            files, sha_paths = context_from_browser(self._browser(ref), stats)
            hit = build_insights(stats, files, sha_paths)
            cache[key] = hit
        return hit


def read_json_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    """The JSON object a POST carried, or ``{}``.

    A beacon flush (``navigator.sendBeacon``) may arrive without an ``application/json`` header,
    so the body is parsed regardless of content type; anything that is not a JSON object at all
    becomes an empty one rather than an exception on a page the user is still looking at."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return {}
    raw = handler.rfile.read(length) if 0 < length <= 1_000_000 else b""
    try:
        body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def write_response(handler: http.server.BaseHTTPRequestHandler, response: "Response | None") -> None:
    """Put a scope's :class:`Response` on the wire, or a 404 for ``None``."""
    if response is None:
        handler.send_error(404, "not found")
        return
    if response.status in (301, 302, 303, 307, 308):
        handler.send_response(response.status)
        for name, value in response.headers.items():
            handler.send_header(name, value)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    body, encoding = maybe_gzip(response.body, handler.headers.get("Accept-Encoding", ""))
    handler.send_response(response.status)
    handler.send_header("Content-Type", response.content_type)
    if encoding:
        handler.send_header("Content-Encoding", encoding)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", response.cache_control)
    for name, value in response.headers.items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    """The single-repository dashboard server: one scope, mounted at the root."""

    scope: RepoScope  # set on the per-server subclass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            parsed = urllib.parse.urlparse(self.path)
            write_response(self, self.scope.get(parsed.path, urllib.parse.parse_qs(parsed.query)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser closed the connection mid-response — a poll superseded by the next one,
            # a refresh, or a closed tab. Harmless; don't let http.server dump a traceback to the
            # console aGiTrack is running in.
            pass

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        try:
            parsed = urllib.parse.urlparse(self.path)
            write_response(self, self.scope.post(parsed.path, read_json_body(self)))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args: object) -> None:
        """Stay quiet: the dashboard is a foreground tool, not a web log."""


def bind_exclusively(sock: socket.socket) -> None:
    """Ask for a port NOBODY else is on, on either family of socket semantics.

    ``SO_REUSEADDR`` means two different things. On POSIX it only lets a new listener take a
    port left in TIME_WAIT, and a port a live listener holds is still refused. On Windows it
    means "bind even if another socket is already bound here", so two dashboards would both
    bind 8765, the port scan below would never step past a taken port, and requests would
    land on whichever socket the OS felt like. Windows spells the POSIX intent
    ``SO_EXCLUSIVEADDRUSE``, which additionally stops anyone stealing OUR port."""
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)


class _DashboardServer(http.server.ThreadingHTTPServer):
    # Threaded so one slow request (e.g. the first gh lookup) never blocks the
    # page; daemon threads so Ctrl-C exits immediately without joining them.
    daemon_threads = True

    # socketserver would set SO_REUSEADDR for us; on Windows that is the wrong request
    # entirely (see bind_exclusively), so take the option over completely.
    allow_reuse_address = False

    def server_bind(self) -> None:
        bind_exclusively(self.socket)
        super().server_bind()

    # A client that vanished mid-write surfaces as BrokenPipeError here too;
    # swallow it so the server doesn't print a traceback per dropped poll.
    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            super().handle_error(request, client_address)


def is_remote_session() -> bool:
    """Whether aGiTrack is running in a shell on a *remote* machine (SSH/Mosh).

    Only the SSH environment is trusted here: it is set by sshd itself for the login
    session and inherited by everything started from it, so it means "the terminal the
    user typed into lives elsewhere". A headless local box is deliberately NOT treated
    as remote — that would put the dashboard on the network for someone sitting at a
    console, which they never asked for."""
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"))


def default_bind_host() -> str:
    """The address the dashboard should listen on.

    Loopback locally; all interfaces in a remote shell, so the user can reach it from
    their own machine at the remote's IP (subject to firewall rules — see
    :func:`remote_access_help` for the SSH-forwarding fallback). ``AGITRACK_DASHBOARD_HOST``
    overrides both."""
    override = os.environ.get(BIND_HOST_ENV, "").strip()
    if override:
        return override
    return ALL_INTERFACES if is_remote_session() else DEFAULT_HOST


def _ssh_server_ip() -> str:
    """The address of THIS machine that the SSH client connected to.

    ``SSH_CONNECTION`` is ``<client-ip> <client-port> <server-ip> <server-port>``, so its
    third field is the one address we know is routable from the user's machine — better
    than guessing among several NICs."""
    parts = os.environ.get("SSH_CONNECTION", "").split()
    return parts[2] if len(parts) >= 4 else ""


def _primary_ip() -> str:
    """This host's IP on the interface that reaches the default route. The UDP socket is
    never connected to anything (no packet leaves the machine); it just asks the kernel
    which local address routing would pick."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("198.51.100.1", 9))  # TEST-NET-3: reserved, never routed anywhere
            return str(probe.getsockname()[0])
    except OSError:
        return ""


def advertised_host(bind_host: str) -> str:
    """A host component someone can actually type, given what the server bound to.

    A wildcard bind ("listening everywhere") is not an address — printing
    ``http://0.0.0.0:8765/`` gives the user nothing to click. Resolve it to the IP the
    SSH client already used, else this host's primary IP, else the hostname."""
    if bind_host and bind_host not in ("0.0.0.0", "::", "*"):
        return bind_host
    for candidate in (_ssh_server_ip(), _primary_ip()):
        if candidate:
            return candidate
    try:
        return socket.gethostname() or DEFAULT_HOST
    except OSError:
        return DEFAULT_HOST


def dashboard_url(bind_host: str, port: int) -> str:
    """The URL to show the user for a server bound to ``bind_host:port``."""
    return f"http://{advertised_host(bind_host)}:{port}/"


def bind_scanning(
    factory: Callable[[tuple[str, int]], _ServerT], host: str, port: int, *, span: int = PORT_SCAN_SPAN
) -> _ServerT:
    """Bind ``factory`` to the first free port at or after ``port``.

    Consecutive allocation is the point: a second dashboard/backtrace on the same box
    lands on 8766, a third on 8767, instead of a random ephemeral port that the user has
    to look up every time (and that no pre-arranged SSH forward can cover). ``port=0``
    keeps its usual meaning — let the OS pick — and an exhausted span falls back to that
    rather than failing to serve at all."""
    if port <= 0:
        return factory((host, 0))
    for candidate in range(port, min(port + max(span, 1), 65536)):
        try:
            return factory((host, candidate))
        except OSError:
            continue
    return factory((host, 0))


def build_server(
    repo: GitRepo,
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    email_logins: dict[str, str] | None = None,
) -> http.server.HTTPServer:
    """An HTTP server serving the dashboard for ``repo``, bound to ``host`` (defaulting to
    :func:`default_bind_host`) on the first free port at or after ``port``.

    ``email_logins`` (lowercased author email → GitHub login) supplements ``gh`` for
    commits not yet on the remote — e.g. fresh session commits — so the current user's
    local work still shows their GitHub ID."""
    handler = type(
        "DashboardHandler",
        (_DashboardHandler,),
        # One scope per server, carrying the repo and its own caches, so two servers (different
        # repos) never share a built dashboard.
        {"scope": RepoScope(repo, email_logins=email_logins)},
    )
    bind_host = default_bind_host() if host is None else host
    return bind_scanning(lambda address: _DashboardServer(address, handler), bind_host, port)


def browser_is_local() -> bool:
    """Whether a browser opened here would land on the user's *current* machine.

    The dashboard binds to localhost on whatever host aGiTrack runs on. When that
    host is a remote one — a Remote-SSH / WSL / container shell, or a plain SSH/Mosh
    session — calling ``webbrowser.open`` would try to launch a browser on the remote
    (which is usually headless, so it fails or opens the wrong screen). In that case we
    must NOT open it here and instead let the user reach the forwarded URL from their
    own machine.

    An explicit ``$BROWSER`` is always honored — editors that forward a local browser
    set it, and a user can point it at their own tunnel — so respecting it routes to the
    current machine. Otherwise a remote shell (``SSH_*``) or a headless Linux box (no
    ``DISPLAY``/``WAYLAND_DISPLAY``) is treated as not-local."""
    if os.environ.get("BROWSER"):
        return True
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def open_dashboard_in_browser(url: str) -> bool:
    """Open ``url`` in the user's browser when it is on this machine; return whether a
    browser was launched. On a remote/headless host it does nothing (the caller should
    tell the user to open the forwarded URL from their own machine)."""
    if not browser_is_local():
        return False
    try:
        return webbrowser.open(url)
    except (webbrowser.Error, OSError):
        return False


# macOS application names per browser family, most-likely first. A family can map to several
# applications (every Chromium browser reports itself as Chrome, and Brave and Arc cannot be told
# apart from it by user agent), so the first one that is actually RUNNING wins — aGiTrack must
# never launch a browser nobody had open.
def browser_family_from_user_agent(user_agent: str) -> str:
    """Which browser a request came from, by its ``User-Agent``.

    The page reports this itself, but a tab loaded before it learned to says nothing — and it is
    still the tab that has to be raised. The header is there on every request either way, so it
    is the more reliable of the two. Order matters: every Chromium browser also says "Chrome"."""
    ua = user_agent or ""
    for needle, family in (
        ("Edg/", "edge"),
        ("OPR/", "opera"),
        ("Vivaldi", "vivaldi"),
        ("Firefox/", "firefox"),
        ("Chrome/", "chrome"),
        ("Safari/", "safari"),
    ):
        if needle in ua:
            return family
    return ""


_BROWSER_APPS = {
    "chrome": ("Google Chrome", "Brave Browser", "Arc", "Chromium", "Google Chrome Canary"),
    "firefox": ("Firefox", "firefox", "Firefox Developer Edition"),
    "safari": ("Safari", "Safari Technology Preview"),
    "edge": ("Microsoft Edge",),
    "opera": ("Opera",),
    "vivaldi": ("Vivaldi",),
}


def raise_browser_window(family: str = "") -> bool:
    """Bring the user's browser to the front. True if we asked an application to activate.

    When aGiTrack steers a dashboard tab that is already open instead of opening another, the tab
    now holds the right page — but it may be behind three other windows, and a page that changed
    where nobody can see it is indistinguishable from nothing happening at all.

    A background page cannot raise itself: ``window.focus()`` is ignored without a user gesture in
    every current browser, by design. So the ASK comes from out here, where a process can talk to
    the window manager. ``family`` is what the page reported about itself, so the right browser is
    raised on a machine with several open.

    Best-effort everywhere and silent on failure: not being able to raise a window is never a
    reason to fail the thing the user actually asked for."""
    if not browser_is_local():
        return False  # a remote/headless host has no window to raise
    if not family:
        # NEVER guess. Trying each running browser in turn raised whichever happened to come
        # first in the list rather than the one holding the tab — and when that browser had no
        # window open, `open -a` made a blank one. The browser that just took the navigation
        # provably has a window (the tab itself), so knowing which it is also guarantees there is
        # something to raise. Not knowing is a reason to do nothing.
        return False
    if sys.platform == "darwin":
        return _raise_macos(family)
    if sys.platform.startswith("linux"):
        return _raise_linux(family)
    return _raise_windows(family)


def _running_macos_apps() -> set[str]:
    """Application bundles currently running, by name, from the process table alone.

    NOT via AppleScript's ``System Events``: asking that for a process list is an Automation
    request, so the first attempt pops "Terminal wants to control System Events" — a permission
    dialog nobody asked for, in service of a side errand. Every bundled application runs an
    executable at ``…/<Name>.app/Contents/MacOS/…``, which ``ps`` will say for free.
    """
    try:
        result = subprocess.run(["ps", "-eo", "comm="], capture_output=True, timeout=5, **UTF8_TEXT)
    except (OSError, subprocess.SubprocessError):
        return set()
    apps = set()
    for line in (result.stdout or "").splitlines():
        marker = ".app/Contents/MacOS/"
        if marker not in line:
            continue
        bundle = line.split(marker, 1)[0]
        apps.add(bundle.rsplit("/", 1)[-1])
    return apps


def _raise_macos(family: str) -> bool:
    running = _running_macos_apps()
    if not running:
        return False
    for app in _BROWSER_APPS.get(family, ()):
        match = next((name for name in running if name.lower() == app.lower()), "")
        if not match:
            continue
        try:
            # `open -a` on an app that is ALREADY running just brings it forward — no Automation
            # permission, no AppleScript. The running check above is what keeps it from launching
            # a browser nobody had open.
            subprocess.run(["open", "-a", match], capture_output=True, timeout=5, **UTF8_TEXT)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def _raise_linux(family: str) -> bool:
    """X11/Wayland: ask the window manager, if a tool for doing so is installed. Many desktops
    also refuse focus-stealing outright, in which case the window is highlighted instead — which
    still answers "where did it go?"."""
    for tool, args in (
        ("wmctrl", ["-a", family or "Mozilla Firefox"]),
        ("xdotool", ["search", "--name", family or "Firefox"]),
    ):
        if shutil.which(tool) is None:
            continue
        try:
            subprocess.run([tool, *args], capture_output=True, timeout=5, **UTF8_TEXT)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _raise_windows(family: str) -> bool:
    """Windows refuses to let a background process steal focus outright; the documented result of
    asking is that the taskbar button flashes, which is still an answer to "where did it go?"."""
    if sys.platform != "win32":
        return False
    titles = {"chrome": "Chrome", "firefox": "Firefox", "edge": "Edge", "opera": "Opera", "vivaldi": "Vivaldi"}
    needle = titles.get(family, "")
    if not needle:
        return False
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$s = New-Object -ComObject WScript.Shell; $s.AppActivate('{needle}')",
            ],
            capture_output=True,
            timeout=5,
            **UTF8_TEXT,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def ssh_target() -> str:
    """``user@host`` for SSH-ing back into this machine, for a copy-pasteable command.

    The login name is this process's user (whoever is running aGiTrack is who the user
    would log in as again) and the host is the address their SSH client already reached
    us on, so the command works verbatim with nothing to fill in."""
    try:
        import getpass

        user = getpass.getuser()
    except Exception:  # no password entry / no USER: fall back to a placeholder
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "<user>"
    host = _ssh_server_ip() or _primary_ip() or socket.gethostname() or "<remote-host>"
    return f"{user}@{host}"


def ssh_forward_command(port: int, *, target: str = "") -> str:
    """The exact ``ssh`` command to run **on the user's own machine** to forward ``port``.

    ``-N`` (no remote command) keeps it a pure tunnel the user can Ctrl-C when done."""
    return f"ssh -N -L {port}:localhost:{port} {target or ssh_target()}"


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def remote_access_help(url: str, port: int, *, bind_host: str = "") -> str:
    """The copy-paste block printed when the dashboard can't be opened here — i.e. aGiTrack
    is on a remote host and the browser is on the user's machine.

    Every route is spelled out in full so nothing has to be understood or adapted. Which
    routes exist depends on what the server bound: a network bind can be opened directly
    when the firewall allows it, with SSH forwarding as the fallback, whereas a loopback
    bind is reachable ONLY through the tunnel — offering the direct URL there would send
    the user to a page that cannot load. ``bind_host`` defaults to reading the URL, which
    is right for both (an advertised loopback URL means a loopback bind)."""
    tunnel = (
        f"Run this ON YOUR OWN MACHINE (in a new terminal; leave it running):\n"
        f"    {ssh_forward_command(port)}\n"
        f"then open:\n"
        f"    http://localhost:{port}/"
    )
    host = bind_host or urllib.parse.urlparse(url).hostname or ""
    if _is_loopback(host):
        return f"The dashboard is bound to this machine's loopback, so reach it over an SSH tunnel.\n{tunnel}"
    return (
        f"Open this from your own machine (works if the firewall allows port {port}):\n"
        f"    {url}\n"
        f"If that does not load, forward the port instead. {tunnel}"
    )


def exposure_note(bind_host: str) -> str:
    """A line (blank when irrelevant) saying the dashboard is reachable beyond this machine.

    Binding all interfaces is what makes a remote dashboard usable at all, but it also
    means anyone who can route to this host and past its firewall can read the repo's
    commits and diffs — so say so, and say how to undo it."""
    if bind_host not in ("0.0.0.0", "::", "*"):
        return ""
    return (
        "Listening on all network interfaces so you can reach it from your own machine; "
        f"anyone able to reach this host on that port can view it. Set {BIND_HOST_ENV}=127.0.0.1 "
        "to keep it loopback-only.\n"
    )


def remote_browser_hint(url: str, port: int) -> str:
    """A compact one-line variant of :func:`remote_access_help`, for the TUI status popup."""
    return f"Open {url} from your own machine, or forward the port: `{ssh_forward_command(port)}`"


def serve_dashboard(
    repo: GitRepo,
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    bind_host = default_bind_host() if host is None else host
    server = build_server(repo, host=bind_host, port=port)
    bound_port = int(server.server_address[1])
    url = dashboard_url(bind_host, bound_port)
    print(f"aGiTrack dashboard live at {url}\nRecomputed from commit metadata; auto-refreshes. Press Ctrl-C to stop.")
    if open_browser and not open_dashboard_in_browser(url):
        print(remote_access_help(url, bound_port, bind_host=bind_host))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the dashboard.")
    finally:
        server.server_close()
    return 0
