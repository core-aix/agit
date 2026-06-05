from agit.backends.base import TokenUsage
from agit.state import AgitState


def test_state_is_repository_local(tmp_path):
    state = AgitState(tmp_path)
    state.add_declined(["new.py"])

    loaded = AgitState(tmp_path)
    assert loaded.path == tmp_path / ".agit" / "state.json"
    assert loaded.declined_untracked() == ["new.py"]


def test_state_prunes_declined_untracked_files(tmp_path):
    state = AgitState(tmp_path)
    state.add_declined(["ignored.log", "keep.py", "removed.py"])

    state.keep_declined(["keep.py"])

    assert state.declined_untracked() == ["keep.py"]


def test_session_name_roundtrip(tmp_path):
    state = AgitState(tmp_path)
    assert state.session_name_for("sess-1") is None
    state.name_session("sess-1", "my-feature")

    assert AgitState(tmp_path).session_name_for("sess-1") == "my-feature"
    # A None/empty id is a no-op, and clearing removes the name.
    state.name_session(None, "ignored")
    state.name_session("sess-1", None)
    assert AgitState(tmp_path).session_name_for("sess-1") is None


def test_trace_roundtrip(tmp_path):
    state = AgitState(tmp_path)
    state.append_trace("user", "hello")
    state.append_trace("agent", "hi")

    assert state.pending_trace() == [
        {"role": "user", "content": "hello"},
        {"role": "agent", "content": "hi"},
    ]
    state.clear_trace()
    assert state.pending_trace() == []


def test_state_adds_repo_local_git_exclude(tmp_path):
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    exclude = git_info / "exclude"
    exclude.write_text("", encoding="utf-8")

    AgitState(tmp_path).save()

    assert ".agit/" in exclude.read_text(encoding="utf-8").splitlines()


def test_state_accumulates_pending_token_usage(tmp_path):
    state = AgitState(tmp_path)
    state.add_token_usage(TokenUsage(context=100, total=25, input=20, output=5, cache_read=3))
    state.add_token_usage(
        TokenUsage(context=120, total=12, input=10, output=2, reasoning=1, subagent_input=8, subagent_output=15)
    )

    assert state.pending_token_usage() == {
        "context": 120,
        "total": 37,
        "input": 30,
        "output": 7,
        "reasoning": 1,
        "cache_read": 3,
        "cache_write": 0,
        "subagent_input": 8,
        "subagent_output": 15,
        "subagent_reasoning": 0,
        "subagent_cache_read": 0,
        "subagent_cache_write": 0,
    }
    state.clear_trace()
    assert state.pending_token_usage()["total"] == 0


def test_backend_session_is_repo_scoped(tmp_path):
    state = AgitState(tmp_path)
    state.backend_session_id = "ses-1"

    assert state.backend_session_matches_repo() is True

    state.data["backend_session_repo"] = str(tmp_path / "other")
    assert state.backend_session_matches_repo() is False


def test_trace_turn_limit_defaults_and_reads_config(tmp_path):
    state = AgitState(tmp_path)
    assert state.trace_turn_limit == 5

    config = tmp_path / ".agit" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"trace_turn_limit": 3}\n', encoding="utf-8")

    assert AgitState(tmp_path).trace_turn_limit == 3
