"""A project around the test host, for the request-root tests (ADR 0017
Decision 4): its middleware, a view and helpers; ``tests.request_urls`` is
its URLconf. This module is in no installed app, so code that runs from it
has no app of its own.
"""

import contextvars
import copy
import functools
from collections.abc import Callable

from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.utils.deprecation import MiddlewareMixin

from neolith import CallContext, ContractError, UsageError, _context, connect
from tests.declaration_contracts import Sku
from tests.host_apps.couriers import services as courier_services

# What each call made through ask() came to, in order.
SEEN: list[str] = []
# A copy of the context in force where hold() ran, to make a call later.
HELD: list[contextvars.Context] = []

couriers = connect("couriers.v3")


def _seen() -> CallContext:
    """The context the couriers action last saw for a call made from the
    scope in force here, matched by its trace, since requests served at
    once append to the same list."""
    scope = _context._CURRENT.get()
    trace = None if scope is None else scope.held.trace_id
    return [c for c in courier_services.CONTEXTS if c.trace_id == trace][-1]


def ask() -> str:
    """Call the couriers contract once; record and return what came of it:
    ``as <app>`` with the calling app the action saw, ``refused <kind>``, or
    ``usage error`` for a call refused before it was sent.
    """
    try:
        couriers.quote(Sku(sku="ab"))
    except ContractError as error:
        outcome = f"refused {error.kind}"
    except UsageError:
        outcome = "usage error"
    else:
        outcome = f"as {_seen().caller}"
    SEEN.append(outcome)
    return outcome


def hold() -> None:
    """Keep the context in force here, as an executor started now would."""
    HELD.append(contextvars.copy_context())


def later() -> str:
    """Make a call in the last context held, as that executor would later."""
    return HELD[-1].run(ask)


class Calling(MiddlewareMixin):
    """Calls in its request phase, before any view is resolved, and in its
    ``process_view``, after the root middleware's.
    """

    def process_request(self, request: HttpRequest) -> None:
        ask()

    def process_view(
        self,
        request: HttpRequest,
        view: object,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        ask()


def sync_only(
    get_response: Callable[[HttpRequest], HttpResponseBase],
) -> Callable[[HttpRequest], HttpResponseBase]:
    """A middleware that can only run sync, so under ASGI Django runs every
    middleware above it in sync mode.
    """

    def middleware(request: HttpRequest) -> HttpResponseBase:
        return get_response(request)

    return middleware


_View = Callable[..., HttpResponseBase]


def wraps_view(view: _View) -> _View:
    """A decorator that copies the view's module, as ``functools.wraps``
    does."""

    @functools.wraps(view)
    def wrapper(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        return view(request, *args, **kwargs)

    return wrapper


def hides_view(view: _View) -> _View:
    """A decorator that does not copy the view's module."""

    def wrapper(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        return view(request, *args, **kwargs)

    return wrapper


def project_page(request: HttpRequest) -> HttpResponse:
    """A view in no app."""
    return HttpResponse(ask())


def unnamed_stream(request: HttpRequest) -> StreamingHttpResponse:
    """A streamed body that makes no call, with an authenticated user the
    Django-user backend fails on: a search for the root would log a
    failure and add Vary."""

    class Unnamed:
        is_authenticated = True
        pk = 7

    request.user = Unnamed()  # type: ignore[assignment]
    return StreamingHttpResponse(iter([b"ok"]))


def project_stream(request: HttpRequest) -> StreamingHttpResponse:
    """``unnamed_stream`` from a view in no app."""
    return unnamed_stream(request)


def copies_request(
    get_response: Callable[[HttpRequest], HttpResponseBase],
) -> Callable[[HttpRequest], HttpResponseBase]:
    """A middleware that passes a copy of the request on."""

    def middleware(request: HttpRequest) -> HttpResponseBase:
        return get_response(copy.copy(request))

    return middleware


# The root middleware under a second path, and a subclass of it.
from neolith.middleware import RootMiddleware  # noqa: E402

Alias = RootMiddleware


class SubRoot(RootMiddleware):  # type: ignore[misc]  # final for typing only
    pass


def serves_class(klass: type) -> _View:
    """A view function for a class, linked to it as DRF's ``as_view`` does
    (``functools.update_wrapper(view, cls, updated=())``)."""

    def view(request: HttpRequest) -> HttpResponseBase:
        response: HttpResponseBase = klass().get(request)
        return response

    functools.update_wrapper(view, klass, updated=())
    return view


def serves_class_unnamed(klass: type) -> _View:
    """As ``serves_class``, but the function keeps this module's name, so
    only its link leads to the class."""

    def view(request: HttpRequest) -> HttpResponseBase:
        response: HttpResponseBase = klass().get(request)
        return response

    functools.update_wrapper(view, klass, assigned=("__name__",), updated=())
    return view


class OddPartial(functools.partial):  # type: ignore[type-arg]
    """A partial subclass whose function is read through a property."""

    reads = 0

    @property
    def func(self) -> object:  # type: ignore[override]
        type(self).reads += 1
        return super().func


def copies_attributes(view: _View) -> _View:
    """A decorator that copies the view's attributes, and so a class-based
    view's ``view_class``, but keeps this module as its own."""

    @functools.wraps(view, assigned=("__name__", "__doc__"))
    def wrapper(request: HttpRequest) -> HttpResponseBase:
        return view(request)

    return wrapper


# What each call made through whom() came to, in order.
WHOM: list[str] = []


def whom() -> str:
    """Call the stock contract once from where it runs; return, and record,
    ``user <subject>`` for the principal couriers saw, ``nobody``,
    ``refused <kind>`` or ``usage error``. The web app may assert a
    principal on stock, which asks couriers on the same principal; the
    connector is asked for here, since a process that imports this module
    may host no app that requires stock."""
    try:
        connect("stock.v1").level(Sku(sku="ab"))
    except ContractError as error:
        outcome = f"refused {error.kind}"
    except UsageError:
        outcome = "usage error"
    else:
        principal = _seen().principal
        outcome = "nobody" if principal is None else f"user {principal.subject}"
    WHOM.append(outcome)
    return outcome


class LateCaller(MiddlewareMixin):
    """Calls in its response phase, which runs after the principal
    middleware's when it is listed before it."""

    def process_response(
        self, request: HttpRequest, response: HttpResponseBase
    ) -> HttpResponseBase:
        whom()
        return response


class CallsInView(MiddlewareMixin):
    """Calls from its ``process_view``, wherever it is listed after the root
    middleware, and lets the request go on."""

    def process_view(
        self,
        request: HttpRequest,
        view: object,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        whom()


class AnswersInView(MiddlewareMixin):
    """Calls from its ``process_view`` and answers there, so no later
    ``process_view`` and no view runs."""

    def process_view(
        self,
        request: HttpRequest,
        view: object,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> HttpResponse:
        return HttpResponse(whom())


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """A ``CSRF_FAILURE_VIEW`` that calls, which Django runs from
    ``CsrfViewMiddleware.process_view``."""
    return HttpResponse(whom(), status=403)


class Streams(MiddlewareMixin):
    """Answers every request from its request phase with a streamed body,
    before any view is resolved, so the root is never bound."""

    def process_request(self, request: HttpRequest) -> StreamingHttpResponse:
        return StreamingHttpResponse(iter([b"ok"]))
