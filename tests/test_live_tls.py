"""The parity call over a live server wrapped in TLS, with a certificate
from a test authority trustme makes at run time (ADR 0028 Decisions 4 and
6).

The client trusts the authority through the one seam of Decision 6, the
function that builds its TLS context, which is made as in production first.
The server is the conformance suite's, with its listening socket wrapped;
the call and the comparison are the contract part's (``tests/conformance``).
"""

import socket
import ssl
from collections.abc import Iterator

import pytest
import trustme

import neolith
from neolith import _http
from tests.conformance import Live, serving
from tests.conformance.contract.conftest import OverBoth
from tests.declaration_contracts import Level, Sku


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Iterator[Live]:
    authority = trustme.CA()
    made = _http._make_context

    def trusting() -> ssl.SSLContext:
        context = made()
        authority.configure_trust(context)
        return context

    monkeypatch.setattr(_http, "_make_context", trusting)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert("127.0.0.1").configure_cert(server)

    def secure(listening: socket.socket) -> socket.socket:
        return server.wrap_socket(listening, server_side=True)

    with serving("127.0.0.1", ipv6=False, secure=secure) as live:
        yield live


@pytest.mark.spec("HTTP-1", "TRN-1")
def test_a_reply_is_the_same_over_tls(live: Live) -> None:
    assert live.origin.startswith("https://127.0.0.1:")

    def call() -> object:
        with neolith.command_scope("web"):
            return neolith.connect("stock.v1").level(Sku(sku="abcde"))

    assert OverBoth(live)(call, identity="stock.v1").reply == Level(on_hand=5)
