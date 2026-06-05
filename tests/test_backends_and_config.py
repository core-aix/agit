import json
from pathlib import Path

import pytest

from agit.backends.claude import ClaudeBackend
from agit.backends.proxy_agents import available_backends, make_proxy_agent
from agit.global_config import GlobalConfig
from agit.state import AgitState


def test_available_backends_includes_opencode_and_claude():
    assert set(available_backends()) == {"opencode", "claude"}


def test_opencode_proxy_agent_spawn_command():
    agent = make_proxy_agent("opencode")
    assert agent.name == "opencode"
    assert agent.new_session_id() is None
    assert agent.spawn_command(Path("/repo"), session_id=None, resume=False) == ["opencode", "/repo"]
    assert agent.spawn_command(Path("/repo"), session_id="s1", resume=True) == ["opencode", "--session", "s1", "/repo"]


def test_claude_proxy_agent_spawn_command_uses_session_id_and_resume():
    agent = make_proxy_agent("claude")
    assert agent.name == "claude"
    # Claude picks an explicit session id so aGiT knows which transcript to read.
    assert len(agent.new_session_id()) == 36
    assert agent.spawn_command(Path("/repo"), session_id="u1", resume=False) == ["claude", "--session-id", "u1"]
    assert agent.spawn_command(Path("/repo"), session_id="u1", resume=True) == ["claude", "--resume", "u1"]


def test_make_proxy_agent_raises_on_unknown_backend():
    # An unknown/stale backend name must surface loudly, not silently launch
    # OpenCode (which contradicts the configured default).
    with pytest.raises(ValueError) as excinfo:
        make_proxy_agent("nonsense")
    assert "nonsense" in str(excinfo.value)


def test_global_config_default_backend_persists(tmp_path):
    path = tmp_path / "config.json"
    config = GlobalConfig(path)
    assert config.default_backend == "opencode"
    config.default_backend = "claude"
    assert GlobalConfig(path).default_backend == "claude"
    assert json.loads(path.read_text())["default_backend"] == "claude"


def test_timings_default_when_unset(tmp_path):
    from agit.global_config import DEFAULT_TIMINGS

    config = GlobalConfig(tmp_path / "config.json")
    assert config.timings == DEFAULT_TIMINGS
    # A fresh copy, not the module-level dict (so callers can't mutate the defaults).
    assert config.timings is not DEFAULT_TIMINGS


def test_timings_override_subset_from_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timings": {"base_poll_seconds": 30}}))
    timings = GlobalConfig(path).timings
    assert timings["base_poll_seconds"] == 30.0  # overridden, coerced to float
    assert timings["background_poll_seconds"] == 2.0  # untouched key keeps its default


def test_timings_ignore_invalid_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"timings": {"base_poll_seconds": -5, "child_idle_seconds": "soon",
                                "file_stable_seconds": True, "parse_cooldown_seconds": 0}})
    )
    timings = GlobalConfig(path).timings
    # Non-positive, wrong-type, and bool values all fall back to the defaults.
    assert timings["base_poll_seconds"] == 3.0
    assert timings["child_idle_seconds"] == 4.0
    assert timings["file_stable_seconds"] == 8.0
    assert timings["parse_cooldown_seconds"] == 10.0


def test_state_uses_default_backend_and_remembers_sessions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = AgitState(repo, default_backend="claude")
    assert state.backend == "claude"

    state.backend_session_id = "claude-session"
    state.remember_backend_session()
    state.backend = "opencode"
    state.backend_session_id = "opencode-session"
    state.remember_backend_session()

    assert state.stored_backend_session("claude") == "claude-session"
    assert state.stored_backend_session("opencode") == "opencode-session"
    # Survives a reload from disk.
    reloaded = AgitState(repo, default_backend="claude")
    assert reloaded.stored_backend_session("claude") == "claude-session"


def test_claude_backend_parses_json_result():
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "the answer",
            "session_id": "sess-xyz",
            "modelUsage": {"claude-opus-4-8": {"inputTokens": 1}},
            "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5},
        }
    )
    response, session_id, model, tokens = backend._parse_output(output)
    assert response == "the answer"
    assert session_id == "sess-xyz"
    assert model == "claude-opus-4-8"
    assert tokens.output == 34
    assert tokens.context == 12 + 100 + 5
    assert tokens.cache_read == 100


def test_claude_backend_tolerates_leading_logs():
    backend = ClaudeBackend(Path("."))
    output = "starting up\n" + json.dumps({"type": "result", "result": "hi", "session_id": "s"})
    response, session_id, _model, _tokens = backend._parse_output(output)
    assert response == "hi"
    assert session_id == "s"
