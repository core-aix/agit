"""HTML dashboard for `agit --dashboard` (#54).

The page renders from two server endpoints so the browser never holds the whole
history — memory stays bounded no matter how big the repo is:

* :func:`aggregates_payload` (served at ``/data?<filters>``) computes every
  metric panel (coverage, tracked-AI vs non-tracked lines, tokens, per-backend/
  model/committer breakdowns, loop detection) over the filtered commits and
  returns just the numbers — no per-commit list.
* :func:`log_page` (served at ``/log?<filters>&offset=&limit=``) returns one
  page of the commit log; only that page carries the heavy message / trace /
  squash constituents.

:func:`format_html` embeds an initial aggregates payload plus the first log page
for an instant first paint, then the JS fetches ``/data`` and ``/log`` as the
filters change and the user pages through the log. Everything is computed from
``git log`` (+ ``gh`` for GitHub IDs when present), so the numbers are identical
on every clone. The visual language matches docs/index.html: a CRT/phosphor
terminal.

:func:`dashboard_data` (the full per-commit serialization) is retained for tests
and ad-hoc use; the live page does not embed it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agit.git import GitRepo
from agit.metrics.collect import CommitStat, Dashboard, build_dashboard


def render_html(repo: GitRepo, ref: str = "HEAD") -> str:
    from agit.metrics.github import resolve_logins

    return format_html(build_dashboard(repo, ref, sha_logins=resolve_logins(repo)))


def format_html(dash: Dashboard) -> str:
    payload = json.dumps(initial_payload(dash), separators=(",", ":"))
    repo_name = dash.repo.rstrip("/").rsplit("/", 1)[-1] or dash.repo
    return (
        _TEMPLATE.replace("__DATA__", payload)
        .replace("__REPO_NAME__", _escape(repo_name))
        .replace("__REPO__", _escape(dash.repo))
        .replace("__BRANCH__", _escape(dash.branch))
    )


def dashboard_data(dash: Dashboard) -> dict:
    """Serialize the dashboard to a JSON-ready dict.

    Covered commits (backend-made, #58) carry no backend/model of their own;
    they inherit the *effective* backend/model of the cover commit that lists
    them, so a per-backend or per-model filter buckets their diff with the
    model that actually wrote it.
    """
    covers: dict[str, CommitStat] = {}
    for stat in dash.stats:
        for short in stat.covered_commits:
            covers[short] = stat

    commits = []
    for stat in dash.stats:
        eff_backend, eff_model = _effective(stat, covers)
        commits.append(
            {
                "short": stat.short,
                # The merged committer identity, so name variants of one person
                # collapse to a single filter/breakdown entry (#54).
                "author": dash.label_of(stat),
                "subject": stat.subject,
                "kind": stat.kind,
                "backend": stat.backend,
                "model": stat.model,
                "eff_backend": eff_backend,
                "eff_model": eff_model,
                "tokens": stat.tokens,
                "ins": stat.insertions,
                "del": stat.deletions,
                "prompt": stat.prompt,
                "user_prompts": stat.user_prompts,
                "ts": stat.timestamp,  # commit time (epoch) for the time filter
                "started": stat.started_at,  # AI conversation span (ISO, may be "")
                "ended": stat.ended_at,
                "message": stat.message,  # full message, shown when a log entry opens
                "url": (dash.commit_base + stat.sha) if dash.commit_base else "",
                # Original commits of a squash, so the log entry can expand into
                # them (recursive, for multiple rounds of squashing).
                "parts": [_part_payload(part) for part in stat.constituents],
            }
        )

    return {
        "repo": dash.repo,
        "branch": dash.branch,
        # HEAD sha lets the live page skip re-rendering when nothing changed.
        "head": dash.stats[-1].sha if dash.stats else "",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "committers": sorted({c["author"] for c in commits if c["author"]}),
        "backends": sorted({c["eff_backend"] for c in commits if c["eff_backend"]}),
        "models": sorted({c["eff_model"] for c in commits if c["eff_model"]}),
        "commits": commits,
    }


def _part_payload(part: CommitStat) -> dict:
    """A squash constituent (an original commit), serialized for the expandable
    log view — recursive in case a constituent is itself a squash."""
    return {
        "subject": part.subject,
        "kind": part.kind,
        "backend": part.backend,
        "model": part.model,
        "tokens": part.tokens,
        "started": part.started_at,
        "ended": part.ended_at,
        "message": part.message,
        "parts": [_part_payload(child) for child in part.constituents],
    }


def _effective(stat: CommitStat, covers: dict[str, CommitStat]) -> tuple[str | None, str | None]:
    if stat.kind in ("agent", "agent-merge"):
        return stat.backend, stat.model
    if stat.kind == "covered":
        for short, cover in covers.items():
            if stat.sha.startswith(short):
                return cover.backend, cover.model
    return None, None


# ---------------------------------------------------------------------------
# Server-side aggregation + paginated log (so the browser never holds every
# commit's full message / trace / squash constituents).
# ---------------------------------------------------------------------------

PAGE_SIZE = 50
_KINDS = ("agent", "covered", "agent-merge", "user", "agit-ops", "untracked")


def _covers(dash: Dashboard) -> dict[str, CommitStat]:
    covers: dict[str, CommitStat] = {}
    for stat in dash.stats:
        for short in stat.covered_commits:
            covers[short] = stat
    return covers


def _filter_stats(dash: Dashboard, *, author: str, backend: str, model: str, frm: int, to: int) -> list[CommitStat]:
    covers = _covers(dash)
    out: list[CommitStat] = []
    for stat in dash.stats:
        if author and dash.label_of(stat) != author:
            continue
        eff_backend, eff_model = _effective(stat, covers)
        if backend and eff_backend != backend:
            continue
        if model and eff_model != model:
            continue
        if frm and (not stat.timestamp or stat.timestamp < frm):
            continue
        if to and (not stat.timestamp or stat.timestamp > to):
            continue
        out.append(stat)
    return out


def _filtered_dashboard(dash: Dashboard, stats: list[CommitStat]) -> Dashboard:
    from agit.metrics.collect import _detect_loops

    return Dashboard(
        repo=dash.repo,
        branch=dash.branch,
        stats=stats,
        loops=_detect_loops(stats),
        sha_logins=dash.sha_logins,
        commit_base=dash.commit_base,
    )


def _aggregates(fd: Dashboard) -> dict:
    return {
        "total": fd.total_commits,
        "tracked": fd.tracked_commits,
        "coverage": fd.coverage,
        "kinds": {kind: fd.count(kind) for kind in _KINDS},
        "ai_lines": list(fd.ai_lines),
        "nontracked_lines": list(fd.nontracked_lines),
        "tokens": fd.token_totals,
        "efficiency": fd.lines_per_1k_output_tokens,
        "by_backend": fd.by_backend,
        "by_model": fd.by_model,
        "by_committer": fd.by_author,
        "loops": [
            {"shas": loop.shas, "prompt": loop.prompt, "output": loop.output_tokens, "within": loop.within_commit}
            for loop in fd.loops
        ],
    }


_AI_KINDS = ("agent", "covered", "agent-merge")
GRANULARITIES = ("hour", "day", "week", "month")
DEFAULT_GRANULARITY = "day"
# Cap the number of plotted buckets so an extreme granularity/range (e.g. hourly
# over years) can't bloat the payload; the most recent buckets are kept.
_MAX_BUCKETS = 1500


def _period_start(ts: int, granularity: str) -> int:
    """Epoch seconds of the start of the calendar period (UTC) that ``ts`` falls
    in, for the chosen granularity — day floors to midnight, week to Monday,
    month to the 1st, hour to the top of the hour."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if granularity == "hour":
        dt = dt.replace(minute=0, second=0, microsecond=0)
    elif granularity == "month":
        dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif granularity == "week":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=dt.weekday())
    else:  # day
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


def _next_period(ts: int, granularity: str) -> int:
    """Epoch seconds of the start of the period after the one starting at ``ts``."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if granularity == "hour":
        return int((dt + timedelta(hours=1)).timestamp())
    if granularity == "week":
        return int((dt + timedelta(days=7)).timestamp())
    if granularity == "month":
        year, month = (dt.year + dt.month // 12), (dt.month % 12 + 1)
        return int(dt.replace(year=year, month=month).timestamp())
    return int((dt + timedelta(days=1)).timestamp())


def _timeseries(stats: list[CommitStat], *, granularity: str = DEFAULT_GRANULARITY) -> dict:
    """Per-period commits / AI lines / token usage over time for the (already
    filtered) commits, bucketed by calendar period (the configurable
    *granularity*). Values are the activity *within* each period — not a running
    total — so a quiet day reads as a dip to zero. Empty periods between the first
    and last are filled with zeros so the time axis is continuous. ``t`` holds each
    bucket's start epoch seconds; every series is the same length as ``t``.

    The browser normalises each series to its own peak so wildly different
    magnitudes (tens of commits vs millions of tokens) share one plot, and shows
    the real per-period value on hover."""
    if granularity not in GRANULARITIES:
        granularity = DEFAULT_GRANULARITY
    dated = [s for s in stats if s.timestamp]
    empty: dict = {
        "t": [],
        "commits": [],
        "ai_lines": [],
        "output_tokens": [],
        "input_tokens": [],
        "granularity": granularity,
    }
    if not dated:
        return empty
    lo = _period_start(min(s.timestamp for s in dated), granularity)
    hi = _period_start(max(s.timestamp for s in dated), granularity)
    starts: list[int] = []
    cur = lo
    # Forward-fill the period boundaries; the loop is bounded so a pathological
    # granularity/range can't spin, and the slice keeps the most recent buckets.
    while cur <= hi and len(starts) <= _MAX_BUCKETS * 12:
        starts.append(cur)
        cur = _next_period(cur, granularity)
    starts = starts[-_MAX_BUCKETS:]
    index = {start: i for i, start in enumerate(starts)}
    n = len(starts)
    commits = [0] * n
    lines = [0] * n
    out_tok = [0] * n
    in_tok = [0] * n
    ai = set(_AI_KINDS)
    for s in dated:
        i = index.get(_period_start(s.timestamp, granularity))
        if i is None:  # in an old bucket dropped by the cap
            continue
        commits[i] += 1
        if s.kind in ai:
            lines[i] += s.insertions + s.deletions
        out_tok[i] += s.tokens.get("output", 0)
        in_tok[i] += s.tokens.get("input", 0)
    return {
        "t": starts,
        "commits": commits,
        "ai_lines": lines,
        "output_tokens": out_tok,
        "input_tokens": in_tok,
        "granularity": granularity,
    }


def _options(dash: Dashboard) -> dict:
    covers = _covers(dash)
    committers, backends, models = set(), set(), set()
    for stat in dash.stats:
        committers.add(dash.label_of(stat))
        eff_backend, eff_model = _effective(stat, covers)
        if eff_backend:
            backends.add(eff_backend)
        if eff_model:
            models.add(eff_model)
    return {
        "committers": sorted(c for c in committers if c),
        "backends": sorted(backends),
        "models": sorted(models),
    }


def aggregates_payload(
    dash: Dashboard,
    *,
    author: str = "",
    backend: str = "",
    model: str = "",
    frm: int = 0,
    to: int = 0,
    granularity: str = DEFAULT_GRANULARITY,
) -> dict:
    """All the metric panels for the given filters — no per-commit list, so the
    response stays small no matter how large the repository is. Filter options
    come from the full history so the dropdowns never lose entries."""
    stats = _filter_stats(dash, author=author, backend=backend, model=model, frm=frm, to=to)
    return {
        "head": dash.stats[-1].sha if dash.stats else "",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "options": _options(dash),
        "agg": _aggregates(_filtered_dashboard(dash, stats)),
        "timeseries": _timeseries(stats, granularity=granularity),
    }


def _log_entry(dash: Dashboard, stat: CommitStat, covers: dict[str, CommitStat]) -> dict:
    eff_backend, eff_model = _effective(stat, covers)
    return {
        "short": stat.short,
        "author": dash.label_of(stat),
        "subject": stat.subject,
        "kind": stat.kind,
        "eff_backend": eff_backend,
        "eff_model": eff_model,
        "tokens": stat.tokens,
        "ins": stat.insertions,
        "del": stat.deletions,
        "ts": stat.timestamp,
        "started": stat.started_at,
        "ended": stat.ended_at,
        "message": stat.message,
        "url": (dash.commit_base + stat.sha) if dash.commit_base else "",
        "parts": [_part_payload(part) for part in stat.constituents],
    }


def log_page(
    dash: Dashboard,
    *,
    author: str = "",
    backend: str = "",
    model: str = "",
    frm: int = 0,
    to: int = 0,
    offset: int = 0,
    limit: int = PAGE_SIZE,
) -> dict:
    """One page of the commit log (newest first) for the given filters. Only this
    page's commits carry the heavy message / squash constituents, so memory and
    payload stay bounded however deep the history is."""
    stats = _filter_stats(dash, author=author, backend=backend, model=model, frm=frm, to=to)
    stats.reverse()  # newest first
    covers = _covers(dash)
    offset = max(0, offset)
    limit = max(1, min(limit, 200))
    page = stats[offset : offset + limit]
    return {
        "total": len(stats),
        "offset": offset,
        "limit": limit,
        "entries": [_log_entry(dash, stat, covers) for stat in page],
    }


def initial_payload(dash: Dashboard) -> dict:
    """What the page embeds for an instant first paint: unfiltered aggregates,
    the first log page, repo metadata, and the page size."""
    return {
        "repo": dash.repo,
        "branch": dash.branch,
        "page_size": PAGE_SIZE,
        **aggregates_payload(dash),
        "log": log_page(dash),
    }


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# The page. Brace-heavy CSS/JS lives verbatim in the template (no f-strings);
# only the __PLACEHOLDER__ tokens are substituted.
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aGiT dashboard · __REPO_NAME__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#070b09; --panel:#0c120e; --panel-2:#101813; --line:#1d2a21;
  --phosphor:#3dffa0; --phosphor-dim:#1f7a52; --amber:#ffb454; --amber-dim:#8a5e2a;
  --fg:#cfe7d8; --fg-dim:#7e998a; --red:#ff6b6b; --ops:#67b8d6;
  --mono:"IBM Plex Mono",ui-monospace,monospace; --display:"VT323",var(--mono);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--ink);color:var(--fg);font-family:var(--mono);font-size:15px;line-height:1.6;overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:30;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px);opacity:.35}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:31;
  background:radial-gradient(ellipse at 50% 12%,transparent 60%,rgba(0,0,0,.55) 100%)}
::selection{background:var(--phosphor);color:var(--ink)}
a{color:var(--phosphor);text-decoration:none;border-bottom:1px solid var(--phosphor-dim)}
a:hover{color:var(--ink);background:var(--phosphor)}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 80px}
.neterror{position:fixed;top:0;left:0;right:0;z-index:40;background:#3a0f0f;color:#ffd5d5;
  border-bottom:2px solid var(--red);padding:10px 18px;font-size:13px;text-align:center;
  box-shadow:0 6px 20px rgba(0,0,0,.55);animation:rise .25s ease}
@keyframes rise{from{transform:translateY(-100%)}to{transform:none}}

header{padding:54px 0 22px}
.brand{font-family:var(--display);font-weight:400;font-size:clamp(56px,11vw,104px);line-height:.85;color:var(--phosphor);
  text-shadow:0 0 14px rgba(61,255,160,.5),0 0 60px rgba(61,255,160,.22);letter-spacing:2px}
.brand .a{color:var(--amber);text-shadow:0 0 14px rgba(255,180,84,.5),0 0 60px rgba(255,180,84,.2)}
.brand .sub{font-family:var(--display);font-size:.42em;color:var(--fg-dim);letter-spacing:3px;text-shadow:none}
.meta{margin-top:12px;color:var(--fg-dim);font-size:13.5px}
.meta b{color:var(--fg);font-weight:600}
.meta .tag{color:var(--amber)}

/* ---- filter bar ---- */
.controls{position:sticky;top:0;z-index:20;margin:22px 0 30px;padding:14px 16px;background:var(--panel);
  border:1px solid var(--line);border-bottom-width:3px;display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end}
.controls .prompt{color:var(--phosphor);font-weight:600;align-self:center}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:11px;color:var(--amber);letter-spacing:.6px;text-transform:uppercase}
.field select{appearance:none;background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13.5px;padding:7px 30px 7px 11px;cursor:pointer;min-width:170px;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.field select:focus{outline:none;border-color:var(--phosphor)}
.field input[type=date]{background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13px;padding:6px 9px;cursor:pointer}
.field input[type=date]:focus{outline:none;border-color:var(--phosphor)}
.field input[type=date]::-webkit-calendar-picker-indicator{filter:invert(.7) sepia(1) hue-rotate(90deg)}
.scope{margin-left:auto;align-self:center;color:var(--fg-dim);font-size:12.5px}
.scope b{color:var(--phosphor)}
.reset{cursor:pointer;border:1px solid var(--amber);color:var(--amber);background:transparent;
  font-family:var(--mono);font-size:12.5px;padding:7px 12px;align-self:flex-end}
.reset:hover{background:var(--amber);color:var(--ink)}

h2.section{font-family:var(--display);font-size:27px;font-weight:400;color:var(--phosphor);letter-spacing:1px;
  margin:38px 0 14px;text-shadow:0 0 16px rgba(61,255,160,.3)}
h2.section::before{content:"# ";color:var(--amber)}

/* ---- stat cards ---- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);padding:16px 18px;position:relative;transition:border-color .15s}
.card:hover{border-color:var(--phosphor-dim)}
/* No text-transform: the "aGiT" brand must never render all-caps. */
.card .label{font-size:11.5px;color:var(--amber);letter-spacing:.5px}
.card .value{font-family:var(--display);font-size:42px;line-height:1.05;color:var(--phosphor);margin-top:6px;
  text-shadow:0 0 14px rgba(61,255,160,.3)}
.card .value.amber{color:var(--amber);text-shadow:0 0 14px rgba(255,180,84,.3)}
.card .note{font-size:12px;color:var(--fg-dim);margin-top:4px}

/* ---- time-series chart ---- */
.chartpanel{padding:14px 16px 10px;position:relative}
.chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:10px}
.gran{display:flex;align-items:center;gap:7px;color:var(--amber);font-size:11px;letter-spacing:.6px;text-transform:uppercase}
.gran select{appearance:none;background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:12.5px;padding:5px 26px 5px 9px;cursor:pointer;text-transform:none;letter-spacing:normal;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 14px) 50%,calc(100% - 9px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.gran select:focus{outline:none;border-color:var(--phosphor)}
.legend{display:flex;flex-wrap:wrap;gap:8px}
.legend .lg{display:inline-flex;align-items:center;gap:7px;cursor:pointer;background:var(--ink);
  border:1px solid var(--line);color:var(--fg);font-family:var(--mono);font-size:12px;padding:5px 10px;transition:opacity .12s}
.legend .lg .sw{width:11px;height:11px;border:1px solid var(--c);background:var(--c);box-shadow:0 0 7px var(--c)}
.legend .lg b{color:var(--c)}
.legend .lg.off{opacity:.4}
.legend .lg.off .sw{background:transparent;box-shadow:none}
.legend .lg:hover{border-color:var(--c)}
.chartwrap{position:relative;width:100%;height:280px}
.chartwrap canvas{display:block;width:100%;height:100%;cursor:crosshair}
.chart-empty{color:var(--fg-dim);padding:40px 4px;text-align:center}
.tip{position:absolute;pointer-events:none;z-index:5;background:var(--panel-2);border:1px solid var(--phosphor-dim);
  padding:7px 10px;font-size:12px;color:var(--fg);white-space:nowrap;box-shadow:0 4px 16px rgba(0,0,0,.6);transform:translateX(-50%)}
.tip .td{color:var(--amber);margin-bottom:3px}
.tip .tr{display:flex;align-items:center;gap:6px}
.tip .tr .sw{width:8px;height:8px;background:var(--c);box-shadow:0 0 6px var(--c)}
.tip .tr b{color:var(--c);margin-left:auto;padding-left:10px}

/* ---- bar / table ---- */
.panel{background:var(--panel);border:1px solid var(--line);padding:6px 0}
.row{display:grid;grid-template-columns:minmax(120px,1.4fr) 2.6fr minmax(150px,1fr);gap:14px;align-items:center;
  padding:11px 18px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:none}
.row .name{color:var(--fg);font-weight:500;overflow-wrap:anywhere}
.row .name small{color:var(--fg-dim);font-weight:400}
.bar{position:relative;height:18px;background:var(--ink);border:1px solid var(--line);overflow:hidden}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--phosphor-dim);box-shadow:0 0 10px rgba(61,255,160,.4)}
.bar i.amber{background:var(--amber-dim);box-shadow:0 0 10px rgba(255,180,84,.35)}
.bar span{position:absolute;right:6px;top:0;font-size:11px;color:var(--fg-dim);line-height:18px}
.row .num{text-align:right;color:var(--fg-dim);font-size:12.5px}
.row .num b{color:var(--phosphor);font-weight:600}
.empty{padding:16px 18px;color:var(--fg-dim)}
.hint{padding:8px 18px 0;color:var(--fg-dim);font-size:11.5px;font-style:italic}
.kindcounts{padding:11px 18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--fg-dim);line-height:1.9}
.kindcounts .klabel{color:var(--amber);margin-right:4px}
.kindcounts .kc{white-space:nowrap;cursor:help;border-bottom:1px dotted var(--fg-dim)}
.kindcounts .kc b{color:var(--fg)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:760px){.split{grid-template-columns:1fr}.row{grid-template-columns:1fr;gap:6px}}

/* ---- loops ---- */
.loop{padding:13px 18px;border-bottom:1px solid var(--line)}
.loop:last-child{border-bottom:none}
.loop .where{color:var(--red)}
.loop .q{color:var(--fg);font-style:italic;word-break:break-word}
.loop .cost{color:var(--amber);font-size:12.5px}

/* ---- commit log rail ---- */
.log{position:relative;padding-left:34px;margin-top:8px}
.log::before{content:"";position:absolute;left:9px;top:6px;bottom:6px;width:2px;
  background:linear-gradient(var(--phosphor-dim),var(--line) 90%,transparent)}
.entry{position:relative;padding:9px 0;border-bottom:1px dashed var(--line);display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;cursor:pointer}
.entry:last-child{border-bottom:none}
.entry:hover{background:rgba(61,255,160,.04)}
.entry::before{content:"";position:absolute;left:-30px;top:15px;width:9px;height:9px;border-radius:50%;
  background:var(--ink);border:2px solid var(--phosphor-dim)}
.entry.ai::before{border-color:var(--phosphor);box-shadow:0 0 8px rgba(61,255,160,.5)}
.entry.ops::before{border-color:var(--ops);box-shadow:0 0 8px rgba(103,184,214,.4)}
.entry.nontracked::before{border-color:var(--amber)}
.entry .sha{color:var(--amber);font-size:12.5px}
.entry .ksub{flex:1;min-width:200px;color:var(--fg)}
.entry .badge{font-size:10.5px;letter-spacing:.5px;padding:1px 7px;border:1px solid var(--line);color:var(--fg-dim)}
.entry .badge.ai{color:var(--phosphor);border-color:var(--phosphor-dim)}
.entry .badge.ops{color:var(--ops);border-color:var(--ops)}
.entry .badge.nontracked{color:var(--amber);border-color:var(--amber-dim)}
.entry .lc{font-size:12px;color:var(--fg-dim)}
.entry .lc .add{color:var(--phosphor)} .entry .lc .rem{color:var(--red)}
.entry .tok{font-size:11px;border:1px solid var(--line);padding:0 5px;color:var(--fg-dim)}
.entry .tok.out{color:var(--phosphor);border-color:var(--phosphor-dim)}
.entry .tok.dim{opacity:.7}
.entry .squash{font-size:10.5px;color:var(--ops);border:1px solid var(--ops);padding:1px 6px;letter-spacing:.4px}
.entry .detail{flex-basis:100%;width:100%;margin:8px 0 4px;border-left:2px solid var(--phosphor-dim);padding-left:14px;cursor:default}
.entry .detail .dhead{color:var(--amber);font-size:12.5px;margin-bottom:4px}
.entry .detail .dmeta{color:var(--ops);font-size:12px;margin-bottom:6px}
.entry .detail .dmsg{font-size:12.5px;color:var(--fg-dim);background:var(--ink);border:1px solid var(--line);
  padding:4px 12px;max-height:440px;overflow:auto;word-break:break-word}
/* rendered Markdown inside the expanded message */
.dmsg.md p{margin:7px 0}
.dmsg.md .md-h{font-family:var(--mono);color:var(--amber);margin:11px 0 5px;font-size:13px;font-weight:600}
/* Heading depth reads at a glance: structural sections (# …) brightest/largest,
   the ## User/## Agent role one step down, and a message's own nested headings
   smaller, dimmer and indented so they sit visibly under their role. md() maps a
   source level L to <h(L+2)>, so these are # → h3, ## → h4, content → h5/h6. */
.dmsg.md h3.md-h{font-size:15px;color:var(--amber)}
.dmsg.md h4.md-h{font-size:13.5px;color:var(--phosphor)}
.dmsg.md h5.md-h{font-size:12.5px;color:var(--ops);font-weight:500;padding-left:10px;border-left:2px solid var(--line)}
.dmsg.md h6.md-h{font-size:12px;color:var(--fg-dim);font-weight:500;padding-left:20px;border-left:2px solid var(--line)}
.dmsg.md ul{margin:6px 0 6px 18px} .dmsg.md li{margin:2px 0}
.dmsg.md code{background:var(--panel-2);border:1px solid var(--line);padding:0 4px;color:var(--phosphor);font-size:12px}
.dmsg.md strong{color:var(--fg)} .dmsg.md em{color:var(--fg)}
.dmsg.md .md-code{white-space:pre-wrap;background:var(--panel-2);border:1px solid var(--line);
  padding:8px 10px;margin:7px 0;color:var(--fg-dim);font-size:12px}
.dmsg.md a{color:var(--phosphor)}
.entry .detail .phead{color:var(--ops);font-size:12px;margin:12px 0 6px}
.part{border:1px solid var(--line);margin:5px 0;background:var(--panel-2)}
.part>summary{cursor:pointer;padding:6px 10px;font-size:12.5px;color:var(--fg);list-style:none}
.part>summary::-webkit-details-marker{display:none}
.part>summary::before{content:"▸ ";color:var(--ops)}
.part[open]>summary::before{content:"▾ "}
.part>summary:hover{background:rgba(103,184,214,.06)}
.part .pkind{font-size:10px;letter-spacing:.4px;padding:0 5px;border:1px solid var(--line);color:var(--fg-dim);margin-right:6px}
.part .pkind.ai{color:var(--phosphor);border-color:var(--phosphor-dim)}
.part .pkind.user{color:var(--amber);border-color:var(--amber-dim)}
.part .pmeta{color:var(--fg-dim)}
.part .dmsg{margin:0 10px 8px}
.part .phead{padding:0 10px}
.part .part{margin:5px 10px}
.more{padding:12px 0;color:var(--fg-dim);font-size:12.5px}
.pager{display:flex;align-items:center;gap:16px;padding:14px 0 2px;color:var(--fg-dim);font-size:12.5px}
.pager span{min-width:160px}
.pager button{cursor:pointer;background:transparent;border:1px solid var(--phosphor-dim);color:var(--phosphor);
  font-family:var(--mono);font-size:12.5px;padding:5px 12px}
.pager button:hover:not([disabled]){background:var(--phosphor);color:var(--ink)}
.pager button[disabled]{opacity:.35;cursor:default;border-color:var(--line);color:var(--fg-dim)}

footer{margin-top:46px;padding-top:22px;border-top:1px dashed var(--line);color:var(--fg-dim);font-size:12.5px;
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
</style>
</head>
<body>
<div id="neterror" class="neterror" hidden>⚠ Can't reach the aGiT dashboard server — it may have been stopped (Ctrl-C in the terminal). Showing the last loaded data; retrying…</div>
<div class="wrap">
  <header>
    <div class="brand"><span class="a">a</span>GiT<span class="sub">&nbsp;dashboard</span></div>
    <div class="meta"><span class="tag">repo</span> <b>__REPO__</b> &nbsp;·&nbsp; <span class="tag">branch</span> <b>__BRANCH__</b> &nbsp;·&nbsp; <span id="genat"></span></div>
  </header>

  <div class="controls">
    <span class="prompt">&gt; filter</span>
    <div class="field"><label for="f-author">committer</label><select id="f-author"></select></div>
    <div class="field"><label for="f-backend">backend</label><select id="f-backend"></select></div>
    <div class="field"><label for="f-model">model</label><select id="f-model"></select></div>
    <div class="field"><label for="f-period">period</label><select id="f-period">
      <option value="">all time</option>
      <option value="1">last 24 hours</option>
      <option value="7">last 7 days</option>
      <option value="30">last 30 days</option>
      <option value="90">last 90 days</option>
      <option value="custom">custom range</option>
    </select></div>
    <div class="field"><label for="f-from">from</label><input type="date" id="f-from"></div>
    <div class="field"><label for="f-to">to</label><input type="date" id="f-to"></div>
    <button class="reset" id="reset">reset</button>
    <span class="scope" id="scope"></span>
  </div>

  <h2 class="section">overview</h2>
  <div class="cards" id="cards"></div>

  <h2 class="section">activity over time</h2>
  <div class="panel chartpanel">
    <div class="chart-head">
      <div class="legend" id="ts-legend"></div>
      <div class="gran"><label for="ts-gran">per</label><select id="ts-gran">
        <option value="hour">hour</option>
        <option value="day">day</option>
        <option value="week">week</option>
        <option value="month">month</option>
      </select></div>
    </div>
    <div class="chartwrap"><canvas id="ts-canvas" title="Scroll to zoom the time axis · drag to pan · double-click to reset"></canvas><div class="tip" id="ts-tip" hidden></div></div>
    <div class="chart-empty" id="ts-empty" hidden>no dated commits in view</div>
  </div>

  <h2 class="section">code changes &amp; tokens</h2>
  <div class="split">
    <div class="panel" id="lines"></div>
    <div class="panel" id="tokens"></div>
  </div>

  <h2 class="section">by backend</h2>
  <div class="panel" id="by-backend"></div>

  <h2 class="section">by model</h2>
  <div class="panel" id="by-model"></div>

  <h2 class="section">by committer</h2>
  <div class="panel" id="by-committer"></div>

  <h2 class="section">possible loops</h2>
  <div class="panel" id="loops"></div>

  <h2 class="section">commit log</h2>
  <div class="log" id="commitlog"></div>

  <footer>
    <span>aGiT · agent + git · metrics from commit metadata</span>
    <span id="count"></span>
  </footer>
</div>

<script type="application/json" id="agit-data">__DATA__</script>
<script>
"use strict";
// The page embeds an INITIAL payload (unfiltered aggregates + first log page)
// for an instant first paint, then talks to the server: /data for the metric
// panels under the active filters, /log for one page of the commit log. The
// browser never holds every commit's message/trace/constituents — only the
// current page — so memory stays bounded no matter how deep the history is.
const INIT = JSON.parse(document.getElementById("agit-data").textContent);
const PAGE_SIZE = INIT.page_size || 50;
let HEAD = INIT.head, AGG = INIT.agg, LOGPAGE = INIT.log, OPTIONS = INIT.options, GENERATED = INIT.generated_at;
let TS = INIT.timeseries || {t:[]};  // per-period series for the activity-over-time plot
let LOG_ENTRIES = [];  // entries of the page currently rendered (for toggleDetail)

// The plottable series and which are currently shown. Each is normalised to its
// own peak so commits, lines, and tokens (orders of magnitude apart) share one
// plot; the legend shows the period total and the hover tooltip the real
// per-period value.
const SERIES = [
  {key:"commits", label:"commits", color:"#3dffa0"},
  {key:"ai_lines", label:"AI lines", color:"#ffb454"},
  {key:"output_tokens", label:"output tokens", color:"#67b8d6"},
  {key:"input_tokens", label:"input tokens", color:"#ff6b6b"},
];
const tsOn = {commits:true, ai_lines:true, output_tokens:true, input_tokens:false};
let tsHover = -1;     // hovered bucket index, or -1
let tsView = null;    // visible x window as [loIdx, hiIdx] floats; null = full range
let tsDrag = null;    // in-progress pan: {x, lo, hi}; null when not dragging

const AI_KINDS = new Set(["agent","covered","agent-merge"]);
const KIND_LABEL = {"agit-ops":"aGiT-ops","agent-merge":"agent-merge"};
const TOKEN_ORDER = [["input","input"],["output","output"],["reasoning","reasoning"],
  ["cache_read","cache read"],["cache_write","cache write"],
  ["subagent_input","subagent input"],["subagent_output","subagent output"],
  ["subagent_cache_read","subagent cache read"],["subagent_cache_write","subagent cache write"],
  ["summary_input","summarizer input"],["summary_output","summarizer output"]];
const REFRESH_MS = 5000, DAY = 86400;

const state = {author:"", backend:"", model:"", fromTs:0, toTs:0, granularity:(INIT.timeseries&&INIT.timeseries.granularity)||"day"};
// Only a page served over http(s) has a backend to reach; a file:// snapshot has
// none, so it must never raise a false "server unreachable" alarm.
const LIVE = location.protocol.indexOf("http") === 0;
const $ = id => document.getElementById(id);
const fmt = n => (n||0).toLocaleString("en-US");
const pct = (a,b) => b ? (a/b*100).toFixed(1)+"%" : "0%";
const esc = s => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const kfmt = n => { n=n||0; return n>=1000 ? (n/1000).toFixed(n>=10000?0:1)+"k" : ""+n; };
function setOffline(on){ const el=$("neterror"); if(el) el.hidden = !on; }

function qs(extra){
  const p = new URLSearchParams();
  if(state.author) p.set("author", state.author);
  if(state.backend) p.set("backend", state.backend);
  if(state.model) p.set("model", state.model);
  if(state.fromTs) p.set("from", state.fromTs);
  if(state.toTs) p.set("to", state.toTs);
  if(state.granularity) p.set("granularity", state.granularity);
  for(const k in (extra||{})) p.set(k, extra[k]);
  return p.toString();
}
async function loadAgg(){
  try{ const r = await fetch("data?"+qs(), {cache:"no-store"}); if(r.ok){
    const d = await r.json(); HEAD=d.head; AGG=d.agg; OPTIONS=d.options; GENERATED=d.generated_at;
    TS = d.timeseries || {t:[]};
    setOffline(false); return true; } }
  catch(e){ if(LIVE) setOffline(true); }  // network failure ⇒ server unreachable
  return false;
}
async function loadLog(offset){
  try{ const r = await fetch("log?"+qs({offset:offset||0, limit:PAGE_SIZE}), {cache:"no-store"});
    if(r.ok){ LOGPAGE = await r.json(); setOffline(false); return true; } }
  catch(e){ if(LIVE) setOffline(true); }
  return false;
}

// --- minimal Markdown for the expanded commit message ---
function md(src){
  const lines = (src||"").replace(/\r\n/g,"\n").split("\n");
  const inline = t => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  let html="", inCode=false, code=[], inList=false;
  const closeList = () => { if(inList){ html+="</ul>"; inList=false; } };
  for(const raw of lines){
    if(raw.trimStart().startsWith("```")){
      if(inCode){ html+="<pre class=\"md-code\">"+esc(code.join("\n"))+"</pre>"; code=[]; inCode=false; }
      else { closeList(); inCode=true; }
      continue;
    }
    if(inCode){ code.push(raw); continue; }
    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if(h){ closeList(); const lvl=Math.min(6,h[1].length+2); html+=`<h${lvl} class="md-h">${inline(h[2])}</h${lvl}>`; continue; }
    const li = raw.match(/^\s*[-*+]\s+(.*)$/);
    if(li){ if(!inList){ html+="<ul>"; inList=true; } html+="<li>"+inline(li[1])+"</li>"; continue; }
    if(raw.trim()===""){ closeList(); continue; }
    html += "<p>"+inline(raw)+"</p>";
  }
  if(inCode){ html+="<pre class=\"md-code\">"+esc(code.join("\n"))+"</pre>"; }
  closeList();
  return html;
}

function barRow(name, sub, value, max, numHtml, amber){
  const w = max ? Math.max(2, value/max*100) : 0;
  // A long name is ellipsized to keep the row tidy, but its full text (and the
  // sub-label) is always available on hover via the title attribute.
  const title = esc(name) + (sub ? " — " + esc(sub) : "");
  return `<div class="row"><div class="name" title="${title}">${esc(name)}${sub?` <small>${esc(sub)}</small>`:""}</div>`+
    `<div class="bar"><i class="${amber?"amber":""}" style="width:${w}%"></i></div>`+
    `<div class="num">${numHtml}</div></div>`;
}
function card(label, value, note, amber){
  return `<div class="card"><div class="label">${esc(label)}</div>`+
    `<div class="value ${amber?"amber":""}">${value}</div><div class="note">${esc(note||"")}</div></div>`;
}
function tokenBrief(t){
  if(!t) return "";
  const parts=[];
  if(t.output) parts.push(`<span class="tok out">${kfmt(t.output)} out</span>`);
  if(t.input) parts.push(`<span class="tok">${kfmt(t.input)} in</span>`);
  if(t.cache_read) parts.push(`<span class="tok dim">${kfmt(t.cache_read)} cache</span>`);
  return parts.length ? `<span class="lc">${parts.join(" ")}</span>` : "";
}

function renderAgg(){
  const total = AGG.total, tracked = AGG.tracked;
  const ai = {ins:AGG.ai_lines[0], del:AGG.ai_lines[1]}; ai.total = ai.ins+ai.del;
  const nt = {ins:AGG.nontracked_lines[0], del:AGG.nontracked_lines[1]}; nt.total = nt.ins+nt.del;
  const allLines = ai.total + nt.total, tok = AGG.tokens, eff = AGG.efficiency, kinds = k => AGG.kinds[k]||0;

  $("genat").textContent = "updated " + GENERATED;
  $("scope").innerHTML = state.author ? `scope: <b>${esc(state.author)}</b>` : `scope: <b>entire team</b>`;
  $("count").textContent = `${fmt(total)} commits in view`;

  $("cards").innerHTML = [
    card("commits", fmt(total), `${fmt(tracked)} via aGiT`),
    card("aGiT coverage", pct(tracked,total), `${fmt(total-tracked)} non-tracked`, true),
    card("aGiT-tracked AI lines", "+"+fmt(ai.ins), `−${fmt(ai.del)} · ${pct(ai.total, allLines)} of changes`),
    card("non-tracked lines", "+"+fmt(nt.ins), `−${fmt(nt.del)} · not tracked as AI`, true),
    card("output tokens", fmt(tok.output||0), `${fmt(tok.input||0)} input`),
    card("efficiency", eff===null?"—":eff.toFixed(1), "AI lines / 1k output tok", true),
  ].join("");

  const lineRow = (label, sub, v, amber) =>
    `<div class="row"><div class="name" title="${esc(label)} — ${esc(sub)}">${label} <small>${sub}</small></div>`+
      `<div class="bar"><i class="${amber?"amber":""}" style="width:${allLines?v.total/allLines*100:0}%"></i></div>`+
      `<div class="num"><b>+${fmt(v.ins)}</b> / −${fmt(v.del)}</div></div>`;
  const kc = (label, key, tip) => `<span class="kc" title="${tip}">${label} <b>${kinds(key)}</b></span>`;
  $("lines").innerHTML =
    lineRow("aGiT-tracked AI", "agent + covered + merge", ai, false) +
    lineRow("Non-tracked", "user + plain commits", nt, true) +
    `<div class="kindcounts"><span class="klabel">commits by kind:</span> `+
      kc("agent", "agent", "Commits aGiT made from the agent's work") + " · " +
      kc("covered", "covered", "Backend-made commits an aGiT cover commit accounts for") + " · " +
      kc("merge", "agent-merge", "Integration merges whose conflicts an agent resolved") + " · " +
      kc("aGiT-ops", "agit-ops", "aGiT's own integration merge commits") + " · " +
      kc("user", "user", "User commits made through aGiT") + " · " +
      kc("untracked", "untracked", "Commits with no aGiT metadata (made outside aGiT)") +
    `</div>`;

  const shown = TOKEN_ORDER.filter(([k])=>tok[k]);
  // Token kinds span orders of magnitude (cache reads dwarf everything), so a
  // linear bar would shrink the small kinds to invisible slivers. Scale the bar
  // widths by log10 instead; the numbers shown on each row remain the real counts.
  const logTok = v => Math.log10((v||0)+1);
  const maxLog = Math.max(1, ...shown.map(([k])=>logTok(tok[k])));
  $("tokens").innerHTML = shown.length
    ? `<div class="hint">bar widths are log-scaled</div>` +
      shown.map(([k,label]) => barRow(label, "", logTok(tok[k]), maxLog, `<b>${fmt(tok[k])}</b>`, k==="output")).join("")
    : `<div class="empty">no token metadata recorded</div>`;

  $("by-backend").innerHTML = groupPanel(AGG.by_backend);
  $("by-model").innerHTML = groupPanel(AGG.by_model);

  const comm = Object.entries(AGG.by_committer).map(([name,b]) => [name, {
    commits:b.commits, agit:b.agit_commits||0,
    ai:(b.ai_insertions||0)+(b.ai_deletions||0), nt:(b.nontracked_insertions||0)+(b.nontracked_deletions||0)}]);
  const maxC = Math.max(1, ...comm.map(([,b])=>b.ai));
  comm.sort((a,b)=>b[1].ai-a[1].ai || b[1].commits-a[1].commits);
  $("by-committer").innerHTML = comm.length
    ? comm.map(([name,b]) => barRow(name, `${b.commits} commits · ${b.agit} via aGiT`, b.ai, maxC,
        `AI-driven <b>${fmt(b.ai)}</b> · non-tracked ${fmt(b.nt)}`)).join("")
    : `<div class="empty">no commits</div>`;

  $("loops").innerHTML = AGG.loops.length
    ? AGG.loops.map(l => {
        const where = l.within ? `within commit ${l.shas[0]}` : `${l.shas.length} commits ${l.shas[0]}..${l.shas[l.shas.length-1]}`;
        return `<div class="loop"><span class="where">${esc(where)}</span> — <span class="q">"${esc(l.prompt)}"</span>`+
          (l.output?` <span class="cost">${fmt(l.output)} output tokens</span>`:"")+`</div>`;
      }).join("")
    : `<div class="empty">none detected</div>`;

  tsHover = -1;
  renderTimeseries();
}
function groupPanel(groups){
  const entries = Object.entries(groups).sort((a,b)=>b[1].commits-a[1].commits ||
    ((b[1].insertions+b[1].deletions)-(a[1].insertions+a[1].deletions)));
  if(!entries.length) return `<div class="empty">no agent commits</div>`;
  const max = Math.max(1, ...entries.map(([,b])=>b.insertions+b.deletions));
  return entries.map(([label,b]) =>
    barRow(label, `${b.commits} commits`, b.insertions+b.deletions, max,
      `+${fmt(b.insertions)}/−${fmt(b.deletions)}${b.output_tokens?` · <b>${fmt(b.output_tokens)}</b> tok`:""}`)).join("");
}

// --- activity-over-time plot (multi-series, per-period, normalised, toggleable) ---
// A bucket label, formatted to the active granularity (the bucket start).
function tsLabel(e){
  const iso = new Date(e*1000).toISOString(), g = TS.granularity||"day";
  if(g==="hour") return iso.slice(0,13).replace("T"," ")+":00";
  if(g==="month") return iso.slice(0,7);
  return iso.slice(0,10);
}

function renderLegend(){
  $("ts-legend").innerHTML = SERIES.map(s => {
    const arr = TS[s.key]||[], total = arr.reduce((a,b)=>a+(b||0),0);  // sum across periods
    return `<button class="lg${tsOn[s.key]?"":" off"}" data-key="${esc(s.key)}" style="--c:${s.color}">`+
      `<span class="sw"></span>${esc(s.label)} <b>${kfmt(total)}</b></button>`;
  }).join("");
}

// The visible x window as a clamped [lo, hi] pair of (fractional) bucket indices.
// null tsView ⇒ the whole range; zoom/pan narrow it without refetching.
function tsBounds(){
  const n = (TS.t||[]).length;
  if(n<=1 || !tsView) return [0, Math.max(0, n-1)];
  let lo = Math.max(0, Math.min(tsView[0], n-1));
  let hi = Math.max(lo, Math.min(tsView[1], n-1));
  return [lo, hi];
}
const PAD_L=10, PAD_R=10;
function tsPlotW(){ return Math.max(1, $("ts-canvas").parentElement.clientWidth - PAD_L - PAD_R); }
// Screen x (CSS px) of bucket index i within the current window.
function tsXAt(i){ const [lo,hi]=tsBounds(), span=hi-lo; return PAD_L + (span<=0 ? tsPlotW()/2 : (i-lo)/span*tsPlotW()); }
// Bucket index nearest a screen x (CSS px from the canvas left edge).
function tsIndexAt(px){ const [lo,hi]=tsBounds(), rel=Math.max(0,Math.min(1,(px-PAD_L)/tsPlotW())); return Math.round(lo+rel*(hi-lo)); }

function renderChart(){
  const cv = $("ts-canvas"), t = TS.t||[], has = t.length>0;
  $("ts-empty").hidden = has;
  cv.parentElement.style.display = has ? "" : "none";
  if(!has) return;
  const wrap = cv.parentElement, dpr = window.devicePixelRatio||1;
  const cssW = wrap.clientWidth, cssH = wrap.clientHeight;
  cv.width = Math.round(cssW*dpr); cv.height = Math.round(cssH*dpr);
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,cssW,cssH);
  const padT=12, padB=22, W=cssW-PAD_L-PAD_R, H=cssH-padT-padB, n=t.length;
  const [lo,hi] = tsBounds(), span = hi-lo;
  const xAt = i => PAD_L + (span<=0 ? W/2 : (i-lo)/span*W);
  ctx.strokeStyle="rgba(29,42,33,.9)"; ctx.lineWidth=1;
  for(let g=0; g<=4; g++){ const y=padT+g/4*H; ctx.beginPath(); ctx.moveTo(PAD_L,y); ctx.lineTo(PAD_L+W,y); ctx.stroke(); }
  // Clip to the plot area so a zoomed-in window doesn't paint points outside it.
  ctx.save(); ctx.beginPath(); ctx.rect(PAD_L,padT,W,H); ctx.clip();
  // Each series is normalised to its own peak so all of them fit one plot. Values
  // are per-period activity; a marker is drawn at each visible bucket (when not
  // too dense) so a single/sparse period — or a zoomed-in view — stays legible.
  ctx.lineWidth=2; ctx.lineJoin="round";
  const dots = span<=60;
  for(const s of SERIES){
    if(!tsOn[s.key]) continue;
    const arr = TS[s.key]||[], max = Math.max(1, ...arr);
    ctx.strokeStyle=s.color; ctx.shadowColor=s.color; ctx.shadowBlur=6;
    ctx.beginPath();
    arr.forEach((v,i)=>{ const x=xAt(i), y=padT+H-(v/max)*H; i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
    ctx.stroke();
    if(dots){
      ctx.fillStyle=s.color;
      arr.forEach((v,i)=>{ const x=xAt(i), y=padT+H-(v/max)*H; ctx.beginPath(); ctx.arc(x,y,2.5,0,Math.PI*2); ctx.fill(); });
    }
  }
  if(tsHover>=lo-1e-9 && tsHover<=hi+1e-9 && tsHover>=0 && tsHover<n){
    const x = xAt(tsHover);
    ctx.strokeStyle="rgba(207,231,216,.28)"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,padT); ctx.lineTo(x,padT+H); ctx.stroke();
    for(const s of SERIES){
      if(!tsOn[s.key]) continue;
      const arr = TS[s.key]||[], max = Math.max(1, ...arr);
      const y = padT+H-((arr[tsHover]||0)/max)*H;
      ctx.fillStyle=s.color; ctx.shadowColor=s.color; ctx.shadowBlur=8;
      ctx.beginPath(); ctx.arc(x,y,3,0,Math.PI*2); ctx.fill(); ctx.shadowBlur=0;
    }
  }
  ctx.restore(); ctx.shadowBlur=0;
  // Axis labels reflect the *visible* window's first and last bucket.
  ctx.fillStyle="#7e998a"; ctx.font="11px IBM Plex Mono, monospace";
  const li = Math.max(0, Math.round(lo)), ri = Math.min(n-1, Math.round(hi));
  ctx.textAlign="left";  ctx.fillText(tsLabel(t[li]), PAD_L, cssH-7);
  if(ri>li){ ctx.textAlign="right"; ctx.fillText(tsLabel(t[ri]), PAD_L+W, cssH-7); }
  // A small "zoomed" cue when the window is narrower than the full range.
  if(tsView){ ctx.textAlign="center"; ctx.fillStyle="#67b8d6";
    ctx.fillText("zoomed — double-click to reset", cssW/2, cssH-7); }
}

function showTip(i){
  const tip = $("ts-tip"), t = TS.t||[];
  const live = SERIES.filter(s=>tsOn[s.key]);
  const [lo,hi] = tsBounds();
  if(i<lo-1e-9 || i>hi+1e-9 || i<0 || i>=t.length || !live.length){ tip.hidden = true; return; }
  const rows = live.map(s => {
    const arr = TS[s.key]||[];
    return `<div class="tr" style="--c:${s.color}"><span class="sw"></span>${esc(s.label)}<b>${fmt(arr[i]||0)}</b></div>`;
  }).join("");
  tip.innerHTML = `<div class="td">${tsLabel(t[i])}</div>${rows}`;
  const wrap = $("ts-canvas").parentElement, x = tsXAt(i);
  tip.style.left = Math.max(58, Math.min(wrap.clientWidth-58, x))+"px";
  tip.style.top = "4px";
  tip.hidden = false;
}

// Narrow/widen tsView to [nlo, nhi], clamped into [0, n-1]; clearing it to null
// (full range) when the window covers everything.
function tsSetWindow(nlo, nhi, n){
  if(nlo<0){ nhi -= nlo; nlo = 0; }
  if(nhi>n-1){ nlo -= (nhi-(n-1)); nhi = n-1; }
  nlo = Math.max(0, nlo);
  tsView = (nhi-nlo >= n-1-1e-9) ? null : [nlo, nhi];
}

function onChartMove(e){
  const t = TS.t||[]; if(!t.length) return;
  const rect = $("ts-canvas").getBoundingClientRect();
  const px = e.clientX-rect.left, n = t.length;
  if(tsDrag){  // panning: shift the window opposite the drag
    const span = tsDrag.hi-tsDrag.lo, ddx = (e.clientX-tsDrag.x)/tsPlotW()*span;
    tsSetWindow(tsDrag.lo-ddx, tsDrag.hi-ddx, n);
    tsHover=-1; $("ts-tip").hidden=true; renderChart(); return;
  }
  const i = n<=1 ? 0 : tsIndexAt(px);
  if(i!==tsHover){ tsHover=i; renderChart(); }
  showTip(i);
}
function onChartLeave(){ if(tsDrag) return; tsHover=-1; $("ts-tip").hidden=true; renderChart(); }

function onChartWheel(e){
  const n = (TS.t||[]).length; if(n<=1) return;
  e.preventDefault();
  const [lo,hi] = tsBounds(), rect = $("ts-canvas").getBoundingClientRect();
  const rel = Math.max(0, Math.min(1, (e.clientX-rect.left-PAD_L)/tsPlotW()));
  const focus = lo + rel*(hi-lo);                       // bucket under the cursor
  let span = Math.max(1, Math.min(n-1, (hi-lo)*(e.deltaY<0?0.8:1.25)));  // zoom in/out
  tsSetWindow(focus-rel*span, focus-rel*span+span, n);  // keep the cursor anchored
  tsHover=-1; renderChart();
}
function onChartDown(e){
  const n = (TS.t||[]).length; if(n<=1) return;
  const [lo,hi] = tsBounds();
  tsDrag = {x:e.clientX, lo, hi};
  $("ts-canvas").style.cursor = "grabbing"; $("ts-tip").hidden = true;
}
function onChartUp(){ if(tsDrag){ tsDrag=null; $("ts-canvas").style.cursor=""; } }
function resetZoom(){ tsView=null; tsHover=-1; }
function renderTimeseries(){ renderLegend(); renderChart(); }

function renderLog(){
  const entries = LOGPAGE.entries || [];
  LOG_ENTRIES = entries;
  const total = LOGPAGE.total||0, offset = LOGPAGE.offset||0, limit = LOGPAGE.limit||PAGE_SIZE;
  const rows = entries.map((c, i) => {
    const cls = AI_KINDS.has(c.kind) ? "ai" : (c.kind==="agit-ops" ? "ops" : "nontracked");
    const badge = `<span class="badge ${cls}">${esc(KIND_LABEL[c.kind]||c.kind)}</span>`;
    const squash = (c.parts&&c.parts.length)?`<span class="squash">⧉ ${c.parts.length} squashed</span>`:"";
    const lc = (c.ins||c.del)?`<span class="lc"><span class="add">+${fmt(c.ins)}</span> <span class="rem">−${fmt(c.del)}</span></span>`:"";
    const m = c.eff_model?`<span class="lc">${esc(c.eff_model)}</span>`:"";
    return `<div class="entry ${cls}" data-i="${i}"><span class="sha">${esc(c.short)}</span>${badge}${squash}`+
      `<span class="ksub">${esc(c.subject)}</span>${lc}${tokenBrief(c.tokens)}${m}`+
      `<div class="detail" id="detail-${i}" hidden></div></div>`;
  }).join("");
  const from = total ? offset+1 : 0, to = offset+entries.length;
  const prevDis = offset<=0 ? "disabled" : "", nextDis = (offset+limit>=total) ? "disabled" : "";
  const pager = `<div class="pager"><button id="log-prev" ${prevDis}>‹ newer</button>`+
    `<span>${fmt(from)}–${fmt(to)} of ${fmt(total)} commits</span>`+
    `<button id="log-next" ${nextDis}>older ›</button></div>`;
  $("commitlog").innerHTML = (rows || `<div class="empty">no commits</div>`) + pager;
  const prev = $("log-prev"), next = $("log-next");
  if(prev) prev.onclick = async () => { if(await loadLog(Math.max(0, offset-limit))) renderLog(); };
  if(next) next.onclick = async () => { if(offset+limit<total && await loadLog(offset+limit)) renderLog(); };
}

function partsHtml(parts){
  // Squash constituents as a nested, expandable tree (native <details>, so the
  // nesting works for multiple rounds of squashing with no extra JS).
  if(!parts || !parts.length) return "";
  const items = parts.map(p => {
    const pcls = AI_KINDS.has(p.kind) ? "ai" : (p.kind==="user" ? "user" : "nt");
    const out = (p.tokens&&p.tokens.output) ? ` · ${kfmt(p.tokens.output)} out` : "";
    const mdl = p.model ? ` · ${esc(p.model)}` : "";
    return `<details class="part"><summary><span class="pkind ${pcls}">${esc(KIND_LABEL[p.kind]||p.kind)}</span> `+
      `${esc(p.subject||"(no subject)")}<span class="pmeta">${mdl}${out}</span></summary>`+
      `<div class="dmsg md">${md(p.message)}</div>${partsHtml(p.parts)}</details>`;
  }).join("");
  return `<div class="phead">squashed from ${parts.length} original commit${parts.length>1?"s":""} `+
    `— tokens &amp; models counted from these:</div>${items}`;
}
function toggleDetail(i){
  const c = LOG_ENTRIES[i], detail = $("detail-"+i);
  if(!c || !detail) return;
  if(detail.hidden){
    const link = c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">view on GitHub ↗</a>` : "";
    const span = (c.started||c.ended)
      ? `<div class="dmeta">AI conversation: ${esc(c.started||"?")} → ${esc(c.ended||"?")}</div>` : "";
    detail.innerHTML = `<div class="dhead">${esc(c.short)} ${link}</div>${span}`+
      `<div class="dmsg md">${md(c.message||"(no message recorded)")}</div>${partsHtml(c.parts)}`;
    detail.hidden = false;
  } else {
    detail.hidden = true;
  }
}

function fillSelect(id, values, allLabel, keep){
  const sel = $(id);
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  sel.value = (keep && values.includes(keep)) ? keep : "";
}
function syncFilters(){
  fillSelect("f-author", OPTIONS.committers, "— entire team —", state.author);
  fillSelect("f-backend", OPTIONS.backends, "— all backends —", state.backend);
  fillSelect("f-model", OPTIONS.models, "— all models —", state.model);
  if(!OPTIONS.committers.includes(state.author)) state.author = "";
  if(!OPTIONS.backends.includes(state.backend)) state.backend = "";
  if(!OPTIONS.models.includes(state.model)) state.model = "";
}

// A filter change refetches the aggregates and resets the log to its first page.
// The plot's zoom window is tied to the old bucket set, so reset it too.
async function applyFilters(){
  resetZoom();
  await loadAgg(); await loadLog(0);
  syncFilters(); renderAgg(); renderLog();
}
async function refresh(){
  const prev = HEAD;
  if(await loadAgg() && HEAD !== prev){  // new commits landed — refresh the view
    resetZoom();  // the bucket set changed; an old window would mis-map
    await loadLog(LOGPAGE.offset||0);
    syncFilters(); renderAgg(); renderLog();
  }
}

// --- time range ---
function dateToTs(value, endOfDay){
  if(!value) return 0;
  const ts = Date.parse(value + "T00:00:00Z")/1000;
  return isNaN(ts) ? 0 : (endOfDay ? ts + DAY - 1 : ts);
}
function applyPeriod(){
  const v = $("f-period").value;
  if(v === "" ){ state.fromTs = 0; state.toTs = 0; $("f-from").value=""; $("f-to").value=""; }
  else if(v === "custom"){ state.fromTs = dateToTs($("f-from").value,false); state.toTs = dateToTs($("f-to").value,true); }
  else { state.fromTs = Math.floor(Date.now()/1000) - (+v)*DAY; state.toTs = 0; $("f-from").value=""; $("f-to").value=""; }
}

function init(){
  syncFilters();
  $("f-author").onchange = e => { state.author = e.target.value; applyFilters(); };
  $("f-backend").onchange = e => { state.backend = e.target.value; applyFilters(); };
  $("f-model").onchange = e => { state.model = e.target.value; applyFilters(); };
  $("f-period").onchange = () => { applyPeriod(); applyFilters(); };
  const onDate = () => { $("f-period").value = "custom"; applyPeriod(); applyFilters(); };
  $("f-from").onchange = onDate;
  $("f-to").onchange = onDate;
  $("reset").onclick = () => {
    state.author=state.backend=state.model=""; state.fromTs=state.toTs=0;
    $("f-period").value=""; $("f-from").value=""; $("f-to").value="";
    applyFilters();
  };
  // Click a commit-log line to open its full message + GitHub link. Clicks
  // inside the opened detail (links, the squash <details> tree, the pager) are
  // left alone.
  $("commitlog").addEventListener("click", e => {
    if(e.target.closest("a") || e.target.closest(".detail") || e.target.closest(".pager")) return;
    const entry = e.target.closest(".entry");
    if(entry) toggleDetail(+entry.dataset.i);
  });
  // Toggle a series on/off from the legend; redraw the plot in place.
  $("ts-legend").addEventListener("click", e => {
    const btn = e.target.closest(".lg"); if(!btn) return;
    const key = btn.dataset.key; tsOn[key] = !tsOn[key];
    renderTimeseries();
  });
  // Bucket granularity: refetch the (re-bucketed) series, reset zoom, redraw plot.
  $("ts-gran").value = state.granularity;
  $("ts-gran").onchange = async e => { state.granularity = e.target.value; resetZoom(); if(await loadAgg()) renderTimeseries(); };
  // Zoom/pan the x axis over the loaded buckets: wheel zooms (anchored on the
  // cursor), drag pans, double-click resets — all client-side, no refetch.
  const cv = $("ts-canvas");
  cv.addEventListener("mousemove", onChartMove);
  cv.addEventListener("mouseleave", onChartLeave);
  cv.addEventListener("wheel", onChartWheel, {passive:false});
  cv.addEventListener("mousedown", onChartDown);
  window.addEventListener("mouseup", onChartUp);
  cv.addEventListener("dblclick", () => { resetZoom(); renderChart(); });
  // The canvas backing store is sized in px, so it must be repainted on resize.
  let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(renderChart, 120); });
  renderAgg(); renderLog();
  // Poll only when there's a live backend; the poll also clears the
  // "unreachable" banner automatically once the server is back.
  if(LIVE) setInterval(refresh, REFRESH_MS);
}
init();
</script>
</body>
</html>
"""
