"""Request roots for views of Django's admin: cases 7 and 8 of the test plan
on #11 (spec 02, Request roots; spec 05, Before Serve).

Django's admin wraps each ``ModelAdmin`` view in a function of its own
module, so the host takes the app of the ``ModelAdmin`` class, or the
model's app for the stock ``ModelAdmin``, which runs no app's code
(``docs/django-host.md``). A ``ModelAdmin`` class in a project module has no
app: its views' calls are refused, and the host warns about it
(``neolith.W001``).

The admin needs tables, users and sessions, so the host runs in a process
of its own on a SQLite file, once for the module, and each test reads what
it printed. Each admin action is posted over WSGI, with the test client,
and over ASGI, with the async test client.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.hosting import in_fresh_process

INSTALLED = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.flatpages",
    "django.contrib.sitemaps",
    "django.contrib.syndication",
    "tests.request_admin.shop",
    "tests.request_admin.depot",
    "tests.host_apps.couriers",
    "neolith",
]

_BODY = """
import asyncio
import io
import logging

import django

logged = io.StringIO()
handler = logging.StreamHandler(logged)
logging.getLogger("neolith.host").addHandler(handler)
django.setup()

from django.core import checks
from django.core.management import call_command
from django.contrib.auth.models import User
from django.test import AsyncClient, Client

from django.contrib import admin
from django.db import models
from django.test import override_settings

from neolith import _checks
from neolith._serving import admin_code
from tests.request_admin.depot.models import Bin, Pallet
from tests.request_admin.shop.models import Basket, Crate, Item, Order, Shelf, Team

call_command("migrate", run_syncdb=True, verbosity=0)
user = User.objects.create_superuser("admin", "", "admin")
made = (Order, Basket, Item, Shelf, Crate, Team, Pallet, Bin)
ids = {m.__name__.lower(): m.objects.create(name="x").pk for m in made}
posts = [
    ("shop", "order", "quote"),
    ("shop", "order", "quote_everywhere"),
    ("shop", "order", "project_quote"),
    ("shop", "basket", "quote_everywhere"),
    ("shop", "team", "quote_everywhere"),
    ("shop", "item", "quote_everywhere"),
    ("shop", "crate", "quote_everywhere"),
    ("shop", "crate", "project_quote"),
    ("shop", "shelf", "quote_everywhere"),
    ("depot", "pallet", "quote_everywhere"),
    ("depot", "bin", "quote_everywhere"),
]
results = {}
client = Client()
client.force_login(user)
for app, model, action in posts:
    answer = client.post(
        f"/admin/{app}/{model}/", {"action": action, "_selected_action": [ids[model]]}
    )
    results[f"wsgi {model} {action}"] = [answer.status_code, answer.content.decode()]


async def over_asgi():
    aclient = AsyncClient()
    await aclient.aforce_login(user)
    for app, model, action in posts:
        data = {"action": action, "_selected_action": [ids[model]]}
        answer = await aclient.post(f"/admin/{app}/{model}/", data)
        content = answer.content.decode()
        results[f"asgi {model} {action}"] = [answer.status_code, content]


asyncio.run(over_asgi())
found = [[m.id, m.msg] for m in checks.run_checks(tags=["neolith"])]


class Stray(models.Model):
    class Meta:
        app_label = "nowhere"


stray = list(admin_code(admin.ModelAdmin(Stray, admin.site)))

# Views the framework supplies, as a project configures them: each judged by
# its own code (spec 02, after TRN-57).
import functools

from django.contrib.auth import views as auth_views
from django.contrib.flatpages import views as flatpages_views
from django.contrib.sitemaps import GenericSitemap
from django.contrib.sitemaps import views as sitemap_views
from django.contrib.syndication.views import Feed
from django.urls import resolve
from django.views import generic

from neolith._serving import label_of
from tests.request_admin.shop.feeds import OrderFeed

framework_views = {
    "list view given a model": generic.ListView.as_view(model=Order),
    "detail view given a queryset": generic.DetailView.as_view(
        queryset=Order.objects.all()
    ),
    "redirect view": generic.RedirectView.as_view(url="/"),
    "sitemap": sitemap_views.sitemap,
    "sitemap with its sitemaps": functools.partial(
        sitemap_views.sitemap,
        sitemaps={"orders": GenericSitemap({"queryset": Order.objects.all()})},
    ),
    "flatpage": flatpages_views.flatpage,
    "feed of the framework": Feed(),
    "feed of the shop app": OrderFeed(),
    "login view": auth_views.LoginView.as_view(),
    "password change view": auth_views.PasswordChangeView.as_view(),
    "admin index": resolve("/admin/").func,
    "admin login": resolve("/admin/login/").func,
    "admin app index": resolve("/admin/shop/").func,
    "admin changelist": resolve("/admin/shop/basket/").func,
}
framework = {name: label_of(view) for name, view in framework_views.items()}
with override_settings(INSTALLED_APPS=["django.contrib.admin", "django.contrib.auth",
        "django.contrib.contenttypes", "django.contrib.sessions",
        "django.contrib.messages"]):
    unhosted = _checks.admin_check()
printed = io.StringIO()
call_command("check", stdout=printed, stderr=printed)
print(json.dumps(
    {"results": results, "checks": found, "log": logged.getvalue(),
     "check": printed.getvalue(), "stray": stray, "unhosted": len(unhosted),
     "framework": framework}
))
"""

_REFUSED = (
    ": admin class in no installed app; its views have no calling app, and a "
    "contract call from them is refused"
)
_W001 = [
    f"tests.request_admin.project_admin.ItemAdmin{_REFUSED}",
    f"tests.request_admin.project_admin.OtherItemAdmin{_REFUSED}",
]


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    database: Path = tmp_path_factory.mktemp("admin") / "db.sqlite3"
    printed = in_fresh_process(
        _BODY,
        INSTALLED,
        {
            "DATABASES": {
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": str(database),
                }
            },
            "DEFAULT_AUTO_FIELD": "django.db.models.AutoField",
            "SITE_ID": 1,
            "MIDDLEWARE": [
                "neolith.middleware.RootMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware",
            ],
            "ROOT_URLCONF": "tests.request_admin.urls",
            "SECRET_KEY": "a key for one test process only",
            "TEMPLATES": [
                {
                    "BACKEND": "django.template.backends.django.DjangoTemplates",
                    "APP_DIRS": True,
                    "OPTIONS": {
                        "context_processors": [
                            "django.template.context_processors.request",
                            "django.contrib.auth.context_processors.auth",
                            "django.contrib.messages.context_processors.messages",
                        ]
                    },
                }
            ],
        },
    )
    found: dict[str, Any] = json.loads(printed.splitlines()[-1])
    return found


MODES = ["wsgi", "asgi"]


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("post", "outcome"),
    [
        # Case 7: an action of the shop app's admin class calls as shop, not
        # as django.contrib.admin, whichever module the action is in.
        ("order quote", "as shop"),
        ("order quote_everywhere", "as shop"),
        ("order project_quote", "as shop"),
        # The admin class's app beats the model's, both ways.
        ("pallet quote_everywhere", "as shop"),
        ("shelf quote_everywhere", "as depot"),
        # No class of the app's own: the model's app.
        ("basket quote_everywhere", "as shop"),
        ("bin quote_everywhere", "as depot"),
        ("team quote_everywhere", "as shop"),
        ("crate quote_everywhere", "as shop"),
        ("crate project_quote", "as shop"),
    ],
)
def test_an_admin_view_calls_as_the_app_of_its_admin_class(
    ran: dict[str, Any], mode: str, post: str, outcome: str
) -> None:
    """The most derived admin class in a hosted app decides; with none, as
    for the stock admin, Django's ``GroupAdmin`` and the class Django builds
    for ``register(Model, **options)``, the model's app does."""
    assert ran["results"][f"{mode} {post}"] == [200, outcome]


@pytest.mark.spec("TRN-58", "TRN-42")
@pytest.mark.parametrize("mode", MODES)
def test_an_admin_action_on_a_project_admin_class_is_refused(
    ran: dict[str, Any], mode: str
) -> None:
    """Case 8: an action on a ``ModelAdmin`` subclass in a project module
    has no caller and is refused."""
    assert ran["results"][f"{mode} item quote_everywhere"] == [
        200,
        "refused unauthenticated",
    ]


@pytest.mark.spec("APP-17")
def test_the_host_warns_about_each_admin_class_in_no_app(ran: dict[str, Any]) -> None:
    """Case 8: ``neolith.W001`` names the project's admin classes, on every
    admin site, by the rule the binding uses, and no other."""
    assert ran["checks"] == [["neolith.W001", m] for m in _W001]


@pytest.mark.spec("APP-17")
def test_set_up_logs_the_warning_and_serves(ran: dict[str, Any]) -> None:
    """A warning does not stop the host; set-up logs it once."""
    assert ran["log"] == "".join(f"(neolith.W001) {m}\n" for m in _W001)


def test_manage_py_check_prints_the_warning(ran: dict[str, Any]) -> None:
    assert all(f"(neolith.W001) {m}" in ran["check"] for m in _W001)


def test_a_stock_admin_of_a_model_in_no_installed_app_has_no_app(
    ran: dict[str, Any],
) -> None:
    assert ran["stray"] == [None, None]


def test_the_warning_judges_only_a_process_that_hosts(ran: dict[str, Any]) -> None:
    assert ran["unhosted"] == 0


def test_the_warning_does_not_import_the_admin() -> None:
    """In a process that never imported the admin, the check finds nothing
    and imports nothing."""
    import sys

    from django.test import override_settings

    from neolith import _checks
    from tests import host_apps

    assert "django.contrib.admin.sites" not in sys.modules
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        assert _checks.admin_check() == []
    assert "django.contrib.admin.sites" not in sys.modules


@pytest.mark.spec("TRN-57", "TRN-58")
@pytest.mark.parametrize(
    ("view", "app"),
    [
        # A generic view is the framework's code, whatever model it is given.
        ("list view given a model", None),
        ("detail view given a queryset", None),
        ("redirect view", None),
        # A view of an installed app of the framework belongs to that app.
        ("sitemap", "sitemaps"),
        ("sitemap with its sitemaps", "sitemaps"),
        ("flatpage", "flatpages"),
        ("feed of the framework", "syndication"),
        ("login view", "auth"),
        ("password change view", "auth"),
        ("admin index", "admin"),
        ("admin login", "admin"),
        ("admin app index", "admin"),
        # A framework class subclassed by an app is the app's code.
        ("feed of the shop app", "shop"),
        # Only the administration site serves a model as the model's app.
        ("admin changelist", "shop"),
    ],
)
def test_a_view_the_framework_supplies_is_judged_by_its_own_code(
    ran: dict[str, Any], view: str, app: str | None
) -> None:
    """Every kind of view Django supplies, as a project configures it: the
    spec's sentences after TRN-57 predict each result."""
    assert ran["framework"][view] == app
