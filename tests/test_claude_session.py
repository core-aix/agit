import json

from agit.transcripts import claude as claude_session
from agit.transcripts.claude import export_session, latest_session_id, list_sessions, parse_rows, session_belongs_to_repo


def test_session_cwd_reads_last_recorded_cwd(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    proj = config / "projects" / claude_session._encode_repo(tmp_path / "wt")
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        '{"type":"user","cwd":"/old/dir"}\n'
        '{"type":"assistant"}\n'                       # a line without cwd is skipped
        '{"type":"user","cwd":"/new/dir"}\n',
        encoding="utf-8",
    )
    assert claude_session.session_cwd("s") == "/new/dir"   # last cwd wins
    assert claude_session.session_cwd("missing") is None


def test_prepare_resume_stages_transcript_into_worktree(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo_root = tmp_path / "repo"
    worktree = repo_root / ".agit" / "worktrees" / "session-1"
    repo_root.mkdir()
    worktree.mkdir(parents=True)

    # A conversation recorded under the repo root (e.g. a plain `claude` run).
    root_proj = config / "projects" / claude_session._encode_repo(repo_root)
    root_proj.mkdir(parents=True)
    (root_proj / "abc.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

    assert claude_session.prepare_resume(worktree, "abc") is True
    staged = config / "projects" / claude_session._encode_repo(worktree) / "abc.jsonl"
    assert staged.is_file()

    # Hardlinked (one inode), so a turn appended from the worktree is visible in
    # the original directory's transcript too — the conversation does not fork.
    with staged.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    assert (root_proj / "abc.jsonl").read_text(encoding="utf-8").count("\n") == 2

    # Idempotent and id-specific.
    assert claude_session.prepare_resume(worktree, "abc") is True
    assert claude_session.prepare_resume(worktree, "missing") is False


def test_link_session_surfaces_worktree_conversation_in_base(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo_root = tmp_path / "repo"
    worktree = repo_root / ".agit" / "worktrees" / "session-1"
    repo_root.mkdir()
    worktree.mkdir(parents=True)

    # A conversation born inside aGiT (recorded under the worktree project dir).
    wt_proj = config / "projects" / claude_session._encode_repo(worktree)
    wt_proj.mkdir(parents=True)
    (wt_proj / "born.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

    assert claude_session.link_session("born", worktree, repo_root) is True
    base = config / "projects" / claude_session._encode_repo(repo_root) / "born.jsonl"
    assert base.is_file()

    # Hardlinked: a later turn from the worktree is visible from the repo root too.
    with (wt_proj / "born.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    assert base.read_text(encoding="utf-8").count("\n") == 2

    # Idempotent, and a no-op when the source isn't recorded.
    assert claude_session.link_session("born", worktree, repo_root) is True
    assert claude_session.link_session("missing", worktree, repo_root) is False


def _user(uuid, text, **extra):
    row = {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def _assistant(msg_id, text, *, model="claude-opus-4-8", usage=None, content=None, stop_reason=None):
    blocks = content if content is not None else [{"type": "text", "text": text}]
    message = {"id": msg_id, "role": "assistant", "model": model, "content": blocks, "usage": usage or {}}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {"type": "assistant", "message": message}


def test_parse_rows_groups_turns_with_final_response_and_tokens():
    rows = [
        _user("u1", "first prompt"),
        _assistant("m0", "", content=[{"type": "thinking", "thinking": "..."}], usage={"input_tokens": 10, "output_tokens": 5}),
        _assistant(
            "m1",
            "final answer one",
            usage={"input_tokens": 20, "output_tokens": 100, "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 200},
        ),
        _user("u2", "second prompt"),
        _assistant("m2", "final answer two", usage={"input_tokens": 30, "output_tokens": 50}),
    ]

    session = parse_rows("sess-1", rows)

    assert session.session_id == "sess-1"
    assert session.model == "claude-opus-4-8"
    assert len(session.turns) == 2

    turn1 = session.turns[0]
    assert turn1.user_prompt == "first prompt"
    assert turn1.final_response == "final answer one"
    assert turn1.assistant_message_id == "m1"
    # output summed across the turn's assistant messages
    assert turn1.tokens.output == 105
    assert turn1.tokens.total == 105
    # context taken from the last assistant message (input + cache read + cache write)
    assert turn1.tokens.context == 20 + 8000 + 200
    assert turn1.tokens.cache_read == 8000

    assert session.turns[1].user_prompt == "second prompt"
    assert session.turns[1].final_response == "final answer two"


def test_parse_rows_marks_turn_incomplete_while_last_message_is_tool_use():
    # A prompt whose latest assistant message is a tool call is still mid-flight:
    # the agent paused between writing code and writing tests. aGiT must see this
    # turn as incomplete so it doesn't commit now and split the prompt in two.
    rows = [
        _user("u1", "fix the bug and add tests"),
        _assistant(
            "m1",
            "Let me add a sanitizer.",
            content=[{"type": "text", "text": "Let me add a sanitizer."}, {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}],
            stop_reason="tool_use",
        ),
    ]

    turn = parse_rows("sess-mid", rows).turns[0]

    assert turn.final_response == "Let me add a sanitizer."
    assert turn.complete is False


def test_parse_rows_marks_turn_complete_when_last_message_ends_the_turn():
    rows = [
        _user("u1", "fix the bug and add tests"),
        _assistant("m1", "Working on it.", content=[{"type": "text", "text": "Working on it."}, {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}], stop_reason="tool_use"),
        _assistant("m2", "Done — code and tests are in.", stop_reason="end_turn"),
    ]

    turn = parse_rows("sess-done", rows).turns[0]

    assert turn.final_response == "Done — code and tests are in."
    assert turn.complete is True


def test_parse_rows_turn_complete_when_stop_reason_absent():
    # Older transcripts (or other backends) may omit the stop reason; default to
    # complete so the commit loop is never stalled.
    rows = [_user("u1", "hello"), _assistant("m1", "hi")]
    assert parse_rows("sess-old", rows).turns[0].complete is True


def test_parse_rows_excludes_meta_sidechain_tool_results_and_commands():
    rows = [
        _user("c", "<local-command-caveat>noise</local-command-caveat>", isMeta=True),
        _user("s", "<command-name>/model</command-name>"),
        _user("side", "subagent prompt", isSidechain=True),
        {"type": "user", "uuid": "tr", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
        _user("real", "the real prompt"),
        _assistant("m1", "response"),
        # sidechain assistant output must not be attributed to the turn
        {"type": "assistant", "isSidechain": True, "message": {"id": "sx", "content": [{"type": "text", "text": "side"}], "usage": {}}},
    ]

    session = parse_rows("sess-2", rows)

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "the real prompt"
    assert session.turns[0].final_response == "response"


def test_parse_rows_excludes_compaction_summary():
    rows = [
        _user(
            "summary",
            "This session is being continued from a previous conversation...",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
        _user("real", "the real prompt"),
        _assistant("m1", "response"),
    ]

    session = parse_rows("sess-compact", rows)

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "the real prompt"


def test_parse_rows_attributes_sidechain_tokens_to_subagent_buckets():
    rows = [
        _user("real", "the real prompt"),
        _assistant("m1", "response", usage={"input_tokens": 30, "output_tokens": 50}),
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "id": "sx",
                "content": [{"type": "text", "text": "side"}],
                "usage": {"input_tokens": 40, "output_tokens": 60, "cache_read_input_tokens": 700},
            },
        },
    ]

    turn = parse_rows("sess-3", rows).turns[0]

    # Main-line counters reflect only the non-sidechain assistant message.
    assert turn.tokens.output == 50
    assert turn.tokens.input == 30
    # Sidechain consumption is recorded separately, not folded into the main count.
    assert turn.tokens.subagent_input == 40
    assert turn.tokens.subagent_output == 60
    assert turn.tokens.subagent_cache_read == 700
    # The sub-agent's context size never overrides the main turn's context.
    assert turn.tokens.context == 30


def test_export_session_reads_jsonl_from_project_dir(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    rows = [_user("u1", "hello"), _assistant("m1", "hi there", usage={"output_tokens": 3})]
    (project_dir / "abc.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    assert session_belongs_to_repo(repo, "abc")
    assert latest_session_id(repo) == "abc"
    session = export_session(repo, "abc")
    assert session is not None
    assert session.turns[0].user_prompt == "hello"
    assert session.turns[0].final_response == "hi there"


def test_list_sessions_returns_refs_with_labels(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    (project_dir / "s1.jsonl").write_text(json.dumps(_user("u1", "first session prompt")) + "\n")
    (project_dir / "s2.jsonl").write_text(json.dumps(_user("u2", "second session prompt")) + "\n")

    refs = list_sessions(repo)
    by_id = {ref.id: ref for ref in refs}
    assert set(by_id) == {"s1", "s2"}
    assert by_id["s1"].label == "first session prompt"
    assert by_id["s2"].updated > 0
    assert latest_session_id(repo) in {"s1", "s2"}


def test_latest_session_id_skips_empty_resumed_sessions(tmp_path, monkeypatch):
    import os
    import time

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)

    real = project_dir / "real.jsonl"
    real.write_text(json.dumps(_user("u1", "real prompt")) + "\n" + json.dumps(_assistant("m1", "ok")) + "\n")
    # Claude's resume/picker artifact: a session with no real user prompt.
    empty = project_dir / "empty.jsonl"
    empty.write_text(json.dumps(_assistant("m0", "")) + "\n")

    now = time.time()
    os.utime(real, (now - 100, now - 100))
    os.utime(empty, (now, now))  # the EMPTY one is newest by mtime

    # Newest by mtime is empty (nothing to resume) → pick the real conversation.
    assert latest_session_id(repo) == "real"


def test_latest_session_id_falls_back_to_recency_when_all_empty(tmp_path, monkeypatch):
    import os
    import time

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    older = project_dir / "older.jsonl"
    older.write_text(json.dumps(_assistant("m0", "")) + "\n")
    newer = project_dir / "newer.jsonl"
    newer.write_text(json.dumps(_assistant("m1", "")) + "\n")

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    # Nothing has content yet → fall back to plain recency.
    assert latest_session_id(repo) == "newer"


def test_list_worktree_sessions_aggregates_by_recency(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    worktrees_root = tmp_path / "repo" / ".agit" / "worktrees"
    worktrees_root.mkdir(parents=True)

    # Two worktree paths -> two encoded project dirs under Claude's projects root.
    alpha_dir = claude_session._project_dir(worktrees_root / "alpha")
    beta_dir = claude_session._project_dir(worktrees_root / "beta")
    alpha_dir.mkdir(parents=True)
    beta_dir.mkdir(parents=True)
    (alpha_dir / "sess-a.jsonl").write_text(json.dumps(_user("u1", "hello from alpha")) + "\n")
    (beta_dir / "sess-b.jsonl").write_text(json.dumps(_user("u2", "hello from beta")) + "\n")
    os.utime(beta_dir / "sess-b.jsonl", (time.time() + 10, time.time() + 10))  # beta is newer

    result = claude_session.list_worktree_sessions(worktrees_root)
    ids = [ref.id for _, ref in result]
    assert ids == ["sess-b", "sess-a"]  # newest first

    # The returned worktree key recreates the same project dir, so resuming from
    # the recreated worktree path finds the transcript again.
    key_by_id = {ref.id: key for key, ref in result}
    assert claude_session._project_dir(worktrees_root / key_by_id["sess-b"]) == beta_dir
    assert key_by_id["sess-b"] == "beta"


def test_encode_repo_matches_claude_naming():
    # Claude names the project directory by replacing every non-alphanumeric
    # character of the absolute working directory with a dash.
    from pathlib import Path

    assert claude_session._encode_repo(Path("/a.b/c_d")) == "-a-b-c-d"


def test_parse_rows_records_session_updated_from_row_timestamps():
    # Issue #26 cleanup: `updated` was declared but never assigned, so every
    # Claude ExportedSession reported None. It now reflects the newest row.
    rows = [
        _user("u1", "first prompt"),
        _assistant("m1", "answer", usage={"input_tokens": 1, "output_tokens": 1}),
    ]
    rows[0]["timestamp"] = "2026-06-10T10:00:00.000Z"
    rows[1]["timestamp"] = "2026-06-10T10:05:30.000Z"

    session = parse_rows("sess-1", rows)

    from datetime import datetime, timezone

    expected = int(datetime(2026, 6, 10, 10, 5, 30, tzinfo=timezone.utc).timestamp())
    assert session.updated == expected


def test_parse_rows_updated_none_without_timestamps():
    rows = [_user("u1", "p"), _assistant("m1", "a", usage={})]
    assert parse_rows("sess-1", rows).updated is None


# --- Esc interrupts: the turn completes and the marker is not a prompt ----------


def test_parse_rows_interrupt_marker_completes_turn_and_is_not_a_prompt():
    # Esc mid-tool-use leaves stop_reason=tool_use followed by a user row
    # "[Request interrupted by user]". The turn will never get more messages,
    # so it must parse as complete (or the commit gate defers forever), be
    # flagged interrupted (queued prompts were discarded), and the marker must
    # not become a turn of its own or pollute the subject/trace.
    rows = [
        _user("u1", "fix the parser"),
        _assistant("m1", "Let me look at the file.", usage={"input_tokens": 5, "output_tokens": 5}, stop_reason="tool_use"),
        _user("int-1", "[Request interrupted by user]"),
    ]

    session = parse_rows("sess-1", rows)

    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_prompt == "fix the parser"
    assert turn.complete is True
    assert turn.interrupted is True


def test_parse_rows_tool_use_interrupt_variant_is_recognized():
    rows = [
        _user("u1", "do it"),
        _assistant("m1", "working", usage={}, stop_reason="tool_use"),
        _user("int-1", "[Request interrupted by user for tool use]"),
        _user("u2", "actually do something else"),
        _assistant("m2", "done", usage={}, stop_reason="end_turn"),
    ]

    session = parse_rows("sess-1", rows)

    assert [turn.user_prompt for turn in session.turns] == ["do it", "actually do something else"]
    assert session.turns[0].interrupted is True
    assert session.turns[0].complete is True
    assert session.turns[1].interrupted is False


def test_parse_rows_superseded_tool_use_turn_is_complete():
    # A turn flushed because a NEW prompt began can never receive more
    # messages — only the transcript's last (dangling) turn may be in-flight.
    rows = [
        _user("u1", "first"),
        _assistant("m1", "starting first", usage={}, stop_reason="tool_use"),
        _user("u2", "second"),
        _assistant("m2", "answering second", usage={}, stop_reason="tool_use"),
    ]

    session = parse_rows("sess-1", rows)

    assert session.turns[0].complete is True   # superseded: finished for good
    assert session.turns[1].complete is False  # dangling tool_use: still mid-flight
