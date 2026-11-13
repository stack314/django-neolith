"""The URLconf of the project around the test host (``tests.request_site``)."""

import functools

from django.urls import path

from tests import request_site as site
from tests.host_apps.stock import views as stock_views
from tests.host_apps.web import views as web
from tests.plain_app import views as plain

urlpatterns = [
    path("page/", web.page),
    path("apage/", web.apage),
    path("cbv/", web.PageView.as_view()),
    path("wrapped/", site.wraps_view(web.page)),
    path("hidden/", site.hides_view(web.page)),
    path("partial/", functools.partial(web.page)),
    path("instance/", web.Endpoint()),
    path("level/", stock_views.level),
    path("plain/", plain.page),
    path("project/", site.project_page),
    path("missing/", web.missing),
    path("stream/<str:body>/", web.stream),
    path("astream/<str:body>/", web.astream),
    path("file/", web.file),
    path("afile/", web.afile),
    path("hang/", web.hang),
    path("ahang/", web.ahang),
    path("fail/", web.fail),
]

handler404 = "tests.host_apps.web.views.not_found"
