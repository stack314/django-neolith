"""The parity call over a live server on ``[::1]``: an origin whose host is
an IPv6 address, written in brackets, reached over IPv6 (ADR 0014 Decision
3; ADR 0028).

The server is the conformance suite's, on another address; the call and the
comparison are the contract part's (``tests/conformance``).
"""

from collections.abc import Iterator

import pytest
from django.conf import settings
from django.test import override_settings

import neolith
from tests.conformance import Live, serving
from tests.conformance.contract.conftest import OverBoth
from tests.declaration_contracts import Level, Sku


@pytest.fixture
def live() -> Iterator[Live]:
    with (
        override_settings(ALLOWED_HOSTS=[*settings.ALLOWED_HOSTS, "[::1]"]),
        serving("::1", ipv6=True) as live,
    ):
        yield live


@pytest.mark.spec("HTTP-1", "TRN-1")
def test_a_reply_is_the_same_over_ipv6(live: Live) -> None:
    assert live.origin.startswith("http://[::1]:")

    def call() -> object:
        with neolith.command_scope("web"):
            return neolith.connect("stock.v1").level(Sku(sku="abcde"))

    assert OverBoth(live)(call, identity="stock.v1").reply == Level(on_hand=5)
