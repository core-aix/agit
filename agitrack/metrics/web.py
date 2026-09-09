"""HTML dashboard for `agitrack --dashboard` (#54).

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
import re
from datetime import datetime, timedelta, timezone

from agitrack.commits import METADATA_HEADER
from agitrack.git import GitRepo
from agitrack.metrics.collect import CommitStat, Dashboard, apply_numstat_for, build_dashboard


def render_html(repo: GitRepo, ref: str = "HEAD") -> str:
    from agitrack.metrics.files import git_browser
    from agitrack.metrics.github import resolve_logins
    from agitrack.metrics.insights import build_insights, context_from_browser

    dash = build_dashboard(repo, ref, sha_logins=resolve_logins(repo))
    # Compute the efficiency insights too: this render is a SELF-CONTAINED page (static
    # export, screenshots) with no /data endpoint behind it to fill them in later.
    try:
        files, sha_paths = context_from_browser(git_browser(repo, dash.stats, ref), dash.stats)
        insights = build_insights(dash.stats, files, sha_paths)
    except Exception:
        insights = []
    return format_html(dash, shared_sessions=shared_sessions_for(repo), insights=insights)


def shared_sessions_for(repo: GitRepo, *, fetch: bool = True) -> list[dict]:
    """Sessions shared into this repo (issue #55), for the dashboard. Reads the
    local shared ref plus a throttled remote fetch so teammates' newly-shared
    sessions appear; never raises (sharing may be unconfigured/offline).

    ``fetch=False`` skips the remote fetch and reads the local ref only: the first
    page paint must never wait on the network (the fetch can take seconds on a slow
    link, and until ``/`` responds the browser shows a blank tab that no loading
    screen can cover). The recurring ``/data`` polls fetch, so shares still appear."""
    try:
        from agitrack.sessions import SharedSessionStore

        store = SharedSessionStore(repo)
    except Exception:
        return []
    # The remote fetch is a best-effort extra (others' shares); a transient failure
    # (e.g. racing a concurrent auto-share push) must NOT blank the list. It fetches into
    # mirror refs, NOT the canonical local ref, so a remote that's momentarily behind can
    # never rewind your own just-shared session — entries() then takes the newest copy of
    # each session, so its "shared" time reflects the latest share, not a stale one.
    if fetch:
        try:
            store.fetch_throttled()
        except Exception:
            pass
    try:
        return [
            {
                # "owner" is the lineage origin; "label" is the contributor-set display
                # (`<id1>+<id2>`) — one logical session shows once however many shared it.
                "owner": entry.github_id,
                "label": "+".join(entry.contributors),
                "name": entry.name,
                "model": entry.manifest.get("model"),
                "backend": entry.manifest.get("backend"),
                "updated": entry.manifest.get("updated", 0),
            }
            for entry in store.entries()
        ]
    except Exception:
        return []


def format_html(
    dash: Dashboard,
    *,
    shared_sessions: list[dict] | None = None,
    banner_html: str = "",
    backtrace: bool = False,
    insights: list[dict] | None = None,
    frm: int = 0,
) -> str:
    payload = _embed_json(
        initial_payload(dash, shared_sessions=shared_sessions, backtrace=backtrace, insights=insights, frm=frm)
    )
    repo_name = dash.repo.rstrip("/").rsplit("/", 1)[-1] or dash.repo
    # The branch is rendered client-side into the meta-line picker (from the
    # embedded payload), so there's no __BRANCH__ placeholder to substitute.
    # ``banner_html`` fills the same slot the update banner uses on the served page —
    # the backtrace view passes its "this is a reconstruction" notice here.
    # Substitute the chrome tokens FIRST and ``__DATA__`` LAST: the embedded JSON can itself
    # contain the literal placeholder strings (backtrace transcripts of aGiTrack's own source
    # mention ``__UPDATE_BANNER__``/``__REPO__``), so replacing them after the JSON is in place
    # would corrupt it.
    return _render_template(repo_name=repo_name, repo_path=dash.repo, banner_html=banner_html, payload=payload)


def _render_template(*, repo_name: str, repo_path: str, banner_html: str, payload: str) -> str:
    """Fill the page template. ``__DATA__`` goes LAST: the embedded JSON can itself contain the
    literal placeholder strings (a backtrace of aGiTrack's own source quotes ``__REPO__`` and
    friends), so substituting chrome after it would corrupt the payload."""
    from agitrack.metrics import ui

    return ui.render(
        _TEMPLATE.replace("__REPO_NAME__", _escape(repo_name))
        .replace("__REPO__", _escape(repo_path))
        .replace("__UPDATE_BANNER__", banner_html)
        .replace("__PREBOOT_CSS__", PREBOOT_CSS)
        .replace("__PREBOOT_HTML__", PREBOOT_HTML)
        .replace("__FONT_LINKS__", FONT_LINKS)
        .replace("__DATA__", payload),
        # The hub bar keeps you on the page you are reading when you switch
        # repository or view; the dashboard IS the mount root, so no sub-path.
        __UI_HUBBAR_PAGE__="",
    )


def shell_html(repo: GitRepo) -> str:
    """The page chrome with NO aggregates or commit log embedded, for an instant first
    paint on a large repo: the browser shows a loading animation, then fetches ``/data``
    and ``/log``. The expensive ``git log`` crunch happens during those fetches, behind
    the animation, instead of blocking the first paint.

    Shared sessions are cheap (a local ref read plus a throttled fetch), so they're still
    embedded for the first paint (#55). The repo path/name fill the header."""
    from agitrack.metrics.collect import _display_repo

    repo_path = _display_repo(str(repo.repo))
    payload = _embed_json({"page_size": PAGE_SIZE, "shared_sessions": shared_sessions_for(repo, fetch=False)})
    repo_name = repo_path.rstrip("/").rsplit("/", 1)[-1] or repo_path
    return _render_template(
        repo_name=repo_name, repo_path=repo_path, banner_html=_update_banner_html(repo), payload=payload
    )


def _reason_and_detail(record) -> str:
    """The "(why) what to do" tail of the update notice, with the two never said twice.

    ``error`` and ``instructions`` are recorded separately but overlap by construction: the
    failure text ends with the same manual-update guidance the instructions carry, so printing
    both produced the guidance twice in one banner — a paragraph of repeated commands where one
    line was wanted. When the reason already contains the instructions, the reason alone says
    everything."""
    reason = (record.error or "").strip()
    detail = (record.instructions or "").strip()
    if detail and detail in reason:
        detail = ""
    return (f" ({reason})" if reason else "") + "." + (f" {detail}" if detail else "")


def _needs_user_sentence(record) -> str:
    """The lead sentence of the "you have to update this yourself" notice.

    A SOURCE checkout and a PACKAGE install are updated in different units, and saying "aGiTrack
    <x> is available" for both was wrong for one of them: on a source install the recorded
    current/latest are short COMMIT HASHES (see `Updater._check_source`), so that sentence read
    "aGiTrack a1b2c3d is available", which looks like a version and says nothing about what
    changed or how far behind the checkout is. Naming the span makes it a commit, and reads the
    way `git log a1b2c3d..e4f5g6h` does."""
    from agitrack.update.updater import KIND_SOURCE

    if record.method == KIND_SOURCE:
        span = f"{record.current} → {record.latest}" if record.current else record.latest
        return f"A newer aGiTrack commit is available ({span}) and has to be pulled by you"
    return f"aGiTrack {record.latest} is available and has to be installed by you"


def _agitrack_version() -> str:
    """The version of the aGiTrack serving this request — the same string `agitrack --version`
    prints, commit included on a source checkout, because one install must not answer "which
    version is this?" two different ways depending on where it is asked. Imported lazily: this
    module is imported during aGiTrack's own startup."""
    from agitrack.versioning import version_line

    return version_line()


def _update_banner_html(repo: "GitRepo | None" = None) -> str:
    """:func:`update_notices` as the markup the page is served with.

    Always a container, even with nothing to say: the page re-renders these from its own polling
    (see ``renderUpdateNotices`` in `agitrack/metrics/ui.py`), and a notice that only ever arrives
    with the HTML is a notice that stays on screen until the tab is closed. That is how a message
    withdrawn hours ago was still being read."""
    return (
        '<div id="updatebanners">'
        + "".join(f'<div class="updatebanner">\u2b06 {_escape(text)}</div>' for text in update_notices(repo))
        + "</div>"
    )


def update_notices(repo: "GitRepo | None" = None) -> list[str]:
    """The update notices for the top of a dashboard. Empty when there is nothing to say.

    aGiTrack updates ITSELF now, so these are only the two cases the user still has to act on,
    and they call for different actions — one line covering both told people to do the wrong thing:

    1. **Install it yourself.** Self-update could not: an MSI needing elevation, a Homebrew
       install, a Windows pip upgrade, or an attempt that failed — and a newer version exists.
       The record is GLOBAL (one file per machine) but the machine's aGiTrack instances need not
       share an install, so on a repo dashboard it is shown only when it describes THIS repo's
       instance — see :func:`~agitrack.update.selfupdate.record_matches_instance`. Without a repo
       (the hub's own pages, backtrace) the global installation is the subject and it always shows.
    2. **Restart your session.** The installation is current, but a session on this repo is still
       running the code it loaded before the update (sessions are deliberately never restarted
       from under the user). Repo-specific, so only that repo's live dashboard shows it.
    """
    parts: list[str] = []
    try:
        from agitrack.update.selfupdate import read_state, record_matches_instance

        record = read_state()
        if record.needs_user and (repo is None or record_matches_instance(repo.repo, record)):
            parts.append(_needs_user_sentence(record) + _reason_and_detail(record))
    except Exception:
        pass
    if repo is not None:
        try:
            from agitrack.update.selfupdate import stale_session_kind

            # WHO is stale decides what to say. A background tracker restarts itself, so asking
            # the user to restart it is asking for work already under way — and that is exactly
            # what the page used to do, in the same breath as announcing the update, for the
            # couple of minutes it took the tracker to come back.
            stale = stale_session_kind(repo.repo)
            if stale == "background":
                parts.append(
                    "aGiTrack updated itself; the background tracker on this repo is still on the old "
                    "version and will load the new one by itself shortly. Nothing to do."
                )
            elif stale == "session":
                parts.append(
                    "aGiTrack updated itself, but the session running on this repo is still on the old "
                    "version — restart aGiTrack when convenient to load the new one."
                )
        except Exception:
            pass
    if not parts and repo is not None:
        # Nothing from the self-updater: fall back to the shared per-repo marker, which an
        # install predating self-update (or a check that ran before any attempt) may carry.
        try:
            from agitrack.update.marker import update_reminder_line

            fallback = update_reminder_line(repo.repo)
        except Exception:
            fallback = None
        if fallback:
            parts.append(fallback)
    return parts


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
                # Every committer credited (primary author + human co-authors), so
                # a co-authored commit is filterable under each of them (#54).
                "committers": dash.committers_of(stat),
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
                # Shown when a log entry opens. For a squash, the constituents are stripped
                # (they're listed separately in "parts") so the message isn't duplicated.
                "message": _main_message(stat),
                "url": (dash.commit_base + stat.sha) if dash.commit_base else "",
                # Original commits of a squash, so the log entry can expand into
                # them (recursive, for multiple rounds of squashing). Newest-first display.
                "parts": _display_parts(stat),
            }
        )

    return {
        "repo": dash.repo,
        "branch": dash.branch,
        # HEAD sha lets the live page skip re-rendering when nothing changed.
        "head": dash.stats[-1].sha if dash.stats else "",
        # The version SERVING this payload, carried on the same answer the new-commit check
        # already reads rather than on a request of its own: the page reloads when it changes
        # (see noteVersion in ui.py), because aGiTrack self-updates and restarts its daemons
        # under an open tab, leaving the old page's JS rendering a new server's data.
        "agitrack_version": _agitrack_version(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        # Only committers who contributed lines — the same set the breakdown shows, so the
        # filter never offers a name whose row is not there (see Dashboard.by_author).
        "committers": sorted(a for a in dash.committers_with_lines() if a),
        "backends": sorted({c["eff_backend"] for c in commits if c["eff_backend"]}),
        "models": sorted({c["eff_model"] for c in commits if c["eff_model"]}),
        "meta_fields": metadata_filter_fields(dash),
        "commits": commits,
    }


def _display_parts(stat: CommitStat) -> list[dict]:
    """A squash's constituents serialized for the expandable log view, ordered
    **newest-first** to match the newest-first commit log. This reorder is DISPLAY-ONLY: the
    raw commit message keeps its constituents in chronological (oldest-first) order, like any
    squash merge — only the dashboard shows the latest one at the top."""
    return [_part_payload(part) for part in reversed(stat.constituents)]


def _main_message(stat: CommitStat) -> str:
    """The commit's own message text for the detail view. For a squash this drops the appended
    constituent blocks (each already shown, in full, in the expandable parts list) so the main
    message doesn't duplicate them — it keeps only the commit's leading text before the first
    ``# aGiTrack Metadata`` block (e.g. the user's own commit subject/body for a manual-mode
    commit, or a PR title for a squash merge). A non-squash commit keeps its full message."""
    if not stat.constituents:
        return stat.message
    head, _, _ = stat.message.partition("\n" + METADATA_HEADER)
    return head.strip()


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
        # A nested squash's own message likewise drops its (separately-listed) constituents.
        "message": _main_message(part),
        "parts": _display_parts(part),  # nested squashes also expand newest-first
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
_KINDS = ("agent", "covered", "agent-merge", "user", "agitrack-ops", "untracked")


def _covers(dash: Dashboard) -> dict[str, CommitStat]:
    covers: dict[str, CommitStat] = {}
    for stat in dash.stats:
        for short in stat.covered_commits:
            covers[short] = stat
    return covers


# --------------------------------------------------------------------------- advanced filter
#
# The named filters (committer/backend/model/date) cover the questions asked most, and cover
# nothing else. Every commit already carries a full metadata block — harness version, OS,
# session name, compactions, token counts — and none of it was selectable, so "show me only what
# Claude Code 2.1.238 produced" or "only the commits from the Ubuntu box" meant reading `git log`
# by hand. The advanced filter selects on ANY recorded field, without a new named filter (and a
# new query parameter, and a new dropdown) per field.

# Above this many distinct values a field is offered as free text rather than a dropdown: a
# select holding 537 conversation anchors is not a control, it is a wall.
_FACET_VALUE_LIMIT = 100

# `eq` exact, `has` substring (case-insensitive), `ge`/`le` bounds, `between` an inclusive
# range written `LOW..HIGH`. Anything else is dropped rather than guessed at — a filter that
# silently means something other than what the URL says would misreport the slice every panel
# on the page is computed from.
_META_OPS = frozenset({"eq", "has", "ge", "le", "between"})

# What separates a range's two ends. `..` reads as a range and appears in no value any of these
# fields carries; a value that does contain it can still be bounded with `ge` and `le` separately.
RANGE_SEPARATOR = ".."

# Splits a value into digit and non-digit runs, so ordering is NATURAL rather than lexical.
# It has to be: bounds are wanted on token counts (numbers), timestamps (ISO-8601) and harness
# versions, and the three cannot share one rule otherwise — plain string order puts 2.1.9 after
# 2.1.10, and plain numeric order cannot read a timestamp at all. Before this, `ge`/`le` parsed
# both sides as floats and returned False for anything else, so a date or version bound matched
# NOTHING and looked like a filter with no results rather than a filter that could not run.
_ORDER_CHUNKS = re.compile(r"(\d+)")


def _order_key(value: str) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in _ORDER_CHUNKS.split(value) if part != "")


def _at_least(actual: str, bound: str) -> bool:
    return _order_key(actual) >= _order_key(bound)


def _at_most(actual: str, bound: str) -> bool:
    return _order_key(actual) <= _order_key(bound)


def parse_meta_filters(raw: list[str]) -> list[tuple[str, str, str]]:
    """``["system:eq:macOS 15.7.3"]`` -> ``[("system", "eq", "macOS 15.7.3")]``.

    Split at most twice, because a value legitimately contains colons (an ISO timestamp, an
    `mcp__server__tool` name). Malformed or unknown-operator entries are DROPPED: the filter
    is applied server-side to every panel, so a condition that cannot be honoured exactly must
    not be silently relaxed into one that can.
    """
    out: list[tuple[str, str, str]] = []
    for entry in raw:
        key, _, rest = (entry or "").partition(":")
        op, _, value = rest.partition(":")
        if not (key.strip() and op in _META_OPS and value != ""):
            continue
        # A range with NEITHER end filled in is a condition the reader has not written yet, so
        # it must not filter. It used to select every commit that merely HAD the field, which
        # silently shrank the view the moment `between` was picked — while the button's badge,
        # which already ignored an empty range, still said nothing was filtered.
        if op == "between" and not any(end.strip() for end in value.split(RANGE_SEPARATOR)):
            continue
        out.append((key.strip(), op, value))
    return out


def _meta_matches(stat: CommitStat, key: str, op: str, value: str) -> bool:
    actual = (stat.metadata or {}).get(key)
    if actual is None:
        return False
    if op == "eq":
        return actual == value
    if op == "has":
        return value.casefold() in actual.casefold()
    if op == "between":
        low, separator, high = value.partition(RANGE_SEPARATOR)
        if not separator:
            return False  # not a range at all; better no rows than a bound the user did not write
        low, high = low.strip(), high.strip()
        # An open end is allowed: `..500` is "up to 500" and `100..` is "from 100".
        return (not low or _at_least(actual, low)) and (not high or _at_most(actual, high))
    return _at_least(actual, value) if op == "ge" else _at_most(actual, value)


def _passes_meta(stat: CommitStat, meta: list[tuple[str, str, str]]) -> bool:
    """Conditions on DIFFERENT keys are ANDed; conditions on the SAME key are ORed.

    That is what makes the control usable as a facet: adding a second value for `system` widens
    "macOS" to "macOS or Ubuntu", while adding a `backend_version` condition narrows within it.
    ANDing everything would make two values of one field select nothing at all, which reads as
    the filter being broken.
    """
    by_key: dict[str, list[tuple[str, str, str]]] = {}
    for condition in meta:
        by_key.setdefault(condition[0], []).append(condition)
    return all(any(_meta_matches(stat, key, op, value) for key, op, value in group) for group in by_key.values())


def metadata_filter_fields(dash: Dashboard) -> list[dict]:
    """The fields the advanced filter offers, each with the values actually present.

    Built from the commits in view rather than a hard-coded list, so a field aGiTrack adds later
    (as `backend_version` was) is filterable the day it lands, with no dashboard change at all.
    """
    values: dict[str, set[str]] = {}
    for stat in dash.stats:
        for key, value in (stat.metadata or {}).items():
            values.setdefault(key, set()).add(value)
    fields = []
    for key in sorted(values):
        seen = values[key]
        numeric = all(_is_number(v) for v in seen)
        fields.append(
            {
                "key": key,
                "numeric": numeric,
                # Omitted above the cap: the page offers a text box instead, so a high-cardinality
                # field stays filterable without shipping every distinct value to the browser.
                "values": sorted(seen, key=_numeric_sort_key if numeric else str)
                if len(seen) <= _FACET_VALUE_LIMIT
                else [],
                "count": len(seen),
            }
        )
    return fields


def _is_number(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _numeric_sort_key(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _filter_stats(
    dash: Dashboard,
    *,
    author: str,
    backend: str,
    model: str,
    frm: int,
    to: int,
    meta: list[tuple[str, str, str]] | None = None,
) -> list[CommitStat]:
    covers = _covers(dash)
    out: list[CommitStat] = []
    for stat in dash.stats:
        if author and author not in dash.committers_of(stat):
            continue
        if meta and not _passes_meta(stat, meta):
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
    return Dashboard(
        repo=dash.repo,
        branch=dash.branch,
        stats=stats,
        sha_logins=dash.sha_logins,
        # Carry the email→login hint too, or the per-committer panels (built from this
        # filtered copy) lose it and show a bare name while the filter dropdown — built
        # from the original dashboard — shows the GitHub ID.
        email_logins=dash.email_logins,
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
        "token_breakdown": fd.token_breakdown,
        "line_yield": fd.lines_per_1k_output_tokens,
        "by_backend": fd.by_backend,
        "by_model": fd.by_model,
        "by_committer": fd.by_author,
    }


# Kinds whose LINES count as tracked-AI in the time series — kept in step with
# Dashboard.ai_lines. Agent-resolved merges are deliberately excluded (a merge's
# lines aren't cleanly attributable), though they remain agent-driven *commits*
# (see the JS AI_KINDS used only for commit-log colouring).
_AI_KINDS = ("agent", "covered")
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
    committers, backends, models = set(dash.committers_with_lines()), set(), set()
    for stat in dash.stats:
        eff_backend, eff_model = _effective(stat, covers)
        if eff_backend:
            backends.add(eff_backend)
        if eff_model:
            models.add(eff_model)
    return {
        "committers": sorted(c for c in committers if c),
        "backends": sorted(backends),
        "models": sorted(models),
        # Every metadata field present in these commits, so the advanced filter can offer them
        # without the page knowing any field name in advance.
        "meta_fields": metadata_filter_fields(dash),
        "branches": dash.branches,
    }


def aggregates_payload(
    dash: Dashboard,
    *,
    author: str = "",
    backend: str = "",
    model: str = "",
    meta: list[tuple[str, str, str]] | None = None,
    frm: int = 0,
    to: int = 0,
    granularity: str = DEFAULT_GRANULARITY,
) -> dict:
    """All the metric panels for the given filters — no per-commit list, so the
    response stays small no matter how large the repository is. Filter options
    come from the full history so the dropdowns never lose entries."""
    stats = _filter_stats(dash, author=author, backend=backend, model=model, frm=frm, to=to, meta=meta)
    # Full-history commit-date span (unfiltered) so the from/to date inputs can show
    # — and be bounded to — the real range the dashboard covers.
    dated = [s.timestamp for s in dash.stats if s.timestamp]
    return {
        "head": dash.stats[-1].sha if dash.stats else "",
        "branch": dash.branch,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "options": _options(dash),
        "span": {"from": min(dated), "to": max(dated)} if dated else {"from": 0, "to": 0},
        "agg": _aggregates(_filtered_dashboard(dash, stats)),
        "timeseries": _timeseries(stats, granularity=granularity),
    }


# Commit-log sort orders the user can choose (applied after the filter scope).
LOG_SORTS = ("date", "lines", "tokens")
DEFAULT_LOG_SORT = "date"


def _sorted_for_log(stats: list[CommitStat], sort: str) -> list[CommitStat]:
    """Order the filtered commits for the log. ``date`` (default) is newest-first;
    ``lines`` and ``tokens`` rank by magnitude (most changed lines / most output
    tokens) with the newest commit breaking ties. ``stats`` arrives oldest-first."""
    if sort == "lines":
        return sorted(stats, key=lambda s: (s.insertions + s.deletions, s.timestamp), reverse=True)
    if sort == "tokens":
        return sorted(stats, key=lambda s: (s.tokens.get("output", 0), s.timestamp), reverse=True)
    return list(reversed(stats))  # "date": newest first


def _log_entry(dash: Dashboard, stat: CommitStat, covers: dict[str, CommitStat]) -> dict:
    eff_backend, eff_model = _effective(stat, covers)
    return {
        "short": stat.short,
        "sha": stat.sha,  # full sha so the live page can fetch this commit's diff from /diff
        "author": dash.label_of(stat),
        "committers": dash.committers_of(stat),
        "subject": stat.subject,
        "kind": stat.kind,
        "pending": stat.pending,
        "token_anomaly": stat.token_anomaly,  # tokens excluded: re-exported history (lost watermark)
        "tracked": stat.tracked,  # backtrace: already committed with aGiTrack metadata
        "eff_backend": eff_backend,
        "eff_model": eff_model,
        "tokens": stat.tokens,
        "ins": stat.insertions,
        "del": stat.deletions,
        "ts": stat.timestamp,
        "started": stat.started_at,
        "ended": stat.ended_at,
        "message": _main_message(stat),  # squash constituents stripped (shown in "parts")
        "url": (dash.commit_base + stat.sha) if dash.commit_base else "",
        "parts": _display_parts(stat),  # newest-first display (message stays chronological)
    }


def log_page(
    dash: Dashboard,
    *,
    repo: GitRepo | None = None,
    author: str = "",
    backend: str = "",
    model: str = "",
    meta: list[tuple[str, str, str]] | None = None,
    frm: int = 0,
    to: int = 0,
    offset: int = 0,
    limit: int = PAGE_SIZE,
    sort: str = DEFAULT_LOG_SORT,
) -> dict:
    """One page of the commit log for the given filters and sort order. Only this
    page's commits carry the heavy message / squash constituents, so memory and
    payload stay bounded however deep the history is.

    When ``repo`` is given, the page's commits get their exact insertions/deletions
    fetched on demand — and *only* those commits' blobs are fetched, so a blobless
    partial clone never pulls its whole history just to render one page (the
    full-history scan in :func:`collect_commit_stats` counts from local blobs alone)."""
    stats = _filter_stats(dash, author=author, backend=backend, model=model, frm=frm, to=to, meta=meta)
    stats = _sorted_for_log(stats, sort)
    covers = _covers(dash)
    offset = max(0, offset)
    limit = max(1, min(limit, 200))
    page = stats[offset : offset + limit]
    if repo is not None and page:
        apply_numstat_for(repo, [stat.sha for stat in page], {stat.sha: stat for stat in page})
    return {
        "total": len(stats),
        "offset": offset,
        "limit": limit,
        "entries": [_log_entry(dash, stat, covers) for stat in page],
    }


# A commit id must be a bare hex object name before it is handed to git, so a crafted
# ?sha= value can never become a git option (e.g. --upload-pack) or reach the shell.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def commit_diff(repo: GitRepo, sha: str) -> dict:
    """One commit's message and the file diffs it introduced (a diffstat + unified patch),
    computed entirely from the local clone so the dashboard needs no GitHub. ``sha`` is
    validated as a hex object id before touching git.

    The message rides along because the storyline shows it FIRST and only then offers the
    file changes: one request answers both, and the static export's baked ``diff/<sha>.json``
    covers both with no extra files."""
    if not _SHA_RE.match(sha or ""):
        return {"sha": sha, "diff": "", "error": "invalid commit id"}
    try:
        patch, truncated = repo.show_commit(sha)
    except Exception:
        return {"sha": sha, "diff": "", "error": "could not read this commit's diff"}
    try:
        message = repo.commit_message(sha)
    except Exception:
        message = ""
    return {"sha": sha, "diff": patch, "truncated": truncated, "message": message}


def initial_payload(
    dash: Dashboard,
    *,
    shared_sessions: list[dict] | None = None,
    backtrace: bool = False,
    insights: list[dict] | None = None,
    frm: int = 0,
) -> dict:
    """What the page embeds for an instant first paint: unfiltered aggregates,
    the first log page, repo metadata, the page size, and any shared sessions.

    ``backtrace`` marks the payload as a historical reconstruction (``--backtrace``)
    rather than live repo status, for any client-side affordance that keys off it.
    ``insights`` (whole-history efficiency insights, never filter-scoped) render as
    the "agent efficiency" section when non-empty. ``frm`` scopes the embedded
    aggregates and first log page to commits at/after that timestamp — the static
    demo export uses it to ship a last-30-days view instead of all time."""
    return {
        "repo": dash.repo,
        "branch": dash.branch,
        "page_size": PAGE_SIZE,
        "backtrace": backtrace,
        "insights": insights or [],
        **aggregates_payload(dash, frm=frm),
        "log": log_page(dash, frm=frm),
        "shared_sessions": shared_sessions or [],
    }


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _embed_json(data: object) -> str:
    """Serialize ``data`` for embedding inside a ``<script>`` tag. Escapes ``<``, ``>`` and ``&``
    as unicode escapes so transcript content containing ``</script>``, ``<!--`` or an HTML tag
    can't break out of the script element or corrupt the JSON (the chars only ever appear inside
    JSON string values, so the escapes round-trip to the same data)."""
    return (
        json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )


# ---------------------------------------------------------------------------
# First paint. Until the whole document has arrived and its script has run, the
# browser has nothing of ours to show — and the default canvas is WHITE, so a
# dashboard opened over a remote/forwarded connection looked like a blank page
# rather than one that was loading. These two snippets fix that at the source and
# are shared with the learn page (learn.py), which has the same head.
# ---------------------------------------------------------------------------

# Placed FIRST in <head>, before the favicon, the fonts and the ~28 KB main stylesheet, and
# deliberately tiny (~700 bytes) so it lands in the first packet: the page is dark and says
# what it is doing from the moment anything is painted at all. Self-contained — no CSS
# variables, no web font, no script — because none of that has arrived yet.
PREBOOT_CSS = """<style>
html{background:#070b09}
#preboot{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:15px;background:#070b09;color:#3dffa0;text-align:center;padding:24px;
  font:400 19px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:1px}
#preboot .pbspin{width:40px;height:40px;border:3px solid #1f7a52;border-top-color:#3dffa0;
  border-radius:50%;animation:pbspin 1s linear infinite}
#preboot .pbsub{font-size:13px;color:#7e998a;letter-spacing:0;max-width:44ch}
@keyframes pbspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){#preboot .pbspin{animation:none}}
</style>"""

# The overlay itself, first thing in <body>. Needs no JavaScript to appear (it is plain markup
# under the style above), so it is up even while the rest of the document is still in flight;
# the app removes it once the chrome is on screen.
PREBOOT_HTML = """<div id="preboot">
  <span class="pbspin"></span>
  <div>loading the aGiTrack dashboard…</div>
  <div class="pbsub">this can take a moment over a remote or forwarded connection</div>
</div>"""

# Google Fonts is a CROSS-ORIGIN stylesheet, and a pending stylesheet blocks first paint: the
# browser would show nothing at all until that request completed — seconds on a slow link, and
# potentially a timeout on a host that can't reach fonts.googleapis.com. The media="print" trick
# makes it non-blocking (it is not "for this medium", so it never delays paint) and the onload
# flips it back to all, so the fonts still apply the instant they arrive. The design already
# names local monospace fallbacks, so the brief swap is a non-issue; <noscript> keeps the fonts
# for script-less browsers, where nothing is dynamic anyway.
FONT_LINKS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet"></noscript>"""


# ---------------------------------------------------------------------------
# The page. Brace-heavy CSS/JS lives verbatim in the template (no f-strings);
# only the __PLACEHOLDER__ tokens are substituted.
# ---------------------------------------------------------------------------

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aGiTrack - Dashboard · __REPO_NAME__</title>
__PREBOOT_CSS__
<!-- Inline SVG favicon (the aGiTrack wordmark mark) — the server only serves /, /data, /log, /diff, so it can't host a file. -->
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2064%2064'%3E%3Crect%20width='64'%20height='64'%20rx='13'%20fill='%23070b09'/%3E%3Ctext%20x='32'%20y='45'%20text-anchor='middle'%20font-family='ui-monospace,monospace'%20font-weight='700'%20font-size='42'%20letter-spacing='-1'%3E%3Ctspan%20fill='%23ffb454'%3Ea%3C/tspan%3E%3Ctspan%20fill='%233dffa0'%3EG%3C/tspan%3E%3C/text%3E%3C/svg%3E">
__FONT_LINKS__
<style>
__UI_TOKENS__
*{box-sizing:border-box;margin:0;padding:0}
__UI_BASE_CSS__
html{scroll-behavior:smooth}
body{background:var(--ink);color:var(--fg);font-family:var(--mono);font-size:15px;line-height:1.6}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:30;
  background:repeating-linear-gradient(0deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px);opacity:.35}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:31;
  background:radial-gradient(ellipse at 50% 12%,transparent 60%,rgba(0,0,0,.55) 100%)}
::selection{background:var(--phosphor);color:var(--ink)}
a{color:var(--phosphor);text-decoration:none;border-bottom:1px solid var(--phosphor-dim)}
/* This page answers a hovered link by INVERTING it — phosphor block, ink text — which is
   affordance enough on its own. The shared rule's underline (for pages whose links are plain
   text in a sentence) drew a line through the middle of that block as well, and the story and
   learn entries in the header wear it most visibly. The footer opts back in for its own links. */
a:hover{color:var(--ink);background:var(--phosphor);text-decoration:none}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 80px}
/* Initial-load animation. On a large repo the server sends the page chrome with no
   aggregates/log embedded; this loader shows while the browser fetches /data and /log,
   and `body.booting` hides the (still-empty) data sections so they don't flash.
   The loader rules are scoped to the ELEMENT id: the state class on <body> is also
   "booting", and a bare `.booting{display:none}` matched the body itself — hiding the
   whole page, loader included, for the entire /data crunch (a black tab, no message). */
#booting{display:none;flex-direction:column;align-items:center;justify-content:center;
  gap:18px;padding:110px 0 130px;text-align:center}
body.booting #booting{display:flex}
body.booting .wrap>*:not(header):not(#booting){display:none}
#booting .spin{width:48px;height:48px;border:3px solid var(--phosphor-dim);border-top-color:var(--phosphor);
  border-radius:50%;animation:spin .8s linear infinite;box-shadow:0 0 18px rgba(61,255,160,.25)}
#booting .bmsg{font-family:var(--display);font-size:32px;color:var(--phosphor);letter-spacing:1px;
  text-shadow:0 0 18px rgba(61,255,160,.35)}
#booting .bdots::after{content:"";animation:bdots 1.4s steps(4,end) infinite}
@keyframes bdots{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
#booting .bsub{font-size:13px;color:var(--fg-dim)}
/* The real progress bar, shown only once the server reports a build it can measure (see
   metrics/progress.py). Hidden until then, so a repo that loads instantly never flashes a bar
   at 0%. The [hidden] rule is explicit because the width/height rules below would otherwise
   out-specify the browser's own `[hidden]{display:none}`. */
#booting .bbar{width:min(420px,70vw);height:8px;border:1px solid var(--phosphor-dim);overflow:hidden}
#booting .bbar[hidden]{display:none}
#booting .bbar i{display:block;height:100%;width:0;background:var(--phosphor);
  box-shadow:0 0 12px rgba(61,255,160,.45);transition:width .25s linear}
/* Every top-of-page notice lives in ONE sticky strip so they STACK instead of covering each
   other. They used to position themselves independently — the offline banner `fixed` at z-40,
   the backtrace/update banners `sticky` at z-60 — so on a backtrace dashboard the higher banner
   painted straight over "can't reach the server", hiding it at the one moment it matters most.
   (Two sticky banners at top:0 likewise converged onto each other once the page was scrolled.)
   Inside the strip the children are back in normal flow, so they stack top to bottom in DOM
   order, a hidden one takes no space, and each one's downward shadow is covered by the sibling
   below it — only the last visible banner casts onto the page. */
.topbanners{position:sticky;top:0;z-index:60}
.topbanners>*{position:static}
.neterror{background:#3a0f0f;color:#ffd5d5;
  border-bottom:2px solid var(--red);padding:10px 18px;font-size:13px;text-align:center;
  box-shadow:0 6px 20px rgba(0,0,0,.55);animation:rise .25s ease}
/* Efficiency insights: one card per detected inefficiency category. The left border and
   the small badge carry the severity; the suggestion line is the actionable part, set off
   from the evidence by a dashed rule. Hidden entirely (with its heading) when empty. */
.insight{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--amber);
  border-radius:10px;padding:12px 16px;margin:0 0 10px}
.insight.high{border-left-color:var(--red)}
.insight.info{border-left-color:var(--fg-dim)}
.insight .ihead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.insight .isev{font-size:10px;text-transform:uppercase;letter-spacing:1px;border:1px solid var(--line);
  border-radius:4px;padding:1px 6px;color:var(--amber)}
.insight.high .isev{color:var(--red)}
.insight.info .isev{color:var(--fg-dim)}
.insight .isum{color:var(--fg-dim);font-size:13px}
.insight .ievidence{margin:8px 0 8px 18px;font-size:13px}
.insight .ievidence li{margin:3px 0}
.insight .isuggest{font-size:13px;color:var(--phosphor);border-top:1px dashed var(--line);
  padding-top:8px;margin-top:2px}
.insightnote{font-size:12px;color:var(--fg-dim);margin:0 0 8px}
/* The learn call-to-action: the prominent, self-explanatory entry to the /learn page,
   sitting with the efficiency insights (same traces, next step: act on them). */
.learncta{display:flex;align-items:center;gap:18px;margin:0 0 16px;padding:16px 20px;
  border:1px solid var(--phosphor-dim);border-bottom:1px solid var(--phosphor-dim);border-radius:6px;
  background:linear-gradient(135deg,rgba(61,255,160,.10),rgba(61,255,160,.02) 55%,rgba(103,184,214,.07));
  transition:border-color .15s,transform .15s,box-shadow .15s}
.learncta:hover{background:linear-gradient(135deg,rgba(61,255,160,.16),rgba(61,255,160,.05) 55%,rgba(103,184,214,.10));
  color:var(--fg);border-color:var(--phosphor);transform:translateY(-2px);box-shadow:0 8px 28px rgba(61,255,160,.14)}
.learncta .lc-icon{font-size:32px;flex:none}
.learncta .lc-text{flex:1;font-size:13px;color:var(--fg-dim);line-height:1.55}
.learncta .lc-text b{display:block;color:var(--phosphor);font-family:var(--display);font-size:21px;
  font-weight:400;letter-spacing:.5px;margin-bottom:2px}
.learncta .lc-btn{flex:none;font-family:var(--display);font-size:21px;letter-spacing:.5px;color:var(--phosphor);
  border:1px solid var(--phosphor-dim);border-radius:4px;padding:8px 18px;white-space:nowrap}
.learncta:hover .lc-btn{background:var(--phosphor);color:var(--ink);border-color:var(--phosphor)}
@media (max-width:640px){.learncta{flex-wrap:wrap}.learncta .lc-btn{margin-left:50px}}
/* The storyline call-to-action, above the log: the same shape as the learn card in amber,
   so the two entries read as siblings without competing for the same green. */
.storycta{border-color:var(--amber-dim);
  background:linear-gradient(135deg,rgba(255,180,84,.10),rgba(255,180,84,.02) 55%,rgba(103,184,214,.06))}
.storycta:hover{border-color:var(--amber);box-shadow:0 8px 28px rgba(255,180,84,.14);
  background:linear-gradient(135deg,rgba(255,180,84,.16),rgba(255,180,84,.05) 55%,rgba(103,184,214,.10))}
.storycta .lc-text b,.storycta .lc-btn{color:var(--amber)}
.storycta .lc-btn{border-color:var(--amber-dim)}
.storycta:hover .lc-btn{background:var(--amber);color:var(--ink);border-color:var(--amber)}
.insight.good{border-left-color:var(--phosphor)}
.insight.good .isev{color:var(--phosphor)}
/* Trend chip: the same metric measured on the earlier vs the later half of the window,
   so a habit that is getting better says so. Lower is always better. */
.itrend{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:var(--fg-dim)}
.itrend.better{color:var(--phosphor);border-color:var(--phosphor-dim)}
.itrend.worse{color:var(--red);border-color:var(--red)}
.insightmore{margin-top:4px}
.insightmore summary{cursor:pointer;color:var(--fg-dim);font-size:12px;padding:6px 0}
.insightmore summary:hover{color:var(--phosphor)}
/* The backtrace notice is a frozen top strip — always visible, like the sticky filter bar.
   Opaque so page content scrolls cleanly beneath it; the filter bar's top offset is set to this
   strip's height in JS so the two stack instead of overlapping. */
__UI_BANNER_CSS__
__UI_HUBBAR_CSS__
/* The uncommitted-work notice. Amber like every other "this is not the whole story" surface on
   these pages (the backtrace strip, the update banner), so the colour means one thing throughout. */
.pending{display:flex;gap:11px;align-items:flex-start;margin:18px 0 0;padding:11px 14px;
  background:rgba(255,180,84,.07);border:1px solid var(--amber-dim);color:var(--warn);
  font-size:12.5px;line-height:1.55}
.pending .pd-icon{flex:none;font-size:14px;line-height:1.4}
.pending .pd-text{flex:1}
.pending .pd-link{flex:none;color:var(--phosphor);border-bottom:1px solid var(--phosphor-dim);white-space:nowrap}
.pending .pd-link:hover{color:var(--ink);background:var(--phosphor);text-decoration:none}
/* The empty state is INFORMATION, not a warning: a repository with no agent history has done
   nothing wrong. Neutral panel, not the amber "something is missing" strip. */
.emptystate{margin:18px 0 0;padding:16px 18px;background:var(--panel);border:1px solid var(--line)}
.emptystate .es-title{color:var(--phosphor);font-family:var(--display);font-size:19px;margin-bottom:6px}
.emptystate .es-text{color:var(--fg-dim);font-size:12.5px;line-height:1.6}
@media (max-width:760px){.pending{flex-wrap:wrap}.pending .pd-link{width:100%}}
@keyframes rise{from{transform:translateY(-100%)}to{transform:none}}

header{padding:26px 0 18px}
.brand{font-family:var(--display);font-weight:400;font-size:38px;line-height:.9;color:var(--phosphor);
  text-shadow:0 0 12px rgba(61,255,160,.5),0 0 44px rgba(61,255,160,.22);letter-spacing:1.5px}
.brand .a{color:var(--amber);text-shadow:0 0 12px rgba(255,180,84,.5),0 0 44px rgba(255,180,84,.2)}
.brand .sub{font-family:var(--display);font-size:.5em;color:var(--fg-dim);letter-spacing:3px;text-shadow:none}
.meta{margin-top:12px;color:var(--fg-dim);font-size:13.5px}
.meta b{color:var(--fg);font-weight:600}
.meta .tag{color:var(--amber)}
/* Branch picker lives inline on the meta line (next to the repo path), styled to
   read as part of the text rather than as a bulky filter control. */
.meta select.branchsel{appearance:none;background:var(--ink);color:var(--fg);font-weight:600;vertical-align:baseline;
  border:1px solid var(--line);font-family:var(--mono);font-size:13px;padding:2px 22px 2px 8px;max-width:min(60vw,520px);cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 12px) 50%,calc(100% - 8px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.meta select.branchsel:focus{outline:none;border-color:var(--phosphor)}
/* A file:// snapshot (or a single-branch repo) can't switch: render it as plain
   inline text — no arrow, no box affordance. */
.meta select.branchsel:disabled{cursor:default;opacity:1;border-color:transparent;padding:2px 0;background-image:none}

/* ---- filter bar ---- */
/* One row while sticky: the FIELDS shrink (min-width:0 below) rather than the row
   wrapping, so a narrower window compresses the selects instead of dropping the reset
   button to a second line — and never forces a horizontal page scroll. The phone layout
   (max-width:760px below) is where the bar finally wraps into a grid. (No overflow
   clipping — the custom-range popup hangs below the bar.) */
.controls{position:sticky;top:0;z-index:20;margin:22px 0 30px;padding:14px 16px;background:var(--panel);
  border:1px solid var(--line);border-bottom-width:3px;display:flex;flex-wrap:nowrap;gap:16px;align-items:flex-end}
.controls .prompt{color:var(--phosphor);font-weight:600;align-self:center;white-space:nowrap}
/* "loading…" badge shown while a filter change re-fetches the data. It FLOATS just below
   the bar's right corner (absolute against the sticky bar), so appearing and disappearing
   never moves the reset button or anything else in the row. */
.loading{position:absolute;top:calc(100% + 8px);right:12px;z-index:30;display:inline-flex;
  align-items:center;gap:8px;color:var(--phosphor);font-size:13px;white-space:nowrap;
  background:var(--panel);border:1px solid var(--phosphor-dim);padding:6px 12px;
  box-shadow:0 10px 26px rgba(0,0,0,.55)}
.loading .spin{width:13px;height:13px;border:2px solid var(--phosphor-dim);border-top-color:var(--phosphor);
  border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* Fields SHRINK (flex-basis with no fixed min-width) so the bar keeps everything on one
   line on a desktop window instead of wrapping just the reset button to a second row;
   below 760px the phone grid takes over. */
.field{display:flex;flex-direction:column;gap:4px;flex:0 1 165px;min-width:0}
.field label{font-size:11px;color:var(--amber);letter-spacing:.6px;text-transform:uppercase;white-space:nowrap}
.field select{appearance:none;background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13.5px;padding:7px 30px 7px 11px;cursor:pointer;min-width:0;width:100%;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.field select:focus{outline:none;border-color:var(--phosphor)}
__UI_RANGE_CSS__
/* The advanced filter. It hangs BELOW the sticky bar rather than expanding it: growing the bar
   pushes the whole page down every time it opens, and on a phone a bar tall enough to hold three
   condition rows covers most of the screen. */
.advwrap{align-self:flex-end;display:inline-flex}
.advbtn{cursor:pointer;border:1px solid var(--line);color:var(--fg);background:transparent;
  font-family:var(--mono);font-size:12.5px;padding:7px 12px;white-space:nowrap}
.advbtn:hover{border-color:var(--phosphor);color:var(--phosphor)}
.advbtn[aria-expanded="true"]{border-color:var(--phosphor);color:var(--phosphor)}
.advbtn .advcount{color:var(--ink);background:var(--phosphor);padding:0 5px;margin-left:6px}
.advpanel{position:absolute;top:calc(100% + 6px);left:12px;right:12px;z-index:29;background:var(--panel);
  border:1px solid var(--phosphor-dim);padding:12px 14px;box-shadow:0 10px 26px rgba(0,0,0,.55)}
.advrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.advrow select,.advrow input{background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13px;padding:6px 9px;min-width:0}
.advrow select:focus,.advrow input:focus{outline:none;border-color:var(--phosphor)}
.advrow .advkey{flex:1 1 210px}
.advrow .advop{flex:0 0 118px}
.advrow .advval{flex:2 1 240px}
.advrow .advend{flex:1 1 130px}
.advrow .advdash{color:var(--dim);padding:0 2px}
.advrow .advdel{cursor:pointer;border:1px solid var(--line);color:var(--amber);background:transparent;
  font-family:var(--mono);font-size:13px;padding:5px 10px}
.advrow .advdel:hover{border-color:var(--amber)}
.advfoot{display:flex;gap:10px;align-items:center;margin-top:4px}
.advadd{cursor:pointer;border:1px solid var(--phosphor-dim);color:var(--phosphor);background:transparent;
  font-family:var(--mono);font-size:12.5px;padding:6px 11px}
.advadd:hover{background:var(--phosphor);color:var(--ink)}
.advapply{cursor:pointer;border:1px solid var(--phosphor);color:var(--ink);background:var(--phosphor);
  font-family:var(--mono);font-size:12.5px;padding:6px 14px;font-weight:600}
.advapply:hover{filter:brightness(1.15)}
/* Nothing to apply: still clickable (it closes the panel) but visibly not the next step. */
.advapply.clean{background:transparent;color:var(--dim);border-color:var(--line);font-weight:400}
.advhint{color:var(--dim);font-size:12px}
@media (max-width:760px){
  .advpanel{position:static;margin-top:10px}
  .advrow .advkey,.advrow .advop,.advrow .advval{flex:1 1 100%}
}
.reset{cursor:pointer;border:1px solid var(--amber);color:var(--amber);background:transparent;
  font-family:var(--mono);font-size:12.5px;padding:7px 12px;align-self:flex-end;margin-left:auto;white-space:nowrap}
.reset:hover{background:var(--amber);color:var(--ink)}
/* Phone-width: a wrapped sticky bar would cover half the screen, so the filters become a
   two-column grid that scrolls with the page. */
@media (max-width:760px){
  /* relative, not static: no stickiness, but still the anchor for the floating badge.
     top:auto !important overrides the inline top stackStickyBanner() sets for the
     sticky desktop layout — on a relative bar that offset just shoves the bar down,
     leaving a banner-sized blank gap at the top (worst on backtrace, whose long
     banner wraps tall at phone widths). */
  .controls{position:relative;flex-wrap:wrap;gap:10px 12px;top:auto !important}
  .controls .prompt{width:100%}
  .field{flex:1 1 42%;min-width:0}
  .field select{min-width:0;width:100%}
  .daterange{left:0;right:auto}
  .reset{flex:1 1 42%;margin-left:0}
}

h2.section{font-family:var(--display);font-size:27px;font-weight:400;color:var(--phosphor);letter-spacing:1px;
  margin:38px 0 14px;text-shadow:0 0 16px rgba(61,255,160,.3)}
h2.section::before{content:"# ";color:var(--amber)}
/* commit-log heading row with its sort selector on the right */
.loghead{display:flex;align-items:baseline;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:38px 0 14px}
.loghead h2.section{margin:0}
.logsort{display:flex;align-items:center;gap:8px}
.logsort label{font-size:11px;color:var(--amber);letter-spacing:.6px;text-transform:uppercase}
.logsort select{appearance:none;background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13px;padding:5px 26px 5px 10px;cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 14px) 50%,calc(100% - 9px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.logsort select:focus{outline:none;border-color:var(--phosphor)}
/* Backtrace only: explains what the log's entries actually are (reconstructed turns, not commits). */
.logintro{margin:0 0 12px;padding:10px 14px;background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--amber);color:var(--fg-dim);font-size:12.5px;line-height:1.6}
.logintro b{color:var(--fg)}
.logintro code{color:var(--fg);background:var(--ink);padding:0 5px}

/* ---- stat cards ---- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);padding:16px 18px;position:relative;transition:border-color .15s}
.card:hover{border-color:var(--phosphor-dim)}
/* No text-transform: the "aGiTrack" brand must never render all-caps. */
.card .label{font-size:11.5px;color:var(--amber);letter-spacing:.5px}
.card .value{font-family:var(--display);font-size:42px;line-height:1.05;color:var(--phosphor);margin-top:6px;
  text-shadow:0 0 14px rgba(61,255,160,.3);white-space:nowrap}
.card .value.amber{color:var(--amber);text-shadow:0 0 14px rgba(255,180,84,.3)}
/* scientific-notation exponent: noticeably smaller than the mantissa (e.g. 1.01×10⁸) */
.card .value sup{font-size:.5em;vertical-align:super;line-height:0;margin-left:1px}
.card .note{font-size:12px;color:var(--fg-dim);margin-top:4px}

/* ---- time-series chart ---- */
.chartpanel{padding:14px 16px 10px;position:relative}
.chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap;margin-bottom:10px}
.chart-controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.zoomhint{color:var(--fg-dim);font-size:11.5px;font-style:italic;white-space:nowrap}
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
/* "of which …" subset rows: indented and dimmed so they read as children of the line above */
.row.sub{padding-top:5px;padding-bottom:5px;border-bottom:none}
.row.sub .name{padding-left:18px;font-weight:400;color:var(--fg-dim);font-size:12.5px}
.row.sub .bar{height:12px}
.row.sub .bar i{background:var(--fg-dim);box-shadow:none;opacity:.55}
.bar{position:relative;height:18px;background:var(--ink);border:1px solid var(--line);overflow:hidden}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--phosphor-dim);box-shadow:0 0 10px rgba(61,255,160,.4)}
.bar i.amber{background:var(--amber-dim);box-shadow:0 0 10px rgba(255,180,84,.35)}
/* Log-scaled bars (the token panel) get a diagonal hatch over the fill plus a "log" tag,
   so they read as non-linear at a glance — distinct from the proportional bars elsewhere. */
.bar i.log{background-image:repeating-linear-gradient(45deg,rgba(2,8,5,.32) 0 3px,transparent 3px 7px)}
.bar .logtag{position:absolute;right:6px;top:50%;transform:translateY(-50%);z-index:1;pointer-events:none;
  font-size:9px;letter-spacing:.5px;text-transform:uppercase;color:var(--fg-dim);opacity:.85;text-shadow:0 0 3px var(--ink)}
.row.sub .bar .logtag{font-size:8px;right:5px}
/* Token panel: a separator delimits each whole token GROUP (a main category plus its
   "of which" sub-rows), never between a parent and its own children. Drawn as a top border
   before each main row — i.e. after the previous group — instead of below every row. */
#tokens .row{border-bottom:none}
#tokens .row:not(.sub){border-top:1px solid var(--line)}
#tokens .row:not(.sub):first-child{border-top:none}
/* The notes apply to every bar, so set them off with a separator above the first one. */
#tokens .row + .hint{border-top:1px solid var(--line);padding-top:10px;margin-top:2px}
.bar span{position:absolute;right:6px;top:0;font-size:11px;color:var(--fg-dim);line-height:18px}
.row .num{text-align:right;color:var(--fg-dim);font-size:12.5px}
.row .num b{color:var(--phosphor);font-weight:600}
.empty{padding:16px 18px;color:var(--fg-dim)}
/* shared sessions list */
.srow{display:grid;grid-template-columns:minmax(160px,1.6fr) 2fr auto;gap:14px;align-items:baseline;
  padding:11px 18px;border-bottom:1px solid var(--line)}
.srow:last-child{border-bottom:none}
.srow .sid{color:var(--phosphor);font-weight:500;overflow-wrap:anywhere}
.srow .sid b{color:var(--amber);font-weight:600}
.srow .smeta{color:var(--fg-dim);font-size:12.5px}
.srow .sage{color:var(--fg-dim);font-size:12px;text-align:right;white-space:nowrap}
@media (max-width:760px){.srow{grid-template-columns:1fr;gap:4px}.srow .sage{text-align:left}}
.hint{padding:8px 18px 0;color:var(--fg-dim);font-size:11.5px;font-style:italic}
.kindcounts{padding:11px 18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--fg-dim);line-height:1.9}
.kindcounts .klabel{color:var(--amber);margin-right:4px}
.kindcounts .kc{white-space:nowrap;cursor:help;border-bottom:1px dotted var(--fg-dim)}
.kindcounts .kc b{color:var(--fg)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:760px){.split{grid-template-columns:1fr}.row{grid-template-columns:1fr;gap:6px}}


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
/* overflow-wrap:anywhere so a subject with no spaces (a shell command, a long flag,
   a URL) breaks instead of pushing the row past the screen and giving the whole page
   a horizontal scrollbar. */
.entry .ksub{flex:1;min-width:200px;color:var(--fg);overflow-wrap:anywhere}
.entry .badge{font-size:10.5px;letter-spacing:.5px;padding:1px 7px;border:1px solid var(--line);color:var(--fg-dim)}
.entry .badge.ai{color:var(--phosphor);border-color:var(--phosphor-dim)}
.entry .badge.ops{color:var(--ops);border-color:var(--ops)}
.entry .badge.nontracked{color:var(--amber);border-color:var(--amber-dim)}
.entry .badge.pending{color:var(--amber);border-color:var(--amber-dim);border-style:dashed}
.entry .badge.tracked{color:var(--phosphor);border-color:var(--phosphor-dim);background:rgba(61,255,160,.08)}
.entry .badge.anomaly{color:var(--red);border-color:var(--red)}
.entry.pending{opacity:.82}
.entry .lc{font-size:12px;color:var(--fg-dim)}
.entry .lc .add{color:var(--phosphor)} .entry .lc .rem{color:var(--red)}
.entry .tok{font-size:11px;border:1px solid var(--line);padding:0 5px;color:var(--fg-dim)}
.entry .tok.out{color:var(--phosphor);border-color:var(--phosphor-dim)}
.entry .tok.dim{opacity:.7}
.entry .squash{font-size:10.5px;color:var(--ops);border:1px solid var(--ops);padding:1px 6px;letter-spacing:.4px}
/* No rule of its own down the left: the log already draws the timeline rail beside every
   entry, so a second vertical line for the expanded one was one stripe too many (and cost
   width where a phone has least). The indent stays, so the detail still reads as belonging
   to its row. */
.entry .detail{flex-basis:100%;width:100%;margin:8px 0 4px;padding-left:14px;cursor:default}
/* The diff toggle and the GitHub link sat flush against each other, reading as one control. */
.entry .detail .dhead{color:var(--amber);font-size:12.5px;margin-bottom:4px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.entry .detail .dmeta{color:var(--ops);font-size:12px;margin-bottom:6px}
/* The expanded commit message. No frame of its own: a bordered, inset box inside the
   already-indented detail wasted horizontal space and read as a nested panel on a phone.
   Scoped to nothing in particular ON PURPOSE — the files view renders the same element and
   used to match none of this, so its messages came out in the page's much larger body font. */
__UI_COMMIT_CSS__
.dmsg .diffempty{color:var(--fg-dim);font-size:12px;font-style:italic;padding:8px 12px;line-height:1.55}
.dmsg .diffempty b{color:var(--phosphor);font-style:normal}
/* loading spinner shown while a detail / diff / file history is being fetched */
.loadbox{display:flex;align-items:center;gap:10px;padding:14px 12px;color:var(--phosphor);font-size:12.5px}
.loadbox .spin{width:15px;height:15px;border:2px solid var(--phosphor-dim);border-top-color:var(--phosphor);
  border-radius:50%;animation:spin .7s linear infinite}
/* rendered Markdown inside the expanded message */
.dmsg.md p{margin:7px 0}
/* Reflow mode (set by reflowParagraphs on paragraphs the layout wraps anyway):
   the source line breaks stop rendering and the paragraph flows as prose. The
   space preceding each <br> in the markup keeps the words apart. */
.dmsg.md p.mdp.reflow br{display:none}
.dmsg.md .md-h{font-family:var(--mono);color:var(--amber);margin:11px 0 5px;font-size:13px;font-weight:600}
/* Heading depth reads at a glance: structural sections (# …) brightest/largest,
   the ## User/## Agent role one step down, and a message's own nested headings
   smaller, dimmer and indented so they sit visibly under their role. md() maps a
   source level L to <h(L+2)>, so these are # → h3, ## → h4, content → h5/h6.
   The commit renderer shifts a message's own headings down two levels
   (TRACE_HEADING_SHIFT), so a message `#` lands at h5 and a `##` at h6 — the two
   content styles below, which is what they are sized for. */
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
.pager{display:flex;align-items:center;flex-wrap:wrap;gap:10px 16px;padding:14px 0 2px;color:var(--fg-dim);font-size:12.5px}
.pager .pcount{min-width:160px}
.pager .pnav{position:relative;display:flex;align-items:center;gap:4px;flex-wrap:wrap}
/* Floating "loading…" badge for a log page switch: same look as the filter badge, but
   anchored just below the page selector — feedback appears where the user clicked. */
.pgload{right:auto;left:0;top:calc(100% + 6px)}
.pager button{cursor:pointer;background:transparent;border:1px solid var(--phosphor-dim);color:var(--phosphor);
  font-family:var(--mono);font-size:12.5px;padding:5px 9px;min-width:31px}
.pager button:hover:not([disabled]){background:var(--phosphor);color:var(--ink)}
.pager button[disabled]{opacity:.35;cursor:default;border-color:var(--line);color:var(--fg-dim)}
/* The current page stays fully lit even though it's disabled (it's a marker, not a dead control). */
.pager button.current[disabled]{opacity:1;background:var(--phosphor);color:var(--ink);border-color:var(--phosphor)}
.pager .pgap{padding:0 2px}
.pager .pgoto{display:flex;align-items:center;gap:7px}
.pager .pgoto input{width:58px;background:transparent;border:1px solid var(--line);color:var(--fg);
  font-family:var(--mono);font-size:12.5px;padding:4px 7px;-moz-appearance:textfield}
.pager .pgoto input:focus{outline:none;border-color:var(--phosphor-dim)}
.pager .pgoto input::-webkit-outer-spin-button,.pager .pgoto input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}

/* ---- log tabs (commits / files) — title on its own line, tabs left below it ---- */
.logsection-head{margin:38px 0 14px}
.logsection-head h2.section{margin:0 0 12px}
.logsection-head .storycta{margin:0 0 14px}
.logtabs{display:flex;gap:8px;justify-content:flex-start}
/* BOTH tabs are amber (like the reset button), never grey: a dim grey unselected tab read as
   "disabled" rather than "clickable". The selected one is told apart by a lit-up background,
   not by being the only one with colour. */
.logtab{font-family:var(--mono);font-size:13px;background:transparent;border:1px solid var(--amber);
  color:var(--amber);padding:5px 16px;cursor:pointer;letter-spacing:.5px}
.logtab.active{color:var(--ink);background:var(--amber);box-shadow:0 0 12px rgba(255,180,84,.35)}
/* Hover on the unselected tab: a tint, so it never impersonates the filled selected state. */
.logtab:not(.active):hover{background:rgba(255,180,84,.22)}
.panehead{display:flex;justify-content:flex-end;align-items:center;margin:0}
/* Tabs and the pane's control (sort / file filter) sit on ONE line, with room above them so
   they read as the start of the log rather than part of the story card above. */
.panebar{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
  margin:22px 0 12px}
/* An author `display` beats the `hidden` attribute, so every hidden flex/grid box on this
   page needs this to actually hide (the files filter showed in the commits view without it). */
#head-files{margin:0 0 12px}

/* ---- file browser (folder tree) — names flush-left, folders collapsible ---- */
.filesearch{background:var(--ink);color:var(--fg);border:1px solid var(--line);font-family:var(--mono);
  font-size:13px;padding:6px 11px;min-width:min(70vw,280px)}
.filesearch:focus{outline:none;border-color:var(--phosphor)}
.filebrowse{background:var(--panel);border:1px solid var(--line);text-align:left}
.ftree-dir{border-bottom:1px solid var(--line)}
.ftree-dir:last-child{border-bottom:none}
/* Left-pack the ▸ marker and folder name; the count is pushed to the right with margin-left:auto
   (NOT justify-content:space-between, which — with the ::before marker as a third flex item —
   would strand the folder name in the centre). */
.ftree-dir>summary{cursor:pointer;list-style:none;padding:9px 16px;display:flex;
  gap:8px;align-items:center;text-align:left}
.ftree-dir>summary::-webkit-details-marker{display:none}
.ftree-dir>summary::before{content:"▸";color:var(--ops);flex:none}
.ftree-dir[open]>summary::before{content:"▾"}
.ftree-dir>summary:hover{background:rgba(103,184,214,.05)}
.ftree-dir .fdir{color:var(--ops);overflow-wrap:anywhere;text-align:left}
.ftree-dir .fdircount{color:var(--fg-dim);font-size:12px;white-space:nowrap;flex:none;margin-left:auto;padding-left:14px}
/* Modest, single-step indentation per nesting level so names stay readable and left-anchored. */
.ftree-children{padding-left:16px}
.frow{display:grid;grid-template-columns:1fr auto auto auto;gap:16px;align-items:center;text-align:left;
  padding:9px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.ftree-children .frow{border-bottom:1px dashed var(--line)}
.frow:last-child{border-bottom:none}
.frow:hover{background:rgba(61,255,160,.04)}
.frow .fp{color:var(--fg);overflow-wrap:anywhere;text-align:left}
.frow.open .fp{color:var(--phosphor)}
.frow .fc{color:var(--fg-dim);font-size:12px;white-space:nowrap}
.frow .fl{font-size:12px;white-space:nowrap}
.frow .fl .add{color:var(--phosphor)} .frow .fl .rem{color:var(--red)}
.fdetail{border-bottom:1px solid var(--line);background:var(--panel-2)}
.fchange{padding:9px 16px 11px;border-bottom:1px dashed var(--line);border-left:2px solid var(--phosphor-dim);margin-left:14px}
.fchange:last-child{border-bottom:none}
.fchange .fchead{cursor:pointer}
.fchange.open .fsub{color:var(--phosphor)}
.fchange .fmeta{color:var(--ops);font-size:12px;margin-bottom:3px}
.fchange .fsub{color:var(--fg);font-size:12.5px;overflow-wrap:anywhere}
.fchange .fdetailc{margin-top:8px}
/* Look: shared (see ui.COMMIT_CSS). Only its spacing here belongs to this list. */
.fchange .fdifftoggle{margin-bottom:8px}
.fmore{padding:12px 16px;color:var(--fg-dim);font-size:12.5px}
footer{margin-top:46px;padding-top:22px;border-top:1px dashed var(--line);color:var(--fg-dim);font-size:12.5px;
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
footer .flink{color:var(--accent,#6be);text-decoration:none}
footer .flink:hover{text-decoration:underline}

/* ---- phone layout: give the content the whole screen ----------------------------------
   A 24px gutter each side plus a 34px commit-log rail costs ~25% of a phone's width. These
   rules live at the END of the sheet on purpose: a media query adds no specificity, so an
   override must come AFTER the base rule it targets (the earlier phone block above only
   works because the filter-bar rules it overrides are declared before it). */
@media (max-width:760px){
  /* .wrap's own side padding is shared (ui.BASE_CSS) so all three pages gain the width. */
  .panel,.card{padding-left:12px;padding-right:12px}
  /* Rail line and entry dots stay concentric at the narrower offset. */
  .log{padding-left:18px}
  .log::before{left:6px}
  .entry::before{left:-17.5px}
  .entry .ksub{min-width:0;flex-basis:100%}
}
</style>
</head>
<body>
__PREBOOT_HTML__
__UI_HUBBAR_HTML__
<div class="topbanners">
<div id="neterror" class="neterror" hidden>⚠ Can't reach the aGiTrack dashboard server — it may have been stopped (Ctrl-C in the terminal). Showing the last loaded data; retrying…</div>
__UI_RELOAD_NOTICE_HTML__
__UPDATE_BANNER__
</div>
<div class="wrap">
  <header>
    <div class="brand"><span class="a">a</span>GiTrack<span class="sub">&nbsp;dashboard</span></div>
    <div class="meta"><span class="tag">repo</span> <b>__REPO__</b><span id="branchmeta"> &nbsp;·&nbsp; <span class="tag">branch</span> <select id="f-branch" class="branchsel" title="View statistics and the commit log for a single branch"></select></span> &nbsp;·&nbsp; <span id="genat"></span> &nbsp;·&nbsp; <a class="flink" id="storylink" href="story" title="Read this repo's history as a story, written by the backend agent from the commits and their traces">&#128214; story</a> &nbsp;·&nbsp; <a class="flink" id="learnlink" href="learn" title="Let the backend agent coach you from your own interaction traces">&#127891; learn</a></div>
  </header>

  <div class="booting" id="booting">
    <span class="spin"></span>
    <div class="bmsg">reading commit history<span class="bdots"></span></div>
    <div class="bbar" id="bbar" hidden><i id="bfill"></i></div>
    <div class="bsub" id="bsub">crunching the git log: a large repo can take a few seconds</div>
  </div>

  <div class="controls">
    <span class="prompt">&gt; filter</span>
    <div class="field"><label for="f-author">committer</label><select id="f-author"></select></div>
    <div class="field"><label for="f-backend">backend</label><select id="f-backend"></select></div>
    <div class="field"><label for="f-model">model</label><select id="f-model"></select></div>
    <div class="field period-field"><label for="f-period">range</label><select id="f-period">
      __UI_RANGE_OPTIONS__
    </select>
      <div class="daterange" id="daterange" hidden>
        <div class="dr-field"><label for="f-from">from</label><input type="date" id="f-from"></div>
        <div class="dr-field"><label for="f-to">to</label><input type="date" id="f-to"></div>
        <button class="dr-done" id="dr-done">done</button>
      </div>
    </div>
    <span class="advwrap"><button class="advbtn" id="advbtn" aria-expanded="false" aria-controls="advpanel" title="Filter on any field the commits record: harness version, OS, session name, compactions, token counts">advanced</button></span>
    <button class="reset" id="reset">reset</button>
    <span class="loading" id="loading" hidden aria-live="polite"><span class="spin"></span>loading…</span>
    <div class="advpanel" id="advpanel" hidden>
      <div id="advrows"></div>
      <div class="advfoot">
        <button class="advadd" id="advadd">+ add condition</button>
        <button class="advapply" id="advapply">apply</button>
        <span class="advhint">Conditions on different fields must all match; several values of the same field match any of them.</span>
      </div>
    </div>
  </div>

  <!-- Work the BACKTRACE can see and this dashboard cannot: sessions or turns no aGiTrack
       commit covers. The two views are deliberately separate (one is recorded, the other
       inferred), but a page that shows only what it recorded while saying nothing about what
       it knows it is missing is reporting "this is everything" when it is not. -->
  <!-- Why this dashboard is empty, when it is. Three different reasons need three different
       answers, and none of them is a blank page (see agitrack/metrics/pending.py). -->
  <div class="emptystate" id="emptystate" hidden>
    <div class="es-title" id="emptystate-title"></div>
    <div class="es-text" id="emptystate-text"></div>
  </div>

  <div class="pending" id="pending" hidden>
    <span class="pd-icon">&#9888;</span>
    <span class="pd-text" id="pending-text"></span>
    <a class="pd-link" id="pending-link" href="#">see it in the backtrace &rarr;</a>
  </div>

  <h2 class="section">overview</h2>
  <div class="cards" id="cards"></div>

  <h2 class="section">activity over time</h2>
  <div class="panel chartpanel">
    <div class="chart-head">
      <div class="legend" id="ts-legend"></div>
      <div class="chart-controls">
        <span class="zoomhint">scroll to zoom · drag to pan · double-click to reset</span>
        <div class="gran"><label for="ts-gran">per</label><select id="ts-gran">
          <option value="hour">hour</option>
          <option value="day">day</option>
          <option value="week">week</option>
          <option value="month">month</option>
        </select></div>
      </div>
    </div>
    <div class="chartwrap"><canvas id="ts-canvas" title="Scroll to zoom the time axis · drag to pan · double-click to reset"></canvas><div class="tip" id="ts-tip" hidden></div></div>
    <div class="chart-empty" id="ts-empty" hidden>no dated commits in view</div>
  </div>

  <h2 class="section">code changes &amp; tokens</h2>
  <div class="split">
    <div class="panel" id="lines"></div>
    <div class="panel" id="tokens"></div>
  </div>

  <h2 class="section" id="insights-head">agent efficiency</h2>
  <a class="learncta" id="learncta" href="learn" title="Open the learn page">
    <span class="lc-icon">&#127891;</span>
    <span class="lc-text"><b>Learn from these traces.</b>
    Your agent reads how you drive it and writes small lessons on agent skills and this
    codebase &mdash; quizzes, exercises, progress tracked.</span>
    <span class="lc-btn">open learn &rarr;</span>
  </a>
  <div id="insights"></div>

  <h2 class="section">by backend</h2>
  <div class="panel" id="by-backend"></div>

  <h2 class="section">by model</h2>
  <div class="panel" id="by-model"></div>

  <h2 class="section">by committer</h2>
  <div class="panel" id="by-committer"></div>

  <h2 class="section">shared sessions</h2>
  <div class="panel" id="shared"></div>

  <div class="logsection-head">
    <h2 class="section">log</h2>
    <a class="learncta storycta" id="storycta" href="story" title="Open the storyline">
      <span class="lc-icon">&#128214;</span>
      <span class="lc-text"><b>Read it as a story.</b>
      The same history below, zoomed out: what changed, why, and what the developers were
      working out at each turn.</span>
      <span class="lc-btn">open story &rarr;</span>
    </a>
  </div>
  <div class="panebar">
    <div class="logtabs">
      <button class="logtab active" data-tab="commits">commits</button>
      <button class="logtab" data-tab="files" id="tab-files-btn">files</button>
    </div>
    <div class="logsort"><label for="f-sort">sort</label><select id="f-sort" title="Sort the filtered commits">
      <option value="date">newest first</option>
      <option value="lines">most lines changed</option>
      <option value="tokens">most output tokens</option>
    </select></div>
  </div>
  <div class="panehead" id="head-files" hidden><input id="file-search" class="filesearch" type="search" placeholder="filter files…" autocomplete="off"></div>

  <div class="logpane" id="pane-commits">
    <div class="logintro" id="logintro" hidden>Each entry below is one <b>reconstructed agent turn</b> from your
    local session transcripts: what was asked, the agent's reply, and the file changes it recovered, shown like a
    commit. The <b>subject line of each entry is the user prompt</b> that started that turn of the conversation
    with the agent. Nothing here has been written to git; a turn with a <b>committed</b> badge is already in your
    history with aGiTrack metadata, and <code>agitrack --backtrace commit</code> bakes the rest into real commits
    on a new branch.</div>
    <div class="log" id="commitlog"></div>
  </div>
  <div class="logpane" id="pane-files" hidden>
    <div class="filebrowse" id="filebrowse"></div>
  </div>

  <footer>
    <span>aGiTrack &nbsp;·&nbsp;
      <a class="flink" href="http://agitrack.core-aix.org/" target="_blank" rel="noopener noreferrer">website</a> &nbsp;·&nbsp;
      <a class="flink" href="https://github.com/core-aix/agitrack" target="_blank" rel="noopener noreferrer">GitHub</a></span>
    <span id="count"></span>
  </footer>
</div>

<script type="application/json" id="agitrack-data">__DATA__</script>
<script>
"use strict";
// The page embeds an INITIAL payload (unfiltered aggregates + first log page)
// for an instant first paint, then talks to the server: /data for the metric
// panels under the active filters, /log for one page of the commit log. The
// browser never holds every commit's message/trace/constituents — only the
// current page — so memory stays bounded no matter how deep the history is.
const INIT = JSON.parse(document.getElementById("agitrack-data").textContent);
// In "shell" mode the server embeds no aggregates/log (just page_size + shared
// sessions) so the page paints instantly on a large repo; the browser then fetches
// /data and /log behind a loading animation. With data embedded (a file:// snapshot
// or a small repo's first paint), the page renders fully right away.
const HAVE_DATA = !!INIT.agg;
if(!HAVE_DATA) document.body.classList.add("booting");  // show the loader at once
// The document is here and the real chrome is styled, so drop the pre-boot overlay that
// covered the transfer. Handoff, not a gap: in shell mode the .booting loader above is
// already up behind it and keeps reporting progress while /data and /log are fetched.
{ const pb = document.getElementById("preboot"); if(pb) pb.remove(); }
const PAGE_SIZE = INIT.page_size || 50;
let HEAD = INIT.head||"", AGG = INIT.agg||null, LOGPAGE = INIT.log||null, OPTIONS = INIT.options||null, GENERATED = INIT.generated_at||"";
let TS = INIT.timeseries || {t:[]};  // per-period series for the activity-over-time plot
let SPAN = INIT.span || {from:0, to:0};  // full-history commit-date range (epoch seconds)
let SHARED = INIT.shared_sessions || [];  // sessions shared into this repo (issue #55)
let INSIGHTS = INIT.insights || [];  // whole-history agent-efficiency insights (not filter-scoped)
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
const KIND_LABEL = {"agitrack-ops":"aGiTrack-ops","agent-merge":"agent-merge"};
const REFRESH_MS = 30000, DAY = 86400;

// DEFAULT_BRANCH is the branch the page first loaded for; "reset" returns to it.
const DEFAULT_BRANCH = INIT.branch || "";
const state = {branch:DEFAULT_BRANCH, author:"", backend:"", model:"", meta:[], fromTs:0, toTs:0, sort:"date", granularity:(INIT.timeseries&&INIT.timeseries.granularity)||"day"};
// Only a page served over http(s) has a backend to reach; a file:// snapshot has
// none, so it must never raise a false "server unreachable" alarm.
const LIVE = location.protocol.indexOf("http") === 0;
// Backtrace mode reconstructs past agent turns from local transcripts: there is no git
// commit and no committer behind a row, so the fabricated commit-hash and committer chrome
// are hidden (see hideFabricatedChrome). Everything shown is real transcript data.
const BACKTRACE = !!INIT.backtrace;
__UI_DOM_JS__
__UI_HUBBAR_JS__
const _pageEsc = null;
const pct = (a,b) => b ? (a/b*100).toFixed(1)+"%" : "0%";
// Commit-log subjects can be very long; cap the displayed line at 120 chars with an
// ellipsis (the full subject stays available via the row's hover title and the expanded
// commit message). The ellipsis counts toward the cap, so the result never exceeds 120.
const SUBJECT_MAX = 120;
// Cut at the end of a word, never mid-word: back up to the last space inside the cap. Only
// when that space sits in the first half (one enormous "word") does the hard cut remain.
const truncSubject = s => {
  s = s||"";
  if(s.length <= SUBJECT_MAX) return s;
  let cut = s.slice(0, SUBJECT_MAX-1);
  const sp = cut.lastIndexOf(" ");
  if(sp > SUBJECT_MAX/2) cut = cut.slice(0, sp);
  return cut.trimEnd()+"…";
};
// Re-measure the top strip whenever a notice appears or disappears (set by stackStickyBanner).
let restackControls = () => {};
function setOffline(on){
  const el=$("neterror"); if(!el) return;
  if(el.hidden === !on) return;  // no change ⇒ no relayout
  el.hidden = !on;
  // The offline notice grows/shrinks the strip while the page is open, so the filter bar's
  // sticky offset has to follow it — otherwise it hides behind the notice it just made room for.
  restackControls();
}
// Show the "loading…" spinner while a user-initiated filter change re-fetches the data
// (not during the background refresh poll, which would make it flicker constantly).
function showLoading(on){ const el=$("loading"); if(el) el.hidden = !on; }

function qs(extra){
  const p = new URLSearchParams();
  // A non-default branch rescopes the whole view (stats + log) server-side.
  if(state.branch && state.branch !== DEFAULT_BRANCH) p.set("branch", state.branch);
  if(state.author) p.set("author", state.author);
  if(state.backend) p.set("backend", state.backend);
  if(state.model) p.set("model", state.model);
  // Repeated (append, not set): the filter is a LIST of conditions, and several may name the
  // same field — which is exactly how one field is widened to "any of these values".
  for(const [k,op,v] of state.meta){ if(k && op && v !== "") p.append("meta", k+":"+op+":"+v); }
  if(state.fromTs) p.set("from", state.fromTs);
  if(state.toTs) p.set("to", state.toTs);
  if(state.granularity) p.set("granularity", state.granularity);
  for(const k in (extra||{})) p.set(k, extra[k]);
  return p.toString();
}
// Coming back from the story or learn page is a fresh page load, so the dashboard would
// crunch the whole git log again behind the boot animation before showing anything. The
// last view is kept for this session and painted IMMEDIATELY, then the real fetch replaces
// it. A hard refresh (reload) skips the snapshot, which is what a refresh is for.
const SNAP_KEY = "agitrack:dash:" + location.pathname;
const SNAP_MAX = 2_000_000;   // never fill sessionStorage with a huge repo's payload
function snapshotSave(){
  try{
    if(!AGG || !LOGPAGE) return;
    const blob = JSON.stringify({head:HEAD, agg:AGG, options:OPTIONS, generated:GENERATED, ts:TS,
                                 span:SPAN, shared:SHARED, insights:INSIGHTS, log:LOGPAGE,
                                 branch:state.branch, at:Date.now()});
    if(blob.length <= SNAP_MAX) sessionStorage.setItem(SNAP_KEY, blob);
  }catch(e){}   // private mode, quota, anything: the snapshot is an optimisation only
}
function snapshotRestore(){
  try{
    const nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
    if(nav && nav.type === "reload") return false;   // an explicit refresh means re-read
    const blob = sessionStorage.getItem(SNAP_KEY);
    if(!blob) return false;
    const d = JSON.parse(blob);
    if(!d || !d.agg || !d.log) return false;
    HEAD=d.head; AGG=d.agg; OPTIONS=d.options; GENERATED=d.generated; TS=d.ts||{t:[]};
    if(d.span) SPAN=d.span; if(d.shared) SHARED=d.shared;
    if(d.insights !== undefined) INSIGHTS=d.insights;
    LOGPAGE=d.log; if(d.branch) state.branch=d.branch;
    return true;
  }catch(e){ return false; }
}

// --- first-load progress ---------------------------------------------------------------
// The boot fetches below can take minutes on a repository whose commits have never been
// diffed (the dashboard indexes the whole history once, then caches it per commit). The
// server publishes how far along that is; ask it while the fetch is in flight, so the
// loading screen shows real movement instead of a spinner and a promise of "a few seconds".
let BOOTPOLL = null;
function bootProgress(on){
  if(BOOTPOLL){ clearInterval(BOOTPOLL); BOOTPOLL = null; }
  if(!on) return;
  const bar = $("bbar"), fill = $("bfill"), sub = $("bsub");
  if(!bar || !fill || !sub) return;
  const tick = async () => {
    try{
      const r = await fetch("progress", {cache:"no-store"});
      if(!r.ok) return;                       // an export (or an older server) has no such route
      const p = await r.json();
      // Nothing measurable yet — a build that has not reached a counted phase, or one that is
      // simply fast. Leave the plain spinner alone rather than flashing an empty bar.
      if(!p || !p.active || !p.total) return;
      const pct = Math.min(100, Math.max(1, Math.round(100 * p.done / p.total)));
      bar.hidden = false;
      fill.style.width = pct + "%";
      // No em-dash: this is page prose (AGENTS.md), and it is the sentence that tells someone
      // watching a minute-long first load that it is a one-off, not how this repo always is.
      sub.textContent = p.stage + ": " + p.done.toLocaleString() + " of " + p.total.toLocaleString()
        + " commits (" + pct + "%). Done once, then cached.";
    }catch(e){}                               // a poll that fails changes nothing on screen
  };
  tick();
  BOOTPOLL = setInterval(tick, 500);
}

async function loadAgg(){
  try{ const r = await fetch("data?"+qs(), {cache:"no-store"}); if(r.ok){
    const d = await r.json(); HEAD=d.head; AGG=d.agg; OPTIONS=d.options; GENERATED=d.generated_at;
    TS = d.timeseries || {t:[]}; if(d.span) SPAN = d.span; if(d.shared_sessions) SHARED = d.shared_sessions;
    if(d.insights !== undefined) INSIGHTS = d.insights;
    // The server reports which branch it actually served (e.g. it fell back to the
    // default if the requested one vanished); reflect that in the state + header.
    if(d.branch !== undefined){ state.branch = d.branch; setBranchLabel(d.branch); }
    setOffline(false); snapshotSave(); return true; } }
  catch(e){ if(LIVE) setOffline(true); }  // network failure ⇒ server unreachable
  return false;
}
async function loadLog(offset){
  try{ const r = await fetch("log?"+qs({offset:offset||0, limit:PAGE_SIZE, sort:state.sort}), {cache:"no-store"});
    if(r.ok){ LOGPAGE = await r.json(); setOffline(false); snapshotSave(); return true; } }
  catch(e){ if(LIVE) setOffline(true); }
  return false;
}

// --- minimal Markdown for the expanded commit message ---
__UI_COMMIT_JS__
const md = commitMd;

// `min` (default 0) is the low end of the scale: the width maps [min, max] → [0, 100],
// so passing the smallest value in the set as `min` spreads the bars across the full
// width instead of compressing them against a fixed 0 baseline. The smallest value then
// shows the 2% floor (still visible) and the largest fills the bar.
// `logScale` marks a bar whose width is log-scaled (the token bars): it gets a striped
// fill and a small "log" tag so it reads differently from the linear (scalar) bars
// elsewhere, whose widths are directly proportional to their value.
function barRow(name, sub, value, max, numHtml, amber, min, logScale){
  const w = barWidth(value, max, min);
  // A long name is ellipsized to keep the row tidy, but its full text (and the
  // sub-label) is always available on hover via the title attribute.
  const title = esc(name) + (sub ? " — " + esc(sub) : "");
  const cls = ((amber?"amber":"") + (logScale?" log":"")).trim();
  const tag = logScale ? `<span class="logtag">log</span>` : "";
  return `<div class="row"><div class="name" title="${title}">${esc(name)}${sub?` <small>${esc(sub)}</small>`:""}</div>`+
    `<div class="bar">${tag}<i class="${cls}" style="width:${w}%"></i></div>`+
    `<div class="num">${numHtml}</div></div>`;
}
function barWidth(value, max, min){
  const lo = min || 0, span = max - lo;
  return span > 0 ? Math.max(2, (value-lo)/span*100) : (value > 0 ? 100 : 0);
}
// An indented "of which …" row: a subset of the category above it. Always log-scaled (it
// only renders token subsets), so its fill is striped and it carries the "log" tag like the
// parent.
function subBarRow(name, value, max, numHtml, min){
  const w = barWidth(value, max, min);
  return `<div class="row sub"><div class="name" title="${esc(name)}">${esc(name)}</div>`+
    `<div class="bar"><span class="logtag">log</span><i class="log" style="width:${w}%"></i></div>`+
    `<div class="num">${numHtml}</div></div>`;
}
function card(label, value, note, amber, tip){
  return `<div class="card"${tip?` title="${esc(tip)}"`:""}><div class="label">${esc(label)}</div>`+
    `<div class="value ${amber?"amber":""}">${bigValue(value)}</div><div class="note">${esc(note||"")}</div></div>`;
}
// Keep the big display font, but when a plain integer is too long to fit the card (e.g. 100M+
// output tokens) show it in scientific notation instead — nicer than shrinking the type. Percents,
// ratios, "—", and already-short numbers are shown verbatim.
function bigValue(value){
  const m = String(value).match(/^([+\-]?)([\d,]+)$/);   // a signed, comma-grouped integer only
  if(!m || m[2].length <= 9) return value;               // ≤ 9 chars incl. commas still fits at 42px
  return m[1] + sci(Number(m[2].replace(/,/g,"")));
}
function sci(n){
  if(!isFinite(n) || n===0) return "0";
  const exp = Math.floor(Math.log10(n));
  const mant = (n/Math.pow(10,exp)).toFixed(2).replace(/\.?0+$/,"");  // 1.01, 1.2, 5, …
  return mant + "×10<sup>" + exp + "</sup>";                          // e.g. 1.01×10⁸ (exp in a <sup>)
}
function tokenBrief(t){
  if(!t) return "";
  const parts=[];
  if(t.output) parts.push(`<span class="tok out">${kfmt(t.output)} out</span>`);
  if(t.input) parts.push(`<span class="tok">${kfmt(t.input)} in</span>`);
  if(t.cache_read) parts.push(`<span class="tok dim">${kfmt(t.cache_read)} cache</span>`);
  return parts.length ? `<span class="lc">${parts.join(" ")}</span>` : "";
}

function insightCard(i){
  const t = i.trend;
  let chip = "";
  if(t){
    const arrow = t.direction==="better" ? "\u2193" : t.direction==="worse" ? "\u2191" : "\u2192";
    const text = t.direction==="steady" ? "steady" : Math.abs(t.change*100).toFixed(0)+"% vs earlier half";
    // Spell the two periods out in the tooltip: a bare "vs earlier" leaves the reader guessing
    // which turns it was measured against.
    const tip = `${t.label}\n${fmtMetric(t.earlier)} over ${trendSpan(t,"earlier")}\n${fmtMetric(t.later)} over ${trendSpan(t,"later")}`;
    chip = `<span class="itrend ${esc(t.direction)}" title="${esc(tip)}">${arrow} ${esc(text)}</span>`;
  }
  return `
    <div class="insight ${esc(i.severity)}">
      <div class="ihead"><span class="isev">${esc(i.severity==="good"?"improved":i.severity)}</span><b>${esc(i.title)}</b>${chip}<span class="isum">${esc(i.summary)}</span></div>
      <ul class="ievidence">${(i.evidence||[]).map(e => `<li>${esc(e)}</li>`).join("")}</ul>
      <div class="isuggest">&rarr; ${esc(i.suggestion)}</div>
    </div>`;
}
// A metric is a share (0..1) or a growth multiple; render it the way the card's text does.
function fmtMetric(v){ return v<=1.5 ? (v*100).toFixed(0)+"%" : v.toFixed(1)+"\u00d7"; }
// "earlier" / "later" as a concrete period: the range is split in two by TURN COUNT, so the
// halves hold equal numbers of turns but rarely equal spans of calendar time. Naming the dates
// (and the turn counts) is the only way "vs earlier" means anything to the reader.
function trendSpan(t, half){
  const from = t[half+"_from"], to = t[half+"_to"], n = t[half+"_turns"];
  if(!from || !to) return half;
  const days = ymd(from)===ymd(to) ? ymd(from) : `${ymd(from)} \u2192 ${ymd(to)}`;
  return `${days} (${fmt(n)} turn${n===1?"":"s"})`;
}
const INSIGHTS_VISIBLE = 4;
function renderInsights(){
  // One card per detected inefficiency, computed from the SAME filtered commits the rest of the
  // page shows — so narrowing the range re-asks the question for that slice, and an improvement
  // becomes visible instead of being diluted by the whole history. Each card carries the trend
  // between the earlier and later half of the range; a habit that stopped shows as "improved".
  // Only the worst few are expanded, the rest fold away, so the panel stays readable.
  const host = $("insights"), head = $("insights-head"), cta = $("learncta");
  if(!host) return;
  const items = INSIGHTS || [];
  const show = items.length > 0;
  host.style.display = show ? "" : "none";
  if(head) head.style.display = show ? "" : "none";
  // The learn call-to-action goes with the section, not just with the heading. Both are fed by
  // the same thing: too little history to read a pattern out of means there is also too little
  // for the agent to write lessons FROM, so the big green "open learn" promised something the
  // page could not deliver — and it was the most prominent element left once the section it sits
  // inside had hidden itself. (The small "learn" link in the header stays: that is navigation.)
  if(cta) cta.style.display = show ? "" : "none";
  if(!show){ host.innerHTML = ""; return; }
  const lead = items.slice(0, INSIGHTS_VISIBLE), rest = items.slice(INSIGHTS_VISIBLE);
  const t = (items.find(i => i.trend) || {}).trend;
  let html = `<div class="insightnote">Patterns in the selected range. Narrow the filters above to check a period on its own.</div>`;
  if(t) html += `<div class="insightnote">Each trend compares the <b>earlier half</b> of this range, ${esc(trendSpan(t,"earlier"))}, with the <b>later half</b>, ${esc(trendSpan(t,"later"))} \u2014 split so each half holds the same number of turns.</div>`;
  html += lead.map(insightCard).join("");
  if(rest.length) html += `<details class="insightmore"><summary>${rest.length} more insight${rest.length===1?"":"s"}</summary>${rest.map(insightCard).join("")}</details>`;
  host.innerHTML = html;
}
// The "there is agent work here that no commit covers" notice. Shown only on the LIVE dashboard
// (the backtrace is where that work IS visible, so saying it there would be circular) and only
// when the server found some; the link jumps to the same repo's backtrace view.
// Why the page has nothing to show. Live dashboard only: the backtrace has its own banner, and
// an empty reconstruction is reported by the daemon before it ever serves a page.
const EMPTY_TITLES = {"no-sessions": "No agent history here", "nothing": "Nothing to show yet"};
function renderEmptyState(){
  const box = $("emptystate"), es = AGG.empty_state;
  if(!box) return;
  if(BACKTRACE || !es || !es.text){ box.hidden = true; return; }
  $("emptystate-title").textContent = EMPTY_TITLES[es.kind] || "Nothing to show yet";
  $("emptystate-text").textContent = es.text;
  box.hidden = false;
}

function renderPending(){
  const box = $("pending"), pd = AGG.pending;
  if(!box) return;
  if(BACKTRACE || !pd || !pd.notice){ box.hidden = true; return; }
  $("pending-text").textContent = pd.notice;
  const link = $("pending-link");
  if(HUB){ link.href = hubUrl("backtrace", HUB.slug); link.hidden = false; }
  else { link.hidden = true; }   // no hub: there is no backtrace view to send anyone to
  box.hidden = false;
}

function renderAgg(){
  renderInsights();
  const total = AGG.total, tracked = AGG.tracked;
  const ai = {ins:AGG.ai_lines[0], del:AGG.ai_lines[1]}; ai.total = ai.ins+ai.del;
  const nt = {ins:AGG.nontracked_lines[0], del:AGG.nontracked_lines[1]}; nt.total = nt.ins+nt.del;
  const allLines = ai.total + nt.total, tok = AGG.tokens, eff = AGG.line_yield, kinds = k => AGG.kinds[k]||0;

  $("genat").textContent = "updated " + GENERATED;
  $("count").textContent = `${fmt(total)} commits in view`;
  renderPending();
  renderEmptyState();

  $("cards").innerHTML = [
    // Backtrace reconstructs conversation TURNS, not commits, and every turn it shows is by
    // definition agent work. So a "commits" count (reads as a git commit count) and an "aGiTrack
    // coverage" ratio (of turns, not commits — and always near 100%) would both mislead here.
    BACKTRACE ? "" : card("commits", fmt(total), `${fmt(tracked)} via aGiTrack`),
    BACKTRACE ? "" : card("aGiTrack coverage", pct(tracked,total), `${fmt(total-tracked)} non-tracked`, true),
    // Backtrace line counts come from the transcript's Edit/Write tool calls only — an agent
    // can also change code in ways a transcript doesn't record as edits (shell commands,
    // formatters), so the reconstruction is a lower bound. Say so instead of "% of changes"
    // (meaningless here: every reconstructed line is AI). The non-tracked card is dropped
    // outright — backtrace turns are all agent work, so it would always read zero.
    BACKTRACE
      ? card("Tracked AI lines", "+"+fmt(ai.ins), `−${fmt(ai.del)} · may miss some edits`, false,
          "Reconstructed from the agent's file-editing tool calls in the transcript. Agents can also "+
          "change code in ways a transcript does not record as edits (shell commands, formatters, "+
          "generated files), so not all changed lines are necessarily counted here.")
      : card("Tracked AI lines", "+"+fmt(ai.ins), `−${fmt(ai.del)} · ${pct(ai.total, allLines)} of changes`),
    BACKTRACE ? "" : card("non-tracked lines", "+"+fmt(nt.ins), `−${fmt(nt.del)} · not tracked as AI`, true),
    card("output tokens", fmt((tok.output||0)+(tok.subagent_output||0)), `${fmt((tok.input||0)+(tok.subagent_input||0))} input`),
    card("line yield", eff===null?"—":eff.toFixed(1), "AI lines / 1k output tok", true),
  ].join("");

  const lineRow = (label, sub, v, amber) =>
    `<div class="row"><div class="name" title="${esc(label)} — ${esc(sub)}">${label} <small>${sub}</small></div>`+
      `<div class="bar"><i class="${amber?"amber":""}" style="width:${allLines?v.total/allLines*100:0}%"></i></div>`+
      `<div class="num"><b>+${fmt(v.ins)}</b> / −${fmt(v.del)}</div></div>`;
  const kc = (label, key, tip) => `<span class="kc" title="${tip}">${label} <b>${kinds(key)}</b></span>`;
  // Backtrace shows only reconstructed agent turns, so the non-tracked bar (like its
  // card above) would always sit at zero — drop it there.
  $("lines").innerHTML =
    lineRow("Tracked AI", "agent + covered", ai, false) +
    (BACKTRACE ? "" : lineRow("Non-tracked", "user + plain commits", nt, true)) +
    `<div class="kindcounts"><span class="klabel">commits by kind:</span> `+
      kc("agent", "agent", "Commits aGiTrack made from the agent's work") + " · " +
      kc("covered", "covered", "Backend-made commits an aGiTrack cover commit accounts for") + " · " +
      kc("merge", "agent-merge", "Integration merges whose conflicts an agent resolved") + " · " +
      kc("aGiTrack-ops", "agitrack-ops", "aGiTrack's own integration merge commits") + " · " +
      kc("user", "user", "User commits made through aGiTrack") + " · " +
      kc("untracked", "untracked", "Commits with no aGiTrack metadata (made outside aGiTrack)") +
    `</div>`;

  // Token panel as a hierarchy (mirrors the text dashboard / collect.token_breakdown):
  // each base category's headline is main-agent + sub-agent, with the sub-agent (and, for
  // input, the cache-write) share shown as an indented "of which" subset of it.
  const tb = AGG.token_breakdown || {categories:[], summarizer:{}};
  const cats = tb.categories || [], summ = tb.summarizer || {};
  // Token kinds span orders of magnitude (cache reads dwarf everything), so a linear bar
  // would shrink the small kinds to invisible slivers. Scale the bar widths by log10
  // instead; the numbers shown on each row remain the real counts.
  const logTok = v => Math.log10((v||0)+1);
  // Summarizer (aGiTrack's own calls) gets the same parent + indented "of which" layout as
  // the agent categories, so its breakdown lines up with the others rather than reading as a
  // one-off. Its headline is the sum of its parts.
  const summKeys = ["input","output","cache_read"].filter(k=>summ[k]);
  const summTotal = summKeys.reduce((a,k)=>a+summ[k], 0);
  // Scale the bars between the SMALLEST and largest value shown (not a fixed 0 baseline), so
  // the widths spread across the full bar — the smallest value gets the floor, the largest
  // fills it. Built from exactly the values that get a bar.
  const barVals = [];
  cats.forEach(c => { barVals.push(c.total); (c.subsets||[]).forEach(s => barVals.push(s.value)); });
  if(summKeys.length){ barVals.push(summTotal); summKeys.forEach(k => barVals.push(summ[k])); }
  const logs = barVals.map(logTok);
  const maxLog = logs.length ? Math.max(...logs) : 1;
  const minLog = logs.length ? Math.min(...logs) : 0;
  const rows = [];
  cats.forEach(c => {
    rows.push(barRow(c.label, "", logTok(c.total), maxLog, `<b>${fmt(c.total)}</b>`, c.label==="output", minLog, true));
    (c.subsets||[]).forEach(s => rows.push(subBarRow("of which "+s.label, logTok(s.value), maxLog, fmt(s.value), minLog)));
  });
  if(summKeys.length){
    rows.push(barRow("summarizer", "aGiTrack's own calls", logTok(summTotal), maxLog, `<b>${fmt(summTotal)}</b>`, false, minLog, true));
    summKeys.forEach(k => rows.push(subBarRow("of which "+(k==="cache_read"?"cache read":k), logTok(summ[k]), maxLog, fmt(summ[k]), minLog)));
  }
  // The hierarchy shows cache-write under input; the note clarifies the one billing nuance
  // it can't — input is what was processed (uncached input + cache write), while cache read
  // is the cached context reused, billed separately.
  const cacheNote = (tok.cache_write||0)+(tok.subagent_cache_write||0)+(tok.cache_read||0) > 0
    ? `<div class="hint" title="aGiTrack counts input as what was processed = uncached input + cache-creation (cache write) tokens, rather than the provider's price sheet. Cache read is the cached context reused and is billed separately.">input counts processed tokens (uncached&nbsp;input + cache&nbsp;write); cache&nbsp;read is the cached context reused, billed separately</div>`
    : "";
  // Notes sit BELOW the bars so the bars lead the panel and the annotations follow.
  $("tokens").innerHTML = rows.length
    ? rows.join("") + `<div class="hint">Note: bar widths are log-scaled; indented rows are a subset of the line above</div>` + cacheNote
    : `<div class="empty">no token metadata recorded</div>`;

  $("by-backend").innerHTML = groupPanel(AGG.by_backend);
  $("by-model").innerHTML = groupPanel(AGG.by_model);

  const comm = Object.entries(AGG.by_committer).map(([name,b]) => [name, {
    commits:b.commits, agitrack:b.agitrack_commits||0,
    ai:(b.ai_insertions||0)+(b.ai_deletions||0), nt:(b.nontracked_insertions||0)+(b.nontracked_deletions||0)}]);
  const maxC = Math.max(1, ...comm.map(([,b])=>b.ai));
  comm.sort((a,b)=>b[1].ai-a[1].ai || b[1].commits-a[1].commits);
  $("by-committer").innerHTML = comm.length
    ? comm.map(([name,b]) => barRow(name, `${b.commits} commits · ${b.agitrack} via aGiTrack`, b.ai, maxC,
        `AI-driven <b>${fmt(b.ai)}</b> · non-tracked ${fmt(b.nt)}`)).join("")
    : `<div class="empty">no commits</div>`;

  tsHover = -1;
  renderTimeseries();
  renderShared();
}
function renderShared(){
  const el = $("shared"); if(!el) return;
  if(!SHARED.length){
    el.innerHTML = `<div class="empty">no sessions shared yet — share one from aGiTrack (Ctrl-G → session → Share this session)</div>`;
    return;
  }
  el.innerHTML = SHARED.map(s => {
    const meta = [s.model, s.backend].filter(Boolean).map(esc).join(" · ");
    const age = s.updated ? sharedAge(s.updated) : "";
    return `<div class="srow"><span class="sid">${esc(s.label || s.owner)}<b>/</b>${esc(s.name)}</span>`+
      `<span class="smeta">${meta}</span><span class="sage">${esc(age)}</span></div>`;
  }).join("");
}
function sharedAge(epoch){
  const d = Math.max(0, Math.floor(Date.now()/1000 - epoch));
  for(const [s,u] of [[86400,"d"],[3600,"h"],[60,"m"]]) if(d>=s) return `${Math.floor(d/s)}${u} ago`;
  return "just now";
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
// null tsView ⇒ the whole range. The period (range) filter selects the DATA range;
// the mouse wheel/drag just zooms/pans WITHIN it for a closer look (no extra range
// selector — double-click resets to the full filtered range).
function tsBounds(){
  const n = (TS.t||[]).length;
  if(n<=1 || !tsView) return [0, Math.max(0, n-1)];
  let lo = Math.max(0, Math.min(tsView[0], n-1));
  let hi = Math.max(lo, Math.min(tsView[1], n-1));
  return [lo, hi];
}
const PAD_L=10, PAD_R=10;
function tsPlotW(){ return Math.max(1, $("ts-canvas").parentElement.clientWidth - PAD_L - PAD_R); }
function tsXAt(i){ const [lo,hi]=tsBounds(), span=hi-lo; return PAD_L + (span<=0 ? tsPlotW()/2 : (i-lo)/span*tsPlotW()); }
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
    const cls = AI_KINDS.has(c.kind) ? "ai" : (c.kind==="agitrack-ops" ? "ops" : "nontracked");
    const badge = `<span class="badge ${cls}">${esc(KIND_LABEL[c.kind]||c.kind)}</span>`;
    // A manual-commit-mode latent turn not yet folded into a commit — flag it so the user
    // can tell in-progress work from committed history.
    const pend = c.pending?`<span class="badge pending" title="not yet committed — folds into your next commit">pending</span>`:"";
    // Backtrace: mark turns already committed to git with aGiTrack metadata, so the user sees what
    // is already tracked vs. what `--backtrace commit` would still add.
    const trk = (BACKTRACE && c.tracked)?`<span class="badge tracked" title="already committed to git with aGiTrack metadata">committed</span>`:"";
    const anom = c.token_anomaly?`<span class="badge anomaly" title="This commit re-exported conversation history that earlier commits already accounted for (a lost watermark on an older aGiTrack), so its token counts are excluded from every total.">tokens excluded</span>`:"";
    const squash = (c.parts&&c.parts.length)?`<span class="squash">⧉ ${c.parts.length} squashed</span>`:"";
    const lc = (c.ins||c.del)?`<span class="lc"><span class="add">+${fmt(c.ins)}</span> <span class="rem">−${fmt(c.del)}</span></span>`:"";
    const m = c.eff_model?`<span class="lc">${esc(c.eff_model)}</span>`:"";
    const subj = c.subject||"", shown = truncSubject(subj);
    const subjTitle = shown!==subj ? ` title="${esc(subj)}"` : "";  // full subject on hover when cut
    const shaTag = BACKTRACE ? "" : `<span class="sha">${esc(c.short)}</span>`;
    return `<div class="entry ${cls}${c.pending?' pending':''}" data-i="${i}">${shaTag}${badge}${pend}${trk}${anom}${squash}`+
      `<span class="ksub"${subjTitle}>${esc(shown)}</span>${lc}${tokenBrief(c.tokens)}${m}`+
      `<div class="detail" id="detail-${i}" hidden></div></div>`;
  }).join("");
  const from = total ? offset+1 : 0, to = offset+entries.length;
  const pages = Math.max(1, Math.ceil(total/limit)), cur = Math.min(pages, Math.floor(offset/limit)+1);
  // Numbered page buttons: all of them when few, otherwise first + a window around
  // the current page + last, with … gaps (0 marks a gap).
  let nums = [];
  if(pages<=9){ for(let p=1;p<=pages;p++) nums.push(p); }
  else {
    const lo = Math.max(2, cur-2), hi = Math.min(pages-1, cur+2);
    nums.push(1);
    if(lo>2) nums.push(0);
    for(let p=lo;p<=hi;p++) nums.push(p);
    if(hi<pages-1) nums.push(0);
    nums.push(pages);
  }
  const numBtns = nums.map(p => p===0 ? `<span class="pgap">…</span>` :
    `<button class="pnum${p===cur?' current':''}" data-p="${p}" ${p===cur?'disabled':''}>${fmt(p)}</button>`).join("");
  const prevDis = cur<=1 ? "disabled" : "", nextDis = cur>=pages ? "disabled" : "";
  const gotoBox = pages>1 ? `<span class="pgoto">go to <input id="log-goto" type="number" inputmode="numeric" `+
    `min="1" max="${pages}" placeholder="${cur}"> / ${fmt(pages)}</span>` : "";
  const pager = `<div class="pager"><span class="pcount">${fmt(from)}–${fmt(to)} of ${fmt(total)} commits</span>`+
    `<span class="pnav"><button id="log-first" title="first page" ${prevDis}>«</button>`+
    `<button id="log-prev" title="previous page" ${prevDis}>‹</button>${numBtns}`+
    `<button id="log-next" title="next page" ${nextDis}>›</button>`+
    `<button id="log-last" title="last page" ${nextDis}>»</button>`+
    `<span class="loading pgload" id="pgloading" hidden aria-live="polite"><span class="spin"></span>loading…</span></span>${gotoBox}</div>`;
  $("commitlog").innerHTML = (rows || `<div class="empty">no commits</div>`) + pager;
  const goPage = async p => {
    p = Math.min(pages, Math.max(1, p));
    if(p===cur) return;
    // Fetching a page can take seconds on a big repo — float a spinner under the page
    // selector. renderLog replaces the whole log (badge included) on success; only a
    // failed fetch leaves this pager standing, so re-hide it then.
    const busy = $("pgloading"); if(busy) busy.hidden = false;
    if(await loadLog((p-1)*limit)) renderLog();
    else if(busy) busy.hidden = true;
  };
  for(const [id, p] of [["log-first",1],["log-prev",cur-1],["log-next",cur+1],["log-last",pages]]){
    const b = $(id); if(b && !b.disabled) b.onclick = () => goPage(p);
  }
  document.querySelectorAll("#commitlog .pnum:not([disabled])").forEach(b => { b.onclick = () => goPage(+b.dataset.p); });
  const gi = $("log-goto");
  if(gi) gi.onkeydown = e => { if(e.key==="Enter" && gi.value!=="") goPage(+gi.value||cur); };
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
  if(!detail.hidden){ detail.hidden = true; return; }
  // Show a loading animation immediately, then build the detail on the next frame — rendering a
  // large message (markdown) or a squash's many parts can take a moment, so the spinner paints first.
  detail.innerHTML = SPIN;
  detail.hidden = false;
  requestAnimationFrame(() => {
    if(detail.hidden) return;
    // Local file diff (served from the clone by /diff) is the primary, GitHub-free action;
    // the GitHub link is kept as an optional extra when a remote is configured. A squash has
    // no single diff worth showing here (its parts expand separately), so skip the button then.
    const diffBtn = (LIVE && c.sha && !(c.parts&&c.parts.length))
      ? `<button class="diffbtn" data-diff="${i}">show file diff</button>` : "";
    const link = c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">view on GitHub ↗</a>` : "";
    const span = (c.started||c.ended)
      ? `<div class="dmeta">AI conversation: ${esc(c.started||"?")} → ${esc(c.ended||"?")}</div>` : "";
    const who = (!BACKTRACE && c.committers&&c.committers.length)
      ? `<div class="dmeta">committer${c.committers.length>1?"s":""}: ${c.committers.map(esc).join(", ")}</div>` : "";
    // The short hash is already shown on the log row itself, so the detail header only carries
    // the actions (diff toggle + optional GitHub link) — no redundant second copy of the hash.
    const head = `${diffBtn}${link}`;
    detail.innerHTML = `<div class="dhead">${head}</div>${who}${span}`+
      `<div class="dmsg md" id="dbody-${i}" data-mode="msg">${md(c.message||"(no message recorded)")}</div>`+
      partsHtml(c.parts);
    reflowParagraphs(detail);   // covers the squashed constituents' messages too
  });
}

// One box that flips between the commit message and this commit's file diff — the button toggles
// which is shown, so you never juggle two panes. The diff (from the local clone via /diff, no
// GitHub) is fetched once and cached by sha, so flipping back and forth is instant.
// Loading animation shown while an async fetch (diff / file history) is in flight, and a
// helper that turns a raw unified diff into HTML — a binary file (no text diff) or an empty
// diff shows a clear hint instead of a blank pane.
const SPIN = '<div class="loadbox"><span class="spin"></span> loading…</div>';
// An empty diff means something different in a reconstruction than on the live dashboard.
// There, no diff means the commit changed nothing. Here it usually means the change was
// made in a way the transcript never recorded as an edit — a shell command, a formatter, a
// generated file — because a reconstruction can only see the agent's file-editing tool
// calls. Saying "no changes" would be a plain untruth, so this says what it can and cannot
// know, and points at the fix.
const BACKTRACE_NO_DIFF =
  '<div class="diffempty">No file changes were recovered for this turn.<br><br>' +
  'A backtrace is reconstructed from your agent\'s local transcript, which records the ' +
  'file-editing tool calls but not every way code can change — edits applied through shell ' +
  'commands, formatters, or generated files leave no edit to read, so their diffs cannot be ' +
  'recovered after the fact.<br><br>' +
  'Run your agent through <b>aGiTrack</b> from now on and this stops being guesswork: each ' +
  'turn is committed as it happens, with its real diff, tokens and the conversation that ' +
  'produced it.</div>';
function diffHtml(text, truncated){
  const t = (text||"").trim();
  if(!t) return BACKTRACE ? BACKTRACE_NO_DIFF : '<div class="diffempty">no changes to show for this file</div>';
  if(/Binary files .* differ/.test(t) && !/^@@/m.test(t)) return '<div class="diffempty">binary file — no text diff to show</div>';
  return renderDiff(text) + (truncated?'<div class="diffempty">…diff truncated (very large diff)</div>':"");
}
const _diffCache = {};
async function toggleDiff(i){
  const c = LOG_ENTRIES[i], body = $("dbody-"+i);
  const btn = document.querySelector('.diffbtn[data-diff="'+i+'"]');
  if(!c || !body) return;
  if(body.dataset.mode === "diff"){                       // diff → back to the commit message
    body.dataset.mode = "msg"; body.className = "dmsg md";
    body.innerHTML = md(c.message||"(no message recorded)");
    reflowParagraphs(body);
    if(btn) btn.textContent = "show file diff";
    return;
  }
  body.dataset.mode = "diff";
  if(btn) btn.textContent = "show commit message";
  const apply = html => { if(body.dataset.mode === "diff"){ body.className = "dmsg diff"; body.innerHTML = html; } };
  if(_diffCache[c.sha] !== undefined){ apply(_diffCache[c.sha]); return; }
  apply(SPIN);  // computing a large commit's diff can take a moment — show a loading animation
  try{
    const r = await fetch("diff?sha="+encodeURIComponent(c.sha), {cache:"no-store"});
    const d = r.ok ? await r.json() : {error:"server error"};
    const html = d.error ? '<div class="diffempty">'+esc(d.error)+'</div>' : diffHtml(d.diff, d.truncated);
    _diffCache[c.sha] = html;
    apply(html);  // apply() no-ops if the user flipped back to the message mid-fetch
  }catch(e){ apply('<div class="diffempty">couldn\'t load the diff (server unreachable)</div>'); }
}

function fillSelect(id, values, allLabel, keep){
  const sel = $(id);
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  sel.value = (keep && values.includes(keep)) ? keep : "";
}
// The branch picker doubles as the branch label on the meta line: keep its shown
// value in sync with the branch actually being viewed.
function setBranchLabel(name){ const el=$("f-branch"); if(el && name && [...el.options].some(o=>o.value===name)) el.value = name; }
// The branch picker is a view switch, not a subset filter, so it has no "all"
// option — exactly one branch is shown at a time. A file:// snapshot can't reach
// the server to re-scope, so it's disabled there (only its own branch is shown).
function fillBranches(){
  const sel = $("f-branch"), branches = OPTIONS.branches || [];
  const list = branches.length ? branches : (state.branch ? [state.branch] : []);
  sel.innerHTML = list.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  sel.value = list.includes(state.branch) ? state.branch : (list[0] || "");
  state.branch = sel.value;
  sel.disabled = !LIVE || list.length <= 1;
}
function syncFilters(){
  fillBranches();
  fillSelect("f-author", OPTIONS.committers, "— entire team —", state.author);
  fillSelect("f-backend", OPTIONS.backends, "— all backends —", state.backend);
  fillSelect("f-model", OPTIONS.models, "— all models —", state.model);
  if(!OPTIONS.committers.includes(state.author)) state.author = "";
  if(!OPTIONS.backends.includes(state.backend)) state.backend = "";
  if(!OPTIONS.models.includes(state.model)) state.model = "";
  // A branch switch changes which metadata fields exist. Drop conditions on fields this view
  // does not have, or the page would keep filtering by something the user can no longer see or
  // clear — an empty dashboard with no visible cause.
  const known = new Set(metaFields().map(f => f.key));
  const kept = known.size ? state.meta.filter(([k]) => known.has(k)) : state.meta;
  const pruned = kept.length !== state.meta.length;
  state.meta = kept;
  // Re-render ONLY when the offered fields actually changed (a branch switch) or a condition
  // was pruned. Every filter change refetches and lands back here, and re-rendering then tore
  // out the very controls the reader was still filling in: typing a range's second bound was
  // wiped the moment the first bound's results arrived, because the element being edited was
  // replaced. Never re-render while the focus is inside the panel, for the same reason.
  const signature = [...known].sort().join("\u0000");
  const editing = document.activeElement && document.activeElement.closest && document.activeElement.closest("#advpanel");
  if(pruned && advDraft) advDraft = advDraft.filter(([k]) => known.has(k));
  if((pruned || signature !== ADV_SIG) && !editing){ renderAdvanced(); syncApply(); }
  ADV_SIG = signature;
  syncAdvBtn();
}

// --- advanced filter ------------------------------------------------------
// Every field the commits actually record, offered without the page knowing any field name in
// advance — so a field aGiTrack adds later is filterable the day it lands. A field with few
// distinct values gets a dropdown of them; a high-cardinality one (session ids, timestamps,
// token counts) gets a text box, because a select holding 500 conversation anchors is a wall,
// not a control. Numeric fields also offer >= and <=.
const META_OPS = {eq:"is", has:"contains", between:"between", ge:"\u2265", le:"\u2264"};
const RANGE_SEP = "..";
// Bounds are offered on EVERY field, not just numeric ones: the ranges worth asking for are
// token counts, timestamps and harness versions, and only the first is a number. The server
// orders all three naturally (see _order_key), so 2.1.9 sorts before 2.1.10.
function opsFor(f){ return f.numeric ? ["eq","between","ge","le"] : ["eq","has","between","ge","le"]; }
function metaFields(){ return (OPTIONS && OPTIONS.meta_fields) || []; }
// The conditions being EDITED, which are not the conditions being applied. Filtering live on
// every keystroke and every dropdown meant the page refetched midway through a half-written
// condition — picking a field applied that field's first value, and typing a range's low bound
// filtered on it before the high bound existed. The panel now edits a draft and `apply` commits
// it. `state.meta` stays the applied set, so it alone decides what the URL and the page show.
let advDraft = null;
function draft(){ if(!advDraft) advDraft = state.meta.map(c => c.slice()); return advDraft; }
function activeConditions(list){
  return list.filter(([k,o,v]) => k && o && v !== "" &&
    !(o === "between" && v.split(RANGE_SEP).every(x => !x.trim())));
}
function advDirty(){ return JSON.stringify(activeConditions(draft())) !== JSON.stringify(state.meta); }
function syncApply(){
  const btn = $("advapply"); if(!btn) return;
  const dirty = advDirty();
  btn.classList.toggle("clean", !dirty);
  btn.textContent = dirty ? "apply" : "close";
  btn.title = dirty ? "Apply these conditions to the whole page" : "No unapplied changes";
}
function metaField(key){ return metaFields().find(f => f.key === key) || null; }
function renderAdvanced(){
  const host = $("advrows");
  if(!host) return;
  const fields = metaFields();
  if(!fields.length){
    host.innerHTML = '<div class="advhint">No metadata fields in this view yet — they appear once commits carry an aGiTrack metadata block.</div>';
    return;
  }
  host.innerHTML = draft().map(([key,op,val], i) => {
    const f = metaField(key) || fields[0];
    const ops = opsFor(f);
    const keyOpts = fields.map(x => `<option value="${esc(x.key)}"${x.key===key?" selected":""}>${esc(x.key)}</option>`).join("");
    const opOpts = ops.map(o => `<option value="${o}"${o===op?" selected":""}>${META_OPS[o]}</option>`).join("");
    // A range needs two ends, so it gets two controls; `is` on a field whose values we shipped
    // gets a dropdown; everything else is free text.
    let valCtl;
    if(op === "between"){
      const cut = val.indexOf(RANGE_SEP);
      const lo = cut < 0 ? val : val.slice(0, cut), hi = cut < 0 ? "" : val.slice(cut + RANGE_SEP.length);
      const end = (part, v, ph) => (f.values && f.values.length)
        ? `<select class="advval advend" data-i="${i}" data-part="${part}">` +
          `<option value=""${v===""?" selected":""}>${ph}</option>` +
          f.values.map(x => `<option value="${esc(x)}"${x===v?" selected":""}>${esc(x)}</option>`).join("") + `</select>`
        : `<input class="advval advend" data-i="${i}" data-part="${part}" type="text" value="${esc(v)}" placeholder="${ph}" autocomplete="off">`;
      // Either end may be left blank — "up to X" and "from X" are both real questions.
      valCtl = end("lo", lo, "from (any)") + `<span class="advdash">\u2192</span>` + end("hi", hi, "to (any)");
    } else if(op === "eq" && f.values && f.values.length){
      valCtl = `<select class="advval" data-i="${i}" data-part="val">` +
        f.values.map(v => `<option value="${esc(v)}"${v===val?" selected":""}>${esc(v)}</option>`).join("") + `</select>`;
    } else {
      valCtl = `<input class="advval" data-i="${i}" data-part="val" type="text" value="${esc(val)}" placeholder="value" autocomplete="off">`;
    }
    return `<div class="advrow">` +
      `<select class="advkey" data-i="${i}" data-part="key">${keyOpts}</select>` +
      `<select class="advop" data-i="${i}" data-part="op">${opOpts}</select>` +
      valCtl +
      `<button class="advdel" data-i="${i}" title="Remove this condition">&times;</button></div>`;
  }).join("");
}
function closeAdvanced(){
  const panel = $("advpanel"), btn = $("advbtn");
  if(!panel || panel.hidden) return;
  panel.hidden = true;
  if(btn) btn.setAttribute("aria-expanded", "false");
}
function syncAdvBtn(){
  const btn = $("advbtn"); if(!btn) return;
  // A range with both ends blank is not a condition anyone set, so it must not show in the
  // badge as though the view were filtered.
  const n = state.meta.length;   // what is APPLIED, never what is merely typed
  btn.innerHTML = "advanced" + (n ? ` <span class="advcount">${n}</span>` : "");
}
// A new condition defaults to the first field and its first value, so it selects something
// real immediately instead of sitting incomplete until three controls have been touched.
function newCondition(){
  const f = metaFields()[0];
  if(!f) return null;
  return [f.key, "eq", (f.values && f.values[0]) || ""];
}
function wireAdvanced(){
  const btn = $("advbtn"), panel = $("advpanel");
  if(!btn || !panel) return;
  btn.onclick = () => {
    const open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    advDraft = state.meta.map(c => c.slice());
    if(open && !advDraft.length){ const c = newCondition(); if(c){ advDraft.push(c); } }
    if(open){ renderAdvanced(); syncApply(); }
  };
  $("advadd").onclick = () => { const c = newCondition(); if(c){ draft().push(c); renderAdvanced(); syncApply(); } };
  // Close on a click anywhere else, and on Escape — a panel that floats over the page and only
  // closes via the button it came from is a panel the reader has to remember how to dismiss.
  // Bound once, on the document, in the CAPTURE phase so a click on some other control both
  // closes this and still reaches that control.
  document.addEventListener("mousedown", e => {
    if(panel.hidden) return;
    if(e.target.closest("#advpanel") || e.target.closest("#advbtn")) return;
    closeAdvanced();
  }, true);
  // Escape is bound to the CONTROL, never to the document: a page-wide keydown scheme is the
  // kind nobody can discover and everybody trips over (tests/test_web_ui.py enforces this, and
  // the story page forbids one outright). Both the panel and the button, because keydown
  // bubbles from whatever inside the panel has focus — and the button keeps focus until the
  // reader moves into it. A click outside already dismisses the panel, which covers the one
  // case this cannot: focus having left the control entirely.
  const applyDraft = () => {
    state.meta = activeConditions(draft());
    advDraft = state.meta.map(c => c.slice());
    closeAdvanced();          // the panel floats over the results it just changed
    syncAdvBtn(); applyFilters();
  };
  // "close" when there is nothing to apply, so the button is never a dead end.
  $("advapply").onclick = () => { if(advDirty()) applyDraft(); else { closeAdvanced(); btn.focus(); } };
  // Enter in a value box is the same intent as pressing apply.
  panel.addEventListener("keydown", e => {
    if(e.key === "Enter" && e.target.closest("input")){ e.preventDefault(); applyDraft(); }
  });
  const escapes = e => { if(e.key === "Escape" && !panel.hidden){ closeAdvanced(); btn.focus(); } };
  panel.addEventListener("keydown", escapes);
  btn.addEventListener("keydown", escapes);
  panel.addEventListener("change", e => {
    const el = e.target.closest("[data-part]"); if(!el) return;
    const i = +el.dataset.i, part = el.dataset.part;
    if(!draft()[i]) return;
    if(part === "key"){
      // Switching field invalidates the operator and value: the new field may not be numeric,
      // and its values are its own. Re-seed from the new field rather than carrying a value
      // that belongs to a different field and silently matches nothing.
      const f = metaField(el.value);
      draft()[i] = [el.value, "eq", (f && f.values && f.values[0]) || ""];
      renderAdvanced();
    } else if(part === "op"){
      const wasRange = draft()[i][1] === "between", isRange = el.value === "between";
      draft()[i][1] = el.value;
      // Leaving a range collapses `lo..hi` to its low end rather than carrying the separator
      // into a single-value operator, where it would match nothing.
      if(wasRange && !isRange) draft()[i][2] = String(draft()[i][2]).split(RANGE_SEP)[0];
      if(!wasRange && isRange) draft()[i][2] = String(draft()[i][2]) + RANGE_SEP;
      renderAdvanced();   // the operator decides whether the value is one control or two
    } else if(part === "lo" || part === "hi"){
      draft()[i][2] = rangeValue(i, part, el.value);
    } else {
      draft()[i][2] = el.value;
    }
    syncApply();
  });
  // Typing in a text value refilters as you go, like every other control on this bar.
  panel.addEventListener("input", e => {
    const el = e.target.closest('input[data-part="val"],input[data-part="lo"],input[data-part="hi"]');
    if(!el) return;
    const i = +el.dataset.i;
    if(!draft()[i]) return;
    const part = el.dataset.part;
    draft()[i][2] = (part === "val") ? el.value : rangeValue(i, part, el.value);
    syncApply();
  });
  panel.addEventListener("click", e => {
    const del = e.target.closest(".advdel"); if(!del) return;
    draft().splice(+del.dataset.i, 1);
    renderAdvanced(); syncApply();
  });
}
// One end changed: rebuild the `lo..hi` the server reads, keeping the other end as it was.
function rangeValue(i, part, value){
  const current = String((draft()[i] || [])[2] || "");
  const cut = current.indexOf(RANGE_SEP);
  let lo = cut < 0 ? current : current.slice(0, cut);
  let hi = cut < 0 ? "" : current.slice(cut + RANGE_SEP.length);
  if(part === "lo") lo = value; else hi = value;
  return lo + RANGE_SEP + hi;
}
// The field set the panel was last drawn for; see fillSelects for why it is remembered.
let ADV_SIG = null;

// A filter change refetches the aggregates and resets the log to its first page.
// PER stays in state; the data range comes from the period filter. A data change
// re-bucketed the series, so any mouse zoom window is reset to the full range.
async function applyFilters(){
  showLoading(true);
  try{
    await loadAgg(); await loadLog(0);
    resetZoom(); setDateBounds(); syncPeriodDates();
    syncFilters(); renderAgg(); renderLog();
  } finally { showLoading(false); }  // always clear the spinner, even on a fetch failure
}
async function refresh(){
  const prev = HEAD;
  // Whether aGiTrack is running here changes for the same reasons new commits appear (a tracker
  // started, a session ended), so the header answers on the same beat as the numbers under it
  // rather than drifting out of step with them on a clock of its own.
  if(typeof refreshHubState === "function") refreshHubState();
  if(!await loadAgg()) return;
  if(HEAD !== prev){  // new commits landed — refresh the whole view
    resetZoom();  // the bucket set changed; an old pixel-zoom window would mis-map
    setDateBounds(); syncPeriodDates();  // extend the shown range to new commits
    await loadLog(LOGPAGE.offset||0);
    syncFilters(); renderAgg(); renderLog();  // renderAgg() also repaints shared sessions
    // Refresh the file list too — but only when no file is expanded, so a poll never
    // yanks a change the user is reading out from under them.
    if(!document.querySelector(".fdetail")){ await loadFiles(); renderFiles(); }
  } else {
    // No new commit, but shared sessions (and their "updated" age) still need to
    // refresh every poll — e.g. an auto-share just bumped a session's timestamp.
    renderShared();
  }
}

// --- time range ---
__UI_RANGE_JS__
// Bound the native date pickers to the actual history span.
function setDateBounds(){
  const lo = ymd(SPAN.from), hi = ymd(SPAN.to);
  for(const id of ["f-from","f-to"]){ const el=$(id); el.min=lo; el.max=hi; }
}
// Reflect the active period in the from/to inputs so they always show the real
// range being viewed — the full history span for "all time", the rolling window
// for a preset. Hand-picked custom dates are left untouched.
function syncPeriodDates(){
  const v = $("f-period").value;
  if(v === "custom") return;
  if(v === ""){ $("f-from").value = ymd(SPAN.from); $("f-to").value = ymd(SPAN.to); }
  else { $("f-from").value = ymd(Math.floor(Date.now()/1000) - (+v)*DAY); $("f-to").value = ymd(SPAN.to || Math.floor(Date.now()/1000)); }
}
function applyPeriod(){
  const v = $("f-period").value;
  if(v === ""){ state.fromTs = 0; state.toTs = 0; }
  else if(v === "custom"){ state.fromTs = dateToTs($("f-from").value,false); state.toTs = dateToTs($("f-to").value,true); }
  else { state.fromTs = Math.floor(Date.now()/1000) - (+v)*DAY; state.toTs = 0; }
  syncPeriodDates();
}

// ---- file browser: every changed file, its change history, and the conversation/tokens
// behind each change. Served by /files (list), /filelog?path= (one file's history), and
// /filediff?path=&sha= (one change's diff for that file). Works for real commits and backtrace.
let FILES = null, FILEQ = "", OPENFILE = { path: null, changes: [] };
async function loadFiles(){
  try{ const r = await fetch("files", {cache:"no-store"}); FILES = (await r.json()).files || []; }
  catch(e){ FILES = FILES || []; }
}
function fileRowHtml(f){
  const name = f.path.split("/").pop() || f.path;
  return `<div class="frow" data-path="${esc(f.path)}"><span class="fp" title="${esc(f.path)}">${esc(name)}</span>`+
    `<span class="fc">${fmt(f.changes)} change${f.changes===1?"":"s"}</span>`+
    `<span class="fl"><span class="add">+${fmt(f.ins)}</span> <span class="rem">−${fmt(f.del)}</span></span>`+
    `<span class="fc">${kfmt(f.output_tokens||0)} out</span></div>`;
}
// Group the flat file list into a folder tree so a big repo's file list is browsable
// (folders collapse/expand) instead of one very long flat list.
function buildFileTree(rows){
  const root = { dirs:{}, files:[] };
  for(const f of rows){
    const parts = f.path.split("/");
    let node = root;
    for(let i=0;i<parts.length-1;i++){ node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs:{}, files:[] }; node = node.dirs[parts[i]]; }
    node.files.push(f);
  }
  return root;
}
function folderStats(node){
  let files = node.files.length, changes = node.files.reduce((a,f)=>a+f.changes,0);
  for(const k in node.dirs){ const s = folderStats(node.dirs[k]); files += s.files; changes += s.changes; }
  return { files, changes };
}
function renderTree(node, forceOpen, depth){
  let html = "";
  for(const d of Object.keys(node.dirs).sort()){
    const child = node.dirs[d], st = folderStats(child), open = forceOpen;  // collapsed by default; a search expands matches
    html += `<details class="ftree-dir"${open?" open":""}><summary><span class="fdir">${esc(d)}/</span>`+
      `<span class="fdircount">${fmt(st.files)} file${st.files===1?"":"s"} · ${fmt(st.changes)} change${st.changes===1?"":"s"}</span></summary>`+
      `<div class="ftree-children">${renderTree(child, forceOpen, depth+1)}</div></details>`;
  }
  for(const f of node.files.slice().sort((a,b)=> (b.last_ts-a.last_ts) || (b.changes-a.changes))) html += fileRowHtml(f);
  return html;
}
function renderFiles(){
  const host = $("filebrowse"), btn = $("tab-files-btn");
  if(!host) return;
  if(!FILES || !FILES.length){ if(btn) btn.style.display = "none"; host.innerHTML = ""; return; }
  if(btn) btn.style.display = "";
  const q = FILEQ.trim().toLowerCase();
  const rows = q ? FILES.filter(f => f.path.toLowerCase().includes(q)) : FILES;
  // A search expands every matching folder so hits aren't hidden; otherwise only the top level is open.
  host.innerHTML = rows.length ? renderTree(buildFileTree(rows), !!q, 0) : `<div class="fmore">no files match “${esc(FILEQ)}”</div>`;
}
function showLogTab(tab){
  document.querySelectorAll(".logtab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  const pc = $("pane-commits"), pf = $("pane-files");
  if(pc) pc.hidden = tab !== "commits";
  if(pf) pf.hidden = tab !== "files";
  // The tabs and the pane's own control (sort / file filter) share one row, so the control
  // has to follow the tab.
  // The file filter does nothing in the commit view, so it is only there for the files tab,
  // on its own line under the tabs (the sort control keeps the right-hand end of that row).
  const hf = $("head-files");
  if(hf) hf.hidden = tab !== "files";
}
function fileChangeHtml(c, i){
  const when = c.ts ? new Date(c.ts*1000).toISOString().slice(0,16).replace("T"," ")+" UTC" : "";
  const who = [c.backend, c.model].filter(Boolean).map(esc).join(" · ");
  const out = (c.tokens && c.tokens.output) ? ` · ${kfmt(c.tokens.output)} out tok` : "";
  const lc = `<span class="add">+${fmt(c.ins)}</span> <span class="rem">−${fmt(c.del)}</span>`;
  // One clickable header per change; opening it reveals a SINGLE box (the conversation by
  // default) with ONE button that toggles that box to the file diff and back — exactly like
  // the commit log, so a change is never shown as two separate panes.
  return `<div class="fchange" data-i="${i}">`+
    `<div class="fchead"><div class="fmeta">${esc(when)}${who?` · ${who}`:""}${out} · ${lc}</div>`+
    `<div class="fsub">${esc(c.subject || c.prompt || "(change)")}</div></div>`+
    `<div class="fdetailc" id="fdetail-${i}" hidden></div></div>`;
}
async function openFile(row){
  const path = row.dataset.path;
  const existing = row.nextElementSibling;
  document.querySelectorAll(".fdetail").forEach(d => d.remove());
  document.querySelectorAll(".frow.open").forEach(r => r.classList.remove("open"));
  if(existing && existing.classList.contains("fdetail")) return;  // was open → just close
  row.classList.add("open");
  // Insert a loading animation immediately — a file's history can take a moment to fetch.
  const div = document.createElement("div");
  div.className = "fdetail";
  div.innerHTML = SPIN;
  row.after(div);
  let data = { changes: [] };
  try{ data = await (await fetch("filelog?path="+encodeURIComponent(path), {cache:"no-store"})).json(); }catch(e){}
  if(!row.classList.contains("open")){ div.remove(); return; }  // user closed it mid-fetch
  OPENFILE = { path, changes: data.changes || [] };
  div.innerHTML = OPENFILE.changes.length
    ? OPENFILE.changes.map((c,i) => fileChangeHtml(c,i)).join("")
    : `<div class="fmore">no recorded changes</div>`;
}
function toggleFileChange(head){
  const fchange = head.closest(".fchange"), i = +fchange.dataset.i, box = $("fdetail-"+i);
  const c = OPENFILE.changes[i] || {};
  if(!box.hidden){ box.hidden = true; box.innerHTML = ""; fchange.classList.remove("open"); return; }
  fchange.classList.add("open");
  box.hidden = false;
  box.innerHTML = SPIN;  // loading animation while the (possibly large) conversation renders
  requestAnimationFrame(() => {
    if(box.hidden) return;
    box.innerHTML = `<button class="fdifftoggle" data-i="${i}">show file diff</button>`+
      `<div class="dmsg md" id="fbody-${i}" data-mode="msg">${md(c.message || "(no conversation recorded)")}</div>`;
    reflowParagraphs(box);
  });
}
const _fdiffCache = {};
async function toggleFileBody(btn){
  const i = +btn.dataset.i, body = $("fbody-"+i), c = OPENFILE.changes[i] || {};
  if(!body) return;
  if(body.dataset.mode === "diff"){                     // diff → back to the conversation
    body.dataset.mode = "msg"; body.className = "dmsg md";
    body.innerHTML = md(c.message || "(no conversation recorded)");
    reflowParagraphs(body);
    btn.textContent = "show file diff";
    return;
  }
  body.dataset.mode = "diff";                            // conversation → file diff
  btn.textContent = "show conversation";
  const key = OPENFILE.path + "\x00" + c.sha;
  const apply = html => { if(body.dataset.mode === "diff"){ body.className = "dmsg diff"; body.innerHTML = html; } };
  if(_fdiffCache[key] !== undefined){ apply(_fdiffCache[key]); return; }
  apply(SPIN);  // a large file diff can take a moment — show a loading animation
  try{
    const r = await fetch(`filediff?path=${encodeURIComponent(OPENFILE.path)}&sha=${encodeURIComponent(c.sha)}`, {cache:"no-store"});
    const d = r.ok ? await r.json() : {error:"server error"};
    const html = d.error ? '<div class="diffempty">'+esc(d.error)+'</div>' : diffHtml(d.diff, d.truncated);
    _fdiffCache[key] = html; apply(html);
  }catch(e){ apply('<div class="diffempty">couldn\'t load the diff (server unreachable)</div>'); }
}
function wireFileBrowser(){
  document.querySelectorAll(".logtab").forEach(b => b.addEventListener("click", () => showLogTab(b.dataset.tab)));
  const search = $("file-search");
  if(search) search.addEventListener("input", e => { FILEQ = e.target.value; renderFiles(); });
  const host = $("filebrowse");
  if(host) host.addEventListener("click", e => {
    const tgl = e.target.closest(".fdifftoggle"); if(tgl){ toggleFileBody(tgl); return; }
    const head = e.target.closest(".fchead"); if(head){ toggleFileChange(head); return; }
    if(e.target.closest(".fdetail")) return;  // other clicks inside a file's change list: ignore
    const row = e.target.closest(".frow"); if(row) openFile(row);
  });
}
function hideFabricatedChrome(){
  // Backtrace is a reconstruction, not a live branch view: drop the parts of the dashboard that
  // don't apply. The committer (no committer behind a turn), the branch picker (there is no
  // branch), and shared sessions (a live-repo feature) would all be misleading here.
  if(!BACKTRACE) return;
  // And say what the log's entries ARE: reconstructed turns shown commit-like, not git commits.
  const li = $("logintro"); if(li) li.hidden = false;
  const fa = $("f-author"); if(fa && fa.closest(".field")) fa.closest(".field").style.display = "none";
  const bc = $("by-committer");
  if(bc){ bc.style.display = "none"; if(bc.previousElementSibling) bc.previousElementSibling.style.display = "none"; }
  const bm = $("branchmeta"); if(bm) bm.style.display = "none";  // no "branch …" in the header
  const sh = $("shared");
  if(sh){ sh.style.display = "none"; if(sh.previousElementSibling) sh.previousElementSibling.style.display = "none"; }
}
function stackStickyBanner(){
  // Pin the filter bar just below the frozen top strip (both are position:sticky top:0), so when
  // the page scrolls the filters stop UNDER the notices instead of behind them. Measure the whole
  // strip rather than the backtrace banner alone: it can also hold the update notice and the
  // offline notice, and an unmeasured sibling puts the filters back behind a banner. An empty
  // strip measures 0, which is exactly the CSS `top` the bar already has.
  const strip = document.querySelector(".topbanners"), ctl = document.querySelector(".controls");
  if(!strip || !ctl) return;
  restackControls = () => { ctl.style.top = strip.offsetHeight + "px"; };
  restackControls();
  window.addEventListener("resize", restackControls);
}
async function init(){
  hideFabricatedChrome();
  stackStickyBanner();
  // Shell mode: the chrome is already on screen with the loading animation; fetch the
  // real aggregates + first log page, then drop the loader and render. A big repo's
  // git-log crunch happens here, behind the animation, instead of blocking first paint.
  if(!HAVE_DATA){
    // Paint the previous view first when there is one (returning from story/learn), so the
    // page is immediately usable; the fetch below then quietly brings it up to date.
    if(snapshotRestore()){
      document.body.classList.remove("booting");
      syncFilters(); renderAgg(); renderLog();
    }
    bootProgress(true);
    try{ await loadAgg(); await loadLog(0); }
    finally{ bootProgress(false); }           // always stop polling, even if a fetch failed
    document.body.classList.remove("booting");
  }
  // Whether the data is in hand (embedded, or just fetched). If the boot fetch failed
  // (server already gone), skip the renders — the offline banner is up and the poll
  // below recovers once the server is back — rather than crashing on null data.
  const ready = AGG && OPTIONS && LOGPAGE;
  if(ready){ syncFilters(); }
  // Show the real range from the first paint: bound the pickers to the history
  // span and fill from/to with it (default period is "all time" = full history).
  if(ready){ setDateBounds(); applyPeriod(); }
  // Switching branch re-scopes everything; the old branch's committer/backend/
  // model picks may not exist on the new one, so clear them for a clean view.
  $("f-branch").onchange = e => { state.branch = e.target.value; state.author=state.backend=state.model=""; applyFilters(); };
  $("f-author").onchange = e => { state.author = e.target.value; applyFilters(); };
  $("f-backend").onchange = e => { state.backend = e.target.value; applyFilters(); };
  $("f-model").onchange = e => { state.model = e.target.value; applyFilters(); };
  // Sort only reorders the (already filtered) commit log, so it just reloads the
  // log's first page — no need to recompute the aggregates.
  $("f-sort").value = state.sort;
  $("f-sort").onchange = async e => { state.sort = e.target.value; showLoading(true);
    try{ if(await loadLog(0)) renderLog(); } finally { showLoading(false); } };
  // "custom range…" reveals a date-range popup anchored under the select; the presets and
  // "all time" hide it. Wired by the shared helper (ui.RANGE_JS), so the story page's copy
  // of this control behaves identically - including re-opening the popup when "custom" is
  // picked again, which fires no change event and so used to do nothing at all.
  const showDateRange = on => { $("daterange").hidden = !on; };
  bindRangeControl(() => { applyPeriod(); applyFilters(); });
  wireAdvanced();
  $("reset").onclick = () => {
    state.author=state.backend=state.model="";
    state.meta=[]; advDraft=null; renderAdvanced(); syncAdvBtn(); syncApply();
    state.branch=DEFAULT_BRANCH;  // back to the branch the page loaded for
    state.sort="date"; $("f-sort").value="date";  // back to newest-first
    $("f-period").value=""; showDateRange(false); applyPeriod();  // back to all time → full span
    applyFilters();
  };
  // Click a commit-log line to open its full message + GitHub link. Clicks
  // inside the opened detail (links, the squash <details> tree, the pager) are
  // left alone.
  $("commitlog").addEventListener("click", e => {
    const dbtn = e.target.closest(".diffbtn");
    if(dbtn){ e.preventDefault(); toggleDiff(+dbtn.dataset.diff); return; }
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
  // Bucket granularity: refetch the (re-bucketed) series and redraw the plot.
  $("ts-gran").value = state.granularity;
  $("ts-gran").onchange = async e => { state.granularity = e.target.value; showLoading(true);
    try{ if(await loadAgg()){ resetZoom(); renderTimeseries(); } } finally { showLoading(false); } };
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
  // Whether a commit message keeps its source line breaks depends on the column width, so
  // every open message is re-measured when the window changes size.
  let rf; window.addEventListener("resize", () => { clearTimeout(rf); rf = setTimeout(() => reflowParagraphs(document), 150); });
  if(ready){ renderAgg(); renderLog(); }
  // The file browser is independent of the commit-log filters, so it loads once here.
  wireFileBrowser();
  await loadFiles(); renderFiles();
  if(location.hash === "#files" && FILES && FILES.length) showLogTab("files");  // deep-link to the Files tab
  // Poll only when there's a live backend; the poll also clears the
  // "unreachable" banner automatically once the server is back — and, after a failed
  // boot fetch, the next successful poll populates the view (HEAD goes "" → real).
  if(LIVE) setInterval(refresh, REFRESH_MS);
}
init();
initHubBar();
</script>
</body>
</html>
"""
