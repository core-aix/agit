"""Compatibility shim for the proxy package (#29, P0).

The implementation lives in ``agit.proxy.runner`` while the decomposition is in
progress; extracted modules land as siblings and their public names are
re-exported here so external callers keep importing from ``agit.proxy``. The
shim is removed in the final phase (P7).
"""

from agit.proxy.process import BackendProcess
from agit.proxy.renderer import ScreenRenderer
from agit.proxy.terminal import TerminalHost
from agit.proxy.runner import (
    ProxyInput,
    ProxyRunner,
    _BackgroundColorEraseScreen,
    _escape_sequence_complete,
    _short_session,
    detect_color_mode,
)

__all__ = [
    "BackendProcess",
    "ProxyInput",
    "ProxyRunner",
    "ScreenRenderer",
    "TerminalHost",
    "_BackgroundColorEraseScreen",
    "_escape_sequence_complete",
    "_short_session",
    "detect_color_mode",
]
