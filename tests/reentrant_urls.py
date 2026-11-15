"""A URLconf that builds a handler while it is imported, which Boot refuses
instead of waiting on itself."""

from django.core.handlers.wsgi import WSGIHandler

WSGIHandler()

urlpatterns: list[object] = []
