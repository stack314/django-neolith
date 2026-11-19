"""The routing part's scripted providers do what ADR 0032 Decision 8 says
each letter does, and record hidden fields as positions only.

They are the oracle of ``tests/conformance/routing/``, so they are tested
here on their own, with a client of raw sockets, not with neolith's
connector. They cite no requirement ID: they are the suite's own tools.
"""

import json
import socket
import threading
from collections.abc import Iterator

import pytest

from tests.scripted_providers import (
    BOUND,
    DECLARED,
    NOT_A_REPLY,
    REPLY,
    STATUS,
    Journal,
    Positions,
    Provider,
    started,
)

TOKEN = "Bearer not-a-real-token"  # noqa: S105
OTHER_TOKEN = "Bearer another-made-up-token"  # noqa: S105


def raw(target: str = "/case/v1/get", fields: tuple[str, ...] = ()) -> bytes:
    body = b'{"n":1}'
    head = [
        f"POST {target} HTTP/1.1",
        "Host: 127.0.0.1",
        f"Content-Length: {len(body)}",
        *fields,
    ]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def port(provider: Provider) -> int:
    return int(provider.url.rpartition(":")[2])


def client(provider: Provider) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port(provider)), BOUND)
    connection.settimeout(BOUND)
    return connection


def read_response(connection: socket.socket) -> tuple[int, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        assert chunk, data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    line, *lines = head.decode("latin-1").split("\r\n")
    fields = {
        name.lower(): value.strip()
        for name, _, value in (entry.partition(":") for entry in lines)
    }
    length = int(fields["content-length"])
    while len(body) < length:
        chunk = connection.recv(65536)
        assert chunk
        body += chunk
    return int(line.split()[1]), fields, body


@pytest.fixture
def journal() -> Journal:
    return Journal(Positions())


@pytest.fixture
def made(journal: Journal) -> Iterator[list[Provider]]:
    """Providers a test makes, closed after it, which must have met no
    error.
    """
    providers: list[Provider] = []
    yield providers
    for provider in providers:
        provider.close()
    assert journal.errors == []


def provider(
    made: list[Provider], journal: Journal, letter: str, **options: object
) -> Provider:
    found = Provider(letter, journal, **options)  # type: ignore[arg-type]
    made.append(found)
    found.start()
    return found


def test_r_refuses_every_connection(made: list[Provider], journal: Journal) -> None:
    r = provider(made, journal, "R")
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port(r)), BOUND)
    assert journal.requests == []


@pytest.mark.parametrize("letter", ["U", "D"])
def test_u_and_d_answer_503_with_no_error_body_on_one_connection(
    made: list[Provider], journal: Journal, letter: str
) -> None:
    u = provider(made, journal, letter)
    with client(u) as connection:
        for _ in range(2):
            connection.sendall(raw())
            status, fields, body = read_response(connection)
            assert (status, body) == (503, b"")
            assert "content-type" not in fields
    assert [r.provider for r in journal.requests] == [letter, letter]


def test_n_answers_503_with_an_unavailable_error_body(
    made: list[Provider], journal: Journal
) -> None:
    n = provider(made, journal, "N")
    with client(n) as connection:
        connection.sendall(raw())
        status, fields, body = read_response(connection)
    assert status == 503
    assert fields["content-type"] == "application/json"
    assert json.loads(body) == {
        "kind": "unavailable",
        "message": "the scripted provider is unavailable",
    }


@pytest.mark.parametrize(
    ("kind", "status"), [*STATUS.items(), ("gone", DECLARED)], ids=str
)
def test_e_answers_its_one_kind_at_its_status(
    made: list[Provider], journal: Journal, kind: str, status: int
) -> None:
    detail = {"reason": "r"} if kind == "gone" else None
    e = provider(made, journal, "E", kind=kind, detail=detail)
    with client(e) as connection:
        connection.sendall(raw())
        found, fields, body = read_response(connection)
    assert found == status
    assert fields["content-type"] == "application/json"
    expected: dict[str, object] = {
        "kind": kind,
        "message": f"the scripted provider answers {kind}",
    }
    if detail is not None:
        expected["data"] = detail
    assert json.loads(body) == expected


@pytest.mark.parametrize(("letter", "body"), [("V", NOT_A_REPLY), ("B", REPLY)])
def test_v_and_b_answer_200(
    made: list[Provider], journal: Journal, letter: str, body: bytes
) -> None:
    found = provider(made, journal, letter)
    with client(found) as connection:
        connection.sendall(raw())
        assert read_response(connection)[::2] == (200, body)


def test_x_reads_the_whole_request_then_resets(
    made: list[Provider], journal: Journal
) -> None:
    x = provider(made, journal, "X")
    with client(x) as connection:
        connection.sendall(raw())
        with pytest.raises(ConnectionResetError):
            connection.recv(65536)
    (request,) = journal.requests
    assert request.body == '{"n":1}'


def test_s_reads_the_request_and_never_answers(
    made: list[Provider], journal: Journal
) -> None:
    arrived = threading.Event()
    s = provider(made, journal, "S", before=arrived.set)
    with client(s) as connection:
        connection.sendall(raw())
        assert arrived.wait(BOUND)
        # Closing S ends the connection: what the caller then reads is the
        # end, with not a byte before it.
        s.close()
        assert connection.recv(65536) == b""
    assert len(journal.requests) == 1


def test_before_runs_after_recording_and_before_answering(
    made: list[Provider], journal: Journal
) -> None:
    entered, release = threading.Event(), threading.Event()
    recorded: list[int] = []

    def before() -> None:
        recorded.append(len(journal.requests))
        entered.set()
        assert release.wait(BOUND)

    u = provider(made, journal, "U", before=before)
    with client(u) as connection:
        connection.sendall(raw())
        assert entered.wait(BOUND)
        connection.setblocking(False)
        with pytest.raises(BlockingIOError):
            connection.recv(65536)
        connection.setblocking(True)
        connection.settimeout(BOUND)
        release.set()
        assert read_response(connection)[0] == 503
    assert recorded == [1]


def test_hidden_fields_are_positions_shared_across_providers() -> None:
    positions = Positions()
    first, second = Journal(positions), Journal(positions)
    head = [
        f"Authorization: {TOKEN}",
        "Idempotency-Key: k-1",
        "Neolith-Principal: p-1",
        "tracestate: a=b",
        "traceparent: 00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    ]
    first.add("U", "POST /a/v1/get HTTP/1.1", head, b"{}")
    second.add("B", "POST /a/v1/get HTTP/1.1", head, b"{}")
    second.add("B", "POST /a/v1/get HTTP/1.1", [f"Authorization: {OTHER_TOKEN}"], b"{}")
    a, b, c = first.requests[0], *second.requests
    assert (
        a.fields
        == b.fields
        == {
            "authorization": "token 1",
            "idempotency-key": "key 1",
            "neolith-principal": "principal 1",
            "tracestate": "tracestate 1",
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        }
    )
    assert c.fields == {"authorization": "token 2"}
    for request in (a, b, c):
        text = json.dumps(request.as_json())
        for value in (TOKEN, OTHER_TOKEN, "k-1", "p-1", "a=b"):
            assert value not in text


def test_a_hidden_field_given_twice_is_recorded_as_such(journal: Journal) -> None:
    head = [f"Authorization: {TOKEN}", f"authorization: {OTHER_TOKEN}"]
    journal.add("U", "POST /a/v1/get HTTP/1.1", head, b"{}")
    assert journal.requests[0].fields == {"authorization": "given twice"}


def test_found_in_names_a_leak_by_position_only() -> None:
    positions = Positions()
    positions.of("authorization", TOKEN)
    positions.of("idempotency-key", "k-1")
    assert positions.found_in(f"logged {TOKEN} here") == ["token 1"]
    assert positions.found_in("a line with k-1 in it") == ["key 1"]
    assert positions.found_in("nothing hidden") == []


@pytest.mark.parametrize(
    ("letter", "options", "message"),
    [
        ("Q", {}, "no scripted provider is called 'Q'"),
        ("E", {}, "E, and only E, answers with a kind"),
        ("B", {"kind": "conflict"}, "E, and only E, answers with a kind"),
    ],
)
def test_a_provider_is_made_only_as_a_letter_says(
    journal: Journal, letter: str, options: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Provider(letter, journal, **options)  # type: ignore[arg-type]


def test_started_closes_every_provider_and_its_threads(journal: Journal) -> None:
    b = Provider("B", journal)
    with started(b), client(b) as connection:
        connection.sendall(raw())
        assert read_response(connection)[0] == 200
    assert all(not thread.is_alive() for thread in b._threads)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port(b)), BOUND)
    assert journal.errors == []
