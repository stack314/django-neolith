"""Suite-wide guards."""

import faulthandler
import gc
from collections.abc import Iterator

import pytest

from neolith import _context, testing


@pytest.fixture(autouse=True, scope="session")
def _booted() -> None:
    """The test host boots once, without reading a schema: the test runner
    builds each test database itself (ADR 0022 Decision 7).
    """
    testing.boot()


@pytest.fixture(autouse=True)
def _no_scope_left_open() -> Iterator[None]:
    """Every scope a test opens is closed when it ends.

    The table of open scopes is the process's, so a scope one test leaves
    open changes what a later test finds there.
    """
    yield
    left = _context.open_scopes()
    for scope in left:
        _context.close(scope)
    assert not left, f"{len(left)} scope(s) left open by this test"


# The modules whose code a collection can reach: the request root's wrappers
# close from finalizers and from generators the collector finalizes.
_COLLECTED = {"tests.test_request_root", "tests.test_request_admin"}
# Far longer than any of their tests takes on the slowest machine.
_HANG = 120


@pytest.fixture(autouse=True)
def _collect_often_and_never_hang(request: pytest.FixtureRequest) -> Iterator[None]:
    """In those modules, collect at nearly every allocation, so a path that
    takes a lock from a collection is met at once, and end the run with
    every thread's stack when a test hangs, rather than hang the suite.
    """
    if request.module.__name__ not in _COLLECTED:
        yield
        return
    thresholds = gc.get_threshold()
    gc.set_threshold(1, 1, 1)
    faulthandler.dump_traceback_later(_HANG, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        gc.set_threshold(*thresholds)
