"""The conformance suite: spec 06's items, made executable in this repository.

It is organised by what it drives (ADR 0032's Decision 2):
``contract/`` runs each case over every transport and compares the outcomes,
``wire/`` drives one transport against bytes the specification fixes, and
``routing/`` runs a host of its own. Each test module's docstring names its
part.

This module holds the live server the HTTP leg calls: neolith's own
development server, the one ``neolith run`` serves with, on a loopback
address and a port the system picks (Decision 5).
"""

import contextlib
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

import pytest
from django.core.handlers.wsgi import WSGIHandler

from neolith import _server

# Far longer than starting, stopping or settling the server takes.
BOUND: Final = 10.0


@dataclass(frozen=True)
class Live:
    """A live server: its origin, and its table of connections."""

    origin: str
    connections: _server.Connections

    def settle(self) -> None:
        """Wait until the server holds no open connection, up to ``BOUND``
        seconds, then fail naming each request still in flight.

        A request in flight is not enough to wait for: a connection whose
        request line the server has not read yet holds none, and its
        provider scope could open during the next test.
        """
        woken = threading.Event()
        self.connections.wake(woken.set)
        deadline = time.monotonic() + BOUND
        try:
            while True:
                woken.clear()
                if self.connections.open_count() == 0:
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                woken.wait(left)
        finally:
            self.connections.wake(_nothing)
        pytest.fail(
            f"the live server still holds a connection after {BOUND:g} seconds; "
            f"requests in flight: {self.connections.in_flight()}"
        )


def _nothing() -> None:
    pass


@contextlib.contextmanager
def serving(
    host: str,
    *,
    ipv6: bool,
    secure: Callable[[socket.socket], socket.socket] | None = None,
) -> Iterator[Live]:
    """Serve the test host on ``host`` and a port the system picks, on a
    daemon thread, while entered; on exit, stop the server and join the
    thread within ``BOUND`` seconds.

    The server serves Django's ``WSGIHandler`` directly, as ``neolith run``
    does, so Django is not set up again. ``secure`` wraps the listening
    socket, for TLS.

    It is not wrapped in ``wsgiref.validate``'s validator, which ADR 0032
    Decision 5 tried: two of its checks go beyond PEP 3333 and refuse what
    spec 03 asks for, so the wire part saw a 500 in their place.
    ``check_content_type`` requires ``Content-Type`` on every response but a
    204 or a 304, so the empty answers of HTTP-30, HTTP-32, HTTP-33 and
    HTTP-47 failed; and ``check_environ`` reads ``CONTENT_LENGTH`` with
    ``int()``, so a ``Content-Length`` of ``x`` raised before the host could
    answer HTTP-38's 400.
    """
    server = _server.listen(host, 0, ipv6=ipv6)
    if secure is not None:
        server.socket = secure(server.socket)
    server.set_app(WSGIHandler())
    thread = threading.Thread(
        target=server.serve_forever, name="neolith-live-server", daemon=True
    )
    thread.start()
    scheme = "http" if secure is None else "https"
    name = f"[{host}]" if ipv6 else host
    try:
        yield Live(f"{scheme}://{name}:{server.server_address[1]}", server.connections)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(BOUND)
        if thread.is_alive():
            pytest.fail(
                f"the live server did not stop within {BOUND:g} seconds; requests "
                f"in flight: {server.connections.in_flight()}"
            )
