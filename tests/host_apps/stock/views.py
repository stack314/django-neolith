"""The stock app's view, for the request-root tests."""

from django.http import HttpRequest, HttpResponse

from tests import request_site as site


def level(request: HttpRequest) -> HttpResponse:
    return HttpResponse(site.ask())
