import json
from pathlib import Path

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


def test_make_proxy_agent_defaults_to_opencode_for_unknown():
    assert make_proxy_agent("nonsense").name == "opencode"


def test_global_config_default_backend_persists(tmp_path):
    path = tmp_path / "config.json"
    config = GlobalConfig(path)
    assert config.default_backend == "opencode"
    config.default_backend = "claude"
    assert GlobalConfig(path).default_backend == "claude"
    assert json.loads(path.read_text())["default_backend"] == "claude"


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
