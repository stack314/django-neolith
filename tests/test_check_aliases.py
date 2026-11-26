"""The check's rule on database aliases, ``neolith.E109`` (ADR 0030
Decisions 3 and 5; spec 05, APP-68): an alias that names ``default`` or
another hosted app's database, written as a literal or as
``DEFAULT_DB_ALIAS``, and the forms that mean ``default`` by leaving the
alias out.

The places of ``using`` are compared with the installed
``django.db.transaction``. Each generated call is judged against what the
interpreter binds: its arguments are bound to the real function's
signature, and the call is flagged exactly when the alias it binds to
``using`` is ``default`` (``None`` or left out, which Django reads as
``default``), or another hosted app's alias. The rule is then checked over
a host built in a child process (``tests/checking.py``).
"""

import ast
import inspect
import itertools
import json
import symtable
from pathlib import Path
from typing import Any

import django
import django.db.transaction
import pytest
from django.db.models import Manager, QuerySet

from neolith._check_calls import USING, App, Judged, judged
from neolith._check_source import _TRANSACTION, bound
from tests.checking import Project

pytestmark = pytest.mark.spec("APP-68")

_APP = App(
    label="shop",
    requires=frozenset(),
    aliases=frozenset({"desk"}),
    database=True,
    installed=frozenset({"shop", "desk", "quiet", "legacy"}),
    user_model="auth.User",
)
_HINT = (
    "name the app's own alias, 'shop', and reach another app's data through a "
    "contract it provides"
)


def _judge(code: str, app: App = _APP) -> list[Judged]:
    tree = ast.parse(code)
    table = symtable.symtable(code, "module.py", "exec")
    return judged(tree, bound(tree, table, "shop"), "shop", app)


def _lines(code: str) -> list[tuple[int, str]]:
    return [(found.line, found.id) for found in _judge(code)]


def test_the_places_of_using_are_those_of_the_installed_transaction_module() -> None:
    module = django.db.transaction
    places = {}
    for name, value in vars(module).items():
        if (
            inspect.isfunction(value)
            and value.__module__ == module.__name__
            and not name.startswith("_")
        ):
            parameters = list(inspect.signature(value).parameters)
            if "using" in parameters:
                places[f"{module.__name__}.{name}"] = parameters.index("using")
    # Every public function of the module takes using, on both versions.
    assert len(places) == (16 if django.VERSION >= (6, 1) else 15)
    assert places == dict(USING)
    # The listed names hold every supported version's; the set holds the
    # installed one's.
    missing = set() if django.VERSION >= (6, 1) else {"savepoint_create"}
    assert {f"django.db.transaction.{name}" for name in _TRANSACTION} == set(USING) | {
        f"django.db.transaction.{name}" for name in missing
    }


def test_a_transaction_function_the_installed_django_lacks_is_not_judged() -> None:
    # savepoint_create on Django 5.2: the call is an AttributeError there,
    # never a use of default.
    missing = [
        name
        for name in _TRANSACTION
        if not callable(getattr(django.db.transaction, name, None))
    ]
    assert missing == ([] if django.VERSION >= (6, 1) else ["savepoint_create"])
    for name in missing:
        for imports, callee in _SPELLINGS:
            code = f"{imports.format(f=name)}\n{callee.format(f=name)}()\n"
            assert _judge(code) == [], code
            decorator = callee.format(f=name)
            code = f"{imports.format(f=name)}\n@{decorator}\ndef f():\n    pass\n"
            assert _judge(code) == [], code


# Every way to bind and name a function of transaction: the import lines and
# the expression, with {f} for the function's name.
_SPELLINGS = [
    ("from django.db import transaction", "transaction.{f}"),
    ("from django.db.transaction import {f}", "{f}"),
    ("from django.db.transaction import {f} as g", "g"),
    ("import django.db.transaction", "django.db.transaction.{f}"),
    ("import django.db.transaction as t", "t.{f}"),
    ("from django import db", "db.transaction.{f}"),
    ("import django", "django.db.transaction.{f}"),
]
# What a function takes before using.
_BEFORE = {
    "on_commit": "print",
    "savepoint_commit": "'s1'",
    "savepoint_rollback": "'s1'",
    "set_autocommit": "True",
    "set_rollback": "True",
}


class _Unknown:
    """A value the check cannot read."""


# An alias as written, with what the interpreter binds for it.
_ALIASES: dict[str, object] = {
    "None": None,
    "'default'": "default",
    "DEFAULT_DB_ALIAS": "default",
    "utils.DEFAULT_DB_ALIAS": "default",
    "'desk'": "desk",
    "'shop'": "shop",
    "'quiet'": "quiet",
    "'replica'": "replica",
    "alias": _Unknown(),
    "1": 1,
    "lambda r: r": lambda r: r,
}


def _bound_using(name: str, arguments: str) -> object:
    """What the interpreter binds to ``using`` for ``arguments`` given to
    the transaction function ``name``, or what Django reads it as.
    """
    signature = inspect.signature(getattr(django.db.transaction, name))
    namespace: dict[str, Any] = {
        "_bind": signature.bind,
        "alias": _Unknown(),
        "view": lambda request: request,
        "DEFAULT_DB_ALIAS": django.db.DEFAULT_DB_ALIAS,
        "utils": django.db.utils,
    }
    call = eval(f"_bind({arguments})", namespace)  # noqa: S307
    return call.arguments.get("using")


# The functions of transaction that read a callable in the place of using
# as the function they decorate, on default (their callable(using) branch).
_READERS = frozenset(
    node.name
    for node in ast.parse(inspect.getsource(django.db.transaction)).body
    if isinstance(node, ast.FunctionDef)
    and not node.name.startswith("_")
    and "callable(using)" in ast.unparse(node)
)


def _flagged(using: object, app: App = _APP, name: str = "") -> bool:
    if callable(using):
        return name in _READERS
    if isinstance(using, _Unknown) or (using is not None and type(using) is not str):
        return False
    return using is None or using == "default" or using in app.aliases


_HEADER = "from django.db import DEFAULT_DB_ALIAS\nfrom django.db import utils\n"


def test_every_call_of_a_transaction_function_is_judged_by_the_alias_it_binds() -> None:
    checked = 0
    for qualified, (imports, callee) in itertools.product(sorted(USING), _SPELLINGS):
        name = qualified.rpartition(".")[2]
        before = [_BEFORE[name]] if name in _BEFORE else []
        variants = [", ".join(before)]
        for written in _ALIASES:
            variants.append(", ".join([*before, written]))
            variants.append(", ".join([*before, f"using={written}"]))
        for arguments in variants:
            code = (
                f"{_HEADER}{imports.format(f=name)}\n\n\ndef f(alias):\n"
                f"    {callee.format(f=name)}({arguments})\n"
            )
            line = code.count("\n")
            flagged = _flagged(_bound_using(name, arguments), name=name)
            assert _lines(code) == ([(line, "neolith.E109")] if flagged else []), code
            checked += 1
    assert checked == len(USING) * len(_SPELLINGS) * (1 + 2 * len(_ALIASES))


def test_every_transaction_function_as_a_bare_decorator_is_judged_by_its_call() -> None:
    # A bare decorator calls the function with the decorated one alone:
    # using is left out, which is default, or is the function itself, which
    # only the readers take as default.
    checked = 0
    for qualified, (imports, callee) in itertools.product(sorted(USING), _SPELLINGS):
        name = qualified.rpartition(".")[2]
        try:
            using = _bound_using(name, "view")
        except TypeError:
            using = _Unknown()
        flagged = _flagged(using, name=name)
        for target in ["def f():", "async def f():", "class C:"]:
            decorator = callee.format(f=name)
            code = f"{imports.format(f=name)}\n\n\n@{decorator}\n{target}\n    pass\n"
            expected = [(4, "neolith.E109")] if flagged else []
            assert _lines(code) == expected, code
            checked += 1
    assert checked == len(USING) * len(_SPELLINGS) * 3
    # The decorator forms the rule reports are those that leave using out
    # and the two readers.
    flagged_names = {
        name.rpartition(".")[2]
        for name, place in USING.items()
        if place > 0 or name.rpartition(".")[2] in _READERS
    }
    assert {"on_commit", "atomic", "non_atomic_requests"} <= flagged_names
    assert "get_autocommit" not in flagged_names


def test_a_call_with_no_alias_names_the_default_and_the_hint_the_own_alias() -> None:
    code = "from django.db import transaction\ntransaction.on_commit(print)\n"
    assert _judge(code) == [
        Judged(
            2,
            "neolith.E109",
            "on_commit() names no database alias, so it uses the database 'default'",
            _HINT,
        )
    ]
    # Following the hint.
    assert _judge(code.replace("print)", "print, using='shop')")) == []
    # An app without a database is pointed at a contract, not at an alias.
    alone = App(**{**{n: getattr(_APP, n) for n in App.__slots__}, "database": False})
    [found] = _judge(code, alone)
    assert found.hint == (
        "the app has no database of its own; reach another app's data through a "
        "contract it provides"
    )


# The other places an alias is read, with {a} for the alias as written: any
# keyword using=, the first argument of .using() and .db_manager() on any
# receiver (QuerySet.using(alias), Manager.db_manager(using=None, ...)), and
# the subscript of django.db.connections, however it was imported.
_PLACES = [
    ("", "Item.objects.using({a})"),
    ("", "Item.objects.using(alias={a})"),
    ("", "self.queryset.filter(x=1).using({a})"),
    ("", "Item.objects.db_manager({a})"),
    ("", "Item.objects.db_manager(using={a})"),
    ("", "item.save(using={a})"),
    ("", "anything(1, using={a})"),
    ("from django.db import connections", "connections[{a}]"),
    ("from django.db import connections as c", "c[{a}].cursor()"),
    ("from django import db", "db.connections[{a}]"),
    ("import django.db", "django.db.connections[{a}]"),
]


def test_the_place_names_are_django_s() -> None:
    assert list(inspect.signature(QuerySet.using).parameters) == ["self", "alias"]
    assert list(inspect.signature(Manager.db_manager).parameters)[:2] == [
        "self",
        "using",
    ]


def test_an_alias_in_each_place_names_default_or_another_app_s_database() -> None:
    for (imports, place), (written, value) in itertools.product(
        _PLACES, _ALIASES.items()
    ):
        code = f"{_HEADER}{imports}\n\n\ndef f(alias):\n    {place.format(a=written)}\n"
        # None in these places is the router's choice, not default.
        flagged = value is not None and _flagged(value)
        line = code.count("\n")
        assert _lines(code) == ([(line, "neolith.E109")] if flagged else []), code


def test_the_message_names_what_was_written_and_whose_database_it_is() -> None:
    code = (
        "from django.db import DEFAULT_DB_ALIAS, connections\n"
        "Item.objects.using('desk')\n"
        "connections[DEFAULT_DB_ALIAS]\n"
        "item.save(using='default')\n"
    )
    assert [(f.line, f.what) for f in _judge(code)] == [
        (2, ".using() names 'desk', the database of app desk"),
        (3, "connections[...] names DEFAULT_DB_ALIAS, the database 'default'"),
        (4, "using= names 'default', the database 'default'"),
    ]


@pytest.mark.parametrize(
    "decorator",
    [
        "atomic",
        "transaction.atomic",
        "non_atomic_requests",
        "transaction.non_atomic_requests",
        "django.db.transaction.atomic",
    ],
)
def test_atomic_and_non_atomic_requests_as_bare_decorators_mean_default(
    decorator: str,
) -> None:
    imports = (
        "import django.db.transaction\n"
        "from django.db import transaction\n"
        "from django.db.transaction import atomic, non_atomic_requests\n"
    )
    name = decorator.rpartition(".")[2]
    for target in ["def f():", "async def f():", "class C:"]:
        code = f"{imports}\n\n@{decorator}\n{target}\n    pass\n"
        assert _judge(code) == [
            Judged(
                6,
                "neolith.E109",
                f"@{name} names no database alias, so it uses the database 'default'",
                _HINT,
            )
        ], code
        # Called: judged by what it binds, like any call of it.
        assert _lines(code.replace(f"@{decorator}", f"@{decorator}()")) == [
            (6, "neolith.E109")
        ]
        assert _lines(code.replace(f"@{decorator}", f"@{decorator}('shop')")) == []
        assert _lines(code.replace(f"@{decorator}", f"@{decorator}(using='desk')")) == [
            (6, "neolith.E109")
        ]
    # A method under a class is decorated the same way.
    method = f"{imports}\nclass C:\n    @{decorator}\n    def f(self):\n        pass\n"
    assert _lines(method) == [(6, "neolith.E109")]


def test_the_bare_decorators_are_those_that_read_a_callable_as_the_view() -> None:
    # atomic and non_atomic_requests are the functions of transaction whose
    # first parameter, using, takes the decorated function itself.
    assert {"atomic", "non_atomic_requests"} == _READERS


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ("from django.db import connection\n", 1),
        ("from django.db import connection as conn\nconn.cursor()\n", 1),
        ("import django.db\ndjango.db.connection.cursor()\n", 2),
        ("from django import db\nx = db.connection\n", 2),
        ("import django.db as d\nd.connection.ops\n", 2),
        ("def f():\n    from django.db import connection\n", 2),
        ("from django.db import (\n    connections,\n    connection,\n)\n", 3),
        ("from django.db import (\n    connection as c,\n)\nc.cursor()\n", 2),
    ],
)
def test_django_db_connection_at_its_import_or_attribute_access(
    code: str, line: int
) -> None:
    assert _judge(code) == [
        Judged(
            line,
            "neolith.E109",
            "django.db.connection is the connection to the database 'default'",
            _HINT,
        )
    ]


@pytest.mark.parametrize(
    "code",
    [
        # The nearest legitimate shapes.
        "from django.db import connections\nconnections['shop'].cursor()\n",
        "from django.db import transaction\ntransaction.atomic('shop')\n",
        "from django.db import transaction\n"
        "@transaction.atomic(using='shop')\ndef f():\n    pass\n",
        "item.save(using=None)\nItem.objects.using(None)\n",
        "Item.objects.using(self._db)\nItem.objects.using(ALIAS)\n",
        "Item.objects.using('quiet')\nItem.objects.using('legacy')\n",
        "connection.cursor()\nself.connection.cursor()\n",
        "from somewhere import connections\nconnections['default']\n",
        "connections = {}\nconnections['default']\n",
        "from django.db import connections\nconnections = {}\nconnections['default']\n",
        "from django.db import transaction\ntransaction = None\ntransaction.atomic()\n",
        "from django.db import transaction\ndef f(transaction):\n"
        "    transaction.atomic()\n",
        "from django.db.transaction import atomic\nfrom os import *\natomic()\n",
        "from django.db import transaction\ntransaction.atomic(*args)\n",
        "from django.db import transaction\ntransaction.on_commit(f, **kwargs)\n",
        "from django.db import transaction\nx = transaction.atomic\n",
        "from django.db import DEFAULT_DB_ALIAS\nx = DEFAULT_DB_ALIAS\n",
        # A callable passed by name in the place of using is read as a
        # computed alias, as using=self._db is (ADR 0030 Decision 5).
        "from django.db import transaction\ntransaction.atomic(views.checkout)\n",
        "from django.db import transaction\ndef checkout(request):\n    pass\n"
        "checkout = transaction.non_atomic_requests(checkout)\n",
    ],
)
def test_nothing_is_reported_where_no_rule_applies(code: str) -> None:
    assert _judge(code) == []


def test_one_place_read_twice_is_reported_once() -> None:
    # A keyword using= that is also the place of using in a function of
    # transaction or of .db_manager() is one place.
    code = (
        "from django.db import transaction\n"
        "transaction.atomic(using='desk')\n"
        "Item.objects.db_manager(using='desk')\n"
    )
    assert _lines(code) == [(2, "neolith.E109"), (3, "neolith.E109")]


# A host of shop and desk, each with a database, and quiet with none, beside
# an undeclared first-party app legacy, on a real default.

_SHOP_VIEWS = """\
from django.db import DEFAULT_DB_ALIAS, connection, connections, transaction

from shop.models import Item


@transaction.atomic
def view(request, alias):
    Item.objects.using("desk")
    Item.objects.using("shop")
    connections[DEFAULT_DB_ALIAS]
    transaction.on_commit(print)
    transaction.on_commit(print, using="shop")
    Item.objects.db_manager("quiet")
    Item.objects.using(alias)
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


def test_e109_over_a_host(tmp_path: Path) -> None:
    project = Project(tmp_path)
    project.app(
        "shop",
        models="from django.db import models\n\n\nclass Item(models.Model):\n"
        "    pass\n",
    )
    project.write("shop/views.py", _SHOP_VIEWS)
    project.app("desk")
    project.write(
        "desk/tasks.py",
        "from django.db import transaction\n\n\ndef run():\n"
        "    with transaction.atomic('desk'):\n        pass\n",
    )
    project.write("quiet/__init__.py")
    project.write("quiet/apps.py", _QUIET_APP)
    project.write(
        "quiet/work.py",
        "from django.db import transaction\n\n\ndef run():\n"
        "    transaction.atomic('quiet')\n    transaction.atomic(using='default')\n",
    )
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write(
        "legacy/models.py",
        "from django.db import models\n\n\nclass Thing(models.Model):\n    pass\n",
    )
    # Code that no hosted app holds is not judged.
    project.write(
        "legacy/views.py", "from django.db import connection\nconnection.cursor()\n"
    )
    project.write(
        "helpers.py", "from django.db import transaction\ntransaction.atomic()\n"
    )
    project.settings(
        ["shop", "desk", "quiet"],
        neolith={
            "DATABASES": project.databases("default", "shop", "desk"),
            "UNDECLARED_APPS": ["legacy"],
        },
    )
    ran = project.run("-c", _SCRIPT, env={"DJANGO_SETTINGS_MODULE": "settings"})
    assert ran.status == 0, ran.stderr
    hint = _HINT
    quiet = (
        "the app has no database of its own; reach another app's data through a "
        "contract it provides"
    )
    prefix = "app shop: module shop.views: "

    def row(line: int, what: str) -> list[Any]:
        return ["shop/views.py", line, "neolith.E109", prefix + what, hint, "shop"]

    # quiet has no database, so its label is no alias of the host, and its
    # hint leads to a contract rather than to an alias.
    assert json.loads(ran.stdout) == [
        [
            "quiet/work.py",
            6,
            "neolith.E109",
            "app quiet: module quiet.work: using= names 'default', the database "
            "'default'",
            quiet,
            "quiet",
        ],
        row(1, "django.db.connection is the connection to the database 'default'"),
        row(6, "@atomic names no database alias, so it uses the database 'default'"),
        row(8, ".using() names 'desk', the database of app desk"),
        row(10, "connections[...] names DEFAULT_DB_ALIAS, the database 'default'"),
        row(
            11,
            "on_commit() names no database alias, so it uses the database 'default'",
        ),
    ]
