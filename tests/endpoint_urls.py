"""A URLconf whose catch-all pattern matches the ledger's contract paths, for
Boot's ``neolith.W003`` (``tests.test_endpoint_parts``).
"""

from django.http import HttpRequest, HttpResponse
from django.urls import re_path


def page(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


urlpatterns = [re_path(r"^ledger/", page)]
