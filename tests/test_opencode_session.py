import json
import subprocess

from agit.opencode_session import latest_session_id, parse_exported_session, session_belongs_to_repo, turns_after


def test_parse_exported_session_turns_model_and_tokens():
    session = parse_exported_session(
        {
            "info": {
                "id": "ses-1",
                "model": {"providerID": "openai", "id": "gpt-5.5"},
                "time": {"updated": 123},
            },
            "messages": [
                {
                    "info": {"role": "user", "id": "u1"},
                    "parts": [{"type": "text", "text": "fix it"}],
                },
                {
                    "info": {
                        "role": "assistant",
                        "id": "a1",
                        "parentID": "u1",
                        "providerID": "openai",
                        "modelID": "gpt-5.5",
                        "tokens": {"total": 99, "input": 90, "output": 9},
                    },
                    "parts": [
                        {
                            "type": "text",
                            "text": "fixed",
                            "metadata": {"openai": {"phase": "final_answer"}},
                        }
                    ],
                },
            ],
        }
    )

    assert session.session_id == "ses-1"
    assert session.model == "openai/gpt-5.5"
    assert session.updated == 123
    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "fix it"
    assert session.turns[0].final_response == "fixed"
    assert session.turns[0].model == "openai/gpt-5.5"
    assert session.turns[0].tokens.total == 9


def test_parse_exported_session_groups_multiple_assistants_until_next_user():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1", "model": {"providerID": "ollama", "id": "qwen"}},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "add timer"}]},
                {
                    "info": {"role": "assistant", "id": "a-tools", "parentID": "u1", "finish": "tool-calls", "tokens": {"total": 5}},
                    "parts": [{"type": "tool", "tool": "edit"}],
                },
                {
                    "info": {"role": "assistant", "id": "a-final", "parentID": "u1", "finish": "stop", "tokens": {"total": 7, "input": 5, "output": 2}},
                    "parts": [{"type": "text", "text": "Added countdown timer."}],
                },
            ],
        }
    )

    assert len(session.turns) == 1
    assert session.turns[0].assistant_message_id == "a-final"
    assert session.turns[0].final_response == "Added countdown timer."
    assert session.turns[0].tokens.total == 2
    assert session.turns[0].tokens.input == 5


def test_parse_exported_session_excludes_compaction_summary():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do the work"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "parentID": "u1", "finish": "stop", "tokens": {"total": 4, "output": 4}},
                    "parts": [{"type": "text", "text": "real response"}],
                },
                {
                    # The compaction summary must not overwrite the turn's response.
                    "info": {"role": "assistant", "id": "sum", "parentID": "u1", "mode": "compaction", "agent": "compaction", "summary": True, "finish": "stop", "tokens": {"total": 0}},
                    "parts": [{"type": "text", "text": "This is a summary of the conversation so far..."}],
                },
            ],
        }
    )

    assert len(session.turns) == 1
    assert session.turns[0].final_response == "real response"
    assert session.turns[0].assistant_message_id == "a1"
    assert session.turns[0].tokens.total == 4


def test_parse_exported_session_counts_reasoning_part_tokens():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "think"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "finish": "stop"},
                    "parts": [
                        {"type": "reasoning", "tokens": {"input": 10, "output": 0, "reasoning": 6, "cache": {"read": 4}}},
                        {"type": "text", "text": "done", "tokens": {"input": 12, "output": 2, "reasoning": 0}},
                    ],
                },
            ],
        }
    )

    assert session.turns[0].tokens.context == 12
    assert session.turns[0].tokens.input == 22
    assert session.turns[0].tokens.total == 8
    assert session.turns[0].tokens.reasoning == 6
    assert session.turns[0].tokens.cache_read == 4


def test_parse_exported_session_extracts_final_text_from_event_blob():
    event_blob = "\n".join(
        [
            '{"type":"step_start","timestamp":1,"sessionID":"ses-1","part":{"type":"step-start"}}',
            '{"type":"text","timestamp":2,"sessionID":"ses-1","part":{"type":"text","text":"Hi.","metadata":{"openai":{"phase":"final_answer"}}}}',
            '{"type":"step_finish","timestamp":3,"sessionID":"ses-1","part":{"type":"step-finish","tokens":{"total":9529,"input":9523,"output":6,"reasoning":0}}}',
        ]
    )
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "hi"}]},
                {"info": {"role": "assistant", "id": "a1", "finish": "stop"}, "parts": [{"type": "text", "text": event_blob}]},
            ],
        }
    )

    assert session.turns[0].final_response == "Hi."


def test_turns_after_last_message_id():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "one"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "parentID": "u1"},
                    "parts": [{"type": "text", "text": "done"}],
                },
                {"info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "two"}]},
                {
                    "info": {"role": "assistant", "id": "a2", "parentID": "u2"},
                    "parts": [{"type": "text", "text": "done"}],
                },
            ],
        }
    )

    assert [turn.assistant_message_id for turn in turns_after(session, "a1")] == ["a2"]


def test_latest_session_id_prefers_most_recent_matching_repo(monkeypatch, tmp_path):
    old = tmp_path / "old"
    old.mkdir()

    sessions = [
        {"id": "other", "directory": str(old), "updated": 300},
        {"id": "older", "directory": str(tmp_path), "updated": 100},
        {"id": "newer", "directory": str(tmp_path), "updated": 200},
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert latest_session_id(tmp_path) == "newer"


def test_session_belongs_to_repo(monkeypatch, tmp_path):
    sessions = [{"id": "ses-1", "directory": str(tmp_path), "updated": 1}]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert session_belongs_to_repo(tmp_path, "ses-1") is True
    assert session_belongs_to_repo(tmp_path, "other") is False


def test_list_worktree_sessions_filters_to_worktrees_newest_first(monkeypatch, tmp_path):
    from agit.opencode_session import list_worktree_sessions

    worktrees_root = tmp_path / ".agit" / "worktrees"
    (worktrees_root / "session-1").mkdir(parents=True)
    (worktrees_root / "session-2").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    sessions = [
        {"id": "a", "directory": str(worktrees_root / "session-1"), "updated": 1000, "title": "first"},
        {"id": "b", "directory": str(worktrees_root / "session-2"), "updated": 3000, "title": "second"},
        {"id": "c", "directory": str(elsewhere), "updated": 9000, "title": "another repo"},
        {"id": "d", "directory": str(worktrees_root / "a" / "deep"), "updated": 5000},  # not an immediate child
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = list_worktree_sessions(worktrees_root)
    # Only the immediate worktree children of this repo, paired with the worktree
    # name, newest first.
    assert [(key, ref.id) for key, ref in result] == [("session-2", "b"), ("session-1", "a")]
    assert result[0][1].label == "second"
