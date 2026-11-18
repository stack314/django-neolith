"""The conformance suite's live server, once for the package, and a bound on
every test.

The server starts with the package's first test that asks for it and stops
at the package's end, so its threads are gone before later modules run,
the HTTP client's fork tests among them.
"""

import faulthandler
from collections.abc import Iterator
from typing import Final

import pytest

from tests.conformance import Live, serving

# Far longer than any test of the package takes on the slowest machine.
_HANG: Final = 120


@pytest.fixture(scope="package")
def live() -> Iterator[Live]:
    """neolith's own development server on ``127.0.0.1``, serving the test
    host behind the PEP 3333 validator.
    """
    with serving("127.0.0.1", ipv6=False) as live:
        yield live


@pytest.fixture(autouse=True)
def _never_hang() -> Iterator[None]:
    """End the run with every thread's stack, rather than hang, when a test
    of the package hangs.
    """
    faulthandler.dump_traceback_later(_HANG, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
