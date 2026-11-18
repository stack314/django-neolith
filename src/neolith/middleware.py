"""The host's two middleware: a request root for each request, and its
principal (ADR 0017 Decision 4, ADR 0022 Decision 6, ADR 0023).

List ``"neolith.middleware.RootMiddleware"`` first in ``MIDDLEWARE``, once,
and ``"neolith.middleware.PrincipalMiddleware"`` once, after it and after
every middleware a principal backend reads: with the Django-user backend,
after ``SessionMiddleware`` and ``AuthenticationMiddleware``; with any
backend, after Django's ``UpdateCacheMiddleware``.

The root middleware. A request on a path the host owns, under a provided
contract version's ``/<name>/v<N>/`` or under ``/.well-known/neolith/``, it
answers itself, and never hands it to the rest of the chain or opens a root
for it: the provider side opens the call's own scope
(``neolith._endpoint``; ADR 0011 Decision 2). For each other request it
opens a root, with the host's
request deadline (``NEOLITH["REQUEST_DEADLINE"]``, 30 seconds when absent,
none when ``None``) counted from when the request reaches it. The root
belongs to no app until the view is resolved; then it belongs to the app
whose code serves the view (``neolith._serving``). It stays open until the
response is done, a streamed body included, and then closes, so a call kept
past it is refused. How each case is met is in the Django host notes
(``docs/django-host.md``).

State the middleware owns (CONTRIBUTING K6): a private context variable
that holds, while a request runs, its root, through which ``process_view``
binds it. Closing never goes through a context variable: the middleware, or
the wrapper it puts around a streamed body, closes the root through its own
reference.
"""

import contextlib
import contextvars
import inspect
import io
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Final, final

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import FileResponse, HttpRequest, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.utils.cache import patch_vary_headers

from neolith import _boot, _checks, _context, _endpoint, _host, _principal
from neolith._serving import label_of

__all__ = ["PrincipalMiddleware", "RootMiddleware"]

_Respond = Callable[[HttpRequest], HttpResponseBase]
_ARespond = Callable[[HttpRequest], Awaitable[HttpResponseBase]]
_Tokens = tuple[
    contextvars.Token[_context.Scope | None],
    contextvars.Token[_context.Scope | None],
]

# While a request runs, its root, so process_view binds the root of the
# request it runs in. Set and reset by the middleware's call alone; a nested
# handler sets its own.
_REQUEST: Final[contextvars.ContextVar[_context.Scope | None]] = contextvars.ContextVar(
    "neolith_request_root", default=None
)
_NO_HOST: Final = (
    f"{_checks.ROOT_MIDDLEWARE}: the host is not set up; build the settings "
    "with neolith.host.build_settings, which installs neolith after the apps "
    "it hosts"
)


@final
class RootMiddleware:
    """Answer each request on a path the host owns, and open, bind and
    close the root of every other request.

    Django runs it in the mode of the handler inside it, sync or async, and
    it behaves the same in both. Its constructor refuses a ``MIDDLEWARE``
    in which it is not the first entry, or in which this class is listed
    more than once under any path, and a process where the host is not set
    up, so a handler is never built with it in the wrong place. Then it
    boots the host, once per process (ADR 0022 Decision 7): ``ROOT_URLCONF``
    is set and imports, and every schema is current; a refusal is kept, so
    every later handler in the process is refused alike. A request
    that arrives while the host is not set up, as under an override that
    leaves neolith out, is refused as well.

    Args:
        get_response: The rest of the chain, as Django passes it.

    Raises:
        ImproperlyConfigured: If ``MIDDLEWARE`` is not a list of str whose
            first entry is this class, listed once, if no set-up of the
            host has passed, or if Boot refuses, now or earlier in the
            process.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: _Respond | _ARespond) -> None:
        """Check the middleware's place and the host, and keep the chain."""
        problems = _checks.middleware_problems(getattr(settings, "MIDDLEWARE", None))
        if problems:
            raise ImproperlyConfigured("\n".join(problems))
        if _host._current() is None:
            raise ImproperlyConfigured(_NO_HOST)
        _boot.boot()
        self._get_response = get_response
        self._async = inspect.iscoroutinefunction(get_response)
        if self._async:
            inspect.markcoroutinefunction(self)
            # Django calls process_view in the handler's mode; a method in
            # the other mode costs a thread switch per request.
            self.process_view = self._aprocess_view  # type: ignore[method-assign,assignment]

    def __call__(
        self, request: HttpRequest
    ) -> HttpResponseBase | Awaitable[HttpResponseBase]:
        """Answer a request on an owned path, or serve one inside its root."""
        if self._async:
            return self._acall(request)
        host = _host._current()
        if host is not None and host.endpoints.owns(request.path):
            return _endpoint.answer(request, host.endpoints, host.callers.backends)
        scope, tokens = _open()
        handed = False
        try:
            respond: _Respond = self._get_response  # type: ignore[assignment]
            response = respond(request)
            handed = _hand_over(response, scope)
            return response
        finally:
            _leave(scope, tokens, handed)

    async def _acall(self, request: HttpRequest) -> HttpResponseBase:
        host = _host._current()
        if host is not None and host.endpoints.owns(request.path):
            return await _endpoint.aanswer(
                request, host.endpoints, host.callers.backends
            )
        scope, tokens = _open()
        handed = False
        try:
            respond: _ARespond = self._get_response  # type: ignore[assignment]
            response = await respond(request)
            handed = _hand_over(response, scope)
            return response
        finally:
            _leave(scope, tokens, handed)

    def process_view(
        self,
        request: HttpRequest,
        view_func: object,
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> None:
        """Bind the request's root to the app whose code serves the view,
        and, in a host with a principal backend, give a root bound to an
        app the host holds the request and the backends in the same step.

        Django calls it once the URL is resolved, before any other
        middleware's ``process_view``, since this middleware comes first.
        So no code runs in a bound root before its principal can be
        resolved.

        Args:
            request: The request.
            view_func: The resolved view.
            view_args: Its positional arguments.
            view_kwargs: Its keyword arguments.
        """
        _bind(request, view_func)

    async def _aprocess_view(
        self,
        request: HttpRequest,
        view_func: object,
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> None:
        _bind(request, view_func)


def _open() -> tuple[_context.Scope, _Tokens]:
    """Open the request's root, of no app, with the host's request deadline
    counted from now, and make it current. A host that is not set up has
    no deadline to give, so the request is refused.
    """
    host = _host._current()
    if host is None:
        raise ImproperlyConfigured(_NO_HOST)
    timeout = host.request_deadline
    scope, token = _context.begin(**_context.root_fields(timeout=timeout))
    return scope, (token, _REQUEST.set(scope))


def _leave(scope: _context.Scope, tokens: _Tokens, handed: bool) -> None:
    """Close the root unless a streamed body took it over, and make the
    variables what they were.
    """
    if not handed:
        _context.close(scope)
    current, request = tokens
    # A call left in another context than it was entered in cannot reset the
    # variables there; the root they name is closed, or the body's to close.
    with contextlib.suppress(ValueError):
        _REQUEST.reset(request)
    with contextlib.suppress(ValueError):
        _context._CURRENT.reset(current)


def _bind(request: HttpRequest, view: object) -> None:
    """Bind the request's root to the view's app. In a host with a backend,
    a root bound to an app the host holds gets its resolution first, so a
    call that sees the root bound, in any thread, sees its resolution too.
    """
    scope = _REQUEST.get()
    if scope is None:
        return
    label = label_of(view)
    requires: frozenset[str] | None = None
    host = _host._current()
    if label is not None and host is not None and label in host.apps:
        requires = frozenset(host.apps[label])
    # A root of no app, or of one this host does not hold, makes no call
    # that is sent (TRN-56, TRN-58), so no search starts for it.
    if (
        requires is not None
        and host is not None
        and host.principals
        and scope.resolution is None
    ):
        scope.resolution = _principal.Resolution(
            host.principals, host.vary, request, scope.held.trace_id
        )
    scope.bind(label, requires)


def _plain_file(value: object) -> bool:
    """Whether reading ``value`` runs no Python code: a file Python opened,
    buffered or not, or bytes in memory.
    """
    kind = type(value)
    if kind is io.FileIO or kind is io.BytesIO:
        return True
    return kind is io.BufferedReader and type(value.raw) is io.FileIO  # type: ignore[attr-defined]


def _kept(response: HttpResponseBase) -> bool:
    """Whether the root middleware keeps the response's body inside the
    root: every streamed body but a ``FileResponse`` over a plain file.
    """
    if not isinstance(response, StreamingHttpResponse):
        return False
    return not (
        isinstance(response, FileResponse) and _plain_file(response.file_to_stream)
    )


def _hand_over(response: HttpResponseBase, scope: _context.Scope) -> bool:
    """Hand the root to a streamed body; return whether it was handed over.

    A ``FileResponse`` over a plain file is left as it is, so the server can
    still send the file itself (``wsgi.file_wrapper``); reading it runs no
    Python code, and its root closes when the middleware returns. Any other
    streamed body is wrapped. The wrapper takes over the closers Django has
    registered so far, those of the body, and runs them inside the root
    when the body ends or the response is closed, then closes the root.
    """
    if not _kept(response):
        return False
    assert isinstance(response, StreamingHttpResponse)  # noqa: S101  # _kept
    # Django's own list of what HttpResponseBase.close() runs, the same in
    # every supported version; the handlers append to it too.
    registered: list[Callable[[], object]] = response._resource_closers  # type: ignore[attr-defined]
    closers = list(registered)
    registered.clear()
    content = response.streaming_content
    if response.is_async:
        response.streaming_content = _AsyncBody(content, scope, closers)  # type: ignore[arg-type]
    else:
        response.streaming_content = _Body(content, scope, closers)  # type: ignore[arg-type]
    return True


class _Closing:
    """What both wrappers share: the root, the body's closers, and closing
    them once, inside the root.

    A finalizer closes the root when the wrapper is collected, for the path
    where the server never closes the response (an ASGI client that
    disconnects before a streamed 4xx is sent; ADR 0017 Consequences). It
    runs wherever a collection happens, so it takes no lock.
    """

    __slots__ = ("__weakref__", "_closers", "_scope")

    def __init__(
        self, scope: _context.Scope, closers: list[Callable[[], object]]
    ) -> None:
        self._scope = scope
        self._closers = closers
        # The finalizer's registry keeps it until the wrapper is collected.
        weakref.finalize(self, _context.close, scope)

    def close(self) -> None:
        """Run the body's closers inside the root, then close the root;
        Django calls it when the response is closed.

        As Django's own close does, one closer's ``Exception`` stops none of
        the others. A signal to stop (``KeyboardInterrupt``, ``SystemExit``)
        from a closer stops the rest, and goes on, never replaced, with a
        note that says so; the root closes whatever happens. Nothing here
        takes a lock, since a collection can call it.
        """
        closers, self._closers = self._closers, []
        token = _context._CURRENT.set(self._scope)
        try:
            for index, closer in enumerate(closers):
                try:
                    closer()
                except Exception:  # noqa: S112  # as HttpResponseBase.close
                    continue
                except BaseException as signal:
                    left = len(closers) - index - 1
                    signal.add_note(
                        "neolith: the request root closed; "
                        f"{left} more of the body's closers did not run"
                    )
                    raise
        finally:
            _context._CURRENT.reset(token)
            _context.close(self._scope)


@final
class _Body(_Closing):
    """A sync streamed body that runs each step inside its root."""

    __slots__ = ("_inner",)

    def __init__(
        self,
        inner: Iterator[bytes],
        scope: _context.Scope,
        closers: list[Callable[[], object]],
    ) -> None:
        super().__init__(scope, closers)
        self._inner = inner

    def __iter__(self) -> "_Body":
        return self

    def __next__(self) -> bytes:
        token = _context._CURRENT.set(self._scope)
        try:
            return next(self._inner)
        except BaseException:
            # Exhausted, failed or interrupted: the body is done.
            self.close()
            raise
        finally:
            _context._CURRENT.reset(token)


@final
class _AsyncBody(_Closing):
    """An async streamed body that runs each step inside its root."""

    __slots__ = ("_inner",)

    def __init__(
        self,
        inner: AsyncIterator[bytes],
        scope: _context.Scope,
        closers: list[Callable[[], object]],
    ) -> None:
        super().__init__(scope, closers)
        self._inner = inner

    def __aiter__(self) -> "_AsyncBody":
        return self

    async def __anext__(self) -> bytes:
        token = _context._CURRENT.set(self._scope)
        try:
            return await anext(self._inner)
        except BaseException:
            self.close()
            raise
        finally:
            _context._CURRENT.reset(token)


@final
class PrincipalMiddleware:
    """Bound the resolution of each request's principal, and vary the
    response on the backends' request inputs (ADR 0023 Decision 6).

    Listed once in every host, with or without backends; with none it does
    nothing per request. Django runs it in the mode of the handler inside
    it, sync or async. The root middleware gives a root its request and the
    host's backends when it binds the root to an app the host holds, so a
    call from such a root resolves the principal
    (``neolith._principal.Resolution``) wherever it is made, before this
    middleware's place in the list too.

    Its response phase, for a root that has its backends:

    1. a streamed body that the root keeps runs inside the root after the
       response is returned, so the principal is resolved now, unless a
       call has resolved it, with ``resolve`` in sync mode and ``aresolve``
       in async mode; a failure is only recorded;
    2. the root is marked, and a later first call fails with ``internal``;
    3. when a search has started, ``Vary`` gains the union of the backends'
       request inputs, or becomes ``*``.

    Args:
        get_response: The rest of the chain, as Django passes it.

    Raises:
        ImproperlyConfigured: If ``MIDDLEWARE`` does not list the root
            middleware first and this class once, if no set-up of the host
            has passed, or if, with a backend configured, Django's
            ``UpdateCacheMiddleware`` is listed after this class.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: _Respond | _ARespond) -> None:
        """Check the middleware's place and the host, and keep the chain."""
        middleware = getattr(settings, "MIDDLEWARE", None)
        problems = _checks.middleware_problems(middleware)
        if problems:
            raise ImproperlyConfigured("\n".join(problems))
        host = _host._current()
        if host is None:
            raise ImproperlyConfigured(_NO_HOST)
        if host.principals:
            problems = _principal.cache_problems(middleware)
            if problems:
                raise ImproperlyConfigured("\n".join(problems))
        self._get_response = get_response
        self._async = inspect.iscoroutinefunction(get_response)
        if self._async:
            inspect.markcoroutinefunction(self)

    def __call__(
        self, request: HttpRequest
    ) -> HttpResponseBase | Awaitable[HttpResponseBase]:
        """Serve one request, then run the response phase for its root."""
        if self._async:
            return self._acall(request)
        scope = _REQUEST.get()
        respond: _Respond = self._get_response  # type: ignore[assignment]
        response = respond(request)
        resolution = _resolution(scope)
        if resolution is not None:
            finish(resolution, response)
        return response

    async def _acall(self, request: HttpRequest) -> HttpResponseBase:
        scope = _REQUEST.get()
        respond: _ARespond = self._get_response  # type: ignore[assignment]
        response = await respond(request)
        resolution = _resolution(scope)
        if resolution is not None:
            await afinish(resolution, response)
        return response


def _resolution(scope: _context.Scope | None) -> _principal.Resolution | None:
    """The root's resolution for the response phase, or ``None``. A root
    that was never bound, or is bound to no app the host holds, has none:
    a call from it has no calling app or is a usage error at any time
    (spec 02, TRN-56 and TRN-58), so the response phase has nothing to
    search, mark or vary for it.
    """
    resolution = None if scope is None else scope.resolution
    return resolution if isinstance(resolution, _principal.Resolution) else None


def finish(resolution: _principal.Resolution, response: HttpResponseBase) -> None:
    """The principal middleware's response phase, in sync mode, for a root
    and its response (ADR 0023 Decision 6): search now for a streamed body
    the root keeps, when a search could start (``Resolution.open``); mark
    the root; and patch ``Vary`` with the resolution's union when a search
    has started.
    """
    if _kept(response) and resolution.open:
        with contextlib.suppress(ValueError):  # only recorded
            resolution.form()
    _vary(response, resolution)


async def afinish(
    resolution: _principal.Resolution, response: HttpResponseBase
) -> None:
    """``finish`` in async mode: the search, if any, awaits ``aresolve``."""
    if _kept(response) and resolution.open:
        with contextlib.suppress(ValueError):  # only recorded
            await resolution.aform()
    _vary(response, resolution)


def _vary(response: HttpResponseBase, resolution: _principal.Resolution) -> None:
    """Mark the root, and patch ``Vary`` when a search has started."""
    if resolution.finish() and resolution.vary:
        patch_vary_headers(response, resolution.vary)
