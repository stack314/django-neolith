"""A provider played byte by byte on the far end of a socket pair.

Shared by the tests of the HTTP transport's client (ADR 0028 Decision 9).
"""

import re
import socket
import threading
from collections.abc import Callable, Sequence

import pytest

from neolith._http import ClientError, Pool, Response, exchange

TARGET = "/inventory/v1/reserve"
# Held only here, never in a test's own locals, so that a frame listing shows
# it only if the client's frames hold it. Not a secret: no key signs it.
TOKEN = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJvcmRlcnMifQ.c2VjcmV0LXNpZ25hdHVyZQ"  # noqa: S105
FIELDS = (
    ("Authorization", TOKEN),
    ("Neolith-Timeout", "250"),
    ("Idempotency-Key", '"k-1"'),
    ("traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
)
OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
BODY = b'{"sku":"a-1"}'
Script = Callable[[socket.socket], None]


def hold(sock: socket.socket) -> None:
    """A provider that holds the connection and says nothing until released."""
    raise AssertionError("run by Provider, which waits instead")  # pragma: no cover


def read_request(sock: socket.socket) -> bytes:
    """One request as the provider reads it, or what arrived before the end."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    found = re.search(rb"\r\nContent-Length: ([0-9]+)", head)
    length = int(found[1]) if found else 0
    while len(rest) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


def answer(*responses: bytes) -> Script:
    """A provider that reads a request and sends each response in turn."""

    def script(sock: socket.socket) -> None:
        for response in responses:
            if not read_request(sock):
                return
            sock.sendall(response)

    return script


class Provider:
    """Plays the provider: one script per connection the client opens."""

    def __init__(self, scripts: Sequence[Script]) -> None:
        self.scripts = list(scripts)
        self.opened = 0
        self.errors: list[BaseException] = []
        self.threads: list[threading.Thread] = []
        self.release = threading.Event()

    def open(self, until: float) -> socket.socket:
        client, server = socket.socketpair()
        script = self.scripts[self.opened]
        self.opened += 1
        thread = threading.Thread(target=self._run, args=(script, server))
        self.threads.append(thread)
        thread.start()
        return client

    def _run(self, script: Script, server: socket.socket) -> None:
        try:
            if script is hold:
                self.release.wait(10)
            else:
                script(server)
        except OSError:
            pass  # the client closed its end
        except BaseException as error:
            self.errors.append(error)
        finally:
            server.close()

    def finish(self) -> None:
        self.release.set()
        for thread in self.threads:
            thread.join(10)
            assert not thread.is_alive()
        assert not self.errors


def call(
    pool: Pool,
    deadline: float | None = None,
    fields: Callable[[], Sequence[tuple[str, str]] | None] | None = None,
    body: bytes = BODY,
) -> Response:
    return exchange(pool, TARGET, fields or (lambda: FIELDS), body, deadline)


def fails(
    pool: Pool,
    deadline: float | None = None,
    fields: Callable[[], Sequence[tuple[str, str]] | None] | None = None,
    body: bytes = BODY,
) -> ClientError:
    with pytest.raises(ClientError) as raised:
        call(pool, deadline, fields, body)
    return raised.value
