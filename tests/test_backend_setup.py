import pytest

import agit.backends.setup as bs
from agit.backends.setup import BackendUnavailable, ensure_installed_backend, select_default_backend
from agit.backends.proxy_agents import available_backends
from agit.config import GlobalConfig


class FakeConfig:
    def __init__(self):
        self._backend = None
        self.saved = []

    @property
    def default_backend(self):
        return self._backend

    @default_backend.setter
    def default_backend(self, value):
        self._backend = value
        self.saved.append(value)


def _inputs(*values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


def test_available_backends_is_alphabetical():
    assert available_backends() == ["claude", "opencode"]


def test_backend_installed_uses_path_lookup(monkeypatch):
    monkeypatch.setattr(bs.shutil, "which", lambda exe: "/usr/bin/" + exe if exe == "claude" else None)
    assert bs.backend_installed("claude") is True
    assert bs.backend_installed("opencode") is False


def test_select_default_backend_picks_installed_choice(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    config = FakeConfig()
    # Menu is alphabetical, so option 2 is opencode.
    chosen = select_default_backend(config, input_fn=_inputs("2"), output_fn=lambda _s: None)
    assert chosen == "opencode"
    assert config.saved == ["opencode"]


def test_select_default_backend_defaults_to_first_on_empty_input(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    config = FakeConfig()
    chosen = select_default_backend(config, input_fn=_inputs(""), output_fn=lambda _s: None)
    assert chosen == "claude"


def test_select_default_backend_offers_install_or_choose_another(monkeypatch):
    # opencode is missing; user picks it, backs out, then picks the installed claude.
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "claude")
    config = FakeConfig()
    chosen = select_default_backend(config, input_fn=_inputs("2", "b", "1"), output_fn=lambda _s: None)
    assert chosen == "claude"


def test_ensure_installed_backend_returns_installed_backend(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    assert ensure_installed_backend("opencode", FakeConfig(), interactive=True) == "opencode"


def test_ensure_installed_backend_switches_to_installed_alternative(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "claude")
    config = FakeConfig()
    resolved = ensure_installed_backend(
        "opencode", config, interactive=True, input_fn=_inputs("claude"), output_fn=lambda _s: None
    )
    assert resolved == "claude"
    assert config.saved == ["claude"]


def test_ensure_installed_backend_non_interactive_raises(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: False)
    with pytest.raises(BackendUnavailable):
        ensure_installed_backend("opencode", FakeConfig(), interactive=False)


def test_ensure_installed_backend_quit_raises(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: False)
    with pytest.raises(BackendUnavailable):
        ensure_installed_backend(
            "opencode", FakeConfig(), interactive=True, input_fn=_inputs("q"), output_fn=lambda _s: None
        )


def test_global_config_has_default_backend(tmp_path):
    config = GlobalConfig(tmp_path / "config.json")
    assert config.has_default_backend() is False
    config.default_backend = "claude"
    assert GlobalConfig(tmp_path / "config.json").has_default_backend() is True
