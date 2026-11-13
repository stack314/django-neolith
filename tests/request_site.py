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


# The root middleware under a second path.
from neolith.middleware import RootMiddleware as Alias  # noqa: E402, F401
