"""The HTTP transport's client, against a provider played on a socket (ADR 0028).

Each test plays the provider byte by byte on the far end of a socket pair, or
of a connection to a listening socket on this machine; nothing reaches the
network. The provider's side waits on events, never on ``sleep``.
"""

import contextlib
import copy
import gzip
import os
import pickle
import select
import socket
import ssl
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from types import TracebackType

import pytest
from django.test import override_settings
from django.views.debug import ExceptionReporter

from neolith import _http
from neolith._connector import _LATE, _NOT_SENT
from neolith._context import now
from neolith._http import ClientError, Origin, Pool, Pools, Response, exchange
from tests.http_peer import (
    BODY,
    FIELDS,
    OK,
    TARGET,
    TOKEN,
    Provider,
    Script,
    answer,
    call,
    fails,
    hold,
    read_request,
)


@pytest.fixture
def provide() -> Iterator[Callable[..., tuple[Pool, Provider]]]:
    made: list[tuple[Pool, Provider]] = []

    def make(
        *scripts: Script, url: str = "http://inventory.test"
    ) -> tuple[Pool, Provider]:
        provider = Provider(scripts)
        origin = Origin.parse(url)
        context = (
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if origin.scheme == "https"
            else None
        )
        pool = Pool(origin, context, opener=provider.open)
        made.append((pool, provider))
        return pool, provider

    yield make
    for pool, provider in made:
        pool.close()
        provider.finish()


# The request the client writes (HTTP-3)


@pytest.mark.spec("HTTP-3")
def test_the_request_is_exactly_the_admitted_one(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    seen: list[bytes] = []

    def script(sock: socket.socket) -> None:
        seen.append(read_request(sock))
        sock.sendall(OK)

    pool, _ = provide(script, url="http://inventory.test:8080")
    assert call(pool) == Response(200, b"{}")
    assert seen == [
        b"POST /inventory/v1/reserve HTTP/1.1\r\n"
        b"Host: inventory.test:8080\r\n"
        b"Accept-Encoding: identity\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 13\r\n"
        b"Authorization: " + TOKEN.encode() + b"\r\n"
        b"Neolith-Timeout: 250\r\n"
        b'Idempotency-Key: "k-1"\r\n'
        b"traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\r\n"
        b"\r\n" + BODY
    ]


def _with(name: str, value: str) -> list[tuple[str, str]]:
    return [(n, value if n == name else v) for n, v in FIELDS]


REFUSED_FIELDS = {
    "a CR LF in a value": _with("Authorization", TOKEN + "\r\nX-A: b"),
    "no Authorization": list(FIELDS[1:]),
    "no Idempotency-Key": [FIELDS[0], FIELDS[1], FIELDS[3]],
    "no traceparent": list(FIELDS[:3]),
    "out of order": [FIELDS[0], FIELDS[2], FIELDS[1]],
    "twice": [*FIELDS, FIELDS[3]],
    "a name not admitted": [*FIELDS, ("User-Agent", "neolith")],
    "a timeout of 0050": _with("Neolith-Timeout", "0050"),
    "a timeout of 11 digits": _with("Neolith-Timeout", "10000000000"),
    "a key of 256 characters": _with("Idempotency-Key", '"' + "k" * 256 + '"'),
    "an empty key": _with("Idempotency-Key", '""'),
    "a key with a lone backslash": _with("Idempotency-Key", '"a\\b"'),
    "an all-zero trace": _with(
        "traceparent", "00-" + "0" * 32 + "-00f067aa0ba902b7-01"
    ),
    "an uppercase trace": _with(
        "traceparent", "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01"
    ),
    "a tracestate of 513": [
        *FIELDS,
        ("tracestate", "a=" + "b" * 255 + ",c=" + "d" * 253),
    ],
    "a tracestate of 33 members": [*FIELDS, ("tracestate", ",".join(["a=1"] * 33))],
    "a tracestate of 33 empty members": [*FIELDS, ("tracestate", "," * 32)],
    "a tracestate key in uppercase": [*FIELDS, ("tracestate", "A=1")],
    "a tracestate member with no value": [*FIELDS, ("tracestate", "a=")],
    "a tracestate with a control character": [*FIELDS, ("tracestate", "a=1\x01")],
    "a token of 4097": _with("Authorization", "Bearer a.b." + "c" * 4093),
    "a principal with padding": [FIELDS[0], ("Neolith-Principal", "eyJ9=")],
    "a principal of 4097": [FIELDS[0], ("Neolith-Principal", "e" * 4097), *FIELDS[1:]],
    "an empty principal": [FIELDS[0], ("Neolith-Principal", ""), *FIELDS[1:]],
}
ADMITTED_FIELDS = {
    "a key of 255 characters with escapes": _with(
        "Idempotency-Key", '"' + "k" * 253 + '\\"\\\\"'
    ),
    "a tracestate of 512": [
        *FIELDS,
        ("tracestate", "a=" + "b" * 255 + ",c=" + "d" * 252),
    ],
    "a multi-tenant tracestate key": [*FIELDS, ("tracestate", "t0@sys=v, b=w")],
    "a tracestate with an empty member": [*FIELDS, ("tracestate", "a=1,,b=2")],
    "a tracestate with a blank member": [*FIELDS, ("tracestate", "a=1, ,b=2")],
    "a tracestate ending in a comma": [*FIELDS, ("tracestate", "a=1,")],
    "a tracestate starting with a comma": [*FIELDS, ("tracestate", ",a=1")],
    "a tracestate with blanks at its end": [*FIELDS, ("tracestate", "a=1 \t")],
    "a tracestate of 32 members": [*FIELDS, ("tracestate", ",".join(["a=1"] * 32))],
    "a tracestate of 32 empty members": [*FIELDS, ("tracestate", "," * 31)],
    "a token of 4096": _with("Authorization", "Bearer a.b." + "c" * 4092),
    "a principal": [FIELDS[0], ("Neolith-Principal", "eyJ9"), *FIELDS[1:]],
    "a principal of 4096": [FIELDS[0], ("Neolith-Principal", "e" * 4096), *FIELDS[1:]],
    "only the fields every call carries": [FIELDS[0], FIELDS[2], FIELDS[3]],
}


@pytest.mark.parametrize("given", REFUSED_FIELDS.values(), ids=REFUSED_FIELDS.keys())
def test_a_field_outside_its_grammar_is_a_mistake_and_nothing_is_written(
    provide: Callable[..., tuple[Pool, Provider]], given: list[tuple[str, str]]
) -> None:
    seen: list[bytes] = []
    pool, provider = provide(lambda sock: seen.append(read_request(sock)))
    with pytest.raises(ValueError, match="field") as raised:
        call(pool, fields=lambda: given)
    assert TOKEN not in str(raised.value)
    assert pool.take() is None
    provider.finish()
    assert seen == [b""]


@pytest.mark.parametrize("given", ADMITTED_FIELDS.values(), ids=ADMITTED_FIELDS.keys())
def test_the_nearest_admitted_fields_are_written(
    provide: Callable[..., tuple[Pool, Provider]], given: list[tuple[str, str]]
) -> None:
    pool, _ = provide(answer(OK))
    assert call(pool, fields=lambda: given).status == 200


@pytest.mark.parametrize(
    "target",
    [
        "/inventory/v1/reserve?x=1",
        "/inventory/v01/reserve",
        "/Inventory/v1/reserve",
        "/inventory/v1/reserve HTTP/1.1\r\nX: y",
        "inventory/v1/reserve",
    ],
)
def test_a_target_outside_the_action_path_grammar_is_a_mistake(
    provide: Callable[..., tuple[Pool, Provider]], target: str
) -> None:
    pool, provider = provide()
    with pytest.raises(ValueError, match="target"):
        exchange(pool, target, lambda: FIELDS, BODY, None)
    assert provider.opened == 0


# The response the client accepts (HTTP-5, HTTP-6)

REFUSED = {
    "+200": b"HTTP/1.1 +200 OK\r\nContent-Length: 0\r\n\r\n",
    "2_00": b"HTTP/1.1 2_00 OK\r\nContent-Length: 0\r\n\r\n",
    "0200": b"HTTP/1.1 0200 OK\r\nContent-Length: 0\r\n\r\n",
    "HTTP/1.7": b"HTTP/1.7 200 OK\r\nContent-Length: 0\r\n\r\n",
    "HTTP/2": b"HTTP/2 200 OK\r\nContent-Length: 0\r\n\r\n",
    "no space after the status": b"HTTP/1.1 200\r\nContent-Length: 0\r\n\r\n",
    "lowercase version": b"http/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
    "a bare LF ending the status line": b"HTTP/1.1 200 OK\nContent-Length: 0\r\n\r\n",
    "Content-Length +10": b"HTTP/1.1 200 OK\r\nContent-Length: +10\r\n\r\n0123456789",
    "Content-Length 1_0": b"HTTP/1.1 200 OK\r\nContent-Length: 1_0\r\n\r\n0123456789",
    "two Content-Length": (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
    ),
    "Content-Length with Transfer-Encoding": (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"2\r\n{}\r\n0\r\n\r\n"
    ),
    "Transfer-Encoding: gzip, chunked": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n"
        b"2\r\n{}\r\n0\r\n\r\n"
    ),
    "Transfer-Encoding in HTTP/1.0": (
        b"HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
    ),
    "Content-Length of 19 digits": (
        b"HTTP/1.1 200 OK\r\nContent-Length: 0000000000000000002\r\n\r\n{}"
    ),
    "Content-Length of 4301 digits": (
        b"HTTP/1.1 200 OK\r\nContent-Length: " + b"1" * 4301 + b"\r\n\r\n{}"
    ),
    "a 101": b"HTTP/1.1 101 Switching Protocols\r\n\r\n" + OK,
    "six interim responses": b"HTTP/1.1 100 Continue\r\n\r\n" * 6 + OK,
    "101 field lines": (
        b"HTTP/1.1 200 OK\r\n" + b"X-A: b\r\n" * 100 + b"Content-Length: 2\r\n\r\n{}"
    ),
    "a field line with a bare CR": (
        b"HTTP/1.1 200 OK\r\nX-A: a\rContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
    ),
    "a space before the colon": b"HTTP/1.1 200 OK\r\nContent-Length : 2\r\n\r\n{}",
    "an obs-fold": b"HTTP/1.1 200 OK\r\nX-A: a\r\n b\r\nContent-Length: 2\r\n\r\n{}",
    "a field line with no colon": (
        b"HTTP/1.1 200 OK\r\nX-A\r\nContent-Length: 2\r\n\r\n{}"
    ),
    "a field line with a control character": (
        b"HTTP/1.1 200 OK\r\nX-A: a\x00b\r\nContent-Length: 2\r\n\r\n{}"
    ),
    "chunk size 0x5": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0x5\r\n",
    "chunk size +5": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n+5\r\n",
    "chunk size 0_5": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0_5\r\n",
    "chunk size ' 5 '": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n 5 \r\n",
    "chunk size -0": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-0\r\n\r\n",
    "chunk size of 17 digits": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"0" * 16
        + b"2\r\n{}\r\n0\r\n\r\n"
    ),
    "a chunk extension with a bare CR": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"2;a\rb\r\n{}\r\n0\r\n\r\n"
    ),
    "a chunk-size line over the limit": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;"
        + b"a" * 65534
        + b"\r\n{}\r\n0\r\n\r\n"
    ),
    "chunk data not followed by CRLF": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}x\r\n0\r\n\r\n"
    ),
    "a trailer field with a space before its colon": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n"
        b"X-A : b\r\n\r\n"
    ),
}

ADMITTED = {
    "an empty reason": (b"HTTP/1.1 200 \r\nContent-Length: 2\r\n\r\n{}", b"{}"),
    "a reason with a tab and obs-text": (
        b"HTTP/1.1 200 \tO\xffK\r\nContent-Length: 2\r\n\r\n{}",
        b"{}",
    ),
    "Content-Length of 18 digits": (
        b"HTTP/1.1 200 OK\r\nContent-Length: 000000000000000002\r\n\r\n{}",
        b"{}",
    ),
    "100 field lines": (
        b"HTTP/1.1 200 OK\r\n" + b"X-A: b\r\n" * 99 + b"Content-Length: 2\r\n\r\n{}",
        b"{}",
    ),
    "OWS around a value": (b"HTTP/1.1 200 OK\r\nContent-Length: \t2 \r\n\r\n{}", b"{}"),
    "an empty field value": (
        b"HTTP/1.1 200 OK\r\nX-A:\r\nContent-Length: 2\r\n\r\n{}",
        b"{}",
    ),
    "chunks with extensions": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: Chunked\r\n\r\n"
        b'1\t;a\r\n{\r\n1;a="b\tc" ; d = e\r\n}\r\n000\r\nX-T: t\r\n\r\n',
        b"{}",
    ),
    "a chunk size of 16 digits": (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"0" * 15
        + b"2\r\n{}\r\n0\r\n\r\n",
        b"{}",
    ),
    "five interim responses": (
        b"HTTP/1.1 100 Continue\r\n\r\n" * 4
        + b"HTTP/1.1 103 Early Hints\r\nLink: </a>\r\n\r\n"
        + OK,
        b"{}",
    ),
    "a body to the end of the connection": (b"HTTP/1.1 200 OK\r\n\r\n{}", b"{}"),
    "HTTP/1.0": (b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}", b"{}"),
}


@pytest.mark.spec("HTTP-6")
@pytest.mark.parametrize("response", REFUSED.values(), ids=REFUSED.keys())
def test_a_response_outside_the_table_is_unavailable_and_closed(
    provide: Callable[..., tuple[Pool, Provider]], response: bytes
) -> None:
    pool, _ = provide(answer(response))
    error = fails(pool)
    assert (error.kind, error.message, error.phase) == (
        "unavailable",
        "the provider at http://inventory.test did not answer in HTTP/1.1",
        "reading",
    )
    assert pool.take() is None


@pytest.mark.spec("HTTP-5", "HTTP-6")
@pytest.mark.parametrize(("response", "body"), ADMITTED.values(), ids=ADMITTED.keys())
def test_the_nearest_admitted_responses_are_read(
    provide: Callable[..., tuple[Pool, Provider]], response: bytes, body: bytes
) -> None:
    pool, _ = provide(answer(response))
    assert call(pool) == Response(200, body)


@pytest.mark.spec("HTTP-6")
def test_a_chunked_body_cut_short_is_unavailable(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(
        answer(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{}")
    )
    error = fails(pool)
    assert (
        error.message
        == "the provider at http://inventory.test did not answer in HTTP/1.1"
    )
    assert "ended early" in error.cause


@pytest.mark.spec("HTTP-6")
def test_a_body_shorter_than_its_length_is_unavailable(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(answer(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n{}"))
    assert fails(pool).message.endswith("did not answer in HTTP/1.1")


@pytest.mark.spec("HTTP-6")
def test_the_cause_of_a_long_status_line_is_cut_for_the_log(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(answer(b"HTTP/1.1 200 " + b"x" * 65522 + b"\r\n\r\n"))
    error = fails(pool)
    assert error.cause.startswith("_Outside: ")
    assert len(error.cause) == len("_Outside: ") + _http.LOG_LIMIT
    pool2, _ = provide(
        answer(b"HTTP/1.1 200 " + b"x" * 65521 + b"\r\nContent-Length: 0\r\n\r\n")
    )
    assert call(pool2).status == 200


# Interim responses on a reused connection (HTTP-5)


@pytest.mark.spec("HTTP-5")
def test_a_103_then_a_late_200_is_read_and_the_next_call_gets_its_own_reply(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pause = threading.Event()

    def script(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(b"HTTP/1.1 103 Early Hints\r\nLink: </a>; rel=preload\r\n\r\n")
        pause.wait(0.2)
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nreply-one")
        read_request(sock)
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nreply-two")

    pool, provider = provide(script)
    assert call(pool) == Response(200, b"reply-one")
    assert call(pool) == Response(200, b"reply-two")
    assert provider.opened == 1


@pytest.mark.spec("HTTP-5")
def test_a_100_before_the_final_response_is_read_past(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(answer(b"HTTP/1.1 100 Continue\r\n\r\n" + OK))
    assert call(pool) == Response(200, b"{}")


# Redirects and content codings (HTTP-4)


@pytest.mark.spec("HTTP-4")
def test_a_redirect_is_handed_on_and_never_followed(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(
        answer(
            b"HTTP/1.1 302 Found\r\nLocation: http://elsewhere.test/\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
    )
    assert call(pool) == Response(302, b"")
    assert provider.opened == 1


@pytest.mark.spec("HTTP-4")
def test_a_content_coding_is_never_undone(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    coded = gzip.compress(b"{}", mtime=0)
    pool, _ = provide(
        answer(
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
            + str(len(coded)).encode()
            + b"\r\n\r\n"
            + coded
        )
    )
    assert call(pool) == Response(200, coded)


# Persistence


@pytest.mark.parametrize(
    ("response", "kept"),
    [
        (OK, True),
        (
            b"HTTP/1.1 200 OK\r\nConnection: x-unclosed\r\nContent-Length: 2\r\n\r\n{}",
            True,
        ),
        (
            b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nConnection: close\r\n"
            b"Content-Length: 2\r\n\r\n{}",
            False,
        ),
        (
            b"HTTP/1.1 200 OK\r\nConnection: Upgrade, CLOSE\r\n"
            b"Content-Length: 2\r\n\r\n{}",
            False,
        ),
        (b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}", False),
        (
            b"HTTP/1.0 200 OK\r\nKeep-Alive: timeout=5\r\nContent-Length: 2\r\n\r\n{}",
            False,
        ),
        (
            b"HTTP/1.0 200 OK\r\nConnection: Keep-Alive\r\nContent-Length: 2\r\n\r\n{}",
            True,
        ),
        (b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n", True),
        (b"HTTP/1.1 204 No Content\r\n\r\n", True),
    ],
    ids=[
        "1.1",
        "x-unclosed",
        "close after keep-alive",
        "close in a list",
        "1.0",
        "1.0 Keep-Alive field",
        "1.0 keep-alive",
        "204 length 0",
        "204",
    ],
)
def test_a_connection_is_kept_only_as_its_connection_options_say(
    provide: Callable[..., tuple[Pool, Provider]], response: bytes, kept: bool
) -> None:
    pool, provider = provide(answer(response, OK), answer(OK))
    call(pool)
    assert call(pool) == Response(200, b"{}")
    assert provider.opened == (1 if kept else 2)


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/1.1 204 No Content\r\nContent-Length: 5\r\n\r\n",
        b"HTTP/1.1 304 Not Modified\r\nTransfer-Encoding: chunked\r\n\r\n",
    ],
)
def test_a_204_or_304_has_no_body_and_with_framing_is_not_kept(
    provide: Callable[..., tuple[Pool, Provider]], head: bytes
) -> None:
    pool, provider = provide(answer(head + b"hello"), answer(OK))
    assert call(pool).body == b""
    assert call(pool) == Response(200, b"{}")
    assert provider.opened == 2


def test_bytes_past_the_response_keep_the_connection_out_of_the_pool(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(
        answer(OK + b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nstale", OK),
        answer(OK),
    )
    assert call(pool) == Response(200, b"{}")
    assert call(pool) == Response(200, b"{}")
    assert provider.opened == 2


# The deadline in every phase (TRN-64)


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


@pytest.mark.spec("TRN-64")
def test_the_deadline_bounds_name_resolution(held_resolver: list[str]) -> None:
    pool = Pool(Origin.parse("http://inventory.test"))
    started = now()
    error = fails(pool, deadline=started + 0.2)
    assert (error.kind, error.message, error.phase) == (
        "timeout",
        _NOT_SENT,
        "before writing",
    )
    assert now() - started < 1
    assert held_resolver == ["inventory.test"]


@pytest.mark.spec("TRN-64")
def test_the_connect_cap_bounds_a_call_with_no_deadline(
    held_resolver: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http, "CONNECT_CAP", 0.2)
    error = fails(Pool(Origin.parse("http://inventory.test")))
    assert (error.kind, error.message) == (
        "unavailable",
        "the provider at http://inventory.test could not be reached: "
        "no connection within 0.2 seconds",
    )


@pytest.mark.spec("TRN-64")
def test_the_earlier_of_the_cap_and_the_deadline_decides_the_kind(
    held_resolver: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http, "CONNECT_CAP", 0.2)
    error = fails(Pool(Origin.parse("http://inventory.test")), deadline=now() + 5)
    assert error.kind == "unavailable"


@pytest.fixture
def unaccepting() -> Iterator[tuple[str, int]]:
    """A listening socket whose queue is full, so a connect never completes."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    address = listener.getsockname()
    filler = socket.create_connection(address)
    yield address
    filler.close()
    listener.close()


@pytest.mark.spec("TRN-64")
def test_the_deadline_bounds_connecting(unaccepting: tuple[str, int]) -> None:
    pool = Pool(Origin.parse(f"http://127.0.0.1:{unaccepting[1]}"))
    started = now()
    error = fails(pool, deadline=started + 0.3)
    assert (error.kind, error.message) == ("timeout", _NOT_SENT)
    assert now() - started < 1


@pytest.mark.spec("TRN-64")
def test_the_deadline_bounds_the_tls_handshake(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(hold, url="https://inventory.test")
    started = now()
    error = fails(pool, deadline=started + 0.3)
    assert (error.kind, error.message, error.phase) == (
        "timeout",
        _NOT_SENT,
        "before writing",
    )
    assert now() - started < 1


@pytest.mark.spec("TRN-64")
def test_the_deadline_bounds_writing(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(hold)
    started = now()
    error = fails(pool, deadline=started + 0.3, body=b"[" + b"0," * 2_000_000 + b"0]")
    assert (error.kind, error.message, error.phase) == ("timeout", _LATE, "writing")
    assert now() - started < 1


def drip(head: bytes, drops: bytes) -> Script:
    def script(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(head)
        wait = threading.Event()
        for byte in drops:
            if wait.wait(0.05):
                return
            sock.sendall(bytes([byte]))

    return script


@pytest.mark.spec("TRN-64")
@pytest.mark.parametrize(
    ("head", "drops"),
    [
        (
            b"HTTP/1.1 200 OK\r\n",
            b"X-Drip: " + b"a" * 40 + b"\r\nContent-Length: 0\r\n\r\n",
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n", b"a" * 40),
    ],
    ids=["header", "body"],
)
def test_the_deadline_cuts_a_drip(
    provide: Callable[..., tuple[Pool, Provider]], head: bytes, drops: bytes
) -> None:
    pool, _ = provide(drip(head, drops))
    started = now()
    error = fails(pool, deadline=started + 0.3)
    assert (error.kind, error.message, error.phase) == ("timeout", _LATE, "reading")
    assert now() - started < 1


class Clock:
    """This host's clock, held still by the test."""

    def __init__(self) -> None:
        self.at = now()

    def __call__(self) -> float:
        return self.at


@pytest.mark.spec("TRN-64")
def test_a_deadline_reached_exactly_opens_nothing(
    provide: Callable[..., tuple[Pool, Provider]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(_http, "_now", clock)
    pool, provider = provide()
    error = fails(pool, deadline=clock.at)
    assert (error.kind, error.message) == ("timeout", _NOT_SENT)
    assert provider.opened == 0


@pytest.mark.spec("TRN-64")
def test_a_deadline_reached_exactly_on_a_ready_connection_touches_no_socket(
    provide: Callable[..., tuple[Pool, Provider]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bytes] = []

    def script(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(OK)
        seen.append(read_request(sock))

    pool, provider = provide(script)
    call(pool)
    clock = Clock()
    monkeypatch.setattr(_http, "_now", clock)
    error = fails(pool, deadline=clock.at)
    assert (error.kind, error.message, error.phase) == ("timeout", _LATE, "writing")
    provider.finish()
    assert seen == [b""]


def test_a_budget_under_1_ms_sends_nothing_and_keeps_the_connection(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(answer(OK))
    error = fails(pool, fields=lambda: None)
    assert (error.kind, error.message, error.phase) == (
        "timeout",
        _NOT_SENT,
        "before writing",
    )
    assert call(pool) == Response(200, b"{}")
    assert provider.opened == 1


# Never sent twice (TRN-65)


@pytest.mark.spec("TRN-65")
def test_a_connection_that_ends_after_the_request_is_not_sent_again(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(lambda sock: read_request(sock) and None)
    error = fails(pool)
    assert (error.kind, error.phase) == ("unavailable", "reading")
    assert error.message == (
        "the connection to the provider at http://inventory.test failed before "
        "the response was complete"
    )
    assert provider.opened == 1


@pytest.mark.spec("TRN-65")
def test_a_first_send_refused_on_a_reused_connection_moves_to_one_new_connection(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    ends: list[socket.socket] = []

    def first(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(OK)
        ends.append(sock)
        provider.release.wait(10)

    pool, provider = provide(first, answer(OK))
    call(pool)
    readings: list[int] = []

    def fields() -> Sequence[tuple[str, str]]:
        readings.append(provider.opened)
        if len(readings) == 1:
            ends[0].shutdown(socket.SHUT_RDWR)
            ends[0].close()
        return FIELDS

    assert call(pool, fields=fields) == Response(200, b"{}")
    assert readings == [1, 2]
    assert provider.opened == 2


@pytest.mark.spec("TRN-65")
def test_a_first_send_refused_on_a_new_connection_is_not_replaced(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(lambda sock: None)
    readings: list[int] = []

    def fields() -> Sequence[tuple[str, str]]:
        readings.append(1)
        provider.threads[0].join(5)
        return FIELDS

    error = fails(pool, fields=fields)
    assert (error.kind, error.phase) == ("unavailable", "writing")
    assert readings == [1]
    assert provider.opened == 1


@pytest.mark.spec("TRN-65")
def test_the_replacement_connection_is_the_only_one(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    ends: list[socket.socket] = []

    def first(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(OK)
        ends.append(sock)
        provider.release.wait(10)

    pool, provider = provide(first, lambda sock: None)
    call(pool)

    def fields() -> Sequence[tuple[str, str]]:
        if provider.opened == 1:
            ends[0].shutdown(socket.SHUT_RDWR)
            ends[0].close()
        else:
            provider.threads[1].join(5)
        return FIELDS

    error = fails(pool, fields=fields)
    assert (error.kind, error.phase) == ("unavailable", "writing")
    assert provider.opened == 2


@pytest.mark.spec("TRN-65")
def test_an_early_refusal_while_writing_is_read(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    refusal = (
        b'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Bearer realm="neolith"\r\n'
        b"Content-Length: 2\r\n\r\n{}"
    )

    def script(sock: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            data += sock.recv(65536)
        sock.sendall(refusal)

    pool, provider = provide(script, answer(OK))
    assert call(pool, body=b"[" + b"0," * 2_000_000 + b"0]") == Response(401, b"{}")
    assert pool._idle == []
    assert call(pool) == Response(200, b"{}")
    assert provider.opened == 2


@pytest.mark.spec("TRN-65")
def test_a_reset_while_writing_with_nothing_to_read_is_unavailable(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    def script(sock: socket.socket) -> None:
        sock.recv(65536)

    pool, provider = provide(script)
    error = fails(pool, body=b"[" + b"0," * 2_000_000 + b"0]")
    assert (error.kind, error.phase) == ("unavailable", "writing")
    assert error.message.endswith("failed before the response was complete")
    assert provider.opened == 1


# The pool


def test_the_newest_idle_connection_is_reused(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(answer(OK, OK))
    call(pool)
    call(pool)
    assert provider.opened == 1


def test_an_idle_connection_expires(
    provide: Callable[..., tuple[Pool, Provider]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(_http, "_now", clock)
    pool, provider = provide(answer(OK, OK), answer(OK))
    call(pool)
    clock.at += _http.IDLE_EXPIRY + 0.001
    call(pool)
    assert provider.opened == 2


def test_an_idle_connection_kept_for_exactly_the_expiry_is_reused(
    provide: Callable[..., tuple[Pool, Provider]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(_http, "_now", clock)
    pool, provider = provide(answer(OK, OK))
    call(pool)
    clock.at += _http.IDLE_EXPIRY
    call(pool)
    assert provider.opened == 1


def test_a_stale_pooled_connection_is_dropped_before_writing(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, provider = provide(answer(OK), answer(OK))
    call(pool)
    provider.threads[0].join(5)
    assert call(pool) == Response(200, b"{}")
    assert provider.opened == 2


def test_at_most_ten_idle_connections_are_kept_newest_first() -> None:
    pool = Pool(Origin.parse("http://inventory.test"))
    pairs = [socket.socketpair() for _ in range(_http.IDLE_LIMIT + 1)]
    connections = [_http._Connection(a) for a, _ in pairs]
    for connection in connections:
        pool.give(connection)
    assert [held for held, _ in pool._idle] == connections[1:]
    assert connections[0].sock.fileno() == -1
    assert all(connection.sock.fileno() != -1 for connection in connections[1:])
    assert pool.take() is connections[-1]
    pool.close()
    assert all(connection.sock.fileno() == -1 for connection in connections[1:-1])
    connections[-1].close()
    for _, b in pairs:
        b.close()


def test_a_closed_pool_closes_a_connection_given_back() -> None:
    pool = Pool(Origin.parse("http://inventory.test"))
    a, b = socket.socketpair()
    pool.close()
    pool.give(_http._Connection(a))
    assert a.fileno() == -1
    assert pool.take() is None
    b.close()


def test_a_pool_starts_empty_in_another_process() -> None:
    pool = Pool(Origin.parse("http://inventory.test"))
    a, b = socket.socketpair()
    pool.give(_http._Connection(a))
    pool._pid = -1
    assert pool.take() is None
    assert a.fileno() == -1
    b.close()


def test_one_pool_per_origin_and_one_tls_context_built_at_the_first_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[ssl.SSLContext] = []

    def make() -> ssl.SSLContext:
        built.append(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        return built[-1]

    monkeypatch.setattr(_http, "_make_context", make)
    pools = Pools()
    plain = pools.pool(Origin.parse("http://a.test"))
    assert built == []
    first = pools.pool(Origin.parse("https://a.test"))
    second = pools.pool(Origin.parse("https://b.test:8443"))
    assert pools.pool(Origin.parse("http://a.test")) is plain
    assert first.context is second.context is built[0]
    assert len(built) == 1
    pools.close()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://a.test",
        "http://a.test/",
        "http://A.test",
        "http://a.test:0",
        "http://a.test:65536",
        "http://user@a.test",
        "http://[::1",
        "http://[fe80::1%25eth0]",
        "http://a_b.test",
        "http://a.test:80\r\n",
        "http://[1.2.3.4]",
    ],
)
def test_an_origin_outside_the_url_grammar_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="url"):
        Origin.parse(url)


@pytest.mark.parametrize(
    ("url", "host", "port", "authority"),
    [
        ("http://a.test", "a.test", 80, "a.test"),
        ("https://a.test", "a.test", 443, "a.test"),
        ("https://a.test:8443", "a.test", 8443, "a.test:8443"),
        ("http://127.0.0.1:8000", "127.0.0.1", 8000, "127.0.0.1:8000"),
        ("http://[::1]:8000", "::1", 8000, "[::1]:8000"),
    ],
)
def test_an_origin_is_read_as_written(
    url: str, host: str, port: int, authority: str
) -> None:
    origin = Origin.parse(url)
    assert (origin.host, origin.port, origin.authority) == (host, port, authority)


# Resolution and connecting, on this machine only


@pytest.fixture
def server() -> Iterator[Callable[[str], tuple[socket.socket, list[bytes]]]]:
    made: list[tuple[socket.socket, threading.Thread]] = []

    def listen(host: str) -> tuple[socket.socket, list[bytes]]:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        listener = socket.socket(family)
        listener.bind((host, 0))
        listener.listen(8)
        seen: list[bytes] = []

        def serve() -> None:
            with contextlib.suppress(OSError):
                while True:
                    sock, _ = listener.accept()
                    with sock:
                        seen.append(read_request(sock))
                        sock.sendall(
                            b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                            b"Content-Length: 2\r\n\r\n{}"
                        )

        thread = threading.Thread(target=serve)
        thread.start()
        made.append((listener, thread))
        return listener, seen

    yield listen
    for listener, thread in made:
        with contextlib.suppress(OSError):
            listener.shutdown(socket.SHUT_RDWR)
        listener.close()
        thread.join(5)


def test_an_address_is_never_resolved(
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_http, "_getaddrinfo", lambda *a: pytest.fail("resolved"))
    listener, seen = server("127.0.0.1")
    pool = Pool(Origin.parse(f"http://127.0.0.1:{listener.getsockname()[1]}"))
    assert call(pool) == Response(200, b"{}")
    assert len(seen) == 1


def test_localhost_is_resolved_and_reached(
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    listener, _ = server("127.0.0.1")
    pool = Pool(Origin.parse(f"http://localhost:{listener.getsockname()[1]}"))
    assert call(pool, deadline=now() + 5) == Response(200, b"{}")


def test_ipv6_loopback_is_reached(
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    try:
        listener, _ = server("::1")
    except OSError:
        pytest.skip("this machine has no IPv6 loopback")
    pool = Pool(Origin.parse(f"http://[::1]:{listener.getsockname()[1]}"))
    assert call(pool) == Response(200, b"{}")


def test_a_connection_has_nodelay_and_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    pool = Pool(Origin.parse(f"http://127.0.0.1:{listener.getsockname()[1]}"))
    sock = pool._opener(now() + 5)
    accepted, _ = listener.accept()
    assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 60
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 6
    for item in (sock, accepted, listener):
        item.close()


def test_a_refused_port_is_unavailable() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    error = fails(Pool(Origin.parse(f"http://127.0.0.1:{port}")))
    assert (error.kind, error.message) == (
        "unavailable",
        f"the provider at http://127.0.0.1:{port} could not be reached: "
        "no address accepted a connection",
    )
    assert error.cause.startswith("ConnectionRefusedError")


def test_a_name_that_does_not_resolve_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(*args: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    error = fails(Pool(Origin.parse("http://inventory.test")))
    assert error.message == (
        "the provider at http://inventory.test could not be reached: "
        "its name did not resolve"
    )


def _two_addresses(
    monkeypatch: pytest.MonkeyPatch, first: tuple[str, int], second: tuple[str, int]
) -> None:
    def resolve(*args: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", first),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", second),
        ]

    monkeypatch.setattr(_http, "_getaddrinfo", resolve)


@pytest.mark.spec("TRN-64")
def test_each_address_gets_its_share_of_the_connect_phase(
    monkeypatch: pytest.MonkeyPatch,
    unaccepting: tuple[str, int],
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    listener, seen = server("127.0.0.1")
    _two_addresses(monkeypatch, unaccepting, listener.getsockname())
    monkeypatch.setattr(_http, "PER_ADDRESS", 0.05)
    started = now()
    assert (
        call(Pool(Origin.parse("http://inventory.test")), deadline=started + 1.0).status
        == 200
    )
    assert now() - started >= 0.45
    assert len(seen) == 1


@pytest.mark.spec("TRN-64")
def test_the_share_is_at_least_the_floor(
    monkeypatch: pytest.MonkeyPatch,
    unaccepting: tuple[str, int],
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    listener, seen = server("127.0.0.1")
    _two_addresses(monkeypatch, unaccepting, listener.getsockname())
    error = fails(Pool(Origin.parse("http://inventory.test")), deadline=now() + 0.4)
    assert (error.kind, error.message) == ("timeout", _NOT_SENT)
    assert seen == []


class CountingEvent(threading.Event):
    """An event that counts the callers waiting on it."""

    waiting = 0
    changed = threading.Condition()

    def wait(self, timeout: float | None = None) -> bool:
        with CountingEvent.changed:
            CountingEvent.waiting += 1
            CountingEvent.changed.notify_all()
        return super().wait(timeout)


def test_calls_that_need_one_name_share_its_resolution(
    monkeypatch: pytest.MonkeyPatch,
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    listener, _ = server("127.0.0.1")
    address = listener.getsockname()
    release = threading.Event()
    asked: list[str] = []

    def resolve(host: str, *args: object) -> list[object]:
        asked.append(host)
        release.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]

    real = _http._Lookup

    def lookup() -> _http._Lookup:
        made = real()
        made.done = CountingEvent()
        return made

    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    monkeypatch.setattr(_http, "_Lookup", lookup)
    monkeypatch.setattr(CountingEvent, "waiting", 0)
    pool = Pool(Origin.parse("http://inventory.test"))
    results: list[int] = []
    callers = [
        threading.Thread(
            target=lambda: results.append(call(pool, deadline=now() + 10).status)
        )
        for _ in range(2)
    ]
    for caller in callers:
        caller.start()
    with CountingEvent.changed:
        assert CountingEvent.changed.wait_for(lambda: CountingEvent.waiting == 2, 10)
    release.set()
    for caller in callers:
        caller.join(10)
    assert results == [200, 200]
    assert asked == ["inventory.test"]
    assert pool._lookup is None
    pool.close()


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX only")
def test_a_forked_child_resolves_on_its_own_while_the_parents_resolution_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    listener, seen = server("127.0.0.1")
    address = listener.getsockname()
    parent = os.getpid()
    release = threading.Event()
    entered = threading.Event()

    def resolve(host: str, *args: object) -> list[object]:
        if os.getpid() == parent:
            entered.set()
            release.wait(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]

    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    pool = Pool(Origin.parse("http://inventory.test"))
    caller = threading.Thread(target=lambda: call(pool, deadline=now() + 10))
    caller.start()
    assert entered.wait(10)
    child = os.fork()
    if child == 0:  # pragma: no cover (the child's outcome is its exit status)
        status = 1
        try:
            status = 0 if call(pool, deadline=now() + 3).status == 200 else 2
        finally:
            os._exit(status)
    _, code = os.waitpid(child, 0)
    release.set()
    caller.join(10)
    pool.close()
    assert os.waitstatus_to_exitcode(code) == 0
    assert len(seen) == 2


# No frame of the client reaches an error report


def _view(pool: Pool) -> str:
    try:
        exchange(pool, TARGET, lambda: FIELDS, b"[" + b"0," * 2_000_000 + b"0]", None)
    except ClientError:
        return ExceptionReporter(None, *sys.exc_info()).get_traceback_html()
    return ""


@override_settings(DEBUG=True)
def test_a_failure_while_writing_leaves_no_token_in_the_technical_500_page(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(lambda sock: sock.recv(65536) and None)
    page = _view(pool)
    assert "failed before the response was complete" in page
    assert TOKEN.split()[1] not in page
    assert "c2VjcmV0" not in page


def test_a_client_error_carries_no_cause_and_no_context(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(lambda sock: sock.recv(65536) and None)
    error = fails(pool, body=b"[" + b"0," * 2_000_000 + b"0]")
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__traceback__ is not None
    frames = []
    tb: TracebackType | None = error.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    assert frames[-1] == "exchange"


@pytest.mark.spec("TRN-64")
def test_a_deadline_reached_between_connecting_and_the_handshake_ends_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(_http, "_now", clock)
    ends: list[socket.socket] = []

    def opener(until: float) -> socket.socket:
        client, server = socket.socketpair()
        ends.extend((client, server))
        clock.at = until
        return client

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    pool = Pool(Origin.parse("https://inventory.test"), context, opener=opener)
    error = fails(pool, deadline=clock.at + 1)
    assert (error.kind, error.message) == ("timeout", _NOT_SENT)
    assert ends[0].fileno() == -1
    ends[1].close()


@pytest.mark.parametrize(
    ("url", "context"),
    [("http://a.test", True), ("https://a.test", False)],
)
def test_a_pool_has_a_tls_context_exactly_for_https(url: str, context: bool) -> None:
    with pytest.raises(ValueError, match="TLS context"):
        Pool(
            Origin.parse(url),
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if context else None,
        )


@pytest.mark.parametrize("poll", [True, False], ids=["poll", "select"])
def test_an_idle_connection_with_something_to_read_is_dropped(
    monkeypatch: pytest.MonkeyPatch, poll: bool
) -> None:
    if not poll:
        monkeypatch.delattr(select, "poll", raising=False)
    pool = Pool(Origin.parse("http://inventory.test"))
    quiet, quiet_peer = socket.socketpair()
    spoken, spoken_peer = socket.socketpair()
    spoken_peer.sendall(b"HTTP/1.1 408 Request Timeout\r\n\r\n")
    pool.give(_http._Connection(quiet))
    pool.give(_http._Connection(spoken))
    taken = pool.take()
    assert taken is not None
    assert taken.sock is quiet
    assert spoken.fileno() == -1
    taken.close()
    for sock in (quiet_peer, spoken_peer):
        sock.close()


def test_an_idle_connection_whose_socket_is_gone_is_dropped() -> None:
    pool = Pool(Origin.parse("http://inventory.test"))
    a, b = socket.socketpair()
    connection = _http._Connection(a)
    pool.give(connection)
    a.close()
    assert pool.take() is None
    b.close()


def test_a_client_error_copies_and_pickles_with_its_fields() -> None:
    error = ClientError("unavailable", "m", "reading", "OSError: 'x'")
    for copied in (
        copy.copy(error),
        copy.deepcopy(error),
        pickle.loads(pickle.dumps(error)),  # noqa: S301 (bytes this test wrote)
    ):
        assert (copied.kind, copied.message, copied.phase, copied.cause) == (
            "unavailable",
            "m",
            "reading",
            "OSError: 'x'",
        )


@pytest.mark.spec("HTTP-6", "TRN-64")
@pytest.mark.parametrize(
    "line",
    [
        b"X-A:" + b" " * 65000 + b"\x01",
        b"X-A:" + b"\t" * 65000 + b"\x01",
        b"X-A: a" + b" a" * 32000 + b"\x01",
        b"X" * 65000 + b" :",
    ],
    ids=["spaces", "tabs", "words", "name"],
)
def test_a_hostile_field_line_is_refused_in_linear_time(
    provide: Callable[..., tuple[Pool, Provider]], line: bytes
) -> None:
    pool, _ = provide(answer(b"HTTP/1.1 200 OK\r\n" + line + b"\r\n\r\n"))
    started = now()
    error = fails(pool, deadline=started + 5)
    assert error.message.endswith("did not answer in HTTP/1.1")
    assert now() - started < 1


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX only")
def test_a_forked_child_gets_a_pool_while_another_thread_held_the_setups_lock() -> None:
    pools = Pools()
    held = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with pools._lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    assert held.wait(10)
    child = os.fork()
    if child == 0:  # pragma: no cover (the child's outcome is its exit status)
        status = 1
        try:
            pool = pools.pool(Origin.parse("http://b.test"))
            status = 0 if pool.take() is None else 2
        finally:
            os._exit(status)
    release.set()
    holder.join(10)
    waited = threading.Event()
    for _ in range(100):
        done, code = os.waitpid(child, os.WNOHANG)
        if done:
            break
        waited.wait(0.05)
    else:
        os.kill(child, 9)
        os.waitpid(child, 0)
        pytest.fail("the child waited on the parent's lock")
    assert os.waitstatus_to_exitcode(code) == 0
    pools.close()


def test_a_resolution_thread_that_cannot_start_is_unavailable_and_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = threading.Thread.start
    refused: list[str] = []

    def start(thread: threading.Thread) -> None:
        if not refused:
            refused.append(thread.name)
            raise RuntimeError("can't start new thread")
        real(thread)

    def resolve(*args: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    pool = Pool(Origin.parse("http://inventory.test"))
    first = fails(pool, deadline=now() + 5)
    assert first.message.endswith("could not be reached: its name did not resolve")
    assert first.cause.startswith("RuntimeError")
    assert pool._lookup is None
    second = fails(pool, deadline=now() + 5)
    assert second.message.endswith("could not be reached: its name did not resolve")
    assert second.cause.startswith("gaierror")
    assert refused == ["neolith-resolve"]


class GatedLock:
    """A lock at which the thread named "giver" stops until the test lets it on."""

    def __init__(self) -> None:
        self.inner = threading.Lock()
        self.arrived = threading.Event()
        self.gate = threading.Event()

    def __enter__(self) -> None:
        if threading.current_thread().name == "giver":
            self.arrived.set()
            self.gate.wait(10)
        self.inner.acquire()

    def __exit__(self, *exc: object) -> None:
        self.inner.release()


def test_a_connection_given_back_while_the_pool_closes_is_closed() -> None:
    pool = Pool(Origin.parse("http://inventory.test"))
    gated = GatedLock()
    pool._lock = gated  # type: ignore[assignment]
    a, b = socket.socketpair()
    giver = threading.Thread(
        target=pool.give, args=(_http._Connection(a),), name="giver"
    )
    giver.start()
    assert gated.arrived.wait(10)
    pool.close()
    gated.gate.set()
    giver.join(10)
    assert a.fileno() == -1
    assert pool._idle == []
    b.close()


@pytest.mark.parametrize("raised", [MemoryError, KeyboardInterrupt])
def test_any_failure_to_start_the_resolution_thread_is_forgotten(
    monkeypatch: pytest.MonkeyPatch, raised: type[BaseException]
) -> None:
    real = threading.Thread.start
    refused: list[str] = []

    def start(thread: threading.Thread) -> None:
        if not refused:
            refused.append(thread.name)
            raise raised
        real(thread)

    def resolve(*args: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    pool = Pool(Origin.parse("http://inventory.test"))
    with pytest.raises(raised):
        call(pool, deadline=now() + 5)
    assert pool._lookup is None
    second = fails(pool, deadline=now() + 5)
    assert second.message.endswith("could not be reached: its name did not resolve")
    assert second.cause.startswith("gaierror")


# No frame below exchange keeps the token, however the call ends


def _admitted_then_refused() -> list[tuple[str, str]]:
    return [*FIELDS, ("tracestate", "A=1")]


def _frames_below_exchange(error: BaseException) -> list[dict[str, object]]:
    seen: list[dict[str, object]] = []
    pending: list[BaseException | None] = [error]
    while pending:
        found = pending.pop()
        if found is None:
            continue
        tb: TracebackType | None = found.__traceback__
        below = found is not error
        while tb is not None:
            if below:
                seen.append(dict(tb.tb_frame.f_locals))
            below = below or tb.tb_frame.f_code is exchange.__code__
            tb = tb.tb_next
        pending.extend((found.__cause__, found.__context__))
    return seen


def _holds_token(frames: list[dict[str, object]]) -> bool:
    secret = TOKEN.split()[1]
    return any(secret in repr(value) for frame in frames for value in frame.values())


class Unexpected(Exception):
    """A failure no rule of the client names."""


@pytest.mark.parametrize("ending", ["refused field", "unexpected", "interrupt"])
def test_no_frame_below_exchange_keeps_the_token(
    provide: Callable[..., tuple[Pool, Provider]],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    pool, _ = provide(answer(OK))
    fields: Callable[[], Sequence[tuple[str, str]]] = lambda: FIELDS  # noqa: E731
    if ending == "refused field":
        fields = _admitted_then_refused
        expected: type[BaseException] = ValueError
    else:
        expected = Unexpected if ending == "unexpected" else KeyboardInterrupt

        def send(*args: object, **kwargs: object) -> None:
            raise expected

        monkeypatch.setattr(_http, "_send", send)
    with pytest.raises(expected) as raised:
        exchange(pool, TARGET, fields, BODY, None)
    assert raised.value.__traceback__ is not None
    frames = _frames_below_exchange(raised.value)
    assert not _holds_token(frames)
    if ending == "refused field":
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        assert str(raised.value) == "the tracestate field does not match its grammar"


def test_a_client_error_leaves_no_frame_below_exchange(
    provide: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = provide(lambda sock: sock.recv(65536) and None)
    with pytest.raises(ClientError) as raised:
        exchange(pool, TARGET, lambda: FIELDS, b"[" + b"0," * 200_000 + b"0]", None)
    assert _frames_below_exchange(raised.value) == []


# A resolution that outlives its callers


@pytest.mark.spec("TRN-64")
def test_a_resolution_every_caller_gave_up_on_is_forgotten(
    monkeypatch: pytest.MonkeyPatch,
    server: Callable[[str], tuple[socket.socket, list[bytes]]],
) -> None:
    listener, seen = server("127.0.0.1")
    address = listener.getsockname()
    stuck = threading.Event()
    asked: list[int] = []

    def resolve(host: str, *args: object) -> list[object]:
        asked.append(1)
        if len(asked) == 1:
            stuck.wait(10)
            raise socket.gaierror("released")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]

    monkeypatch.setattr(_http, "_getaddrinfo", resolve)
    pool = Pool(Origin.parse("http://inventory.test"))
    try:
        error = fails(pool, deadline=now() + 0.2)
        assert (error.kind, error.message) == ("timeout", _NOT_SENT)
        assert pool._lookup is None
        assert call(pool, deadline=now() + 5) == Response(200, b"{}")
        assert asked == [1, 1]
        assert len(seen) == 1
    finally:
        stuck.set()
        pool.close()
