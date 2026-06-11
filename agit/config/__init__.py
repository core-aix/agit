"""Persistence and settings: repository-local state (``.agit/state.json``) and
user-wide settings (``~/.agit/config.json``)."""

from agit.config.settings import DEFAULT_TIMINGS, GlobalConfig
from agit.config.state import AgitState

__all__ = ["AgitState", "GlobalConfig", "DEFAULT_TIMINGS"]
