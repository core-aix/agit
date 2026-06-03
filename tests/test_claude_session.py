import json

from agit import claude_session
from agit.claude_session import export_session, latest_session_id, parse_rows, session_belongs_to_repo


def _user(uuid, text, **extra):
    row = {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def _assistant(msg_id, text, *, model="claude-opus-4-8", usage=None, content=None):
    blocks = content if content is not None else [{"type": "text", "text": text}]
    return {
        "type": "assistant",
        "message": {"id": msg_id, "role": "assistant", "model": model, "content": blocks, "usage": usage or {}},
    }


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


def test_encode_repo_matches_claude_naming():
    # Claude names the project directory by replacing every non-alphanumeric
    # character of the absolute working directory with a dash.
    from pathlib import Path

    assert claude_session._encode_repo(Path("/a.b/c_d")) == "-a-b-c-d"
