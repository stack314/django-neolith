"""A view of the shop app."""

from django.http import HttpRequest, HttpResponse

from tests import request_site as site


def page(request: HttpRequest) -> HttpResponse:
    return HttpResponse(site.ask())
