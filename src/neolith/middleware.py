"""The root middleware: a request root for each request (ADR 0017 Decision 4).

List ``"neolith.middleware.RootMiddleware"`` first in ``MIDDLEWARE``, once
(ADR 0022 Decision 6). For each request it opens a root, with the host's
request deadline (``NEOLITH["REQUEST_DEADLINE"]``, 30 seconds when absent,
none when ``None``) counted from when the request reaches it. The root
belongs to no app until the view is resolved; then it belongs to the app
whose code serves the view. It stays open until the response is done, a
streamed body included, and then closes, so a call kept past it is refused.
How each case is met is in the Django host notes (``docs/django-host.md``).

State the middleware owns (CONTRIBUTING K6): a private context variable
that holds, while a request runs, the request and its root, through which
``process_view`` binds the root. Closing never goes through a context
variable: the middleware, or the wrapper it puts around a streamed body,
closes the root through its own reference.
"""

import contextlib
import contextvars
import inspect
import sys
import types
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, Final, final

from django.apps import apps as global_apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models.options import Options
from django.http import FileResponse, HttpRequest, StreamingHttpResponse
from django.http.response import HttpResponseBase

from neolith import _checks, _context, _host

__all__ = ["RootMiddleware"]

_Respond = Callable[[HttpRequest], HttpResponseBase]
_ARespond = Callable[[HttpRequest], Awaitable[HttpResponseBase]]

# While a request runs: the request and its root, so process_view binds the
# root of its own request. Set and reset by the middleware's call alone.
_REQUEST: Final[contextvars.ContextVar[tuple[HttpRequest, _context.Scope] | None]] = (
    contextvars.ContextVar("neolith_request_root", default=None)
)
_NO_HOST: Final = (
    f"{_checks.ROOT_MIDDLEWARE}: the host is not set up; install neolith as the "
    "last entry of INSTALLED_APPS, after the apps it hosts"
)


@final
class RootMiddleware:
    """Open, bind and close the root of each request.

    Django runs it in the mode of the handler inside it, sync or async, and
    it behaves the same in both. Its constructor refuses a ``MIDDLEWARE``
    in which it is not the first entry, or is listed more than once, and a
    process where the host is not set up, so a handler is never built with
    it in the wrong place.

    Args:
        get_response: The rest of the chain, as Django passes it.

    Raises:
        ImproperlyConfigured: If ``MIDDLEWARE`` is not a list of str whose
            first entry is this class, listed once, or if no set-up of the
            host has passed.
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
        """Serve one request inside its root."""
        if self._async:
            return self._acall(request)
        scope, tokens = _open(request)
        handed = False
        try:
            respond: _Respond = self._get_response  # type: ignore[assignment]
            response = respond(request)
            handed = _hand_over(response, scope)
            return response
        finally:
            _leave(scope, tokens, handed)

    async def _acall(self, request: HttpRequest) -> HttpResponseBase:
        scope, tokens = _open(request)
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
        """Bind the request's root to the app whose code serves the view.

        Django calls it once the URL is resolved, before any other
        middleware's ``process_view``, since this middleware comes first.

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


def _open(
    request: HttpRequest,
) -> tuple[
    _context.Scope,
    tuple[
        contextvars.Token[_context.Scope | None],
        contextvars.Token[tuple[HttpRequest, _context.Scope] | None],
    ],
]:
    """Open the request's root, of no app, with the host's request deadline
    counted from now, and make it current.
    """
    host = _host._current()
    seconds = (
        _checks.DEFAULT_REQUEST_DEADLINE if host is None else host.request_deadline
    )
    scope, token = _context.begin(**_context.root_fields(timeout=seconds))
    return scope, (token, _REQUEST.set((request, scope)))


def _leave(
    scope: _context.Scope,
    tokens: tuple[
        contextvars.Token[_context.Scope | None],
        contextvars.Token[tuple[HttpRequest, _context.Scope] | None],
    ],
    handed: bool,
) -> None:
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
    found = _REQUEST.get()
    if found is None or found[0] is not request:
        return
    scope = found[1]
    label = _label(view)
    requires: frozenset[str] | None = None
    host = _host._current()
    if label is not None and host is not None and label in host.apps:
        requires = frozenset(host.apps[label])
    scope.bind(label, requires)


def _label(view: object) -> str | None:
    """The label of the app whose code serves ``view``, or ``None``.

    Read with ``vars()`` and ``type()``, so no attribute hook of the view's
    author runs. A view Django's admin wraps carries its ``ModelAdmin``:
    its class's app, or the model's app for the stock ``ModelAdmin``, which
    runs no app's code. A class-based view takes its class's module; a
    function its own ``__module__``, as ``functools.wraps`` leaves it; a
    bound method its function's; anything else its type's.
    """
    kind = type(view)
    if kind is types.FunctionType or kind is types.MethodType:
        function = view.__func__ if kind is types.MethodType else view  # type: ignore[attr-defined]
        if type(function) is not types.FunctionType:
            return _containing(vars(type(function)).get("__module__"))
        attributes = vars(function)
        admin = attributes.get("model_admin")
        if admin is not None:
            return _admin_label(admin)
        view_class = attributes.get("view_class")
        if issubclass(type(view_class), type):
            return _containing(vars(view_class).get("__module__"))
        return _containing(function.__module__)
    return _containing(vars(kind).get("__module__"))


def _admin_label(admin: object) -> str | None:
    klass = type(admin)
    options = sys.modules.get("django.contrib.admin.options")
    if options is not None and klass is vars(options).get("ModelAdmin"):
        model = vars(admin).get("model")
        meta = vars(model).get("_meta") if issubclass(type(model), type) else None
        label = meta.app_label if type(meta) is Options else None
        installed = type(label) is str and label in global_apps.app_configs
        return label if installed else None
    return _containing(vars(klass).get("__module__"))


def _containing(module: object) -> str | None:
    if type(module) is not str:
        return None
    config = global_apps.get_containing_app_config(module)
    return None if config is None else config.label


def _hand_over(response: HttpResponseBase, scope: _context.Scope) -> bool:
    """Hand the root to a streamed body; return whether it was handed over.

    A ``FileResponse`` that streams a file is left as it is, so the server
    can still send the file itself (``wsgi.file_wrapper``); reading a file
    runs no app code, and its root closes when the middleware returns. Any
    other streamed body is wrapped, and its wrapper closes the root.
    """
    if not isinstance(response, StreamingHttpResponse):
        return False
    if isinstance(response, FileResponse) and response.file_to_stream is not None:
        return False
    content = response.streaming_content
    if response.is_async:
        response.streaming_content = _AsyncBody(content, scope)  # type: ignore[arg-type]
    else:
        response.streaming_content = _Body(content, scope)  # type: ignore[arg-type]
    return True


class _Closing:
    """What both wrappers share: the root, and closing it once.

    A finalizer closes the root when the wrapper is collected, for the path
    where the server never closes the response (an ASGI client that
    disconnects before a streamed 4xx is sent; ADR 0017 Consequences).
    """

    __slots__ = ("__weakref__", "_close", "_scope")

    def __init__(self, scope: _context.Scope) -> None:
        self._scope = scope
        self._close = weakref.finalize(self, _context.close, scope)

    def close(self) -> None:
        """Close the root; Django calls it when the response is closed."""
        self._close()


@final
class _Body(_Closing):
    """A sync streamed body that runs each step inside its root."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Iterator[bytes], scope: _context.Scope) -> None:
        super().__init__(scope)
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

    def __init__(self, inner: AsyncIterator[bytes], scope: _context.Scope) -> None:
        super().__init__(scope)
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
