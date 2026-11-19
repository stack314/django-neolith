"""Wire part: neolith's connector, driven against the suite's own scripted
provider.

Each case calls the reference contract through the private seam
``over_http`` (ADR 0014 Decision 6), sent to a provider on a loopback socket
that answers with bytes the case writes and records the bytes it receives.
So each row of spec 03's response mapping is checked against bytes, not
against neolith's own provider, and the request the connector writes is
checked by spec 03's rules as the suite writes them. The cases are listed in
``spec/conformance/cases/wire-connector.json``. HTTP only: the wire is the
subject (T2).
"""

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import pytest

import neolith
from neolith import ContractError, Principal
from neolith._http_transport import over_http
from tests.conformance.wire import (
    CALLER,
    IDENTITY,
    ISSUER,
    PRINCIPAL_HEADER,
    SUBJECT,
    Scripted,
    bare,
    read_json,
    response,
    scripted,
    token_problem,
)
from tests.host_apps.reference.contract import Deposit, Refusal, RefusalKind, Values

# The smallest request ``accept`` takes.
_VALUES = Values.model_validate_json(
    b'{"boolean":false,"integer":0,"number":0,"string":"","bytes":"",'
    b'"decimal":"0","timestamp":"2027-01-01T00:00:00Z","date":"2027-01-01",'
    b'"duration":"0s","list":[],"map":{},"record":{"label":""},"enum":"red"}'
)
Call = Callable[[Any], object]


def _deposit(connector: Any) -> object:  # noqa: ANN401
    return connector.deposit(Deposit(account="a", amount=1))


def _accept(connector: Any) -> object:  # noqa: ANN401
    return connector.accept(_VALUES)


def _refuse(connector: Any) -> object:  # noqa: ANN401
    """A call to ``refuse``, the action that declares the kind ``gone``."""
    return connector.refuse(Refusal(kind=RefusalKind.NOT_FOUND, message="m"))


def _outcome(answer: bytes, call: Call = _deposit) -> tuple[object, str, Scripted]:
    """What the caller gets when the provider answers ``answer``: the reply,
    or the ``ContractError``; and the provider's origin and record.
    """
    with (
        scripted(answer) as (origin, provider),
        over_http(IDENTITY, origin),
        neolith.command_scope(CALLER),
    ):
        try:
            outcome = call(neolith.connect(IDENTITY))
        except ContractError as error:
            outcome = error
    return outcome, origin, provider


def _error(outcome: object) -> ContractError:
    assert type(outcome) is ContractError, outcome
    return outcome


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


# The request the connector writes.


@pytest.mark.conformance_case("wire_connector_writes_the_request_spec_03_fixes")
@pytest.mark.spec("HTTP-3", "TRN-2", "TRN-30", "TRN-31", "TRN-33")
def test_the_connector_writes_the_request_spec_03_fixes() -> None:
    principal = Principal(ISSUER, SUBJECT, {"roles": ("clerk",)})
    answer = response(200, b'{"total":5}')
    with (
        scripted(answer, answer) as (origin, provider),
        over_http(IDENTITY, origin),
        neolith.command_scope(CALLER, principal=principal, timeout=10),
    ):
        reference = neolith.connect(IDENTITY)
        reference.deposit(Deposit(account="a\u00e9", amount=5))
        reference.deposit(Deposit(account="a\u00e9", amount=5))
    first, second = provider.received
    for request in (first, second):
        assert request.line == "POST /reference/v1/deposit HTTP/1.1"
        assert request.field("Host") == origin.removeprefix("http://")
        assert request.field("Accept-Encoding") == "identity"
        assert request.field("Content-Type") == "application/json"
        assert request.field("Content-Length") == str(len(request.body))
        assert request.field("Transfer-Encoding") is None
        assert read_json(request.body) == {"account": "a\u00e9", "amount": 5}
        authorization = request.field("Authorization")
        assert authorization is not None
        assert token_problem(authorization, CALLER, IDENTITY) is None
        # Spec 03's own example: the principal's sent form, base64url.
        assert request.field("Neolith-Principal") == PRINCIPAL_HEADER
        # The budget in whole milliseconds, left of the root's 10 seconds.
        budget = request.field("Neolith-Timeout")
        assert budget is not None
        assert re.fullmatch(r"[1-9][0-9]{0,9}", budget)
        assert 8000 < int(budget) <= 10000
    # Each call from a root with no key gets a random key of its own, 128
    # bits in base64url (TRN-33).
    keys = [request.field("Idempotency-Key") for request in (first, second)]
    for key in keys:
        assert key is not None
        assert re.fullmatch(r'"[A-Za-z0-9_-]{22}"', key)
    assert keys[0] != keys[1]
    # Both carry the root's trace identifier, each with a parent of its own.
    parents = [request.field("traceparent") or "" for request in (first, second)]
    found = [
        re.fullmatch(r"00-((?!0{32})[0-9a-f]{32})-((?!0{16})[0-9a-f]{16})-0[01]", p)
        for p in parents
    ]
    assert all(found), parents
    traces = {match[1] for match in found if match}
    assert len(traces) == 1, parents


# A reply: 200 or 204, paired with the action's reply (HTTP-19).


@pytest.mark.conformance_case("wire_connector_reads_a_reply")
@pytest.mark.spec("HTTP-19", "TRN-12")
def test_a_200_carries_the_reply() -> None:
    reply, _, _ = _outcome(response(200, b'{"total":7}'))
    assert type(reply).__name__ == "Balance"
    assert reply.model_dump(mode="json") == {"total": 7}  # type: ignore[attr-defined]


@pytest.mark.conformance_case("wire_connector_reads_a_reply")
@pytest.mark.spec("HTTP-19", "TRN-12")
def test_a_204_is_the_reply_of_an_action_whose_reply_is_empty() -> None:
    reply, _, _ = _outcome(bare(204), _accept)
    assert reply is None


@pytest.mark.conformance_case("wire_connector_success_that_is_no_reply_is_internal")
@pytest.mark.spec("HTTP-19", "TRN-18")
@pytest.mark.parametrize(
    ("answer", "call"),
    [
        # The other pairing (ADR 0011 Decision 4).
        (response(200, b"{}"), _accept),
        (response(200, b"null"), _accept),
        (bare(204), _deposit),
        # A success status that is not 200 or 204.
        (response(201, b'{"total":1}'), _deposit),
        (response(202, b""), _accept),
        (response(206, b'{"total":1}'), _deposit),
        (response(299, b'{"total":1}'), _deposit),
        # A 200 whose body is not a valid reply.
        (response(200, b""), _deposit),
        (response(200, b"<html></html>"), _deposit),
        (response(200, b'{"total":"seven"}'), _deposit),
        (response(200, b"{}"), _deposit),
        (response(200, b'{"kind":"not_found","message":"m"}'), _deposit),
    ],
)
def test_a_success_that_is_not_a_reply_is_internal(answer: bytes, call: Call) -> None:
    outcome, _, _ = _outcome(answer, call)
    assert _error(outcome).kind == "internal"


@pytest.mark.conformance_case("wire_connector_reply_gets_the_receive_check")
@pytest.mark.spec("HTTP-19", "CON-42", "TRN-18")
@pytest.mark.parametrize(
    "body",
    [
        b'{"total":1,"total":2}',
        b'{"total":1,"x":"\\ud800"}',
        b'{"total":1,"x":1e400}',
        b'{"total":1,"x":NaN}',
        b'{"total":1,"x":' + b"[" * 300 + b"]" * 300 + b"}",
        b'{"total":1}\xff',
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
    # Each would read as a Balance; the receive check refuses it first.
    outcome, _, _ = _outcome(response(200, body))
    assert _error(outcome).kind == "internal"


# The kind comes from a neolith error body only, never from the status.


@pytest.mark.conformance_case(
    "wire_connector_failure_without_an_error_body_is_unavailable"
)
@pytest.mark.spec("HTTP-19", "TRN-3")
@pytest.mark.parametrize(
    "answer",
    [
        response(404, b""),
        response(404, b"<h1>Not Found</h1>"),
        response(302, b"", reason="Found"),
        response(301, b'{"total":1}', reason="Moved"),
        response(503, b"Service Unavailable"),
        response(500, b'{"error":"boom"}'),
        response(500, b'["internal"]'),
        response(400, b'{"kind":7,"message":"m"}'),
        response(400, b'{"kind":null,"message":"m"}'),
        response(400, b'{"kind":3,"kind":["moved"],"message":"m"}'),
        response(400, b'{"kind":"moved","message":"m","data":{"a":"\xff"}}'),
        response(400, b'{"message":"m","data":{"kind":"moved"}}'),
        response(400, b'"not_found"'),
        response(400, b'\xff{"kind":"not_found","message":"m"}'),
        response(400, b"[" * 100_000 + b"]" * 100_000),
        bare(304),
    ],
)
def test_a_failure_without_a_neolith_error_body_is_unavailable(
    answer: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, "neolith"):
        outcome, origin, _ = _outcome(answer)
    error = _error(outcome)
    assert error.kind == "unavailable"
    status = answer.split(b" ")[1].decode()
    assert error.message == (
        f"the provider at {origin} answered {status} without a neolith error body"
    )
    # Logged once, with the trace identifier, at WARNING.
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert re.search(r"\(trace [0-9a-f]{32}\)$", record.getMessage())


@pytest.mark.conformance_case("wire_connector_takes_the_kind_from_the_body")
@pytest.mark.spec("HTTP-19")
@pytest.mark.parametrize("status", [400, 404, 409, 418, 422, 500, 503, 504, 302])
@pytest.mark.parametrize(
    "kind", ["not_found", "internal", "timeout", "unavailable", "permission_denied"]
)
def test_the_kind_comes_from_the_body_whatever_the_status(
    status: int, kind: str
) -> None:
    answer = response(status, _json({"kind": kind, "message": "said"}))
    outcome, _, _ = _outcome(answer, _refuse)
    error = _error(outcome)
    assert (error.kind, error.message, error.data) == (kind, "said", None)


@pytest.mark.conformance_case("wire_connector_takes_the_kind_from_the_body")
@pytest.mark.spec("HTTP-19")
def test_a_declared_kind_arrives_with_its_detail_record() -> None:
    body = b'{"kind":"gone","message":"m","data":{"reason":"r"}}'
    error = _error(_outcome(response(422, body), _refuse)[0])
    assert error.kind == "gone"
    assert type(error.data).__name__ == "Gone"
    assert error.data.model_dump(mode="json") == {"reason": "r"}  # type: ignore[union-attr]


@pytest.mark.conformance_case("wire_connector_takes_the_kind_from_the_body")
@pytest.mark.spec("HTTP-19")
def test_a_detail_is_read_from_the_text_the_provider_wrote() -> None:
    # Whitespace anywhere JSON admits it, and members in any order.
    body = b' \r\n{ "data" :\t{ "reason" : "r" } , "message":"m" ,"kind" : "gone" }\n'
    error = _error(_outcome(response(422, body), _refuse)[0])
    assert (error.kind, error.message) == ("gone", "m")
    assert error.data.model_dump(mode="json") == {"reason": "r"}  # type: ignore[union-attr]


@pytest.mark.conformance_case("wire_connector_error_body_it_cannot_read_is_internal")
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
        b'{"kind":"gone","message":"m","data":{"reason":7}}',
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
        outcome, _, _ = _outcome(response(422, body), _refuse)
    error = _error(outcome)
    assert error.kind == "internal"
    assert "logged the cause under trace" in error.message
    assert caplog.records
