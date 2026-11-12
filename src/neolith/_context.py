"""The call context: its value, the scopes that issue it, and how it crosses.

ADR 0017. A ``CallContext`` is a frozen value: the principal, the trace and
the deadline, which pass from a call to the calls it makes; the idempotency
key and the calling app, which belong to one call; and the id of the scope
that issued it, which never leaves this host.

Scopes are private. The host opens one at each root and around each action it
runs, and closes it when that ends. One context variable holds the scope a
connector call derives its context from; a call may name another open scope
with ``context=``, which is matched by its scope id and never read. This
variable is state the host owns (CONTRIBUTING K6): app code has no way to read
it.

A principal is checked through its sent form (spec 03): its JCS serialization
in UTF-8, whose base64url encoding is at most 4096 bytes, read by the same
check a provider runs on a bare assertion that arrives over HTTP. A provider
receives the principal that check returns, in every mode.
"""

import base64
import contextlib
import contextvars
import dataclasses
import hashlib
import json
import math
import secrets
import threading
import time
import types
import typing
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, final

from neolith._jcs import canonical, canonical_text
from neolith._portable import SAFE_INTEGER_MAX, text_problem

__all__ = ["CallContext", "Principal"]

# Spec 03: a principal's base64url encoding is at most 4096 bytes.
PRINCIPAL_LIMIT = 4096
# ADR 0017 Decision 3: a budget above this many milliseconds counts as it.
BUDGET_CAP = 9_999_999_999
# An idempotency key is a structured-field String of 1 to 255 characters
# (ADR 0017 Decision 3), so it holds printable ASCII only (RFC 9651).
KEY_LIMIT = 255

type _Claim = str | int | bool | tuple[str, ...]


def now() -> float:
    """This host's clock for deadlines: monotonic, in seconds."""
    return time.monotonic()


def b64url(data: bytes) -> str:
    """RFC 4648 section 5, without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@final
class Principal:
    """On whose behalf a call is made: an issuer, a subject and claims.

    A frozen value made of portable types (ADR 0010 Decision 2), never a user
    model instance. Two principals are equal when their issuers, subjects and
    claims are, a claim's value compared with its type, so ``True`` and ``1``
    differ, as they do in JSON.

    Building one checks only the types of its parts. Whether it may cross is
    decided when a call is made, from its sent form (spec 03): a principal
    with an empty issuer or subject, a lone surrogate, an integer outside
    ±(2^53 - 1) or an encoding of more than 4096 bytes fails every call made
    for it with ``internal``.

    Args:
        issuer: Who vouches for the subject, such as an identity provider's
            URL.
        subject: Who the principal is, unique within its issuer.
        claims: What the issuer says of the subject. Maps a name to a string,
            an integer, a boolean, or a list or tuple of strings, which is
            held as a tuple.

    Raises:
        TypeError: If ``issuer`` or ``subject`` is not a ``str``, if
            ``claims`` is not a mapping, if a claim's name is not a ``str``,
            or if a claim's value is of another type than those. Defining a
            subclass raises it too.
    """

    __slots__ = ("_claims", "_issuer", "_subject")
    _issuer: str
    _subject: str
    _claims: Mapping[str, _Claim]

    def __init__(
        self,
        issuer: str,
        subject: str,
        claims: Mapping[str, str | int | bool | Sequence[str]] | None = None,
    ) -> None:
        if type(issuer) is not str or type(subject) is not str:
            raise TypeError("a principal's issuer and subject are each a str")
        if claims is not None and not isinstance(claims, Mapping):
            raise TypeError(
                f"a principal's claims are a mapping, not {type(claims).__qualname__}"
            )
        held: dict[str, _Claim] = {}
        # Each container is copied once and the copy is checked, so a change
        # made to the original meanwhile cannot get past the check.
        given: dict[Any, Any] = {} if claims is None else dict(claims)
        for name, value in given.items():
            if type(name) is not str:
                raise TypeError(
                    f"a claim's name is a str, not {type(name).__qualname__}"
                )
            if type(value) in (str, int, bool):
                held[name] = typing.cast("str | int | bool", value)
                continue
            items = (
                tuple(typing.cast("Sequence[object]", value))
                if type(value) in (list, tuple)
                else None
            )
            if items is None or any(type(item) is not str for item in items):
                raise TypeError(
                    "a claim's value is a str, an int, a bool or a list of str, "
                    f"not {type(value).__qualname__}"
                )
            held[name] = typing.cast("tuple[str, ...]", items)
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_subject", subject)
        object.__setattr__(self, "_claims", types.MappingProxyType(held))

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError(
            "Principal cannot be subclassed: a principal crosses as its "
            "issuer, subject and claims, and nothing else"
        )

    @property
    def issuer(self) -> str:
        """Who vouches for the subject, such as an identity provider's URL."""
        return self._issuer

    @property
    def subject(self) -> str:
        """Who the principal is, unique within its issuer."""
        return self._subject

    @property
    def claims(self) -> Mapping[str, str | int | bool | tuple[str, ...]]:
        """What the issuer says of the subject, as a read-only mapping."""
        return self._claims

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("a Principal is frozen")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("a Principal is frozen")

    def _key(self) -> tuple[object, ...]:
        claims = frozenset(
            (name, type(value), value) for name, value in self._claims.items()
        )
        return (self._issuer, self._subject, claims)

    def __eq__(self, other: object) -> bool:
        if type(other) is not Principal:
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        try:
            issuer, subject, claims = self._issuer, self._subject, self._claims
        except AttributeError:  # made with __new__ alone, never built
            return "<unbuilt Principal>"
        return f"Principal({issuer!r}, {subject!r}, {dict(claims)!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (Principal, (self._issuer, self._subject, dict(self._claims)))


def read_principal(form: bytes) -> Principal:
    """Read a principal object (spec 03), or raise ``ValueError``.

    This is a provider's whole check of a bare assertion, once its base64url
    encoding is undone: UTF-8, a JSON object with no member name twice,
    exactly ``issuer`` and ``subject``, both non-empty strings, and optionally
    ``claims``, an object whose values are strings, booleans, lists of strings
    or integers within ±(2^53 - 1) written with no fraction or exponent; and
    no lone surrogate in any text. The messages name the rule and never show
    a value, which is the end user's data.
    """

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        found = dict(items)
        if len(found) != len(items):
            raise ValueError("a member name occurs twice in one object")
        return found

    def refuse(literal: str) -> NoReturn:
        raise ValueError("a number is not an integer with no fraction or exponent")

    try:
        text = form.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("the principal is not UTF-8 text") from None
    try:
        value = json.loads(
            text, object_pairs_hook=pairs, parse_float=refuse, parse_constant=refuse
        )
    except RecursionError:
        raise ValueError("the principal is not a principal object") from None
    except json.JSONDecodeError:
        raise ValueError("the principal is not JSON text") from None
    if type(value) is not dict or not {"issuer", "subject"} <= value.keys() <= {
        "issuer",
        "subject",
        "claims",
    }:
        raise ValueError(
            "a principal object has exactly the members issuer and subject, "
            "and optionally claims"
        )
    issuer, subject, claims = value["issuer"], value["subject"], value.get("claims", {})
    if type(issuer) is not str or type(subject) is not str or not issuer or not subject:
        raise ValueError("a principal's issuer and subject are non-empty strings")
    if type(claims) is not dict:
        raise ValueError("a principal's claims are an object")
    texts = [issuer, subject]
    for name, claim in claims.items():
        texts.append(name)
        if type(claim) is str:
            texts.append(claim)
        elif type(claim) is int:
            if abs(claim) > SAFE_INTEGER_MAX:
                raise ValueError(f"a claim is an integer outside ±{SAFE_INTEGER_MAX}")
        elif type(claim) is list and all(type(item) is str for item in claim):
            texts.extend(claim)
        elif type(claim) is not bool:
            raise ValueError(
                "a claim's value is a string, an integer, a boolean or a list "
                "of strings"
            )
    if any(text_problem(item) is not None for item in texts):
        raise ValueError("the principal's text holds a lone surrogate")
    return Principal(issuer, subject, claims)


def principal_form(principal: Principal) -> bytes:
    """A principal's sent form (spec 03), or ``ValueError`` if it has none.

    The form is its JCS serialization in UTF-8, with ``claims`` left out when
    empty. It is valid when its base64url encoding is at most 4096 bytes and
    the provider's own check (``read_principal``) reads it back as the same
    principal, so a caller refuses exactly what a provider would.
    """
    member: dict[str, object] = {
        "issuer": principal.issuer,
        "subject": principal.subject,
    }
    if principal.claims:
        member["claims"] = {
            name: list(value) if type(value) is tuple else value
            for name, value in principal.claims.items()
        }
    try:
        form = canonical(member)
    except ValueError:
        # JCS refuses a lone surrogate and an integer no double holds; its
        # message would show the integer.
        raise ValueError(
            "the principal holds a lone surrogate, or an integer outside "
            f"±{SAFE_INTEGER_MAX}"
        ) from None
    if len(b64url(form)) > PRINCIPAL_LIMIT:
        raise ValueError(
            f"the principal's encoding is longer than {PRINCIPAL_LIMIT} bytes"
        )
    if read_principal(form) != principal:
        raise ValueError("the principal's sent form reads back as another")
    return form


@final
@dataclass(frozen=True, slots=True, eq=False)
class CallContext:
    """The context of one call (ADR 0017 Decision 1).

    An action that reads it takes it as a second parameter annotated
    ``CallContext``.

    Passed as ``context=`` to a connector call, a value only names its scope:
    the call's context is derived from what that scope holds, so a changed
    copy changes nothing, and a value whose call has ended is refused.

    Two contexts are equal when their principal, trace identifier,
    idempotency key and caller are equal and both have a deadline or neither
    does. The span identifier, ``tracestate``, the deadline's value and the
    scope id are left out, since each hop or host changes them, so the
    contexts an action sees over two transports compare equal.

    Attributes:
        principal: On whose behalf the call is made, or ``None``.
        trace_id: The W3C trace identifier.
        span_id: The span of this call.
        tracestate: The W3C ``tracestate`` that goes with them, empty when
            there is none.
        deadline: A ``time.monotonic()`` reading on this host, or ``None``
            when the call has none.
        idempotency_key: This call's key; ``None`` on a root that is not
            itself a call.
        caller: The app that made the call; ``None`` on a root that is not
            itself a call.
        scope_id: Names the scope on this host that issued the value.
    """

    principal: Principal | None
    trace_id: str
    span_id: str
    tracestate: str
    deadline: float | None
    idempotency_key: str | None
    caller: str | None
    scope_id: str = field(repr=False)

    def _key(self) -> tuple[object, ...]:
        return (
            self.principal,
            self.trace_id,
            self.deadline is None,
            self.idempotency_key,
            self.caller,
        )

    def __eq__(self, other: object) -> bool:
        if type(other) is not CallContext:
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


@final
@dataclass(frozen=True, slots=True)
class Sent:
    """A call's context as a transport carries it.

    These are the fields spec 03 puts in headers. ``principal`` is the sent
    form, ``budget`` the remaining whole milliseconds, at least 1, or ``None``
    for a call with no deadline, and ``caller`` the calling app, which a
    network transport authenticates and the in-process one is told.
    """

    principal: bytes | None
    trace_id: str
    tracestate: str
    budget: int | None
    idempotency_key: str
    caller: str | None


@final
class Scope:
    """A scope this host opened: the context it issued, and what it counts.

    ``app`` is the app the scope belongs to, the caller of the calls made from
    it. ``context`` is the value the scope hands out, and ``held`` its own
    copy of the same fields, which calls are derived from, so nothing done to
    a value that was handed out reaches them. The occurrence counts and the
    principal's sent form belong to the scope, behind its lock, so every call
    made from it, in any thread, shares them.
    """

    __slots__ = ("_counts", "_form", "_lock", "app", "closed", "context", "held")

    def __init__(self, context: CallContext, app: str | None) -> None:
        # The value handed to code that asks for the context, and the scope's
        # own copy of its fields, which is all a call is ever derived from.
        self.context = context
        self.held = dataclasses.replace(context)
        self.app = app
        self.closed = False
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str, str], int] = {}
        # Unset until the first call; then the form, or why there is none.
        self._form: tuple[bytes | None, str | None] | None = None

    def principal(self) -> bytes | None:
        """The principal's sent form, or ``None`` when the scope has none.

        Decided by the scope's first call and fixed from then on, so every
        call from one scope gets one answer. Raises ``ValueError`` when the
        principal is not valid.
        """
        with self._lock:
            if self._form is None:
                held = self.held.principal
                try:
                    self._form = (None if held is None else principal_form(held), None)
                except ValueError as error:
                    self._form = (None, str(error))
            form, problem = self._form
        if problem is not None:
            raise ValueError(problem)
        return form

    def child_key(self, contract: str, action: str, request: bytes) -> str:
        """The idempotency key of the next call made from this scope.

        Derived from this scope's key, the contract identity, the action, the
        request's hash and how many calls with those the scope has made, or
        random when the scope has no key (ADR 0017 Decision 3).
        """
        parent = self.held.idempotency_key
        if parent is None:
            return b64url(secrets.token_bytes(16))
        request_hash = b64url(hashlib.sha256(canonical_text(request)).digest())
        with self._lock:
            count = self._counts.get((contract, action, request_hash), 0)
            self._counts[contract, action, request_hash] = count + 1
        derived = canonical([parent, contract, action, request_hash, count])
        return b64url(hashlib.sha256(derived).digest())

    def budget(
        self, timeout: float | None, started: float
    ) -> tuple[int | None, float | None]:
        """The remaining budget of a call sent now, in whole milliseconds, and
        the call's deadline on this host's clock.

        The deadline is the earlier of the scope's and the call's own:
        ``timeout`` seconds from ``started``, when the connector was called.
        The budget is rounded down, so it is 0 or less once the deadline has
        passed, and capped at ``BUDGET_CAP``, as the deadline is with it.
        Both are ``None`` when there is no deadline.
        """
        current = now()
        deadline = self.held.deadline
        remaining = None if deadline is None else deadline - current
        if timeout is not None:
            # Taken from the timeout itself, so that a call that took no time
            # so far has exactly its timeout left.
            own = timeout - (current - started)
            if remaining is None or own < remaining:
                remaining, deadline = own, started + timeout
        if remaining is None or deadline is None:
            return None, None
        return (
            min(math.floor(remaining * 1000), BUDGET_CAP),
            min(deadline, current + BUDGET_CAP / 1000),
        )


_CURRENT: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "neolith_scope", default=None
)
_OPEN: dict[str, Scope] = {}
_OPEN_LOCK = threading.Lock()


def _hex(size: int) -> str:
    """A random identifier of ``size`` bytes in hexadecimal, not all zeros."""
    while True:  # W3C Trace Context: an identifier of all zeros is invalid
        found = secrets.token_hex(size)
        if found.strip("0"):
            return found


@contextlib.contextmanager
def opened(
    *,
    principal: Principal | None,
    trace_id: str,
    tracestate: str,
    deadline: float | None,
    idempotency_key: str | None,
    caller: str | None,
    app: str | None,
) -> Iterator[Scope]:
    """Open a scope, make it the ambient one, and close it on the way out.

    The scope issues a new context with a span identifier of its own. It is
    closed when the block ends, by return or by any exception, so a context
    kept past its call or root is refused (ADR 0017 Decision 3).
    """
    scope_id = secrets.token_hex(16)
    scope = Scope(
        CallContext(
            principal=principal,
            trace_id=trace_id,
            span_id=_hex(8),
            tracestate=tracestate,
            deadline=deadline,
            idempotency_key=idempotency_key,
            caller=caller,
            scope_id=scope_id,
        ),
        app,
    )
    with _OPEN_LOCK:
        _OPEN[scope_id] = scope
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        scope.closed = True
        with _OPEN_LOCK:
            del _OPEN[scope_id]
        # A block left in another context than it was entered in cannot reset
        # the variable there; the scope it still names is closed.
        with contextlib.suppress(ValueError):
            _CURRENT.reset(token)


def _app(app: object) -> str | None:
    if app is not None and type(app) is not str:
        raise TypeError(f"an app is named by a str, not {type(app).__qualname__}")
    return app


def timeout_option(timeout: object) -> float | None:
    """Check a ``timeout=`` value: finite seconds, or ``None`` for none.

    A timeout beyond the budget cap, either way, is cut to just past it: the
    cap bounds the budget, and a timeout below zero has passed already.
    """
    if timeout is None:
        return None
    if type(timeout) not in (int, float):
        raise TypeError(
            f"timeout= takes a number of seconds, not {type(timeout).__qualname__}"
        )
    seconds = typing.cast("int | float", timeout)
    if type(seconds) is float and not math.isfinite(seconds):
        raise ValueError(f"timeout= takes a finite number of seconds, not {seconds}")
    # An int may be beyond any float, so it is cut before it becomes one.
    limit = BUDGET_CAP // 1000 + 1
    return float(max(min(seconds, limit), -limit))


def root(
    app: str | None = None,
    *,
    principal: Principal | None = None,
    timeout: float | None = None,
) -> contextlib.AbstractContextManager[Scope]:
    """Open a root that is not itself a call, as for a command or a test.

    Private until the host opens roots (roadmap step 4, #11). It starts a new
    trace and has no key and no caller. ``app`` is the app it runs for, the
    caller of the calls made from it. It has no deadline unless ``timeout``
    gives one, in seconds from now.
    """
    if principal is not None and type(principal) is not Principal:
        raise TypeError(
            f"principal= takes a Principal, not {type(principal).__qualname__}"
        )
    seconds = timeout_option(timeout)
    return opened(
        principal=principal,
        trace_id=_hex(16),
        tracestate="",
        deadline=None if seconds is None else now() + seconds,
        idempotency_key=None,
        caller=None,
        app=_app(app),
    )


def received(
    sent: Sent, deadline: float | None, app: str | None
) -> contextlib.AbstractContextManager[Scope]:
    """The scope a provider's side runs an action in (ADR 0017 Decision 4).

    It holds the principal read from its sent form, the caller's trace with a
    span of its own, the call's key and caller, and ``deadline``, which the
    transport counts from when the call arrived. ``app`` is the app the
    provider belongs to. The principal is read here; the scope opens when the
    result is entered.
    """
    return opened(
        principal=None if sent.principal is None else read_principal(sent.principal),
        trace_id=sent.trace_id,
        tracestate=sent.tracestate,
        deadline=deadline,
        idempotency_key=sent.idempotency_key,
        caller=sent.caller,
        app=app,
    )


def parent(context: object) -> Scope:
    """The open scope a connector call derives its context from.

    ``context`` is what the call passed as ``context=``, or ``None`` for the
    ambient scope. A value only names its scope, by its scope id, so a copy
    matches and its other fields are never read. A missing or closed scope is
    a mistake in the calling code (spec 01, CON-29): it raises
    ``RuntimeError``, never a contract error, and never starts a context.
    """
    if context is not None:
        if type(context) is not CallContext:
            raise TypeError(
                f"context= takes a CallContext, not {type(context).__qualname__}"
            )
        scope_id = context.scope_id
        with _OPEN_LOCK:
            found = _OPEN.get(scope_id) if type(scope_id) is str else None
        if found is None or found.closed:
            raise RuntimeError(
                "the call context passed as context= names no open scope on "
                "this host: its call or scope has ended, or it was not issued "
                "here. Work that outlives its call opens a scope of its own"
            )
        return found
    ambient = _CURRENT.get()
    if ambient is None:
        raise RuntimeError(
            "a connector call needs a call context, and none is active here. "
            "Make the call from inside an action or another scope the host "
            "opened, or pass context=; a thread the app starts does not "
            "inherit the context"
        )
    if ambient.closed:
        raise RuntimeError(
            "the call context active here belongs to a call or scope that has "
            "ended. Work that outlives its call opens a scope of its own"
        )
    return ambient
