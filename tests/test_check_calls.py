"""The check's rules on calls: ``connect()`` outside ``requires``
(``neolith.E108``), a command scope for another app (``neolith.E110``), a
model or a config of another app named by a string (``neolith.E111``) and
``neolith.testing.boot()`` outside test modules (``neolith.W103``) (ADR
0030; spec 02, TRN-41; spec 05, APP-66 and APP-68).

Each rule is checked on generated modules, every way of binding each name
against every literal, with the expected answer worked out apart from the
rule, and then over a host built in a child process from a project written
under ``tmp_path`` (``tests/checking.py``), where the modules are read as
the check reads them and nothing of the project is imported.
"""

import ast
import itertools
import json
import symtable
import textwrap
from typing import Any

import pytest

from neolith._check_calls import App, Judged, booted, judged
from neolith._check_source import bound
from tests.checking import Project

_APP = App(
    label="shop",
    requires=frozenset({"desk.v1"}),
    aliases=frozenset({"desk"}),
    database=True,
    installed=frozenset({"shop", "desk", "quiet", "legacy", "auth", "contenttypes"}),
    user_model="auth.User",
)
_RECORDS_HINT = "reach that app's records through a contract it provides"


def _judge(code: str, app: App = _APP, package: str = "shop") -> list[Judged]:
    text = textwrap.dedent(code)
    tree = ast.parse(text)
    table = symtable.symtable(text, "module.py", "exec")
    return judged(tree, bound(tree, table, package), package, app)


def _ids(code: str, app: App = _APP) -> list[tuple[int, str]]:
    return sorted((found.line, found.id) for found in _judge(code, app))


# Every way a file binds each listed callee at its top level, as the import
# lines and the expression that names it.
_CONNECT = [
    ("from neolith import connect", "connect"),
    ("from neolith import connect as c", "c"),
    ("import neolith", "neolith.connect"),
    ("import neolith as n", "n.connect"),
    ("from neolith._host import connect", "connect"),
    ("try:\n    from neolith import connect\nexcept ImportError:\n    pass", "connect"),
]
_SCOPE = [
    ("from neolith import command_scope", "command_scope"),
    ("from neolith import command_scope as scope", "scope"),
    ("import neolith", "neolith.command_scope"),
    ("if True:\n    import neolith as n", "n.command_scope"),
]
_USER = [
    ("from django.contrib.auth import get_user_model", "get_user_model"),
    ("from django.contrib import auth", "auth.get_user_model"),
    ("import django.contrib.auth", "django.contrib.auth.get_user_model"),
    ("from django.contrib.auth import get_user_model as user", "user"),
]


def _module(imports: str, call: str) -> str:
    """A module with ``imports`` at the top and ``call`` on line 5, inside a
    function that never runs, which takes ``name`` as a computed value.
    """
    lines = imports.splitlines()
    assert len(lines) <= 4
    return "\n".join(
        [*lines, *[""] * (4 - len(lines)), "def f(name):", f"    {call}", ""]
    )


@pytest.mark.spec("TRN-41")
@pytest.mark.parametrize(("imports", "callee"), _CONNECT)
def test_connect_with_a_literal_outside_requires_is_e108(
    imports: str, callee: str
) -> None:
    # (argument, flagged): a required identity is admitted, the app's own
    # contract and an unknown one are not, a computed or non-str argument
    # is W101's or Python's.
    cases = [
        ("'desk.v1'", False),
        ("'other.v1'", True),
        ("'shop.v1'", True),
        ("'not an identity'", True),
        ("name", False),
        ("1", False),
        ("*name", False),
    ]
    for argument, flagged in cases:
        found = _judge(_module(imports, f"{callee}({argument})"))
        expected = [(6, "neolith.E108")] if flagged else []
        assert [(f.line, f.id) for f in found] == expected, argument


def test_e108_names_the_identity_and_the_hint_leads_to_requires() -> None:
    [found] = _judge(_module("from neolith import connect", "connect('other.v1')"))
    assert found == Judged(
        6,
        "neolith.E108",
        "connect('other.v1') names a contract version the app does not require",
        "list the contract in the app's requires",
    )
    # Following the hint: the same call with the contract required.
    required = App(**{**_fields(_APP), "requires": frozenset({"other.v1"})})
    assert (
        _judge(_module("from neolith import connect", "connect('other.v1')"), required)
        == []
    )


def _fields(app: App) -> dict[str, Any]:
    return {name: getattr(app, name) for name in App.__slots__}


@pytest.mark.spec("APP-68")
@pytest.mark.parametrize(("imports", "callee"), _SCOPE)
def test_command_scope_for_another_installed_app_is_e110(
    imports: str, callee: str
) -> None:
    cases = [
        ("'shop'", False),
        ("'desk'", True),
        ("app='desk'", True),
        ("'legacy'", True),
        ("'auth'", True),
        ("'nosuch'", False),
        ("name", False),
        ("app=name", False),
        ("**name", False),
    ]
    for argument, flagged in cases:
        found = _judge(_module(imports, f"{callee}({argument})"))
        expected = [(6, "neolith.E110")] if flagged else []
        assert [(f.line, f.id) for f in found] == expected, argument


def test_e110_names_the_app_and_its_hint_names_the_own_label() -> None:
    [found] = _judge(
        _module("from neolith import command_scope", "command_scope('desk')")
    )
    assert found == Judged(
        6,
        "neolith.E110",
        "command_scope('desk') opens a root for app desk",
        "open the scope for the app's own label, 'shop', and reach another app "
        "through a contract it provides",
    )


# get_model on any receiver, as Apps.get_model(app_label, model_name=None,
# require_ready=True) and AppConfig.get_model(model_name, require_ready=True)
# read their arguments: (arguments, the model named, or None).
_GET_MODEL = [
    ("'auth.User'", "auth.User"),
    ("'auth', 'User'", "auth.User"),
    ("app_label='auth', model_name='User'", "auth.User"),
    ("'auth', model_name='User'", "auth.User"),
    ("'auth.User', require_ready=False", "auth.User"),
    ("'auth.User', None", "auth.User"),
    ("'desk.Note'", "desk.Note"),
    ("'legacy', 'Thing'", "legacy.Thing"),
    ("'shop.Item'", None),
    ("'shop', 'Item'", None),
    ("'User'", None),
    ("'User', False", None),
    ("model_name='User'", None),
    ("'nosuch.User'", None),
    ("'auth.User.x'", None),
    ("'auth.'", None),
    ("'.User'", None),
    ("'auth', ''", None),
    ("'', 'User'", None),
    ("'auth', name", None),
    ("name, 'User'", None),
    ("*name", None),
]
_RECEIVERS = ["apps", "self.apps", "django_apps", "config", "registry().state"]


@pytest.mark.spec("APP-66")
def test_get_model_on_any_receiver_naming_another_app_is_e111() -> None:
    for receiver, (arguments, named) in itertools.product(_RECEIVERS, _GET_MODEL):
        found = _judge(_module("", f"{receiver}.get_model({arguments})"))
        expected = (
            []
            if named is None
            else [
                Judged(
                    6,
                    "neolith.E111",
                    f"get_model() names {named!r}, a model of app "
                    f"{named.partition('.')[0]}",
                    _RECORDS_HINT,
                )
            ]
        )
        assert found == expected, (receiver, arguments)


@pytest.mark.spec("APP-66")
def test_get_app_config_on_any_receiver_naming_another_app_is_e111() -> None:
    cases = [
        ("'auth'", "auth"),
        ("app_label='legacy'", "legacy"),
        ("'desk'", "desk"),
        ("'shop'", None),
        ("'nosuch'", None),
        ("name", None),
        ("1", None),
    ]
    for receiver, (arguments, label) in itertools.product(_RECEIVERS, cases):
        found = _judge(_module("", f"{receiver}.get_app_config({arguments})"))
        expected = (
            []
            if label is None
            else [
                Judged(
                    6,
                    "neolith.E111",
                    f"get_app_config({label!r}) names the config of app {label}",
                    _RECORDS_HINT,
                )
            ]
        )
        assert found == expected, (receiver, arguments)


@pytest.mark.spec("APP-66")
def test_a_sender_named_by_a_string_of_another_app_is_e111() -> None:
    cases = [
        ("post_save.connect(f, sender='auth.User')", "auth.User"),
        ("receiver(post_save, sender='desk.Note')", "desk.Note"),
        ("anything(sender='legacy.Thing')", "legacy.Thing"),
        ("post_save.connect(f, sender='shop.Item')", None),
        ("post_save.connect(f, sender='nosuch.Item')", None),
        ("post_save.connect(f, sender='auth')", None),
        ("post_save.connect(f, sender='auth.User.x')", None),
        ("post_save.connect(f, sender=User)", None),
        ("post_save.connect(f, 'auth.User')", None),
    ]
    for call, named in cases:
        found = _judge(_module("", call))
        expected = (
            []
            if named is None
            else [
                Judged(
                    6,
                    "neolith.E111",
                    f"sender={named!r} names a model of app {named.partition('.')[0]}",
                    _RECORDS_HINT,
                )
            ]
        )
        assert found == expected, call


@pytest.mark.spec("APP-66")
@pytest.mark.parametrize(("imports", "callee"), _USER)
def test_get_user_model_is_e111_when_auth_user_model_is_another_app_s(
    imports: str, callee: str
) -> None:
    # (AUTH_USER_MODEL, what follows its app in the message, or None).
    cases = [
        ("auth.User", ""),
        ("desk.Member", ""),
        ("gone.User", ", which this host does not hold"),
        ("shop.User", None),
        ("User", None),
        ("a.b.c", None),
        (None, None),
    ]
    for user_model, note in cases:
        app = App(**{**_fields(_APP), "user_model": user_model})
        found = _judge(_module(imports, f"{callee}()"), app)
        expected = (
            []
            if note is None
            else [
                Judged(
                    6,
                    "neolith.E111",
                    f"get_user_model() names AUTH_USER_MODEL {user_model!r}, a "
                    f"model of app {str(user_model).partition('.')[0]}{note}",
                    _RECORDS_HINT,
                )
            ]
        )
        assert found == expected, user_model
    # A reference that is not called names nothing.
    assert _judge(_module(imports, f"x = {callee}")) == []


# A name the file binds in another way as well is not followed (ADR 0030
# Decision 3), so a call through it is not judged.
@pytest.mark.parametrize(
    "code",
    [
        "from neolith import connect\nconnect = None\nconnect('other.v1')\n",
        "from neolith import connect\ndef f(connect):\n    connect('other.v1')\n",
        "def f():\n    from neolith import connect\n    connect('other.v1')\n",
        "from neolith import command_scope\nfrom os import *\ncommand_scope('desk')\n",
        "from django.contrib.auth import get_user_model\n"
        "for get_user_model in []:\n    pass\nget_user_model()\n",
        "c = connect\nc('other.v1')\n",
    ],
)
def test_a_name_bound_in_another_way_is_not_judged(code: str) -> None:
    assert _judge(code) == []


@pytest.mark.parametrize(
    ("imports", "callee"),
    [
        ("from neolith import testing", "testing.boot"),
        ("from neolith.testing import boot", "boot"),
        ("from neolith.testing import boot as b", "b"),
        ("import neolith.testing", "neolith.testing.boot"),
        ("import neolith.testing as t", "t.boot"),
        ("import neolith", "neolith.testing.boot"),
    ],
)
def test_every_way_to_call_testing_boot_is_found(imports: str, callee: str) -> None:
    code = _module(imports, f"{callee}()")
    tree = ast.parse(code)
    followed = bound(tree, symtable.symtable(code, "m.py", "exec"), "")
    assert booted(tree, followed) == [6]
    # Named but not called, or another boot.
    others = [f"x = {callee}", "boot()", "testing.boot()", "_boot.boot()"]
    for other in [each for each in others if each != f"{callee}()"]:
        code = _module(imports, other)
        tree = ast.parse(code)
        followed = bound(tree, symtable.symtable(code, "m.py", "exec"), "")
        assert booted(tree, followed) == [], other


# A host of three hosted apps, shop (which provides shop.v1 and whose model
# is AUTH_USER_MODEL), desk (which requires it) and quiet (no database),
# beside an undeclared first-party app legacy and the contrib apps auth and
# contenttypes, on a real default.

_SHOP_CONTRACTS = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Ping(BaseModel):
    n: int


@contract("shop.v1")
class Shop(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""

_SHOP_SERVICE = """\
from shop.contracts import Ping


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""

_SHOP_MODELS = """\
from django.db import models


class Item(models.Model):
    pass


class User(models.Model):
    last_login = None
"""

# Line 6 onwards, one call per line; the comment names what each finds.
_DESK_VIEWS = """\
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save

from neolith import command_scope, connect


def view(request, name):
    connect("shop.v1")
    connect("stock.v1")
    command_scope("desk")
    command_scope("shop")
    apps.get_model("shop", "Item")
    apps.get_model("desk.Note")
    apps.get_app_config("legacy")
    post_save.connect(view, sender="auth.User")
    get_user_model()
    connect(name)
"""

_SHOP_VIEWS = """\
from django.contrib.auth import get_user_model

from neolith import command_scope


def view(request):
    get_user_model()
    command_scope("shop")
    command_scope("nosuch")
"""

_MIGRATION = """\
from django.db import migrations


def forwards(apps, schema_editor):
    apps.get_model("shop", "Item")
    apps.get_model("desk", "Note")


class Migration(migrations.Migration):
    dependencies = [("desk", "0001_initial")]
    operations = [migrations.RunPython(forwards)]
"""

_BOOTS = """\
from neolith import testing

testing.boot()
"""

_LEGACY_APP = """\
from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
    default_auto_field = "django.db.models.AutoField"
"""

_QUIET_APP = """\
from neolith import NeolithAppConfig


class Config(NeolithAppConfig):
    name = "quiet"
    label = "quiet"
"""

_SCRIPT = """\
import json

import django

django.setup()

from django.apps import apps

from neolith import _check, _check_calls, _check_source
from neolith._declaration import declaration_of

hosted = {
    config.label: config
    for config in apps.get_app_configs()
    if declaration_of(type(config)) is not None
}
with _check.guarded():
    source = _check_source.Source(hosted, _check._owner, settings_module="settings")
    found = _check_calls.findings(source)
print(json.dumps(sorted(
    [f.path, f.line, f.message.id, f.message.msg, f.message.hint, f.app]
    for f in found
)))
"""


@pytest.fixture(scope="module")
def host(tmp_path_factory: pytest.TempPathFactory) -> list[list[Any]]:
    """The findings of the call rules over one host, as
    ``[path, line, id, message, hint, app]``, sorted.
    """
    project = Project(tmp_path_factory.mktemp("calls"))
    project.write("shop/contracts.py", _SHOP_CONTRACTS)
    project.write("shop/service.py", _SHOP_SERVICE)
    project.app(
        "shop",
        models=_SHOP_MODELS,
        imports="from neolith import Provided\nfrom shop.contracts import Shop\n",
        config="""\
        provides = (
            Provided(Shop, implementation="shop.service.Service", principal_from=()),
        )
        """,
    )
    project.write("shop/views.py", _SHOP_VIEWS)
    # A test module of an app is the app's code for every rule but W103.
    project.write(
        "shop/tests.py", _BOOTS + "\nfrom neolith import connect\nconnect('stock.v1')\n"
    )
    project.write("shop/setup_host.py", _BOOTS)
    project.app(
        "desk",
        models="from django.db import models\n\n\nclass Note(models.Model):\n"
        "    pass\n",
        imports="from shop.contracts import Shop\n",
        config="requires = (Shop,)\n",
    )
    project.write("desk/views.py", _DESK_VIEWS)
    project.write("desk/migrations/0002_data.py", _MIGRATION)
    project.write("quiet/__init__.py")
    project.write("quiet/apps.py", _QUIET_APP)
    project.write("quiet/views.py", _BOOTS)
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/models.py", "")
    # Code no hosted app holds is not judged, but for W103.
    project.write(
        "legacy/views.py",
        _BOOTS + "\nfrom neolith import command_scope\ncommand_scope('desk')\n",
    )
    project.write("helpers.py", "from neolith import connect\nconnect('stock.v1')\n")
    project.write("wsgi.py", _BOOTS)
    project.write("conftest.py", _BOOTS)
    project.write("tests/helpers.py", _BOOTS)
    project.write("pkg/__init__.py", _BOOTS)
    project.write("pkg/test_more.py", _BOOTS)
    project.write("pkg/more_test.py", _BOOTS)
    project.write("pkg/sub/tests/deep.py", _BOOTS)
    project.write("broken.py", _BOOTS + "return\n")
    project.settings(
        ["shop", "desk", "quiet"],
        neolith={
            "DATABASES": project.databases("default", "shop", "desk"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "legacy",
            ],
        },
        below='AUTH_USER_MODEL = "shop.User"\n',
    )
    ran = project.run("-c", _SCRIPT, env={"DJANGO_SETTINGS_MODULE": "settings"})
    assert ran.status == 0, ran.stderr
    found: list[list[Any]] = json.loads(ran.stdout)
    return found


def _rows(host: list[list[Any]], kind: str) -> list[tuple[str, int, str]]:
    return [
        (path, line, message) for path, line, id_, message, _, _ in host if id_ == kind
    ]


@pytest.mark.spec("TRN-41")
def test_e108_over_a_host(host: list[list[Any]]) -> None:
    assert _rows(host, "neolith.E108") == [
        (
            "desk/views.py",
            10,
            "app desk: module desk.views: connect('stock.v1') names a contract "
            "version the app does not require",
        ),
        (
            "shop/tests.py",
            6,
            "app shop: module shop.tests: connect('stock.v1') names a contract "
            "version the app does not require",
        ),
    ]


@pytest.mark.spec("APP-68")
def test_e110_over_a_host(host: list[list[Any]]) -> None:
    # desk's own label, shop's own label in shop and a label no app has are
    # admitted; code in an undeclared app is not judged.
    assert _rows(host, "neolith.E110") == [
        (
            "desk/views.py",
            12,
            "app desk: module desk.views: command_scope('shop') opens a root for "
            "app shop",
        )
    ]


@pytest.mark.spec("APP-66")
def test_e111_over_a_host_a_migration_s_registry_included(
    host: list[list[Any]],
) -> None:
    assert _rows(host, "neolith.E111") == [
        (
            "desk/migrations/0002_data.py",
            5,
            "app desk: module desk.migrations.0002_data: get_model() names "
            "'shop.Item', a model of app shop",
        ),
        (
            "desk/views.py",
            13,
            "app desk: module desk.views: get_model() names 'shop.Item', a model "
            "of app shop",
        ),
        (
            "desk/views.py",
            15,
            "app desk: module desk.views: get_app_config('legacy') names the "
            "config of app legacy",
        ),
        (
            "desk/views.py",
            16,
            "app desk: module desk.views: sender='auth.User' names a model of app auth",
        ),
        (
            "desk/views.py",
            17,
            "app desk: module desk.views: get_user_model() names AUTH_USER_MODEL "
            "'shop.User', a model of app shop",
        ),
    ]
    # Every finding names the hosted app whose module holds it, with the hint.
    for path, _, kind, _, hint, app in host:
        if kind == "neolith.E111":
            assert hint == _RECORDS_HINT
            assert app == path.partition("/")[0]


_BOOT_HINT = (
    "call it from a test module only: conftest.py, tests.py, test_*.py, "
    "*_test.py, or a file under a directory named tests"
)


def test_w103_in_first_party_modules_other_than_test_modules(
    host: list[list[Any]],
) -> None:
    def boots(path: str, module: str, app: str | None) -> list[Any]:
        where = "" if app is None else f"app {app}: "
        hosted = app if app in ("shop", "desk", "quiet") else None
        return [
            path,
            3,
            "neolith.W103",
            f"{where}module {module}: calls neolith.testing.boot(), which turns "
            "Boot's schema read off",
            _BOOT_HINT,
            hosted,
        ]

    assert [row for row in host if row[2] == "neolith.W103"] == [
        boots("legacy/views.py", "legacy.views", "legacy"),
        boots("pkg/__init__.py", "pkg", None),
        boots("quiet/views.py", "quiet.views", "quiet"),
        boots("shop/setup_host.py", "shop.setup_host", "shop"),
        boots("wsgi.py", "wsgi", None),
    ]


def test_only_the_call_rules_are_found_and_each_once(host: list[list[Any]]) -> None:
    assert {row[2] for row in host} == {
        "neolith.E108",
        "neolith.E110",
        "neolith.E111",
        "neolith.W103",
    }
    assert len({tuple(row) for row in host}) == len(host)
