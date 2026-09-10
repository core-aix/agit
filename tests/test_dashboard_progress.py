"""The first load of a repository the dashboard has never indexed must SAY what it is doing.

That load diffs every commit in the history (line counts, then the per-file index). Both are
cached per commit afterwards, so it happens once, but on a large repository "once" is minutes,
and the page had only a spinner and the words "a large repo can take a few seconds" to show for
it, which is indistinguishable from a hang.

So the build reports its progress into a small registry (``metrics/progress.py``) and the page
asks for it on ``/progress`` while its own ``/data`` fetch is still in flight. That only works
because the two requests are answered on different threads of the same threading server, which
is what these tests stand in for.
"""

from __future__ import annotations

import json
import threading

from agitrack.git import GitRepo
from agitrack.metrics import progress
from agitrack.metrics.server import RepoScope


def _repo_with_commits(tmp_path, count: int = 4) -> GitRepo:
    repo = GitRepo.init(tmp_path)
    for i in range(count):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n" * (i + 1), encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    return repo


def test_progress_is_readable_while_a_build_is_running(tmp_path):
    """The whole point: a thread that is not the one doing the work can see how far it has got."""
    seen: list[dict] = []
    reached = threading.Event()
    release = threading.Event()

    def build() -> None:
        with progress.publishing(tmp_path):
            progress.step("counting changed lines", 128, 916)
            reached.set()
            release.wait(5)

    worker = threading.Thread(target=build)
    worker.start()
    reached.wait(5)
    seen.append(progress.snapshot(tmp_path))
    release.set()
    worker.join(5)

    assert seen[0]["active"] is True
    assert (seen[0]["stage"], seen[0]["done"], seen[0]["total"]) == ("counting changed lines", 128, 916)
    assert progress.snapshot(tmp_path) == {"active": False}, "a finished build must stop reporting"


def test_reporting_with_nobody_publishing_is_a_no_op():
    """`step` sits in the hot loop of every history scan, including the ones no page is waiting
    on (an export, a CLI report), so it has to cost nothing and never raise."""
    progress.step("counting changed lines", 1, 2)  # no publishing scope on this thread
    assert progress.snapshot("/nowhere") == {"active": False}


def test_a_nested_build_does_not_take_the_answer_away(tmp_path):
    """`/data` builds the dashboard and then the file index, each publishing. If the inner scope
    owned the registry entry, finishing it would report "nothing running" while the request was
    still crunching, and the page's bar would vanish mid-load."""
    with progress.publishing(tmp_path):
        progress.step("counting changed lines", 1, 10)
        with progress.publishing(tmp_path):
            progress.step("indexing changed files", 2, 10)
        assert progress.snapshot(tmp_path)["active"] is True
        assert progress.snapshot(tmp_path)["stage"] == "indexing changed files"
    assert progress.snapshot(tmp_path) == {"active": False}


def test_the_progress_route_answers_without_building_anything(tmp_path, monkeypatch):
    """It is polled every half second WHILE the expensive request runs, so it must not touch git:
    a /progress that queued behind the build it describes would report nothing until the thing it
    is reporting on had already finished."""
    repo = _repo_with_commits(tmp_path)
    scope = RepoScope(repo)
    calls: list[list[str]] = []
    original = repo._run
    monkeypatch.setattr(repo, "_run", lambda command, **kwargs: (calls.append(command), original(command, **kwargs))[1])

    body = json.loads(scope.get("/progress", {}).body)

    assert body == {"active": False}
    assert not calls, f"/progress ran git: {calls}"


def test_two_requests_do_not_index_the_same_history_twice(tmp_path, monkeypatch):
    """The server is threaded, so a second tab (or the hub) asking for the same repository used
    to start its own copy of the same minutes-long index, doubling the work and writing the same
    rows into the cache twice. The second one waits and takes the first one's result."""
    from agitrack.metrics import server as server_mod

    repo = _repo_with_commits(tmp_path)
    scope = RepoScope(repo)
    builds: list[str] = []
    original_build = server_mod.build_dashboard

    def counted(*args, **kwargs):
        builds.append("build")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(server_mod, "build_dashboard", counted)
    threads = [threading.Thread(target=lambda: scope._dashboard("HEAD")) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert len(builds) == 1, f"{len(builds)} concurrent builds of one repository"
