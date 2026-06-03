from agit.backends.base import TokenUsage
from agit.state import AgitState


def test_state_is_repository_local(tmp_path):
    state = AgitState(tmp_path)
    state.add_declined(["new.py"])

    loaded = AgitState(tmp_path)
    assert loaded.path == tmp_path / ".agit" / "state.json"
    assert loaded.declined_untracked() == ["new.py"]


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
    state.add_token_usage(TokenUsage(context=120, total=12, input=10, output=2, reasoning=1))

    assert state.pending_token_usage() == {
        "context": 120,
        "total": 37,
        "input": 30,
        "output": 7,
        "reasoning": 1,
        "cache_read": 3,
        "cache_write": 0,
    }
    state.clear_trace()
    assert state.pending_token_usage()["total"] == 0
