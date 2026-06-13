import json
from pathlib import Path

import pytest

from agit.backends.claude import ClaudeBackend
from agit.backends.proxy_agents import available_backends, make_proxy_agent
from agit.config import GlobalConfig
from agit.config import AgitState


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
    from agit.config import DEFAULT_TIMINGS

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
        json.dumps(
            {
                "timings": {
                    "base_poll_seconds": -5,
                    "child_idle_seconds": "soon",
                    "file_stable_seconds": True,
                    "parse_cooldown_seconds": 0,
                }
            }
        )
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
            "usage": {
                "input_tokens": 12,
                "output_tokens": 34,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 5,
            },
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


def test_claude_backend_picks_main_model_from_multi_model_usage():
    # Issue #24: modelUsage can contain several models for one invocation (e.g.
    # a Haiku sub-agent alongside the main model). The recorded model — which
    # also becomes --model on later runs — must be the main conversation model
    # (most output), not whichever dict key happens to come first.
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 80},
                "claude-opus-4-8": {"inputTokens": 5000, "outputTokens": 2200},
            },
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"

    # And it is not just "the larger usage dict last": reverse the ordering.
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "modelUsage": {
                "claude-opus-4-8": {"inputTokens": 5000, "outputTokens": 2200},
                "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 80},
            },
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"


def test_claude_backend_prefers_explicit_top_level_model():
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "model": "claude-opus-4-8",
            "modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 999}},
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"


def test_claude_backend_model_falls_back_to_total_volume():
    # No output recorded (e.g. cached/aborted turn): fall back to overall
    # token volume rather than dict order.
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"inputTokens": 10},
                "claude-opus-4-8": {"inputTokens": 800},
            },
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"


def test_menu_key_defaults_and_validation(tmp_path):
    from agit.config import GlobalConfig

    config = GlobalConfig(tmp_path / "config.json")
    assert config.menu_key == "ctrl-g"
    assert config.menu_key_byte == b"\x07"
    assert config.menu_key_label == "Ctrl-G"
    assert config.is_shift_modified is False

    # A configured key is normalized and converted to its control byte.
    config.data["menu_key"] = "Ctrl+P"
    assert config.menu_key == "ctrl-p"
    assert config.menu_key_byte == b"\x10"
    assert config.menu_key_label == "Ctrl-P"
    assert config.is_shift_modified is False

    # Conflicting or invalid values fall back to the default, so a config
    # typo can never lock the user out of the menu.
    for bad in ("ctrl-c", "ctrl-m", "ctrl-i", "ctrl-j", "ctrl-h", "shift-g", "g", 7, None):
        config.data["menu_key"] = bad
        assert config.menu_key == "ctrl-g"


def test_menu_key_shift_modified(tmp_path):
    from agit.config import GlobalConfig

    config = GlobalConfig(tmp_path / "config.json")

    # Test ctrl+shift+g format
    config.data["menu_key"] = "ctrl+shift+g"
    assert config.menu_key == "ctrl+shift+g"
    assert config.is_shift_modified is True
    assert config.menu_key_byte == b""  # Empty for shift-modified keys
    assert config.menu_key_sequence == b"\x1b[103;6u"  # CSI 103 ; 6 u (g=103, modifiers=6)
    assert config.menu_key_label == "Ctrl+Shift-G"

    # Test normalization
    config.data["menu_key"] = "Ctrl+Shift+P"
    assert config.menu_key == "ctrl+shift+p"
    assert config.is_shift_modified is True
    assert config.menu_key_sequence == b"\x1b[112;6u"  # p=112
    assert config.menu_key_label == "Ctrl+Shift-P"

    # Test that plain ctrl-<letter> still works
    config.data["menu_key"] = "ctrl-y"
    assert config.menu_key == "ctrl-y"
    assert config.is_shift_modified is False
    assert config.menu_key_byte == b"\x19"
    assert config.menu_key_sequence == b"\x19"  # Same as byte for plain keys
    assert config.menu_key_label == "Ctrl-Y"


# --- use_worktrees config (#9) ----------------------------------------------


def test_use_worktrees_defaults_true(tmp_path, monkeypatch):
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path))
    assert GlobalConfig().use_worktrees is True


def test_use_worktrees_config_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text('{"use_worktrees": false}')
    assert GlobalConfig().use_worktrees is False
