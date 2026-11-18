"""The URLconf of the project around the test host (``tests.request_site``)."""

import functools
import types

from django.urls import path

from tests import request_site as site
from tests.host_apps.stock import views as stock_views
from tests.host_apps.web import principals
from tests.host_apps.web import views as web
from tests.plain_app import views as plain


def _chain(view: site._View, links: int) -> site._View:
    """``view`` under ``links`` decorators that each copy what they wrap."""
    for _ in range(links):
        view = site.wraps_view(view)
    return view


urlpatterns = [
    path("page/", web.page),
    path("apage/", web.apage),
    path("cbv/", web.PageView.as_view()),
    path("wrapped/", site.wraps_view(web.page)),
    path("hidden/", site.hides_view(web.page)),
    path("partial/", functools.partial(web.page)),
    path("method/", web.Pages().page),
    path("exempt/", web.ExemptView.as_view()),
    path("own-dispatch/", web.OwnDispatchView.as_view()),
    path("guarded/", web.GuardedView.as_view()),
    path("viewset/", site.serves_class(web.Viewset)),
    path("viewset-unnamed/", site.serves_class_unnamed(web.Viewset)),
    path("cbv-partly-wrapped/", site.copies_attributes(web.PageView.as_view())),
    path("wraps-framework/", web.wraps_framework),
    path("chain/", _chain(web.page, 10)),
    path("odd-partial/", site.OddPartial(web.page)),
    # functools.wraps finds no __module__ on a partial, so only __wrapped__
    # leads to the view.
    path("wrapped-partial/", site.wraps_view(functools.partial(web.page))),
    path(
        "method-partial/",
        types.MethodType(functools.partial(web.Pages.page), web.Pages()),
    ),
    path("stock-hidden/", stock_views.hides(web.page)),
    path("appfile/", web.appfile),
    path("aappfile/", web.aappfile),
    path("realfile/", web.realfile),
    path("cleaned/", web.cleaned),
    path("acleaned/", web.acleaned),
    path("afail/", web.afail),
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
    path("principal/quiet/", principals.quiet),
    path("principal/aquiet/", principals.aquiet),
    path("principal/who/", principals.who),
    path("principal/awho/", principals.awho),
    path("principal/stream/", principals.stream),
    path("principal/astream/", principals.astream),
    path("principal/bearer/", principals.bearer),
    path("principal/stranger/", principals.stranger),
    path("principal/cached/", principals.cached),
    path("principal/pair/", principals.pair),
    path("principal/apair/", principals.apair),
    path("principal/alogout-stream/", principals.alogout_stream),
]

handler404 = "tests.host_apps.web.views.not_found"
