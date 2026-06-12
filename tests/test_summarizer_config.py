from agit.config import AgitState, GlobalConfig


def test_summarization_enabled_default(tmp_path):
    state = AgitState(tmp_path)
    assert state.summarization_enabled is True


def test_summarization_enabled_toggle(tmp_path):
    state = AgitState(tmp_path)
    state.summarization_enabled = False
    assert state.summarization_enabled is False
    state.summarization_enabled = True
    assert state.summarization_enabled is True


def test_summarization_enabled_persists(tmp_path):
    state = AgitState(tmp_path)
    state.summarization_enabled = False
    state.save()

    state2 = AgitState(tmp_path)
    assert state2.summarization_enabled is False


def test_global_summarization_enabled_default(tmp_path):
    config_path = tmp_path / "config.json"
    config = GlobalConfig(config_path)
    assert config.summarization_enabled is True


def test_global_summarization_enabled_toggle(tmp_path):
    config_path = tmp_path / "config.json"
    config = GlobalConfig(config_path)
    config.summarization_enabled = False
    assert config.summarization_enabled is False
    config.summarization_enabled = True
    assert config.summarization_enabled is True


def test_global_summarization_enabled_persists(tmp_path):
    config_path = tmp_path / "config.json"
    config = GlobalConfig(config_path)
    config.summarization_enabled = False
    config.save()

    config2 = GlobalConfig(config_path)
    assert config2.summarization_enabled is False
