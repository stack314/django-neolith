"""A project around the test host, for the request-root tests (ADR 0017
Decision 4): its middleware, a view and helpers; ``tests.request_urls`` is
its URLconf. This module is in no installed app, so code that runs from it
has no app of its own.
"""

import contextvars
import copy
import functools
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.deprecation import MiddlewareMixin

from neolith import ContractError, UsageError, connect
from tests.declaration_contracts import Sku
from tests.host_apps.couriers import services as courier_services

# What each call made through ask() came to, in order.
SEEN: list[str] = []
# A copy of the context in force where hold() ran, to make a call later.
HELD: list[contextvars.Context] = []

couriers = connect("couriers.v3")


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
        outcome = f"as {courier_services.CONTEXTS[-1].caller}"
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
        principal = courier_services.CONTEXTS[-1].principal
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
