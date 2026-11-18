"""The connector over HTTP: the HTTP transport of ADR 0014 on ADR 0028's client.

A provider is played on the far end of a socket pair (``tests.http_peer``),
or by a local TCP server where the test says so. The transport writes ADR
0027 Decision 9's fields in ADR 0028's order, refuses a call with no calling
app or no signing key before anything is sent, reads the budget once a
connection is ready, and maps each final response as spec 03 says. Time is
a clock the tests set, or a short deadline on the local machine (T3).
"""

import base64
import contextlib
import dataclasses
import json
import logging
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import pytest
from pydantic import BaseModel

import neolith
from neolith import (
    Contract,
    ContractError,
    Principal,
    _caller,
    _context,
    _host,
    _http,
    _http_transport,
    _token,
    action,
    contract,
    transports,
)
from neolith._connector import Connector, Failure, InProcess
from neolith._context import Sent
from neolith._contracts import Action
from neolith._crossing import Receiver
from neolith._http_transport import HTTPTransport, origin_problem, over_http
from tests.declaration_contracts import Level, Sku
from tests.http_peer import Provider, Script, read_request


class Ask(BaseModel):
    n: int = 0


class Told(BaseModel):
    n: int


class Gone(BaseModel):
    n: int


@contract("desk.v1")
class Desk(Protocol):
    @action(idempotent=True, errors={"gone": Gone})
    def ask(self, request: Ask) -> Told: ...

    @action
    def tell(self, request: Ask) -> None: ...


DESK = Contract.of(Desk)
ORIGIN = "http://desk.test:8080"
ALICE = Principal("https://id.example.com", "alice", {"roles": ("clerk",)})
# Generated per run: no secret is stored in the repository (K9).
_SHOP_SECRET = secrets.token_urlsafe(32)


def _secret(app: str = "shop", text: str = _SHOP_SECRET) -> _token.Secret:
    return _token.secret(text, app, f"{app}-1")


def _callers(*apps: str) -> _caller.Callers:
    return _caller.Callers(signing=tuple(_secret(app) for app in apps))


def _response(
    status: int, body: bytes = b"", *, reason: str = "X", length: bool = True
) -> bytes:
    head = f"HTTP/1.1 {status} {reason}\r\n"
    if length:
        head += f"Content-Length: {len(body)}\r\n"
    return head.encode() + b"\r\n" + body


def _error(**members: object) -> bytes:
    return json.dumps(members).encode()


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


@contextlib.contextmanager
def _desk(
    *scripts: Script, callers: _caller.Callers | None = None
) -> Iterator[tuple[Any, Provider]]:
    """A connector to desk.v1 over HTTP, with the provider ``scripts`` play."""
    provider = Provider(scripts)
    pool = _http.Pool(_http.Origin.parse(ORIGIN), opener=provider.open)
    transport = HTTPTransport(Desk, pool, callers or _callers("shop"))
    try:
        yield Connector(Desk, transport), provider
    finally:
        pool.close()
        provider.finish()


def _root(app: str | None = "shop", **given: Any) -> Any:  # noqa: ANN401
    return _context.root(app, requires=("desk.v1",), **given)


def _outcome(response: bytes, call: Callable[[Any], object] | None = None) -> object:
    """What the caller gets when the provider answers ``response``: the
    reply, or the ``ContractError``.
    """
    with _desk(Recorded(response).script) as (connector, _), _root():
        try:
            return (call or (lambda c: c.ask(Ask(n=1))))(connector)
        except ContractError as error:
            return error


def _kind(outcome: object) -> str:
    assert type(outcome) is ContractError
    return outcome.kind


# HTTP-19: a reply is 200 or 204, paired with the action's reply.


@pytest.mark.spec("HTTP-19", "TRN-12")
def test_a_200_carries_the_reply() -> None:
    assert _outcome(_response(200, b'{"n":7}')) == Told(n=7)


@pytest.mark.spec("HTTP-19")
def test_a_204_is_the_reply_of_an_action_whose_reply_is_empty() -> None:
    assert _outcome(_response(204, length=False), lambda c: c.tell(Ask())) is None


@pytest.mark.spec("HTTP-19", "TRN-18")
@pytest.mark.parametrize(
    ("response", "action"),
    [
        # The other pairing (ADR 0011 Decision 4).
        (_response(200, b"{}"), "tell"),
        (_response(200, b"null"), "tell"),
        (_response(204, length=False), "ask"),
        # A success status that is not 200 or 204.
        (_response(201, b'{"n":1}'), "ask"),
        (_response(202, b""), "tell"),
        (_response(206, b'{"n":1}'), "ask"),
        (_response(299, b'{"n":1}'), "ask"),
        # A 200 whose body is not a valid reply.
        (_response(200, b""), "ask"),
        (_response(200, b"<html></html>"), "ask"),
        (_response(200, b'{"n":"seven"}'), "ask"),
        (_response(200, b"{}"), "ask"),
        (_response(200, b'{"kind":"not_found","message":"m"}'), "ask"),
    ],
)
def test_a_success_that_is_not_a_reply_is_internal(
    response: bytes, action: str
) -> None:
    outcome = _outcome(response, lambda c: getattr(c, action)(Ask()))
    assert _kind(outcome) == "internal"


@pytest.mark.spec("HTTP-19", "CON-42", "TRN-18")
@pytest.mark.parametrize(
    "body",
    [
        b'{"n":1,"n":2}',
        b'{"n":1,"x":"\\ud800"}',
        b'{"n":1,"x":1e400}',
        b'{"n":1,"x":NaN}',
        b'{"n":1,"x":' + b"[" * 300 + b"]" * 300 + b"}",
        b'{"n":1}\xff',
    ],
    ids=[
        "a member twice",
        "a lone surrogate",
        "past a double",
        "NaN",
        "deep",
        "not UTF-8",
    ],
)
def test_a_reply_gets_the_network_receive_check(body: bytes) -> None:
    # Each would read as a Told; the receive check refuses it first.
    assert _kind(_outcome(_response(200, body))) == "internal"


@pytest.mark.spec("CON-40")
def test_a_reply_is_checked_against_exact_integer_bounds() -> None:
    # The reply's description admits n up to 2**53 - 1; the integer one past
    # 2**53 reads as 2**53 as a double, and is refused here as written.
    assert _kind(_outcome(_response(200, b'{"n":9007199254740993}'))) == "internal"
    assert _outcome(_response(200, b'{"n":9007199254740991}')) == Told(n=2**53 - 1)


# HTTP-19: the kind is taken from the body only, never from the status.


@pytest.mark.spec("HTTP-19", "TRN-3")
@pytest.mark.parametrize(
    "response",
    [
        _response(404, b""),
        _response(404, b"<h1>Not Found</h1>"),
        _response(302, b"", reason="Found"),
        _response(301, b'{"n":1}', reason="Moved"),
        _response(503, b"Service Unavailable"),
        _response(500, b'{"error":"boom"}'),
        _response(500, b'["internal"]'),
        _response(400, b'{"kind":7,"message":"m"}'),
        _response(400, b'{"kind":null,"message":"m"}'),
        _response(400, b'{"kind":3,"kind":["moved"],"message":"m"}'),
        _response(400, b'{"kind":"moved","message":"m","data":{"a":"\xff"}}'),
        _response(400, b'{"message":"m","data":{"kind":"moved"}}'),
        _response(400, b'"not_found"'),
        _response(400, b'\xff{"kind":"not_found","message":"m"}'),
        _response(400, b"[" * 100_000 + b"]" * 100_000),
        _response(304, length=False),
    ],
)
def test_a_failure_without_a_neolith_error_body_is_unavailable(
    response: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, "neolith"):
        outcome = _outcome(response)
    assert _kind(outcome) == "unavailable"
    assert isinstance(outcome, ContractError)
    status = response.split(b" ")[1].decode()
    assert outcome.message == (
        f"the provider at {ORIGIN} answered {status} without a neolith error body"
    )
    # Logged once, with the trace identifier, at WARNING.
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert re.search(r"\(trace [0-9a-f]{32}\)$", record.getMessage())


@pytest.mark.spec("HTTP-19")
@pytest.mark.parametrize("status", [400, 404, 409, 418, 422, 500, 503, 504, 302])
@pytest.mark.parametrize(
    "kind", ["not_found", "internal", "timeout", "unavailable", "permission_denied"]
)
def test_the_kind_comes_from_the_body_whatever_the_status(
    status: int, kind: str
) -> None:
    outcome = _outcome(_response(status, _error(kind=kind, message="said so")))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message, outcome.data) == (kind, "said so", None)


@pytest.mark.spec("HTTP-19", "CON-43")
def test_a_declared_kind_arrives_with_its_detail_record() -> None:
    outcome = _outcome(_response(422, b'{"kind":"gone","message":"m","data":{"n":4}}'))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.data) == ("gone", Gone(n=4))


@pytest.mark.spec("HTTP-19")
def test_a_detail_is_read_from_the_text_the_provider_wrote() -> None:
    # Whitespace anywhere JSON admits it, and members in any order.
    body = b' \r\n{ "data" :\t{ "n" : 4 } , "message":"m" ,"kind" : "gone" }\n'
    outcome = _outcome(_response(422, body))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message, outcome.data) == ("gone", "m", Gone(n=4))


@pytest.mark.spec("CON-28")
def test_an_unknown_kind_arrives_with_its_detail_as_an_untyped_map() -> None:
    body = b'{"kind":"moved","message":"m","data":{"to":"b-2","n":[1,2.5,null]}}'
    outcome = _outcome(_response(422, body))
    assert type(outcome) is ContractError
    assert outcome.kind == "moved"
    assert outcome.data == {"to": "b-2", "n": [1, 2.5, None]}


@pytest.mark.spec("CON-28")
def test_an_unknown_kind_without_data_arrives_without_a_detail() -> None:
    outcome = _outcome(_response(422, _error(kind="moved", message="m")))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message, outcome.data) == ("moved", "m", None)


@pytest.mark.spec("CON-49", "HTTP-19")
@pytest.mark.parametrize(
    "body",
    [
        # An unknown kind whose data is not a JSON object (CON-30).
        b'{"kind":"moved","message":"m","data":null}',
        b'{"kind":"moved","message":"m","data":[1]}',
        b'{"kind":"moved","message":"m","data":"x"}',
        b'{"kind":"moved","message":"m","data":3}',
        b'{"kind":"moved","message":"m","data":{"n":9007199254740992}}',
        b'{"kind":"moved","message":"m","data":' + b'{"a":' * 40 + b"1" + b"}" * 41,
        # A declared kind without its detail, or with one its record refuses.
        b'{"kind":"gone","message":"m"}',
        b'{"kind":"gone","message":"m","data":null}',
        b'{"kind":"gone","message":"m","data":{"n":"x"}}',
        b'{"kind":"gone","message":"m","data":{"n":9007199254740993}}',
        # A standard kind with a detail.
        b'{"kind":"not_found","message":"m","data":{}}',
        b'{"kind":"not_found","message":"m","data":null}',
        # A kind or a message outside CON-22 and CON-25.
        b'{"kind":"Not Found","message":"m"}',
        b'{"kind":"","message":"m"}',
        b'{"kind":"not_found","message":"\\udc00"}',
        # Not exactly kind, message and data.
        b'{"kind":"not_found"}',
        b'{"kind":"not_found","message":7}',
        b'{"kind":"not_found","message":null}',
        b'{"kind":"not_found","message":"m","detail":"d"}',
        b'{"kind":"not_found","message":"m","trace":"t"}',
        # Not I-JSON (CON-42), whichever occurrence of a name comes last.
        b'{"kind":"not_found","message":"m","message":"n"}',
        b'{"kind":"moved","kind":3,"message":"m"}',
        b'{"kind":3,"kind":"moved","message":"m"}',
        b'{"kind":null,"message":"m","kind":"moved"}',
        b'{"kind":"moved","message":"m","data":{"a":1,"a":2}}',
        b'{"kind":"not_found","message":"m","x":1e400}',
    ],
)
def test_a_neolith_error_body_that_cannot_be_read_is_internal(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, "neolith"):
        outcome = _outcome(_response(422, body))
    assert _kind(outcome) == "internal"
    assert isinstance(outcome, ContractError)
    assert "logged the cause under trace" in outcome.message
    assert caplog.records


@pytest.mark.spec("CON-28")
def test_an_unknown_kinds_detail_at_the_untyped_bounds_arrives() -> None:
    deep = b'{"a":' * 31 + b"1" + b"}" * 31
    outcome = _outcome(
        _response(422, b'{"kind":"moved","message":"m","data":' + deep + b"}")
    )
    assert type(outcome) is ContractError
    assert outcome.kind == "moved"


def test_a_deep_error_body_is_judged_without_recursion_on_a_small_stack() -> None:
    # Issue #125: on a 1 MB thread stack, 9,000 levels of nesting would end the
    # process inside the json module; the nesting is judged before it reads.
    deep = b"[" * 9000 + b"]" * 9000
    bodies = [
        _response(500, deep),
        _response(422, b'{"kind":"moved","message":"m","data":' + deep + b"}"),
        _response(422, b'{"kind":"moved","message":"m","data":{"a":' + deep + b"}}"),
    ]
    found: list[object] = []

    def run() -> None:
        found.extend(_outcome(body) for body in bodies)

    old = threading.stack_size(1 << 20)
    try:
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(30)
    finally:
        threading.stack_size(old)
    # Text nested deeper than the reader admits is not JSON text to it, so
    # it carries no neolith error body (spec 03, as HTTP-7 reads a token).
    assert [_kind(outcome) for outcome in found] == ["unavailable"] * 3


# The fields of one attempt (ADR 0027 Decision 9, in ADR 0028's order).


def _head(request: bytes) -> list[str]:
    head, _, _ = request.partition(b"\r\n\r\n")
    return head.decode("ascii").split("\r\n")


@pytest.mark.spec("HTTP-3", "TRN-2", "TRN-30")
def test_the_request_carries_each_field_in_its_one_form_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(_context, "now", clock)
    monkeypatch.setattr(_http, "_now", clock)
    provider = Recorded(_response(200, b'{"n":1}'))
    fields = _context.root_fields("shop", requires=("desk.v1",), principal=ALICE)
    with (
        _desk(provider.script) as (connector, _),
        _context.opened(**{**fields, "tracestate": "a=1,b=2"}) as scope,
    ):
        connector.ask(Ask(n=3), timeout=2)
    (request,) = provider.requests
    lines = _head(request)
    names = [line.split(":", 1)[0] for line in lines[1:]]
    assert lines[0] == "POST /desk/v1/ask HTTP/1.1"
    assert names == [
        "Host",
        "Accept-Encoding",
        "Content-Type",
        "Content-Length",
        "Authorization",
        "Neolith-Principal",
        "Neolith-Timeout",
        "Idempotency-Key",
        "traceparent",
        "tracestate",
    ]
    fields = dict(line.split(": ", 1) for line in lines[1:])
    assert fields["Host"] == "desk.test:8080"
    assert fields["Accept-Encoding"] == "identity"
    assert fields["Content-Type"] == "application/json"
    assert request.endswith(b"\r\n\r\n" + b'{"n":3}')
    assert fields["Content-Length"] == "7"
    principal = base64.urlsafe_b64decode(fields["Neolith-Principal"] + "==")
    assert principal == _context.principal_form(ALICE)
    assert fields["Neolith-Timeout"] == "2000"
    assert re.fullmatch(r'"[A-Za-z0-9_-]{22}"', fields["Idempotency-Key"])
    trace = scope.context.trace_id
    assert re.fullmatch(
        rf"00-{trace}-(?!0{{16}})[0-9a-f]{{16}}-00", fields["traceparent"]
    )
    assert fields["tracestate"] == "a=1,b=2"


@pytest.mark.spec("HTTP-7", "HTTP-13", "HTTP-14")
def test_the_token_is_the_calling_apps_for_the_contract() -> None:
    provider = Recorded(_response(200, b'{"n":1}'))
    with _desk(provider.script) as (connector, _), _root():
        connector.ask(Ask())
    fields = dict(line.split(": ", 1) for line in _head(provider.requests[0])[1:])
    keys = {"shop": {"shop-1": _secret()}}
    now = int(_token_now())
    for identity, expected in (("desk.v1", ("shop", None)), ("desk.v2", None)):
        app, refused = _token.check(fields["Authorization"], identity, keys, now)
        if expected is None:
            assert refused == _token.AUDIENCE
        else:
            assert (app, refused) == expected


def _token_now() -> float:
    return time.time()


def test_a_call_with_no_principal_deadline_or_tracestate_sends_no_field_for_it() -> (
    None
):
    provider = Recorded(_response(200, b'{"n":1}'))
    with _desk(provider.script) as (connector, _), _root():
        connector.ask(Ask())
    names = [line.split(":", 1)[0] for line in _head(provider.requests[0])[1:]]
    assert names == [
        "Host",
        "Accept-Encoding",
        "Content-Type",
        "Content-Length",
        "Authorization",
        "Idempotency-Key",
        "traceparent",
    ]


@pytest.mark.spec("HTTP-17", "TRN-64")
def test_the_budget_is_read_once_a_connection_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(_context, "now", clock)
    monkeypatch.setattr(_http, "_now", clock)
    provider = Recorded(_response(200, b'{"n":1}'))
    with _desk(provider.script) as (connector, peer), _root():
        opener = peer.open

        def open_slowly(until: float) -> socket.socket:
            # Reaching the provider takes a quarter of a second.
            clock.time += 0.25
            return opener(until)

        connector._transport._pool._opener = open_slowly
        connector.ask(Ask(), timeout=1)
    fields = dict(line.split(": ", 1) for line in _head(provider.requests[0])[1:])
    assert fields["Neolith-Timeout"] == "750"


@pytest.mark.spec("HTTP-17", "TRN-28", "TRN-64")
def test_a_budget_under_1_ms_once_connected_fails_with_timeout_unsent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(_context, "now", clock)
    monkeypatch.setattr(_http, "_now", clock)
    provider = Recorded(_response(200, b'{"n":1}'))
    minted: list[str] = []
    real = _caller.Callers.mint

    def counting(self: _caller.Callers, app: str, identity: str) -> Any:  # noqa: ANN401
        minted.append(app)
        return real(self, app, identity)

    monkeypatch.setattr(_caller.Callers, "mint", counting)
    with _desk(provider.script) as (connector, peer), _root():
        opener = peer.open

        def open_slowly(until: float) -> socket.socket:
            clock.time += 1 - 2**-11
            return opener(until)

        connector._transport._pool._opener = open_slowly
        with pytest.raises(ContractError) as raised:
            connector.ask(Ask(), timeout=1)
    assert (raised.value.kind, raised.value.message) == (
        "timeout",
        "the deadline passed before the call was sent",
    )
    # Nothing was written, and no token was minted for it.
    assert provider.requests == []
    assert minted == []


def test_each_attempt_mints_its_own_token_once_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    real = _caller.Callers.mint

    def counting(self: _caller.Callers, app: str, identity: str) -> Any:  # noqa: ANN401
        order.append(f"mint {app} {identity}")
        return real(self, app, identity)

    monkeypatch.setattr(_caller.Callers, "mint", counting)
    provider = Recorded(_response(200, b'{"n":1}'), _response(200, b'{"n":2}'))
    with _desk(provider.script) as (connector, peer), _root():
        opener = peer.open

        def opening(until: float) -> socket.socket:
            order.append("open")
            return opener(until)

        connector._transport._pool._opener = opening
        connector.ask(Ask())
        connector.ask(Ask())
    # One connection, reused; a token minted after it was ready, per call.
    assert order == ["open", "mint shop desk.v1", "mint shop desk.v1"]


class _Clock:
    """A clock the test moves; each reading returns ``time``."""

    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


# HTTP-18: refused before anything is sent.


@pytest.mark.spec("HTTP-18", "TRN-42")
def test_a_call_from_no_app_is_refused_unsent_as_in_process() -> None:
    with (
        _desk() as (connector, provider),
        _root(None),
        pytest.raises(ContractError) as over_http_,
    ):
        connector.ask(Ask())
    assert provider.opened == 0
    in_process: Any = Connector(
        Desk, InProcess(Desk, _Told(), app="desk", principal_from=("shop",))
    )
    with _root(None), pytest.raises(ContractError) as in_process_:
        in_process.ask(Ask())
    assert (over_http_.value.kind, over_http_.value.message) == (
        in_process_.value.kind,
        in_process_.value.message,
    )
    assert over_http_.value.kind == "unauthenticated"
    assert over_http_.value.message == (
        "a call to desk.v1 has no calling app; a call is made from a scope that "
        "belongs to an app"
    )


class _Told:
    def ask(self, request: Ask) -> Told:
        return Told(n=request.n)

    def tell(self, request: Ask) -> None:
        return None


@pytest.mark.spec("HTTP-18")
def test_a_call_from_an_app_with_no_signing_key_is_refused_unsent() -> None:
    with (
        _desk(callers=_callers("other")) as (connector, provider),
        _root("shop"),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask())
    assert provider.opened == 0
    assert raised.value.kind == "unauthenticated"
    assert raised.value.message == (
        'app shop has no signing key in NEOLITH["SIGNING_SECRETS"], so it cannot '
        "call desk.v1 over HTTP"
    )


# Failures of the client: unavailable or timeout, logged once.


@pytest.mark.spec("TRN-3", "TRN-4", "TRN-64")
def test_a_provider_that_holds_the_call_past_its_deadline_gives_timeout() -> None:
    def hold(sock: socket.socket) -> None:
        read_request(sock)
        sock.recv(1)  # until the client closes its end

    with (
        _desk(hold) as (connector, _),
        _root(),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask(), timeout=0.2)
    assert (raised.value.kind, raised.value.message) == (
        "timeout",
        "the deadline passed before the call ended; the action might have run",
    )
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.spec("TRN-3")
def test_an_unreachable_provider_gives_unavailable_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A port that was just free on the loopback address: nothing listens.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    pools = _http.Pools()
    transport = HTTPTransport(
        Desk, pools.pool(_http.Origin.parse(origin)), _callers("shop")
    )
    connector: Any = Connector(Desk, transport)
    try:
        with (
            _root(),
            caplog.at_level(logging.WARNING, "neolith"),
            pytest.raises(ContractError) as raised,
        ):
            connector.ask(Ask(), timeout=5)
    finally:
        pools.close()
    assert raised.value.kind == "unavailable"
    assert raised.value.message.startswith(
        f"the provider at {origin} could not be reached"
    )
    (record,) = caplog.records
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "desk.v1, action 'ask'" in message
    assert "before writing" in message
    assert re.search(r"\(trace [0-9a-f]{32}\)$", message)


@pytest.mark.spec("TRN-3")
def test_a_response_outside_http_1_1_is_unavailable() -> None:
    outcome = _outcome(b"HTTP/1.1 200\r\nContent-Length: 2\r\n\r\n{}")
    assert _kind(outcome) == "unavailable"
    assert isinstance(outcome, ContractError)
    assert outcome.message == f"the provider at {ORIGIN} did not answer in HTTP/1.1"


def test_no_log_line_or_error_holds_the_token(caplog: pytest.LogCaptureFixture) -> None:
    provider = Recorded(_response(503, b"down"))
    with (
        caplog.at_level(logging.DEBUG),
        _desk(provider.script) as (connector, _),
        _root(),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask())
    fields = dict(line.split(": ", 1) for line in _head(provider.requests[0])[1:])
    token = fields["Authorization"].removeprefix("Bearer ")
    texts = [record.getMessage() for record in caplog.records]
    texts += [str(raised.value), repr(raised.value)]
    for part in token.split("."):
        assert all(part not in text for text in texts)


# The transport's own surface.


def test_the_transport_is_the_public_name_of_the_setting() -> None:
    assert transports.HTTPTransport is HTTPTransport
    assert transports.__all__ == ["HTTPTransport"]
    assert HTTPTransport.receiver is Receiver.NETWORK
    assert InProcess.receiver is Receiver.IN_PROCESS


def test_a_connector_refuses_a_transport_that_says_no_receiver() -> None:
    class Unsaid:
        contract = DESK
        receiver = "network"

        def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
            raise AssertionError  # pragma: no cover

    with pytest.raises(TypeError, match="a transport's receiver is a Receiver"):
        Connector(Desk, Unsaid())  # type: ignore[arg-type]


def test_the_transport_refuses_what_a_connector_never_passes() -> None:
    with _desk() as (connector, provider):
        transport = connector._transport
        sent = Sent(None, "1" * 32, "", None, "key", "shop")
        with pytest.raises(TypeError):
            transport.call(Contract.of(_Other).actions["ask"], b"{}", sent)
        with pytest.raises(TypeError):
            transport.call(DESK.actions["ask"], b"{}", dataclasses.asdict(sent))
        with pytest.raises(TypeError):
            transport.call(DESK.actions["ask"], "{}", sent)
    assert provider.opened == 0


@contract("other.v1")
class _Other(Protocol):
    @action
    def ask(self, request: Ask) -> Told: ...


@pytest.mark.parametrize(
    ("deadline", "at", "budget"),
    [
        (None, 1000.0, None),
        (1001.0, 1000.0, 1000),
        (1000.0015, 1000.0, 1),
        (1000.001, 1000.0, 1),
        (1000.0009, 1000.0, 0),
        # Short of a whole millisecond by less than the deadline's float can
        # hold, and by more.
        (1000.0 + 0.001, 1000.0, 1),
        (1000.0019999996, 1000.0, 1),
        (1000.0009999996, 1000.0, 0),
        (1e6 + 0.003, 1e6, 3),
        (1000.0, 1000.0, 0),
        (999.0, 1000.0, -1000),
        (1e9, 0.0, 9_999_999_999),
    ],
)
def test_one_helper_turns_a_deadline_into_a_budget(
    deadline: float | None, at: float, budget: int | None
) -> None:
    assert _context.budget(deadline, at) == budget


@pytest.mark.spec("TRN-28")
@pytest.mark.parametrize("timeout", [0.0009999996, 0.0009999999999])
def test_a_timeout_just_short_of_1_ms_is_never_sent(timeout: float) -> None:
    with (
        _desk() as (connector, provider),
        _root(),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask(), timeout=timeout)
    assert raised.value.kind == "timeout"
    assert provider.opened == 0


@pytest.mark.spec("TRN-36", "TRN-38")
@pytest.mark.parametrize(
    "deadline", [1000.0019999996, 1000.001, 1000.0005, 1001.0, 999.5, 1000.0]
)
def test_an_in_process_providers_deadline_is_never_later_than_the_callers(
    monkeypatch: pytest.MonkeyPatch, deadline: float
) -> None:
    clock = _Clock()
    monkeypatch.setattr(_context, "now", clock)
    seen: list[float | None] = []

    class Seeing:
        def ask(self, request: Ask, ctx: neolith.CallContext) -> Told:
            seen.append(ctx.deadline)
            return Told(n=1)

        def tell(self, request: Ask) -> None:
            return None

    transport = InProcess(Desk, Seeing(), app="desk", principal_from=("shop",))
    sent = Sent(None, "1" * 32, "", deadline, "key", "shop")
    with contextlib.suppress(Failure):
        transport.call(DESK.actions["ask"], b"{}", sent)
    assert all(found is not None and found <= deadline for found in seen)


# A remote provider's url (ADR 0014 Decision 3), as over_http admits it.


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8000",
        "https://inventory.internal:8443",
        "https://a-b.c-d.example",
        "http://127.0.0.1:65535",
        "http://[::1]:8000",
        "https://[2001:db8::1]",
        "http://1.example",
        "http://x" + ".y" * 125,
    ],
)
def test_an_origin_in_its_one_spelling_is_admitted(url: str) -> None:
    assert origin_problem(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("HTTP://localhost", _http_transport.SCHEME),
        ("ftp://localhost", _http_transport.SCHEME),
        (" http://localhost", _http_transport.SCHEME),
        ("http:/localhost", _http_transport.SCHEME),
        ("http://user:pa55@localhost", _http_transport.CREDENTIAL),
        ("http://localhost/", _http_transport.PATH),
        ("http://localhost?token=s3cret", _http_transport.PATH),
        ("http://localhost#f", _http_transport.PATH),
        ("http://Localhost", _http_transport.CASE),
        ("http://bücher.example", _http_transport.CASE),
        ("http://a_b.example", _http_transport.NAME),
        ("http://example.com.", _http_transport.NAME),
        ("http://-a.example", _http_transport.NAME),
        ("http://exa mple.com", _http_transport.NAME),
        ("http://", _http_transport.NAME),
        ("http://" + "a" * 64, _http_transport.NAME),
        ("http://x" + ".y" * 127, _http_transport.NAME),
        ("http://0x7f.1", _http_transport.IPV4),
        ("http://1.0x2", _http_transport.IPV4),
        ("http://127.1", _http_transport.IPV4),
        ("http://127.000.0.1", _http_transport.IPV4),
        ("http://256.0.0.1", _http_transport.IPV4),
        ("http://a.0x", _http_transport.IPV4),
        ("http://[::ffff:127.0.0.1]", _http_transport.IPV6),
        ("http://[::ffff:7f00:1]", _http_transport.IPV6),
        ("http://[0:0::1]", _http_transport.IPV6),
        ("http://[fe80::1%eth0]", _http_transport.IPV6),
        ("http://[::1", _http_transport.IPV6),
        ("http://localhost:0", _http_transport.PORT),
        ("http://localhost:08000", _http_transport.PORT),
        ("http://localhost:", _http_transport.PORT),
        ("http://localhost:65536", _http_transport.PORT),
        ("http://localhost:80 ", _http_transport.PORT),
        ("http://[::1]x", _http_transport.PORT),
        ("http://localhost:80", _http_transport.DEFAULT_PORT),
        ("https://localhost:443", _http_transport.DEFAULT_PORT),
    ],
)
def test_any_other_spelling_is_refused_with_its_reason(url: str, reason: str) -> None:
    assert origin_problem(url) == reason


# The over_http seam (ADR 0014 Decision 6), on the test host.


def _serving(
    *responses: bytes,
) -> tuple[str, Recorded, threading.Thread, socket.socket]:
    """A local TCP server that plays ``responses`` on one connection."""
    server = socket.create_server(("127.0.0.1", 0))
    # A test that never connects still ends: the wait for one is bounded.
    server.settimeout(10)
    recorded = Recorded(*responses)

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            recorded.script(connection)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.getsockname()[1]}", recorded, thread, server


@pytest.fixture
def signing(monkeypatch: pytest.MonkeyPatch) -> _caller.Callers:
    """The test host's table, with a signing key for each hosted app."""
    host, installed = _host._table()
    assert host is not None
    callers = _callers("stock", "web", "couriers")
    signed = dataclasses.replace(host, callers=callers)
    monkeypatch.setattr(_host, "_table", lambda: (signed, installed))
    return callers


@pytest.mark.spec("HTTP-19", "TRN-31")
def test_over_http_sends_a_hosted_contracts_calls_over_the_network(
    signing: _caller.Callers,
) -> None:
    origin, recorded, thread, server = _serving(_response(200, b'{"on_hand":5}'))
    try:
        with over_http("couriers.v3", origin), neolith.command_scope("web"):
            level = neolith.connect("couriers.v3").quote(Sku(sku="abcde"))
    finally:
        thread.join(10)
        server.close()
    assert level == Level(on_hand=5)
    (request,) = recorded.requests
    lines = _head(request)
    assert lines[0] == "POST /couriers/v3/quote HTTP/1.1"
    fields = dict(line.split(": ", 1) for line in lines[1:])
    assert fields["Host"] == origin.removeprefix("http://")
    keys = {"web": {"web-1": _secret("web")}}
    app, refused = _token.check(
        fields["Authorization"], "couriers.v3", keys, int(_token_now())
    )
    assert (app, refused) == ("web", None)
    # On exit, the set-up's own transport is back: in-process, to the app.
    with neolith.command_scope("web"):
        assert neolith.connect("couriers.v3").quote(Sku(sku="ab")) == Level(on_hand=2)


def test_over_http_refuses_what_it_cannot_carry(signing: _caller.Callers) -> None:
    with (
        pytest.raises(neolith.UsageError, match=r"does not know desk\.v1"),
        over_http("desk.v1", "http://localhost:1"),
    ):
        pass  # pragma: no cover
    with (
        pytest.raises(
            neolith.UsageError, match=re.escape(_http_transport.PATH)
        ) as raised,
        over_http("couriers.v3", "http://localhost:1/?token=s3cret"),
    ):
        pass  # pragma: no cover
    assert "s3cret" not in str(raised.value)
    with (
        over_http("couriers.v3", "http://localhost:1"),
        pytest.raises(neolith.UsageError, match="already entered"),
        over_http("couriers.v3", "http://localhost:2"),
    ):
        pass  # pragma: no cover
    with (
        pytest.raises(TypeError),
        over_http("couriers.v3", b"http://localhost:1"),  # type: ignore[arg-type]
    ):
        pass  # pragma: no cover


def test_over_http_refuses_an_identity_required_by_an_app_with_no_signing_key() -> None:
    # The test host has no SIGNING_SECRETS; stock and web require couriers.v3.
    with (
        pytest.raises(neolith.UsageError) as raised,
        over_http("couriers.v3", "http://localhost:1"),
    ):
        pass  # pragma: no cover
    assert "has no signing key in NEOLITH" in str(raised.value)
    assert "couriers.v3" in str(raised.value)


def test_over_http_closes_its_pool_and_leaves_no_seam_behind(
    signing: _caller.Callers,
) -> None:
    def inside() -> None:
        with over_http("couriers.v3", "http://localhost:1"):
            assert _http_transport.seamed("couriers.v3") is not None
            raise RuntimeError("inside")

    with pytest.raises(RuntimeError, match="inside"):
        inside()
    assert _http_transport.seamed("couriers.v3") is None


def test_a_failure_over_http_to_the_test_host_is_unavailable(
    signing: _caller.Callers, caplog: pytest.LogCaptureFixture
) -> None:
    origin, _, thread, server = _serving(_response(404, b"Not Found"))
    try:
        with (
            over_http("couriers.v3", origin),
            neolith.command_scope("web"),
            caplog.at_level(logging.WARNING, "neolith"),
            pytest.raises(ContractError) as raised,
        ):
            neolith.connect("couriers.v3").quote(Sku(sku="a"))
    finally:
        thread.join(10)
        server.close()
    assert raised.value.kind == "unavailable"
    assert re.search(r"answered 404 without a neolith error body", raised.value.message)


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200\r\n\r\n",
        _response(503, b"down"),
        _response(404, _error(kind="x")),
    ],
)
def test_the_callers_error_is_never_chained_to_the_client(response: bytes) -> None:
    # Built afresh from kind and message, so no client frame reaches a report.
    outcome = _outcome(response)
    assert isinstance(outcome, ContractError)
    assert outcome.__context__ is None
    assert outcome.__cause__ is None
