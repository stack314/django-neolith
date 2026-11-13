"""A view of an app with no declaration: it requires nothing."""

from django.http import HttpRequest, HttpResponse

from tests import request_site as site


def page(request: HttpRequest) -> HttpResponse:
    return HttpResponse(site.ask())
