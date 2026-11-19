"""The wire part: one transport driven against bytes the specification fixes
(ADR 0032's Decisions 2 and 7).

``test_provider``, ``test_projection`` and ``test_idempotency`` drive
neolith's provider, the package's live server, with the suite's own caller:
the standard library's ``http.client``, writing each request as spec 03
fixes it, with a token this module mints by spec 03's rules.
``test_connector`` drives neolith's connector against the suite's own
scripted provider: a server on a loopback socket that answers with bytes
the test writes, and records the bytes it receives.

Nothing here imports neolith. What a case expects comes from spec 03's
rules, written here with the standard library, and from the bytes each test
writes, so the oracle never shares code with the implementation it checks.
"""

import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from django.conf import settings

# A bound on every socket wait, far longer than any answer takes.
BOUND: Final = 10.0
CALLER: Final = "reference_caller"
STRANGER: Final = "reference_stranger"
IDENTITY: Final = "reference.v1"
OPENAPI: Final = "/.well-known/neolith/openapi.json"
HEALTH: Final = "/.well-known/neolith/health"
# The principal of spec 03's own example, with the claim roles holding
# ["clerk"], and its header value there: the base64url of its sent form, the
# JCS serialization of the principal object.
ISSUER: Final = "https://id.example.com"
SUBJECT: Final = "u-81f3"
PRINCIPAL_HEADER: Final = (
    "eyJjbGFpbXMiOnsicm9sZXMiOlsiY2xlcmsiXX0sImlzc3VlciI6Imh0dHBzOi8vaWQuZXhh"
    "bXBsZS5jb20iLCJzdWJqZWN0IjoidS04MWYzIn0"
)
# Spec 03's error table.
STATUS: Final = {
    "invalid_argument": 400,
    "unauthenticated": 401,
    "permission_denied": 403,
    "not_found": 404,
    "conflict": 409,
    "timeout": 504,
    "unavailable": 503,
    "internal": 500,
}
DECLARED: Final = 422


def b64url(data: bytes) -> str:
    """base64url with no padding (RFC 4648, section 5)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


_B64URL: Final = re.compile(r"[A-Za-z0-9_-]*")


def unb64url(text: str) -> bytes | None:
    """The bytes of base64url text under spec 03's rules (alphabet only, no
    padding, zero unused bits), or ``None``.
    """
    if not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        return None
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    return data if b64url(data) == text else None


class NotJSON(ValueError):
    """Text that is not I-JSON (RFC 7493)."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise NotJSON(f"a member name given twice among {names}")
    return dict(pairs)


def _constant(name: str) -> object:
    raise NotJSON(f"{name} is not JSON")


def read_json(data: bytes) -> Any:  # noqa: ANN401
    """JSON text read strictly: UTF-8, no member name twice, no ``NaN`` or
    infinity, and numbers as exact decimals, so that ``true`` is never ``1``
    and no number is rounded.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NotJSON("not UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=Decimal,
        )
    except json.JSONDecodeError as error:
        raise NotJSON(str(error)) from error


def secret_of(app: str) -> tuple[str, bytes]:
    """The key id and secret ``app`` signs with on the test host: 43
    base64url characters, which are 32 bytes (ADR 0027).
    """
    table: Any = settings.NEOLITH
    entry: dict[str, str] = table["SIGNING_SECRETS"][app]
    key = unb64url(entry["SECRET"])
    assert key is not None
    assert len(key) == 32
    return entry["KEY_ID"], key


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def mint(
    app: str,
    audience: str = IDENTITY,
    *,
    header: dict[str, object] | None = None,
    claims: dict[str, object] | None = None,
    key: bytes | None = None,
    signer: str | None = None,
    shift: int = 0,
    lifetime: int = 300,
) -> str:
    """A shared-secret token for ``app`` and ``audience``, by spec 03's
    table: HS256 over the first two parts as sent, issued ``shift`` seconds
    from now and expiring ``lifetime`` seconds later. ``signer`` signs with
    another app's key id and secret; ``header`` and ``claims`` replace or
    add members, and a member given ``None`` is left out.
    """
    key_id, secret = secret_of(signer or app)
    iat = int(time.time()) + shift
    head = {"alg": "HS256", "typ": "neolith+jwt", "kid": key_id, **(header or {})}
    body = {"iss": app, "aud": audience, "iat": iat, "exp": iat + lifetime}
    body.update(claims or {})
    signing = ".".join(
        b64url(_json({k: v for k, v in part.items() if v is not None}))
        for part in (head, body)
    )
    signature = hmac.new(key or secret, signing.encode("ascii"), hashlib.sha256)
    return f"{signing}.{b64url(signature.digest())}"


def token_problem(
    authorization: str,
    app: str,
    audience: str,
    *,
    key: tuple[str, bytes] | None = None,
    now: int | None = None,
) -> str | None:
    """Why ``authorization`` is not a valid shared-secret credential of
    ``app`` for ``audience`` under spec 03's rules, or ``None``: with
    ``key``, a key id and its secret, the key ``app`` signs with on the test
    host by default, at ``now``, the current time by default.
    """
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return f"the scheme is {scheme!r}"
    if len(token.encode()) > 4096:
        return "the token is over 4096 bytes"
    parts = token.split(".")
    if len(parts) != 3:
        return f"the token has {len(parts)} parts"
    decoded = [unb64url(part) for part in parts]
    if any(part is None for part in decoded):
        return "a part is not base64url"
    try:
        head, body = (read_json(part) for part in decoded[:2] if part is not None)
    except NotJSON as error:
        return f"a part is not I-JSON: {error}"
    if not (isinstance(head, dict) and isinstance(body, dict)):
        return "a part is not a JSON object"
    key_id, secret = key or secret_of(app)
    if head != {"alg": "HS256", "typ": "neolith+jwt", "kid": key_id}:
        return f"the header is {head}"
    if set(body) != {"iss", "aud", "iat", "exp"}:
        return f"the claims are {sorted(body)}"
    if body["iss"] != app:
        return f"iss is {body['iss']!r}"
    if body["aud"] != audience:
        return f"aud is {body['aud']!r}"
    iat, exp = body["iat"], body["exp"]
    if not all(type(t) is int for t in (iat, exp)):
        return "iat and exp are not integers"
    now = int(time.time()) if now is None else now
    if not iat < exp:
        return f"exp {exp} is not after iat {iat}"
    if not exp <= iat + 300:
        return f"exp {exp} is more than 300 s after iat {iat}"
    if not iat <= now + 60:
        return f"iat {iat} is more than 60 s after now, {now}"
    if not now < exp + 60:
        return f"exp {exp} is 60 s or more before now, {now}"
    expected = hmac.new(
        secret, f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(decoded[2] or b"", expected):
        return "the signature is not the HMAC SHA-256 of the first two parts"
    return None


# The suite's own caller.


@dataclass(frozen=True)
class Answer:
    """A response as ``http.client`` read it: the status, every field in
    the order received, and the body's bytes.
    """

    status: int
    fields: tuple[tuple[str, str], ...]
    body: bytes

    def values(self, name: str) -> list[str]:
        """Each value of the fields called ``name``, ignoring ASCII case."""
        return [v for n, v in self.fields if n.lower() == name.lower()]

    def field(self, name: str) -> str | None:
        """The value of the one field called ``name``, or ``None``."""
        found = self.values(name)
        assert len(found) <= 1, f"{name} given {len(found)} times: {self}"
        return found[0] if found else None

    def error(self) -> dict[str, Any]:
        """The neolith error body: JSON with a string ``kind``, as
        ``application/json``.
        """
        assert self.field("Content-Type") == "application/json", self
        assert self.field("Content-Length") == str(len(self.body)), self
        body = read_json(self.body)
        assert isinstance(body, dict), self
        assert isinstance(body.get("kind"), str), self
        return body


def send(
    origin: str,
    method: str,
    target: str,
    fields: Sequence[tuple[str, str]],
    body: bytes | None = None,
) -> Answer:
    """Write one request with exactly ``fields``, on a connection of its
    own, and read the response.
    """
    host, _, port = origin.removeprefix("http://").rpartition(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=BOUND)
    try:
        connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in fields:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read()
        return Answer(response.status, tuple(response.getheaders()), data)
    finally:
        connection.close()


@dataclass(frozen=True)
class Caller:
    """The suite's own caller, for the live server at ``origin``. After
    each request it waits until the server holds no open connection, so a
    case ends only once the provider's side has.
    """

    origin: str
    settle: Callable[[], None]

    @property
    def host(self) -> str:
        return self.origin.removeprefix("http://")

    def request(
        self,
        method: str,
        target: str,
        fields: Sequence[tuple[str, str]] = (),
        body: bytes | None = None,
    ) -> Answer:
        try:
            return send(
                self.origin, method, target, [("Host", self.host), *fields], body
            )
        finally:
            self.settle()

    def call(
        self,
        action: str,
        body: bytes = b"{}",
        *,
        app: str = CALLER,
        fields: Sequence[tuple[str, str]] = (),
        method: str = "POST",
        query: str = "",
    ) -> Answer:
        """Call ``action`` of the reference contract as spec 03 fixes a
        request, signed for ``app``, with ``fields`` after the fixed ones.
        """
        sent = [
            ("Accept-Encoding", "identity"),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Authorization", f"Bearer {mint(app)}"),
            *fields,
        ]
        target = f"/reference/v1/{action}" + (f"?{query}" if query else "")
        return self.request(method, target, sent, body)


# The suite's own scripted provider.


def response(status: int, body: bytes = b"", *, reason: str = "X") -> bytes:
    """A response with its ``Content-Length``."""
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def bare(status: int, reason: str = "X") -> bytes:
    """A response with no body and no ``Content-Length``, for a status
    that has none (204, 304).
    """
    return f"HTTP/1.1 {status} {reason}\r\n\r\n".encode()


@dataclass(frozen=True)
class Received:
    """One request as the scripted provider read it."""

    line: str
    fields: tuple[tuple[str, str], ...]
    body: bytes

    def field(self, name: str) -> str | None:
        found = [v for n, v in self.fields if n.lower() == name.lower()]
        assert len(found) <= 1, f"{name} given {len(found)} times"
        return found[0] if found else None


def _read(connection: socket.socket) -> Received | None:
    """One request, framed by its ``Content-Length``, or ``None`` once the
    caller has closed the connection.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            return None
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    line, *lines = head.decode("latin-1").split("\r\n")
    fields = tuple(
        (name, value.strip(" \t"))
        for name, _, value in (entry.partition(":") for entry in lines)
    )
    lengths = [v for n, v in fields if n.lower() == "content-length"]
    single = len(lengths) == 1 and re.fullmatch("[0-9]+", lengths[0])
    length = int(lengths[0]) if single else 0
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        body += chunk
    return Received(line, fields, body)


@dataclass
class Scripted:
    """A provider on ``127.0.0.1`` that answers each request it reads with
    the next of ``responses``, on as many connections as the caller opens,
    and stops once it has sent them all.
    """

    responses: Sequence[bytes]
    received: list[Received] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)

    def serve(self, server: socket.socket) -> None:
        left = list(self.responses)
        while left:
            try:
                connection, _ = server.accept()
            except OSError as error:
                self.errors.append(error)
                return
            with connection:
                connection.settimeout(BOUND)
                try:
                    while left:
                        request = _read(connection)
                        if request is None:
                            break
                        self.received.append(request)
                        connection.sendall(left.pop(0))
                except OSError as error:
                    self.errors.append(error)
                    return


@contextlib.contextmanager
def scripted(*responses: bytes) -> Iterator[tuple[str, Scripted]]:
    """A scripted provider playing ``responses``, while entered, and its
    origin. On exit it waits for the provider's thread, within ``BOUND``.
    """
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(BOUND)
    provider = Scripted(responses)
    thread = threading.Thread(
        target=provider.serve, args=(server,), name="scripted-provider", daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}", provider
    finally:
        # Longer than the provider's own wait for a connection.
        thread.join(2 * BOUND)
        server.close()
        assert not thread.is_alive(), "the scripted provider is still serving"
    assert not provider.errors, f"the scripted provider failed: {provider.errors}"
