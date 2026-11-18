"""The HTTP transport's client: the standard library's sockets and TLS (ADR 0028).

The client writes each request itself, exactly as ADR 0028's admitted set
gives it, and reads the response with a strict HTTP/1.1 reader of its own. One
deadline bounds every blocking step: name resolution runs in a thread the
caller waits for, each address gets a share of the connect phase, the TLS
handshake gets what is left of it, and every ``send`` and ``recv_into`` gets
the time left when it starts. Connections are pooled per provider origin. The
client never sends a call twice; the one exception is a first ``send`` on a
reused connection that the socket refused before taking any byte, which
cannot have reached the provider.

``exchange`` returns the final status and the body's bytes, or raises a
``ClientError`` with the kind and message of ADR 0028's failure table. That
error is raised outside every exception handler of the client, so it carries
no cause, no context and none of the client's frames, which hold the request's
header bytes.
"""

import contextlib
import ipaddress
import os
import re
import select
import socket
import ssl
import sys
import threading
import traceback
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, NoReturn, final

from neolith import _context, _naming
from neolith._connector import _LATE, _NOT_SENT
from neolith._contracts import _SEGMENT
from neolith._frozen import once

__all__ = ["ClientError", "Origin", "Pool", "Pools", "Response", "exchange"]

# ADR 0028, the admitted set's constants.
CONNECT_CAP = 10.0  # seconds: resolve, connect and handshake together
PER_ADDRESS = 2.0  # seconds: the least share of the connect phase per address
INTERIM_LIMIT = 5  # interim responses before the final one
IDLE_LIMIT = 10  # idle connections per origin
IDLE_EXPIRY = 1.0  # seconds an idle connection is kept
LINE_LIMIT = 65536  # bytes in a status, field or chunk-size line, with its CRLF
FIELD_LIMIT = 100  # field lines in one header or trailer section
LOG_LIMIT = 200  # characters of a failure's text kept for the log
_KEEPALIVE = (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 6))
_CHUNK = 65536  # bytes asked of one recv_into
# Bytes given to one send over TLS: one record's plaintext at most, so a send
# that fails has delivered nothing the provider can decrypt.
_RECORD = 16384

# Seams for the tests: the clock, the resolver and the TLS context's builder.
_now = _context.now
_getaddrinfo = socket.getaddrinfo

Phase = Literal["before writing", "writing", "reading"]

# The request the client writes (ADR 0028, the admitted set), in this order.
_TARGET = re.compile(rf"/{_SEGMENT}(?:\.{_SEGMENT})*/v[1-9][0-9]*/{_SEGMENT}")
_B64 = r"[A-Za-z0-9_-]"
_TS_KEY = (
    r"(?:[a-z][a-z0-9_*/-]{0,255}"
    r"|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,13})"
)
_TS_VALUE = r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]"
_TS_MEMBER = rf"{_TS_KEY}={_TS_VALUE}"
_FIELDS: Final = {
    "Authorization": re.compile(
        rf"Bearer (?=.{{1,4096}}\Z){_B64}+\.{_B64}+\.{_B64}+", re.ASCII
    ),
    "Neolith-Principal": re.compile(rf"{_B64}{{1,4096}}"),
    "Neolith-Timeout": re.compile(r"[1-9][0-9]{0,9}"),
    "Idempotency-Key": re.compile(r'"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\["\\])+"'),
    "traceparent": re.compile(
        r"00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}"
    ),
    # W3C Trace Context 3.3.1.1: list = list-member 0*31( OWS "," OWS
    # list-member ), list-member = (key "=" value) / OWS, so a member can be
    # empty or blank. The blanks are written once on each side of a comma, so
    # no run of them can be split two ways.
    "tracestate": re.compile(
        rf"(?=.{{1,512}}\Z)(?:{_TS_MEMBER})?"
        rf"(?:[ \t]*,(?:[ \t]*{_TS_MEMBER})?){{0,31}}[ \t]*"
    ),
}
_KEY_LIMIT = _context.KEY_LIMIT
# Every call carries these; the others only when the call has a value for them.
_REQUIRED = frozenset({"Authorization", "Idempotency-Key", "traceparent"})

# The response the client accepts (ADR 0028, the response table).
_STATUS = re.compile(rb"HTTP/1\.([01]) ([1-5][0-9]{2}) [\t\x20-\x7e\x80-\xff]*")
_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_NAME = re.compile(_TOKEN)
_VALUE = re.compile(rb"[\t\x20-\x7e\x80-\xff]*")  # field-value with its OWS
_QUOTED = rb'"(?:[\t \x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\t \x21-\x7e\x80-\xff])*"'
_CHUNK_SIZE = re.compile(
    rb"([0-9A-Fa-f]{1,16})(?:[ \t]*;[ \t]*"
    + _TOKEN
    + rb"(?:[ \t]*=[ \t]*(?:"
    + _TOKEN
    + rb"|"
    + _QUOTED
    + rb"))?)*"
)
_LENGTH = re.compile(rb"[0-9]{1,18}")
_NO_BODY = frozenset({204, 304})


@final
class ClientError(Exception):
    """A call the client could not carry to a final response.

    ``kind`` is ``timeout`` or ``unavailable`` and ``message`` its text, as
    ADR 0028's failure table gives them; neither carries a header value, a
    token or a body. ``phase`` is where the call stopped, and ``cause`` the
    class of the failure underneath with the ``repr`` of its text cut to
    ``LOG_LIMIT`` characters, for the transport's log line.
    """

    def __init__(self, kind: str, message: str, phase: Phase, cause: str) -> None:
        super().__init__(kind, message, phase, cause)
        self.kind = kind
        self.message = message
        self.phase = phase
        self.cause = cause


@final
@once
@dataclass(frozen=True, slots=True)
class Response:
    """The final response of a call: its status and its body's bytes."""

    status: int
    body: bytes


@final
@once
@dataclass(frozen=True, slots=True)
class Origin:
    """A provider's origin, from a url that is exactly its serialisation.

    ``url`` is the url as configured, ``host`` the name or address to connect
    to (an IPv6 address without its brackets) and ``authority`` the url's
    authority as written, which is the request's ``Host``. The url's full
    admission is the remote provider's (ADR 0014); this reads only what the
    client needs, and refuses anything else with ``ValueError``.
    """

    url: str
    scheme: Literal["http", "https"]
    host: str
    port: int
    authority: str

    @classmethod
    def parse(cls, url: str) -> "Origin":
        """Read ``url``, an origin such as ``https://inventory.internal:8443``."""
        found = _ORIGIN.fullmatch(url)
        if found is None:
            raise ValueError("the url is not an http or https origin")
        scheme: Literal["http", "https"] = (
            "https" if found["scheme"] == "https" else "http"
        )
        host = found["host"]
        if host.startswith("["):
            host = host[1:-1]
            if not isinstance(_address(host), ipaddress.IPv6Address):
                raise ValueError("the url's host is not an IPv6 address")
        written = found["port"]
        port = int(written) if written else (443 if scheme == "https" else 80)
        if port > 65535:
            raise ValueError("the url's port is above 65535")
        return cls(url, scheme, host, port, url[len(scheme) + 3 :])


_ORIGIN = re.compile(
    r"(?P<scheme>https?)://(?P<host>\[[0-9a-f:.]+\]|[a-z0-9-]+(?:\.[a-z0-9-]+)*)"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?",
    re.ASCII,
)


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address ``host`` writes, or ``None`` for a name."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class _Stop(Exception):
    """Ends a call inside the client; ``exchange`` raises ``error`` instead."""

    def __init__(self, error: ClientError) -> None:
        super().__init__()
        self.error = error


class _Ended(Exception):
    """The connection ended or failed while the client was reading."""


class _Outside(Exception):
    """A response outside ADR 0028's response table."""


class _Refused(ValueError):
    """A target or field outside its grammar; ``exchange`` raises it afresh."""


class _Unsent(Exception):
    """A first ``send`` on a reused connection that took no byte."""


def _cause(error: BaseException | None) -> str:
    """The class of ``error`` and the ``repr`` of its text, cut for the log."""
    if error is None:
        return ""
    return f"{_naming.type_name(error)}: {repr(_naming.text(error, None))[:LOG_LIMIT]}"


@final
class _Connection:
    """One socket to a provider, with what has been received but not read."""

    __slots__ = ("buffer", "scratch", "sock")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()
        self.scratch = bytearray(_CHUNK)

    def close(self) -> None:
        """Close the socket; closing never fails the call."""
        with contextlib.suppress(OSError):
            self.sock.close()

    def idle_clean(self) -> bool:
        """Whether nothing past the last response has been received."""
        if self.buffer:
            return False
        return not (isinstance(self.sock, ssl.SSLSocket) and self.sock.pending())


def _readable(sock: socket.socket) -> bool:
    """Whether ``sock`` has something to read, or has closed, without waiting."""
    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(sock, select.POLLIN)
            return bool(poller.poll(0))
        return bool(select.select([sock], [], [], 0)[0])
    except (OSError, ValueError):
        return True


@final
class _Lookup:
    """One resolution of an origin's name in flight, which callers share."""

    __slots__ = ("done", "error", "result", "waiting")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.waiting = 0
        self.result: list[tuple[int, int, int, tuple[object, ...]]] = []
        self.error: BaseException | None = None


Opener = Callable[[float], socket.socket]

_POOLS: "weakref.WeakSet[Pool]" = weakref.WeakSet()
_SETUPS: "weakref.WeakSet[Pools]" = weakref.WeakSet()


@final
class Pool:
    """The idle connections to one provider origin, and its resolution in flight.

    It holds at most ``IDLE_LIMIT`` idle connections, reuses the newest first
    and drops one idle for more than ``IDLE_EXPIRY`` seconds or that has
    something to read. A call never waits for a slot. In a forked child, and
    in any other process than the one that built it, it starts empty.
    """

    def __init__(
        self,
        origin: Origin,
        context: ssl.SSLContext | None = None,
        opener: Opener | None = None,
    ) -> None:
        """Build the pool; it opens no connection until the first call.

        Args:
            origin: The provider's origin.
            context: The TLS context, required for an ``https`` origin.
            opener: The tests' seam: what gives a connected socket by a time
                on this host's clock. By default the name is resolved and
                each address tried.
        """
        if (origin.scheme == "https") != (context is not None):
            raise ValueError("a TLS context is given exactly for an https origin")
        self.origin = origin
        self.context = context
        self._opener: Opener = opener if opener is not None else self._connect
        self._lock = threading.Lock()
        self._idle: list[tuple[_Connection, float]] = []
        self._lookup: _Lookup | None = None
        self._pid = os.getpid()
        self._closed = False
        _POOLS.add(self)

    def _reset(self) -> None:
        """Forget every connection and resolution: this is another process."""
        self._lock = threading.Lock()
        idle, self._idle = self._idle, []
        self._lookup = None
        self._pid = os.getpid()
        for connection, _ in idle:
            connection.close()

    def _here(self) -> None:
        if self._pid != os.getpid():
            self._reset()

    def take(self) -> _Connection | None:
        """The newest idle connection still usable, or ``None``."""
        self._here()
        dropped: list[_Connection] = []
        found = None
        with self._lock:
            while self._idle:
                connection, since = self._idle.pop()
                if _now() - since > IDLE_EXPIRY or _readable(connection.sock):
                    dropped.append(connection)
                    continue
                found = connection
                break
        for connection in dropped:
            connection.close()
        return found

    def give(self, connection: _Connection) -> None:
        """Keep ``connection`` for the next call, or close it."""
        self._here()
        surplus: _Connection | None = connection
        with self._lock:
            if not self._closed:
                self._idle.append((connection, _now()))
                surplus = self._idle.pop(0)[0] if len(self._idle) > IDLE_LIMIT else None
        if surplus is not None:
            surplus.close()

    def close(self) -> None:
        """Close the idle connections now, and each one in use when its call ends."""
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for connection, _ in idle:
            connection.close()

    def _resolve(self, until: float, stop: Callable[[], NoReturn]) -> _Lookup:
        """Resolve the origin's name in a thread, waiting at most ``until``.

        The last caller to stop waiting on a resolution that has not ended
        forgets it, so the next call starts its own rather than wait on one
        that may never end, unlike Go's ``ForgetUnshared``, which forgets only
        a lookup no other call joined. The forgotten thread ends on its own;
        its result is not used.
        """
        with self._lock:
            lookup = self._lookup
            if lookup is None:
                lookup = self._lookup = _Lookup()
                try:
                    threading.Thread(
                        target=self._run_lookup,
                        args=(lookup,),
                        name="neolith-resolve",
                        daemon=True,
                    ).start()
                except BaseException as error:
                    self._lookup = None
                    if isinstance(error, RuntimeError):
                        raise _Unreachable("its name did not resolve", error) from None
                    raise
            lookup.waiting += 1
        left = until - _now()
        try:
            ended = left > 0 and lookup.done.wait(left)
        finally:
            with self._lock:
                lookup.waiting -= 1
                if (
                    not lookup.done.is_set()
                    and not lookup.waiting
                    and self._lookup is lookup
                ):
                    self._lookup = None
        if not ended:
            stop()
        return lookup

    def _run_lookup(self, lookup: _Lookup) -> None:
        try:
            found = _getaddrinfo(
                self.origin.host, self.origin.port, 0, socket.SOCK_STREAM
            )
            lookup.result = [(f, t, p, tuple(a)) for f, t, p, _, a in found]
        except Exception as error:
            lookup.error = error
        finally:
            with self._lock:
                if self._lookup is lookup:
                    self._lookup = None
            lookup.done.set()

    def _connect(self, until: float) -> socket.socket:
        """Resolve the name and try each address, by ``until`` at the latest."""
        origin = self.origin
        address = _address(origin.host)

        def late() -> NoReturn:
            raise _Late

        addresses: Sequence[tuple[int, int, int, tuple[object, ...]]]
        if address is None:
            lookup = self._resolve(until, late)
            if lookup.error is not None or not lookup.result:
                raise _Unreachable("its name did not resolve", lookup.error)
            addresses = lookup.result
        else:
            ip = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            addresses = [(ip, socket.SOCK_STREAM, 0, (origin.host, origin.port))]
        last: OSError | None = None
        for index, (family, kind, proto, sockaddr) in enumerate(addresses):
            left = until - _now()
            if left <= 0:
                raise _Late
            share = max(left / (len(addresses) - index), PER_ADDRESS)
            try:
                sock = socket.socket(family, kind, proto)
            except OSError as error:
                last = error
                continue
            try:
                sock.settimeout(min(left, share))
                sock.connect(sockaddr)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for name, value in _KEEPALIVE:
                    if hasattr(socket, name):
                        option = getattr(socket, name)
                        sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError as error:
                sock.close()
                last = error
                continue
            except BaseException:
                sock.close()
                raise
            return sock
        if until - _now() <= 0:
            raise _Late
        raise _Unreachable("no address accepted a connection", last)


class _Late(Exception):
    """The connect phase's time ran out."""


class _Unreachable(Exception):
    """The provider could not be reached, for ``reason``."""

    def __init__(self, reason: str, error: BaseException | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


@final
class Pools:
    """The pools of one set-up: one per origin, and one TLS context for all."""

    def __init__(self) -> None:
        self._pools: dict[str, Pool] = {}
        self._context: ssl.SSLContext | None = None
        self._lock = threading.Lock()
        _SETUPS.add(self)

    def pool(self, origin: Origin) -> Pool:
        """The pool for ``origin``, built the first time it is asked for."""
        with self._lock:
            found = self._pools.get(origin.url)
            if found is None:
                context = None
                if origin.scheme == "https":
                    if self._context is None:
                        self._context = _make_context()
                    context = self._context
                found = self._pools[origin.url] = Pool(origin, context)
            return found

    def close(self) -> None:
        """Close every pool."""
        with self._lock:
            pools = list(self._pools.values())
        for pool in pools:
            pool.close()


def _make_context() -> ssl.SSLContext:
    """The TLS context: the system store, TLS 1.2 or later, strict on every Python.

    ``SSLContext`` is built directly rather than by
    ``ssl.create_default_context``, which reads ``SSLKEYLOGFILE`` (ADR 0028
    Decision 6). The TLS tests replace this function, and nothing else
    reaches it.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= ssl.VERIFY_X509_STRICT | ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


def _after_fork() -> None:
    """Empty every pool in a forked child and give every lock a new one.

    The child has only the thread that forked, so a lock another thread held
    at the fork would never be released, and a resolution in flight never
    ends.
    """
    for setup in list(_SETUPS):
        setup._lock = threading.Lock()
    for pool in list(_POOLS):
        pool._reset()


os.register_at_fork(after_in_child=_after_fork)


def _stop(kind: str, message: str, phase: Phase, cause: str) -> _Stop:
    return _Stop(ClientError(kind, message, phase, cause))


def _unreachable(origin: Origin, reason: str, error: BaseException | None) -> _Stop:
    return _stop(
        "unavailable",
        f"the provider at {origin.url} could not be reached: {reason}",
        "before writing",
        _cause(error),
    )


def _broken(origin: Origin, phase: Phase, cause: str) -> _Stop:
    return _stop(
        "unavailable",
        f"the connection to the provider at {origin.url} failed before the "
        "response was complete",
        phase,
        cause,
    )


def _outside(origin: Origin, error: BaseException) -> _Stop:
    return _stop(
        "unavailable",
        f"the provider at {origin.url} did not answer in HTTP/1.1",
        "reading",
        _cause(error),
    )


def exchange(
    pool: Pool,
    target: str,
    fields: Callable[[], Sequence[tuple[str, str]] | None],
    body: bytes,
    deadline: float | None,
    connect_by: float | None = None,
) -> Response:
    """Send one call and read its final response, by ``deadline`` at the latest.

    Args:
        pool: The pool of the provider's origin.
        target: The action's path, ``/<name>/v<N>/<action>``.
        fields: Called once a connection is ready, and again for the one
            replacement connection, to give the call-context fields in the
            admitted order; ``None`` when the budget, read then, is under
            1 ms, and the call is not sent.
        body: The request's JSON text.
        deadline: The call's deadline on this host's clock, or ``None``.
        connect_by: A time on this host's clock by which a connection must be
            ready, besides the connect cap and the deadline, or ``None``: an
            attempt's share of the connect phase (ADR 0031 Decision 5). The
            replacement connection is held to the same time.

    Returns:
        The final status and the body's bytes.

    Raises:
        ClientError: The call failed with ``timeout`` or ``unavailable``.
        ValueError: ``target`` or a field is outside its grammar: a mistake
            of the code that called this, never a transport failure.
    """
    handled = sys.exception()
    try:
        return _exchange(pool, target, fields, body, deadline, connect_by)
    except _Stop as stop:
        error: Exception = stop.error
    except _Refused as refused:
        error = ValueError(refused.args[0])
    except BaseException as unexpected:
        _forget(unexpected, handled)
        raise
    raise error


def _forget(error: BaseException, handled: BaseException | None) -> None:
    """Clear the locals of every finished frame the client's exceptions hold.

    The frames below ``exchange`` hold the request's header bytes, the token
    among them, and a logger or error reporter that records frame locals
    would keep them. ``exchange``'s own frame, still running, holds none.
    The chain is followed down to ``handled``, the exception the caller was
    handling when it called ``exchange``, whose frames are the caller's.
    """
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        found = pending.pop()
        if found is None or found is handled or id(found) in seen:
            continue
        seen.add(id(found))
        traceback.clear_frames(found.__traceback__)
        pending.extend((found.__cause__, found.__context__))


def _exchange(
    pool: Pool,
    target: str,
    fields: Callable[[], Sequence[tuple[str, str]] | None],
    body: bytes,
    deadline: float | None,
    connect_by: float | None,
) -> Response:
    if not _TARGET.fullmatch(target):
        raise _Refused("the target is not a contract action's path")
    origin = pool.origin
    connection = pool.take()
    first = connection is not None
    while True:
        if connection is None:
            connection = _open(pool, deadline, connect_by)
        try:
            given = fields()
            if given is None:
                pool.give(connection)
                raise _stop("timeout", _NOT_SENT, "before writing", "")
            request = _head(origin, target, given, len(body)) + body
            failed = _send(connection, request, deadline, first=first)
        except _Unsent:
            connection.close()
            connection, first = None, False
            continue
        except _Stop as stop:
            if stop.error.kind != "timeout" or stop.error.phase != "before writing":
                connection.close()
            raise
        except BaseException:
            connection.close()
            raise
        break
    try:
        response, keep = _read(connection, deadline, origin, failed)
    except BaseException:
        connection.close()
        raise
    if keep and failed is None and connection.idle_clean():
        pool.give(connection)
    else:
        connection.close()
    return response


def _open(
    pool: Pool, deadline: float | None, connect_by: float | None = None
) -> _Connection:
    """Open a connection, within the connect cap, ``connect_by`` and the
    deadline, whichever comes first.

    When the deadline ends the phase, the call fails with ``timeout``;
    otherwise with ``unavailable``, naming the bound that ended it.
    """
    origin = pool.origin
    started = _now()
    bound = started + CONNECT_CAP
    if connect_by is not None and connect_by < bound:
        bound = connect_by
    until = bound if deadline is None else min(bound, deadline)

    def out_of_time(error: BaseException | None) -> _Stop:
        if deadline is not None and deadline <= bound:
            return _stop("timeout", _NOT_SENT, "before writing", _cause(error))
        seconds = round(max(bound - started, 0), 3)
        return _unreachable(origin, f"no connection within {seconds:g} seconds", error)

    if until - _now() <= 0:
        raise out_of_time(None)
    try:
        sock = pool._opener(until)
    except _Late:
        raise out_of_time(None) from None
    except _Unreachable as error:
        raise _unreachable(origin, error.reason, error.error) from None
    if pool.context is None:
        return _Connection(sock)
    left = until - _now()
    if left <= 0:
        sock.close()
        raise out_of_time(None)
    try:
        sock.settimeout(left)
        secured = pool.context.wrap_socket(
            sock,
            server_hostname=origin.host,
            suppress_ragged_eofs=False,
            do_handshake_on_connect=False,
        )
    except (OSError, ValueError) as error:
        sock.close()
        raise _unreachable(origin, "the TLS handshake failed", error) from None
    except BaseException:
        sock.close()
        raise
    try:
        secured.do_handshake()
    except TimeoutError as error:
        secured.close()
        raise out_of_time(error) from None
    except ssl.SSLCertVerificationError as error:
        secured.close()
        reason = f"its TLS certificate did not verify ({error.verify_message})"
        raise _unreachable(origin, reason, error) from None
    except OSError as error:
        secured.close()
        raise _unreachable(origin, "the TLS handshake failed", error) from None
    except BaseException:
        secured.close()
        raise
    return _Connection(secured)


def _head(
    origin: Origin, target: str, given: Sequence[tuple[str, str]], length: int
) -> bytes:
    """The request line and header section, in the admitted order."""
    order = list(_FIELDS)
    lines = [
        f"POST {target} HTTP/1.1",
        f"Host: {origin.authority}",
        "Accept-Encoding: identity",
        "Content-Type: application/json",
        f"Content-Length: {length}",
    ]
    at = 0
    for name, value in given:
        if name not in _FIELDS or order.index(name) < at:
            raise _Refused(f"the field {name!r} is not in the admitted order")
        at = order.index(name) + 1
        if not _FIELDS[name].fullmatch(value) or (
            name == "Idempotency-Key" and len(_unescaped(value)) > _KEY_LIMIT
        ):
            raise _Refused(f"the {name} field does not match its grammar")
        lines.append(f"{name}: {value}")
    missing = _REQUIRED.difference(name for name, _ in given)
    if missing:
        raise _Refused(f"the field {sorted(missing)[0]} is missing")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _unescaped(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value[1:-1])


def _left(deadline: float | None) -> float | None:
    """The time left, ``None`` with no deadline; ``TimeoutError`` at none left."""
    if deadline is None:
        return None
    left = deadline - _now()
    if left <= 0:
        raise TimeoutError
    return left


def _send(
    connection: _Connection, data: bytes, deadline: float | None, *, first: bool
) -> OSError | None:
    """Write ``data``; the error, if the connection failed while writing.

    The error is returned, not raised, so that a response the provider sent
    before it, an early refusal, can still be read. ``_Unsent`` is raised for
    a first ``send`` on a reused connection (``first``) that took no byte.
    Over TLS each ``send`` is given one record at most: ``SSLSocket.send``
    reports no count when it fails, and a record that was not written whole
    holds nothing the provider can read.
    """
    sock = connection.sock
    step = _RECORD if isinstance(sock, ssl.SSLSocket) else len(data)
    done = 0
    with memoryview(data) as view:
        while done < len(view):
            try:
                sock.settimeout(_left(deadline))
                done += sock.send(view[done : done + step])
            except TimeoutError as error:
                raise _stop("timeout", _LATE, "writing", _cause(error)) from None
            except OSError as error:
                if first and done == 0:
                    raise _Unsent from None
                return error
    return None


def _read(
    connection: _Connection,
    deadline: float | None,
    origin: Origin,
    failed: OSError | None,
) -> tuple[Response, bool]:
    """Read the final response; whether the connection can be kept after it."""
    try:
        return _response(connection, deadline)
    except _Outside as error:
        raise _outside(origin, error) from None
    except _Ended as error:
        if failed is not None:
            raise _broken(origin, "writing", _cause(failed)) from None
        raise _broken(origin, "reading", _cause(error)) from None


def _fill(connection: _Connection, deadline: float | None) -> int:
    """Receive more into the connection's buffer; 0 at the end of the stream."""
    sock = connection.sock
    try:
        sock.settimeout(_left(deadline))
        received = sock.recv_into(connection.scratch)
    except TimeoutError as error:
        raise _stop("timeout", _LATE, "reading", _cause(error)) from None
    except OSError as error:
        raise _Ended(error) from None
    connection.buffer += connection.scratch[:received]
    return received


def _line(connection: _Connection, deadline: float | None) -> bytes:
    """One line up to its CRLF, at most ``LINE_LIMIT`` bytes with it."""
    buffer = connection.buffer
    searched = 0
    while True:
        end = buffer.find(b"\r\n", max(searched - 1, 0))
        if end != -1:
            if end + 2 > LINE_LIMIT:
                raise _Outside(f"a line is too long: {bytes(buffer[:LOG_LIMIT])!r}")
            line = bytes(buffer[:end])
            del buffer[: end + 2]
            if b"\r" in line or b"\n" in line:
                raise _Outside(f"a line holds a bare CR or LF: {line!r}")
            return line
        if len(buffer) >= LINE_LIMIT:
            raise _Outside(f"a line is too long: {bytes(buffer[:LOG_LIMIT])!r}")
        searched = len(buffer)
        if _fill(connection, deadline) == 0:
            raise _Ended("the connection closed")


def _section(
    connection: _Connection, deadline: float | None
) -> list[tuple[bytes, bytes]]:
    """A header or trailer section: names lowercased, values without OWS."""
    fields: list[tuple[bytes, bytes]] = []
    while True:
        line = _line(connection, deadline)
        if not line:
            return fields
        if len(fields) == FIELD_LIMIT:
            raise _Outside(f"more than {FIELD_LIMIT} field lines")
        name, colon, value = line.partition(b":")
        if not colon or not _NAME.fullmatch(name) or not _VALUE.fullmatch(value):
            raise _Outside(f"a field line is not admitted: {line!r}")
        fields.append((name.lower(), value.strip(b" \t")))


def _response(connection: _Connection, deadline: float | None) -> tuple[Response, bool]:
    """Read past interim responses to the final one, then its body."""
    interim = 0
    while True:
        line = _line(connection, deadline)
        status_line = _STATUS.fullmatch(line)
        if status_line is None:
            raise _Outside(f"a status line is not admitted: {line!r}")
        minor, status = status_line[1], int(status_line[2])
        fields = _section(connection, deadline)
        if status >= 200:
            break
        if status == 101 or interim == INTERIM_LIMIT:
            raise _Outside(f"an interim response is not admitted: {line!r}")
        interim += 1
    options = {
        option.strip(b" \t").lower()
        for name, value in fields
        if name == b"connection"
        for option in value.split(b",")
    }
    keep = b"close" not in options and (minor == b"1" or b"keep-alive" in options)
    lengths = [value for name, value in fields if name == b"content-length"]
    codings = [value for name, value in fields if name == b"transfer-encoding"]
    if len(lengths) > 1 or len(codings) > 1 or (lengths and codings):
        raise _Outside("the response's framing has more than one reading")
    if lengths and not _LENGTH.fullmatch(lengths[0]):
        raise _Outside(f"a Content-Length is not admitted: {lengths[0]!r}")
    if codings and (codings[0].lower() != b"chunked" or minor == b"0"):
        raise _Outside(f"a Transfer-Encoding is not admitted: {codings[0]!r}")
    if status in _NO_BODY:
        if codings or (lengths and int(lengths[0]) != 0):
            keep = False
        return Response(status, b""), keep
    try:
        if lengths:
            body = _exactly(connection, deadline, int(lengths[0]))
        elif codings:
            body = _chunked(connection, deadline)
        else:
            body, keep = _to_end(connection, deadline), False
    except _Ended as error:
        raise _Outside(f"the body ended early: {error}") from None
    return Response(status, body), keep


def _exactly(connection: _Connection, deadline: float | None, size: int) -> bytes:
    buffer = connection.buffer
    while len(buffer) < size:
        if _fill(connection, deadline) == 0:
            raise _Ended("the connection closed")
    body = bytes(buffer[:size])
    del buffer[:size]
    return body


def _chunked(connection: _Connection, deadline: float | None) -> bytes:
    parts: list[bytes] = []
    while True:
        line = _line(connection, deadline)
        size_line = _CHUNK_SIZE.fullmatch(line)
        if size_line is None:
            raise _Outside(f"a chunk-size line is not admitted: {line!r}")
        size = int(size_line[1], 16)
        if size == 0:
            _section(connection, deadline)
            return b"".join(parts)
        parts.append(_exactly(connection, deadline, size))
        if _line(connection, deadline):
            raise _Outside("a chunk's data is not followed by CRLF")


def _to_end(connection: _Connection, deadline: float | None) -> bytes:
    while _fill(connection, deadline):
        pass
    body = bytes(connection.buffer)
    connection.buffer.clear()
    return body
