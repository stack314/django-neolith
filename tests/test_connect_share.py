"""An attempt's share of the connect phase, as the HTTP client applies it
(spec 02, TRN-73; ADR 0031 Decision 5, narrowing ADR 0028 Decision 2).

A connector routing a call among providers passes the HTTP transport a time
by which a connection must be ready. The client ends the connect phase at the
earliest of that time, the connect cap and the deadline: with ``unavailable``
naming the bound, unless the deadline ended it. A provider is played on the
far end of a socket pair (``tests.http_peer``), and name resolution by a
stand-in that answers only when the test ends (ADR 0028 Decision 9); nothing
reaches the network.
"""

import logging
import re
import secrets
import socket
import threading
from collections.abc import Iterator, Sequence
from typing import Any, Protocol

import pytest
from pydantic import BaseModel

from neolith import (
    ContractError,
    Principal,
    _caller,
    _connector,
    _context,
    _http,
    _token,
    action,
    contract,
)
from neolith._connector import _LATE, _NOT_SENT, Connector
from neolith._http import ClientError, Origin, Pool, Response, exchange
from neolith._http_transport import HTTPTransport
from tests.http_peer import (
    BODY,
    FIELDS,
    OK,
    TARGET,
    Provider,
    Script,
    answer,
    read_request,
)


class Ask(BaseModel):
    n: int = 0


class Answer(BaseModel):
    n: int


@contract("share.v1")
class Desk(Protocol):
    @action(idempotent=True)
    def get(self, request: Ask) -> Answer: ...

    @action
    def put(self, request: Ask) -> Answer: ...


ALICE = Principal("https://id.example.com", "alice", {"roles": ("clerk",)})
# Generated per run: no secret is stored in the repository (K9).
_CALLERS = _caller.Callers(
    signing=(_token.secret(secrets.token_urlsafe(32), "shop", "shop-1"),)
)


class Clock:
    """This host's clock, held still by the test."""

    def __init__(self) -> None:
        self.at = 1_000_000.0

    def __call__(self) -> float:
        return self.at


class Timed(Provider):
    """A provider that also records the time each connection was due by."""

    def __init__(self, scripts: Sequence[Script]) -> None:
        super().__init__(scripts)
        self.untils: list[float] = []

    def open(self, until: float) -> socket.socket:
        self.untils.append(until)
        return super().open(until)


@pytest.fixture
def held_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """A resolver that answers only when the test ends."""
    release = threading.Event()
    asked: list[str] = []

    def resolve(host: str, *args: object) -> list[object]:
        asked.append(host)
        release.wait(10)
        raise socket.gaierror("released")

    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    yield asked
    release.set()


def fails(pool: Pool, deadline: float | None, connect_by: float | None) -> ClientError:
    with pytest.raises(ClientError) as raised:
        exchange(pool, TARGET, lambda: FIELDS, BODY, deadline, connect_by)
    return raised.value


# The client's bound


@pytest.mark.spec("TRN-73")
def test_connect_by_ends_the_phase_with_unavailable(held_resolver: list[str]) -> None:
    pool = Pool(Origin.parse("http://a.test"))
    started = _context.now()
    error = fails(pool, started + 5, started + 0.2)
    assert (error.kind, error.phase) == ("unavailable", "before writing")
    assert error.message == (
        "the provider at http://a.test could not be reached: no connection "
        "within 0.2 seconds"
    )
    assert _context.now() - started < 1
    assert held_resolver == ["a.test"]


@pytest.mark.spec("TRN-73", "TRN-64")
@pytest.mark.parametrize("by", [0.2, 0.5], ids=["equal", "after"])
def test_the_deadline_at_or_before_connect_by_ends_the_phase_with_timeout(
    held_resolver: list[str], by: float
) -> None:
    pool = Pool(Origin.parse("http://a.test"))
    started = _context.now()
    error = fails(pool, started + 0.2, started + by)
    assert (error.kind, error.message) == ("timeout", _NOT_SENT)


@pytest.mark.spec("TRN-73")
def test_the_cap_before_connect_by_ends_the_phase_with_its_own_bound(
    held_resolver: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http, "CONNECT_CAP", 0.2)
    pool = Pool(Origin.parse("http://a.test"))
    started = _context.now()
    error = fails(pool, None, started + 5)
    assert (error.kind, error.message) == (
        "unavailable",
        "the provider at http://a.test could not be reached: no connection "
        "within 0.2 seconds",
    )


@pytest.mark.spec("TRN-73")
def test_connect_by_already_passed_opens_nothing() -> None:
    provider = Timed([answer(OK)])
    pool = Pool(Origin.parse("http://a.test"), opener=provider.open)
    started = _context.now()
    error = fails(pool, started + 30, started - 1)
    assert error.kind == "unavailable"
    assert provider.opened == 0
    pool.close()
    provider.finish()


@pytest.mark.spec("TRN-73", "TRN-65")
def test_the_replacement_connection_is_held_to_the_same_time() -> None:
    ends: list[socket.socket] = []

    def first(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(OK)
        ends.append(sock)
        provider.release.wait(10)

    provider = Timed([first, answer(OK)])
    pool = Pool(Origin.parse("http://a.test"), opener=provider.open)
    try:
        assert exchange(pool, TARGET, lambda: FIELDS, BODY, None) == Response(
            200, b"{}"
        )
        readings: list[int] = []

        def fields() -> Sequence[tuple[str, str]]:
            readings.append(provider.opened)
            if len(readings) == 1:
                ends[0].shutdown(socket.SHUT_RDWR)
                ends[0].close()
            return FIELDS

        started = _context.now()
        by = started + 3
        response = exchange(pool, TARGET, fields, BODY, started + 30, by)
        assert response == Response(200, b"{}")
        assert readings == [1, 2]
        assert provider.untils[1] == by
    finally:
        pool.close()
        provider.finish()


@pytest.mark.spec("TRN-17")
def test_without_connect_by_the_cap_bounds_as_before() -> None:
    provider = Timed([answer(OK)])
    pool = Pool(Origin.parse("http://a.test"), opener=provider.open)
    try:
        started = _context.now()
        exchange(pool, TARGET, lambda: FIELDS, BODY, started + 30)
        assert started + _http.CONNECT_CAP <= provider.untils[0]
        assert provider.untils[0] <= _context.now() + _http.CONNECT_CAP
    finally:
        pool.close()
        provider.finish()


# Through the connector and the HTTP transport


def _transport(pool: Pool) -> HTTPTransport:
    return HTTPTransport(Desk, pool, _CALLERS)


def _fields(request: bytes) -> dict[str, str]:
    head = request.partition(b"\r\n\r\n")[0].decode("ascii")
    return dict(line.split(": ", 1) for line in head.split("\r\n")[1:])


class Recorded:
    """A provider that records each request and answers it in turn."""

    def __init__(self, *responses: bytes) -> None:
        self.requests: list[bytes] = []
        self._responses = responses

    def script(self, sock: socket.socket) -> None:
        for response in self._responses:
            request = read_request(sock)
            if not request:
                return
            self.requests.append(request)
            sock.sendall(response)


_UNAVAILABLE_503 = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
_REPLY = b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n{"n":7}'


@pytest.mark.spec("TRN-71", "TRN-73", "TRN-8")
def test_a_moved_call_over_http_keeps_its_context_and_gets_its_share(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    monkeypatch.setattr(_context, "now", clock)
    monkeypatch.setattr(_http, "_now", clock)
    first, second = Recorded(_UNAVAILABLE_503), Recorded(_REPLY)
    a, b = Timed([first.script]), Timed([second.script])
    pools = [
        Pool(Origin.parse("http://a.test"), opener=a.open),
        Pool(Origin.parse("http://b.test:8080"), opener=b.open),
    ]
    try:
        connector: Any = Connector(Desk, *map(_transport, pools))
        fields = _context.root_fields("shop", requires=("share.v1",), principal=ALICE)
        fields.update(tracestate="vendor=value", idempotency_key="parent-key")
        with (
            caplog.at_level(logging.INFO, logger="neolith"),
            _context.opened(**fields),
        ):
            assert connector.get(Ask(n=1), timeout=8) == Answer(n=7)
    finally:
        for pool in pools:
            pool.close()
        a.finish()
        b.finish()
    # The first attempt's share: 8 s over two entries. The last has none, so
    # the deadline, before the cap, bounds it.
    assert a.untils == [clock.at + 4]
    assert b.untils == [clock.at + 8]
    (one,), (two,) = first.requests, second.requests
    at_a, at_b = _fields(one), _fields(two)
    for name in ("Neolith-Principal", "Idempotency-Key", "tracestate"):
        assert at_a[name] == at_b[name]
    trace_a, span_a = at_a["traceparent"].split("-")[1:3]
    trace_b, span_b = at_b["traceparent"].split("-")[1:3]
    assert trace_a == trace_b
    assert span_a != span_b
    assert at_a["Neolith-Timeout"] == at_b["Neolith-Timeout"] == "8000"
    assert at_a["Authorization"].startswith("Bearer ")
    assert at_b["Authorization"].startswith("Bearer ")
    assert re.search(r"Host: a\.test\r\n", one.decode())
    assert re.search(r"Host: b\.test:8080\r\n", two.decode())
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "provider 1 of 2 at http://a.test was unavailable" in infos[0]
    assert "trying provider 2 at http://b.test:8080 with 8000 ms left" in infos[0]


@pytest.mark.spec("TRN-73", "TRN-74", "TRN-64")
def test_a_held_resolver_costs_the_share_then_the_deadline(
    held_resolver: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first provider's name never resolves: its attempt ends at its
    share with ``unavailable``, and the call moves; the last entry has no
    share, so the deadline ends its phase, and the call, having moved, ends
    with ``timeout`` saying the action might have run."""
    monkeypatch.setattr(_connector, "SHARE_FLOOR", 0.1)
    pools = [Pool(Origin.parse("http://a.test")), Pool(Origin.parse("http://b.test"))]
    connector: Any = Connector(Desk, *map(_transport, pools))
    started = _context.now()
    try:
        with (
            _context.root("shop", requires=("share.v1",)),
            pytest.raises(ContractError) as raised,
        ):
            connector.get(Ask(n=1), timeout=0.4)
    finally:
        for pool in pools:
            pool.close()
    took = _context.now() - started
    assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)
    assert held_resolver == ["a.test", "b.test"]
    assert 0.4 <= took < 2


@pytest.mark.spec("TRN-73", "CON-4")
def test_a_non_idempotent_call_gets_no_share(
    held_resolver: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_connector, "SHARE_FLOOR", 0.1)
    pools = [Pool(Origin.parse("http://a.test")), Pool(Origin.parse("http://b.test"))]
    connector: Any = Connector(Desk, *map(_transport, pools))
    try:
        with (
            _context.root("shop", requires=("share.v1",)),
            pytest.raises(ContractError) as raised,
        ):
            connector.put(Ask(n=1), timeout=0.3)
    finally:
        for pool in pools:
            pool.close()
    # Held to its deadline, not to a share, and never moved: not sent.
    assert (raised.value.kind, raised.value.message) == ("timeout", _NOT_SENT)
    assert held_resolver == ["a.test"]


def test_the_http_transport_takes_connect_by_as_a_time() -> None:
    provider = Timed([])
    pool = Pool(Origin.parse("http://a.test"), opener=provider.open)
    transport = _transport(pool)
    action_ = transport.contract.actions["get"]
    with _context.root("shop", requires=("share.v1",)):
        sent = _context.Sent(
            principal=None,
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            tracestate="",
            deadline=None,
            idempotency_key="k",
            caller="shop",
        )
        with pytest.raises(TypeError, match="connect_by is a time as float, not int"):
            transport.call(action_, b"{}", sent, connect_by=5)
    assert provider.opened == 0
    pool.close()
