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
import webbrowser

from agit.git import GitRepo
from agit.metrics.collect import build_dashboard
from agit.metrics.web import dashboard_data, format_html

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    repo: GitRepo  # set on the per-server subclass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._respond("text/html; charset=utf-8", self._html())
        elif path == "/data":
            self._respond("application/json", self._data())
        else:
            self.send_error(404, "not found")

    def _html(self) -> bytes:
        return format_html(build_dashboard(self.repo)).encode("utf-8")

    def _data(self) -> bytes:
        return json.dumps(dashboard_data(build_dashboard(self.repo))).encode("utf-8")

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


def build_server(repo: GitRepo, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> http.server.HTTPServer:
    """An HTTP server bound to ``host:port`` serving the dashboard for ``repo``.
    Falls back to an OS-assigned free port if the preferred one is taken."""
    handler = type("DashboardHandler", (_DashboardHandler,), {"repo": repo})
    try:
        return http.server.HTTPServer((host, port), handler)
    except OSError:
        return http.server.HTTPServer((host, 0), handler)


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
