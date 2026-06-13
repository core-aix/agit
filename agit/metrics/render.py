"""Plain-text dashboard for `agit --dashboard` (#54).

Plain text on purpose: it works in any terminal, pipes into `less`, and can be
pasted into an issue. Everything shown is computed from commit messages alone,
so the numbers are identical on every clone of the repository.
"""

from __future__ import annotations

from agit.git import GitRepo
from agit.metrics.collect import Dashboard, build_dashboard

_TOKEN_ORDER = [
    ("input", "input"),
    ("output", "output"),
    ("reasoning", "reasoning"),
    ("cache_read", "cache read"),
    ("cache_write", "cache write"),
    ("subagent_input", "subagent input"),
    ("subagent_output", "subagent output"),
    ("subagent_cache_read", "subagent cache read"),
    ("subagent_cache_write", "subagent cache write"),
    ("summary_input", "summarizer input"),
    ("summary_output", "summarizer output"),
]


def render_dashboard(repo: GitRepo, ref: str = "HEAD") -> str:
    return format_dashboard(build_dashboard(repo, ref))


def format_dashboard(dash: Dashboard) -> str:
    lines: list[str] = []
    lines.append(f"aGiT Dashboard — {dash.repo}")
    lines.append(f"branch {dash.branch}, {dash.total_commits:,} commits")
    lines.append("")

    lines.append("Coverage")
    tracked = dash.tracked_commits
    untracked = dash.total_commits - tracked
    lines.append(f"  aGiT-tracked commits: {tracked:,}/{dash.total_commits:,} ({dash.coverage:.1%})")
    lines.append(
        f"    agent {dash.count('agent'):,} | backend-made (covered) {dash.count('covered'):,}"
        f" | agent-merge {dash.count('agent-merge'):,} | user {dash.count('user'):,}"
    )
    lines.append(f"  untracked (no aGiT metadata): {untracked:,}")
    lines.append("")

    ai_ins, ai_del = dash.ai_lines
    human_ins, human_del = dash.human_lines
    nt_ins, nt_del = dash.nontracked_lines
    total_lines = ai_ins + ai_del + human_ins + human_del + nt_ins + nt_del
    lines.append("Code changes (lines)")
    lines.append(f"  AI (agent + covered):       +{ai_ins:,} / -{ai_del:,}{_share(ai_ins + ai_del, total_lines)}")
    lines.append(
        f"  Human (user commits, aGiT): +{human_ins:,} / -{human_del:,}{_share(human_ins + human_del, total_lines)}"
    )
    lines.append(f"  Non-tracked (outside aGiT): +{nt_ins:,} / -{nt_del:,}{_share(nt_ins + nt_del, total_lines)}")
    lines.append("")

    lines.append("Tokens (from aGiT commit metadata)")
    totals = dash.token_totals
    shown = [(label, totals[key]) for key, label in _TOKEN_ORDER if totals.get(key)]
    if shown:
        lines.extend(f"  {label}: {value:,}" for label, value in shown)
        efficiency = dash.lines_per_1k_output_tokens
        if efficiency is not None:
            lines.append(f"  efficiency: {efficiency:,.1f} AI-changed lines per 1k output tokens")
    else:
        lines.append("  (no token metadata recorded)")
    lines.append("")

    lines.extend(_group_section("By backend", dash.by_backend))
    lines.extend(_group_section("By model", dash.by_model))

    lines.append("By committer")
    for author, stats in sorted(dash.by_author.items(), key=lambda item: -item[1]["commits"]):
        lines.append(
            f"  {author}: {stats['commits']:,} commits"
            f" ({stats.get('agit_commits', 0):,} via aGiT),"
            f" +{stats['insertions']:,} / -{stats['deletions']:,}"
        )
    lines.append("")

    lines.append("Possible loops (near-identical repeated prompts)")
    if dash.loops:
        for loop in dash.loops:
            prompt = loop.prompt if len(loop.prompt) <= 60 else loop.prompt[:57] + "..."
            where = (
                f"within commit {loop.shas[0]}"
                if loop.within_commit
                else f"{len(loop.shas)} commits {loop.shas[0]}..{loop.shas[-1]}"
            )
            tokens = f", {loop.output_tokens:,} output tokens" if loop.output_tokens else ""
            lines.append(f'  {where}: "{prompt}"{tokens}')
    else:
        lines.append("  none detected")

    return "\n".join(lines)


def _share(part: int, total: int) -> str:
    return f"  ({part / total:.1%})" if total else ""


def _group_section(title: str, groups: dict[str, dict[str, int]]) -> list[str]:
    lines = [title]
    if not groups:
        lines.append("  (no agent commits)")
    for label, stats in sorted(groups.items(), key=lambda item: -item[1]["commits"]):
        tokens = ""
        if stats.get("output_tokens"):
            tokens = f", {stats['output_tokens']:,} output tokens"
        lines.append(
            f"  {label}: {stats['commits']:,} commits, +{stats['insertions']:,} / -{stats['deletions']:,}{tokens}"
        )
    lines.append("")
    return lines
