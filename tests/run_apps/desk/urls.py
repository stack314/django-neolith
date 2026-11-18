"""The URLconf a test deployer names for ``desk``.

A ``request_started`` receiver raises for ``/boom/``, so Django ends that
request before any response exists (ADR 0022 fact 13).
"""

from django.core.signals import request_started
from django.urls import path

from tests.run_apps.desk import views


def _boom(sender: object, environ: dict[str, object], **kwargs: object) -> None:
    if environ.get("PATH_INFO") == "/boom/":
        raise RuntimeError("a request_started receiver raised")


request_started.connect(_boom, dispatch_uid="tests.run_apps.desk.urls.boom")

urlpatterns = [
    path("ping/", views.ping),
    path("count/", views.count),
    path("hold/<str:name>/", views.hold),
    path("stream/<str:name>/", views.stream),
    path("signal/", views.signal_here),
    path("signal/<str:name>/", views.signal_after),
]
