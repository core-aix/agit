"""Public surface of the ``agit.proxy`` package.

The proxy subsystem is spread across several modules (runner, renderer,
terminal, session, integration, commit_engine, process); this package
re-exports the names that callers need so that ``from agit.proxy import Foo``
always works regardless of which module ``Foo`` lives in.

Internal helpers prefixed with ``_`` are re-exported for test compatibility
but are not part of the stable API.
"""

from agit.proxy.commit_engine import CommitEngine
from agit.proxy.integration import IntegrationService, MergeContext, MergePhase
from agit.proxy.process import BackendProcess
from agit.proxy.renderer import ScreenRenderer, _BackgroundColorEraseScreen, detect_color_mode
from agit.proxy.session import Session
from agit.proxy.terminal import TerminalHost
from agit.proxy.runner import (
    ProxyInput,
    ProxyRunner,
    _escape_sequence_complete,
    _short_session,
)

__all__ = [
    # Public production API
    "CommitEngine",
    "BackendProcess",
    "IntegrationService",
    "MergeContext",
    "MergePhase",
    "ProxyInput",
    "ProxyRunner",
    "ScreenRenderer",
    "Session",
    "TerminalHost",
    "detect_color_mode",
    # Internal helpers (re-exported for test access)
    "_BackgroundColorEraseScreen",
    "_escape_sequence_complete",
    "_short_session",
]
