"""Repository metrics extracted from aGiT commit metadata (#54)."""

from agit.metrics.collect import CommitStat, Dashboard, build_dashboard, collect_commit_stats
from agit.metrics.render import render_dashboard
from agit.metrics.web import dashboard_data, render_html

__all__ = [
    "CommitStat",
    "Dashboard",
    "build_dashboard",
    "collect_commit_stats",
    "dashboard_data",
    "render_dashboard",
    "render_html",
]
