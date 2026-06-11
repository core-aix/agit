"""Test helper utilities for ProxyRunner tests (#29, P7).

This module provides ``make_runner`` — the canonical factory for constructing
:class:`~agit.proxy.runner.ProxyRunner` instances in unit tests, replacing
the ``ProxyRunner.__new__(ProxyRunner)`` idiom from earlier test files.

Usage::

    from tests.proxy_helpers import make_runner

    def test_something(tmp_path):
        runner = make_runner(state=AgitState(tmp_path), repo=FakeRepo())
        runner.agent_in_flight = True
        assert runner.active.agent_in_flight is True
"""
from __future__ import annotations

from agit.proxy.runner import ProxyRunner


def make_runner(**overrides) -> ProxyRunner:
    """Build a :class:`ProxyRunner` for tests without production dependencies.

    Delegates to :meth:`ProxyRunner.for_testing`; all keyword arguments are
    forwarded as overrides (session-level fields such as ``repo``, ``state``,
    ``backend``, ``master_fd`` are routed to the session; runner-level fields
    such as ``verbose``, ``cols``, ``color_mode``, ``_base_branch`` are set
    directly on the runner).

    Returns a fully-initialized runner whose ``active`` session carries real
    :class:`~agit.proxy.session.Session` state.  No filesystem access,
    no TTY, no child process is involved.
    """
    return ProxyRunner.for_testing(**overrides)
