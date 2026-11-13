"""The stock app's view, for the request-root tests."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseBase

from tests import request_site as site


def level(request: HttpRequest) -> HttpResponse:
    return HttpResponse(site.ask())


def hides(view: Callable[..., HttpResponseBase]) -> Callable[..., HttpResponseBase]:
    """A decorator of this app that keeps no link to the view it wraps."""

    def wrapper(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        return view(request, *args, **kwargs)

    return wrapper
