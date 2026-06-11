from agit.backends.base import TokenUsage
from agit.config import AgitState


def test_state_is_repository_local(tmp_path):
    state = AgitState(tmp_path)
    state.add_declined(["new.py"])

    loaded = AgitState(tmp_path)
    assert loaded.path == tmp_path / ".agit" / "state.json"
    assert loaded.declined_untracked() == ["new.py"]


def test_backend_defaults_to_configured_default_not_opencode(tmp_path):
    # A fresh repo with no recorded backend must honour the configured default
    # rather than silently falling back to a hardcoded backend.
    state = AgitState(tmp_path, default_backend="claude")
    assert state.backend == "claude"

    # An explicitly null/empty stored backend also falls back to the default.
    state.data["backend"] = None
    assert state.backend == "claude"

    # A stale/unknown stored backend is coerced to the default rather than passed
    # through (which would later raise in make_proxy_agent).
    state.data["backend"] = "retired-agent"
    assert state.backend == "claude"

    # A known stored backend is honoured as-is.
    state.data["backend"] = "opencode"
    assert state.backend == "opencode"


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


# --- issue #17: corrupt state must not brick startup; writes are atomic --------


def test_corrupt_state_json_falls_back_to_defaults_and_keeps_backup(tmp_path):
    state_path = tmp_path / ".agit" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"agit_session_id": "agit-x", "backe', encoding="utf-8")  # truncated mid-write

    state = AgitState(tmp_path)  # must not raise

    assert state.session_id.startswith("agit-")
    # The corrupt file is kept aside for debugging, not silently destroyed.
    backup = state_path.with_name("state.json.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8").endswith('"backe')
    # And the state is usable again: save() writes a fresh valid file.
    state.save()
    assert AgitState(tmp_path).session_id == state.session_id


def test_non_dict_state_json_is_treated_as_corrupt(tmp_path):
    state_path = tmp_path / ".agit" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('["not", "a", "dict"]', encoding="utf-8")

    state = AgitState(tmp_path)  # must not raise

    assert state.session_id.startswith("agit-")
    assert state_path.with_name("state.json.bak").exists()


def test_corrupt_config_json_falls_back_to_defaults(tmp_path):
    config_path = tmp_path / ".agit" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not json", encoding="utf-8")

    state = AgitState(tmp_path)  # must not raise

    assert state.trace_turn_limit == 5  # default config


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    state = AgitState(tmp_path)
    state.save()

    state_dir = tmp_path / ".agit"
    assert (state_dir / "state.json").exists()
    assert list(state_dir.glob("*.tmp")) == []
    # The written file is complete, valid JSON round-tripping the data.
    import json

    with (state_dir / "state.json").open(encoding="utf-8") as handle:
        assert json.load(handle)["agit_session_id"] == state.session_id


def test_missing_info_exclude_is_created_with_agit_ignore(tmp_path):
    # Issue #26: repos created without the default template have no
    # info/exclude; saving state must create it rather than leave .agit/
    # unignored.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    exclude = tmp_path / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()

    state = AgitState(tmp_path)
    state.save()

    assert exclude.exists()
    assert ".agit/" in exclude.read_text(encoding="utf-8").splitlines()


def test_no_exclude_created_outside_a_git_repo(tmp_path):
    state = AgitState(tmp_path)  # tmp_path is not a git repo
    state.save()
    assert not (tmp_path / ".git").exists()
