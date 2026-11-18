"""The HTTP transport: a connector's calls to a remote provider (ADR 0014).

The transport carries one contract version's calls to a provider's origin
with the client of ``neolith._http`` (ADR 0028). Before it sends anything it
refuses a call that has no calling app, with the in-process transport's
message, and one from an app that has no signing key (ADR 0027 Decision 9).
Once the client has a connection ready, it writes the call-context fields
in the admitted order, with a token minted for that attempt and the budget
read from the call's deadline then (ADR 0028 Decision 2).

It maps the final response as spec 03 and ADR 0011 Decision 4 say, taking
the kind from the body only, never from the status:

- 200 is the reply's text and 204 an empty reply; the connector refuses the
  other pairing, and any text that fails the network receive check, as
  ``internal``;
- any other success status is not a reply, so ``internal``;
- any other status with a neolith error body is that error: the body is
  read as a network receiver reads text (CON-42, its nesting judged before
  the json module reads it), has exactly the members ``kind``, ``message``
  and, when it carries a detail, ``data``, and its detail is handed on as
  the text the provider wrote; a body that breaks those rules is
  ``internal``, and one that is not UTF-8 JSON text carries none;
- any other status without one is ``unavailable``, and so is every failure
  of the client to carry the call, which is logged once at ``WARNING``.

``over_http`` is a private test seam (ADR 0014 Decision 6): while it is
entered, calls for one identity go over HTTP to an origin.
"""

import contextlib
import ipaddress
import json
import logging
import re
import secrets
import threading
from collections.abc import Iterator, Sequence
from typing import Any, Final, final

from neolith import _caller, _context, _http, _naming
from neolith._connector import Connector, Failure, no_calling_app
from neolith._context import Sent, UsageError
from neolith._contracts import Action, Contract
from neolith._crossing import Receiver, _interchange

__all__ = ["HTTPTransport", "origin_problem", "over_http", "seamed"]

_log = logging.getLogger("neolith")

# The members of a neolith error body (spec 03, Errors).
_ERROR_MEMBERS: Final = frozenset({"kind", "message", "data"})
# JSON's whitespace (RFC 8259, section 2).
_WHITESPACE: Final = re.compile(r"[ \t\n\r]*")


@final
class HTTPTransport:
    """The HTTP transport to one contract version's remote provider.

    It is the value a remote provider's entry gives under ``BACKEND``
    (``neolith.transports.HTTPTransport``, ADR 0014 Decision 4). Its
    constructor and methods are private in 0.1: an adopter names it in
    settings and never builds or subclasses it (ADR 0014 Decision 7).
    """

    __slots__ = ("_callers", "_contract", "_pool", "_targets")

    #: The reply's and an error's text come from a network.
    receiver = Receiver.NETWORK

    def __init__(
        self, declaration: type[Any], pool: _http.Pool, callers: _caller.Callers
    ) -> None:
        """Carry the calls of ``declaration`` to the origin of ``pool``,
        signing each with the calling app's key in ``callers``.
        """
        contract = Contract.of(declaration)
        if type(pool) is not _http.Pool:
            raise TypeError(f"a pool is a Pool, not {_naming.type_name(pool)}")
        if type(callers) is not _caller.Callers:
            raise TypeError(
                f"the signing keys are Callers, not {_naming.type_name(callers)}"
            )
        self._contract = contract
        self._pool = pool
        self._callers = callers
        self._targets = {
            name: f"/{contract.name}/v{contract.major}/{name}"
            for name in contract.actions
        }

    @property
    def contract(self) -> Contract:
        """The contract version whose calls this transport carries."""
        return self._contract

    @property
    def origin(self) -> str:
        """The provider's origin, as configured."""
        return self._pool.origin.url

    def __repr__(self) -> str:
        return f"<HTTP transport of {self._contract.identity} to {self.origin}>"

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        """Send the call once and return the reply's text, or ``None`` for an
        empty reply.

        Every outcome that is not a reply is a ``Failure``, raised outside
        any exception handler, so it carries nothing of the client. A
        response that is a reply of no kind the connector reads, or a neolith
        error body that cannot be read, is a ``ValueError``, which the
        connector logs and gives its caller as ``internal``. An action of
        another contract is a ``TypeError``.
        """
        identity = self._contract.identity
        if self._contract.actions.get(action.name) is not action:
            raise TypeError(
                f"{identity} has no action {action.name!r} of this declaration"
            )
        if type(context) is not Sent:
            raise TypeError(
                f"a call's context is sent as Sent, not {_naming.type_name(context)}"
            )
        if type(request) is not bytes:
            raise TypeError(f"the request is {_naming.type_name(request)}, not text")
        caller = context.caller
        # Refused before anything is sent: a call with no app has nothing to
        # sign for, and one from an app with no key would be refused.
        if caller is None:
            raise Failure("unauthenticated", no_calling_app(identity))
        if not any(secret.app == caller for secret in self._callers.signing):
            raise Failure("unauthenticated", _caller.no_signing_key(caller, identity))
        error: _http.ClientError | None = None
        try:
            response = _http.exchange(
                self._pool,
                self._targets[action.name],
                lambda: self._fields(caller, context),
                request,
                context.deadline,
            )
        except _http.ClientError as failed:
            error = failed
        if error is not None:
            if error.kind == "unavailable":
                self._warn(
                    action, context, f"{error.message} ({error.phase}; {error.cause})"
                )
            failure = Failure(error.kind, error.message)
            del error
            raise failure
        return self._outcome(action, context, response)

    def _fields(self, caller: str, sent: Sent) -> Sequence[tuple[str, str]] | None:
        """The call-context fields of one attempt, in the admitted order, or
        ``None`` when the budget, read now, is under 1 ms.

        The client calls this once a connection is ready, and again for its
        one replacement connection, so each attempt has its own token and a
        budget read as it is written (ADR 0028 Decisions 2 and 5).
        """
        budget = _context.budget(sent.deadline, _context.now())
        if budget is not None and budget < 1:
            return None
        token = self._callers.mint(caller, self._contract.identity)
        if token is None:  # the keys are fixed for a set-up; never reached
            raise ValueError(f"app {caller} has no signing key")
        fields = [("Authorization", token.authorization())]
        if sent.principal is not None:
            fields.append(("Neolith-Principal", _context.b64url(sent.principal)))
        if budget is not None:
            fields.append(("Neolith-Timeout", str(budget)))
        fields.append(("Idempotency-Key", _sf_string(sent.idempotency_key)))
        fields.append(("traceparent", f"00-{sent.trace_id}-{_span()}-00"))
        if sent.tracestate:
            fields.append(("tracestate", sent.tracestate))
        return fields

    def _outcome(
        self, action: Action, sent: Sent, response: _http.Response
    ) -> bytes | None:
        """Map a final response as spec 03 says."""
        status = response.status
        if status == 200:
            return response.body
        if status == 204:
            return None
        if 200 <= status < 300:
            raise ValueError(f"the provider answered {status}, which is not a reply")
        error = _error_body(response.body)
        if error is None:
            message = (
                f"the provider at {self.origin} answered {status} without a neolith "
                "error body"
            )
            self._warn(action, sent, message)
            raise Failure("unavailable", message)
        raise Failure(*error)

    def _warn(self, action: Action, sent: Sent, what: str) -> None:
        """Log an ``unavailable`` once, never raising."""
        with contextlib.suppress(Exception):
            _log.warning(
                "%s, action %r: %s (trace %s)",
                self._contract.identity,
                action.name,
                what,
                sent.trace_id,
            )


def _sf_string(value: str) -> str:
    """An RFC 9651 String (section 4.1.6): quoted, with each backslash and
    double quote escaped by a backslash.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _span() -> str:
    """A new span identifier: 16 lower-case hex digits, never all zeros."""
    while True:
        span = secrets.token_hex(8)
        if span != "0" * 16:
            return span


def _error_body(body: bytes) -> tuple[str, str, bytes | None] | None:
    """The kind, message and detail text of a neolith error body, or ``None``
    when ``body`` carries none.

    A body carries one when it is JSON text, UTF-8 and within the nesting a
    reader admits, whose value is an object with a member ``kind`` written
    as a string (spec 03), judged on the members as written, so a name
    written twice cannot hide one. Such a body that is not I-JSON (CON-42),
    or holds a member other than ``kind``, ``message`` and ``data``, or no
    ``message`` that is a string, raises ``ValueError``: it cannot be read,
    so the caller gets ``internal``. The detail is the text of ``data``
    exactly as the provider wrote it, or ``None`` when ``data`` is absent.
    """
    value, broken, _ = _interchange(body)
    if not isinstance(value, dict):
        return None
    text = body.decode("utf-8")
    members = _members(text)
    if not any(name == "kind" and text[start] == '"' for name, start, _ in members):
        return None
    if broken is not None:
        raise ValueError(f"the neolith error body {broken}")
    if not _ERROR_MEMBERS.issuperset(value) or type(value.get("message")) is not str:
        raise ValueError(
            "the neolith error body does not have exactly the members kind, "
            "message and, for a detail, data"
        )
    detail = next(
        (
            text[start:end].encode("utf-8")
            for name, start, end in members
            if name == "data"
        ),
        None,
    )
    return value["kind"], value["message"], detail


def _members(text: str) -> list[tuple[str, int, int]]:
    """Each member of a JSON object's text, in order: its name, and where its
    value starts and ends.

    The text has been read as JSON already, so it is an object and its
    nesting is bounded. Numbers are kept as written, so none is converted.
    """
    decoder = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=str)

    def skip(at: int) -> int:
        found = _WHITESPACE.match(text, at)
        return at if found is None else found.end()

    found: list[tuple[str, int, int]] = []
    at = skip(skip(0) + 1)  # past the opening brace
    while text[at] != "}":
        name, at = decoder.raw_decode(text, at)
        at = skip(skip(at) + 1)  # past the colon
        _, end = decoder.raw_decode(text, at)
        found.append((name, at, end))
        at = skip(end)
        if text[at] == ",":
            at = skip(at + 1)
    return found


# A remote provider's url (ADR 0014 Decision 3, the admitted set), and the
# reasons a value is not one, in the order they are judged.
SCHEME: Final = "the scheme is http or https, in lower case"
CREDENTIAL: Final = (
    "it carries a user or a password; a remote provider's URL holds no credential"
)
PATH: Final = (
    "it has a path, a query or a fragment; contract paths sit at the root of the "
    "provider's origin (spec 03)"
)
CASE: Final = "the host is not in lower-case ASCII; write a name in its ASCII form"
NAME: Final = (
    "a host name is labels of letters, digits and hyphens joined by dots, each 1 "
    "to 63 characters, neither starting nor ending with a hyphen, at most 253 in "
    "all, with no trailing dot; a provider refuses any other Host"
)
IPV4: Final = (
    "the host ends in a number, so it is read as an IPv4 address; write the "
    "address in dotted decimal, without leading zeros"
)
IPV6: Final = (
    "the host is not a valid IPv6 address, or is not in its compressed form, or "
    "has a zone, or is IPv4-mapped; write an IPv4-mapped address as its IPv4 "
    "address"
)
PORT: Final = "the port is 1 to 65535, without a leading zero"
DEFAULT_PORT: Final = "leave out the scheme's default port (80 for http, 443 for https)"
_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_NUMBER: Final = re.compile(r"[0-9]+|0x[0-9a-f]*", re.ASCII)
_PORT: Final = re.compile(r"[1-9][0-9]{0,4}", re.ASCII)
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def origin_problem(url: str) -> str | None:
    """Why ``url`` is not a remote provider's url, or ``None`` when it is.

    A url is admitted exactly when it is the ASCII serialisation of its own
    origin, within ADR 0014's grammar: ``http`` or ``https``, a host name
    of RFC 1123 labels, a dotted-decimal IPv4 address or a compressed IPv6
    address that is neither zoned nor IPv4-mapped, and a port that is not
    the scheme's default. A label that begins with ``xn--`` is taken as
    written, never decoded: an A-label keeps its spelling under the URL
    Standard, while a Fake A-label such as ``xn--zz``, whose host the URL
    Standard refuses, is admitted and fails at the first call, as a
    misspelt host name does (ADR 0014, fact 11). The reason is fixed text,
    the first of ADR 0014's list that applies, and never shows any part of
    the value.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or scheme not in _DEFAULT_PORTS:
        return SCHEME
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in authority:
        return CREDENTIAL
    if authority != rest:
        return PATH
    if authority.startswith("["):
        host, bracket, after = authority.partition("]")
        host += bracket
        if after and not after.startswith(":"):
            return PORT
        port = after[1:] if after else None
    else:
        host, colon, written = authority.partition(":")
        port = written if colon else None
    if not host.isascii() or host != host.lower():
        return CASE
    problem = _host_problem(host)
    if problem is not None:
        return problem
    if port is not None:
        if not _PORT.fullmatch(port) or int(port) > 65535:
            return PORT
        if int(port) == _DEFAULT_PORTS[scheme]:
            return DEFAULT_PORT
    return None


def _host_problem(host: str) -> str | None:
    """Why a lower-case ASCII host is not one the grammar admits."""
    if host.startswith("["):
        inner = host[1:-1] if host.endswith("]") else ""
        try:
            address = ipaddress.IPv6Address(inner) if "%" not in inner else None
        except ValueError:
            address = None
        if address is None or str(address) != inner or address.ipv4_mapped:
            return IPV6
        return None
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        return NAME
    if _NUMBER.fullmatch(labels[-1]):
        try:
            written = str(ipaddress.IPv4Address(host))
        except ValueError:
            return IPV4
        if written != host:
            return IPV4
    return None


# The connectors over_http stands in for the set-up's, by identity.
_SEAMS: dict[str, Connector] = {}
_SEAMS_LOCK: Final = threading.Lock()


def seamed(identity: str) -> Connector | None:
    """The connector ``over_http`` has entered for ``identity``, or ``None``."""
    return _SEAMS.get(identity)


@contextlib.contextmanager
def over_http(identity: str, origin: str) -> Iterator[None]:
    """Send every call for ``identity`` over HTTP to ``origin`` while entered.

    A private test seam (ADR 0014 Decision 6), never reached from settings:
    the calls go through the HTTP transport to ``origin``, whether an app of
    this host provides ``identity`` or not, signed with the set-up's keys.
    It brings a pool for ``origin`` and closes it on exit; the set-up's
    transport is back on exit. It refuses with ``UsageError`` a host that is
    not set up, an identity the host does not know, an origin outside a
    remote provider's url grammar, an identity required by a hosted app that
    has no signing key, and an identity already entered.
    """
    # The host's table is read here, after set-up, to avoid importing the
    # host when this module is.
    from django.apps import apps

    from neolith import _host
    from neolith._declaration import declaration_of

    if type(identity) is not str or type(origin) is not str:
        raise TypeError("over_http takes a contract identity and an origin, as str")
    host, installed = _host._table()
    if host is None or not installed:
        raise UsageError(f"over_http({identity!r}): the host is not set up")
    if identity not in host.known:
        raise UsageError(f"over_http: this host does not know {identity}")
    problem = origin_problem(origin)
    if problem is not None:
        raise UsageError(
            f"over_http({identity!r}): the origin is not an origin: {problem}"
        )
    contract: Contract | None = None
    for config in apps.get_app_configs():
        declaration = declaration_of(type(config))
        if declaration is None:
            continue
        named = [
            *(provision.contract for provision in declaration.provides),
            *declaration.requires,
        ]
        contract = next((c for c in named if c.identity == identity), contract)
        if identity in (c.identity for c in declaration.requires) and not any(
            secret.app == declaration.label for secret in host.callers.signing
        ):
            raise UsageError(
                f"over_http({identity!r}): "
                + _caller.no_signing_key(declaration.label, identity)
            )
    if contract is None:  # every known identity is named by an app
        raise UsageError(f"over_http: this host does not know {identity}")
    pools = _http.Pools()
    try:
        transport = HTTPTransport(
            contract.declaration, pools.pool(_http.Origin.parse(origin)), host.callers
        )
        connector = Connector(contract.declaration, transport)
        with _SEAMS_LOCK:
            if identity in _SEAMS:
                raise UsageError(f"over_http({identity!r}) is already entered")
            _SEAMS[identity] = connector
    except BaseException:
        pools.close()
        raise
    try:
        yield
    finally:
        with _SEAMS_LOCK:
            if _SEAMS.get(identity) is connector:
                del _SEAMS[identity]
        pools.close()
