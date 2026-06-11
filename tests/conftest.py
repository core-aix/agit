"""Shared test fixtures for the agit test suite (#29, P7).

The ``make_runner`` factory lives in ``tests/proxy_helpers.py`` (importable
as ``proxy_helpers`` on the pytest sys.path); this file also re-exports it
for convenience.
"""
from __future__ import annotations

import pytest

from proxy_helpers import make_runner as _make_runner  # noqa: F401 – re-exported below


@pytest.fixture
def runner_factory():
    """Pytest fixture providing the make_runner factory."""
    return _make_runner


# Re-export so callers can do: from conftest import make_runner
make_runner = _make_runner
