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
import functools
import gc
import io
import threading
import types
import warnings
from collections.abc import Awaitable, Callable, Iterator, Mapping
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
from tests.hosting import in_fresh_process

ROOT = "neolith.middleware.RootMiddleware"
PRINCIPAL = "neolith.middleware.PrincipalMiddleware"
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
        MIDDLEWARE=[*host_apps.MIDDLEWARE, *middleware],
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
    on_send_async: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
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
        if on_send_async is not None:
            await on_send_async(message)

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
        ("/partial/", "as web"),
        ("/method/", "as web"),
        ("/wrapped-partial/", "as web"),
        ("/exempt/", "as web"),
        ("/own-dispatch/", "as web"),
        ("/guarded/", "as web"),
        ("/viewset/", "as web"),
        ("/viewset-unnamed/", "as web"),
        ("/cbv-partly-wrapped/", "as web"),
        ("/wraps-framework/", "as web"),
        ("/chain/", "as web"),
        ("/method-partial/", "as web"),
        ("/level/", "as stock"),
        ("/stock-hidden/", "as stock"),
    ],
)
def test_a_view_calls_as_the_app_its_code_is_in(
    site: None, mode: str, path: str, outcome: str
) -> None:
    """Case 12: a view from another installed app calls as that app. A
    wrapper is followed through the links it keeps (``__wrapped__``, a
    partial's function, a bound method's function); one that keeps none,
    in another app, serves as that app, as spec 02 says after TRN-57."""
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
@pytest.mark.parametrize("path", ["/project/", "/hidden/", "/odd-partial/"])
def test_a_view_in_no_app_has_no_caller_and_is_refused(
    site: None, mode: str, path: str
) -> None:
    """A view in a project module, a view wrapped by a project decorator
    that keeps no link to it, and a partial subclass, whose function the
    host never reads since a property could run code, serve from no app."""
    assert serve(mode, path).body == b"refused unauthenticated"


def test_a_partial_subclass_is_judged_by_its_type_alone(site: None) -> None:
    from neolith._serving import label_of
    from tests.host_apps.web import views as web

    odd = request_site.OddPartial(web.page)
    before = request_site.OddPartial.reads
    assert label_of(odd) is None
    assert request_site.OddPartial.reads == before


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
    with override_settings(NEOLITH={**host_apps.SERVED, **neolith}), _site():
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
    assert not _context.open_scopes()


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
        assert len(_context.open_scopes()) == 1
    finally:
        gc.enable()
    gc.collect()
    assert not _context.open_scopes()


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
            ["tests.request_site.Calling", ROOT, PRINCIPAL],
            f"MIDDLEWARE: {ROOT} must be the first entry",
        ),
        ([ROOT, ROOT, PRINCIPAL], f"MIDDLEWARE: {ROOT} is listed 2 times"),
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
        override_settings(INSTALLED_APPS=[], MIDDLEWARE=[ROOT, PRINCIPAL]),
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

    from neolith._serving import label_of

    setups: list[None] = []

    def setup() -> object:
        setups.append(None)
        raise AssertionError("evaluated")

    def admin_view(request: object) -> None:
        return None

    admin_view.model_admin = SimpleLazyObject(setup)  # type: ignore[attr-defined]
    assert label_of(admin_view) is None
    assert setups == []
    assert label_of(_Hooked()) is None
    assert _Hooked.reads == 0


# Round 1 of review: each way a body ends, the body's own clean-up, file-like
# objects, and the guards.


_UNDER_LOCK = """
import gc
import threading

import django

django.setup()

from django.test import Client

from neolith import _context
from neolith.middleware import _Body
from tests import request_site

gc.disable()
# A wrapper in a cycle, which only a collection frees, so its finalizer runs
# inside the collection.
scope, token = _context.begin(**_context.root_fields())
_context._CURRENT.reset(token)
closers = []
closers.append(_Body(iter([b"x"]), scope, closers))
del closers
# The test client's stream wrapper, partly read and dropped: a generator in
# a cycle whose finally closes the response, and so the body.
response = Client().get("/cleaned/")
next(iter(response.streaming_content))
del response


def collect():
    # A collection runs the dropped body's finally wherever it happens: here
    # inside an open root, so its call goes through and opens the provider's
    # scope, which takes the table's lock again in this thread.
    with _context.root("web", requires=("couriers.v3",)):
        with _context._OPEN_LOCK:
            gc.collect()
    done.append(scope.closed)


done = []
worker = threading.Thread(target=collect, daemon=True)
worker.start()
worker.join(20)
print("closed" if done == [True] else "deadlock")
print(len(_context.open_scopes()) if done else "-")
print(" ".join(request_site.SEEN).replace(" ", "-"))
"""


@pytest.mark.spec("TRN-63", "TRN-62")
def test_a_collection_while_the_table_s_lock_is_held_does_not_deadlock() -> None:
    """Nothing a collection runs takes a lock the thread may hold: the
    backstop's finalizer and the close that a dropped, partly read stream
    reaches from its generator's ``finally``. Run in a process of its own
    with a timeout, so a deadlock fails the test and leaves no lock held in
    the suite."""
    printed = in_fresh_process(
        _UNDER_LOCK,
        [*host_apps.HOSTED, "neolith"],
        {"ROOT_URLCONF": "tests.request_urls", "ALLOWED_HOSTS": ["testserver"]},
        timeout=60,
    )
    assert printed.split() == ["closed", "0", "as-web"]


def _scope_for_body() -> tuple[
    _context.Scope, contextvars.Token[_context.Scope | None]
]:
    return _context.begin(**_context.root_fields())


def _sync_body(end: str) -> Iterator[bytes]:
    yield b"<"
    if end == "fails":
        raise RuntimeError("the body failed")
    if end == "interrupted":
        raise KeyboardInterrupt


async def _async_body(end: str) -> Any:  # noqa: ANN401
    yield b"<"
    if end == "fails":
        raise RuntimeError("the body failed")
    if end == "interrupted":
        raise asyncio.CancelledError


def _is_closed(scope: _context.Scope) -> bool:
    return scope.closed


_ENDS = {
    "exhausted": StopIteration,
    "fails": RuntimeError,
    "interrupted": BaseException,
}


@pytest.mark.spec("TRN-62")
@pytest.mark.parametrize("end", ["exhausted", "fails", "interrupted", "closed"])
def test_a_sync_body_closes_its_root_however_it_ends(end: str) -> None:
    """Before any server closes the response: the body's end alone closes
    the root, and runs the closers it took over inside it."""
    from neolith.middleware import _Body

    scope, token = _scope_for_body()
    _context._CURRENT.reset(token)
    seen: list[object] = []
    body = _Body(_sync_body(end), scope, [lambda: seen.append(_context._CURRENT.get())])
    assert next(body) == b"<"
    assert not _is_closed(scope)
    if end == "closed":
        body.close()
    else:
        with pytest.raises(_ENDS[end]):
            next(body)
    assert _is_closed(scope)
    assert seen == [scope]
    assert _context._CURRENT.get() is None


@pytest.mark.spec("TRN-62")
@pytest.mark.parametrize("end", ["exhausted", "fails", "interrupted", "closed"])
def test_an_async_body_closes_its_root_however_it_ends(end: str) -> None:
    from neolith.middleware import _AsyncBody

    scope, token = _scope_for_body()
    _context._CURRENT.reset(token)
    seen: list[object] = []
    body = _AsyncBody(
        _async_body(end), scope, [lambda: seen.append(_context._CURRENT.get())]
    )

    async def run() -> None:
        assert await anext(body) == b"<"
        assert not scope.closed
        if end == "closed":
            body.close()
        else:
            expected = StopAsyncIteration if end == "exhausted" else _ENDS[end]
            with pytest.raises(expected):
                await anext(body)

    asyncio.run(run())
    assert scope.closed
    assert seen == [scope]


@pytest.mark.spec("TRN-62")
def test_an_async_body_that_fails_over_asgi_closes_its_root(site: None) -> None:
    """Django's ASGI handler raises the body's error before it closes the
    response, so the wrapper's own close is all that closes the root."""
    # Django 5.2's handler leaves the request's body file open when the body
    # raises (6.1 closes it with contextlib.closing); it is collected here.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.disable()
        try:
            with pytest.raises(RuntimeError, match="the body failed"):
                asgi("/afail/")
            assert _closed(request_site.HELD[-1])
        finally:
            gc.enable()
            gc.collect()


@pytest.mark.spec("TRN-61", "TRN-62")
def test_a_body_s_clean_up_on_an_early_close_runs_in_the_root(site: None) -> None:
    """A WSGI server that stops after the first chunk closes the response;
    the generator's ``finally`` then runs inside the root."""
    result = WSGIHandler()(_environ("/cleaned/"), Started())
    assert next(iter(result)) == b"<"
    result.close()
    assert request_site.SEEN == ["as web"]


@pytest.mark.spec("TRN-61")
@pytest.mark.parametrize("mode", MODES)
def test_a_body_s_clean_up_at_its_end_runs_in_the_root(site: None, mode: str) -> None:
    if mode == "asgi":
        with pytest.warns(Warning, match=_ADAPTED):
            assert serve(mode, "/cleaned/").body == b"<>"
    else:
        assert serve(mode, "/cleaned/").body == b"<>"
    assert request_site.SEEN == ["as web"]


@pytest.mark.spec("TRN-61", "TRN-62")
def test_a_disconnect_inside_the_body_s_await_cleans_up_in_the_root(
    site: None,
) -> None:
    """The cancellation lands inside a step the wrapper runs, so the body's
    ``finally`` runs inside the root, and then the root closes."""

    async def run() -> None:
        gone = asyncio.Event()

        def on_send(message: Mapping[str, Any]) -> None:
            if message.get("body") == b"<":
                gone.set()

        await _asgi("/acleaned/", disconnect=gone, on_send=on_send)

    asyncio.run(run())
    assert request_site.SEEN == ["as web"]
    assert _closed(request_site.HELD[-1])


@pytest.mark.spec("TRN-25")
def test_an_async_body_stopped_at_a_yield_cleans_up_after_the_response(
    site: None,
) -> None:
    """A disconnect while the server is sending a chunk stops the body at
    its ``yield``. The ASGI handler never closes an async generator, so the
    event loop finishes it after the response is done, and its call is
    refused, as spec 02 says after TRN-61."""

    async def run() -> None:
        gone = asyncio.Event()

        async def blocked(message: Mapping[str, Any]) -> None:
            if message.get("body") == b"<":
                gone.set()
                await asyncio.Event().wait()

        await _asgi("/acleaned/", disconnect=gone, on_send_async=blocked)

    asyncio.run(run())
    assert request_site.SEEN == ["usage error"]
    assert _closed(request_site.HELD[-1])


@pytest.mark.spec("TRN-61", "TRN-60")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", ["/appfile/", "/aappfile/"])
def test_a_file_response_over_an_app_s_file_like_object_reads_in_the_root(
    site: None, mode: str, path: str
) -> None:
    """Reading a file-like object that is not a plain file may run the
    app's code, so the body is wrapped and its reads are made in the root."""
    if mode == "asgi":
        with pytest.warns(Warning, match=_ADAPTED):
            served = serve(mode, path)
    else:
        environ = _environ(path)
        environ["wsgi.file_wrapper"] = _FileWrapper
        result: Any = WSGIHandler()(environ, Started())
        assert type(result) is not _FileWrapper
        served = Served(200, b"".join(result))
        result.close()
    assert served.body == b"as web"
    assert _closed(request_site.HELD[-1])


@pytest.mark.spec("TRN-60")
def test_a_file_response_over_a_file_python_opened_is_left_to_the_server(
    site: None,
) -> None:
    environ = _environ("/realfile/")
    environ["wsgi.file_wrapper"] = _FileWrapper
    result: object = WSGIHandler()(environ, Started())
    assert type(result) is _FileWrapper
    assert _closed(request_site.HELD[-1])
    result.close()


@pytest.mark.parametrize(
    ("value", "plain"),
    [
        (io.BytesIO(b"x"), True),
        (io.BufferedReader(io.BytesIO(b"x")), False),
        (io.StringIO("x"), False),
        (_FileWrapper(io.BytesIO(b"x"), 1), False),
    ],
)
def test_only_a_plain_file_is_left_unwrapped(value: object, plain: bool) -> None:
    from neolith.middleware import _plain_file

    assert _plain_file(value) is plain


def test_a_file_python_opened_is_plain(tmp_path: Any) -> None:  # noqa: ANN401
    from neolith.middleware import _plain_file

    path = tmp_path / "f"
    path.write_bytes(b"x")
    with path.open("rb") as buffered, path.open("rb", buffering=0) as raw:
        assert _plain_file(buffered)
        assert _plain_file(raw)


@pytest.mark.spec("TRN-57")
def test_a_middleware_that_copies_the_request_leaves_the_root_bound() -> None:
    request_site.SEEN.clear()
    with _site("tests.request_site.copies_request"):
        assert wsgi("/page/").body == b"as web"
    request_site.HELD.clear()


@pytest.mark.spec("APP-16")
def test_a_handler_is_not_built_with_the_root_under_a_second_path(
    site: None,
) -> None:
    with (
        override_settings(MIDDLEWARE=[ROOT, PRINCIPAL, "tests.request_site.Alias"]),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        WSGIHandler()
    assert str(raised.value) == f"MIDDLEWARE: {ROOT} is listed 2 times"


@pytest.mark.spec("APP-16")
def test_a_request_that_arrives_once_the_host_is_gone_is_refused(site: None) -> None:
    handler = WSGIHandler()
    with override_settings(INSTALLED_APPS=[]):
        started = Started()
        result = handler(_environ("/project/"), started)
        result.close()
    assert started.status.startswith("500")
    assert request_site.SEEN == []


def test_the_middleware_leaves_no_root_behind_in_its_caller(site: None) -> None:
    from neolith import middleware

    def respond(request: object) -> Any:  # noqa: ANN401
        assert middleware._REQUEST.get() is not None
        return None

    RootMiddleware(respond)(object())  # type: ignore[arg-type]
    assert middleware._REQUEST.get() is None
    assert _context._CURRENT.get() is None


def test_process_view_runs_in_the_chain_s_mode(site: None) -> None:
    """In an async chain ``process_view`` is a coroutine function, so Django
    does not switch threads to call it."""
    import inspect

    async def arespond(request: object) -> None:
        return None

    chains: list[Any] = [arespond, lambda request: None]
    modes = [
        inspect.iscoroutinefunction(RootMiddleware(c).process_view) for c in chains
    ]
    assert modes == [True, False]


@pytest.mark.spec("TRN-57")
def test_a_call_reads_the_app_it_checked(
    site: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root bound while a call is under way: the call carries the app
    whose ``requires`` it checked, never the app bound after the check."""
    checked = _context.check_required

    def rebound(scope: _context.Scope, identity: str) -> str | None:
        app = checked(scope, identity)
        scope.bind("stock", frozenset({"couriers.v3"}))
        return app

    monkeypatch.setattr(_context, "check_required", rebound)
    with _context.root("web", requires=("couriers.v3",)):
        assert request_site.ask() == "as web"


def test_check_required_reads_the_owner_once() -> None:
    class Flipping:
        reads = 0

        @property
        def owner(self) -> tuple[str, frozenset[str]]:
            type(self).reads += 1
            if self.reads == 1:
                return ("web", frozenset({"couriers.v3"}))
            return ("stock", frozenset())

    assert _context.check_required(Flipping(), "couriers.v3") == "web"  # type: ignore[arg-type]
    assert Flipping.reads == 1


@pytest.mark.spec("TRN-57")
def test_a_wrapper_loop_has_no_app() -> None:
    from neolith._serving import label_of

    def loop(request: object) -> None:
        return None

    loop.__wrapped__ = loop  # type: ignore[attr-defined]
    assert label_of(loop) is None


@pytest.mark.spec("TRN-62")
def test_a_closer_that_fails_stops_neither_the_others_nor_the_close() -> None:
    from neolith.middleware import _Body

    scope, token = _scope_for_body()
    _context._CURRENT.reset(token)
    ran: list[str] = []

    def fails() -> None:
        ran.append("fails")
        raise OSError("cannot close")

    body = _Body(iter([b"x"]), scope, [fails, lambda: ran.append("after")])
    body.close()
    body.close()
    assert ran == ["fails", "after"]
    assert _is_closed(scope)


@pytest.mark.spec("TRN-62")
@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
def test_a_signal_to_stop_from_a_closer_still_closes_the_root(
    stop: type[BaseException],
) -> None:
    """The signal goes on, never replaced, with a note; the closers after it
    do not run; the root closes."""
    from neolith.middleware import _Body

    scope, token = _scope_for_body()
    _context._CURRENT.reset(token)
    ran: list[str] = []

    def stops() -> None:
        raise stop

    body = _Body(iter([b"x"]), scope, [stops, lambda: ran.append("after")])
    with pytest.raises(stop) as raised:
        body.close()
    assert raised.value.__notes__ == [
        "neolith: the request root closed; 1 more of the body's closers did not run"
    ]
    assert ran == []
    assert _is_closed(scope)
    assert _context._CURRENT.get() is None


# The class check for the which-app rule: every wrapper owner, every way of
# writing a wrapper, and every kind of target, each with the app that
# spec 02 (TRN-57 and the paragraph after it) predicts.

_OWNERS = {
    "hosted": "tests.host_apps.stock.views",
    "unhosted app": "tests.plain_app.views",
    "project": "tests.request_site",
    "Django, in no app": "django.views.decorators.cache",
    "Django, in a contrib app": "django.contrib.auth.decorators",
}
_FORMS = ("no link", "wraps", "wraps(assigned=())", "update_wrapper")
# Each target's own app, as label_of gives it with no wrapper (the view
# rules of spec 02 after TRN-57), and whether it is hosted code.
_TARGETS = {
    "hosted function": ("web", True),
    "hosted CBV": ("web", True),
    "project function": (None, False),
    "Django function, in no app": (None, False),
    "Django function, in a contrib app": ("flatpages", False),
    "Django CBV, in no app": (None, False),
    "Django CBV, in a contrib app": ("auth", False),
    "partial": ("web", True),
    "bound method over a partial": ("web", True),
}


def _targets() -> dict[str, object]:
    from django.contrib.auth.views import LoginView
    from django.contrib.flatpages.views import flatpage
    from django.views import defaults
    from django.views.generic import TemplateView

    from tests.host_apps.web import views as web

    return {
        "hosted function": web.page,
        "hosted CBV": web.PageView.as_view(),
        "project function": request_site.project_page,
        "Django function, in no app": defaults.page_not_found,
        "Django function, in a contrib app": flatpage,
        "Django CBV, in no app": TemplateView.as_view(template_name="x.html"),
        "Django CBV, in a contrib app": LoginView.as_view(),
        "partial": functools.partial(web.page),
        "bound method over a partial": types.MethodType(
            functools.partial(web.Pages.page), web.Pages()
        ),
    }


def _wrapper(owner: str, form: str, target: object) -> object:
    """A wrapper around ``target`` whose code is in the owner's module, as
    if it were written there, in the given form."""
    import importlib

    def template(request: object) -> object:
        return target(request)  # type: ignore[operator]

    module = importlib.import_module(_OWNERS[owner])
    wrapper = types.FunctionType(
        template.__code__, vars(module), "wrapper", None, template.__closure__
    )
    if form == "wraps":
        return functools.wraps(target)(wrapper)  # type: ignore[arg-type]
    if form == "wraps(assigned=())":
        return functools.wraps(target, assigned=())(wrapper)  # type: ignore[arg-type]
    if form == "update_wrapper":
        return functools.update_wrapper(wrapper, target)  # type: ignore[arg-type]
    return wrapper


def _predicted(owner: str, form: str, target: str) -> str | None:
    """The app spec 02 predicts for a wrapper (the paragraph after TRN-57)."""
    own_app, hosted = _TARGETS[target]
    if form == "no link":
        # Nothing to follow: the wrapper is its own code, judged as a view.
        return {
            "hosted": "stock",
            "unhosted app": "plain_app",
            "project": None,
            "Django, in no app": None,
            "Django, in a contrib app": "auth",
        }[owner]
    if owner == "project" or owner.startswith("Django"):
        # A wrapper whose code is no app's only wraps: it is the code it
        # wraps, wherever its link leads.
        return own_app
    if hosted:
        # An app's wrapper follows a link into hosted code.
        return "web"
    # Not followed: the wrapper is its app's code.
    return "stock" if owner == "hosted" else "plain_app"


@pytest.fixture
def contrib_site() -> Iterator[None]:
    """The test host with Django's contrib apps a wrapper or a view can come
    from installed beside it."""
    # django.contrib.auth is installed on the test host already.
    contrib = [
        "django.contrib.sites",
        "django.contrib.flatpages",
    ]
    with override_settings(
        INSTALLED_APPS=[
            *contrib,
            *host_apps.INSTALLED[:-1],
            "tests.plain_app",
            "neolith",
        ]
    ):
        yield


@pytest.mark.spec("TRN-57", "TRN-58")
@pytest.mark.parametrize("target", list(_TARGETS))
@pytest.mark.parametrize("form", _FORMS)
@pytest.mark.parametrize("owner", list(_OWNERS))
def test_every_wrapper_serves_as_the_spec_predicts(
    contrib_site: None, owner: str, form: str, target: str
) -> None:
    """Every wrapper owner (with Django's code both in no app and in a
    contrib app), every way of writing a wrapper, and every kind of target."""
    from neolith._serving import label_of

    view = _targets()[target]
    assert label_of(view) == _TARGETS[target][0]
    assert label_of(_wrapper(owner, form, view)) == _predicted(owner, form, target)


@pytest.mark.spec("TRN-57", "TRN-58")
@pytest.mark.parametrize(
    ("target", "app"),
    [
        ("project", None),
        ("unhosted app", "plain_app"),
        ("unhosted app, partial", "plain_app"),
        ("hosted", "web"),
        ("flatpage", "flatpages"),
        ("Django CBV", None),
    ],
)
def test_login_required_is_the_code_it_wraps(
    contrib_site: None, target: str, app: str | None
) -> None:
    from django.contrib.auth.decorators import login_required
    from django.contrib.flatpages.views import flatpage
    from django.views.generic import TemplateView

    from neolith._serving import label_of
    from tests.host_apps.web import views as web
    from tests.plain_app import views as plain

    views: dict[str, Any] = {
        "project": request_site.project_page,
        "unhosted app": plain.page,
        "unhosted app, partial": functools.partial(plain.page),
        "hosted": web.page,
        "flatpage": flatpage,
        "Django CBV": TemplateView.as_view(template_name="x.html"),
    }
    assert label_of(login_required(views[target])) == app


# The sentences after TRN-57 as equations, each checked over every cell of
# the matrix used as the inner view, so a rule that holds one layer deep but
# not when wrappers compose fails here.

_CELLS = [
    (owner, form, target) for owner in _OWNERS for form in _FORMS for target in _TARGETS
]


def _django_wrappers() -> dict[str, Callable[[Any], Any]]:
    from django.contrib.auth.decorators import login_required
    from django.views.decorators.cache import never_cache

    return {"login_required": login_required, "never_cache": never_cache}


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("cell", _CELLS, ids=lambda c: "-".join(c))
@pytest.mark.parametrize("django_wrapper", ["login_required", "never_cache"])
def test_a_django_wrapper_is_the_code_it_wraps(
    contrib_site: None, django_wrapper: str, cell: tuple[str, str, str]
) -> None:
    """``label_of(D(x)) == label_of(x)``: a wrapper the framework supplies is
    the code it wraps, wherever its link leads."""
    from neolith._serving import label_of

    inner = _wrapper(*cell[:2], _targets()[cell[2]])
    outer = _django_wrappers()[django_wrapper](inner)
    assert label_of(outer) == label_of(inner)


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("cell", _CELLS, ids=lambda c: "-".join(c))
def test_a_partial_is_its_function_s_code(
    contrib_site: None, cell: tuple[str, str, str]
) -> None:
    """``label_of(partial(x)) == label_of(x)``: a wrapper that runs no code
    of its own is the code it wraps."""
    from neolith._serving import label_of

    inner = _wrapper(*cell[:2], _targets()[cell[2]])
    assert label_of(functools.partial(inner)) == label_of(inner)  # type: ignore[arg-type,operator,misc]


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("cell", _CELLS, ids=lambda c: "-".join(c))
def test_a_bound_method_is_its_function_s_code(
    contrib_site: None, cell: tuple[str, str, str]
) -> None:
    """``label_of(MethodType(x, obj)) == label_of(x)``."""
    from neolith._serving import label_of

    inner = _wrapper(*cell[:2], _targets()[cell[2]])
    assert label_of(types.MethodType(inner, object())) == label_of(inner)  # type: ignore[arg-type]


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("cell", _CELLS, ids=lambda c: "-".join(c))
@pytest.mark.parametrize("app_wrapper", ["hosted", "unhosted app"])
@pytest.mark.parametrize("form", ["wraps", "wraps(assigned=())", "update_wrapper"])
def test_an_app_s_wrapper_follows_only_into_hosted_code(
    contrib_site: None, app_wrapper: str, form: str, cell: tuple[str, str, str]
) -> None:
    """``label_of(W(x)) == label_of(x)`` when ``x``'s own code is hosted,
    and ``W``'s app otherwise: an app's wrapper follows its link only into
    code of an app the host holds, and is its own app's code when it does
    not, whatever the code it wraps carries."""
    from neolith._serving import label_of

    inner = _wrapper(*cell[:2], _targets()[cell[2]])
    outer = _wrapper(app_wrapper, form, inner)
    own_app = "stock" if app_wrapper == "hosted" else "plain_app"
    # The inner wrapper serves as a hosted app when its code is hosted, or
    # when it is followed into hosted code: a wrapper that is the code it
    # wraps, or an app's wrapper, keeping a link to a hosted target.
    following = (
        "project",
        "Django, in no app",
        "Django, in a contrib app",
        "unhosted app",
    )
    inner_is_hosted = cell[0] == "hosted" or (
        cell[0] in following and cell[1] != "no link" and _TARGETS[cell[2]][1]
    )
    expected = label_of(inner) if inner_is_hosted else own_app
    assert label_of(outer) == expected


def _transparent() -> dict[str, Callable[[Any], Any]]:
    """Each wrapper that is the code it wraps: Django's, the project's, a
    partial and a bound method."""
    from django.contrib.auth.decorators import login_required
    from django.views.decorators.cache import never_cache
    from django.views.decorators.csrf import csrf_exempt
    from django.views.decorators.http import require_POST

    return {
        "login_required": login_required,
        "never_cache": never_cache,
        "csrf_exempt": csrf_exempt,
        "require_POST": require_POST,
        "project wraps": lambda x: _wrapper("project", "wraps", x),
        "partial": functools.partial,
        "bound method": lambda x: types.MethodType(x, object()),
    }


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("form", ["wraps", "wraps(assigned=())", "update_wrapper"])
@pytest.mark.parametrize("app_wrapper", ["hosted", "unhosted app"])
@pytest.mark.parametrize(
    "transparent",
    [
        "login_required",
        "never_cache",
        "csrf_exempt",
        "require_POST",
        "project wraps",
        "partial",
        "bound method",
    ],
)
def test_a_wrapper_that_is_the_code_it_wraps_is_transparent_under_an_app_s(
    contrib_site: None, transparent: str, app_wrapper: str, form: str
) -> None:
    """``label_of(W(T(x))) == label_of(W(x))``: an app's wrapper ``W`` sees
    past a wrapper ``T`` that is the code it wraps, for every matrix cell and
    every target used as ``x``."""
    from neolith._serving import label_of

    wrap = _transparent()[transparent]
    targets = _targets()
    inner_views = [targets[name] for name in _TARGETS] + [
        _wrapper(owner, cell_form, targets[target])
        for owner, cell_form, target in _CELLS
    ]
    wrong = [
        index
        for index, x in enumerate(inner_views)
        if label_of(_wrapper(app_wrapper, form, wrap(x)))
        != label_of(_wrapper(app_wrapper, form, x))
    ]
    assert wrong == []


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize(
    ("view", "app"),
    [
        ("stock_gate(web.page)", "web"),
        ("stock_gate(login_required(web.page))", "web"),
        ("plain_gate(require_POST(web.page))", "web"),
        ("stock_gate(project_wrap(web.page))", "web"),
        ("stock_gate(login_required(project_wrap(web.page)))", "web"),
        ("stock_gate(login_required(plain.page))", "stock"),
        ("plain_gate(never_cache(project_page))", "plain_app"),
    ],
)
def test_an_app_s_wrapper_judges_its_link_past_transparent_wrappers(
    contrib_site: None, view: str, app: str
) -> None:
    from django.contrib.auth.decorators import login_required
    from django.views.decorators.cache import never_cache
    from django.views.decorators.http import require_POST

    from neolith._serving import label_of
    from tests.host_apps.web import views as web
    from tests.plain_app import views as plain

    def stock_gate(x: object) -> object:
        return _wrapper("hosted", "wraps", x)

    def plain_gate(x: object) -> object:
        return _wrapper("unhosted app", "wraps", x)

    def project_wrap(x: object) -> object:
        return _wrapper("project", "wraps", x)

    views = {
        "stock_gate(web.page)": stock_gate(web.page),
        "stock_gate(login_required(web.page))": stock_gate(login_required(web.page)),
        "plain_gate(require_POST(web.page))": plain_gate(require_POST(web.page)),
        "stock_gate(project_wrap(web.page))": stock_gate(project_wrap(web.page)),
        "stock_gate(login_required(project_wrap(web.page)))": stock_gate(
            login_required(project_wrap(web.page))  # type: ignore[call-overload]
        ),
        "stock_gate(login_required(plain.page))": stock_gate(
            login_required(plain.page)
        ),
        "plain_gate(never_cache(project_page))": plain_gate(
            never_cache(request_site.project_page)
        ),
    }
    assert label_of(views[view]) == app
