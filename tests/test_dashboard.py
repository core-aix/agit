"""`agit --dashboard` (#54): repo metrics computed from aGiT commit metadata.

The collector reads everything from `git log` alone, so the tests build a real
repository whose history contains every commit kind the classifier knows:
untracked (plain git), user (commit_type: user), agent (commit_type: agent,
with token metadata), backend-made commits covered by a merge-shaped cover
commit (#58), and an agent-merge.
"""

from pathlib import Path

import json
import re
import threading
import urllib.error
import urllib.request

import agit.metrics as metrics
from agit import cli
from agit.commits import build_agent_commit_message, build_agent_merge_message, build_user_commit_message
from agit.git import GitRepo
from agit.metrics import build_dashboard, build_server, dashboard_data, render_dashboard, render_html
from agit.metrics.collect import CommitStat, _detect_loops, resolve_committers


def _write_lines(repo: GitRepo, name: str, count: int) -> None:
    (repo.repo / name).write_text("".join(f"line {i}\n" for i in range(count)), encoding="utf-8")
    repo.stage_paths([name])


def _agent_message(prompt: str, *, tokens: dict | None = None, covered: list[str] | None = None) -> str:
    return build_agent_commit_message(
        latest_prompt=prompt,
        trace=[{"role": "user", "content": prompt}, {"role": "agent", "content": "done"}],
        backend="claude",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="claude-opus-4-8",
        token_usage=tokens,
        covered_commits=covered,
    )


_TOKENS = {
    "context": 100,
    "total": 50,
    "input": 1000,
    "output": 50,
    "reasoning": 0,
    "cache_read": 0,
    "cache_write": 0,
}


def _demo_repo(tmp_path: Path) -> GitRepo:
    repo = GitRepo.init(tmp_path)  # seed commit: untracked

    _write_lines(repo, "human.txt", 10)
    repo.commit("plain human commit")  # untracked

    _write_lines(repo, "user.txt", 4)
    repo.commit(build_user_commit_message(message="save my edits", agit_session_id="agit-1"))  # user

    _write_lines(repo, "agent.txt", 20)
    repo.commit(_agent_message("add the feature", tokens=_TOKENS))  # agent, 1050 in / 50 out

    # Backend-made commit covered by a merge-shaped cover commit (#58).
    turn_start = repo.rev_parse("HEAD")
    _write_lines(repo, "backend.txt", 8)
    repo.commit("backend made this itself")
    backend_sha = repo.rev_parse("HEAD")
    repo.cover_commit(
        _agent_message("refactor the parser", tokens=_TOKENS, covered=[repo.short_sha(backend_sha)]),
        first_parent=turn_start,
        second_parent=backend_sha,
    )

    repo.commit_message("HEAD")  # sanity: readable
    repo._run(
        ["git", "commit", "--allow-empty", "-F", "-"],
        input_text=build_agent_merge_message(
            session_name="s1",
            base_branch="main",
            source_branch="agit/claude/s1/t1",
            agit_session_id="agit-1",
            backend="claude",
        ),
    )  # agent-merge
    return repo


def test_dashboard_classifies_every_commit_kind(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    kinds = {stat.kind for stat in dash.stats}
    assert kinds == {"untracked", "user", "agent", "covered", "agent-merge"}
    assert dash.count("agent") == 2  # the regular agent commit + the cover commit
    assert dash.count("covered") == 1  # the backend-made commit, identified via covered_commits
    assert dash.count("untracked") == 2  # seed + plain human commit
    assert dash.total_commits == 7
    assert dash.tracked_commits == 5
    assert abs(dash.coverage - 5 / 7) < 1e-9


def test_dashboard_splits_lines_into_tracked_ai_and_nontracked(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    ai_ins, _ = dash.ai_lines
    nt_ins, _ = dash.nontracked_lines
    # aGiT-tracked AI: agent.txt (20) + backend.txt (8, via the covered commit);
    # the cover commit itself is a merge and contributes no numstat — no double
    # count.
    assert ai_ins == 28
    # Non-tracked: the user commit (user.txt, 4) + the plain commit (human.txt,
    # 10). We do not claim these as "human" — a user commit's lines may still be
    # AI-produced off the record; they are simply not tracked as AI.
    assert nt_ins == 14


def test_squash_or_pr_merge_with_many_metadata_blocks_is_non_tracked(tmp_path):
    # A squash / PR-merge message concatenates several commits' metadata blocks.
    # Its first block here is an agent commit, which would otherwise credit the
    # whole squashed diff as aGiT-tracked AI. More than one metadata block ⇒
    # aggregate of unknown provenance ⇒ non-tracked, not AI.
    repo = GitRepo.init(tmp_path)
    _write_lines(repo, "squashed.txt", 500)
    message = (
        "Stability improvements (#9)\n\n"
        "# aGiT Metadata\ncommit_type: agent\nbackend: claude\n\n"
        "# aGiT Metadata\ncommit_type: user\nagit_session_id: s1\n"
    )
    repo.commit(message)

    dash = build_dashboard(repo)
    squashed = next(s for s in dash.stats if s.subject.startswith("Stability"))
    assert squashed.kind == "untracked"
    assert dash.ai_lines == (0, 0)  # none of the 500 lines counted as AI
    assert dash.nontracked_lines[0] == 500


def test_dashboard_sums_tokens_and_efficiency(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    totals = dash.token_totals
    assert totals["input"] == 2000  # two agent commits, 1000 input tokens each
    assert totals["output"] == 100
    assert dash.lines_per_1k_output_tokens == (28 + 0) / 100 * 1000


def test_dashboard_groups_by_backend_model_and_author(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    backend = dash.by_backend["claude"]
    assert backend["commits"] == 2
    # The cover commit's bucket includes the covered backend commit's lines.
    assert backend["insertions"] == 28
    assert dash.by_model["claude-opus-4-8"]["commits"] == 2
    (author_stats,) = dash.by_author.values()
    assert author_stats["commits"] == 7
    assert author_stats["agit_commits"] == 5
    # The agent/covered lines this committer git-authored are reported as
    # AI-driven (28); their user + plain commits are non-tracked (4 + 10), never
    # claimed as their own human lines.
    assert author_stats["ai_insertions"] == 28
    assert author_stats["nontracked_insertions"] == 14


def test_render_dashboard_contains_all_sections(tmp_path):
    text = render_dashboard(_demo_repo(tmp_path))

    for heading in (
        "aGiT Dashboard",
        "Coverage",
        "Code changes",
        "Tokens",
        "By backend",
        "By model",
        "By committer",
        "Possible loops",
    ):
        assert heading in text
    assert "aGiT-tracked commits: 5/7" in text
    assert "claude-opus-4-8" in text
    # Lines are split only two ways now — tracked AI vs non-tracked — with no
    # "human" category, since a user commit's lines may still be AI-produced.
    assert "aGiT-tracked AI:" in text
    assert "Non-tracked:" in text
    assert "Human (" not in text and "human (own code)" not in text


def test_pr_merge_commit_does_not_double_count_the_cover_turn(tmp_path):
    """A GitHub-style merge that inherits the cover commit's message (and so its
    metadata block) must not be counted as a second turn (#58 regression)."""
    repo = GitRepo.init(tmp_path)

    _write_lines(repo, "agent.txt", 20)
    cover_message = _agent_message("add the feature", tokens=_TOKENS)
    repo.commit(cover_message)
    cover_sha = repo.rev_parse("HEAD")

    base = build_dashboard(repo)

    # Integrate the branch with a real merge commit whose message is a verbatim
    # copy of the cover commit's — exactly what GitHub does on PR merge.
    repo._run(["git", "checkout", "-q", "-b", "base", "HEAD~1"])
    repo._run(
        ["git", "merge", "--no-ff", "-m", cover_message, cover_sha],
    )

    merged = build_dashboard(repo)

    assert merged.token_totals == base.token_totals
    assert merged.count("agent") == base.count("agent")
    assert merged.by_backend["claude"]["output_tokens"] == base.by_backend["claude"]["output_tokens"]


# --- committer identity merging ------------------------------------------------


def _person(name: str, email: str, kind: str = "agent") -> CommitStat:
    return CommitStat(sha=name + email, author=name, email=email, subject="", kind=kind)


def test_resolve_committers_merges_name_variants_sharing_an_email():
    stats = [
        _person("Pat Example", "pat@example.com"),
        _person("Pat Example", "pat@example.com"),
        _person("Patricia Example", "pat@example.com"),
    ]
    labels = resolve_committers(stats)
    # One identity, labelled with the most frequent name.
    assert set(labels.values()) == {"Pat Example"}


def test_resolve_committers_bridges_personal_and_noreply_via_login():
    stats = [
        _person("dev", "octodev@example.com"),
        _person("Dev Example", "octodev@users.noreply.github.com"),
    ]
    labels = resolve_committers(stats)
    # Same GitHub login across a personal email and a no-reply address (the
    # personal email's local-part matches the login) → one identity, labelled
    # with the login.
    assert set(labels.values()) == {"octodev"}


def test_resolve_committers_merges_same_name_across_two_emails():
    stats = [
        _person("Sam Sample", "sam@sample.test"),
        _person("Sam Sample", "sam.sample@example.com"),
    ]
    # No login signal, but the identical name yields the same label, so the two
    # collapse to one committer downstream.
    assert set(resolve_committers(stats).values()) == {"Sam Sample"}


def test_resolve_committers_keeps_distinct_people_apart():
    stats = [
        _person("Alice Example", "alice@example.com"),
        _person("Bob Example", "bob@example.com"),
    ]
    assert len(set(resolve_committers(stats).values())) == 2


# --- loop detection ------------------------------------------------------------


def _agent_stat(sha: str, prompt: str, output: int = 10, user_prompts: list[str] | None = None) -> CommitStat:
    return CommitStat(
        sha=sha,
        author="a",
        email="a@example.com",
        subject=f"<aGiT> {prompt}",
        kind="agent",
        tokens={"output": output},
        prompt=prompt,
        user_prompts=user_prompts or [],
    )


def test_loop_detected_across_consecutive_similar_prompts():
    stats = [
        _agent_stat("a" * 40, "fix the failing parser test"),
        _agent_stat("b" * 40, "fix the failing parser test again"),
        _agent_stat("c" * 40, "please fix the failing parser test"),
        _agent_stat("d" * 40, "write the changelog"),
    ]

    (finding,) = _detect_loops(stats)
    assert finding.shas == ["a" * 7, "b" * 7, "c" * 7]
    assert finding.output_tokens == 30
    assert finding.within_commit is False


def test_no_loop_for_two_similar_prompts_or_distinct_prompts():
    stats = [
        _agent_stat("a" * 40, "fix the failing parser test"),
        _agent_stat("b" * 40, "fix the failing parser test again"),  # a retry, not a loop
        _agent_stat("c" * 40, "add the dashboard renderer"),
    ]
    assert _detect_loops(stats) == []


def test_loop_detected_within_a_single_commit_trace():
    stats = [
        _agent_stat(
            "a" * 40,
            "make the tests pass",
            output=99,
            user_prompts=["make the tests pass", "make the tests pass", "make the tests pass now"],
        )
    ]

    (finding,) = _detect_loops(stats)
    assert finding.within_commit is True
    assert finding.output_tokens == 99


def test_dashboard_loops_from_real_history(tmp_path):
    repo = GitRepo.init(tmp_path)
    for index, name in enumerate(("a.txt", "b.txt", "c.txt")):
        _write_lines(repo, name, 2)
        repo.commit(_agent_message("fix the flaky integration test", tokens=_TOKENS))

    dash = build_dashboard(repo)
    assert len(dash.loops) == 1
    assert len(dash.loops[0].shas) == 3
    assert "Possible loops" in render_dashboard(repo)


# --- HTML dashboard (filterable web view) --------------------------------------


def _embedded_data(html: str) -> dict:
    match = re.search(r'id="agit-data">(.*?)</script>', html, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_dashboard_data_serializes_every_commit_with_filters(tmp_path):
    data = dashboard_data(build_dashboard(_demo_repo(tmp_path)))

    assert len(data["commits"]) == 7
    assert data["branch"]
    assert data["head"]  # HEAD sha, so the live page can skip no-op re-renders
    # Filter option lists are derived from the (effective) commit fields.
    assert data["backends"] == ["claude"]
    assert data["models"] == ["claude-opus-4-8"]
    assert data["committers"]  # at least the one git author


def test_dashboard_data_covered_commit_inherits_effective_backend(tmp_path):
    data = dashboard_data(build_dashboard(_demo_repo(tmp_path)))

    covered = next(c for c in data["commits"] if c["kind"] == "covered")
    # The backend-made commit carries no metadata of its own, but inherits the
    # cover commit's backend/model so a per-backend filter buckets it correctly.
    assert covered["backend"] is None
    assert covered["eff_backend"] == "claude"
    assert covered["eff_model"] == "claude-opus-4-8"


def test_render_html_is_self_contained_with_embedded_data(tmp_path):
    html = render_html(_demo_repo(tmp_path))

    assert html.startswith("<!DOCTYPE html>")
    assert "aGiT dashboard" in html
    # The page ships its data inline so it renders instantly, then polls /data
    # to stay live. The filter UI is present.
    data = _embedded_data(html)
    assert len(data["commits"]) == 7
    for control in ('id="f-author"', 'id="f-backend"', 'id="f-model"'):
        assert control in html


# --- live localhost server -----------------------------------------------------


def _serve(repo: GitRepo):
    server = build_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8")


def test_dashboard_server_serves_html_and_live_data(tmp_path):
    repo = _demo_repo(tmp_path)
    server, thread, base = _serve(repo)
    try:
        html = _get(base + "/")
        assert "<!DOCTYPE html>" in html and 'id="f-author"' in html

        data = json.loads(_get(base + "/data"))
        assert len(data["commits"]) == 7
        first_head = data["head"]

        # /data is recomputed each request, so a new commit shows up live.
        _write_lines(repo, "live.txt", 3)
        repo.commit(_agent_message("add a live change", tokens=_TOKENS))
        refreshed = json.loads(_get(base + "/data"))
        assert refreshed["head"] != first_head
        assert len(refreshed["commits"]) == 8
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_404s_unknown_paths(tmp_path):
    server, thread, base = _serve(_demo_repo(tmp_path))
    try:
        try:
            _get(base + "/nope")
            raise AssertionError("expected HTTP 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- CLI -----------------------------------------------------------------------


def test_cli_dashboard_html_is_default_and_serves_on_localhost(tmp_path, monkeypatch):
    _demo_repo(tmp_path)
    served: dict[str, GitRepo] = {}

    def fake_serve(repo, **kwargs):
        served["repo"] = repo
        return 0

    monkeypatch.setattr(metrics, "serve_dashboard", fake_serve)

    # Bare --dashboard now means html, which serves on localhost.
    rc = cli.main(["--dashboard", "--repo", str(tmp_path)])

    assert rc == 0
    assert served["repo"].repo == GitRepo.discover(tmp_path).repo


def test_cli_dashboard_shorthand_d_serves_like_dashboard(tmp_path, monkeypatch):
    _demo_repo(tmp_path)
    served: dict[str, GitRepo] = {}

    def fake_serve(repo, **kwargs):
        served["repo"] = repo
        return 0

    monkeypatch.setattr(metrics, "serve_dashboard", fake_serve)

    # `-d` is shorthand for `--dashboard`; bare form defaults to html (serve).
    assert cli.main(["-d", "--repo", str(tmp_path)]) == 0
    assert served["repo"].repo == GitRepo.discover(tmp_path).repo


def test_cli_dashboard_shorthand_d_accepts_text(tmp_path, capsys, monkeypatch):
    _demo_repo(tmp_path)
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("text must not serve"))
    )

    assert cli.main(["-d", "text", "--repo", str(tmp_path)]) == 0
    assert "aGiT Dashboard" in capsys.readouterr().out


def test_cli_dashboard_text_prints_report_and_exits(tmp_path, capsys, monkeypatch):
    _demo_repo(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("dashboard must not prompt")))
    # The text path must never start a server.
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("text must not serve"))
    )

    rc = cli.main(["--dashboard", "text", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "aGiT Dashboard" in out
    assert "aGiT-tracked commits" in out


def test_cli_dashboard_outside_repo_fails_cleanly(tmp_path, capsys, monkeypatch):
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not serve"))
    )

    rc = cli.main(["--dashboard", "--repo", str(tmp_path / "plain")])

    assert rc == 1
    assert "Not a Git repository" in capsys.readouterr().out


def test_cli_dashboard_missing_directory_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not serve"))
    )
    assert cli.main(["--dashboard", "--repo", str(tmp_path / "nowhere")]) == 1
