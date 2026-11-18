"""A request's principal on the test host (spec 05, A request's principal).

The test host lists the Django-user backend alone, so its union of request
inputs is ``("authorization", "cookie")`` (ADR 0023 Consequences). These
are the cases that need Django's own middleware chain: cases 9 to 11 of
the test plan on #11, ``login``, ``logout``, ``alogin`` and ``alogout``
before a call, a first call made too late, a user put on the request from
``Authorization``, and Django's cache middleware. Each runs on Django's own
WSGI and ASGI handlers, and on ASGI with a sync-only middleware after the
principal middleware, so Django runs it in sync mode.
"""

import asyncio
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth import middleware as auth_middleware
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.asgi import ASGIHandler
from django.core.handlers.wsgi import WSGIHandler
from django.db import connections
from django.db.backends.signals import connection_created
from django.test import Client, override_settings

from neolith import _principal
from tests import host_apps, request_site
from tests.host_apps.couriers import services as courier_services
from tests.host_apps.web import principals as views

ROOT = "neolith.middleware.RootMiddleware"
SESSION = "django.contrib.sessions.middleware.SessionMiddleware"
AUTH = "django.contrib.auth.middleware.AuthenticationMiddleware"
PRINCIPAL = "neolith.middleware.PrincipalMiddleware"
SYNC_ONLY = "tests.request_site.sync_only"
UPDATE = "django.middleware.cache.UpdateCacheMiddleware"
FETCH = "django.middleware.cache.FetchFromCacheMiddleware"
MODES = ("wsgi", "asgi", "asgi, principal middleware in sync mode")
ISSUER = host_apps.ISSUER
# Generated per run: no secret is stored in the repository (CONTRIBUTING K9).
OTHER_KEY = secrets.token_urlsafe(50)

pytestmark = pytest.mark.django_db(transaction=True, databases=["default"])


@dataclass
class Served:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def vary(self) -> list[str]:
        found = self.headers.get("vary")
        return [] if found is None else [n.strip() for n in found.split(",")]


def _environ(path: str, headers: dict[str, str], method: str) -> dict[str, Any]:
    route, _, query = path.partition("?")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": route,
        "QUERY_STRING": query,
        "HTTP_HOST": "testserver",
    }
    for name, value in headers.items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    setup_testing_defaults(environ)
    return environ


def wsgi(
    path: str,
    headers: dict[str, str],
    *,
    before: Callable[[], None],
    method: str = "GET",
) -> Served:
    """Serve ``path`` as a WSGI server does; ``before`` runs once the
    response is returned and before its body is iterated."""
    started: dict[str, Any] = {}

    def start(status: str, sent: list[tuple[str, str]], exc_info: object = None):  # type: ignore[no-untyped-def]  # noqa: ANN202
        started["status"] = status
        started["headers"] = {k.lower(): v for k, v in sent}
        return lambda data: None

    result = WSGIHandler()(_environ(path, headers, method), start)
    before()
    try:
        body = b"".join(result)
    finally:
        result.close()
    return Served(int(started["status"].split()[0]), body, started["headers"])


async def _asgi(
    path: str, headers: dict[str, str], before: Callable[[], None], method: str
) -> Served:
    route, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": route,
        "raw_path": route.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")]
        + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent: list[Mapping[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            before()
        sent.append(message)

    await ASGIHandler()(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return Served(
        start["status"],
        body,
        {k.decode().lower(): v.decode() for k, v in start["headers"]},
    )


def serve(
    mode: str,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    before: Callable[[], None] = lambda: None,
    method: str = "GET",
) -> Served:
    given = headers or {}
    if mode == "wsgi":
        return wsgi(path, given, before=before, method=method)
    return asyncio.run(_asgi(path, given, before, method))


def _middleware(mode: str, *outside: str, after: tuple[str, ...] = ()) -> list[str]:
    inside = [SYNC_ONLY] if mode.endswith("sync mode") else []
    return [ROOT, *outside, SESSION, AUTH, PRINCIPAL, *after, *inside]


# The test database's connections this module opened, kept open: Django
# never closes an in-memory SQLite database, since that would drop it.
_KEPT: list[Any] = []


@pytest.fixture(autouse=True, scope="module")
def _later_tests_find_no_connection() -> Iterator[None]:
    """Django closes old connections at the end of every request, which
    touches an open connection; in a later test without database access
    pytest-django refuses that. So the connections this module opened are
    taken off Django's wrappers when it ends, and kept open, so the
    in-memory test database lives on for any later test that connects."""
    yield
    for alias in connections:
        wrapper = connections[alias]
        if wrapper.connection is not None:
            _KEPT.append(wrapper.connection)
            wrapper.connection = None


@pytest.fixture(autouse=True)
def _threads_close_their_connections() -> Iterator[None]:
    """The handlers' worker threads open database connections that outlive
    the request; Python 3.13 and later warn when one is collected open.
    Each one a thread other than the test's opened is closed when the test
    ends (Django opens SQLite with ``check_same_thread=False``)."""
    main = threading.get_ident()
    opened: list[Any] = []

    def created(sender: object, connection: Any, **kwargs: object) -> None:  # noqa: ANN401
        if threading.get_ident() != main:
            opened.append(connection.connection)

    connection_created.connect(created, weak=False)
    try:
        yield
    finally:
        connection_created.disconnect(created)
        for raw in opened:
            raw.close()


@pytest.fixture
def site() -> Iterator[None]:
    request_site.WHOM.clear()
    views.RAN.clear()
    with override_settings(ROOT_URLCONF="tests.request_urls"):
        yield
    request_site.HELD.clear()


def _users() -> dict[str, str]:
    """Two users, each logged in once: the session cookie of each."""
    cookies: dict[str, str] = {}
    for name in ("first", "second"):
        user = get_user_model().objects.create_user(name)
        client = Client()
        client.force_login(user)
        cookies[name] = f"sessionid={client.cookies['sessionid'].value}"
    return cookies


def _subject(name: str) -> str:
    return str(get_user_model().objects.get(username=name).pk)


@pytest.fixture
def users(site: None) -> dict[str, str]:
    return _users()


class Reads:
    """Counts how often Django's ``AuthenticationMiddleware`` reads the
    request's user, sync or async."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        auth: Any = auth_middleware.auth  # type: ignore[attr-defined]
        sync, asynchronous = auth_middleware.get_user, auth.aget_user

        def counted(request: Any) -> Any:  # noqa: ANN401
            self.count += 1
            return sync(request)

        async def acounted(request: Any) -> Any:  # noqa: ANN401
            self.count += 1
            return await asynchronous(request)

        monkeypatch.setattr(auth_middleware, "get_user", counted)
        monkeypatch.setattr(auth, "aget_user", acounted)


# Case 9: cost, and a streamed body


@pytest.mark.spec("APP-57", "APP-61")
@pytest.mark.parametrize("view", ["quiet", "aquiet"])
@pytest.mark.parametrize("mode", MODES)
def test_a_page_that_makes_no_call_reads_no_user_and_gains_no_vary(
    users: dict[str, str], mode: str, view: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(monkeypatch)
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, f"/principal/{view}/", {"Cookie": users["first"]})
    assert served.body == b"quiet"
    assert reads.count == 0
    assert served.vary == []


@pytest.mark.spec("APP-51", "APP-52", "APP-60")
@pytest.mark.parametrize("mode", MODES)
def test_a_page_that_calls_carries_the_user_and_varies_on_both_inputs(
    users: dict[str, str], mode: str
) -> None:
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, "/principal/who/", {"Cookie": users["first"]})
    assert served.body == f"user {_subject('first')}".encode()
    assert courier_services.CONTEXTS[-1].principal.issuer == ISSUER  # type: ignore[union-attr]
    # SessionMiddleware writes Cookie; the host adds authorization.
    assert sorted(n.lower() for n in served.vary) == ["authorization", "cookie"]


@pytest.mark.spec("APP-53", "APP-60")
@pytest.mark.parametrize("mode", MODES)
def test_a_visitor_who_is_not_logged_in_calls_with_no_principal(
    site: None, mode: str
) -> None:
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, "/principal/who/")
    assert served.body == b"nobody"
    assert "authorization" in served.vary


@pytest.mark.spec("APP-58", "APP-60")
# A body Django adapts to the handler's mode warns as it does so.
@pytest.mark.filterwarnings("ignore:StreamingHttpResponse must consume")
@pytest.mark.parametrize("view", ["stream", "astream"])
@pytest.mark.parametrize("mode", MODES)
def test_a_streamed_body_has_its_principal_resolved_before_it_is_returned(
    users: dict[str, str], mode: str, view: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body's first call happens while the server iterates it; the
    user was read, and Vary set, before the response was returned."""
    reads = Reads(monkeypatch)
    at_return: list[int] = []
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(
            mode,
            f"/principal/{view}/",
            {"Cookie": users["first"]},
            before=lambda: at_return.append(reads.count),
        )
    assert at_return == [1]
    assert served.body == f"<user {_subject('first')}".encode()
    assert sorted(n.lower() for n in served.vary) == ["authorization", "cookie"]


# Case 10, and its siblings: the user a view logged in or out


@pytest.mark.spec("APP-51", "APP-53")
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", "first"),
        ("do=logout", None),
        ("do=login&as=second", "second"),
    ],
)
@pytest.mark.parametrize("mode", MODES)
def test_a_sync_view_calls_as_the_user_on_the_request(
    users: dict[str, str], mode: str, query: str, expected: str | None
) -> None:
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, f"/principal/who/?{query}", {"Cookie": users["first"]})
    want = "nobody" if expected is None else f"user {_subject(expected)}"
    assert served.body == want.encode()


@pytest.mark.spec("APP-51", "APP-53")
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Case 10: not as the user who just logged out, on both versions.
        ("do=alogout", None),
        ("do=alogin&as=second", "second"),
        ("via=thread", "first"),
        ("do=alogout&via=thread", None),
        ("do=alogin&as=second&via=thread", "second"),
    ],
)
@pytest.mark.parametrize("mode", MODES)
def test_an_async_view_calls_as_the_user_on_the_request(
    users: dict[str, str], mode: str, query: str, expected: str | None
) -> None:
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, f"/principal/awho/?{query}", {"Cookie": users["first"]})
    want = "nobody" if expected is None else f"user {_subject(expected)}"
    assert served.body == want.encode()


@pytest.mark.spec("APP-53", "APP-58")
@pytest.mark.filterwarnings("ignore:StreamingHttpResponse must consume")
@pytest.mark.parametrize("mode", MODES)
def test_an_async_view_that_logs_out_streams_with_no_principal(
    users: dict[str, str], mode: str
) -> None:
    """Case 10 where the response phase resolves: ``alogout`` leaves a plain
    anonymous user on the request, while Django 5.2's ``auser()`` still
    returns the user who logged out; the backend reads the plain user."""
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, "/principal/alogout-stream/", {"Cookie": users["first"]})
    assert served.body == b"<nobody"


@pytest.mark.spec("APP-54", "APP-55")
@pytest.mark.parametrize("mode", MODES)
def test_a_sync_call_in_the_event_loop_that_reads_the_session_fails(
    users: dict[str, str], mode: str
) -> None:
    """A synchronous first call made in the event loop reads the session
    there, Django refuses it, and the failure is the root's (ADR 0023
    Consequences): async code calls through ``sync_to_async``."""
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, "/principal/awho/", {"Cookie": users["first"]})
    assert served.body == b"refused internal"


# Case 11: concurrent first calls


@pytest.mark.spec("APP-56")
@pytest.mark.parametrize("view", ["pair", "apair"])
@pytest.mark.parametrize("mode", MODES)
def test_two_concurrent_first_calls_share_a_principal_and_a_trace(
    users: dict[str, str], mode: str, view: str
) -> None:
    before = len(courier_services.CONTEXTS)
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, f"/principal/{view}/", {"Cookie": users["first"]})
    assert served.body == b"pair"
    assert [f"user {_subject('first')}"] * 2 == request_site.WHOM
    first, second = courier_services.CONTEXTS[before:]
    assert first.principal == second.principal
    assert first.trace_id == second.trace_id


# One principal per root, from the moment it is bound

CALLS_IN_VIEW = "tests.request_site.CallsInView"
ANSWERS_IN_VIEW = "tests.request_site.AnswersInView"
CSRF = "django.middleware.csrf.CsrfViewMiddleware"


def _around(mode: str, probe: str, place: str) -> list[str]:
    """The test host's middleware with ``probe`` listed at ``place``:
    outside the two the backend reads, between them and the principal
    middleware, or after it."""
    inside = [SYNC_ONLY] if mode.endswith("sync mode") else []
    listed = {
        "outside": [ROOT, probe, SESSION, AUTH, PRINCIPAL],
        "before the principal middleware": [ROOT, SESSION, AUTH, probe, PRINCIPAL],
        "after the principal middleware": [ROOT, SESSION, AUTH, PRINCIPAL, probe],
    }[place]
    return [*listed, *inside]


PLACES = (
    "outside",
    "before the principal middleware",
    "after the principal middleware",
)


@pytest.mark.spec("APP-51", "APP-56")
@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("view", ["who", "awho"])
@pytest.mark.parametrize("mode", MODES)
def test_a_call_from_any_process_view_of_a_bound_root_has_its_principal(
    users: dict[str, str], mode: str, view: str, place: str
) -> None:
    """A middleware's ``process_view`` runs in the bound root wherever it is
    listed, so its call and the view's carry the one principal."""
    with override_settings(MIDDLEWARE=_around(mode, CALLS_IN_VIEW, place)):
        served = serve(mode, f"/principal/{view}/", {"Cookie": users["first"]})
    me = f"user {_subject('first')}"
    assert served.body == me.encode()
    assert [me, me] == request_site.WHOM
    assert sorted(n.lower() for n in served.vary) == ["authorization", "cookie"]


@pytest.mark.spec("APP-51", "APP-60")
@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("mode", MODES)
def test_a_process_view_that_answers_calls_with_the_principal(
    users: dict[str, str], mode: str, place: str
) -> None:
    """A middleware that answers from ``process_view``, before the principal
    middleware's place or after it, calls as the user."""
    with override_settings(MIDDLEWARE=_around(mode, ANSWERS_IN_VIEW, place)):
        served = serve(mode, "/principal/quiet/", {"Cookie": users["first"]})
    me = f"user {_subject('first')}"
    assert served.body == me.encode()
    assert [me] == request_site.WHOM
    assert "authorization" in [n.lower() for n in served.vary]


@pytest.mark.spec("APP-51", "APP-60")
@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("mode", MODES)
def test_a_csrf_failure_view_calls_with_the_principal(
    users: dict[str, str], mode: str, place: str
) -> None:
    """Django runs ``CSRF_FAILURE_VIEW`` from the CSRF middleware's
    ``process_view``, in the bound root, wherever that middleware is
    listed."""
    with override_settings(
        MIDDLEWARE=_around(mode, CSRF, place),
        CSRF_FAILURE_VIEW="tests.request_site.csrf_failure",
    ):
        served = serve(
            mode, "/principal/quiet/", {"Cookie": users["first"]}, method="POST"
        )
    me = f"user {_subject('first')}"
    assert served.status == 403
    assert served.body == me.encode()
    assert [me] == request_site.WHOM


@pytest.mark.spec("APP-57", "APP-61")
@pytest.mark.filterwarnings("ignore:StreamingHttpResponse must consume")
@pytest.mark.parametrize("mode", MODES)
def test_a_stream_from_a_root_never_bound_records_nothing(
    users: dict[str, str], mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A streamed answer from a request phase leaves the root unbound: the
    response phase searches for nobody, records nothing and logs nothing,
    and the response gains no Vary."""
    middleware = _middleware(mode, after=("tests.request_site.Streams",))
    with (
        caplog.at_level("DEBUG", logger="neolith"),
        override_settings(MIDDLEWARE=middleware),
    ):
        served = serve(mode, "/principal/who/", {"Cookie": users["first"]})
    assert (served.status, served.body) == (200, b"ok")
    assert request_site.WHOM == []
    assert "authorization" not in [n.lower() for n in served.vary]
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []


# The latest point


@pytest.mark.spec("APP-59", "APP-55")
@pytest.mark.parametrize("mode", MODES)
def test_a_first_call_after_the_response_phase_fails_and_reads_nothing(
    users: dict[str, str], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A middleware outside the principal middleware calls from its
    response phase: the call fails with ``internal``, no user is read, and
    the response gains no Vary from the host."""
    reads = Reads(monkeypatch)
    middleware = _middleware(mode, "tests.request_site.LateCaller")
    with override_settings(MIDDLEWARE=middleware):
        served = serve(mode, "/principal/quiet/", {"Cookie": users["first"]})
    assert served.body == b"quiet"
    assert request_site.WHOM == ["refused internal"]
    assert reads.count == 0
    assert "authorization" not in served.vary


@pytest.mark.spec("APP-59", "APP-61")
@pytest.mark.parametrize("mode", MODES)
def test_a_late_first_call_from_a_root_never_bound_fails_too(
    site: None, mode: str
) -> None:
    """An unresolved URL's root is never bound: its error view's call has no
    caller, and a call after the response phase fails as on a bound root."""
    middleware = _middleware(mode, "tests.request_site.LateCaller")
    with override_settings(MIDDLEWARE=middleware):
        served = serve(mode, "/no-such-page/")
    assert served.status == 404
    assert request_site.WHOM == ["refused internal"]
    assert served.vary == []


@pytest.mark.spec("APP-59")
@pytest.mark.parametrize("mode", MODES)
def test_a_late_first_call_leaves_a_session_on_a_fallback_key_as_it_was(
    users: dict[str, str], mode: str
) -> None:
    """With ``SECRET_KEY_FALLBACKS`` in use, reading the user would cycle
    the session's key; a late first call reads nothing, so the session's
    row and key stay as they were."""
    key = users["first"].removeprefix("sessionid=")
    from django.conf import settings

    middleware = _middleware(mode, "tests.request_site.LateCaller")
    with override_settings(
        MIDDLEWARE=middleware,
        SECRET_KEY=OTHER_KEY,
        SECRET_KEY_FALLBACKS=[settings.SECRET_KEY],
    ):
        served = serve(mode, "/principal/quiet/", {"Cookie": users["first"]})
    assert request_site.WHOM == ["refused internal"]
    assert "set-cookie" not in served.headers
    assert Session.objects.filter(session_key=key).exists()


@pytest.mark.parametrize("mode", MODES)
def test_a_call_inside_the_principal_middleware_cycles_a_fallback_key_and_saves_it(
    users: dict[str, str], mode: str
) -> None:
    """The nearest legitimate shape: the view's own call reads the user,
    which cycles the key, and SessionMiddleware, outside the principal
    middleware, saves the session and sends its new cookie."""
    key = users["first"].removeprefix("sessionid=")
    from django.conf import settings

    with override_settings(
        MIDDLEWARE=_middleware(mode),
        SECRET_KEY=OTHER_KEY,
        SECRET_KEY_FALLBACKS=[settings.SECRET_KEY],
    ):
        served = serve(mode, "/principal/who/", {"Cookie": users["first"]})
    assert served.body == f"user {_subject('first')}".encode()
    assert not Session.objects.filter(session_key=key).exists()
    assert "sessionid=" in served.headers["set-cookie"]


# A user that other code put on the request


@pytest.mark.spec("APP-52", "APP-60")
@pytest.mark.parametrize("mode", MODES)
def test_a_user_put_on_the_request_from_authorization_is_the_principal(
    users: dict[str, str], mode: str
) -> None:
    """As Django REST framework's setter does: the call carries that user,
    and the response varies on ``authorization`` and ``cookie``."""
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, "/principal/bearer/", {"Authorization": "Bearer second"})
    assert served.body == f"user {_subject('second')}".encode()
    assert sorted(n.lower() for n in served.vary) == ["authorization", "cookie"]


@pytest.mark.spec("APP-54", "APP-55", "APP-60")
@pytest.mark.parametrize("mode", MODES)
def test_an_authenticated_user_that_is_not_a_model_instance_fails(
    site: None, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    with override_settings(MIDDLEWARE=_middleware(mode)):
        served = serve(mode, "/principal/stranger/")
    assert served.body == b"refused internal"
    assert "authorization" in served.vary
    logged = [r.getMessage() for r in caplog.records if r.name == "neolith.host"]
    assert any(
        "DjangoUserBackend" in m and "not an instance of the user model" in m
        for m in logged
    )


# Django's cache middleware


@pytest.mark.spec("APP-60")
@pytest.mark.parametrize("mode", MODES)
def test_two_logged_in_users_never_share_a_cache_entry(
    users: dict[str, str], mode: str
) -> None:
    """``UpdateCacheMiddleware`` before the principal middleware, and
    ``FetchFromCacheMiddleware`` after: the cache keys on the Vary the host
    wrote. A second request of one user is served from the cache, so the
    case is not vacuous; the other user's is not."""
    cache.clear()
    middleware = _middleware(mode, UPDATE, after=(FETCH,))
    with override_settings(MIDDLEWARE=middleware):
        one = serve(mode, "/principal/cached/", {"Cookie": users["first"]})
        again = serve(mode, "/principal/cached/", {"Cookie": users["first"]})
        other = serve(mode, "/principal/cached/", {"Cookie": users["second"]})
    cache.clear()
    assert one.body == again.body == f"user {_subject('first')}".encode()
    assert other.body == f"user {_subject('second')}".encode()
    assert views.RAN["cached"] == 2


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    "middleware",
    [
        [ROOT, SESSION, AUTH, PRINCIPAL, UPDATE],
        [ROOT, SESSION, AUTH, PRINCIPAL, "django.middleware.cache.CacheMiddleware"],
    ],
)
def test_a_handler_is_not_built_with_the_cache_after_the_principal_middleware(
    site: None, middleware: list[str]
) -> None:
    with (
        override_settings(MIDDLEWARE=middleware),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        WSGIHandler()
    assert str(raised.value).startswith("MIDDLEWARE: entry 4 (")
    assert f"listed after {PRINCIPAL}" in str(raised.value)


def test_the_test_host_has_the_django_user_backend_alone() -> None:
    from neolith import _host

    host = _host._current()
    assert host is not None
    assert [b.path for b in host.principals] == ["neolith.principals.DjangoUserBackend"]
    assert host.vary == ("authorization", "cookie")
    assert _principal.KEY in host_apps.SERVED
