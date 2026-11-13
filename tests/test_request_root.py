"""The request root: one root for each request Django serves (spec 02, Request
roots).

``neolith.middleware.RootMiddleware``, first in ``MIDDLEWARE``, opens a root
when a request reaches it, binds it to the app whose code serves the view in
``process_view``, and closes it when the response is done, a streamed body
included (ADR 0017 Decision 4, ADR 0022 Decision 6, ``docs/django-host.md``).
These are the cases 1 to 6, 12 and 13 of the test plan on #11; the admin
cases, 7 and 8, are in ``test_request_admin.py``.

Each case runs on Django's own WSGI and ASGI handlers, not the test client,
so the server's iteration and closing of a response are the real ones, with
a sync and an async view. Time is a clock the tests set, never a sleep.
"""

import asyncio
import contextvars
import gc
import io
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers import base
from django.core.handlers.asgi import ASGIHandler
from django.core.handlers.wsgi import WSGIHandler
from django.test import override_settings

from neolith import _context
from neolith.middleware import RootMiddleware
from tests import host_apps, request_site
from tests.host_apps.couriers import services as courier_services

ROOT = "neolith.middleware.RootMiddleware"
CALLING = "tests.request_site.Calling"
SYNC_ONLY = "tests.request_site.sync_only"
MODES = ("wsgi", "asgi")
# A streamed body Django must adapt to the handler's mode warns as it does so.
_ADAPTED = r"StreamingHttpResponse must consume"


class Clock:
    """A clock the test sets: each reading returns ``time``."""

    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    made = Clock()
    monkeypatch.setattr(_context, "now", made)
    return made


def _site(*middleware: str, **more: object) -> Any:  # noqa: ANN401
    return override_settings(
        INSTALLED_APPS=[*host_apps.INSTALLED[:-1], "tests.plain_app", "neolith"],
        ROOT_URLCONF="tests.request_urls",
        MIDDLEWARE=[ROOT, *middleware],
        **more,
    )


@pytest.fixture
def site() -> Iterator[None]:
    request_site.SEEN.clear()
    request_site.HELD.clear()
    with _site():
        yield
    request_site.HELD.clear()


@dataclass
class Served:
    status: int
    body: bytes


def _environ(path: str) -> dict[str, Any]:
    route, _, query = path.partition("?")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": route,
        "QUERY_STRING": query,
        "HTTP_HOST": "testserver",
    }
    setup_testing_defaults(environ)
    return environ


class Started:
    """A WSGI server's ``start_response``: it keeps the status."""

    status = ""

    def __call__(
        self, status: str, headers: list[tuple[str, str]], exc_info: object = None, /
    ) -> Callable[[bytes], object]:
        self.status = status
        return lambda data: None


def wsgi(path: str) -> Served:
    """Serve ``path`` as a WSGI server does: call, iterate, close."""
    started = Started()
    result = WSGIHandler()(_environ(path), started)
    try:
        body = b"".join(result)
    finally:
        result.close()
    return Served(int(started.status.split()[0]), body)


def _scope(path: str) -> dict[str, Any]:
    route, _, query = path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": route,
        "raw_path": route.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


async def _asgi(
    path: str,
    *,
    disconnect: asyncio.Event | None = None,
    on_send: Callable[[Mapping[str, Any]], None] | None = None,
) -> Served:
    """Serve ``path`` through Django's ASGI handler. The client disconnects
    once ``disconnect`` is set, and otherwise stays until the response ends.
    """
    sent: list[Mapping[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await (disconnect or asyncio.Event()).wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)
        if on_send is not None:
            on_send(message)

    await ASGIHandler()(_scope(path), receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return Served(status, body)


def asgi(path: str) -> Served:
    return asyncio.run(_asgi(path))


def serve(mode: str, path: str) -> Served:
    return wsgi(path) if mode == "wsgi" else asgi(path)


def _closed(context: contextvars.Context) -> bool:
    """Whether a call made in ``context`` is refused as made in a closed
    scope."""
    scope = context.run(_context._CURRENT.get)
    return scope is not None and scope.closed and request_site.later() == "usage error"


# Opening, and the app a root belongs to


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("path", "outcome"),
    [
        ("/page/", "as web"),
        ("/apage/", "as web"),
        ("/cbv/", "as web"),
        ("/wrapped/", "as web"),
        ("/instance/", "as web"),
        ("/level/", "as stock"),
    ],
)
def test_a_view_calls_as_the_app_its_code_is_in(
    site: None, mode: str, path: str, outcome: str
) -> None:
    """Case 12: a view from another installed app calls as that app."""
    served = serve(mode, path)
    assert (served.status, served.body.decode()) == (200, outcome)
    assert [outcome] == request_site.SEEN


@pytest.mark.spec("TRN-57", "TRN-39")
@pytest.mark.parametrize("mode", MODES)
def test_a_view_of_an_app_with_no_declaration_calls_as_that_app(
    site: None, mode: str
) -> None:
    """The root belongs to the app, which requires nothing, so its call is a
    usage error at the caller rather than a call of no app."""
    assert serve(mode, "/plain/").body == b"usage error"


@pytest.mark.spec("TRN-58", "TRN-42")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", ["/project/", "/hidden/", "/partial/"])
def test_a_view_in_no_app_has_no_caller_and_is_refused(
    site: None, mode: str, path: str
) -> None:
    """A view in a project module, one whose decorator did not copy its
    module, and one the host cannot place all serve from no app."""
    assert serve(mode, path).body == b"refused unauthenticated"


@pytest.mark.spec("TRN-55", "TRN-56")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", ["/page/", "/apage/"])
@pytest.mark.parametrize("sync_only", [False, True], ids=["async-capable", "sync-only"])
def test_a_call_before_the_view_is_resolved_has_no_caller(
    mode: str, path: str, sync_only: bool
) -> None:
    """Case 6: another middleware's request phase runs in the root, before
    it is bound, so its call is refused as one of no app, not as a call made
    outside any scope; its ``process_view`` runs after binding. With a
    sync-only middleware below, Django runs the root middleware in sync mode
    under ASGI, and nothing changes."""
    request_site.SEEN.clear()
    chain = [SYNC_ONLY, CALLING] if sync_only else [CALLING]
    with _site(*chain):
        assert serve(mode, path).body == b"as web"
    assert request_site.SEEN == ["refused unauthenticated", "as web", "as web"]
    request_site.HELD.clear()


def test_without_the_root_a_middleware_call_is_made_in_no_scope() -> None:
    """The control for case 6: what the request phase gets with no root."""
    assert _context._CURRENT.get() is None
    assert request_site.ask() == "usage error"


@pytest.mark.spec("TRN-56")
@pytest.mark.parametrize("mode", MODES)
def test_an_error_view_for_an_unresolved_url_has_no_caller(
    site: None, mode: str
) -> None:
    """Case 13: no view was resolved, so the root is not bound, although the
    error view's code is in the web app."""
    served = serve(mode, "/nowhere/")
    assert (served.status, served.body) == (404, b"refused unauthenticated")


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("mode", MODES)
def test_an_error_view_for_an_error_in_a_view_is_in_the_bound_root(
    site: None, mode: str
) -> None:
    served = serve(mode, "/missing/")
    assert (served.status, served.body) == (404, b"as web")


@pytest.mark.spec("TRN-55")
def test_concurrent_requests_each_have_their_own_root(site: None) -> None:
    async def both() -> list[Served]:
        return list(await asyncio.gather(_asgi("/apage/"), _asgi("/level/")))

    first, second = asyncio.run(both())
    assert (first.body, second.body) == (b"as web", b"as stock")
    assert sorted(request_site.SEEN) == ["as stock", "as web"]


# The deadline


@pytest.mark.spec("TRN-59")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("neolith", "deadline"),
    [
        ({}, 1030.0),
        ({"REQUEST_DEADLINE": 5}, 1005.0),
        ({"REQUEST_DEADLINE": 0.5}, 1000.5),
        ({"REQUEST_DEADLINE": None}, None),
    ],
    ids=["absent", "int", "float", "none"],
)
def test_a_request_root_has_the_host_s_request_deadline(
    clock: Clock, mode: str, neolith: dict[str, object], deadline: float | None
) -> None:
    """30 seconds when ``REQUEST_DEADLINE`` is absent, the configured
    seconds, or none, counted from when the request starts. Set-up reads
    ``NEOLITH``, and Django loads the apps of an override before it applies
    the override's settings, so ``NEOLITH`` is overridden first."""
    request_site.SEEN.clear()
    with override_settings(NEOLITH=neolith), _site():
        assert serve(mode, "/page/").body == b"as web"
    assert courier_services.CONTEXTS[-1].deadline == deadline
    request_site.HELD.clear()


# Closing


@pytest.mark.spec("TRN-62", "TRN-25")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", ["/page/", "/apage/"])
def test_a_call_held_past_the_response_fails(site: None, mode: str, path: str) -> None:
    """Case 2: an executor that kept the request's context cannot call once
    the response is done."""
    assert serve(mode, path).body == b"as web"
    assert _closed(request_site.HELD[-1])
    assert not _context._OPEN


@pytest.mark.spec("TRN-60", "TRN-61", "TRN-62")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("view", ["stream", "astream"])
@pytest.mark.parametrize("body", ["sync", "async"])
def test_a_streamed_body_calls_in_the_root(
    site: None, mode: str, view: str, body: str
) -> None:
    """Case 1: the body runs after the middleware has returned, in whatever
    context the server iterates it, and its call is the view's app's; the
    root closes when the response is closed."""
    adapted = (mode == "wsgi") == (body == "async")
    if adapted:
        with pytest.warns(Warning, match=_ADAPTED):
            served = serve(mode, f"/{view}/{body}/")
    else:
        served = serve(mode, f"/{view}/{body}/")
    assert (served.status, served.body) == (200, b"<as web>")
    assert _closed(request_site.HELD[-1])


@pytest.mark.spec("TRN-62")
def test_a_streamed_body_that_fails_closes_its_root(site: None) -> None:
    result = WSGIHandler()(_environ("/fail/"), Started())
    with pytest.raises(RuntimeError, match="the body failed"):
        b"".join(result)
    assert _closed(request_site.HELD[-1])
    result.close()


@pytest.mark.spec("TRN-62")
def test_a_streamed_body_closed_before_it_ends_closes_its_root(site: None) -> None:
    """A WSGI server that stops early, as on a client disconnect, closes the
    response; the root closes with it."""
    result = WSGIHandler()(_environ("/stream/sync/"), Started())
    assert next(iter(result)) == b"<"
    assert not _closed(request_site.HELD[-1])
    result.close()
    assert _closed(request_site.HELD[-1])
    assert request_site.SEEN == ["usage error"]


@pytest.mark.spec("TRN-62")
@pytest.mark.parametrize("view", ["hang", "ahang"])
def test_an_asgi_client_disconnect_during_the_body_closes_the_root(
    site: None, view: str
) -> None:
    """Case 3: the body is suspended after its first chunk when the client
    goes; Django cancels the send and closes the response."""

    async def run() -> Served:
        gone = asyncio.Event()

        def on_send(message: Mapping[str, Any]) -> None:
            if message.get("body") == b"<":
                gone.set()

        return await _asgi(f"/{view}/", disconnect=gone, on_send=on_send)

    served = asyncio.run(run())
    assert served.body == b"<"
    assert _closed(request_site.HELD[-1])
    assert request_site.SEEN == ["usage error"]


@pytest.mark.spec("TRN-63")
@pytest.mark.parametrize("view", ["stream", "astream"])
def test_a_streamed_4xx_whose_client_goes_before_the_body_closes_on_collection(
    site: None, monkeypatch: pytest.MonkeyPatch, view: str
) -> None:
    """Case 4: Django's ASGI handler awaits ``log_response`` for a status of
    400 or more after the chain returns; a disconnect then cancels the
    request before anything is sent, and the response is never closed. The
    wrapper's finalizer closes the root once it is collected."""
    logging = threading.Event()
    release = threading.Event()
    real = vars(base)["log_response"]

    def held_log(*args: object, **kwargs: object) -> None:
        logging.set()
        release.wait(30)
        real(*args, **kwargs)

    monkeypatch.setattr(base, "log_response", held_log)

    async def run() -> None:
        gone = asyncio.Event()

        async def watch() -> None:
            await asyncio.to_thread(logging.wait, 30)
            gone.set()

        watcher = asyncio.create_task(watch())
        try:
            await _asgi_unsent(f"/{view}/async/?status=404", gone)
        finally:
            release.set()
            await watcher

    # No collection runs on its own meanwhile, so the root is seen open
    # until the test collects the wrapper.
    gc.disable()
    try:
        asyncio.run(run())
        assert not request_site.HELD  # the body never ran
        assert len(_context._OPEN) == 1
    finally:
        gc.enable()
    gc.collect()
    assert not _context._OPEN


async def _asgi_unsent(path: str, gone: asyncio.Event) -> None:
    sent: list[Mapping[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    await ASGIHandler()(_scope(path), receive, send)
    assert sent == []


class _FileWrapper:
    """A server's ``wsgi.file_wrapper``: it sends the file itself."""

    def __init__(self, file: io.BytesIO, block_size: int) -> None:
        self.file = file

    def __iter__(self) -> Iterator[bytes]:
        return iter([self.file.read()])

    def close(self) -> None:
        self.file.close()


@pytest.mark.spec("TRN-60", "TRN-62")
@pytest.mark.parametrize("path", ["/file/", "/afile/"])
def test_a_file_response_keeps_its_file_and_closes_its_root_on_return(
    site: None, path: str
) -> None:
    """Case 5, WSGI: the server still gets the file to send itself, and the
    root is closed before the body is sent, since reading a file runs no
    app's code."""
    environ = _environ(path)
    environ["wsgi.file_wrapper"] = _FileWrapper
    result: object = WSGIHandler()(environ, Started())
    assert type(result) is _FileWrapper
    assert _closed(request_site.HELD[-1])
    assert b"".join(result) == b"a file"
    result.close()


@pytest.mark.spec("TRN-60", "TRN-62")
@pytest.mark.parametrize("path", ["/file/", "/afile/"])
def test_a_file_response_over_asgi_is_sent_unwrapped(site: None, path: str) -> None:
    """Case 5, ASGI: the file is sent as Django sends a file, which warns
    that it reads a sync iterator, and its root is already closed."""
    with pytest.warns(Warning, match=_ADAPTED):
        assert asgi(path).body == b"a file"
    assert _closed(request_site.HELD[-1])


@pytest.mark.spec("TRN-62")
@pytest.mark.parametrize("stop", [KeyboardInterrupt, asyncio.CancelledError])
def test_a_signal_to_stop_from_the_chain_closes_the_root_and_goes_on(
    site: None, stop: type[BaseException]
) -> None:
    """However the request ends: a ``BaseException`` that escapes the chain
    still closes the root, and is not replaced."""
    held: list[contextvars.Context] = []

    def respond(request: object) -> Any:  # noqa: ANN401
        held.append(contextvars.copy_context())
        raise stop

    async def arespond(request: object) -> Any:  # noqa: ANN401
        return respond(request)

    with pytest.raises(stop):
        RootMiddleware(respond)(object())  # type: ignore[arg-type]
    middleware = RootMiddleware(arespond)
    with pytest.raises(stop):
        asyncio.run(middleware(object()))  # type: ignore[arg-type]
    assert len(held) == 2
    assert all(held_one.run(_context._CURRENT.get).closed for held_one in held)  # type: ignore[union-attr]
    assert _context._CURRENT.get() is None


# The middleware's own checks


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("middleware", "message"),
    [
        (
            ["tests.request_site.Calling", ROOT],
            f"MIDDLEWARE: {ROOT} must be the first entry",
        ),
        ([ROOT, ROOT], f"MIDDLEWARE: {ROOT} is listed 2 times"),
        ((ROOT,), "MIDDLEWARE: expected a list, got tuple"),
    ],
)
def test_a_handler_is_not_built_with_the_root_out_of_place(
    site: None, middleware: object, message: str
) -> None:
    """The constructor checks the list its handler is built from, since a
    later change of ``MIDDLEWARE`` runs no set-up."""
    with (
        override_settings(MIDDLEWARE=middleware),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        WSGIHandler()
    assert str(raised.value) == message


@pytest.mark.spec("APP-16")
def test_a_handler_is_not_built_where_the_host_is_not_set_up() -> None:
    with (
        override_settings(MIDDLEWARE=[ROOT]),
        pytest.raises(ImproperlyConfigured, match="the host is not set up"),
    ):
        WSGIHandler()


@pytest.mark.spec("TRN-57")
def test_the_app_is_found_for_each_request_not_kept(site: None) -> None:
    """An override that changes the installed apps between requests is
    seen by the next request."""
    assert wsgi("/plain/").body == b"usage error"
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        assert wsgi("/plain/").body == b"refused unauthenticated"
    assert wsgi("/plain/").body == b"usage error"


class _Hooked:
    """A view whose class runs code on attribute access."""

    reads = 0

    def __getattr__(self, name: str) -> object:
        type(self).reads += 1
        raise AttributeError(name)

    def __call__(self, request: object) -> None:
        return None


@pytest.mark.spec("TRN-57", "TRN-58")
def test_the_app_is_read_without_running_the_view_s_hooks(site: None) -> None:
    """A lazy object in the place of the admin's ``ModelAdmin``, or a view
    whose class hooks attribute access, is judged by its type: nothing of
    the author's runs, and code in no app has no app."""
    from django.utils.functional import SimpleLazyObject

    from neolith import middleware

    setups: list[None] = []

    def setup() -> object:
        setups.append(None)
        raise AssertionError("evaluated")

    def admin_view(request: object) -> None:
        return None

    admin_view.model_admin = SimpleLazyObject(setup)  # type: ignore[attr-defined]
    assert middleware._label(admin_view) is None
    assert setups == []
    assert middleware._label(_Hooked()) is None
    assert _Hooked.reads == 0
