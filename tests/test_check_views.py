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
``as_view()`` of a plain view and of one whose ``dispatch`` is decorated, a
bound method, a callable instance, a class), under every sequence
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

# The same views in each owner's module: a function, a class-based view, one
# whose own dispatch is decorated, a class with a handler method and a
# callable class. The wrapper is used for
# the owners that wrap others too.
_VIEWS: Final = """\
import functools

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt


def page(request, *args, **kwargs):
    return HttpResponse("")


class Page(View):
    def get(self, request, *args, **kwargs):
        return HttpResponse("")


class Guarded(View):
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


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
    BASES[f"{owner}-decorated"] = module.Guarded.as_view()
    BASES[f"{owner}-method"] = module.Handler().handle
    BASES[f"{owner}-instance"] = module.Callable()
    BASES[f"{owner}-class"] = module.Callable
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
from django.urls import URLPattern, include, path
from django.urls.resolvers import RoutePattern
from django.views.generic import TemplateView

import site_admin
import site_api
import site_more
import site_views
from shop import views as shop_views


class Odd:
    \"\"\"An entry that is neither Django's pattern nor its resolver.\"\"\"

    def resolve(self, path):
        return None


class Unnamed(RoutePattern):
    \"\"\"A route that cannot be read.\"\"\"

    def __str__(self):
        raise RuntimeError("no route")


class Endless:
    \"\"\"Patterns that include a new URLconf like this one each time.\"\"\"

    def __iter__(self):
        return iter([path("a/", include(Lazy())), path("p/", site_views.page)])


class Lazy:
    def __init__(self):
        self.urlpatterns = Endless()


# A function of site_views's whose code was compiled from no file.
made = {"__name__": "site_views"}
exec(compile("\\n" * 5 + "def made(request):\\n    pass\\n", "<m>", "exec"), made)

# as_view's function, with a view_class that is not a class.
odd_view_class = site_views.Page.as_view()
odd_view_class.view_class = site_views.page

urlpatterns = [
    path("composed/", include("composed")),
    path("nested/", include("nested")),
    path("listed/", include([path("page/", site_views.page)])),
    path("loop/", include("loop")),
    path("self/", include("urls")),
    path("a/", include("twice")),
    path("b/", include("twice")),
    path("deep/", include("deep")),
    path("admin/", admin.site.urls),
    path("unregistered/", site_admin.OTHER.other),
    path("partial/project/", site_more.Part(TemplateView.as_view())),
    path("partial/hosted/", site_more.Part(shop_views.page)),
    path("twice/called/", site_more.Twice()),
    path("twice/bare/", site_more.Bare),
    path("kept/", site_more.Kept),
    path("factory/", site_more.factory()),
    path("outer/", site_more.Outer.Inner),
    path("again/", site_more.Again),
    path("gone/", site_more.Gone),
    path("moved/methods/", site_api.Moved.as_view()),
    path("moved/bare/", site_api.Plain),
    path("made/", made["made"]),
    path("odd-view-class/", odd_view_class),
    path("empty/", include("empty")),
    URLPattern(Unnamed("unnamed/", is_endpoint=True), site_views.page),
    path("lazy/", include(Lazy())),
    Odd(),
]
"""

# Classes that the project defines twice, and a partial of its own.
_MORE: Final = """\
import functools


class Part(functools.partial):
    pass


class Twice:
    pass


if True:

    class Twice:  # noqa: F811
        def __call__(self, request):
            return None


class Bare:
    pass


if True:

    class Bare:  # noqa: F811
        pass


def keep(cls):
    return cls


@keep
class Kept:
    pass


def factory():
    class Made:
        def __call__(self, request):
            return None

    return Made


class Outer:
    class Inner:
        pass


# Defined twice, and its __module__ set again, which drops the
# __firstlineno__ that Python 3.13 keeps: told apart by its methods only, or
# not at all.
class Again:
    pass


if True:

    class Again:  # noqa: F811
        @staticmethod
        def helper():
            return None


Again.__module__ = __name__


class Gone:
    pass


if True:

    class Gone:  # noqa: F811
        pass


Gone.__module__ = __name__
"""

# Classes defined in one module that name another as theirs; the other is
# long enough to hold their lines, and defines a class of one's name.
_IMPL: Final = """\
from django.views import View


class Moved(View):
    def get(self, request):
        return None


class Plain:
    pass
"""

_API: Final = (
    """\
# A class of Moved's name, whose lines hold those of Moved's method.

class Moved:
    \"\"\"Not the class that site_impl defines.

    It is replaced below.
    \"\"\"


from site_impl import Moved, Plain  # noqa: E402, F811

Moved.__module__ = "site_api"
Plain.__module__ = "site_api"
"""
    + "\n".join(f"LINE_{n} = {n}" for n in range(5, 15))
    + "\n"
)

_TWICE: Final = """\
from django.urls import path

import site_views

urlpatterns = [path("p/", site_views.page)]
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

# A URLconf that includes itself, which Django serves at every depth.
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
from neolith._serving import _LINKS
from shop import views as shop_views


def layered(count, view=site_views.page):
    for _ in range(count):
        view = vendorlib.wrap.wrapped(view)
    return view


urlpatterns = [
    path("inside/", layered(60)),
    path("past/", layered(70)),
    path("edge/project/", layered(_LINKS)),
    path("edge/hosted/", layered(_LINKS, shop_views.page)),
    path("beyond/project/", layered(_LINKS + 1)),
    path("beyond/hosted/", layered(_LINKS + 1, shop_views.page)),
]
"""

# An admin class of the project's that overrides a view, so the link
# Django's admin wrapper keeps leads to the project's code, and adds views of
# its own; and one that no site registers, which W001 does not report.
_ADMIN: Final = """\
from django.contrib import admin
from django.urls import path

from legacy.models import Thing


class Mixin:
    def mixed(self, request):
        return None


class ThingAdmin(Mixin, admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, extra_context)

    def get_urls(self):
        def inner(request):
            return None

        return [
            path("extra/", self.admin_site.admin_view(self.extra)),
            path("bound/", self.extra),
            path("mixed/", self.mixed),
            path("inner/", inner),
            *super().get_urls(),
        ]

    def extra(self, request):
        return None


class OtherAdmin(admin.ModelAdmin):
    def other(self, request):
        return None


admin.site.register(Thing, ThingAdmin)
OTHER = OtherAdmin(Thing, admin.site)
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
full = [route for route, _ in _check_views.patterns(settings.ROOT_URLCONF)]
entries = _check_views._ENTRIES
_check_views._ENTRIES = 7
capped = [route for route, _ in _check_views.patterns(settings.ROOT_URLCONF)]
_check_views._ENTRIES = entries
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
for value in ("broken", "absent", 5, None, "no_patterns"):
    settings.ROOT_URLCONF = value
    skipped[repr(value)] = [f.message.msg for f in _check_views.findings(source)]
print(json.dumps({
    "lines": [_check._line(f, report_only=False) for f in found],
    "again": [_check._line(f, report_only=False) for f in again],
    "apps": sorted({str(f.app) for f in found}),
    "routes": routes,
    "skipped": skipped,
    "full": full[:20],
    "capped": capped,
    "depth": _check_views._DEPTH,
}))
"""

_OWNERS: Final = ("hosted", "legacy", "project", "library")
_FORMS: Final = ("function", "as_view", "decorated", "method", "instance", "class")
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
    # as_view copies a decorated dispatch's __wrapped__ onto the view.
    "decorated": ("class Guarded(", "site_views.Guarded"),
    "method": ("    def handle(", "site_views.Handler.handle"),
    "instance": ("class Callable:", "site_views.Callable"),
    "class": ("class Callable:", "site_views.Callable"),
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


def _finding(route: str, line: int, name: str, file: str = "site_views.py") -> str:
    return (
        f"{file}:{line}: neolith.W102 URL pattern {route!r}: view {name} is "
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
    project.write("site_more.py", _MORE)
    project.write("site_impl.py", _IMPL)
    project.write("site_api.py", _API)
    project.write("twice.py", _TWICE)
    project.write("empty.py", "# A URLconf with no patterns.\n")
    project.write("no_patterns.py", "import site_views  # noqa: F401\n")
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
    result["site_more"] = project.root.joinpath("site_more.py").read_text()
    result["site_admin"] = project.root.joinpath("site_admin.py").read_text()
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


def test_every_other_pattern_is_reported_at_its_innermost_project_code(
    ran: dict[str, Any],
) -> None:
    """Includes are followed with the whole route, one URLconf included at
    two prefixes is reported at both, and a class is placed at the one
    definition that is the class, or at line 1 of the module it names when
    that file does not hold it.
    """
    text = ran["site_views"]
    more = ran["site_more"]
    page = _line_of(text, "def page(")
    view = _line_of(text, "class Page(")
    callable_ = _line_of(text, "class Callable:")
    bare = _line_of(more, "    class Bare:") if sys.version_info >= (3, 13) else 1
    others = sorted(
        line
        for line in ran["lines"]
        if " URL pattern 'composed/" not in line and " URL pattern 'lazy/" not in line
    )
    assert others == sorted(
        [
            _finding("nested/^old/(?P<n>[0-9]+)/$", page, "site_views.page"),
            _finding("nested/deeper/leaf/", view, "site_views.Page"),
            _finding("listed/page/", page, "site_views.page"),
            _finding("loop/leaf/", page, "site_views.page"),
            _finding("a/p/", page, "site_views.page"),
            _finding("b/p/", page, "site_views.page"),
            _finding("deep/inside/", page, "site_views.page"),
            _finding("deep/edge/project/", page, "site_views.page"),
            _finding(
                "unregistered/",
                _line_of(ran["site_admin"], "    def other("),
                "site_admin.OtherAdmin.other",
                "site_admin.py",
            ),
            _finding(
                "partial/project/",
                _line_of(more, "class Part("),
                "site_more.Part",
                "site_more.py",
            ),
            _finding(
                "partial/hosted/",
                _line_of(more, "class Part("),
                "site_more.Part",
                "site_more.py",
            ),
            _finding(
                "twice/called/",
                _line_of(more, "    class Twice:"),
                "site_more.Twice",
                "site_more.py",
            ),
            _finding("twice/bare/", bare, "site_more.Bare", "site_more.py"),
            _finding(
                "kept/", _line_of(more, "@keep"), "site_more.Kept", "site_more.py"
            ),
            _finding(
                "outer/",
                _line_of(more, "    class Inner:"),
                "site_more.Outer.Inner",
                "site_more.py",
            ),
            _finding(
                "again/",
                _line_of(more, "    class Again:"),
                "site_more.Again",
                "site_more.py",
            ),
            _finding("gone/", 1, "site_more.Gone", "site_more.py"),
            _finding(
                "factory/",
                _line_of(more, "    class Made:"),
                "site_more.factory.<locals>.Made",
                "site_more.py",
            ),
            _finding("moved/methods/", 1, "site_api.Moved", "site_api.py"),
            _finding("moved/bare/", 1, "site_api.Plain", "site_api.py"),
            _finding("made/", 1, "site_views.made"),
        ]
    )
    assert callable_ > 1


def test_an_include_of_a_urlconf_inside_itself_is_read_once(
    ran: dict[str, Any],
) -> None:
    loop = sorted(route for route in ran["routes"] if route.startswith("loop/"))
    assert loop == ["loop/leaf/"]
    assert not [route for route in ran["routes"] if route.startswith("self/")]


def test_the_walk_ends_at_its_bounds(ran: dict[str, Any]) -> None:
    """A URLconf built anew at each include is followed ``_DEPTH`` includes
    deep, and the walk reads no more than ``_ENTRIES`` entries: here 7, the
    first an include.
    """
    lazy = sorted(route for route in ran["routes"] if route.startswith("lazy/"))
    assert lazy == sorted("lazy/" + "a/" * k + "p/" for k in range(ran["depth"]))
    assert ran["capped"] == ran["full"][:6]


def test_an_entry_that_cannot_be_read_is_passed_over(ran: dict[str, Any]) -> None:
    """An include of a URLconf with no patterns, and a route that cannot be
    shown, are passed over; the patterns around them are still reported.
    """
    assert not [r for r in ran["routes"] if r.startswith(("empty/", "unnamed/"))]
    assert "made/" in ran["routes"]
    assert "lazy/p/" in ran["routes"]


def test_a_walk_past_the_links_label_of_follows_is_not_reported(
    ran: dict[str, Any],
) -> None:
    [past] = ran["routes"]["deep/past/"]
    assert past == {"label": None, "walked": None}
    [inside] = ran["routes"]["deep/inside/"]
    assert inside["label"] is None
    assert inside["walked"] is not None
    assert inside["walked"][-1] is None
    # At the bound exactly: label_of still reads the innermost code, and so
    # does the walk; one link past it, neither does.
    for owner, label in (("project", None), ("hosted", "shop")):
        [edge] = ran["routes"][f"deep/edge/{owner}/"]
        assert edge["label"] == label
        assert edge["walked"] is not None
        assert edge["walked"][-1] == label
        assert ran["routes"][f"deep/beyond/{owner}/"] == [
            {"label": None, "walked": None}
        ]


def test_admin_views_are_left_to_w001(ran: dict[str, Any]) -> None:
    """Django's admin views of a project admin class serve as no app and are
    ``neolith.W001``'s; its own views serve as the admin app. None is
    reported here, though the project's class overrides a view and adds its
    own: a method behind the admin site's wrapper or bare, one it inherits
    from a class that is not an admin, and a function defined in the class.
    An admin class no site registers is not W001's, and its method is
    reported (above).
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
    for added in ("extra", "bound", "mixed", "inner"):
        [seen] = admin[f"admin/legacy/thing/{added}/"]
        assert seen == {"label": None, "walked": None}, added
    assert not [line for line in ran["lines"] if "'admin/" in line]


def test_an_entry_that_is_not_djangos_is_passed_over(ran: dict[str, Any]) -> None:
    assert all(route.split("/")[0] != "" for route in ran["routes"])


def test_a_second_run_gives_the_same_lines(ran: dict[str, Any]) -> None:
    assert ran["again"] == ran["lines"]


def test_nothing_is_reported_when_root_urlconf_is_missing_or_unreadable(
    ran: dict[str, Any],
) -> None:
    assert ran["skipped"] == {
        "'broken'": [],
        "'absent'": [],
        "5": [],
        "None": [],
        "'no_patterns'": [],
    }
    assert ran["database files"] == []
