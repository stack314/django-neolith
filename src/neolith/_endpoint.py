"""The host's contract endpoints: it answers every path it owns itself
(spec 03; ADR 0011, as amended for step 5; ADR 0027).

The host owns every path that starts with ``/<name>/v<N>/`` for a contract
version it provides, or with ``/.well-known/neolith/``, compared on the full,
percent-decoded ``request.path``. The root middleware hands each request on
such a path to ``answer`` (or ``aanswer`` in async mode), which never calls
the rest of the middleware chain. On an owned path:

- ``request.get_host()`` runs first, so a ``Host`` outside ``ALLOWED_HOSTS``
  raises ``DisallowedHost`` and Django answers it as it answers any request;
- the two well-known paths answer ``GET`` and ``HEAD``: the OpenAPI document
  set-up built, as fixed bytes, and an empty 200 for health;
- any other path that is not a contract path gets an empty 404.

On every owned path the host first sets a reporter filter on the request
that hides every ``HTTP_NEOLITH_`` key, so no report made with the request,
``DisallowedHost``'s included, shows the end user's principal. On a contract
path it takes the call's arrival and then runs ADR
0011's table in the order of ADR 0017 Decision 9: the caller backends
(step 1), the allow-list (step 2), the method, the query, the scheme and the
call-context headers (step 3), the deadline (step 4), and the body's header
fields and the body (step 5), the body last, with the provider side shared
with the in-process transport (``neolith._connector.Provider``). Every
outcome is a response with spec 03's status and error body, logged once on
``neolith.calls`` and marked logged, so Django's ``log_response`` adds no
line with the request; ``internal``'s cause is logged on ``django.neolith``.

Each message names the rule and never a value that arrived.
"""

import contextlib
import functools
import json
import logging
import re
import sys
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, final

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, RequestDataTooBig
from django.core.handlers.asgi import ASGIRequest
from django.http import HttpRequest, HttpResponse, UnreadablePostError
from django.views.debug import SafeExceptionReporterFilter
from django.views.decorators.debug import sensitive_variables

from neolith import _caller, _context, _crossing, _http, _openapi, _token
from neolith._connector import (
    _NOT_RUN,
    Failure,
    InProcess,
    Provider,
    internal_message,
)
from neolith._context import Arrived, Principal
from neolith._contracts import Action, _FrozenMap
from neolith._frozen import once

__all__ = [
    "HEALTH",
    "NO_ENDPOINTS",
    "OPENAPI",
    "Endpoints",
    "Route",
    "aanswer",
    "answer",
    "table",
]

# One line per contract response (ADR 0011 Decision 5).
_calls = logging.getLogger("neolith.calls")
# The cause of an internal on a contract path: a child of django, so Django's
# default logging mails it when DEBUG is false, as for a failing view.
_failures = logging.getLogger("django.neolith")

WELL_KNOWN: Final = "/.well-known/neolith/"
OPENAPI: Final = f"{WELL_KNOWN}openapi.json"
HEALTH: Final = f"{WELL_KNOWN}health"
_JSON: Final = "application/json"

# ADR 0011, the admitted set, and ADR 0027 Decision 8: the messages.
_METHOD: Final = "a contract action is called with POST"
_QUERY: Final = "a contract call has no query string"
_SCHEME: Final = "a contract call to this host is made over HTTPS (SECURE_SSL_REDIRECT)"
_PRINCIPAL_FORM: Final = (
    "Neolith-Principal: a principal object is base64url-encoded, in at most "
    f"{_context.PRINCIPAL_LIMIT} bytes"
)
_TIMEOUT_FORM: Final = "Neolith-Timeout: 1 to 10 ASCII digits, the first not 0"
_KEY_FORM: Final = (
    "Idempotency-Key: a structured-field String of 1 to "
    f"{_context.KEY_LIMIT} characters, with no parameters"
)
_CONTENT_TYPE: Final = "the request body is sent as Content-Type application/json"
_CONTENT_ENCODING: Final = "the request body is sent without Content-Encoding"
_CONTENT_LENGTH: Final = (
    "the request body is sent with one Content-Length of 1 to 19 digits"
)
_ENDED: Final = "the request body ended before its Content-Length"
_LONGER: Final = "the request body is longer than its Content-Length"
_CHALLENGE: Final = 'Bearer realm="neolith"'

_OWS: Final = " \t"
# RFC 9110, section 8.3.1, as ADR 0011's row 9 admits it: ASCII case only,
# so no Unicode case folding (such as U+017F for "s") admits another value.
_MEDIA_TYPE: Final = re.compile(
    r'application/json(?:[ \t]*;[ \t]*charset=(?:utf-8|"utf-8"))?',
    re.ASCII | re.IGNORECASE,
)
# HTTP-38: 1 to 19 ASCII digits, so neither this host nor Django's
# HttpRequest.body, which reads the field with int() too, meets Python's
# 4,300-digit limit on reading an int.
_DIGITS: Final = re.compile(r"[0-9]{1,19}")
_B64: Final = re.compile(r"[A-Za-z0-9_-]*")
_TIMEOUT: Final = re.compile(r"[1-9][0-9]{0,9}")
_KEY: Final = re.compile(r'"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\["\\])+"')
_ESCAPED: Final = re.compile(r"\\(.)")
# W3C Trace Context, section 3.2.2 and 3.2.4: version 00 is exactly these
# 55 characters; a later version may follow them with "-" and more.
_TRACEPARENT: Final = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<parent>[0-9a-f]{16})"
    r"-[0-9a-f]{2}(?P<rest>-[\x20-\x7e]*)?"
)
_NO_TRACE: Final = "0" * 32
_NO_PARENT: Final = "0" * 16
# W3C Trace Context, section 3.3.1: one list-member, as the client writes
# it, so whatever the host passes on is a value the client sends.
_TRACESTATE_MEMBER: Final = re.compile(_http._TS_MEMBER)
_TRACESTATE_MEMBERS: Final = 32
_TRACESTATE_LIMIT: Final = 512
_TRACESTATE_LONG: Final = 128
# The neolith headers step 3 reads, as META names them.
_PRINCIPAL: Final = "HTTP_NEOLITH_PRINCIPAL"
_TIMEOUT_KEY: Final = "HTTP_NEOLITH_TIMEOUT"
_NEOLITH: Final = "HTTP_NEOLITH_"
_KNOWN_NEOLITH: Final = frozenset({_PRINCIPAL, _TIMEOUT_KEY})
_HEADER_NAME: Final = re.compile(r"neolith-[a-z0-9-]+")
# An app's name as the log line shows a token's iss: longer ones are
# "malformed", so a crafted token cannot write a long line.
_APP_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


@final
class _Network(Provider):
    """The shared provider side as the HTTP endpoint runs it, which logs a
    failure of the provider on ``django.neolith`` (ADR 0011 Decision 5).
    """

    __slots__ = ()

    def _failed(self, action: Action, trace_id: str, failure: BaseException) -> None:
        _cause(
            f"{self.contract.identity}, action {action.name!r}: the provider "
            f"failed (trace {trace_id})",
            failure,
        )


def _network(provider: InProcess) -> _Network:
    """The provider side ``provider`` holds, with the same implementation,
    app, requirements and allow-list, logging as the endpoint does.
    """
    served = _Network.__new__(_Network)
    for name in Provider.__slots__:
        object.__setattr__(served, name, getattr(provider, name))
    return served


@final
@once
@dataclass(frozen=True, slots=True)
class Route:
    """One contract path: its contract version, its action and its provider."""

    identity: str
    action: Action
    provider: Provider


@final
@once
@dataclass(frozen=True, slots=True)
class Endpoints:
    """What one set-up serves: each provided version's prefix, each contract
    path, and the OpenAPI document's bytes.
    """

    prefixes: tuple[str, ...] = ()
    # Read-only, and copied and pickled as the table it sits on is.
    routes: Mapping[str, Route] = field(default_factory=_FrozenMap)
    document: bytes = b""

    def owns(self, path: str) -> bool:
        """Whether the host answers ``path`` itself (ADR 0011 Decision 2)."""
        return path.startswith(WELL_KNOWN) or path.startswith(self.prefixes)

    def shadowable(self) -> list[tuple[str, str]]:
        """Each contract path and each provided prefix, with its contract
        version, for Boot's ``neolith.W003``.
        """
        found = [(route.identity, path) for path, route in self.routes.items()]
        found += [(_identity_of(prefix), prefix) for prefix in self.prefixes]
        return sorted(found, key=lambda pair: (pair[0], pair[1]))


def _identity_of(prefix: str) -> str:
    name, _, version = prefix.strip("/").rpartition("/")
    return f"{name}.{version}"


def table(connectors: Mapping[str, object]) -> Endpoints:
    """The endpoints of a set-up whose connectors are ``connectors``: one
    route for each action of each contract version a hosted app provides.

    Raises:
        ImproperlyConfigured: The OpenAPI document cannot be built, which is
            a fault of neolith, never of an app.
    """
    routes: dict[str, Route] = {}
    prefixes: list[str] = []
    contracts = []
    for identity, connector in sorted(connectors.items()):
        transport = getattr(connector, "_transport", None)
        if type(transport) is not InProcess:
            continue
        provider = _network(transport)
        contract = provider.contract
        contracts.append(contract)
        prefix = f"/{contract.name}/v{contract.major}/"
        prefixes.append(prefix)
        for name, action in contract.actions.items():
            routes[f"{prefix}{name}"] = Route(identity, action, provider)
    try:
        document = _openapi.document(contracts)
    except ValueError as error:
        raise ImproperlyConfigured(
            "host: the OpenAPI document of the provided contracts cannot be "
            f"built: {error}; this is a fault of neolith, not of an app, so "
            "please report it"
        ) from None
    return Endpoints(tuple(prefixes), _FrozenMap(routes), document)


# A set-up with nothing provided: the well-known paths only.
NO_ENDPOINTS: Final = Endpoints()


class _Filter(SafeExceptionReporterFilter):
    """Django's reporter filter, which on a contract path also hides every
    ``HTTP_NEOLITH_`` key, the end user's principal among them, and hides
    marked locals whatever ``DEBUG`` says (ADR 0027 Decision 3).
    """

    hidden_settings = re.compile(
        f"{SafeExceptionReporterFilter.hidden_settings.pattern}|^{_NEOLITH}",
        re.IGNORECASE,
    )

    def is_active(self, request: HttpRequest | None) -> bool:
        """Always active on a contract path."""
        return True


def _protect(request: HttpRequest) -> None:
    """Give the request the reporter filter, before anything on an owned
    path can report it, ``DisallowedHost`` included, so whatever reports
    the request hides the neolith headers (ADR 0027 Decision 3).
    """
    # Django reads it with getattr (views/debug.py, 5.2 and 6.1).
    request.exception_reporter_filter = _Filter()


def _method(request: HttpRequest) -> str:
    """The request's method as it arrived: methods are case-sensitive (RFC
    9110, section 9.1), and Django upper-cases ``request.method``.
    """
    if isinstance(request, ASGIRequest):
        return str(request.scope["method"])
    return str(request.META["REQUEST_METHOD"])


def _cause(message: str, error: BaseException) -> None:
    """Log an internal's cause on ``django.neolith``, never raising, with no
    request attached.
    """
    with contextlib.suppress(Exception):
        _failures.error("%s", message, exc_info=error)


def _empty(status: int) -> HttpResponse:
    """A response with no body and no ``Content-Type``."""
    response = HttpResponse(status=status)
    del response["Content-Type"]
    return _framed(response)


def _framed(response: HttpResponse, length: int | None = None) -> HttpResponse:
    """State the body's length, ``length`` for a ``HEAD`` answer, as
    Django's ``CommonMiddleware`` does for a response it passes, since no
    middleware of the deployer runs on an owned path: so a server keeps the
    connection open for the caller's next call. A 204 states none (RFC 9110,
    section 8.6).
    """
    if response.status_code != 204:
        size = len(response.content) if length is None else length
        response["Content-Length"] = str(size)
    return response


def _other(request: HttpRequest, path: str, endpoints: Endpoints) -> HttpResponse:
    """The answer on an owned path that is not a contract path."""
    if path not in (OPENAPI, HEALTH):
        return _empty(404)
    method = _method(request)
    if method not in ("GET", "HEAD"):
        response = _empty(405)
        response["Allow"] = "GET, HEAD"
        return response
    if request.META.get("QUERY_STRING"):
        return _empty(400)
    if path == HEALTH:
        return _empty(200)
    document = endpoints.document
    response = HttpResponse(b"" if method == "HEAD" else document, content_type=_JSON)
    return _framed(response, len(document))


def answer(
    request: HttpRequest, endpoints: Endpoints, backends: Sequence[_caller.Built]
) -> HttpResponse:
    """Answer a request on a path the host owns, in sync mode.

    Raises:
        DisallowedHost: The request's ``Host`` is not one this host serves,
            for Django to answer.
    """
    arrived = _context.now()
    _protect(request)
    request.get_host()
    route = endpoints.routes.get(request.path)
    if route is None:
        return _other(request, request.path, endpoints)
    call = _Call(request, route, arrived)
    try:
        caller = _caller.identify(backends, request, route.identity)
    except Exception as error:
        return call.unidentified(error)
    return call.admitted(caller)


async def aanswer(
    request: HttpRequest, endpoints: Endpoints, backends: Sequence[_caller.Built]
) -> HttpResponse:
    """``answer`` in async mode: each backend's ``aauthenticate`` is awaited,
    and the rest, the response's log line included whatever the outcome,
    runs through ``sync_to_async`` on the request's own thread, as Django
    runs a sync view (ADR 0011 Decision 6).
    """
    arrived = _context.now()
    _protect(request)
    request.get_host()
    route = endpoints.routes.get(request.path)
    if route is None:
        return _other(request, request.path, endpoints)
    call = _Call(request, route, arrived)
    finish: Callable[[], HttpResponse]
    try:
        caller = await _caller.aidentify(backends, request, route.identity)
    except Exception as error:
        finish = functools.partial(call.unidentified, error)
    else:
        finish = functools.partial(call.admitted, caller)
    # Every outcome is logged, and may be mailed, so none is finished on the
    # event loop, where a slow log handler would hold every other request.
    return await sync_to_async(finish, thread_sensitive=True)()


class _Refused(Exception):
    """A refusal at steps 1 to 5: its kind, message and extra header."""

    def __init__(
        self, kind: str, message: str, header: tuple[str, str] | None = None
    ) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self.message = message
        self.header = header


class _Call:
    """One call on a contract path, from its arrival to its response."""

    __slots__ = ("_arrived", "_request", "_route", "_step", "_trace")

    def __init__(self, request: HttpRequest, route: Route, arrived: float) -> None:
        self._request = request
        self._route = route
        self._arrived = arrived
        # Until traceparent is read, a fresh identifier, made when needed.
        self._trace: str | None = None
        self._step = 1

    @property
    def trace_id(self) -> str:
        if self._trace is None:
            self._trace = _context._hex(16)
        return self._trace

    def unidentified(self, error: Exception) -> HttpResponse:
        """The response when no backend named the calling app (step 1)."""
        if type(error) is _caller.Refused:
            return self._refused(_Refused("unauthenticated", str(error)))
        if type(error) is _caller.Failed:
            _cause(f"{error} (trace {self.trace_id})", error)
        else:
            _cause(
                f"{self._route.identity}: the caller backends could not be "
                f"asked (trace {self.trace_id})",
                error,
            )
        return self._internal()

    def admitted(self, caller: str) -> HttpResponse:
        """Steps 2 to 6 for a call from ``caller``; never raises an
        ``Exception``.
        """
        try:
            return self._admitted(caller)
        except _Refused as refusal:
            return self._refused(refusal)
        except Exception as error:
            _cause(
                f"{self._route.identity}, action {self._route.action.name!r}: "
                f"the endpoint failed (trace {self.trace_id})",
                error,
            )
            return self._internal()

    # Its locals hold the request's headers and the principal: hidden from
    # a report of a failure's frames, such as an HTML error mail.
    @sensitive_variables()
    def _admitted(self, caller: str) -> HttpResponse:
        request, route = self._request, self._route
        meta = request.META
        # Step 2: the allow-list, judged on the header's presence alone.
        self._step = 2
        refusal = route.provider._refusal(caller, _PRINCIPAL in meta)
        if refusal is not None:
            raise _Refused(refusal.kind, refusal.message)
        # Step 3: the request line and the scheme, then the neolith headers.
        self._step = 3
        if _method(request) != "POST":
            raise _Refused("invalid_argument", _METHOD, ("Allow", "POST"))
        if meta.get("QUERY_STRING"):
            raise _Refused("invalid_argument", _QUERY)
        if _redirected(request):
            raise _Refused("invalid_argument", _SCHEME)
        principal = _principal(meta.get(_PRINCIPAL))
        budget = _budget(meta.get(_TIMEOUT_KEY))
        key = _idempotency_key(meta.get("HTTP_IDEMPOTENCY_KEY"))
        trace, tracestate = _trace(
            meta.get("HTTP_TRACEPARENT"), meta.get("HTTP_TRACESTATE")
        )
        self._trace = trace
        _unknown_headers(meta, route.identity)
        # Step 4: the deadline, counted from the arrival.
        self._step = 4
        deadline = None if budget is None else self._arrived + budget / 1000
        if deadline is not None and _context.now() >= deadline:
            raise _Refused("timeout", _NOT_RUN)
        # Step 5: the body's header fields, then the body; the provider side
        # runs the receive check on it.
        self._step = 5
        body = _body(request)
        arrived = Arrived(
            principal=principal,
            trace_id=self.trace_id,
            tracestate=tracestate,
            deadline=deadline,
            idempotency_key=key,
            caller=caller,
        )
        self._step = 6
        try:
            reply = route.provider.serve(
                route.action, body, arrived, _crossing.Receiver.NETWORK
            )
        except Failure as failure:
            return self._failure(failure)
        if reply is None:
            response = _empty(204)
        else:
            response = HttpResponse(reply, content_type=_JSON)
        return self._logged(response, None)

    def _error(
        self, kind: str, message: str, detail: bytes | None = None
    ) -> HttpResponse:
        status = _openapi.KIND_STATUS.get(kind, 422)
        body = (
            b'{"kind":'
            + json.dumps(kind).encode("ascii")
            + b',"message":'
            # A backend's refusal is any str; one it cannot write as UTF-8
            # loses those characters rather than the response.
            + json.dumps(message, ensure_ascii=False).encode("utf-8", "replace")
            + (b"" if detail is None else b',"data":' + detail)
            + b"}"
        )
        response = HttpResponse(body, status=status, content_type=_JSON)
        if status == 401:
            response["WWW-Authenticate"] = _CHALLENGE
        return response

    def _refused(self, refusal: _Refused) -> HttpResponse:
        response = self._error(refusal.kind, refusal.message)
        if refusal.header is not None:
            response[refusal.header[0]] = refusal.header[1]
        detail = f"; refused at step {self._step}: {refusal.message}"
        if self._step == 1 and "HTTP_AUTHORIZATION" in self._request.META:
            detail += _credential(self._request.META["HTTP_AUTHORIZATION"])
        return self._logged(response, refusal.kind, detail)

    def _failure(self, failure: Failure) -> HttpResponse:
        """A failure the provider side delivered; an ``internal`` there has
        had its cause logged by the provider side.
        """
        response = self._error(failure.kind, failure.message, failure.detail)
        return self._logged(response, failure.kind)

    def _internal(self) -> HttpResponse:
        response = self._error("internal", internal_message(self.trace_id))
        return self._logged(response, "internal")

    def _logged(
        self, response: HttpResponse, kind: str | None, detail: str = ""
    ) -> HttpResponse:
        """Log the response's one line on ``neolith.calls`` and mark it
        logged, so Django's ``log_response`` skips it (ADR 0011 Decision 5).
        """
        if kind is None or kind not in _openapi.KIND_STATUS:
            level = logging.INFO
        elif kind == "internal":
            level = logging.ERROR
        else:
            level = logging.WARNING
        route = self._route
        with contextlib.suppress(Exception):
            _calls.log(
                level,
                "%s, action %r: %d %s (trace %s)%s",
                route.identity,
                route.action.name,
                response.status_code,
                kind or "reply",
                self.trace_id,
                detail,
                extra={
                    "contract": route.identity,
                    "action": route.action.name,
                    "status_code": response.status_code,
                    "kind": kind,
                    "trace_id": self.trace_id,
                },
            )
        # Private to Django, with one meaning in 5.2 and 6.1 (utils/log.py,
        # log_response): a response marked so is not logged again.
        response._has_been_logged = True  # type: ignore[attr-defined]  # Django's own mark
        return _framed(response)


def _credential(authorization: str) -> str:
    """The token's ``iss`` and ``kid`` for the log line, each only when it
    has its admitted form, else ``malformed``; never any other part of it.
    """
    issuer = key_id = "malformed"
    with contextlib.suppress(Exception):
        token = _token._token_of(authorization)
        parts = [] if token is None else token.split(".")
        if len(parts) == 3:
            header = _decoded(parts[0])
            claims = _decoded(parts[1])
            found = None if claims is None else claims.get("iss")
            if type(found) is str and _APP_NAME.fullmatch(found):
                issuer = found
            found = None if header is None else header.get("kid")
            if type(found) is str and _token.KEY_ID.fullmatch(found):
                key_id = _token.shown_key_id(found)
    return f"; iss {issuer}, kid {key_id}"


def _decoded(part: str) -> dict[str, object] | None:
    data = _token.decode(part)
    return None if data is None else _token._strict(data)


def _redirected(request: HttpRequest) -> bool:
    """Whether ``SecurityMiddleware`` would redirect the request to HTTPS,
    as it reads its settings (``middleware/security.py``, 5.2 and 6.1).
    """
    if not settings.SECURE_SSL_REDIRECT or request.is_secure():
        return False
    path = request.path.lstrip("/")
    return not any(
        re.compile(pattern).search(path) for pattern in settings.SECURE_REDIRECT_EXEMPT
    )


def _value(raw: object) -> str | None:
    """A header's value with OWS at either end removed, or ``None`` when it
    holds a character outside ASCII, which no admitted form has.
    """
    text = typing.cast(str, raw).strip(_OWS)
    return text if text.isascii() else None


@sensitive_variables()
def _principal(raw: object) -> Principal | None:
    """The principal ``Neolith-Principal`` carries, or ``None`` when it is
    absent; a header that is present, empty included, is read (spec 03).
    """
    if raw is None:
        return None
    text = _value(raw)
    if text is None or len(text) > _context.PRINCIPAL_LIMIT or not _B64.fullmatch(text):
        raise _Refused("invalid_argument", _PRINCIPAL_FORM)
    form = b"" if not text else _token.decode(text)
    if form is None:
        raise _Refused("invalid_argument", _PRINCIPAL_FORM)
    # read_principal bounds the nesting before the json module reads it (#125).
    try:
        return _context.read_principal(form)
    except ValueError as error:
        raise _Refused("invalid_argument", f"Neolith-Principal: {error}") from None


def _budget(raw: object) -> int | None:
    """The budget ``Neolith-Timeout`` carries, in milliseconds, capped."""
    if raw is None:
        return None
    text = _value(raw)
    if text is None or not _TIMEOUT.fullmatch(text):
        raise _Refused("invalid_argument", _TIMEOUT_FORM)
    return min(int(text), _context.BUDGET_CAP)


def _idempotency_key(raw: object) -> str | None:
    """The key ``Idempotency-Key`` carries, unescaped, or ``None``."""
    if raw is None:
        return None
    text = _value(raw)
    if text is None or not _KEY.fullmatch(text):
        raise _Refused("invalid_argument", _KEY_FORM)
    key = _ESCAPED.sub(r"\1", text[1:-1])
    if len(key) > _context.KEY_LIMIT:
        raise _Refused("invalid_argument", _KEY_FORM)
    return key


def _trace(traceparent: object, tracestate: object) -> tuple[str, str]:
    """The trace identifier and ``tracestate`` a call continues: a fresh
    trace with none when ``traceparent`` is absent or invalid (W3C Trace
    Context, section 3.2; ADR 0017 Decision 4).
    """
    trace = _trace_id(traceparent)
    if trace is None:
        return _context._hex(16), ""
    return trace, "" if tracestate is None else _tracestate(tracestate)


def _trace_id(raw: object) -> str | None:
    if raw is None:
        return None
    text = _value(raw)
    found = None if text is None else _TRACEPARENT.fullmatch(text)
    if found is None:
        return None
    version, rest = found["version"], found["rest"]
    # Version ff is invalid; version 00 has no more fields; a later version
    # is read by its first four fields (section 3.2.4).
    if version == "ff" or (version == "00" and rest is not None):
        return None
    if found["trace"] == _NO_TRACE or found["parent"] == _NO_PARENT:
        return None
    return found["trace"]


def _tracestate(raw: object) -> str:
    """The ``tracestate`` passed on: the list's non-empty list-members, in
    order, shortened by whole entries to at most 512 characters, or ``""``
    when the value is not a list of 1 to 32 list-members (ADR 0027
    Decision 6; W3C Trace Context, sections 3.3.1.1 and 3.3.1.5).
    """
    text = _value(raw)
    if text is None:
        return ""
    pieces = text.split(",")
    if len(pieces) > _TRACESTATE_MEMBERS:
        return ""
    members: list[str] = []
    for piece in pieces:
        member = piece.strip(_OWS)
        if not member:
            continue
        if not _TRACESTATE_MEMBER.fullmatch(member):
            return ""
        members.append(member)
    while len(",".join(members)) > _TRACESTATE_LIMIT:
        long = [i for i, member in enumerate(members) if len(member) > _TRACESTATE_LONG]
        del members[long[-1] if long else -1]
    return ",".join(members)


def _unknown_headers(meta: Mapping[str, object], identity: str) -> None:
    """Log each ``neolith-`` header spec 03 does not name at ``DEBUG``, and
    read nothing from it (ADR 0027 Decision 6).
    """
    for key in meta:
        if not key.startswith(_NEOLITH) or key in _KNOWN_NEOLITH:
            continue
        name = "neolith-" + key[len(_NEOLITH) :].lower().replace("_", "-")
        with contextlib.suppress(Exception):
            _calls.debug(
                "%s: ignored the header %s",
                identity,
                name if _HEADER_NAME.fullmatch(name) else "malformed",
            )


def _body(request: HttpRequest) -> bytes:
    """The request body, once its header fields pass rows 9 to 12 of ADR
    0011's table.
    """
    meta = request.META
    media = meta.get("CONTENT_TYPE")
    if media is None or not _MEDIA_TYPE.fullmatch(typing.cast(str, media).strip(_OWS)):
        raise _Refused("invalid_argument", _CONTENT_TYPE, ("Accept-Post", _JSON))
    if "HTTP_CONTENT_ENCODING" in meta:
        raise _Refused(
            "invalid_argument", _CONTENT_ENCODING, ("Accept-Encoding", "identity")
        )
    given = meta.get("CONTENT_LENGTH")
    digits = None if given is None else typing.cast(str, given).strip(_OWS)
    if digits is None or not _DIGITS.fullmatch(digits):
        raise _Refused("invalid_argument", _CONTENT_LENGTH)
    limit: int | None = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
    bound = sys.maxsize if limit is None else limit
    length = int(digits)
    if length > bound:
        raise _Refused("invalid_argument", _larger(bound))
    try:
        body = request.body
    except RequestDataTooBig:
        raise _Refused("invalid_argument", _larger(bound)) from None
    except UnreadablePostError:
        raise _Refused("invalid_argument", _ENDED) from None
    if len(body) < length:
        raise _Refused("invalid_argument", _ENDED)
    if len(body) > length:
        raise _Refused("invalid_argument", _LONGER)
    return body


def _larger(bound: int) -> str:
    return f"the request body is larger than this host admits, {bound} bytes"
