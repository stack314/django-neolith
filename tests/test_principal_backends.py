"""Principal backends as plain functions, with built inputs and no host
(ADR 0023 Decision 2; spec 05, A request's principal).

The setting's shape, building backends, the search, a root's one outcome
and the principal middleware's response phase are functions of their
arguments, so any list of backends is given here; the cases that need
Django's middleware chain are in ``test_request_principal.py``.
"""

import asyncio
import dataclasses
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.middleware.cache import FetchFromCacheMiddleware, UpdateCacheMiddleware
from django.test import RequestFactory, override_settings
from django.test.utils import isolate_apps
from hypothesis import given
from hypothesis import strategies as st

from neolith import CredentialRefused, Principal, _checks, _principal
from neolith._context import principal_form, read_principal
from neolith._principal import Built, Resolution, build, search, shape_problems
from neolith.middleware import afinish, finish
from neolith.principals import DjangoUserBackend, PrincipalBackend, key_problem
from neolith.principals import order_problems as user_order_problems
from tests import host_apps
from tests.hosting import in_built_host

SETTING = 'NEOLITH["PRINCIPAL_BACKENDS"]'
HERE = "tests.test_principal_backends"
ROOT = "neolith.middleware.RootMiddleware"
SESSION = "django.contrib.sessions.middleware.SessionMiddleware"
AUTH = "django.contrib.auth.middleware.AuthenticationMiddleware"
PRINCIPAL = "neolith.middleware.PrincipalMiddleware"
UPDATE = "django.middleware.cache.UpdateCacheMiddleware"
FETCH = "django.middleware.cache.FetchFromCacheMiddleware"
ISSUER = "https://users.example.com"


# Test-only backends


class Fixed(PrincipalBackend):
    """Answers what it was built with, and counts its calls."""

    request_inputs = ("x-fixed",)

    def __init__(self, *, answer: object = None, inputs: object = ("x-fixed",)) -> None:
        self.answer = answer
        self.request_inputs = inputs  # type: ignore[assignment]
        self.calls = 0

    def resolve(self, request: HttpRequest) -> Principal | None:
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer  # type: ignore[return-value]


class Header(PrincipalBackend):
    """The principal named by the ``X-User`` header."""

    request_inputs = ("x-user",)

    def resolve(self, request: HttpRequest) -> Principal | None:
        name = request.headers.get("X-User")
        return None if name is None else Principal(ISSUER, name)


class Anything(PrincipalBackend):
    """Reads the client's address, which no header names."""

    request_inputs = ("*",)

    def resolve(self, request: HttpRequest) -> Principal | None:
        return Principal(ISSUER, str(request.META.get("REMOTE_ADDR")))


class NoInputs(PrincipalBackend):
    def resolve(self, request: HttpRequest) -> Principal | None:
        return None


class Abstract(PrincipalBackend):
    request_inputs = ()


class AsyncResolve(PrincipalBackend):
    request_inputs = ()

    async def resolve(self, request: HttpRequest) -> Principal | None:  # type: ignore[override]
        return None


class SyncAresolve(PrincipalBackend):
    request_inputs = ()

    def resolve(self, request: HttpRequest) -> Principal | None:
        return None

    def aresolve(self, request: HttpRequest) -> Principal | None:  # type: ignore[override]
        return None


class Raises(PrincipalBackend):
    request_inputs = ()

    def __init__(self, **options: object) -> None:
        raise ValueError(f"no options are taken: {sorted(options)}")

    def resolve(self, request: HttpRequest) -> Principal | None:
        return None


class Unrelated:
    request_inputs = ()

    def resolve(self, request: HttpRequest) -> None:
        return None


NOT_A_CLASS = 3


def entry(backend: str, **options: object) -> dict[str, Any]:
    return {"BACKEND": backend, "OPTIONS": options}


def built(*backends: PrincipalBackend) -> tuple[Built, ...]:
    return tuple(
        Built(i, f"{HERE}.{type(b).__qualname__}", b, tuple(b.request_inputs))
        for i, b in enumerate(backends)
    )


def resolution(
    *backends: PrincipalBackend, request: HttpRequest | None = None
) -> Resolution:
    found = built(*backends)
    given = request if request is not None else RequestFactory().get("/")
    return Resolution(found, _principal.vary_of(found), given, "0" * 31 + "1")


ALICE = Principal(ISSUER, "alice")
BOB = Principal(ISSUER, "bob")


# The setting's shape (the builder's rows)


_HEADER_NAMES = st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True)
_IDENT = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}", fullmatch=True).filter(
    lambda s: not __import__("keyword").iskeyword(s)
)
_PATHS = st.lists(_IDENT, min_size=2, max_size=4).map(".".join)
_OPTIONS = st.dictionaries(_IDENT, st.integers() | st.text(max_size=5), max_size=3)
_ENTRIES = st.builds(lambda b, o: {"BACKEND": b, "OPTIONS": o}, _PATHS, _OPTIONS)
_SETTINGS = st.lists(_ENTRIES, min_size=1, max_size=4)


@pytest.mark.spec("APP-16")
@given(_SETTINGS)
def test_every_setting_the_grammar_draws_has_the_admitted_shape(
    value: list[Any],
) -> None:
    assert shape_problems(value) == []


_MUTATIONS: list[tuple[str, Callable[[dict[str, Any]], Any], str]] = [
    ("not a dict", lambda e: ["BACKEND"], "expected a dict with exactly the keys"),
    ("a third key", lambda e: {**e, "NAME": "x"}, "expected exactly the keys"),
    ("no OPTIONS", lambda e: {"BACKEND": e["BACKEND"]}, "expected exactly the keys"),
    ("no BACKEND", lambda e: {"OPTIONS": e["OPTIONS"]}, "expected exactly the keys"),
    (
        "a key of another case",
        lambda e: {"backend": e["BACKEND"], "OPTIONS": {}},
        "expected exactly the keys",
    ),
    ("one part", lambda e: {**e, "BACKEND": "Backend"}, "is not a dotted path"),
    ("a space", lambda e: {**e, "BACKEND": e["BACKEND"] + " "}, "is not a dotted path"),
    (
        "an empty part",
        lambda e: {**e, "BACKEND": e["BACKEND"] + ".."},
        "is not a dotted path",
    ),
    (
        "a keyword",
        lambda e: {**e, "BACKEND": e["BACKEND"] + ".class"},
        "is not a dotted path",
    ),
    (
        "non-ASCII",
        lambda e: {**e, "BACKEND": e["BACKEND"] + ".é"},
        "is not a dotted path",
    ),
    ("not a str", lambda e: {**e, "BACKEND": object}, "is not a dotted path"),
    ("OPTIONS a list", lambda e: {**e, "OPTIONS": []}, "OPTIONS: expected a dict"),
    ("OPTIONS None", lambda e: {**e, "OPTIONS": None}, "OPTIONS: expected a dict"),
    (
        "a key not an identifier",
        lambda e: {**e, "OPTIONS": {"an issuer": 1}},
        "is not an identifier",
    ),
    ("a key not a str", lambda e: {**e, "OPTIONS": {1: 1}}, "is not an identifier"),
]


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("change", "message"),
    [(m[1], m[2]) for m in _MUTATIONS],
    ids=[m[0] for m in _MUTATIONS],
)
@given(_SETTINGS, st.data())
def test_one_element_outside_the_grammar_is_refused_naming_its_entry(
    change: Callable[[dict[str, Any]], Any],
    message: str,
    value: list[Any],
    data: st.DataObject,
) -> None:
    position = data.draw(st.integers(0, len(value) - 1))
    value[position] = change(value[position])
    problems = shape_problems(value)
    assert len(problems) == 1
    assert problems[0].startswith(f"{SETTING}[{position}]: ")
    assert message in problems[0]


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], f"{SETTING}: is empty; leave the key out for no backend"),
        ((), f"{SETTING}: expected a list of backends, got tuple"),
        (None, f"{SETTING}: expected a list of backends, got NoneType"),
        ({"BACKEND": "a.B"}, f"{SETTING}: expected a list of backends, got dict"),
    ],
)
def test_the_setting_is_a_list_of_one_or_more(value: object, message: str) -> None:
    assert shape_problems(value) == [message]


def test_every_entry_s_refusals_reach_the_diagnostic() -> None:
    value = [entry("a"), {"BACKEND": "a.B"}, {"BACKEND": 1, "OPTIONS": {"x y": 1}}]
    assert [p.split(":")[0] for p in shape_problems(value)] == [
        f"{SETTING}[0]",
        f"{SETTING}[1]",
        f"{SETTING}[2]",
        f"{SETTING}[2]",
    ]


# Building the backends (the check rows, neolith.E019)


_BUILD_REFUSALS = [
    (entry("tests.no_such_module.Backend"), "does not import: ModuleNotFoundError"),
    (entry(f"{HERE}.NOT_A_CLASS"), "is a int, not a class"),
    (entry("neolith.principals.PrincipalBackend"), "is not a subclass of"),
    (entry(f"{HERE}.Unrelated"), "is not a subclass of"),
    (entry(f"{HERE}.Abstract"), "is abstract; it does not define resolve"),
    (entry(f"{HERE}.AsyncResolve"), "resolve is a coroutine function"),
    (entry(f"{HERE}.SyncAresolve"), "aresolve is not a coroutine function"),
    (
        entry(f"{HERE}.Raises", issuer="x"),
        "the constructor raised, with the options issuer: ValueError",
    ),
    (
        entry(f"{HERE}.Fixed", unknown=1),
        "the constructor raised, with the options unknown: TypeError",
    ),
    (entry(f"{HERE}.NoInputs"), "has no request_inputs; the attribute has no default"),
    (
        entry(f"{HERE}.Fixed", inputs=["cookie"]),
        "request_inputs is a list, not a tuple",
    ),
    (entry(f"{HERE}.Fixed", inputs="cookie"), "request_inputs is a str, not a tuple"),
    (
        entry(f"{HERE}.Fixed", inputs=("Cookie",)),
        "holds 'Cookie', which is not a header name",
    ),
    (entry(f"{HERE}.Fixed", inputs=("x_user",)), "holds 'x_user'"),
    (entry(f"{HERE}.Fixed", inputs=("-x",)), "holds '-x'"),
    (entry(f"{HERE}.Fixed", inputs=("",)), "holds ''"),
    (entry(f"{HERE}.Fixed", inputs=("cookie ",)), "holds 'cookie '"),
    (
        entry(f"{HERE}.Fixed", inputs=("cookie", "*")),
        "holds '*', which is not a header name in lower case ('*' stands alone)",
    ),
    (entry(f"{HERE}.Fixed", inputs=(b"cookie",)), "holds a bytes"),
    (entry(f"{HERE}.Fixed", inputs=("cookie", "cookie")), "names a header twice"),
    (
        entry("neolith.principals.DjangoUserBackend", options={"issuer": ISSUER}),
        "the option 'options' is not taken",
    ),
]


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("given", "message"), _BUILD_REFUSALS, ids=[m for _, m in _BUILD_REFUSALS]
)
def test_a_backend_set_up_cannot_build_is_refused_naming_its_entry(
    given: dict[str, Any], message: str
) -> None:
    found, problems = build([given])
    assert found == ()
    assert len(problems) == 1
    assert problems[0].startswith(f"{SETTING}[0] ({given['BACKEND']}): ")
    assert message in problems[0]


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("inputs", "named"),
    [
        (("Bad Name", "cookie", "cookie"), ["holds 'Bad Name'", "twice: 'cookie'"]),
        (("*", "*"), ["holds '*'", "twice: '*'"]),
        (
            ("A", b"b", "a", "a", "c", "c"),
            ["holds 'A'", "holds a bytes", "twice: 'a', 'c'"],
        ),
        (("X", "X"), ["holds 'X'", "twice: 'X'"]),
    ],
)
def test_every_refusal_of_request_inputs_is_named(
    inputs: tuple[object, ...], named: list[str]
) -> None:
    """A refused name hides neither another one nor a repeat."""
    found, problems = build([entry(f"{HERE}.Fixed", inputs=inputs)])
    assert found == ()
    assert len(problems) == len(named)
    for problem, part in zip(problems, named, strict=True):
        assert problem.startswith(f"{SETTING}[0] ({HERE}.Fixed): ")
        assert part in problem


@pytest.mark.spec("APP-16")
@given(
    st.lists(_HEADER_NAMES, unique=True, min_size=2, max_size=5),
    st.sampled_from(["Cookie", "x_user", "", "-x", "*", "cookie "]),
    st.data(),
)
def test_a_refused_name_and_a_repeat_are_named_together(
    names: list[str], bad: str, data: st.DataObject
) -> None:
    where = data.draw(st.integers(0, len(names) - 1))
    again = data.draw(st.integers(0, len(names) - 1).filter(lambda i: i != where))
    inputs = [*names]
    inputs[where] = bad
    inputs.insert(data.draw(st.integers(0, len(inputs))), names[again])
    problems = build([entry(f"{HERE}.Fixed", inputs=tuple(inputs))])[1]
    assert len(problems) == 2
    assert f"holds {bad!r}" in problems[0]
    assert problems[1].endswith(f"names a header twice: {names[again]!r}")


@pytest.mark.parametrize(
    "inputs",
    [(), ("*",), ("cookie",), ("authorization", "cookie"), ("x-api-key", "x2")],
)
def test_the_nearest_admitted_inputs_build(inputs: tuple[str, ...]) -> None:
    found, problems = build([entry(f"{HERE}.Fixed", inputs=inputs)])
    assert problems == []
    assert found[0].inputs == inputs


@given(st.lists(_HEADER_NAMES, unique=True, max_size=5).map(tuple))
def test_every_header_name_the_grammar_draws_is_admitted(
    inputs: tuple[str, ...],
) -> None:
    assert build([entry(f"{HERE}.Fixed", inputs=inputs)])[1] == []


@pytest.mark.spec("APP-16")
def test_every_entry_is_built_or_refused_and_none_stops_another() -> None:
    found, problems = build(
        [
            entry(f"{HERE}.Fixed"),
            entry(f"{HERE}.NoInputs"),
            entry(f"{HERE}.Header"),
            entry("tests.no_such_module.Backend"),
            entry(f"{HERE}.Fixed"),
        ]
    )
    assert [(b.position, b.path) for b in found] == [
        (0, f"{HERE}.Fixed"),
        (2, f"{HERE}.Header"),
        (4, f"{HERE}.Fixed"),
    ]
    assert found[0].backend is not found[2].backend
    assert [p.split(" ")[0] for p in problems] == [f"{SETTING}[1]", f"{SETTING}[3]"]


def test_the_host_keeps_its_own_copy_of_the_inputs() -> None:
    (one,), _ = build([entry(f"{HERE}.Header")])
    one.backend.request_inputs = ("other",)
    assert one.inputs == ("x-user",)


@pytest.mark.parametrize(
    ("inputs", "vary"),
    [
        ([("cookie",), ("authorization", "cookie")], ("cookie", "authorization")),
        ([(), ("x-user",)], ("x-user",)),
        ([("cookie",), ("*",)], ("*",)),
        ([("*",), ()], ("*",)),
        ([(), ()], ()),
    ],
)
def test_the_union_of_the_inputs(
    inputs: list[tuple[str, ...]], vary: tuple[str, ...]
) -> None:
    found = built(*(Fixed(inputs=i) for i in inputs))
    assert _principal.vary_of(found) == vary


# The search: order, the first principal, abstaining and failing


_FIRST = {
    "a principal": ALICE,
    "None": None,
    "a raise": RuntimeError("the identity provider is down"),
    "a wrong type": "alice",
}


@pytest.mark.spec("APP-51", "APP-52", "APP-53", "APP-54")
@pytest.mark.parametrize("outcome", list(_FIRST))
@pytest.mark.parametrize("second", [BOB, None])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_two_backends_in_order(
    outcome: str, second: Principal | None, mode: str
) -> None:
    first, other = (
        Fixed(answer=_FIRST[outcome]),
        Fixed(answer=second, inputs=("x-other",)),
    )
    for order in ((first, other), (other, first)):
        first.calls = other.calls = 0
        held = resolution(*order)
        response = HttpResponse()
        got: object
        try:
            got = held.form() if mode == "sync" else asyncio.run(held.aform())
        except ValueError:
            got = "failed"
        (finish if mode == "sync" else _run_afinish)(held, response)
        lead = order[0]
        expected: object
        if lead is first:
            if outcome == "a principal":
                expected = principal_form(ALICE)
            elif outcome == "None":
                expected = None if second is None else principal_form(second)
            else:
                expected = "failed"
            assert other.calls == (1 if outcome == "None" else 0)
        else:
            if second is not None:
                expected = principal_form(second)
                assert first.calls == 0
            elif outcome == "a principal":
                expected = principal_form(ALICE)
            elif outcome == "None":
                expected = None
            else:
                expected = "failed"
        assert got == expected
        assert lead.calls == 1
        # The union, whichever backend decided.
        assert response.headers["Vary"] == ", ".join(
            [*built(*order)[0].inputs, *built(*order)[1].inputs]
        )


def _run_afinish(held: Resolution, response: HttpResponse) -> None:
    asyncio.run(afinish(held, response))


@pytest.mark.spec("APP-53", "APP-61")
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_no_backend_asks_nothing_and_adds_no_vary(mode: str) -> None:
    assert search((), RequestFactory().get("/")) is None
    held = resolution()
    response = HttpResponse()
    (finish if mode == "sync" else _run_afinish)(held, response)
    assert "Vary" not in response.headers


@pytest.mark.spec("APP-60")
def test_a_backend_that_reads_beyond_the_headers_makes_vary_a_star() -> None:
    held = resolution(Header(), Anything())
    response = HttpResponse()
    response.headers["Vary"] = "Cookie"
    held.form()
    finish(held, response)
    assert response.headers["Vary"] == "*"


@pytest.mark.spec("APP-60")
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_backends_that_declare_no_input_add_no_vary(mode: str) -> None:
    """A search started, but the union of the inputs is empty: the response
    gains no ``Vary`` header, not an empty one."""
    held = resolution(Fixed(answer=ALICE, inputs=()))
    held.form()
    response = HttpResponse()
    (finish if mode == "sync" else _run_afinish)(held, response)
    assert "Vary" not in response.headers


@pytest.mark.spec("APP-61")
def test_a_root_whose_search_never_started_gains_no_vary() -> None:
    held = resolution(Header())
    response = HttpResponse()
    finish(held, response)
    assert "Vary" not in response.headers


@pytest.mark.spec("APP-54", "APP-55")
def test_a_failure_is_recorded_once_and_logged_with_the_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = Fixed(answer=RuntimeError("down"))
    held = resolution(failing)
    for _ in range(3):
        with pytest.raises(ValueError, match="could not be resolved"):
            held.form()
    assert failing.calls == 1
    logged = [r for r in caplog.records if r.name == "neolith.host"]
    assert len(logged) == 1
    message = logged[0].getMessage()
    assert "0" * 31 + "1" in message
    assert f"{SETTING}[0] ({HERE}.Fixed): resolve raised RuntimeError: down" in message


@pytest.mark.spec("APP-54")
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_a_refused_credential_fails_with_internal_and_is_logged_at_warning(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A backend that raises ``CredentialRefused`` fails the root as any
    failure does, and the host logs it at WARNING, with its message alone
    (ADR 0027 Decision 7, 0023 Open question 4)."""
    refusing = Fixed(answer=CredentialRefused("the session was revoked"))
    held = resolution(refusing)
    read = held.form if mode == "sync" else async_to_sync(held.aform)
    with pytest.raises(ValueError, match="could not be resolved"):
        read()
    logged = [r for r in caplog.records if r.name == "neolith.host"]
    method = "resolve" if mode == "sync" else "aresolve"
    assert [(r.levelno, r.getMessage(), r.exc_info) for r in logged] == [
        (
            logging.WARNING,
            f"request root (trace {'0' * 31}1): {SETTING}[0] ({HERE}.Fixed): "
            f"{method} refused the credential: the session was revoked",
            None,
        )
    ]


@pytest.mark.spec("APP-54")
def test_a_coroutine_returned_by_resolve_is_closed_and_refused() -> None:
    async def coroutine() -> Principal:
        return ALICE

    held = resolution(Fixed(answer=coroutine()))
    with pytest.raises(ValueError, match="could not be resolved"):
        held.form()


@pytest.mark.spec("APP-54")
@pytest.mark.parametrize("asynchronous", [False, True], ids=["resolve", "aresolve"])
def test_a_wrong_value_is_logged_under_the_method_that_returned_it(
    asynchronous: bool, caplog: pytest.LogCaptureFixture
) -> None:
    class Wrong(PrincipalBackend):
        request_inputs = ()

        def resolve(self, request: HttpRequest) -> Principal | None:
            return 3  # type: ignore[return-value]

        async def aresolve(self, request: HttpRequest) -> Principal | None:
            return 3  # type: ignore[return-value]

    held = resolution(Wrong())
    with pytest.raises(ValueError, match="could not be resolved"):
        asyncio.run(held.aform()) if asynchronous else held.form()
    method = "aresolve" if asynchronous else "resolve"
    logged = [r.getMessage() for r in caplog.records if r.name == "neolith.host"]
    assert len(logged) == 1
    assert f"<locals>.Wrong): {method} returned a int, not a" in logged[0]


@pytest.mark.spec("APP-54")
def test_a_principal_with_no_valid_sent_form_is_a_failure() -> None:
    held = resolution(Fixed(answer=Principal(ISSUER, "x" * 5000)))
    with pytest.raises(ValueError, match="could not be resolved"):
        held.form()


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_a_signal_to_stop_records_nothing_and_goes_on(
    signal: type[BaseException],
) -> None:
    backend = Fixed(answer=signal())
    held = resolution(backend)
    with pytest.raises(signal):
        held.form()
    backend.answer = ALICE
    assert held.form() == principal_form(ALICE)


@pytest.mark.spec("APP-59")
def test_a_first_call_after_the_response_phase_fails_and_asks_no_backend() -> None:
    backend = Fixed(answer=ALICE)
    held = resolution(backend)
    response = HttpResponse()
    finish(held, response)
    with pytest.raises(ValueError, match="could not be resolved"):
        held.form()
    with pytest.raises(ValueError, match="could not be resolved"):
        asyncio.run(held.aform())
    assert backend.calls == 0
    assert held.request is None


@pytest.mark.parametrize("bound", [True, False])
def test_a_host_with_no_backend_gives_a_root_no_resolution(
    monkeypatch: pytest.MonkeyPatch, bound: bool
) -> None:
    """With no backend the principal middleware does nothing per request,
    so a late call goes on with no principal (APP-53, APP-59)."""
    from neolith import _context, _host, middleware

    host = _host._current()
    assert host is not None
    bare = dataclasses.replace(host, principals=(), vary=())
    monkeypatch.setattr(_host, "_current", lambda: bare)
    with _context.opened(**_context.root_fields()) as scope:
        token = middleware._REQUEST.set(scope)
        try:
            if bound:
                middleware._bind(RequestFactory().get("/"), HttpResponse)
            assert middleware._resolution(scope) is None
            assert scope.principal() is None
        finally:
            middleware._REQUEST.reset(token)


@pytest.mark.spec("APP-59")
def test_a_root_never_bound_has_no_resolution_to_mark() -> None:
    """A call from it has no calling app at any time (TRN-56), so the
    response phase leaves it as it is, and its principal stays none."""
    from neolith import _context, middleware

    with _context.opened(**_context.root_fields()) as scope:
        assert middleware._resolution(scope) is None
        assert scope.resolution is None
        assert scope.principal() is None


@pytest.mark.spec("APP-56")
def test_an_outcome_fixed_before_the_response_phase_stays() -> None:
    backend = Fixed(answer=ALICE)
    held = resolution(backend)
    assert held.form() == principal_form(ALICE)
    response = StreamingHttpResponse(iter([b"x"]))
    finish(held, response)
    assert held.form() == principal_form(ALICE)
    assert backend.calls == 1
    assert response.headers["Vary"] == "x-fixed"


# Concurrent first calls


class Gated(PrincipalBackend):
    """``resolve`` answers once ``release`` is set; ``aresolve`` once
    ``arelease`` is; each says when it started."""

    request_inputs = ()

    def __init__(self, sync: Principal, asynchronous: Principal) -> None:
        self.sync, self.asynchronous = sync, asynchronous
        self.started = threading.Event()
        self.astarted = threading.Event()
        self.release = threading.Event()
        self.arelease = threading.Event()
        self.calls: list[str] = []

    def resolve(self, request: HttpRequest) -> Principal | None:
        self.calls.append("sync")
        self.started.set()
        assert self.release.wait(30)
        return self.sync

    async def aresolve(self, request: HttpRequest) -> Principal | None:
        self.calls.append("async")
        self.astarted.set()
        while not self.arelease.is_set():
            await asyncio.sleep(0.001)
        return self.asynchronous


def _wait(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


@pytest.mark.spec("APP-56")
def test_a_sync_and_an_async_first_call_neither_waits_for_the_other() -> None:
    """The sync call runs on the thread-sensitive thread while the async
    one's search is in flight; each runs its own search, and both see the
    principal of the first to complete."""
    backend = Gated(ALICE, BOB)
    held = resolution(backend)

    async def both() -> tuple[bytes | None, bytes | None]:
        flight = asyncio.create_task(held.aform())
        while not backend.astarted.is_set():
            await asyncio.sleep(0.001)
        threaded = asyncio.create_task(sync_to_async(held.form)())
        while not backend.started.is_set():
            await asyncio.sleep(0.001)
        backend.arelease.set()
        first = await flight
        backend.release.set()
        return first, await threaded

    assert async_to_sync(both)() == (principal_form(BOB), principal_form(BOB))
    assert sorted(backend.calls) == ["async", "sync"]


@pytest.mark.spec("APP-56")
def test_async_first_calls_on_one_loop_share_one_search() -> None:
    backend = Gated(ALICE, BOB)
    held = resolution(backend)
    backend.arelease.set()

    async def many() -> list[bytes | None]:
        return await asyncio.gather(*(held.aform() for _ in range(5)))

    assert asyncio.run(many()) == [principal_form(BOB)] * 5
    assert backend.calls == ["async"]


@pytest.mark.spec("APP-56")
def test_async_first_calls_on_two_loops_each_search_and_see_one_principal() -> None:
    first, second = Principal(ISSUER, "1"), Principal(ISSUER, "2")
    answers = iter([first, second])

    class TwoLoops(PrincipalBackend):
        request_inputs = ()
        both = threading.Barrier(2, timeout=30)
        calls = 0

        def resolve(self, request: HttpRequest) -> Principal | None:
            raise AssertionError("not called")

        async def aresolve(self, request: HttpRequest) -> Principal | None:
            type(self).calls += 1
            await asyncio.to_thread(self.both.wait)
            return next(answers)

    held = resolution(TwoLoops())
    seen: list[bytes | None] = []

    def run() -> None:
        seen.append(asyncio.run(held.aform()))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert TwoLoops.calls == 2
    assert len(seen) == 2
    assert seen[0] == seen[1]


@pytest.mark.spec("APP-56")
def test_a_cancelled_first_call_leaves_a_waiting_caller_to_resolve() -> None:
    attempts: list[int] = []

    class Slow(PrincipalBackend):
        request_inputs = ()

        def resolve(self, request: HttpRequest) -> Principal | None:
            raise AssertionError("not called")

        async def aresolve(self, request: HttpRequest) -> Principal | None:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                await asyncio.Event().wait()  # until cancelled
            return ALICE

    held = resolution(Slow())

    async def scenario() -> bytes | None:
        first = asyncio.create_task(held.aform())
        while not attempts:
            await asyncio.sleep(0)
        waiting = asyncio.create_task(held.aform())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await waiting

    assert asyncio.run(scenario()) == principal_form(ALICE)
    assert attempts == [0, 1]


@pytest.mark.spec("APP-56")
def test_a_cancelled_first_call_records_nothing_for_a_later_one() -> None:
    attempts: list[int] = []

    class Slow(PrincipalBackend):
        request_inputs = ()

        def resolve(self, request: HttpRequest) -> Principal | None:
            return BOB

        async def aresolve(self, request: HttpRequest) -> Principal | None:
            attempts.append(1)
            await asyncio.Event().wait()
            return ALICE

    held = resolution(Slow())

    async def cancelled() -> None:
        task = asyncio.create_task(held.aform())
        while not attempts:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    assert not held.fixed
    assert held.form() == principal_form(BOB)


@pytest.mark.spec("APP-59", "APP-56")
def test_a_call_after_the_mark_fixes_a_failure_over_a_search_still_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A thread that copied the root's context is still searching when the
    response phase marks the root. A call made then fixes the failure, and
    the running search's principal is dropped: its caller fails too, and
    every later call, with one log."""
    started, release = threading.Event(), threading.Event()

    class Slow(PrincipalBackend):
        request_inputs = ()

        def resolve(self, request: HttpRequest) -> Principal | None:
            started.set()
            release.wait(timeout=30)
            return ALICE

    held = resolution(Slow())
    outcomes: list[str] = []

    def first() -> None:
        try:
            held.form()
        except ValueError as error:
            outcomes.append(str(error))
        else:
            outcomes.append("answered")

    thread = threading.Thread(target=first)
    thread.start()
    assert started.wait(timeout=30)
    finish(held, HttpResponse())
    with pytest.raises(ValueError, match="could not be resolved"):
        held.form()
    release.set()
    thread.join(timeout=30)
    assert len(outcomes) == 1
    assert "could not be resolved" in outcomes[0]
    with pytest.raises(ValueError, match="could not be resolved"):
        held.form()
    logged = [r.getMessage() for r in caplog.records if r.name == "neolith.host"]
    assert len(logged) == 1
    assert "a call came after the principal middleware's response phase" in logged[0]


# A call from inside the root's own search

_REENTERED = (
    "a call was made from the request root while its principal was being resolved"
)


def _again(held: Resolution) -> str:
    """What a call from the root, made by a backend, came to."""
    try:
        held.form()
    except ValueError as error:
        return str(error)
    return "answered"


async def _aagain(held: Resolution) -> str:
    try:
        await held.aform()
    except ValueError as error:
        return str(error)
    return "answered"


def _in_thread(held: Resolution) -> str:
    """The call made on a thread the backend starts with a copy of its
    context, as an executor given a copy does."""
    import contextvars

    got: list[str] = []
    context = contextvars.copy_context()
    thread = threading.Thread(
        target=context.run, args=(lambda: got.append(_again(held)),)
    )
    thread.start()
    thread.join(timeout=30)
    return got[0]


async def _in_task(held: Resolution) -> str:
    return await asyncio.create_task(_aagain(held))


async def _through_thread(held: Resolution) -> str:
    return await sync_to_async(_again, thread_sensitive=False)(held)


# How a backend reaches its own root again: (name, sync route, async route).
_ROUTES: list[tuple[str, Any, Any]] = [
    ("directly", _again, _aagain),
    ("from a thread or task it starts", _in_thread, _in_task),
    ("through sync_to_async", _again, _through_thread),
]


@pytest.mark.spec("APP-54", "APP-55")
@pytest.mark.parametrize("asynchronous", [False, True], ids=["resolve", "aresolve"])
@pytest.mark.parametrize("route", _ROUTES, ids=[r[0] for r in _ROUTES])
def test_a_backend_that_calls_from_the_root_it_resolves_fails_once(
    route: tuple[str, Any, Any], asynchronous: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A backend that calls from its own root, as one asking an identity app
    would, fixes a failure: its call fails at once rather than searching
    again or waiting for the search it is in, no later backend is asked,
    and one failure is logged."""
    _, sync_route, async_route = route
    seen: list[str] = []
    later = Fixed(answer=ALICE)

    class Calls(PrincipalBackend):
        request_inputs = ()

        def resolve(self, request: HttpRequest) -> Principal | None:
            seen.append(sync_route(held))
            return None

        async def aresolve(self, request: HttpRequest) -> Principal | None:
            seen.append(await async_route(held))
            return None

    held = resolution(Calls(), later)

    async def first() -> bytes | None:
        return await asyncio.wait_for(held.aform(), timeout=10)

    with pytest.raises(ValueError, match="could not be resolved"):
        asyncio.run(first()) if asynchronous else held.form()
    with pytest.raises(ValueError, match="could not be resolved"):
        held.form()
    assert len(seen) == 1
    assert "could not be resolved" in seen[0]
    assert later.calls == 0
    logged = [r.getMessage() for r in caplog.records if r.name == "neolith.host"]
    assert len(logged) == 1
    assert _REENTERED in logged[0]


@pytest.mark.spec("APP-56")
def test_a_call_from_another_root_s_search_is_not_a_reentry() -> None:
    """Only a call from the root being resolved is refused: a backend may
    resolve another root's principal, which searches as usual."""
    other = resolution(Fixed(answer=BOB))

    class Asks(PrincipalBackend):
        request_inputs = ()

        def resolve(self, request: HttpRequest) -> Principal | None:
            other.form()
            return ALICE

    held = resolution(Asks())
    assert held.form() == principal_form(ALICE)
    assert other.form() == principal_form(BOB)


# Django's cache middleware, called directly


@pytest.mark.spec("APP-60")
def test_two_principals_never_share_a_cache_entry() -> None:
    """The response phase with a backend that declares a custom header,
    then Django's cache middleware on the same request and response."""
    cache.clear()
    factory = RequestFactory()

    def served(user: str) -> tuple[HttpResponse | None, HttpRequest]:
        request = factory.get("/page/", HTTP_X_USER=user)
        hit = FetchFromCacheMiddleware(lambda r: HttpResponse()).process_request(
            request
        )
        return hit, request

    def store(user: str) -> None:
        hit, request = served(user)
        assert hit is None
        held = resolution(Header(), request=request)
        response = HttpResponse(held.form())
        finish(held, response)
        UpdateCacheMiddleware(lambda r: response).process_response(request, response)

    store("alice")
    assert served("alice")[0] is not None  # stored, so the case is not vacuous
    assert served("bob")[0] is None
    store("bob")
    alice, bob = served("alice")[0], served("bob")[0]
    assert alice is not None
    assert bob is not None
    assert alice.content != bob.content
    cache.clear()


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("middleware", "count"),
    [
        ([ROOT, UPDATE, SESSION, AUTH, PRINCIPAL, FETCH], 0),
        ([ROOT, SESSION, AUTH, PRINCIPAL, UPDATE], 1),
        (
            [ROOT, SESSION, AUTH, PRINCIPAL, "django.middleware.cache.CacheMiddleware"],
            1,
        ),
        ([ROOT, PRINCIPAL, UPDATE, UPDATE], 2),
        ([ROOT, UPDATE], 0),  # no principal middleware: E006's to report
        ((ROOT, PRINCIPAL, UPDATE), 0),  # not a list: E006's to report
    ],
)
def test_the_cache_middleware_is_listed_before_the_principal_middleware(
    middleware: object, count: int
) -> None:
    problems = _principal.cache_problems(middleware)
    assert len(problems) == count
    for problem in problems:
        assert f"listed after {PRINCIPAL}; list it before" in problem


# The Django-user backend


class _Request:
    """A request that holds a given user."""

    def __init__(self, user: object) -> None:
        self.user = user


@pytest.fixture
def backend() -> DjangoUserBackend:
    return DjangoUserBackend(issuer=ISSUER)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({}, "the option 'issuer' is required"),
        ({"issuer": ""}, "the option 'issuer' is required"),
        ({"issuer": "a b"}, "the option 'issuer' is required"),
        ({"issuer": "a\n"}, "the option 'issuer' is required"),
        ({"issuer": "é"}, "the option 'issuer' is required"),
        ({"issuer": "x" * 256}, "the option 'issuer' is required"),
        ({"issuer": b"x"}, "the option 'issuer' is required"),
        ({"issuer": None}, "the option 'issuer' is required"),
        (
            {"issuer": ISSUER, "subject_field": "email"},
            "the option 'subject_field' is not taken",
        ),
    ],
)
def test_the_django_user_backend_refuses_its_options(
    options: dict[str, object], message: str
) -> None:
    with pytest.raises(ImproperlyConfigured, match=message):
        DjangoUserBackend(**options)


@pytest.mark.parametrize("issuer", ["x", "x" * 255, "~!", ISSUER, "urn:example:users"])
def test_the_nearest_admitted_issuers_build(issuer: str) -> None:
    assert DjangoUserBackend(issuer=issuer).request_inputs == (
        "authorization",
        "cookie",
    )


def test_every_problem_of_the_django_user_backend_is_in_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.apps import apps

    monkeypatch.setattr(apps, "is_installed", lambda name: False)
    with (
        override_settings(MIDDLEWARE=[ROOT, PRINCIPAL, SESSION, AUTH]),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        DjangoUserBackend(issuer="a b", other=1)
    parts = str(raised.value).split("; ")
    assert parts[0] == "the option 'other' is not taken (the one option is 'issuer')"
    assert parts[1].startswith("the option 'issuer' is required")
    assert any("django.contrib.auth is not installed" in p for p in parts)
    assert sum("is not listed before" in p for p in parts) == 2


@pytest.mark.parametrize(
    ("middleware", "missing"),
    [
        ([ROOT, SESSION, AUTH, PRINCIPAL], []),
        ([ROOT, "tests.test_principal_backends.Sessions", AUTH, PRINCIPAL], []),
        ([ROOT, AUTH, PRINCIPAL], [SESSION]),
        ([ROOT, SESSION, PRINCIPAL, AUTH], [AUTH]),
        ([ROOT, PRINCIPAL, SESSION, AUTH], [SESSION, AUTH]),
        ([ROOT, SESSION, AUTH], []),  # no principal middleware: E006's to report
        (None, []),
    ],
)
def test_the_django_user_backend_follows_the_middleware_it_reads(
    middleware: object, missing: list[str]
) -> None:
    problems = user_order_problems(middleware)
    assert [p.split(",")[0].removeprefix("MIDDLEWARE: ") for p in problems] == missing


from django.contrib.sessions.middleware import SessionMiddleware  # noqa: E402


class Sessions(SessionMiddleware):
    """A subclass, which counts as the class."""


@isolate_apps("tests.host_apps.web")
def _models() -> dict[str, type]:
    from django.contrib.auth.models import AbstractBaseUser

    def model(name: str, *bases: type, **fields: Any) -> type:  # noqa: ANN401
        meta = type("Meta", (), {"app_label": "web"})
        attrs = {"__module__": __name__, "Meta": meta, **fields}
        return type(name, bases or (models.Model,), attrs)

    class OwnAuto(models.BigAutoField):  # type: ignore[type-arg]
        pass

    person = model("Person")
    found = [
        model("AutoKey", id=models.AutoField(primary_key=True)),
        model("BigKey", id=models.BigAutoField(primary_key=True)),
        model("SmallKey", id=models.SmallAutoField(primary_key=True)),
        model("UuidKey", id=models.UUIDField(primary_key=True, default=uuid.uuid4)),
        model("SubclassKey", id=OwnAuto(primary_key=True)),
        model("IntegerKey", id=models.IntegerField(primary_key=True)),
        model("TextKey", id=models.CharField(primary_key=True, max_length=10)),
        model("Inherited", person),
        model("AbstractUser", AbstractBaseUser, USERNAME_FIELD="id"),
    ]
    return {m.__name__: m for m in found}


@pytest.mark.parametrize(
    ("name", "admitted"),
    [
        ("AutoKey", True),
        ("BigKey", True),
        ("SmallKey", True),
        ("UuidKey", True),
        ("SubclassKey", True),
        ("AbstractUser", True),
        ("IntegerKey", False),
        ("TextKey", False),
        ("Inherited", False),
    ],
)
def test_the_key_field_rule(name: str, admitted: bool) -> None:
    problem = key_problem(_models()[name])
    assert (problem is None) == admitted
    if name == "Inherited":
        assert "(the key of a model that inherits from another model)" in str(problem)


def test_the_test_host_s_user_model_is_admitted() -> None:
    assert key_problem(get_user_model()) is None


def _user(key: object) -> Any:  # noqa: ANN401
    user: Any = get_user_model()()
    user.pk = key
    return user


_KEYS = st.integers(min_value=-(2**63), max_value=2**63 - 1) | st.uuids()


@given(_KEYS)
def test_every_admitted_key_gives_a_principal_whose_sent_form_reads_back(
    key: object,
) -> None:
    found = DjangoUserBackend(issuer=ISSUER).resolve(_Request(_user(key)))  # type: ignore[arg-type]
    assert found is not None
    assert found.issuer == ISSUER
    assert found.subject == str(key)
    assert found.claims == {}
    if isinstance(key, uuid.UUID):
        assert found.subject == found.subject.lower()
        assert len(found.subject) == 36
    else:
        assert found.subject == str(int(found.subject))
    assert read_principal(principal_form(found)) == found
    # Two backends with one issuer give equal principals for one user.
    assert DjangoUserBackend(issuer=ISSUER).resolve(_Request(_user(key))) == found  # type: ignore[arg-type]


@pytest.mark.parametrize("key", [True, None, "7", 7.0, (1, 2)])
def test_a_key_that_is_not_exactly_an_int_or_a_uuid_is_refused(
    backend: DjangoUserBackend, key: object
) -> None:
    with pytest.raises(TypeError, match="not exactly an int or a UUID"):
        backend.resolve(_Request(_user(key)))  # type: ignore[arg-type]


def test_a_user_who_is_not_logged_in_abstains(backend: DjangoUserBackend) -> None:
    assert backend.resolve(_Request(AnonymousUser())) is None  # type: ignore[arg-type]


class _Truthy:
    is_authenticated = 1
    pk = 1


def test_a_user_whose_is_authenticated_is_not_true_abstains(
    backend: DjangoUserBackend,
) -> None:
    assert backend.resolve(_Request(_Truthy())) is None  # type: ignore[arg-type]


def test_a_request_with_no_user_is_refused(backend: DjangoUserBackend) -> None:
    with pytest.raises(LookupError, match="AuthenticationMiddleware did not run"):
        backend.resolve(HttpRequest())
    with pytest.raises(LookupError, match="AuthenticationMiddleware did not run"):
        asyncio.run(backend.aresolve(HttpRequest()))


def test_a_lazy_user_with_no_auser_is_refused_on_the_async_path(
    backend: DjangoUserBackend,
) -> None:
    from django.utils.functional import SimpleLazyObject

    request = HttpRequest()
    request.user = SimpleLazyObject(AnonymousUser)  # type: ignore[assignment]
    with pytest.raises(LookupError, match="no auser"):
        asyncio.run(backend.aresolve(request))


def test_a_plain_user_is_used_as_it_is_on_the_async_path(
    backend: DjangoUserBackend,
) -> None:
    """``auser()`` is not asked when ``request.user`` is a plain instance."""
    request = HttpRequest()
    request.user = _user(7)

    async def other() -> object:
        raise AssertionError("auser() was asked")

    request.auser = other  # type: ignore[assignment]
    assert asyncio.run(backend.aresolve(request)) == Principal(ISSUER, "7")


def test_the_django_user_backend_s_refusal_reaches_e019_whole() -> None:
    """``build`` bounds a constructor's cause, but not the backend's own
    refusal, which lists every problem it found (ADR 0023 Decision 10)."""
    with override_settings(MIDDLEWARE=[ROOT, PRINCIPAL]):
        _, problems = build(
            [entry("neolith.principals.DjangoUserBackend", issuer="a b", extra=1)]
        )
    assert len(problems) == 1
    for part in (
        "the option 'extra' is not taken",
        "(hosts that share a user table share the issuer)",
        f"{SESSION}, or a subclass, is not listed before",
        f"{AUTH}, or a subclass, is not listed before",
    ):
        assert part in problems[0]


_SET_UP_BODY = """
import django
from django.core.exceptions import ImproperlyConfigured
try:
    django.setup()
except ImproperlyConfigured as error:
    print(error)
else:
    print("set up")
"""
_CONTENTTYPES = "django.contrib.contenttypes"
_SESSIONS = "django.contrib.sessions"
_AUTH_APP = "django.contrib.auth"
_GOOD = {"issuer": ISSUER}
_IN_ORDER = [ROOT, SESSION, AUTH, PRINCIPAL]


@pytest.mark.parametrize(
    ("undeclared", "middleware", "options", "deployer", "said"),
    [
        pytest.param(
            [_CONTENTTYPES, _AUTH_APP, _SESSIONS],
            _IN_ORDER,
            {"issuer": "a b"},
            {},
            ["the option 'issuer' is required", "share the issuer"],
            id="issuer outside the grammar",
        ),
        pytest.param(
            [_CONTENTTYPES, _AUTH_APP, _SESSIONS],
            _IN_ORDER,
            {**_GOOD, "extra": 1},
            {},
            ["the option 'extra' is not taken"],
            id="another option",
        ),
        pytest.param(
            [_CONTENTTYPES, _SESSIONS],
            _IN_ORDER,
            _GOOD,
            {},
            ["django.contrib.auth is not installed; add it to"],
            id="auth not installed",
        ),
        pytest.param(
            [_CONTENTTYPES, _SESSIONS],
            [ROOT, SESSION, PRINCIPAL],
            _GOOD,
            {},
            [
                "django.contrib.auth is not installed; add it to",
                f"{AUTH}, or a subclass, is not listed before",
            ],
            id="auth not installed nor listed",
        ),
        pytest.param(
            [_CONTENTTYPES, _AUTH_APP, _SESSIONS, "tests.principal_apps.textkey"],
            _IN_ORDER,
            _GOOD,
            {"AUTH_USER_MODEL": "textkey.User"},
            ["the user model textkey.User's key is a CharField"],
            id="key field",
        ),
        pytest.param(
            [_CONTENTTYPES, _AUTH_APP, _SESSIONS],
            [ROOT, PRINCIPAL, SESSION, AUTH],
            _GOOD,
            {},
            [
                f"{SESSION}, or a subclass, is not listed before",
                f"{AUTH}, or a subclass, is not listed before",
            ],
            id="middleware order",
        ),
        pytest.param(
            [_CONTENTTYPES, _AUTH_APP, _SESSIONS],
            [ROOT, PRINCIPAL],
            {"issuer": "a b", "extra": 1},
            {},
            [
                "the option 'extra' is not taken",
                "share the issuer",
                f"{SESSION}, or a subclass, is not listed before",
                f"{AUTH}, or a subclass, is not listed before",
            ],
            id="every row at once",
        ),
    ],
)
def test_each_refusal_of_the_django_user_backend_reaches_set_up(
    undeclared: list[str],
    middleware: list[str],
    options: dict[str, object],
    deployer: dict[str, object],
    said: list[str],
) -> None:
    """Decision 10's rows for the Django-user backend, as a deployer meets
    them: set-up in a fresh process, where nothing has imported auth's
    models before, reports each under the constructor's row of E019."""
    printed = in_built_host(
        _SET_UP_BODY,
        host_apps.HOSTED,
        {
            **deployer,
            "MIDDLEWARE": middleware,
            "NEOLITH": {
                **host_apps.NEOLITH,
                "UNDECLARED_APPS": undeclared,
                "PRINCIPAL_BACKENDS": [
                    {
                        "BACKEND": "neolith.principals.DjangoUserBackend",
                        "OPTIONS": options,
                    }
                ],
            },
        },
    )
    rows = [line for line in printed.splitlines() if "(neolith.E019)" in line]
    assert len(rows) == 1, printed
    assert rows[0].startswith(
        f"(neolith.E019) {SETTING}[0] (neolith.principals.DjangoUserBackend): "
        "the constructor raised"
    ), printed
    for part in said:
        assert part in rows[0], printed


def test_set_up_reports_a_backend_it_cannot_build(registered: None) -> None:
    """Set-up builds the backends after app loading and stops with every
    refusal under neolith.E019."""
    neolith = {
        **host_apps.SERVED,
        "PRINCIPAL_BACKENDS": [
            entry(f"{HERE}.NoInputs"),
            entry("neolith.principals.DjangoUserBackend"),
        ],
    }
    with (
        _in_place(NEOLITH=neolith),
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        pass
    lines = str(raised.value).splitlines()
    assert lines[1].startswith(
        f"(neolith.E019) {SETTING}[0] ({HERE}.NoInputs): has no request_inputs"
    )
    assert lines[2].startswith(
        f"(neolith.E019) {SETTING}[1] (neolith.principals.DjangoUserBackend): "
        "the constructor raised"
    )


def test_set_up_reports_the_cache_after_the_principal_middleware(
    registered: None,
) -> None:
    with (
        _in_place(MIDDLEWARE=[*host_apps.MIDDLEWARE, UPDATE]),
        pytest.raises(
            ImproperlyConfigured, match=r"\(neolith\.E020\) MIDDLEWARE: entry 4"
        ),
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        pass


def test_set_up_with_no_backend_does_not_check_the_cache_order(
    registered: None,
) -> None:
    neolith = {k: v for k, v in host_apps.SERVED.items() if k != "PRINCIPAL_BACKENDS"}
    with (
        _in_place(NEOLITH=neolith, MIDDLEWARE=[ROOT, PRINCIPAL, UPDATE]),
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        from neolith import _host

        host = _host._current()
        assert host is not None
        assert host.principals == ()
        assert host.vary == ()


@pytest.fixture
def registered() -> None:
    _checks.register()


def _in_place(**changed: object) -> Any:  # noqa: ANN401
    from tests.test_host_checks import _in_place as in_place

    return in_place(**changed)
