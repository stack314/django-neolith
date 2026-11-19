"""The routing part's scripted providers (ADR 0032 Decision 8).

Each is a server on ``127.0.0.1`` and a port the system picks, bound when it
is made, and answers every request it reads in one fixed way, named by a
letter as in ADR 0031's Decision 9 table:

- **R** refuses: a socket bound and never listening, so a connection to it
  is refused at once;
- **U** answers ``503`` with no neolith error body, and **D** is a second U;
- **N** answers ``503`` with a neolith error body of kind ``unavailable``;
- **X** reads the whole request, then resets the connection;
- **E** answers with the neolith error body of the one kind it is made
  with, at that kind's status;
- **V** answers ``200`` with a body that is not a valid reply;
- **S** reads the request and never answers;
- **B** answers ``200`` with a valid reply.

Every provider records each request it reads, in arrival order, in the
``Journal`` it is made with. A field that can carry a secret, a principal or
a key (``Authorization``, ``Neolith-Principal``, ``Idempotency-Key``, and
``tracestate`` with them) is recorded as its position among the distinct
values of that field that the ``Positions`` it is given has seen, across
every provider (``token 1``, ``token 2``), never as its value. So a case can
show that two providers saw one key without printing it.

Every wait has a bound of ``BOUND`` seconds: accepting, reading, and the
silent provider's wait for the caller to go. Nothing here imports neolith:
the bytes each provider writes come from spec 03, so the oracle shares no
code with the connector it drives.
"""

import contextlib
import json
import re
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final

# A bound on every wait, far longer than any answer takes.
BOUND: Final = 10.0
# Fields recorded as a position, never as a value, with the word that
# names a position. Names are compared in lower case.
HIDDEN: Final = {
    "authorization": "token",
    "neolith-principal": "principal",
    "idempotency-key": "key",
    "tracestate": "tracestate",
}
# Spec 03's statuses for the kinds E can answer with; a declared kind is 422.
STATUS: Final = {
    "timeout": 504,
    "unauthenticated": 401,
    "permission_denied": 403,
    "conflict": 409,
    "internal": 500,
}
DECLARED: Final = 422
LETTERS: Final = frozenset("RUDNXEVSB")
# B's reply, and the reply V sends in place of one. Every test contract's
# reply is a record with one integer field, n.
REPLY: Final = b'{"n":7}'
NOT_A_REPLY: Final = b'{"n":"seven"}'


class Positions:
    """The distinct values of each hidden field, numbered from 1 in the
    order first seen, across every provider that shares this table.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, list[str]] = {name: [] for name in HIDDEN}

    def of(self, name: str, value: str) -> str:
        """The position of ``value`` among the values of field ``name``."""
        with self._lock:
            seen = self._seen[name]
            if value not in seen:
                seen.append(value)
            return f"{HIDDEN[name]} {seen.index(value) + 1}"

    def found_in(self, text: str) -> list[str]:
        """The position of every value seen that ``text`` holds, so a check
        can name a leak without printing it.
        """
        with self._lock:
            return [
                f"{HIDDEN[name]} {index}"
                for name, seen in self._seen.items()
                for index, value in enumerate(seen, 1)
                if value in text
            ]


@dataclass(frozen=True)
class Request:
    """One request as a provider read it. ``fields`` maps each field name,
    in lower case, to its value, or to its position for a hidden field.
    """

    provider: str
    line: str
    fields: dict[str, str]
    body: str

    def as_json(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "line": self.line,
            "fields": self.fields,
            "body": self.body,
        }


@dataclass
class Journal:
    """Every request the providers of one case read, in arrival order."""

    positions: Positions
    requests: list[Request] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, provider: str, line: str, head: list[str], body: bytes) -> None:
        fields: dict[str, str] = {}
        for entry in head:
            name, _, value = entry.partition(":")
            name, value = name.strip(" \t").lower(), value.strip(" \t")
            if name in fields:
                # A field given twice is recorded as such, never joined, so
                # a hidden value cannot hide in the second.
                fields[name] = "given twice"
            elif name in HIDDEN:
                fields[name] = self.positions.of(name, value)
            else:
                fields[name] = value
        text = body.decode("utf-8", "replace")
        with self._lock:
            self.requests.append(Request(provider, line, fields, text))

    def fail(self, provider: str, error: BaseException | str) -> None:
        """Note what went wrong in ``provider``, by the error's class only."""
        what = error if isinstance(error, str) else type(error).__name__
        with self._lock:
            self.errors.append(f"{provider}: {what}")


def _read(connection: socket.socket) -> tuple[str, list[str], bytes] | None:
    """One request, framed by its ``Content-Length``: its line, its field
    lines and its body; ``None`` once the caller has closed the connection
    before a whole request arrived.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            return None
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    line, *lines = head.decode("latin-1").split("\r\n")
    lengths = [
        value.strip(" \t")
        for name, _, value in (entry.partition(":") for entry in lines)
        if name.strip(" \t").lower() == "content-length"
    ]
    if len(lengths) != 1 or not re.fullmatch("[0-9]+", lengths[0]):
        return line, lines, body
    length = int(lengths[0])
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            return None
        body += chunk
    return line, lines, body[:length]


def _response(status: int, body: bytes = b"", *, json_body: bool = False) -> bytes:
    head = f"HTTP/1.1 {status} X\r\n"
    if json_body:
        head += "Content-Type: application/json\r\n"
    return f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body


def error_body(kind: str, message: str, data: object = None) -> bytes:
    """A neolith error body (spec 03)."""
    value: dict[str, object] = {"kind": kind, "message": message}
    if data is not None:
        value["data"] = data
    return json.dumps(value, separators=(",", ":")).encode()


def _answer(letter: str, kind: str | None, detail: object) -> bytes | None:
    """What ``letter`` writes after reading a request; ``None`` for the
    providers that write nothing (X, S).
    """
    if letter in "UD":
        return _response(503)
    if letter == "N":
        body = error_body("unavailable", "the scripted provider is unavailable")
        return _response(503, body, json_body=True)
    if letter == "E":
        assert kind is not None
        status = STATUS.get(kind, DECLARED)
        body = error_body(kind, f"the scripted provider answers {kind}", detail)
        return _response(status, body, json_body=True)
    if letter == "V":
        return _response(200, NOT_A_REPLY, json_body=True)
    if letter == "B":
        return _response(200, REPLY, json_body=True)
    return None


class Provider:
    """A scripted provider, bound on ``127.0.0.1`` when made.

    It accepts nothing until ``start`` (R never does), so a case's bound on
    every wait counts from the case, not from the process. ``before`` runs
    after each request is recorded and before it is answered, in the
    provider's thread: the routing part moves its clock there.
    """

    def __init__(
        self,
        letter: str,
        journal: Journal,
        *,
        kind: str | None = None,
        detail: object = None,
        before: Callable[[], None] | None = None,
    ) -> None:
        if letter not in LETTERS:
            raise ValueError(f"no scripted provider is called {letter!r}")
        if (letter == "E") != (kind is not None):
            raise ValueError("E, and only E, answers with a kind")
        self.letter = letter
        self.name = letter
        self._journal = journal
        self._kind = kind
        self._detail = detail
        self._before = before
        self._threads: list[threading.Thread] = []
        self._connections: list[socket.socket] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        if letter == "R":
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.bind(("127.0.0.1", 0))
        else:
            self._socket = socket.create_server(("127.0.0.1", 0))
            self._socket.settimeout(BOUND)
        self.url: Final = f"http://127.0.0.1:{self._socket.getsockname()[1]}"

    def start(self) -> None:
        """Start accepting connections."""
        if self.letter == "R":
            return
        self._spawn(self._accept)

    def close(self) -> None:
        """Stop accepting, close every connection, and wait for the threads
        within ``BOUND`` seconds.
        """
        self._closed.set()
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()
        with self._lock:
            connections = list(self._connections)
            threads = list(self._threads)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        for thread in threads:
            thread.join(BOUND)
            if thread.is_alive():
                self._journal.fail(self.name, "a thread did not end")

    def _spawn(self, target: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(
            target=target, args=args, name=f"scripted-{self.name}", daemon=True
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if not self._closed.is_set():
                    self._journal.fail(self.name, error)
                return
            with self._lock:
                self._connections.append(connection)
            self._spawn(self._serve, connection)

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(BOUND)
            try:
                while True:
                    request = _read(connection)
                    if request is None:
                        return
                    self._journal.add(self.name, *request)
                    if self._before is not None:
                        self._before()
                    if self.letter == "X":
                        # A zero linger makes close send a reset.
                        connection.setsockopt(
                            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                        )
                        return
                    if self.letter == "S":
                        # Wait for the caller to go, and say nothing.
                        while connection.recv(65536):
                            pass
                        return
                    answer = _answer(self.letter, self._kind, self._detail)
                    assert answer is not None
                    connection.sendall(answer)
            except OSError as error:
                if not self._closed.is_set():
                    self._journal.fail(self.name, error)


@contextlib.contextmanager
def started(*providers: Provider) -> Iterator[None]:
    """Start ``providers`` while entered; close them all on exit."""
    try:
        for provider in providers:
            provider.start()
        yield
    finally:
        for provider in providers:
            provider.close()
