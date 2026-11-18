"""The site the contract endpoint tests serve: a contract version of their
own, provided on the test host's table with caller backends of their own,
and the requests they send, through Django's own WSGI and ASGI handlers.

Under WSGI the header fields reach Django as wsgiref and Django's server
build the environ: repeated fields joined with ``,``, the first
``Content-Type`` and ``Content-Length`` kept, a missing ``Content-Type``
read as ``text/plain``, names with ``_`` dropped, and values decoded from
their bytes as Latin-1. Under ASGI they are a list of fields, which Django
joins itself.
"""

import asyncio
import dataclasses
import io
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import pytest
from django.core.handlers.asgi import ASGIHandler
from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpRequest
from pydantic import BaseModel

from neolith import (
    CallContext,
    ContractError,
    CredentialRefused,
    _caller,
    _context,
    _endpoint,
    _host,
    _token,
    action,
    contract,
)
from neolith._connector import Connector, InProcess
from neolith.callers import CallerBackend, SharedSecretBackend

MODES = ("wsgi", "asgi")
LEDGER = "ledger.v1"
POST = "/ledger/v1/post"
CLEAR = "/ledger/v1/clear"
CHALLENGE = 'Bearer realm="neolith"'
# A principal object's sent form, base64url-encoded (spec 03's example).
PRINCIPAL = (
    "eyJjbGFpbXMiOnsicm9sZXMiOlsiY2xlcmsiXX0sImlzc3VlciI6Imh0dHBzOi8vaWQuZXhhbX"
    "BsZS5jb20iLCJzdWJqZWN0IjoidS04MWYzIn0"
)
TRACE = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE}-b7ad6b7169203331-01"


class Entry(BaseModel):
    amount: int


class Receipt(BaseModel):
    amount: int


class Overdrawn(BaseModel):
    short: int


@contract(LEDGER)
class Ledger(Protocol):
    @action(errors={"overdrawn": Overdrawn})
    def post(self, request: Entry) -> Receipt: ...

    @action
    def clear(self, request: Entry) -> None: ...


class Books:
    """The ledger's implementation: it keeps the context of each call."""

    def __init__(self) -> None:
        self.seen: list[CallContext] = []

    def post(self, request: Entry, ctx: CallContext) -> Receipt:
        self.seen.append(ctx)
        if request.amount < 0:
            raise ContractError(
                "overdrawn", "the account is short", Overdrawn(short=-request.amount)
            )
        if request.amount == 404:
            raise ContractError("not_found", "no such account")
        if request.amount == 500:
            raise RuntimeError("the books are closed")
        return Receipt(amount=request.amount)

    def clear(self, request: Entry) -> None:
        return None


# Secrets made for the run: none is stored in the repository (K9).
ORDERS_SECRET = secrets.token_urlsafe(32)
STRANGER_SECRET = secrets.token_urlsafe(32)


def token(
    app: str = "orders",
    *,
    identity: str = LEDGER,
    issued: int | None = None,
    key_id: str | None = None,
) -> str:
    """A valid ``Authorization`` value of ``app`` for ``identity``."""
    value = ORDERS_SECRET if app == "orders" else STRANGER_SECRET
    secret = _token.secret(value, app, key_id or f"{app}-1")
    now = int(time.time()) if issued is None else issued
    return _token.mint(secret, identity, now).authorization()


def shared() -> _caller.Built:
    backend = SharedSecretBackend(
        secrets={
            "orders": {"orders-1": ORDERS_SECRET},
            "stranger": {"stranger-1": STRANGER_SECRET},
        }
    )
    return _caller.Built(0, "neolith.callers.SharedSecretBackend", backend)


class Abstains(CallerBackend):
    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        return None


class Fails(CallerBackend):
    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        raise RuntimeError("the backend is down")


class Returns(CallerBackend):
    def __init__(self, found: object) -> None:
        self.found = found

    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        return self.found  # type: ignore[return-value]  # the case under test


class Refuses(CallerBackend):
    def __init__(self, message: str) -> None:
        self.message = message

    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        raise CredentialRefused(self.message)


class Watches(CallerBackend):
    """Names ``orders`` and notes what of the request had been read."""

    def __init__(self, clock: "Clock | None" = None, cost: float = 0) -> None:
        self.read: list[bool] = []
        self.filters: list[object] = []
        self.clock = clock
        self.cost = cost

    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        self.read.append(hasattr(request, "_body") or request._read_started)  # type: ignore[attr-defined]  # Django's own state
        self.filters.append(getattr(request, "exception_reporter_filter", None))
        if self.clock is not None:
            self.clock.time += self.cost
        return "orders"


def built(backend: CallerBackend, position: int = 0) -> _caller.Built:
    return _caller.Built(position, f"tests.{type(backend).__name__}", backend)


class Clock:
    """A clock the test sets: each reading returns ``time``."""

    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


@dataclass
class Site:
    books: Books
    backends: list[_caller.Built] = field(default_factory=list)


def publish(monkeypatch: pytest.MonkeyPatch) -> Site:
    """The test host, with the ledger provided by app ``ledger``, which
    admits a principal from ``orders`` only, and the shared-secret backend.
    The table is built again on each read, so a test may change the
    backends first.
    """
    books = Books()
    provider = InProcess(Ledger, books, app="ledger", principal_from=("orders",))
    endpoints = _endpoint.table({LEDGER: Connector(Ledger, provider)})
    made = Site(books, [shared()])
    current = _host._current()
    assert current is not None
    monkeypatch.setattr(
        _host,
        "_current",
        lambda: dataclasses.replace(
            current,
            endpoints=endpoints,
            callers=_caller.Callers(tuple(made.backends)),
        ),
    )
    return made


@dataclass
class Served:
    status: int
    headers: dict[str, list[str]]
    body: bytes

    def header(self, name: str) -> str | None:
        found = self.headers.get(name.lower())
        return None if found is None else ",".join(found)

    def error(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.body)
        return value


Fields = list[tuple[str, str]]


def _environ(
    method: str, path: str, query: str, fields: Fields, body: bytes
) -> dict[str, Any]:
    """The environ wsgiref and Django's server build for these fields."""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SCRIPT_NAME": "",
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    # Django's server drops every name with "_"; wsgiref keeps the first
    # Content-Type and Content-Length, reports a missing Content-Type as
    # text/plain, and joins every other repeated field with ",".
    kept = [(n, v) for n, v in fields if "_" not in n]
    types = [v for n, v in kept if n.lower() == "content-type"]
    environ["CONTENT_TYPE"] = (
        types[0].encode().decode("latin-1") if types else "text/plain"
    )
    lengths = [v for n, v in kept if n.lower() == "content-length"]
    if lengths and lengths[0]:
        environ["CONTENT_LENGTH"] = lengths[0].encode().decode("latin-1")
    for name, value in kept:
        key = name.replace("-", "_").upper()
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            continue
        key = f"HTTP_{key}"
        # The server reads bytes, which WSGI hands on decoded as Latin-1.
        value = value.strip().encode().decode("latin-1")
        environ[key] = f"{environ[key]},{value}" if key in environ else value
    return environ


class Started:
    status = ""
    headers: list[tuple[str, str]]

    def __call__(
        self, status: str, headers: list[tuple[str, str]], exc_info: object = None, /
    ) -> Callable[[bytes], object]:
        self.status = status
        self.headers = headers
        return lambda data: None


def _headers(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, value in pairs:
        found.setdefault(name.lower(), []).append(value)
    return found


def wsgi(method: str, path: str, query: str, fields: Fields, body: bytes) -> Served:
    started = Started()
    result = WSGIHandler()(_environ(method, path, query, fields, body), started)
    try:
        content = b"".join(result)
    finally:
        result.close()
    return Served(int(started.status.split()[0]), _headers(started.headers), content)


async def _asgi(
    method: str, path: str, query: str, fields: Fields, body: bytes
) -> Served:
    sent: list[Mapping[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(n.lower().encode(), v.encode()) for n, v in fields],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await ASGIHandler()(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    content = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    pairs = [(n.decode("latin-1"), v.decode("latin-1")) for n, v in start["headers"]]
    return Served(start["status"], _headers(pairs), content)


def asgi(method: str, path: str, query: str, fields: Fields, body: bytes) -> Served:
    return asyncio.run(_asgi(method, path, query, fields, body))


ENTRY = b'{"amount":5}'


def call(
    mode: str,
    *,
    path: str = POST,
    method: str = "POST",
    query: str = "",
    body: bytes = ENTRY,
    authorization: str | None = "orders",
    fields: Fields | None = None,
    content_type: str | None = "application/json",
    content_length: str | None = "auto",
) -> Served:
    """Send a call: by default a valid one from ``orders``."""
    sent: Fields = [("Host", "testserver")]
    if authorization == "orders":
        sent.append(("Authorization", token()))
    elif authorization is not None:
        sent.append(("Authorization", authorization))
    if content_type is not None:
        sent.append(("Content-Type", content_type))
    if content_length == "auto":
        sent.append(("Content-Length", str(len(body))))
    elif content_length is not None:
        sent.append(("Content-Length", content_length))
    sent += fields or []
    serve = wsgi if mode == "wsgi" else asgi
    return serve(method, path, query, sent, body)


def refused(served: Served, status: int, kind: str, message: str) -> None:
    assert served.status == status, served
    assert served.header("Content-Type") == "application/json"
    assert served.error() == {"kind": kind, "message": message}


def set_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    made = Clock()
    monkeypatch.setattr(_context, "now", made)
    return made


def capture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger="neolith.calls")
    caplog.set_level(logging.DEBUG, logger="django.neolith")
    caplog.set_level(logging.DEBUG, logger="django.request")
    return caplog
