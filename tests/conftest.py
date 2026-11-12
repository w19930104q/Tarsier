"""Shared pytest fixtures and markers."""

import pytest

from tarsier.core.ref import SCOPE_MANAGER


@pytest.fixture(autouse=True)
def _isolate_global_scopes():
    """Never let workflow scopes leak between tests."""
    SCOPE_MANAGER._scopes.clear()
    SCOPE_MANAGER._counter.__init__(1)
    yield
    SCOPE_MANAGER._scopes.clear()
