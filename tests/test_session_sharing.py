"""Sharing full agent sessions via git (issue #55).

Real temp repos exercise the history-free shared-session ref, redaction, identity
resolution, the Claude transcript import/export, and a push/fetch round-trip
through a local bare remote.
"""

import subprocess


from agit.git import GitRepo
from agit.sessions import SharedSessionStore, github_login, redact_transcript
from agit.sessions.identity import slug


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _manifest(name, *, session_id, updated, model="claude-opus-4-8"):
    return {"github_id": "alice", "name": name, "session_id": session_id, "updated": updated, "model": model}


# --- redaction --------------------------------------------------------------


def test_redact_masks_secrets_and_home_path_but_keeps_structure():
    line = '{"cwd":"/Users/alice/Code/x","t":"api_key=sk-ABCDEFGHIJKLMNOP token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}'
    out = redact_transcript(line)
    assert "sk-ABCDEFGHIJKLMNOP" not in out and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out
    assert "[REDACTED]" in out
    assert "/Users/alice" not in out and "/Users/user/Code/x" in out  # username masked, path kept
    assert out.startswith('{"cwd"')  # JSON shape preserved


def test_redact_leaves_ordinary_text_untouched():
    assert redact_transcript("just a normal sentence\nsecond line") == "just a normal sentence\nsecond line"


# --- identity ---------------------------------------------------------------


def test_github_login_prefers_gh(monkeypatch):
    import agit.sessions.identity as identity

    monkeypatch.setattr(identity.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="octocat\n", stderr=""),
    )
    assert github_login() == "octocat"


def test_github_login_falls_back_to_git_name(tmp_path, monkeypatch):
    import agit.sessions.identity as identity

    monkeypatch.setattr(identity.shutil, "which", lambda _: None)  # no gh
    repo = _init_repo(tmp_path)
    assert github_login(repo) == "Test-User"  # slug of "Test User"


def test_slug_is_safe():
    assert slug("a/b..c d") == "a-b-c-d"
    assert slug("") == "anonymous"


# --- store: share / list / read / rewrite / fingerprint / prune -------------


def test_share_lists_and_reads_back(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="alice",
        name="fix-parser",
        transcript="hello",
        manifest=_manifest("fix-parser", session_id="id1", updated=10),
    )
    entries = store.entries()
    assert [e.display for e in entries] == ["alice/fix-parser"]
    assert store.read_transcript(entries[0]) == "hello"
    assert entries[0].manifest["session_id"] == "id1"


def test_shared_ref_is_history_free_and_keeps_only_latest(tmp_path):
    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(github_id="alice", name="s", transcript="v1", manifest=_manifest("s", session_id="id", updated=1))
    store.publish(github_id="alice", name="s", transcript="v2", manifest=_manifest("s", session_id="id", updated=2))
    # The ref is a single parent-less commit (no history), holding only the latest.
    assert repo.parents(store.ref) == []
    assert store.read_transcript(store.entries()[0]) == "v2"


def test_entries_are_scoped_to_this_repo_fingerprint(tmp_path):
    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(
        github_id="alice", name="mine", transcript="x", manifest=_manifest("mine", session_id="id", updated=1)
    )
    # Inject an entry under a DIFFERENT repo fingerprint directly into the ref.
    paths = repo.read_tree_paths(store.ref)
    paths["other-repo-root/bob/theirs/transcript.jsonl"] = repo.write_blob("foreign")
    paths["other-repo-root/bob/theirs/manifest.json"] = repo.write_blob("{}")
    repo.update_ref(store.ref, repo.commit_tree_orphan(repo.write_tree_from(paths), "inject"))
    # Only this repo's session surfaces; the foreign-fingerprint one is hidden.
    assert [e.display for e in store.entries()] == ["alice/mine"]


def test_prune_keeps_only_the_newest_k_per_contributor(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    for i in range(7):
        store.publish(
            github_id="alice",
            name=f"s{i}",
            transcript=f"t{i}",
            manifest=_manifest(f"s{i}", session_id=f"id{i}", updated=100 + i),
            keep=3,
        )
    names = [e.name for e in store.entries()]
    assert names == ["s6", "s5", "s4"]  # newest 3 kept, older pruned


def test_prune_never_touches_other_contributors(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="bob",
        name="keep",
        transcript="b",
        manifest={"github_id": "bob", "name": "keep", "session_id": "b1", "updated": 1},
    )
    for i in range(5):
        store.publish(
            github_id="alice",
            name=f"s{i}",
            transcript="a",
            manifest=_manifest(f"s{i}", session_id=f"a{i}", updated=10 + i),
            keep=2,
        )
    displays = {e.display for e in store.entries()}
    assert "bob/keep" in displays  # bob's single session survives alice's pruning
    assert sum(1 for e in store.entries() if e.github_id == "alice") == 2


# --- push / fetch round-trip through a bare remote --------------------------


def test_publish_pushes_and_a_clone_can_fetch_and_resume(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (tmp_path / "src").mkdir()
    src = _init_repo(tmp_path / "src")
    # Push the source's default branch (name varies with git's init.defaultBranch:
    # main vs master) and point the bare remote's HEAD at it, so the clone checks
    # it out and has a born HEAD — otherwise root_commit() (the fingerprint) is
    # unborn in CI where the default branch differs.
    branch = src.current_branch()
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=src.repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=src.repo, check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)

    result = SharedSessionStore(src).publish(
        github_id="alice",
        name="shared",
        transcript="conversation",
        manifest=_manifest("shared", session_id="sid", updated=5),
    )
    assert result.remote and result.pushed

    subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / "clone")], check=True)
    clone_store = SharedSessionStore(GitRepo(tmp_path / "clone"))
    assert clone_store.fingerprint() == SharedSessionStore(src).fingerprint()  # clone-stable
    assert clone_store.fetch()
    entries = clone_store.entries()
    assert [e.display for e in entries] == ["alice/shared"]
    assert clone_store.read_transcript(entries[0]) == "conversation"


def test_publish_without_remote_saves_locally(tmp_path):
    result = SharedSessionStore(_init_repo(tmp_path)).publish(
        github_id="alice", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.remote is False and result.pushed is False


def test_unshare_removes_only_that_entry(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(github_id="alice", name="keep", transcript="k", manifest=_manifest("keep", session_id="k", updated=1))
    store.publish(github_id="alice", name="drop", transcript="d", manifest=_manifest("drop", session_id="d", updated=2))
    store.unshare("alice", "drop")
    assert [e.name for e in store.entries()] == ["keep"]


# --- auto-share opt-in state ------------------------------------------------


def test_state_auto_share_opt_in(tmp_path):
    from agit.config import AgitState

    state = AgitState(tmp_path)
    assert state.auto_share_enabled("sid") is False
    state.set_auto_share("sid", True)
    assert state.auto_share_enabled("sid") is True
    assert "sid" in state.auto_share_session_ids()
    state.set_auto_share("sid", False)
    assert state.auto_share_enabled("sid") is False
    assert state.auto_share_enabled(None) is False


# --- Claude transcript export / import --------------------------------------


def test_claude_export_and_import_retargets_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from agit.transcripts import claude

    src = tmp_path / "srcrepo"
    src.mkdir()
    project = claude._project_dir(src)
    project.mkdir(parents=True)
    (project / "sid.jsonl").write_text('{"type":"user","cwd":"/Users/alice/old","x":1}\n{"noop":true}\n')

    raw = claude.export_session_raw(src, "sid")
    assert raw is not None and "/Users/alice/old" in raw

    dst = tmp_path / "dstrepo"
    dst.mkdir()
    assert claude.import_shared_session(dst, "sid", raw)
    imported = (claude._project_dir(dst) / "sid.jsonl").read_text()
    assert str(dst.resolve()) in imported and "/Users/alice/old" not in imported
    assert claude.session_belongs_to_repo(dst, "sid")
    # Re-importing must not clobber an existing local transcript.
    (claude._project_dir(dst) / "sid.jsonl").write_text("LOCAL")
    assert claude.import_shared_session(dst, "sid", raw)
    assert (claude._project_dir(dst) / "sid.jsonl").read_text() == "LOCAL"


# --- runner glue: share + resume-shared through the session menu ------------


class _StubBackend:
    name = "claude"
    supports_session_sharing = True

    def __init__(self, transcript="conversation text"):
        self._transcript = transcript
        self.imported: tuple | None = None

    def session_belongs_to_repo(self, repo, session_id):
        return True

    def export_session_raw(self, repo, session_id):
        return self._transcript

    def transcript_size(self, repo, session_id):
        return len(self._transcript.encode("utf-8"))

    def import_shared_session(self, repo, session_id, transcript):
        self.imported = (session_id, transcript)
        return True


def _runner_with_store(tmp_path, monkeypatch, backend):
    from proxy_helpers import make_runner
    from agit.config import AgitState, GlobalConfig

    (tmp_path / "repo").mkdir()
    repo = _init_repo(tmp_path / "repo")
    state = AgitState(tmp_path / "repo")
    runner = make_runner(repo=repo, state=state)
    runner.base_repo = repo
    runner.backend = backend
    runner.state.backend_session_id = "sid-123"
    runner.global_config = GlobalConfig(path=tmp_path / "config.json")
    runner.global_config.acknowledge_session_sharing()  # skip the consent prompt
    runner.global_config.github_login = "tester"  # deterministic identity (no gh call)
    runner._render = lambda: None
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    return runner, repo


def test_runner_share_session_publishes_and_redacts(tmp_path, monkeypatch):
    secret = '{"t":"token sk-ABCDEFGHIJKLMNOPQR"}'
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript=secret))

    runner._share_session()

    store = SharedSessionStore(repo)
    entries = store.entries()
    assert len(entries) == 1
    transcript = store.read_transcript(entries[0])
    assert "sk-ABCDEFGHIJKLMNOPQR" not in transcript and "[REDACTED]" in transcript  # redacted
    assert entries[0].manifest["session_id"] == "sid-123"
    assert any("Saved shared session" in m or "Shared" in m for m in runner.messages)


def test_runner_share_unsupported_backend_shows_message(tmp_path, monkeypatch):
    class _OpenCodeStub:
        name = "opencode"
        supports_session_sharing = False

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _OpenCodeStub())

    runner._share_session()

    assert SharedSessionStore(repo).entries() == []  # nothing shared
    assert any("isn't supported" in m and "opencode" in m for m in runner.messages)


def test_runner_resume_shared_imports_and_resumes(tmp_path, monkeypatch):
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    # Seed a shared session as if a teammate published it.
    SharedSessionStore(repo).publish(
        github_id="bob",
        name="cool-fix",
        transcript="bob's chat",
        manifest={"github_id": "bob", "name": "cool-fix", "session_id": "bob-sid", "updated": 99},
    )
    resumed = []
    runner._resume_conversation = lambda name, sid: resumed.append((name, sid))
    runner._select_popup = lambda title, options: options[0]  # pick the first (only) entry

    runner._resume_shared_session_menu()

    assert backend.imported == ("bob-sid", "bob's chat")  # transcript installed for resume
    assert resumed == [("bob-cool-fix", "bob-sid")]  # resumed under <id>-<name>


def test_runner_auto_share_publishes_in_background(tmp_path, monkeypatch):
    backend = _StubBackend(transcript='{"t":"a new turn"}')
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)

    runner._maybe_auto_share_active()
    if runner._auto_share_thread is not None:
        runner._auto_share_thread.join(timeout=10)

    entries = SharedSessionStore(repo).entries()
    assert len(entries) == 1 and entries[0].manifest["session_id"] == "sid-123"
    # The throttle timestamp is stamped on every check, so an immediate re-check is
    # debounced and does NOT re-do the (potentially large) read/redact or re-spawn —
    # this is the fix for the "terminal becomes slow" bug.
    runner._auto_share_thread = None
    runner._maybe_auto_share_active()
    assert runner._auto_share_thread is None


def test_auto_share_main_thread_does_no_heavy_work(tmp_path, monkeypatch):
    # The reactor-thread part must never read/redact the transcript itself — that
    # happens in the worker. Prove it by making export_session_raw blow up if the
    # MAIN thread ever calls it; only the spawned worker may.
    import threading

    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    main = threading.get_ident()
    calls = {"main": 0}
    real_export = backend.export_session_raw

    def guarded(repo_path, sid):
        if threading.get_ident() == main:
            calls["main"] += 1
        return real_export(repo_path, sid)

    backend.export_session_raw = guarded
    runner.state.set_auto_share("sid-123", True)

    runner._maybe_auto_share_active()
    if runner._auto_share_thread is not None:
        runner._auto_share_thread.join(timeout=10)

    assert calls["main"] == 0  # the transcript was only read on the worker thread
    assert SharedSessionStore(repo).entries()  # and the worker still shared it


def test_runner_auto_share_skipped_when_not_opted_in(tmp_path, monkeypatch):
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    runner._maybe_auto_share_active()  # session not opted in
    assert runner._auto_share_thread is None
    assert SharedSessionStore(repo).entries() == []


def test_runner_manage_unshare_removes_session(tmp_path, monkeypatch):
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="session-1",
        transcript="t",
        manifest={
            "github_id": "tester",
            "name": "session-1",
            "session_id": "sid-123",
            "updated": 1,
            "content_hash": "h",
        },
    )
    # First popup picks the (only) session; the "Manage" popup picks Unshare (3rd action).
    runner._select_popup = lambda title, options: options[2] if title.startswith("Manage") else options[0]

    runner._manage_shared_sessions_menu()

    assert SharedSessionStore(repo).entries() == []


def test_manage_menu_opens_without_fetch_or_transcript_read(tmp_path, monkeypatch):
    # The menu must open instantly: no network fetch, and no transcript read/redact
    # while building the list (the "takes a few seconds" bug).
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="s1",
        transcript="t",
        manifest={"github_id": "tester", "name": "s1", "session_id": "sid-123", "updated": 1, "transcript_bytes": 5},
    )

    def boom_fetch(*a, **k):
        raise AssertionError("manage menu must not fetch from the network")

    def boom_read(*a, **k):
        raise AssertionError("manage menu must not read/redact transcripts to build the list")

    monkeypatch.setattr(repo, "fetch_ref", boom_fetch)
    backend.export_session_raw = boom_read
    captured = {}
    runner._select_popup = lambda title, options: captured.update(title=title, options=options) or None

    runner._manage_shared_sessions_menu()

    assert captured["title"].startswith("Your shared sessions")
    assert len(captured["options"]) == 1 and "s1" in captured["options"][0]


def test_shared_entry_status_is_size_based(tmp_path, monkeypatch):
    from agit.sessions import SharedEntry

    backend = _StubBackend(transcript="x" * 100)  # current transcript = 100 bytes
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, backend)
    up_to_date = SharedEntry("tester", "s", {"session_id": "sid-123", "transcript_bytes": 100})
    grown = SharedEntry("tester", "s", {"session_id": "sid-123", "transcript_bytes": 50})
    unknown = SharedEntry("tester", "s", {"session_id": "sid-123"})  # no recorded size
    assert "up to date" in runner._shared_entry_status(up_to_date, "sid-123")
    assert "newer turns" in runner._shared_entry_status(grown, "sid-123")
    assert runner._shared_entry_status(unknown, "sid-123") == "shared"


def test_claude_backend_flags_sharing_support(tmp_path):
    from agit.backends.proxy_agents import make_proxy_agent

    assert make_proxy_agent("claude").supports_session_sharing is True
    opencode = make_proxy_agent("opencode")
    assert opencode.supports_session_sharing is False
    # OpenCode has no portable transcript, so export/import are inert.
    assert opencode.export_session_raw(tmp_path, "x") is None
    assert opencode.import_shared_session(tmp_path, "x", "data") is False
