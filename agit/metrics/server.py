"""Localhost server for the live aGiT dashboard (#54).

`agit --dashboard` serves the HTML dashboard on localhost and opens it in the
browser. The page polls ``/data`` on an interval and re-renders, so the
dashboard reflects new commits as they land — useful for watching an agent
work. Everything is recomputed from ``git log`` on each request: read-only, no
state, identical on every clone.
"""

from __future__ import annotations

import http.server
import json
import sys
import urllib.parse
import webbrowser
from typing import Any

from agit.git import GitRepo
from agit.metrics.collect import Dashboard, build_dashboard
from agit.metrics.github import resolve_logins
from agit.metrics.web import aggregates_payload, format_html, log_page, shared_sessions_for

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _str(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _str(query, key)
    return int(raw) if raw.lstrip("-").isdigit() else default


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    repo: GitRepo  # set on the per-server subclass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            author, backend, model = _str(query, "author"), _str(query, "backend"), _str(query, "model")
            frm, to = _int(query, "from", 0), _int(query, "to", 0)
            if parsed.path in ("/", "/index.html"):
                html = format_html(self._dashboard(), shared_sessions=shared_sessions_for(self.repo))
                self._respond("text/html; charset=utf-8", html.encode("utf-8"))
            elif parsed.path == "/data":
                payload = aggregates_payload(
                    self._dashboard(),
                    author=author,
                    backend=backend,
                    model=model,
                    frm=frm,
                    to=to,
                    granularity=_str(query, "granularity"),
                )
                payload["shared_sessions"] = shared_sessions_for(self.repo)
                self._respond("application/json", self._json(payload))
            elif parsed.path == "/log":
                page = log_page(
                    self._dashboard(),
                    author=author,
                    backend=backend,
                    model=model,
                    frm=frm,
                    to=to,
                    offset=_int(query, "offset", 0),
                    limit=_int(query, "limit", 50),
                )
                self._respond("application/json", self._json(page))
            else:
                self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser closed the connection mid-response — a poll superseded
            # by the next one, a refresh, or a closed tab. Harmless; don't let
            # http.server dump a traceback to the console aGiT is running in.
            pass

    @staticmethod
    def _json(payload: dict) -> bytes:
        return json.dumps(payload).encode("utf-8")

    def _dashboard(self) -> Dashboard:
        # resolve_logins is cached with a TTL, so frequent refreshes don't
        # re-hit the GitHub API; it returns {} when gh is unavailable.
        return build_dashboard(self.repo, sha_logins=resolve_logins(self.repo))

    def _respond(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Always recompute; never let the browser cache /data.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Stay quiet: the dashboard is a foreground tool, not a web log."""


class _DashboardServer(http.server.ThreadingHTTPServer):
    # Threaded so one slow request (e.g. the first gh lookup) never blocks the
    # page; daemon threads so Ctrl-C exits immediately without joining them.
    daemon_threads = True

    # A client that vanished mid-write surfaces as BrokenPipeError here too;
    # swallow it so the server doesn't print a traceback per dropped poll.
    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            super().handle_error(request, client_address)


def build_server(repo: GitRepo, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> http.server.HTTPServer:
    """An HTTP server bound to ``host:port`` serving the dashboard for ``repo``.
    Falls back to an OS-assigned free port if the preferred one is taken."""
    handler = type("DashboardHandler", (_DashboardHandler,), {"repo": repo})
    try:
        return _DashboardServer((host, port), handler)
    except OSError:
        return _DashboardServer((host, 0), handler)


def serve_dashboard(
    repo: GitRepo,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    server = build_server(repo, host=host, port=port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"aGiT dashboard live at {url}\nRecomputed from commit metadata; auto-refreshes. Press Ctrl-C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            print("Could not open a browser automatically; open the URL above manually.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the dashboard.")
    finally:
        server.server_close()
    return 0
