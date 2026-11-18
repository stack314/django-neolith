"""Serve and Drain under ``neolith run`` (spec 05; ADR 0022 Decisions 9 and 10).

The host serves with subclasses of Django's own development server classes,
``ThreadedWSGIServer`` and ``WSGIRequestHandler`` from
``django.core.servers.basehttp``, the classes Django's ``LiveServerThread``
starts and stops. They add no runtime dependency (K5), and they are fit for
development, tests and the 0.1 proof, not for production.

**Which requests are in flight.** One lock holds the state of each open
connection: idle, or busy with a request. A request is in flight from the
moment its request line is read until the request handler returns, which is
after the server has closed its response, or after the request ended without
one. The count therefore rests on neither Django's ``request_started`` and
``request_finished`` pair, which a receiver that raises can split (ADR 0022
fact 13), nor on the WSGI callable's entry, which comes after the request
line is read.

**Signals.** A signal may be delivered to any thread, and Python runs its
handler in the main thread only once that thread runs again, so the main
thread never waits on anything a signal cannot end: it waits on a socket
that Python's signal machinery writes to, and that the table writes to when
a request ends.

**Drain.** On the first ``SIGINT`` or ``SIGTERM``, under the lock, the host
marks itself draining and shuts down every idle connection. It then stops
the accept loop and closes the listening socket, and waits until no request
is in flight, or until the bound has passed since Drain began: the request
deadline, or 30 seconds when there is none. A request line read after the
mark is not served, and a busy connection closes once its response ends, so
no request that begins after Drain is served (ADR 0022 fact 14). A second
signal stops at once. Each request still in flight at the end is named on
standard error and its connection shut down, and the status is 1; otherwise
it is 0.

State the module owns (CONTRIBUTING K6): the connections table of the one
server ``neolith run`` starts.
"""

import contextlib
import select
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType
from typing import Any, Final, Protocol, TextIO

from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler

from neolith import _checks

__all__ = ["Connections", "drain_bound", "listen", "serve"]

_SHOWN: Final = 200


def drain_bound(deadline: float | None) -> float:
    """The longest Drain waits, in seconds: the request deadline, or 30
    seconds, the deadline a host has when none is configured, when the host
    gives requests none.
    """
    return float(_checks.DEFAULT_REQUEST_DEADLINE if deadline is None else deadline)


class _Connection(Protocol):
    """What the table reads of a request handler: its socket."""

    connection: Any


@dataclass(frozen=True, slots=True)
class _Request:
    line: str
    began: float


def _shown(raw: bytes) -> str:
    """A request line for a message: decoded as HTTP reads it, with each
    character that does not print replaced, within a bound.
    """
    text = raw.decode("iso-8859-1").rstrip("\r\n")
    text = "".join(c if c.isprintable() else "?" for c in text)
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


def _nothing() -> None:
    pass


def _shut(handler: _Connection) -> None:
    """Shut a connection down both ways: a read blocked on it returns, and
    the client finds its end.
    """
    with contextlib.suppress(OSError):
        handler.connection.shutdown(socket.SHUT_RDWR)


class Connections:
    """Each open connection's state, under one lock: ``None`` when it is
    between requests, or the request in flight on it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[_Connection, _Request | None] = {}
        self._draining = False
        self._woken: Callable[[], None] = _nothing

    def wake(self, woken: Callable[[], None]) -> None:
        """Call ``woken``, which must not block, whenever a request ends or
        a connection closes.
        """
        self._woken = woken

    def opened(self, handler: _Connection) -> bool:
        """Note a new connection; whether it may be served."""
        with self._lock:
            if self._draining:
                return False
            self._open[handler] = None
            return True

    def begin(self, handler: _Connection, line: bytes) -> bool:
        """Note that a request line was read; whether it may be served."""
        with self._lock:
            if self._draining or handler not in self._open:
                return False
            self._open[handler] = _Request(_shown(line), time.monotonic())
            return True

    def ended(self, handler: _Connection) -> bool:
        """Note that a request ended; whether its connection must close."""
        with self._lock:
            if handler in self._open:
                self._open[handler] = None
            draining = self._draining
        self._woken()
        return draining

    def closed(self, handler: _Connection) -> None:
        """Note that a connection closed."""
        with self._lock:
            self._open.pop(handler, None)
        self._woken()

    def drain(self) -> int:
        """Begin Drain: refuse every request and connection from now on, and
        shut down every connection that is between requests. Returns the
        number of requests in flight.
        """
        with self._lock:
            self._draining = True
            for handler, request in self._open.items():
                if request is None:
                    _shut(handler)
            return len(self._busy())

    def in_flight(self) -> list[str]:
        """The line of each request in flight."""
        with self._lock:
            return [request.line for request in self._busy().values()]

    def cut(self) -> list[str]:
        """Stop: shut down every connection with a request in flight, and
        return the line of each such request.
        """
        with self._lock:
            self._draining = True
            busy = self._busy()
            for handler in busy:
                _shut(handler)
            return [request.line for request in busy.values()]

    def _busy(self) -> dict[_Connection, _Request]:
        return {h: r for h, r in self._open.items() if r is not None}


class _Handler(WSGIRequestHandler):
    """Django's request handler, which reports each connection and request
    to the server's table.
    """

    server: "_Server"

    def setup(self) -> None:
        super().setup()
        if not self.server.connections.opened(self):
            _shut(self)

    def parse_request(self) -> bool:
        if self.raw_requestline and not self.server.connections.begin(
            self, self.raw_requestline
        ):
            self.close_connection = True
            return False
        found: bool = super().parse_request()
        return found

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        finally:
            if self.server.connections.ended(self):
                self.close_connection = True

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.connections.closed(self)


class _Server(ThreadedWSGIServer):
    """Django's threaded development server, with a table of its
    connections.
    """

    def __init__(self, address: tuple[str, int], *, ipv6: bool) -> None:
        self.connections = Connections()
        super().__init__(address, _Handler, ipv6=ipv6)


def listen(host: str, port: int, *, ipv6: bool) -> _Server:
    """Bind a server to the address; it serves once ``serve`` is called.

    Raises:
        OSError: The address cannot be bound.
    """
    return _Server((host, port), ipv6=ipv6)


class _Wake:
    """What wakes the main thread: a socket that the signal machinery and
    the connections table write a byte to, so a wait ends on a signal
    whichever thread the signal was delivered to, and when a request ends.
    """

    def __init__(self) -> None:
        self._read, self._write = socket.socketpair()
        self._read.setblocking(False)
        self._write.setblocking(False)

    def fileno(self) -> int:
        """The end Python's signal handling writes the signal number to."""
        return self._write.fileno()

    def __call__(self) -> None:
        with contextlib.suppress(OSError):
            self._write.send(b"\0")

    def wait(self, seconds: float | None) -> None:
        """Wait until woken, or ``seconds`` have passed; ``None`` waits
        without a bound.
        """
        select.select([self._read], [], [], seconds)
        with contextlib.suppress(OSError):
            while self._read.recv(4096):
                pass

    def close(self) -> None:
        """Close both ends."""
        self._read.close()
        self._write.close()


def serve(server: _Server, bound: float, out: TextIO, err: TextIO) -> int:
    """Serve until a signal, then drain; return the exit status.

    Runs in the main thread, where Python runs signal handlers; the accept
    loop and each connection run on threads of their own. A signal may be
    delivered to any thread, so the handler only notes it, and the main
    thread waits on a socket that Python's signal machinery writes to
    (``signal.set_wakeup_fd``) and that the connections table writes to when
    a request ends. The first signal ends serving and the second ends the
    wait; a later one is only noted.
    """
    received: list[int] = []

    def noted(number: int, frame: FrameType | None) -> None:
        received.append(number)

    wake = _Wake()
    server.connections.wake(wake)
    previous: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
    woken: int | None = None
    try:
        woken = signal.set_wakeup_fd(wake.fileno(), warn_on_full_buffer=False)
        for signalled in (signal.SIGINT, signal.SIGTERM):
            previous[signalled] = signal.getsignal(signalled)
            signal.signal(signalled, noted)
        threading.Thread(
            target=server.serve_forever, name="neolith-serve", daemon=True
        ).start()
        while not received:
            wake.wait(None)
        began = time.monotonic()
        in_flight = server.connections.drain()
        server.shutdown()
        server.server_close()
        _say(
            out,
            "draining: no longer accepting connections; waiting up to "
            f"{_seconds(bound)} for {_requests(in_flight)} in flight",
        )
        reason = f"a request was still in flight after {_seconds(bound)}"
        while server.connections.in_flight():
            if len(received) > 1:
                reason = "stopped at once with a request in flight"
                break
            left = bound - (time.monotonic() - began)
            if left <= 0:
                break
            wake.wait(left)
        cut = server.connections.cut()
        for line in cut:
            _say(err, f"drain: {reason}: {line}")
        if cut:
            return 1
        if in_flight:
            _say(out, "stopped; every request in flight finished")
        else:
            _say(out, "stopped; no request was in flight")
        return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if woken is not None:
            signal.set_wakeup_fd(woken)
        server.connections.wake(_nothing)
        wake.close()


def _requests(count: int) -> str:
    return "1 request" if count == 1 else f"{count} requests"


def _seconds(value: float) -> str:
    shown = f"{value:g}"
    return f"{shown} second" if shown == "1" else f"{shown} seconds"


def _say(stream: TextIO, line: str) -> None:
    stream.write(f"neolith: {line}\n")
    stream.flush()
