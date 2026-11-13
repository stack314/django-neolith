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
    "tests.request_admin.shop",
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

from tests.request_admin.shop.models import Basket, Crate, Item, Order, Shelf

call_command("migrate", run_syncdb=True, verbosity=0)
user = User.objects.create_superuser("admin", "", "admin")
models = (Order, Basket, Item, Shelf, Crate)
ids = {m.__name__.lower(): m.objects.create(name="x").pk for m in models}
posts = [
    ("order", "quote"),
    ("order", "quote_everywhere"),
    ("basket", "quote_everywhere"),
    ("item", "quote_everywhere"),
    ("shelf", "quote_everywhere"),
    ("crate", "quote_everywhere"),
    ("crate", "project_quote"),
]
results = {}
client = Client()
client.force_login(user)
for model, action in posts:
    answer = client.post(
        f"/admin/shop/{model}/", {"action": action, "_selected_action": [ids[model]]}
    )
    results[f"wsgi {model} {action}"] = [answer.status_code, answer.content.decode()]


async def over_asgi():
    aclient = AsyncClient()
    await aclient.aforce_login(user)
    for model, action in posts:
        data = {"action": action, "_selected_action": [ids[model]]}
        answer = await aclient.post(f"/admin/shop/{model}/", data)
        content = answer.content.decode()
        results[f"asgi {model} {action}"] = [answer.status_code, content]


asyncio.run(over_asgi())
found = [[m.id, m.msg] for m in checks.run_checks(tags=["neolith"])]
printed = io.StringIO()
call_command("check", stdout=printed, stderr=printed)
print(json.dumps(
    {"results": results, "checks": found, "log": logged.getvalue(),
     "check": printed.getvalue()}
))
"""

_REFUSED = (
    " in no installed app; its views have no calling app, and a contract call "
    "from them is refused"
)
_W001 = f"tests.request_admin.project_admin.ItemAdmin: admin class{_REFUSED}"
_W001_OPTION = (
    f"tests.request_admin.project_admin.project_quote: admin function{_REFUSED}"
)


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


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("mode", ["wsgi", "asgi"])
@pytest.mark.parametrize("action", ["quote", "quote_everywhere"])
def test_an_admin_action_calls_as_the_app_of_its_admin_class(
    ran: dict[str, Any], mode: str, action: str
) -> None:
    """Case 7: the action is registered in the shop app's ``admin.py``, on
    its ``ModelAdmin``, so it calls as shop, not as ``django.contrib.admin``."""
    assert ran["results"][f"{mode} order {action}"] == [200, "as shop"]


@pytest.mark.spec("TRN-57")
@pytest.mark.parametrize("mode", ["wsgi", "asgi"])
def test_the_stock_admin_class_serves_as_the_model_s_app(
    ran: dict[str, Any], mode: str
) -> None:
    """The stock ``ModelAdmin`` runs no app's code, so its views run as the
    app whose model they serve; so does the class Django builds for
    ``register`` with options that hold no app's code."""
    assert ran["results"][f"{mode} basket quote_everywhere"] == [200, "as shop"]
    assert ran["results"][f"{mode} shelf quote_everywhere"] == [200, "as shop"]


@pytest.mark.spec("TRN-58", "TRN-42")
@pytest.mark.parametrize("mode", ["wsgi", "asgi"])
def test_an_admin_action_on_a_project_admin_class_is_refused(
    ran: dict[str, Any], mode: str
) -> None:
    """Case 8: the same action, on a ``ModelAdmin`` subclass in a project
    module, has no caller and is refused."""
    refused = [200, "refused unauthenticated"]
    assert ran["results"][f"{mode} item quote_everywhere"] == refused
    # A project function given as an option is the admin's own code.
    assert ran["results"][f"{mode} crate quote_everywhere"] == refused
    assert ran["results"][f"{mode} crate project_quote"] == refused


@pytest.mark.spec("APP-17")
def test_the_host_warns_about_an_admin_class_in_no_app(ran: dict[str, Any]) -> None:
    """Case 8: ``neolith.W001`` names the project's admin class and the
    project function given as an option, by the rule the binding uses, not
    the shop app's class, the stock ones or those of ``django.contrib.auth``."""
    assert ran["checks"] == [
        ["neolith.W001", _W001],
        ["neolith.W001", _W001_OPTION],
    ]


@pytest.mark.spec("APP-17")
def test_set_up_logs_the_warning_and_serves(ran: dict[str, Any]) -> None:
    """A warning does not stop the host; set-up logs it once."""
    assert ran["log"] == (f"(neolith.W001) {_W001}\n(neolith.W001) {_W001_OPTION}\n")


def test_manage_py_check_prints_the_warning(ran: dict[str, Any]) -> None:
    assert f"(neolith.W001) {_W001}" in ran["check"]
