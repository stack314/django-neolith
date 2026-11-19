"""Wire part: the suite's own oracles, tested themselves.

Every helper in ``tests.conformance.wire`` that decides whether a case
passes is code under test: the token check, the base64url and JSON readers,
the reading of a response and of a request. Each gets a known-good input,
taken from spec 03's own bytes where the spec gives them, and one known-bad
input per rule, with the reason the helper gives, so that a rule left out
of a helper fails a test here. These tests cite no requirement: they check
the suite, not neolith (the CNF-2 comparison's own tests are in
``test_projection.py``).
"""

import hashlib
import hmac
import json
import socket
import threading
from collections.abc import Iterator
from decimal import Decimal

import pytest

from tests.conformance.wire import (
    PRINCIPAL_HEADER,
    Answer,
    NotJSON,
    Received,
    _read,
    b64url,
    read_json,
    token_problem,
    unb64url,
)

# Spec 03's example token: the app orders calling inventory.v1 under the
# key id orders-1 and the example secret, issued at 1767225600 (iat) for
# 300 seconds. Its bytes are the spec's.
EXAMPLE: str = (
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6Im5lb2xpdGgrand0Iiwia2lkIjoib3JkZXJzLTEifQ"
    ".eyJpc3MiOiJvcmRlcnMiLCJhdWQiOiJpbnZlbnRvcnkudjEiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6"
    "MTc2NzIyNTkwMH0.AhMk-cm_SInC6ek-6CLP8pN7ciQFHuGb-i2MSGu3efU"
)
SECRET = bytes.fromhex(
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
)
KEY = ("orders-1", SECRET)
IAT = 1767225600
EXP = IAT + 300
HEADER = b'{"alg":"HS256","typ":"neolith+jwt","kid":"orders-1"}'


def _claims(**members: object) -> bytes:
    body: dict[str, object] = {
        "iss": "orders",
        "aud": "inventory.v1",
        "iat": IAT,
        "exp": EXP,
    }
    body.update(members)
    kept = {k: v for k, v in body.items() if v is not None}
    return json.dumps(kept, separators=(",", ":")).encode()


def _token(header: bytes = HEADER, claims: bytes | None = None) -> str:
    """A token over exactly these bytes, signed under the example secret."""
    signing = f"{b64url(header)}.{b64url(claims or _claims())}"
    signature = hmac.new(SECRET, signing.encode("ascii"), hashlib.sha256).digest()
    return f"Bearer {signing}.{b64url(signature)}"


def _problem(authorization: str, now: int = IAT) -> str | None:
    return token_problem(authorization, "orders", "inventory.v1", key=KEY, now=now)


def _flip_last(part: str) -> str:
    """``part`` with its last character's lowest bit flipped."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return part[:-1] + alphabet[alphabet.index(part[-1]) ^ 1]


# The token check.


@pytest.mark.parametrize("now", [IAT, IAT - 60, EXP + 59], ids=["iat", "-60", "+59"])
def test_spec_03_s_example_token_is_valid(now: int) -> None:
    assert _token() == EXAMPLE
    assert _problem(EXAMPLE, now) is None
    assert _problem("bearer " + EXAMPLE.removeprefix("Bearer "), now) is None


def _parts() -> list[str]:
    return EXAMPLE.removeprefix("Bearer ").split(".")


@pytest.mark.parametrize(
    ("authorization", "now", "reason"),
    [
        ("Basic " + EXAMPLE.removeprefix("Bearer "), IAT, "the scheme is"),
        ("Bearer " + ".".join(_parts()[:2]), IAT, "has 2 parts"),
        ("Bearer " + ".".join([*_parts(), "x"]), IAT, "has 4 parts"),
        ("Bearer " + ".".join([_parts()[0] + "=", *_parts()[1:]]), IAT, "base64url"),
        (
            "Bearer " + ".".join([*_parts()[:2], "A+" + _parts()[2][2:]]),
            IAT,
            "base64url",
        ),
        (
            "Bearer " + ".".join([*_parts()[:2], _flip_last(_parts()[2])]),
            IAT,
            "base64url",
        ),
        (
            _token(b'{"alg":"HS256","typ":"neolith+jwt","kid":"orders-1","x":1}'),
            IAT,
            "the header",
        ),
        (_token(b'{"alg":"HS256","typ":"JWT","kid":"orders-1"}'), IAT, "the header"),
        (
            _token(b'{"alg":"HS512","typ":"neolith+jwt","kid":"orders-1"}'),
            IAT,
            "the header",
        ),
        (
            _token(b'{"alg":"HS256","typ":"neolith+jwt","kid":"orders-2"}'),
            IAT,
            "the header",
        ),
        (
            _token(
                b'{"alg":"HS256","alg":"HS256","typ":"neolith+jwt","kid":"orders-1"}'
            ),
            IAT,
            "I-JSON",
        ),
        (_token(b'["HS256"]'), IAT, "not a JSON object"),
        (_token(claims=b"[1]"), IAT, "not a JSON object"),
        (_token(claims=_claims(sub="u")), IAT, "the claims are"),
        (_token(claims=_claims(exp=None)), IAT, "the claims are"),
        (_token(claims=_claims(iss="shop")), IAT, "iss is"),
        (_token(claims=_claims(aud="inventory.v2")), IAT, "aud is"),
        (_token(claims=_claims(iat=1.5)), IAT, "not integers"),
        (_token(claims=_claims(iat=True)), IAT, "not integers"),
        (_token(claims=_claims(exp=float(EXP))), IAT, "not integers"),
        (_token(claims=_claims(exp=IAT)), IAT, "is not after iat"),
        (_token(claims=_claims(exp=IAT + 301)), IAT, "more than 300 s"),
        (EXAMPLE, IAT - 61, "more than 60 s after now"),
        (EXAMPLE, EXP + 60, "60 s or more before now"),
        ("Bearer " + ".".join([*_parts()[:2], b64url(bytes(32))]), IAT, "signature"),
    ],
    ids=[
        "scheme",
        "two-parts",
        "four-parts",
        "padded",
        "outside-the-alphabet",
        "unused-bits",
        "a-fourth-header-member",
        "typ",
        "alg",
        "kid",
        "a-header-member-twice",
        "header-an-array",
        "claims-an-array",
        "a-fifth-claim",
        "no-exp",
        "iss",
        "aud",
        "iat-a-fraction",
        "iat-true",
        "exp-a-float",
        "exp-at-iat",
        "lifetime-301-s",
        "issued-61-s-ahead",
        "expired-60-s-ago",
        "signature",
    ],
)
def test_each_rule_of_the_token_has_a_reason(
    authorization: str, now: int, reason: str
) -> None:
    problem = _problem(authorization, now)
    assert problem is not None
    assert reason in problem


def test_a_token_over_4096_bytes_is_refused_and_one_of_4096_is_read() -> None:
    # A long key id, and the claims' spacing, make a token of each size; the
    # rest of it is valid. (A base64url part's length is never 1 modulo 4,
    # so the key id alone misses every fourth size.)
    sized: dict[int, tuple[str, tuple[str, bytes]]] = {}
    spaced = json.dumps(json.loads(_claims())).encode()
    for length in range(2900, 3100):
        kid = "k" * length
        members = {"alg": "HS256", "typ": "neolith+jwt", "kid": kid}
        for claims in (_claims(), spaced):
            token = _token(json.dumps(members).encode(), claims)
            size = len(token.removeprefix("Bearer "))
            sized.setdefault(size, (token, (kid, SECRET)))
    token, key = sized[4096]
    assert token_problem(token, "orders", "inventory.v1", key=key, now=IAT) is None
    token, key = sized[4097]
    problem = token_problem(token, "orders", "inventory.v1", key=key, now=IAT)
    assert problem == "the token is over 4096 bytes"


# The readers.


@pytest.mark.parametrize(
    ("text", "data"),
    [("", b""), ("AA", b"\x00"), ("AP_-", b"\x00\xff\xfe"), ("_w", b"\xff")],
)
def test_base64url_in_its_one_spelling_is_read(text: str, data: bytes) -> None:
    assert unb64url(text) == data
    assert b64url(data) == text


@pytest.mark.parametrize(
    "text",
    ["AA==", "AA=", "A", "AP/+", "AP_-\n", "AB", "_x", " AA"],
    ids=[
        "padded",
        "half-padded",
        "one-character",
        "base64-alphabet",
        "newline",
        "unused-bits",
        "unused-bits-2",
        "space",
    ],
)
def test_base64url_in_any_other_spelling_is_none(text: str) -> None:
    assert unb64url(text) is None


def test_the_principal_header_is_the_sent_form_of_spec_03_s_example() -> None:
    data = unb64url(PRINCIPAL_HEADER)
    assert data == (
        b'{"claims":{"roles":["clerk"]},"issuer":"https://id.example.com",'
        b'"subject":"u-81f3"}'
    )


def test_json_numbers_are_exact_and_never_booleans() -> None:
    assert read_json(b"[1, 1.0, 1e400, true]") == [
        1,
        Decimal("1.0"),
        Decimal("1e400"),
        True,
    ]
    assert type(read_json(b"1")) is int
    assert read_json(b"true") is True
    assert read_json(b'{"a": {"b": 1}}') == {"a": {"b": 1}}


@pytest.mark.parametrize(
    "text",
    [b'{"a":{"b":1,"b":2}}', b"-Infinity", b'"\xff"'],
    ids=["nested-member-twice", "-Infinity", "not-UTF-8"],
)
def test_text_that_is_not_i_json_is_refused_at_any_depth(text: bytes) -> None:
    with pytest.raises(NotJSON):
        read_json(text)


# A response, as the suite's caller reads it.


def _answer(
    *fields: tuple[str, str], body: bytes = b'{"kind":"x","message":"m"}'
) -> Answer:
    return Answer(400, fields, body)


GOOD = (("Content-Type", "application/json"), ("Content-Length", "26"))


def test_an_error_body_in_its_form_is_read() -> None:
    assert _answer(*GOOD).error() == {"kind": "x", "message": "m"}
    assert _answer(*GOOD).field("content-type") == "application/json"
    assert _answer(*GOOD).field("Allow") is None


@pytest.mark.parametrize(
    "answer",
    [
        _answer(("Content-Type", "text/plain"), GOOD[1]),
        _answer(GOOD[1]),
        _answer(GOOD[0], ("Content-Length", "25")),
        _answer(GOOD[0]),
        _answer(*GOOD, GOOD[0]),
        _answer(GOOD[0], ("Content-Length", "6"), body=b'["x"] '),
        _answer(GOOD[0], ("Content-Length", "11"), body=b'{"kind":7} '),
        _answer(GOOD[0], ("Content-Length", "12"), body=b'{"message":1'),
    ],
    ids=[
        "another-type",
        "no-type",
        "length-off",
        "no-length",
        "type-twice",
        "an-array",
        "kind-not-a-string",
        "not-json",
    ],
)
def test_a_response_outside_the_error_form_fails_the_case(answer: Answer) -> None:
    with pytest.raises((AssertionError, NotJSON)):
        answer.error()


# A request, as the scripted provider reads it.


@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    ends = socket.socketpair()
    for end in ends:
        end.settimeout(10)
    yield ends
    for end in ends:
        end.close()


def _write(sock: socket.socket, *chunks: bytes) -> threading.Thread:
    def run() -> None:
        for chunk in chunks:
            sock.sendall(chunk)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_a_request_is_read_by_its_content_length_across_reads(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    caller, provider = pair
    writer = _write(
        caller,
        b"POST /a/v1/b HTTP/1.1\r\nHost: h\r\nContent-",
        b'Length: \t7 \r\nX-Two:  a b  \r\n\r\n{"a"',
        b":1}",
    )
    received = _read(provider)
    writer.join(10)
    assert received == Received(
        "POST /a/v1/b HTTP/1.1",
        (("Host", "h"), ("Content-Length", "7"), ("X-Two", "a b")),
        b'{"a":1}',
    )
    assert received is not None
    assert received.field("x-two") == "a b"
    assert received.field("Authorization") is None


@pytest.mark.parametrize(
    "length", ["", "²", "1, 1"], ids=["empty", "a-latin-1-digit", "two"]
)
def test_a_content_length_outside_digits_reads_no_body(
    pair: tuple[socket.socket, socket.socket], length: str
) -> None:
    caller, provider = pair
    caller.sendall(
        f"POST / HTTP/1.1\r\nContent-Length: {length}\r\n\r\n".encode("latin-1")
    )
    received = _read(provider)
    assert received is not None
    assert received.body == b""


def test_a_closed_connection_is_no_request(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    caller, provider = pair
    caller.sendall(b"POST / HTTP/1.1\r\n")
    caller.shutdown(socket.SHUT_WR)
    assert _read(provider) is None


def test_a_field_given_twice_fails_the_case() -> None:
    received = Received("POST / HTTP/1.1", (("A", "1"), ("a", "2")), b"")
    with pytest.raises(AssertionError):
        received.field("A")
