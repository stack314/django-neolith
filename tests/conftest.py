"""Suite-wide guards."""

from collections.abc import Iterator

import pytest

from neolith import _context


@pytest.fixture(autouse=True)
def _no_scope_left_open() -> Iterator[None]:
    """Every scope a test opens is closed when it ends.

    The table of open scopes is the process's, so a scope one test leaves
    open changes what a later test finds there.
    """
    yield
    left = list(_context._OPEN.values())
    for scope in left:
        _context.close(scope)
    assert not left, f"{len(left)} scope(s) left open by this test"
