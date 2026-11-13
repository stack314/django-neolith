"""The stock app's view, for the request-root tests."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

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


class Guarded(View):
    """A mixin of this app whose dispatch is decorated: a class-based view
    of another app that inherits it is still that app's code."""

    @method_decorator(never_cache)
    def dispatch(
        self, request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponseBase:
        return super().dispatch(request, *args, **kwargs)
