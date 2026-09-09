"""History reads must survive a busy repo's git internals (dashboard "no commits" bug).

aGiTrack commits every turn, which constantly fires background ``git gc --auto``. That
destabilises history reads two ways, and ``check=False`` (every read path) then uses the
aborted/partial output as-is, so the dashboard shows a truncated — or empty — commit log.
Small/idle repos never gc enough to reproduce it, which is why it "works for smaller repos":

  1. STALE COMMIT-GRAPH — a repack moves the objects the graph indexes, so a walk aborts
     NON-ZERO ("commit <sha> exists in commit-graph but not in the object database").
  2. LAZY (PROMISOR) FETCH on a blobless partial clone — mid-repack, a walk briefly can't
     find a local object and tries to fetch it from origin; for aGiTrack's own local-only
     commits the remote answers "not our ref" and the read aborts. The count then flaps.

``GitRepo._run`` disables the commit-graph for read-only traversals, and disables lazy
fetching for the ones that don't need file CONTENT (so they use the local objects instead
of a doomed network fetch). Content reads (``--numstat`` line counts, ``-p`` diffs) keep
lazy fetch ON by default, since on a blobless clone they legitimately fetch absent blobs —
unless the caller passes ``allow_lazy_fetch=False``.

There is a third way a big blobless clone's dashboard breaks: the full-history ``--numstat``
scan would lazily fetch EVERY historical blob on every poll (tens of seconds; the interrupted
fetches even litter ``.git`` with ``tmp_pack_*`` files), so the dashboard looks like it hangs
with no commits. So ``collect_commit_stats``'s full scan opts out of lazy fetch (counts from
local blobs), and ``apply_numstat_for`` fetches blobs for ONLY the log page being displayed.

Both of those are also CACHED to ``.agitrack/numstat.cache``, because a commit's diff against
its parent never changes while the dashboard rebuilds on every ref move (every turn). The cache
records which kind of count each row is: a local-blob count is exact on an ordinary clone but
only a floor on a partial one, so only a row marked exact can answer a page fetch.
"""

import subprocess
from pathlib import Path

import agitrack.git.repo as repo_mod
from agitrack.git import GitRepo
from agitrack.metrics.collect import apply_numstat_for, collect_commit_stats


def _captured_git_calls(monkeypatch) -> list[tuple[list[str], dict]]:
    """Record every ``(argv, env)`` ``GitRepo._run`` hands to ``subprocess.run``."""
    calls: list[tuple[list[str], dict]] = []
    real = subprocess.run

    def spy(command, *args, **kwargs):
        if isinstance(command, list):
            calls.append((list(command), dict(kwargs.get("env") or {})))
        return real(command, *args, **kwargs)

    monkeypatch.setattr(repo_mod.subprocess, "run", spy)
    return calls


def _config_values(cmd: list[str], key: str) -> list[str]:
    """Values of every ``-c <key>=<value>`` pair in a git argv."""
    out = []
    for i, a in enumerate(cmd):
        if a == "-c" and i + 1 < len(cmd) and cmd[i + 1].startswith(key + "="):
            out.append(cmd[i + 1].split("=", 1)[1])
    return out


def _no_lazy_fetch(env: dict) -> bool:
    """Whether this call disables promisor lazy fetch via the env var that actually works
    (measured: ``-c fetch.disableLazyFetch=true`` alone is not honoured for ``git log
    --numstat`` on git 2.50.1/Apple, but ``GIT_NO_LAZY_FETCH=1`` is)."""
    return env.get("GIT_NO_LAZY_FETCH") == "1"


def test_content_free_traversals_disable_commit_graph_and_lazy_fetch(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")

    calls = _captured_git_calls(monkeypatch)
    repo._run(["git", "log", "--format=%H", "HEAD"], check=False)
    repo._run(["git", "rev-list", "--count", "HEAD"], check=False)
    repo._run(["git", "shortlog", "-s", "HEAD"], check=False)

    for cmd, env in calls:
        # commit-graph off + lazy fetch off (env var) — neither a stale graph nor a doomed
        # promisor fetch can truncate a content-free walk.
        assert _config_values(cmd, "core.commitGraph") == ["false"], cmd
        assert _no_lazy_fetch(env), (cmd, env)
        # The subcommand is the first bare token (skip ``-c`` and its ``key=value`` operands).
        sub = next(a for a in cmd[1:] if not a.startswith("-") and "=" not in a)
        assert sub in ("log", "rev-list", "shortlog"), cmd


def test_numstat_keeps_lazy_fetch_for_blob_content(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")

    calls = _captured_git_calls(monkeypatch)
    repo._run(["git", "log", "--numstat", "--format=%H", "HEAD"], check=False)

    cmd, env = calls[0]
    # A line-count read still needs blob objects, which a blobless partial clone fetches
    # lazily — so the commit-graph is still bypassed, but lazy fetch stays ON by default or
    # the numbers would be zeroed.
    assert _config_values(cmd, "core.commitGraph") == ["false"], cmd
    assert not _no_lazy_fetch(env), (cmd, env)


def test_allow_lazy_fetch_false_disables_fetch_even_for_content_read(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")

    calls = _captured_git_calls(monkeypatch)
    # The dashboard's full-history scan opts out: a content read that must NOT trigger a
    # promisor fetch (it would pull the whole blobless history every poll).
    repo._run(["git", "log", "--numstat", "--format=%H", "HEAD"], check=False, allow_lazy_fetch=False)

    cmd, env = calls[0]
    assert _config_values(cmd, "core.commitGraph") == ["false"], cmd
    # GIT_NO_LAZY_FETCH=1 is what actually keeps the walk local; the config is set too as a
    # secondary defence, but the env var is the contract this test pins.
    assert _no_lazy_fetch(env), (cmd, env)


def test_mutations_and_plumbing_are_untouched(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])

    calls = _captured_git_calls(monkeypatch)
    repo.commit("c1")
    repo.current_branch()  # git rev-parse --abbrev-ref
    repo.list_branches()  # git for-each-ref

    # Only history *traversal* opts out — neither the commit-graph flag nor the no-lazy-fetch
    # env may leak onto commits (which write the graph) or plumbing that never traverses.
    for cmd, env in calls:
        if _config_values(cmd, "core.commitGraph") or _no_lazy_fetch(env):
            assert any(sub in cmd for sub in ("log", "rev-list", "shortlog")), (cmd, env)


def test_collect_reads_full_history_resiliently(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    # Even with a commit-graph present, the collector must not trust it for any ``git log``.
    repo._run(["git", "commit-graph", "write", "--reachable"], check=False)

    calls = _captured_git_calls(monkeypatch)
    stats = collect_commit_stats(repo, "HEAD")

    assert len(stats) == 7  # the GitRepo.init seed commit + the 6 here
    log_calls = [(cmd, env) for cmd, env in calls if "log" in cmd]
    assert log_calls, "collect_commit_stats should run git log"
    for cmd, env in log_calls:
        assert "core.commitGraph=false" in cmd, cmd
        # The full-history scan disables lazy fetch on EVERY read now — the plain commit-list
        # / parents walks AND the --numstat scan — so a big blobless clone never pulls its whole
        # history (per-page blobs are fetched on demand by apply_numstat_for instead).
        assert _no_lazy_fetch(env), (cmd, env)


def test_apply_numstat_for_fetches_only_the_named_commits(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n" * (i + 1), encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    stats = collect_commit_stats(repo, "HEAD")
    by_sha = {s.sha: s for s in stats if s.sha}
    target = next(s for s in stats if s.subject == "c3")
    target.insertions = target.deletions = 0  # pretend the full scan couldn't read its blobs
    # …and that it recorded no usable answer for it either, which is the state a partial clone
    # is really in when its blobs are missing: nothing EXACT to serve this from.
    (tmp_path / ".agitrack" / "numstat.cache").unlink(missing_ok=True)

    calls = _captured_git_calls(monkeypatch)
    apply_numstat_for(repo, [target.sha], by_sha)

    # Exactly one scoped numstat read, bounded with --no-walk to the commits named on stdin,
    # and it keeps lazy fetch ON so a blobless clone fetches just this page's blobs.
    numstat_calls = [(cmd, env) for cmd, env in calls if "--numstat" in cmd]
    assert len(numstat_calls) == 1, calls
    cmd, env = numstat_calls[0]
    assert "--no-walk=unsorted" in cmd and "--stdin" in cmd
    assert not _no_lazy_fetch(env), (cmd, env)
    # c3 wrote 4 lines into f3.txt; the scoped pass restored its real count.
    assert target.insertions == 4 and target.deletions == 0


def test_a_page_that_was_counted_once_is_never_recounted(tmp_path: Path, monkeypatch) -> None:
    """The counts a page fetch produces are exact and a commit's diff never changes, so they are
    remembered. Without that, every dashboard rebuild re-fetched the displayed page's blobs:
    measured on one partial clone at 18.3s for a 50-commit page and 12.1s for 20 pending turns,
    on every rebuild, which is what made a big repo's dashboard feel like it hangs."""
    repo = GitRepo.init(tmp_path)
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n" * (i + 1), encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    stats = collect_commit_stats(repo, "HEAD")
    by_sha = {s.sha: s for s in stats if s.sha}
    target = next(s for s in stats if s.subject == "c2")
    apply_numstat_for(repo, [target.sha], by_sha)  # warms the cache with the exact count
    counted = (target.insertions, target.deletions)

    target.insertions = target.deletions = 0
    calls = _captured_git_calls(monkeypatch)
    apply_numstat_for(repo, [target.sha], by_sha)

    assert not [cmd for cmd, _env in calls if "--numstat" in cmd], "recounted an already-exact commit"
    assert (target.insertions, target.deletions) == counted


def test_collect_handles_ref_that_collides_with_a_path(tmp_path: Path) -> None:
    """A branch whose name also names a tracked path made the dashboard's commit log show
    NOTHING: ``git log dev`` is "ambiguous argument 'dev': both revision and filename", so git
    aborts with no output and the empty result is used as-is. The reads terminate the revision
    with ``--`` so the branch is read, not mistaken for a pathspec. (This is the bug behind
    "the dashboard doesn't show the commit log" on a repo that has both a ``dev`` branch and a
    ``dev/`` directory — the live page requests ``/log?branch=dev``.)"""
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")
    # A tracked directory whose name equals the branch we will read below.
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "note.md").write_text("note\n", encoding="utf-8")
    repo.stage_paths(["dev/note.md"])
    repo.commit("c2")
    repo._run(["git", "branch", "dev", "HEAD"])

    # Sanity: the bare ``git log dev`` really is ambiguous here (no output, non-zero).
    ambiguous = repo._run(["git", "log", "--format=%H", "dev"], check=False)
    assert ambiguous.returncode != 0 and not ambiguous.stdout.strip()

    stats = collect_commit_stats(repo, "dev")
    assert [s.subject for s in stats] == ["Initial commit", "c1", "c2"]


def _numstat_reads(calls: list[tuple[list[str], dict]]) -> list[list[str]]:
    return [cmd for cmd, _env in calls if "--numstat" in cmd]


def _cache_lines(tmp_path: Path) -> list[str]:
    return (tmp_path / ".agitrack" / "numstat.cache").read_text(encoding="utf-8").splitlines()


def test_the_whole_history_is_diffed_once_and_then_cached(tmp_path: Path, monkeypatch) -> None:
    """Diffing the whole history is the dashboard's dominant cost — 34.7s of a 35.5s build on one
    916-commit repo, redone on every ref move — and it is pure repetition: the diff of a commit
    against its parent is fixed forever."""
    repo = GitRepo.init(tmp_path)
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n" * (i + 1), encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    first = collect_commit_stats(repo, "HEAD")

    calls = _captured_git_calls(monkeypatch)
    second = collect_commit_stats(repo, "HEAD")

    assert not _numstat_reads(calls), "re-diffed history the cache already answered"
    assert [(s.sha, s.insertions, s.deletions) for s in second] == [(s.sha, s.insertions, s.deletions) for s in first]
    assert all(line.endswith(" E") for line in _cache_lines(tmp_path))  # a full clone: exact


def test_a_partial_clones_history_count_is_a_floor_the_page_still_recounts(tmp_path: Path, monkeypatch) -> None:
    """On a partial clone the whole-history scan counts without fetching, so its answer can be
    short — it is cached as a floor, and the commits actually displayed are still recounted with
    fetching allowed (and only then remembered as final)."""
    repo = GitRepo.init(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    repo.stage_paths(["a.txt"])
    repo.commit("c0")
    monkeypatch.setattr(GitRepo, "is_partial_clone", lambda self: True)

    stats = collect_commit_stats(repo, "HEAD")
    assert all(line.endswith(" L") for line in _cache_lines(tmp_path))

    by_sha = {s.sha: s for s in stats if s.sha}
    target = next(s for s in stats if s.subject == "c0")
    calls = _captured_git_calls(monkeypatch)
    apply_numstat_for(repo, [target.sha], by_sha)

    assert _numstat_reads(calls), "a floor must not be served as the exact count"
    assert any(line.startswith(target.sha) and line.endswith(" E") for line in _cache_lines(tmp_path))


def test_a_damaged_numstat_cache_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    """Every number in the cache is recomputable from git, so a truncated write, a half-line from
    two dashboards appending at once, or a file from a future format is a miss — never an error."""
    repo = GitRepo.init(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    repo.stage_paths(["a.txt"])
    repo.commit("c0")
    truth = collect_commit_stats(repo, "HEAD")

    cache = tmp_path / ".agitrack" / "numstat.cache"
    cache.write_text("garbage\n" + truth[-1].sha + " 12 notanumber X\n" + truth[-1].sha[:8], encoding="utf-8")
    again = collect_commit_stats(repo, "HEAD")

    assert [(s.sha, s.insertions, s.deletions) for s in again] == [(s.sha, s.insertions, s.deletions) for s in truth]


def test_the_file_browsers_history_scan_is_cached_too(tmp_path: Path, monkeypatch) -> None:
    """The file tab's ``sha -> files changed`` index is a SECOND whole-history diff, rebuilt on
    every tip move and reached from the ordinary ``/data`` poll (the efficiency insights read
    it), so on a large repo it cost another 30.8s per new commit."""
    from agitrack.metrics.collect import build_dashboard
    from agitrack.metrics.files import git_browser

    repo = GitRepo.init(tmp_path)
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n" * (i + 1), encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    dash = build_dashboard(repo, "HEAD")
    first = git_browser(repo, dash.stats, "HEAD").files_payload()

    calls = _captured_git_calls(monkeypatch)
    second = git_browser(repo, dash.stats, "HEAD").files_payload()

    assert not _numstat_reads(calls), "re-diffed a history the file cache already held"
    assert second == first
    assert first, "the fixture must actually produce file rows"


def test_a_new_commit_only_costs_the_file_browser_that_commit(tmp_path: Path, monkeypatch) -> None:
    """The point of the cache: a rebuild after one new commit diffs one commit, not the history."""
    from agitrack.metrics.collect import build_dashboard
    from agitrack.metrics.files import git_browser

    repo = GitRepo.init(tmp_path)
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x\n", encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    git_browser(repo, build_dashboard(repo, "HEAD").stats, "HEAD")

    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    repo.stage_paths(["new.txt"])
    repo.commit("c3")
    dash = build_dashboard(repo, "HEAD")
    new_sha = next(s.sha for s in dash.stats if s.subject == "c3")

    calls = _captured_git_calls(monkeypatch)
    browser = git_browser(repo, dash.stats, "HEAD")

    reads = _numstat_reads(calls)
    assert len(reads) == 1 and "--stdin" in reads[0], reads  # one scoped read, not a walk
    assert "new.txt" in {row["path"] for row in browser.files_payload()}
    assert new_sha  # the commit really is the one that was missing from the cache
