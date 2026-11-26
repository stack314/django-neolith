"""The check's rule on views: the project's own code in no app that serves
requests, ``neolith.W102`` (spec 05, APP-69; ADR 0030 Decision 9).

One project under a temporary directory (``tests/checking.py``) holds a
hosted app ``shop``, an undeclared first-party app ``legacy``, project code
in no app, and a library on a directory of its own, which is not
first-party; its URLconf reaches every view of the matrix below and a few
URLconf shapes. The rule runs once over that host in a child process and
reports each URL pattern, its view's app by ``label_of`` and the code the
rule walked; the tests compare that with an answer worked out here from
how each view is composed, not from the rule.

The matrix: a view's owner (the hosted app, the undeclared first-party
app, the project in no app, a library, Django's generic and plain views,
``static.serve``, and a contrib app) in each form it can take (a function,
``as_view()``, a bound method, a callable instance), under every sequence
of up to two wrappers (none, ``functools.wraps`` and ``wraps(assigned=())``
in a library, ``partial``, Django's ``csrf_exempt``, contrib's
``login_required``, and a wrapper of the project's, of the hosted app's and
of the undeclared app's). Only a view that serves as no app and runs the
project's own code is reported.
"""

import itertools
import json
import os
import re
import sys
from typing import Any, Final

import pytest

from tests.checking import Project

pytestmark = pytest.mark.spec("APP-69")

_HINT: Final = (
    "move the view into the app whose work it does, so that its requests run in "
    "that app's root"
)

# The same views in each owner's module: a function, a class-based view, a
# class with a handler method and a callable class. The wrapper is used for
# the owners that wrap others too.
_VIEWS: Final = """\
import functools

from django.http import HttpResponse
from django.views import View


def page(request, *args, **kwargs):
    return HttpResponse("")


class Page(View):
    def get(self, request, *args, **kwargs):
        return HttpResponse("")


class Handler:
    def handle(self, request, *args, **kwargs):
        return HttpResponse("")


class Callable:
    def __call__(self, request, *args, **kwargs):
        return HttpResponse("")


def wrap(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        return view(request, *args, **kwargs)

    return wrapper
"""

_LIBRARY_WRAP: Final = """\
import functools


def wrapped(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        return view(request, *args, **kwargs)

    return wrapper


def bare(view):
    @functools.wraps(view, assigned=())
    def wrapper(request, *args, **kwargs):
        return view(request, *args, **kwargs)

    return wrapper
"""

# Every base view under every sequence of up to two wrappers, the outermost
# first in the route.
_COMPOSED: Final = """\
import functools
import itertools

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, logout_then_login
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.defaults import bad_request
from django.views.generic import RedirectView, TemplateView
from django.views.static import serve

import site_views
import vendorlib.views
import vendorlib.wrap
from legacy import views as legacy_views
from shop import views as shop_views

BASES = {}
for owner, module in {
    "hosted": shop_views,
    "legacy": legacy_views,
    "project": site_views,
    "library": vendorlib.views,
}.items():
    BASES[f"{owner}-function"] = module.page
    BASES[f"{owner}-as_view"] = module.Page.as_view()
    BASES[f"{owner}-method"] = module.Handler().handle
    BASES[f"{owner}-instance"] = module.Callable()
BASES["django-function"] = bad_request
BASES["django-as_view"] = TemplateView.as_view(template_name="page.html")
BASES["django-method"] = RedirectView(url="/").get
BASES["static-function"] = serve
BASES["contrib-function"] = logout_then_login
BASES["contrib-as_view"] = LoginView.as_view()

LAYERS = {
    "lib_wraps": vendorlib.wrap.wrapped,
    "lib_bare": vendorlib.wrap.bare,
    "partial": functools.partial,
    "django": csrf_exempt,
    "contrib": login_required,
    "project": site_views.wrap,
    "hosted": shop_views.wrap,
    "legacy": legacy_views.wrap,
}

urlpatterns = []
for base, view in BASES.items():
    for depth in range(3):
        for layers in itertools.product(LAYERS, repeat=depth):
            served = view
            for layer in reversed(layers):
                served = LAYERS[layer](served)
            urlpatterns.append(path("/".join([*layers, base]) + "/", served))
"""

_URLS: Final = """\
from django.contrib import admin
from django.urls import include, path

import site_admin  # noqa: F401
import site_views


class Odd:
    \"\"\"An entry that is neither Django's pattern nor its resolver.\"\"\"

    def resolve(self, path):
        return None


urlpatterns = [
    path("composed/", include("composed")),
    path("nested/", include("nested")),
    path("listed/", include([path("page/", site_views.page)])),
    path("loop/", include("loop")),
    path("deep/", include("deep")),
    path("admin/", admin.site.urls),
    Odd(),
]
"""

_NESTED: Final = """\
from django.urls import include, path, re_path

import site_views

urlpatterns = [
    re_path(r"^old/(?P<n>[0-9]+)/$", site_views.page),
    path("deeper/", include("deeper")),
]
"""

_DEEPER: Final = """\
from django.urls import path

import site_views

urlpatterns = [path("leaf/", site_views.Page.as_view())]
"""

# A URLconf that includes itself, which Django never finishes resolving.
_LOOP: Final = """\
from django.urls import include, path

import site_views

urlpatterns = [path("again/", include("loop")), path("leaf/", site_views.page)]
"""

# A chain of library wrappers inside and past the links label_of follows.
_DEEP: Final = """\
from django.urls import path

import site_views
import vendorlib.wrap


def layered(count):
    view = site_views.page
    for _ in range(count):
        view = vendorlib.wrap.wrapped(view)
    return view


urlpatterns = [
    path("inside/", layered(60)),
    path("past/", layered(70)),
]
"""

# An admin class of the project's that overrides a view, so the link
# Django's admin wrapper keeps leads to the project's code.
_ADMIN: Final = """\
from django.contrib import admin

from legacy.models import Thing


class ThingAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, extra_context)


admin.site.register(Thing, ThingAdmin)
"""

_LEGACY_APP: Final = """\
from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
    default_auto_field = "django.db.models.AutoField"
"""

_LEGACY_MODELS: Final = """\
from django.db import models


class Thing(models.Model):
    name = models.CharField(max_length=10)
"""

# Run in the child: build the host, run the rule, and report each URL
# pattern with its view's app and the app of each code the rule walked;
# then the rule under a ROOT_URLCONF that is missing, not text, or does not
# import.
_SCRIPT: Final = """\
import json

import django

django.setup()

from django.apps import apps
from django.conf import settings

from neolith import _check, _check_source, _check_views, _serving
from neolith._declaration import declaration_of

hosted = {
    config.label: config
    for config in apps.get_app_configs()
    if declaration_of(type(config)) is not None
}
source = _check_source.Source(hosted, _check._owner, settings_module="settings")
found = _check_views.findings(source)
again = _check_views.findings(source)
routes = {}
for route, view in _check_views.patterns(settings.ROOT_URLCONF):
    walked = _check_views.codes(view)
    routes.setdefault(route, []).append({
        "label": _serving.label_of(view),
        "walked": None if walked is None else [
            _check._owner(code.module) if type(code.module) is str else "?"
            for code in walked
        ],
    })
skipped = {}
for value in ("broken", "absent", 5, None):
    settings.ROOT_URLCONF = value
    skipped[repr(value)] = [f.message.msg for f in _check_views.findings(source)]
print(json.dumps({
    "lines": [_check._line(f, report_only=False) for f in found],
    "again": [_check._line(f, report_only=False) for f in again],
    "apps": sorted({str(f.app) for f in found}),
    "routes": routes,
    "skipped": skipped,
}))
"""

_OWNERS: Final = ("hosted", "legacy", "project", "library")
_FORMS: Final = ("function", "as_view", "method", "instance")
_BASES: Final = (
    *(f"{owner}-{form}" for owner in _OWNERS for form in _FORMS),
    "django-function",
    "django-as_view",
    "django-method",
    "static-function",
    "contrib-function",
    "contrib-as_view",
)
_LAYERS: Final = (
    "lib_wraps",
    "lib_bare",
    "partial",
    "django",
    "contrib",
    "project",
    "hosted",
    "legacy",
)
# The app each base serves as, by the rule that binds request roots: its
# own app's code, or none for code in no app; contrib's views are the auth
# app's.
_BASE_APP: Final[dict[str, str | None]] = {
    "hosted": "shop",
    "legacy": "legacy",
    "project": None,
    "library": None,
    "django": None,
    "static": None,
    "contrib": "auth",
}
# The project's code each project base runs, by the line that starts it and
# the name a finding gives it.
_PROJECT_CODE: Final = {
    "function": ("def page(", "site_views.page"),
    "as_view": ("class Page(", "site_views.Page"),
    "method": ("    def handle(", "site_views.Handler.handle"),
    "instance": ("class Callable:", "site_views.Callable"),
}
_WRAPPER: Final = ("    @functools.wraps(view)", "site_views.wrap.<locals>.wrapper")


def _served_as(layers: tuple[str, ...], base: str) -> str | None:
    """The app a composed view serves as, from the inside out: a wrapper of
    an app's is the code it wraps when that is a hosted app's, and its own
    app's code otherwise; any other wrapper only wraps.
    """
    label = _BASE_APP[base.partition("-")[0]]
    for layer in reversed(layers):
        if layer == "hosted":
            label = "shop"
        elif layer == "legacy":
            label = "shop" if label == "shop" else "legacy"
    return label


def _expected(layers: tuple[str, ...], base: str) -> tuple[str, str] | None:
    """The code a finding names for a composed view, by its first line and
    its name, or ``None`` when the view is not reported: it serves as no
    app and runs the project's code, placed at the innermost.
    """
    if _served_as(layers, base) is not None:
        return None
    owner, _, form = base.partition("-")
    if owner == "project":
        return _PROJECT_CODE[form]
    if "project" in layers:
        return _WRAPPER
    return None


def _line_of(text: str, start: str) -> int:
    lines = text.splitlines()
    found = [number for number, line in enumerate(lines, 1) if line.startswith(start)]
    assert len(found) == 1, start
    return found[0]


def _finding(route: str, line: int, name: str) -> str:
    return (
        f"site_views.py:{line}: neolith.W102 URL pattern {route!r}: view {name} is "
        "the project's code in no installed app; a request it serves has no "
        f"calling app, and a contract call from it is refused; {_HINT}"
    )


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The rule's run over the fixture project, once for this module."""
    root = tmp_path_factory.mktemp("views")
    project = Project(root / "project")
    project.app("shop")
    project.write("shop/views.py", _VIEWS)
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/models.py", _LEGACY_MODELS)
    project.write("legacy/views.py", _VIEWS)
    project.write("site_views.py", _VIEWS)
    project.write("site_admin.py", _ADMIN)
    project.write("composed.py", _COMPOSED)
    project.write("nested.py", _NESTED)
    project.write("deeper.py", _DEEPER)
    project.write("loop.py", _LOOP)
    project.write("deep.py", _DEEP)
    project.write("urls.py", _URLS)
    project.write("broken.py", "raise RuntimeError('a URLconf that fails')\n")
    library = root / "vendor"
    (library / "vendorlib").mkdir(parents=True)
    (library / "vendorlib" / "__init__.py").write_text("")
    (library / "vendorlib" / "views.py").write_text(_VIEWS)
    (library / "vendorlib" / "wrap.py").write_text(_LIBRARY_WRAP)
    project.settings(
        ["shop"],
        neolith={
            "DATABASES": project.databases("default", "shop"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django.contrib.admin",
                "legacy",
            ],
        },
    )
    path = os.pathsep.join([str(project.root), str(library), *sys.path])
    done = project.run(
        "-c", _SCRIPT, env={"DJANGO_SETTINGS_MODULE": "settings", "PYTHONPATH": path}
    )
    assert done.status == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    result["site_views"] = project.root.joinpath("site_views.py").read_text()
    result["database files"] = sorted(p.name for p in project.root.glob("*.sqlite3"))
    return result


def _composed() -> list[tuple[tuple[str, ...], str]]:
    return [
        (layers, base)
        for base in _BASES
        for depth in range(3)
        for layers in itertools.product(_LAYERS, repeat=depth)
    ]


def _route(layers: tuple[str, ...], base: str) -> str:
    return "composed/" + "/".join([*layers, base]) + "/"


def test_every_composed_view_is_judged_as_its_owner_and_wrappers_say(
    ran: dict[str, Any],
) -> None:
    """The matrix: only a view that serves as no app and runs the project's
    code is reported, at that code; the hosted app's, the undeclared app's,
    a library's, Django's, ``static.serve`` and a contrib app's are not.
    """
    text = ran["site_views"]
    composed = _composed()
    expected = []
    for layers, base in composed:
        code = _expected(layers, base)
        if code is not None:
            expected.append(
                _finding(_route(layers, base), _line_of(text, code[0]), code[1])
            )
    got = [line for line in ran["lines"] if " URL pattern 'composed/" in line]
    assert sorted(got) == sorted(expected)
    # The plan's matrix, cell by cell: owner by form under no wrapper, each
    # library wraps form, partial, and Django's own wrapper.
    for layers in [(), ("lib_wraps",), ("lib_bare",), ("partial",), ("django",)]:
        for base in _BASES:
            reported = any(f"{_route(layers, base)!r}" in line for line in got)
            assert reported == base.startswith("project-"), (layers, base)
    assert ran["apps"] == ["None"]
    assert len(composed) == len([r for r in ran["routes"] if r.startswith("composed/")])


def test_label_of_judges_each_view_as_the_answer_worked_out_here(
    ran: dict[str, Any],
) -> None:
    for layers, base in _composed():
        [seen] = ran["routes"][_route(layers, base)]
        assert seen["label"] == _served_as(layers, base), (layers, base)


def test_the_walk_ends_where_label_of_ends(ran: dict[str, Any]) -> None:
    """For every pattern: the code the rule walks to last is in no app
    exactly when ``label_of`` gives no app, unless the walk stopped at an
    admin view or past the links ``label_of`` follows.
    """
    for route, seen in ran["routes"].items():
        for one in seen:
            walked = one["walked"]
            if walked is None:
                assert one["label"] is None or route.startswith("admin/"), route
                continue
            assert walked, route
            assert (walked[-1] is None) == (one["label"] is None), route


def test_includes_are_followed_with_the_whole_route(ran: dict[str, Any]) -> None:
    text = ran["site_views"]
    page = _line_of(text, "def page(")
    view = _line_of(text, "class Page(")
    others = sorted(
        line for line in ran["lines"] if " URL pattern 'composed/" not in line
    )
    assert others == sorted(
        [
            _finding("nested/^old/(?P<n>[0-9]+)/$", page, "site_views.page"),
            _finding("nested/deeper/leaf/", view, "site_views.Page"),
            _finding("listed/page/", page, "site_views.page"),
            _finding("loop/leaf/", page, "site_views.page"),
            _finding("deep/inside/", page, "site_views.page"),
        ]
    )


def test_an_include_of_a_urlconf_inside_itself_is_read_once(
    ran: dict[str, Any],
) -> None:
    loop = sorted(route for route in ran["routes"] if route.startswith("loop/"))
    assert loop == ["loop/leaf/"]


def test_a_walk_past_the_links_label_of_follows_is_not_reported(
    ran: dict[str, Any],
) -> None:
    [past] = ran["routes"]["deep/past/"]
    assert past == {"label": None, "walked": None}
    [inside] = ran["routes"]["deep/inside/"]
    assert inside["label"] is None
    assert inside["walked"] is not None
    assert inside["walked"][-1] is None


def test_admin_views_are_left_to_w001(ran: dict[str, Any]) -> None:
    """Django's admin views of a project admin class serve as no app and are
    ``neolith.W001``'s; its own views serve as the admin app. None is
    reported here, though the project's class overrides a view.
    """
    admin = {
        route: seen
        for route, seen in ran["routes"].items()
        if route.startswith("admin/")
    }
    changelist = [
        one
        for route, seen in admin.items()
        if re.fullmatch(r"admin/legacy/thing/", route)
        for one in seen
    ]
    assert changelist == [{"label": None, "walked": None}]
    assert not [line for line in ran["lines"] if "'admin/" in line]


def test_an_entry_that_is_not_djangos_is_passed_over(ran: dict[str, Any]) -> None:
    assert all(route.split("/")[0] != "" for route in ran["routes"])


def test_a_second_run_gives_the_same_lines(ran: dict[str, Any]) -> None:
    assert ran["again"] == ran["lines"]


def test_nothing_is_reported_when_root_urlconf_is_missing_or_does_not_import(
    ran: dict[str, Any],
) -> None:
    assert ran["skipped"] == {"'broken'": [], "'absent'": [], "5": [], "None": []}
    assert ran["database files"] == []
