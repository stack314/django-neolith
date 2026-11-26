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

Beside the matrix, each guard of the rule has a fixture that fails when the
guard is deleted or loosened: the walk's bounds, cycle set and reads
(``mutual/``, ``flat``, ``raising``, a resolver look-alike), the class
placement's steps (``placed/``), the kinds of class and the admin's names
(``kinds/``), the blind spots the module names (``blind/``), and the parse
of each file once a run.
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


def plain(view):
    def wrapper(request, *args, **kwargs):
        return view(request, *args, **kwargs)

    return wrapper


class Part(functools.partial):
    pass
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
import functools
import importlib.util
import os
import sys

from asgiref.sync import sync_to_async
from django.contrib import admin
from django.urls import URLPattern, include, path
from django.urls.resolvers import RoutePattern
from django.views.generic import TemplateView

import site_admin
import site_api
import site_more
import site_views
import vendorlib.wrap
from legacy import admin as legacy_admin
from shop import admin as shop_admin
from legacy.models import Thing
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


class LookAlike:
    \"\"\"Django's resolver in all but its type.\"\"\"

    pattern = RoutePattern("alike/")
    urlconf_module = site_views
    url_patterns = [path("p/", site_views.page)]


# site_dot loaded from a path that names its file another way.
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "site_dot", os.path.join(here, ".", "site_dot.py")
)
site_dot = importlib.util.module_from_spec(spec)
sys.modules["site_dot"] = site_dot
spec.loader.exec_module(site_dot)

# A function of the namespace package site_ns, which has no file.
nowhere = {"__name__": "site_ns"}
exec(compile("def nowhere(request):\\n    pass\\n", "<n>", "exec"), nowhere)

# A function whose module's name cannot be looked up.
unhashable = {"__name__": ["site_views"]}
exec(compile("def odd(request):\\n    pass\\n", "<u>", "exec"), unhashable)


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
    path("mutual/", include("mutual_a")),
    path("raising/", include("raising")),
    LookAlike(),
    path("placed/told/", site_more.Told),
    path("placed/awaited/", site_more.Awaited),
    path("placed/twin/", site_more.Twin),
    path("placed/in/", site_more.Box.In),
    path("placed/short/", site_more.Short),
    path("placed/first/", site_more.First),
    path("placed/dotted/", site_dot.dotted),
    path("placed/borrows/", site_more.Borrows),
    path("placed/lent/", site_more.Lent),
    path("placed/oneline/", site_more.OneLine),
    path("kinds/abstract/", site_more.Abstract),
    path("kinds/metaview/", site_more.MetaView.as_view()),
    path("kinds/falsy/", vendorlib.wrap.wrapped(site_more.Falsy())),
    path("kinds/help/", site_admin.ThingAdminHelp),
    path("kinds/renamed/", site_admin.renamed),
    path("kinds/tagged/", site_admin.tagged),
    path("kinds/stripped/", site_admin.stripped),
    path("kinds/appadmin/", legacy_admin.site._registry[Thing].legacy),
    path("kinds/hostedadmin/", shop_admin.site._registry[Thing].shared),
    path("kinds/zero/", site_admin.zero),
    path("kinds/unnamed/", site_admin.unnamed),
    path("kinds/unhashable/", unhashable["odd"]),
    path("blind/lru/", functools.lru_cache(site_views.page)),
    path("blind/sync/", sync_to_async(site_views.page)),
    path("blind/partial/", vendorlib.wrap.Part(site_views.page)),
    path("blind/plain/", vendorlib.wrap.plain(site_views.page)),
    path("blind/nowhere/", nowhere["nowhere"]),
]
"""

# Classes that the project defines twice, and a partial of its own.
_MORE: Final = """\
import abc
import functools

from django.views import View


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


# Defined twice, its __firstlineno__ dropped: told apart by a classmethod.
class Told:
    pass


if True:

    class Told:  # noqa: F811
        @classmethod
        def build(cls):
            return None


Told.__module__ = __name__


async def amake():
    class Awaited:
        pass

    return Awaited


made = amake()
try:
    made.send(None)
except StopIteration as done:
    Awaited = done.value


# A class beside a nested class of its name, its __firstlineno__ dropped.
class Nest:
    class Twin:
        pass


class Twin:
    pass


Twin.__module__ = __name__


class Box:
    def method(self):
        return None

    if True:

        class In:
            pass


class Short:
    def get(self, request): return None


# Defined twice, the first kept under another name.
class Early:
    pass


First = Early

if True:

    class Early:  # noqa: F811
        pass


def helper(request):
    return None


# A function of its body defined outside it, which does not place it.
class Borrows:
    handle = helper


class LentMixin:
    def handle(self, request):
        return None


# A function of a class whose name starts with its own.
class Lent:
    handle = LentMixin.handle


# A class on one line, whose body is a lambda on that line.
class OneLine: get = lambda self, request: None


class Abstract(abc.ABC):
    def __call__(self, request):
        return None


class Kind(type):
    pass


class MetaView(View, metaclass=Kind):
    pass


class Falsy:
    def __call__(self, request):
        return None

    def __bool__(self):
        return False
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

_DOT: Final = """\
# Loaded by the root URLconf under a path with "/./" in it.


def dotted(request):
    return None
"""

# Two URLconfs that include each other.
_MUTUAL_A: Final = """\
from django.urls import include, path

import site_views

urlpatterns = [path("b/", include("mutual_b")), path("leaf/", site_views.page)]
"""

_MUTUAL_B: Final = """\
from django.urls import include, path

import site_views

urlpatterns = [path("a/", include("mutual_a")), path("leaf/", site_views.page)]
"""

# A URLconf whose patterns never end, flat; read past 1,000 entries, it
# raises. Each read of its urlpatterns is a new generator.
_FLAT: Final = """\
from django.urls import path

import site_views


reads = 0


def _endless():
    global reads
    entry = path("p/", site_views.page)
    for _ in range(1_000):
        reads += 1
        yield entry
    raise RuntimeError("read past the bound")


def __getattr__(name):
    if name == "urlpatterns":
        return _endless()
    raise AttributeError(name)
"""

# A URLconf whose patterns raise after the first.
_RAISING: Final = """\
from django.urls import path

import site_views


def _raising():
    yield path("p/", site_views.page)
    raise RuntimeError("a URLconf that fails partway")


def __getattr__(name):
    if name == "urlpatterns":
        return _raising()
    raise AttributeError(name)
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
import functools
import types

from django.urls import path

import site_views
import vendorlib.wrap
from neolith._serving import _LINKS
from shop import views as shop_views


def layered(count, view=site_views.page):
    for _ in range(count):
        view = vendorlib.wrap.wrapped(view)
    return view


def parted(request):
    return None


# The project's function, then _LINKS links that are not functions: bound
# methods and exact partials in turn (a partial of a partial is flattened
# into one, a partial of a bound method is not). The view is one link past
# them, so the walk holds only this function when the bound runs out.
chain = site_views.page
for i in range(_LINKS):
    chain = functools.partial(chain) if i % 2 else types.MethodType(chain, object())
parted.__wrapped__ = chain


urlpatterns = [
    path("inside/", layered(60)),
    path("past/", layered(70)),
    path("edge/project/", layered(_LINKS)),
    path("edge/hosted/", layered(_LINKS, shop_views.page)),
    path("beyond/project/", layered(_LINKS + 1)),
    path("beyond/hosted/", layered(_LINKS + 1, shop_views.page)),
    path("beyond/partials/", parted),
]
"""

# An admin class of the project's that overrides a view, so the link
# Django's admin wrapper keeps leads to the project's code, and adds views of
# its own; and one that no site registers, which W001 does not report.
_ADMIN: Final = """\
from django.contrib import admin
from django.db import models
from django.urls import path

from legacy.models import Thing


class Mixin:
    def mixed(self, request):
        return None


class Shared:
    def shared(self, request):
        return None


class Legacy:
    def legacy(self, request):
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

    def changelist_view(self, request, extra_context=None):
        return None


admin.site.register(Thing, ThingAdmin)
OTHER = OtherAdmin(Thing, admin.site)


# A model of an app that is not installed, under Django's own admin class:
# its admin is in no app and names no class.
class Ghost(models.Model):
    class Meta:
        app_label = "ghost"


admin.site.register(Ghost)


# The project's views in the admin's module, none of them the admin's.
def ThingAdminHelp(request):
    return None


def renamed(request):
    return None


renamed.__qualname__ = "ThingAdmin.renamed"


def tagged(request):
    return None


tagged.model_admin = None


# The function Django's admin builds for a view, with its model_admin taken
# away: a wrapper like any other.
[stripped] = [
    entry.callback
    for entry in OTHER.get_urls()
    if getattr(entry.callback, "__wrapped__", None) == OTHER.changelist_view
]
del stripped.model_admin


# An admin site whose registry is not a dict.
odd = admin.AdminSite(name="odd")
odd._registry = None


# A function whose code starts at line 0, and one whose code names a file
# that cannot be looked up.
def zero(request):
    return None


zero.__code__ = zero.__code__.replace(co_firstlineno=0)


def unnamed(request):
    return None


unnamed.__code__ = unnamed.__code__.replace(co_filename=__file__ + "\\0")
"""

# An admin of an undeclared app and one of the hosted app, each registered
# on a site of its own, that inherit a view from the project. The first is
# W001's (it names the project's class); the second serves as the hosted
# app, so W001 does not name it.
_APP_ADMIN: Final = """\
from django.contrib import admin

import site_admin
from legacy.models import Thing


class AppAdmin(site_admin.MIXIN, admin.ModelAdmin):
    pass


site = admin.AdminSite(name=__name__)
site.register(Thing, AppAdmin)
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
import sys

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


def attempt(make):
    try:
        return make()
    except Exception as error:
        return f"raised {type(error).__name__}"


_check_views._ENTRIES = 7
flat = attempt(lambda: [r for r, _ in _check_views.patterns("flat")])
flat_reads = sys.modules["flat"].reads
_check_views._ENTRIES = entries
raising = attempt(lambda: [r for r, _ in _check_views.patterns("raising")])

# Each file is parsed once a run, however many classes are placed in it.
import ast
import os
import types

parsed = []
counting = types.SimpleNamespace(**vars(ast))
counting.parse = lambda *a, **k: parsed.append(1) or ast.parse(*a, **k)
_check_views.ast = counting
counted = attempt(lambda: _check_views.findings(source))
_check_views.ast = ast

# A file gone, or not Python, when a class is placed in it.
import site_more

files = os.environ["PROBE_FILES"]
unread = {
    name: attempt(
        lambda: _check_views._class_line(
            site_more.Bare, os.path.join(files, name), {}
        )
    )
    for name in ("missing.py", "syntax.py", "nul.py")
}

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
    "flat": flat,
    "flat reads": flat_reads,
    "raising": raising,
    "parsed": len(parsed),
    "counted": (
        counted if isinstance(counted, str)
        else [_check._line(f, report_only=False) for f in counted]
    ),
    "unread": unread,
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
    project.write("legacy/admin.py", _APP_ADMIN.replace("MIXIN", "Legacy"))
    project.write("shop/admin.py", _APP_ADMIN.replace("MIXIN", "Shared"))
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
    project.write("site_dot.py", _DOT)
    project.write("site_ns/part.py")
    project.write("mutual_a.py", _MUTUAL_A)
    project.write("mutual_b.py", _MUTUAL_B)
    project.write("flat.py", _FLAT)
    project.write("raising.py", _RAISING)
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
    files = root / "files"
    files.mkdir()
    (files / "syntax.py").write_text("class (:\n")
    (files / "nul.py").write_bytes(b"class A:\n    pass\0\n")
    path = os.pathsep.join([str(project.root), str(library), *sys.path])
    done = project.run(
        "-c",
        _SCRIPT,
        env={
            "DJANGO_SETTINGS_MODULE": "settings",
            "PYTHONPATH": path,
            "PROBE_FILES": str(files),
        },
    )
    assert done.status == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    result["site_views"] = project.root.joinpath("site_views.py").read_text()
    result["site_more"] = project.root.joinpath("site_more.py").read_text()
    result["site_admin"] = project.root.joinpath("site_admin.py").read_text()
    result["site_dot"] = project.root.joinpath("site_dot.py").read_text()
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
            # "?": a module whose name is not text, which is in no app.
            assert (walked[-1] in (None, "?")) == (one["label"] is None), route


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
        if not re.search(r" URL pattern '(composed|lazy|placed|kinds|mutual)/", line)
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
    """A URLconf built anew at each include is followed 64 includes deep
    (``lazy/`` and 63 ``a/``), and not 65, and the walk reads no more than
    ``_ENTRIES`` entries: here 7, the first an include.
    """
    lazy = sorted(route for route in ran["routes"] if route.startswith("lazy/"))
    assert lazy == sorted("lazy/" + "a/" * k + "p/" for k in range(64))
    assert ran["depth"] == 64
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
    # A project function, then bound methods and partials past the bound:
    # the walk holds that one function when the bound runs out, and still
    # reports nothing.
    assert ran["routes"]["deep/beyond/partials/"] == [{"label": None, "walked": None}]
    assert not [line for line in ran["lines"] if "'deep/beyond/" in line]


def test_admin_views_are_left_to_w001(ran: dict[str, Any]) -> None:
    """Django's admin views of a project admin class serve as no app and are
    ``neolith.W001``'s; its own views serve as the admin app. None is
    reported here, though the project's class overrides a view and adds its
    own: a method behind the admin site's wrapper or bare, one it inherits
    from a class that is not an admin, and a function defined in the class.
    An admin class no site registers is not W001's, and its method is
    reported (above). Django's own admin class over a model of an app that
    is not installed names no class, and the rule runs past it.
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
    assert "admin/ghost/ghost/" in admin


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


def _group(ran: dict[str, Any], prefix: str) -> list[str]:
    return sorted(line for line in ran["lines"] if f" URL pattern '{prefix}" in line)


def test_a_class_is_placed_at_the_one_definition_that_agrees_with_it(
    ran: dict[str, Any],
) -> None:
    """Each step of the placement, pinned by a class it alone places: a
    classmethod's line, a class made in an ``async def``, a name that only
    ends a nested class's, a class inside an ``if`` inside a class, a method
    on a class's last line, and a first definition kept under another name
    (told apart by ``__firstlineno__``, from Python 3.13), a class that holds
    a function defined outside it, one that holds a function of a class whose
    name starts with its own, a class on one line whose body is a lambda;
    and a function whose code names its file by another path.
    """
    more = ran["site_more"]
    first = _line_of(more, "class Early:") if sys.version_info >= (3, 13) else 1
    assert _group(ran, "placed/") == sorted(
        [
            _finding(
                "placed/told/",
                _line_of(more, "    class Told:"),
                "site_more.Told",
                "site_more.py",
            ),
            _finding(
                "placed/awaited/",
                _line_of(more, "    class Awaited:"),
                "site_more.amake.<locals>.Awaited",
                "site_more.py",
            ),
            _finding(
                "placed/twin/",
                _line_of(more, "class Twin:"),
                "site_more.Twin",
                "site_more.py",
            ),
            _finding(
                "placed/in/",
                _line_of(more, "        class In:"),
                "site_more.Box.In",
                "site_more.py",
            ),
            _finding(
                "placed/short/",
                _line_of(more, "class Short:"),
                "site_more.Short",
                "site_more.py",
            ),
            _finding("placed/first/", first, "site_more.Early", "site_more.py"),
            _finding(
                "placed/borrows/",
                _line_of(more, "class Borrows:"),
                "site_more.Borrows",
                "site_more.py",
            ),
            _finding(
                "placed/lent/",
                _line_of(more, "class Lent:"),
                "site_more.Lent",
                "site_more.py",
            ),
            _finding(
                "placed/oneline/",
                _line_of(more, "class OneLine:"),
                "site_more.OneLine",
                "site_more.py",
            ),
            _finding(
                "placed/dotted/",
                _line_of(ran["site_dot"], "def dotted("),
                "site_dot.dotted",
                "site_dot.py",
            ),
        ]
    )


def test_a_file_that_cannot_be_read_or_parsed_places_a_class_at_line_1(
    ran: dict[str, Any],
) -> None:
    assert ran["unread"] == {"missing.py": 1, "syntax.py": 1, "nul.py": 1}


def test_each_file_is_parsed_once_a_run(ran: dict[str, Any]) -> None:
    """However many classes are placed in one file (the matrix places
    hundreds in ``site_views.py``), it is parsed once.
    """
    files = {line.split(":")[0] for line in ran["lines"]}
    assert 0 < ran["parsed"] <= len(files)
    assert ran["counted"] == ran["lines"]


def test_every_kind_of_class_and_admin_name_is_judged_by_its_code(
    ran: dict[str, Any],
) -> None:
    """A class with a metaclass, called or behind ``as_view``; a wrapper
    whose ``__wrapped__`` is falsy; and, in the admin's module, a function
    whose name only starts with the admin's, one whose ``__qualname__``
    claims the admin's, one that carries ``model_admin``, and the function
    Django's admin builds with its ``model_admin`` taken away: each is the
    project's view, not the admin's. A project method bound to the hosted
    app's admin is reported; bound to an undeclared app's, it is W001's. A
    code that starts at line 0, or whose file cannot be looked up, is placed
    at line 1; a function whose module's name is not text is passed over.
    """
    more = ran["site_more"]
    admin = ran["site_admin"]
    # OtherAdmin's changelist_view, the second in the file.
    stripped = [
        number
        for number, line in enumerate(admin.splitlines(), 1)
        if line.startswith("    def changelist_view(")
    ][1]
    assert stripped > _line_of(admin, "class OtherAdmin(")
    assert _group(ran, "kinds/") == sorted(
        [
            _finding(
                "kinds/abstract/",
                _line_of(more, "class Abstract("),
                "site_more.Abstract",
                "site_more.py",
            ),
            _finding(
                "kinds/metaview/",
                _line_of(more, "class MetaView("),
                "site_more.MetaView",
                "site_more.py",
            ),
            _finding(
                "kinds/falsy/",
                _line_of(more, "class Falsy:"),
                "site_more.Falsy",
                "site_more.py",
            ),
            _finding(
                "kinds/help/",
                _line_of(admin, "def ThingAdminHelp("),
                "site_admin.ThingAdminHelp",
                "site_admin.py",
            ),
            _finding(
                "kinds/renamed/",
                _line_of(admin, "def renamed("),
                "site_admin.renamed",
                "site_admin.py",
            ),
            _finding(
                "kinds/tagged/",
                _line_of(admin, "def tagged("),
                "site_admin.tagged",
                "site_admin.py",
            ),
            _finding(
                "kinds/stripped/",
                stripped,
                "site_admin.OtherAdmin.changelist_view",
                "site_admin.py",
            ),
            _finding(
                "kinds/hostedadmin/",
                _line_of(admin, "    def shared("),
                "site_admin.Shared.shared",
                "site_admin.py",
            ),
            _finding("kinds/zero/", 1, "site_admin.zero", "site_admin.py"),
            _finding("kinds/unnamed/", 1, "site_admin.unnamed", "site_admin.py"),
        ]
    )
    [app_admin] = ran["routes"]["kinds/appadmin/"]
    assert app_admin == {"label": None, "walked": None}


def test_a_view_behind_a_wrapper_label_of_does_not_follow_is_not_reported(
    ran: dict[str, Any],
) -> None:
    """The blind spots the module names: ``functools.lru_cache``,
    ``sync_to_async``, a library's ``partial`` subclass and a library
    wrapper with no ``__wrapped__`` around a project view; and a function of
    a namespace package, which has no file.
    """
    blind = sorted(route for route in ran["routes"] if route.startswith("blind/"))
    assert blind == [
        "blind/lru/",
        "blind/nowhere/",
        "blind/partial/",
        "blind/plain/",
        "blind/sync/",
    ]
    assert all(ran["routes"][route][0]["label"] is None for route in blind)
    assert _group(ran, "blind/") == []


def test_urlconfs_that_include_each_other_are_read_once_each(
    ran: dict[str, Any],
) -> None:
    mutual = sorted(route for route in ran["routes"] if route.startswith("mutual/"))
    assert mutual == ["mutual/b/leaf/", "mutual/leaf/"]
    assert len(_group(ran, "mutual/")) == 2


def test_one_urlconf_is_read_no_further_than_the_bound(ran: dict[str, Any]) -> None:
    """A flat ``urlpatterns`` without end is read to ``_ENTRIES`` (here 7),
    not to the 1,000 past which it raises, and not one entry further.
    """
    assert ran["flat"] == ["p/"] * 7
    assert ran["flat reads"] == 7


def test_patterns_that_raise_partway_are_passed_over_whole(
    ran: dict[str, Any],
) -> None:
    """Read in full before the walk, so that nothing escapes: as the root,
    nothing is reported; as an include, its routes are passed over.
    """
    assert ran["raising"] == []
    assert not [route for route in ran["routes"] if route.startswith("raising/")]


def test_an_object_shaped_like_a_resolver_is_not_followed(
    ran: dict[str, Any],
) -> None:
    assert not [route for route in ran["routes"] if route.startswith("alike/")]
