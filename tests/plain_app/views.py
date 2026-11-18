"""A view of an app with no declaration: it requires nothing."""

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse

from tests import request_site as site


def page(request: HttpRequest) -> HttpResponse:
    return HttpResponse(site.ask())


def stream(request: HttpRequest) -> StreamingHttpResponse:
    """A streamed body with a user no backend can name on the request, and
    no call (``tests.request_site.unnamed_stream``)."""
    return site.unnamed_stream(request)
