"""An app's database: its own alias, and a router that keeps its data there
(spec 05, An app's database; ADR 0004, ADR 0009).

Routing is judged as Django runs it: a host in a process of its own, once
with a real ``default`` and once closed (``default: {}``), on SQLite files,
with ``neolith.routers.AppRouter`` as its one router. Each process asks
every routing question over the matrix of owners (hosted apps with and
without a database, an app with no declaration, Django's contrib apps, and
labels of no installed app) and prints the answers; the tests here hold
each answer to the spec's equation:

- ``read(M) == write(M) == home(label(M))``, whatever the hints say;
- ``allow_migrate(db, L) == (db == home(L))``;
- ``allow_relation(x, y) == (home(x) == home(y))``;

where ``home(L)`` is ``L`` for a hosted app with a database and
``"default"`` for every other label. Then the same process shows each
equation through Django's own paths: where rows land, which tables a
migrate creates on each alias, which writes a transaction covers, and what a
raw connection reaches.

The two checks the host runs at set-up, ``neolith.E014`` and
``neolith.E017``, are tested through their plain functions on isolated
registries, and once each through a set-up that refuses.
"""

import itertools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.apps.registry import Apps
from django.core import checks
from django.db import connection, models
from django.test.utils import isolate_apps
from hypothesis import given, settings
from hypothesis import strategies as st

from neolith import _checks
from neolith._checks import database_problems, table_name_problems
from tests.hosting import in_fresh_process

# A hosted app with a database lives on its own alias, named by its label;
# everything else lives on default (ADR 0009 Decisions 2 to 4).
HOSTED_WITH_DATABASE = ("ledger", "billing")
LABELS = (
    "ledger",
    "billing",
    "notes",
    "legacy",
    "auth",
    "contenttypes",
    "neolith",
    "django_cache",
    "unknown",
)


def home(label: str) -> str:
    return label if label in HOSTED_WITH_DATABASE else "default"


_ROUTER = ["neolith.routers.AppRouter"]

_BODY = """
import contextlib
import json
import sqlite3
import types

import django
from django.conf import settings

django.setup()

from django.apps import apps
from django.apps.registry import Apps
from django.core import checks
from django.core.cache.backends.db import Options as CacheOptions
from django.core.management import call_command
from django.db import connection, models, router, transaction

from neolith.routers import AppRouter
from tests.app_db.billing.models import Invoice
from tests.app_db.ledger.models import Account, Entry, Tag

# Django fills a blank default in with its dummy backend.
real_default = settings.DATABASES["default"]["ENGINE"] != "django.db.backends.dummy"
ALIASES = list(settings.DATABASES)
DBS = [*ALIASES, "notes", "other"]
LABELS = json.loads(sys.argv[3])


def outcome(run):
    try:
        return ["ok", run()]
    except Exception as error:
        return ["raised", type(error).__name__]


def tables(alias):
    with contextlib.closing(sqlite3.connect(settings.DATABASES[alias]["NAME"])) as db:
        return sorted(
            row[0]
            for row in db.execute(
                "select name from sqlite_master where type = 'table' "
                "and name not like 'sqlite_%'"
            )
        )


def count(table):
    found = {}
    for alias in ALIASES:
        if alias == "default" and not real_default:
            continue
        name = settings.DATABASES[alias]["NAME"]
        with contextlib.closing(sqlite3.connect(name)) as db:
            if db.execute(
                "select 1 from sqlite_master where name = ?", (table,)
            ).fetchone():
                found[alias] = db.execute(f"select count(*) from {table}").fetchone()[0]
    return found


# Every alias is migrated with no app label, so allow_migrate alone decides
# what is created where.
migrated = {
    alias: outcome(lambda: call_command("migrate", database=alias, verbosity=0))
    for alias in ALIASES
}
schema = {
    alias: tables(alias) for alias in ALIASES if alias != "default" or real_default
}
model_tables = sorted(
    [model._meta.app_label, model._meta.db_table]
    for model in apps.get_models(include_auto_created=True)
    if model._meta.managed and not model._meta.proxy
)

# Models of every label: the installed ones, one per label in a registry of
# its own, and the database cache's stand-in, as Django builds it.
fake_apps = Apps([])


def fake(label):
    meta = type("Meta", (), {"app_label": label, "apps": fake_apps})
    attrs = {"__module__": __name__, "Meta": meta}
    return type(f"Fake_{label}", (models.Model,), attrs)


questioned = list(apps.get_models(include_auto_created=True))
questioned += [fake(label) for label in LABELS]
questioned.append(type("CacheEntry", (), {"_meta": CacheOptions("cache_table")}))


def at(db):
    return types.SimpleNamespace(_state=types.SimpleNamespace(db=db))


HINTS = [{}] + [{"instance": at(db)} for db in [None, *DBS]]
mine = AppRouter()
routes = []
for model in questioned:
    for hints in HINTS:
        hint = None if not hints else hints["instance"]._state.db
        for name, asked in (("router", mine), ("django", router)):
            routes.append([
                model._meta.app_label, hint, name,
                asked.db_for_read(model, **hints), asked.db_for_write(model, **hints),
            ])

migrations = []
for db in DBS:
    for label in LABELS:
        for hints in ({}, {"model_name": "x"}, {"model": questioned[0]}):
            for name, asked in (("router", mine), ("django", router)):
                migrations.append([db, label, sorted(hints), name,
                                   asked.allow_migrate(db, label, **hints)])

# Relations between instances, each with a _state.db the router must ignore.
instances = []
for model in questioned[:-1]:
    for db in (None, "default", "ledger"):
        made = model()
        made._state.db = db
        instances.append(made)
relations = []
for first, second in itertools.product(instances, repeat=2):
    for name, asked in (("router", mine), ("django", router)):
        relations.append([
            first._meta.app_label, second._meta.app_label, name,
            asked.allow_relation(first, second),
        ])

# Where Django's own paths take each operation.
querysets = {m._meta.label: m.objects.all().db for m in (Account, Entry, Tag, Invoice)}
account = Account.objects.create(name="a")
entry = Entry.objects.create(account=account, amount=1)
account.tags.add(Tag.objects.create(name="t"))
invoice = Invoice.objects.create(number="1")
placed = {
    "states": [m._state.db for m in (account, entry, invoice)],
    "fetched": [type(m).objects.get(pk=m.pk)._state.db for m in (account, invoice)],
    "related": [account.entry_set.all().db, account.tags.all().db],
    "rows": {t: count(t) for t in ("ledger_account", "ledger_entry",
                                   "ledger_account_tags", "billing_invoice")},
}
written = {"ledger_account": Account, "billing_invoice": Invoice}
if real_default:
    from django.contrib.auth.models import User
    from tests.app_db.legacy.models import Memo

    memo = Memo.objects.create(text="m", author=User.objects.create(username="u"))
    placed["states"].append(memo._state.db)
    placed["rows"]["legacy_memo"] = count("legacy_memo")
    placed["rows"]["auth_user"] = count("auth_user")
    written["legacy_memo"] = Memo


class Boom(Exception):
    pass


def write_all():
    Account.objects.create(name="x")
    Invoice.objects.create(number="x")
    if real_default:
        Memo.objects.create(text="x")
    raise Boom


covered = {}
for opener in [*ALIASES, None]:
    before = {table: model.objects.count() for table, model in written.items()}
    try:
        with transaction.atomic(using=opener):
            write_all()
    except Boom:
        ran = "rolled back"
    except Exception as error:
        ran = type(error).__name__
    after = {table: model.objects.count() for table, model in written.items()}
    covered[str(opener)] = [ran, {t: after[t] - before[t] for t in written}]

raw = {
    "alias": connection.alias,
    "transaction": transaction.get_connection().alias,
    "cursor": outcome(lambda: connection.cursor().execute("select 1").fetchone()[0]),
    "tables": outcome(lambda: sorted(connection.introspection.table_names())),
    "on_commit": outcome(lambda: transaction.on_commit(lambda: None)),
}

# A relation Django refuses by asking the router: a billing model's foreign
# key to a ledger model, assigned on unsaved instances, so nothing is read.
class Card(models.Model):
    tag = models.ForeignKey(Tag, null=True, on_delete=models.CASCADE)

    class Meta:
        app_label = "billing"


def assign(field, value):
    card = Card()
    setattr(card, field, value)
    return card._state.db


descriptors = {
    "across": outcome(lambda: assign("tag", Tag())),
    "within": outcome(lambda: Entry(account=Account())._state.db),
}
if real_default:
    descriptors["default"] = outcome(lambda: Memo(author=User())._state.db)

print(json.dumps({
    "migrated": migrated, "schema": schema, "model_tables": model_tables,
    "routes": routes, "migrations": migrations, "relations": relations,
    "querysets": querysets, "placed": placed, "covered": covered, "raw": raw,
    "descriptors": descriptors,
}))
"""

_HOSTED = ["tests.app_db.ledger", "tests.app_db.billing", "tests.app_db.notes"]
_UNDECLARED = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "tests.app_db.legacy",
]


def _run(root: Path, *, real_default: bool) -> dict[str, Any]:
    def sqlite(name: str) -> dict[str, str]:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(root / name)}

    databases = {
        "default": sqlite("default.sqlite3") if real_default else {},
        "ledger": sqlite("ledger.sqlite3"),
        "billing": sqlite("billing.sqlite3"),
    }
    installed = [*(_UNDECLARED if real_default else []), *_HOSTED, "neolith"]
    body = _BODY.replace("LABELS = json.loads(sys.argv[3])", f"LABELS = {LABELS!r}")
    printed = in_fresh_process(
        "import sys\nimport itertools\n" + body,
        installed,
        {
            "DATABASES": databases,
            "DATABASE_ROUTERS": _ROUTER,
            "DEFAULT_AUTO_FIELD": "django.db.models.AutoField",
        },
    )
    found: dict[str, Any] = json.loads(printed.splitlines()[-1])
    return found


HOSTS = ["real default", "closed"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    return {
        host: _run(tmp_path_factory.mktemp(host.replace(" ", "_")), real_default=real)
        for host, real in (("real default", True), ("closed", False))
    }


@pytest.mark.spec("APP-18", "APP-19")
@pytest.mark.parametrize("host", HOSTS)
def test_a_read_or_a_write_goes_to_the_home_of_the_models_label(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    routes = ran[host]["routes"]
    # Every label was asked about, by the router and through Django, under
    # every hint, including the database cache's stand-in.
    assert {label for label, *_ in routes} >= {*LABELS}
    for label, hint, asker, read, write in routes:
        assert (read, write) == (home(label), home(label)), (label, hint, asker)


@pytest.mark.spec("APP-18")
def test_each_hosted_app_with_a_database_has_an_alias_apart_from_every_other() -> None:
    homes = [home(label) for label in HOSTED_WITH_DATABASE]
    assert len(set(homes)) == len(homes)
    assert "default" not in homes


@pytest.mark.spec("APP-23")
@pytest.mark.parametrize("host", HOSTS)
def test_migrate_is_allowed_only_on_the_home_of_the_label(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    migrations = ran[host]["migrations"]
    assert {label for _, label, *_ in migrations} == {*LABELS}
    for db, label, hints, asker, allowed in migrations:
        assert allowed is (db == home(label)), (db, label, hints, asker)


@pytest.mark.spec("APP-22")
@pytest.mark.parametrize("host", HOSTS)
def test_a_relation_is_allowed_only_within_one_home(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    relations = ran[host]["relations"]
    assert len({(first, second) for first, second, *_ in relations}) >= len(LABELS) ** 2
    for first, second, asker, allowed in relations:
        assert allowed is (home(first) == home(second)), (first, second, asker)


@pytest.mark.spec("APP-22")
@pytest.mark.parametrize("host", HOSTS)
def test_django_refuses_a_foreign_key_across_homes_and_admits_one_within(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    descriptors = ran[host]["descriptors"]
    assert descriptors["across"] == ["raised", "ValueError"]
    assert descriptors["within"] == ["ok", "ledger"]
    if host == "real default":
        assert descriptors["default"] == ["ok", "default"]


@pytest.mark.spec("APP-23")
@pytest.mark.parametrize("host", HOSTS)
def test_each_alias_holds_the_tables_of_the_labels_it_is_home_to(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    found = ran[host]
    for alias, tables in found["schema"].items():
        expected = {
            table for label, table in found["model_tables"] if home(label) == alias
        }
        assert set(tables) - {"django_migrations"} == expected, alias
    if host == "closed":
        # Django's dummy default refuses the migrate that names no alias.
        assert found["migrated"]["default"] == ["raised", "ImproperlyConfigured"]
        assert "default" not in found["schema"]
    for alias in HOSTED_WITH_DATABASE:
        assert found["migrated"][alias] == ["ok", None]


@pytest.mark.spec("APP-19")
@pytest.mark.parametrize("host", HOSTS)
def test_records_land_in_their_apps_own_database(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    placed = ran[host]["placed"]
    assert ran[host]["querysets"] == {
        "ledger.Account": "ledger",
        "ledger.Entry": "ledger",
        "ledger.Tag": "ledger",
        "billing.Invoice": "billing",
    }
    assert placed["fetched"] == ["ledger", "billing"]
    assert placed["related"] == ["ledger", "ledger"]
    rows = {
        "ledger_account": {"ledger": 1},
        "ledger_entry": {"ledger": 1},
        "ledger_account_tags": {"ledger": 1},
        "billing_invoice": {"billing": 1},
    }
    if host == "real default":
        assert placed["states"] == ["ledger", "ledger", "billing", "default"]
        rows |= {"legacy_memo": {"default": 1}, "auth_user": {"default": 1}}
    else:
        assert placed["states"] == ["ledger", "ledger", "billing"]
    assert placed["rows"] == rows


@pytest.mark.spec("APP-20")
@pytest.mark.parametrize("host", HOSTS)
def test_a_transaction_covers_only_the_writes_to_its_own_database(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    covered = ran[host]["covered"]
    tables = {"ledger_account": "ledger", "billing_invoice": "billing"}
    if host == "real default":
        tables["legacy_memo"] = "default"
    for opener in [
        *HOSTED_WITH_DATABASE,
        *(["default"] if host == "real default" else []),
    ]:
        ran_as, kept = covered[opener]
        assert ran_as == "rolled back"
        # Its own write is rolled back; every other database keeps its row.
        assert kept == {t: int(alias != opener) for t, alias in tables.items()}, opener


@pytest.mark.spec("APP-21")
@pytest.mark.parametrize("host", HOSTS)
def test_an_operation_that_names_no_database_reaches_no_apps_database(
    ran: dict[str, dict[str, Any]], host: str
) -> None:
    found = ran[host]
    raw = found["raw"]
    assert raw["alias"] == raw["transaction"] == "default"
    if host == "closed":
        # Django's dummy backend refuses each of them where it is made.
        for key in ("cursor", "tables", "on_commit"):
            assert raw[key] == ["raised", "ImproperlyConfigured"], key
        assert found["covered"]["None"][0] == "ImproperlyConfigured"
        return
    assert raw["cursor"] == ["ok", 1]
    assert raw["on_commit"] == ["ok", None]
    on_default = {
        table for label, table in found["model_tables"] if home(label) == "default"
    }
    assert set(raw["tables"][1]) - {"django_migrations"} == on_default
    # A bare transaction is default's: it rolls back the write there and
    # leaves the apps' own writes in place (ADR 0009 Decision 3, the cost).
    assert found["covered"]["None"] == [
        "rolled back",
        {"ledger_account": 1, "billing_invoice": 1, "legacy_memo": 0},
    ]


# The checks at set-up: plain functions over isolated registries.

# A hosted app without a database, one with a database, and an app with
# no declaration; none of them has a models module.
_ISOLATED = ("tests.app_db.notes", "tests.app_db.archive", "tests.plain_app")


def _model(
    label: str,
    name: str,
    *,
    fields: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    bases: tuple[type, ...] = (models.Model,),
) -> type[models.Model]:
    attrs: dict[str, Any] = {"__module__": __name__, **(fields or {})}
    attrs["Meta"] = type("Meta", (), {"app_label": label, **(meta or {})})
    made: type[models.Model] = type(name, bases, attrs)
    return made


@pytest.fixture
def isolated() -> Iterator[Apps]:
    with isolate_apps(*_ISOLATED) as registry:
        assert registry is not None
        yield registry


def _defines(
    shape: str,
) -> Callable[[str], None]:
    def define(label: str) -> None:
        if shape == "model":
            _model(label, "Thing")
        elif shape == "unmanaged":
            _model(label, "Thing", meta={"managed": False})
        elif shape == "proxy of another app's model":
            base = _model("archive", "Base")
            _model(label, "Thing", meta={"proxy": True}, bases=(base,))
        elif shape == "model defined in another module":
            _model(label, "Elsewhere")
        elif shape == "abstract only":
            _model(label, "Thing", meta={"abstract": True})
        else:
            assert shape == "nothing"

    return define


_MODELS_MESSAGE = "app {}: has models but declares no database resource"


@pytest.mark.spec("APP-24")
@pytest.mark.parametrize(
    "shape",
    [
        "nothing",
        "abstract only",
        "model",
        "unmanaged",
        "proxy of another app's model",
        "model defined in another module",
    ],
)
@pytest.mark.parametrize("label", ["notes", "archive", "plain_app"])
def test_an_app_with_models_and_no_database_is_refused(
    isolated: Apps, shape: str, label: str
) -> None:
    _defines(shape)(label)
    refused = label == "notes" and shape not in ("nothing", "abstract only")
    assert database_problems(isolated.get_app_configs()) == (
        [_MODELS_MESSAGE.format("notes")] if refused else []
    )


@pytest.mark.spec("APP-24")
def test_the_database_check_judges_the_registry_it_is_given() -> None:
    with isolate_apps("tests.app_db.notes") as registry:
        assert registry is not None
        _model("notes", "Thing")
        assert _checks.database_check(app_configs=registry.get_app_configs()) == [
            checks.Error(_MODELS_MESSAGE.format("notes"), id="neolith.E014")
        ]
    # The suite's own registry hosts nothing.
    assert _checks.database_check() == []
    assert _checks.table_name_check() == []


_SHORT = (
    "app {}: the table name of {} was shortened to fit the default database; "
    "set db_table"
)


def _limited(limit: int | None) -> "mock._patch[mock.Mock]":
    """Give ``default`` a limit on names while models are built."""
    return mock.patch.object(connection.ops, "max_name_length", return_value=limit)


# Each row: what the hosted app defines, under a default whose limit is 10,
# and the names refused. "archive_" is 8 characters.
_TABLES: list[tuple[str, Callable[[str], object], list[str]]] = [
    ("short name", lambda label: _model(label, "A"), []),
    ("name at the limit", lambda label: _model(label, "Ab"), []),
    ("name one past the limit", lambda label: _model(label, "Abc"), ["Abc"]),
    ("long name", lambda label: _model(label, "Long" * 20), ["Long" * 20]),
    (
        "long name with db_table",
        lambda label: _model(label, "Long" * 20, meta={"db_table": "set_" + "x" * 80}),
        [],
    ),
    (
        "long name with an empty db_table",
        lambda label: _model(label, "Long" * 20, meta={"db_table": ""}),
        ["Long" * 20],
    ),
    (
        "unmanaged long name",
        lambda label: _model(label, "Long" * 20, meta={"managed": False}),
        ["Long" * 20],
    ),
    (
        "proxy of a long name",
        lambda label: _model(
            label, "Proxy", meta={"proxy": True}, bases=(_model(label, "Long" * 20),)
        ),
        ["Long" * 20],
    ),
    ("db_table inherited from an abstract base", lambda label: _child(label, True), []),
    (
        "db_table not inherited from an abstract base",
        lambda label: _child(label, False),
        ["Child" * 10],
    ),
    (
        "many-to-many with a long default name",
        lambda label: _model(
            label, "Ab", fields={"links": models.ManyToManyField(_model(label, "Ac"))}
        ),
        ["Ab.links"],
    ),
    (
        "many-to-many with db_table",
        lambda label: _model(
            label,
            "Ab",
            fields={
                "links": models.ManyToManyField(
                    _model(label, "Ac"), db_table="set_" + "z" * 80
                )
            },
        ),
        [],
    ),
    (
        "many-to-many on a model with db_table",
        lambda label: _model(
            label,
            "Ab",
            meta={"db_table": "t"},
            fields={"links": models.ManyToManyField(_model(label, "Ac"))},
        ),
        [],
    ),
    (
        "many-to-many through a model of the app",
        lambda label: _through(label),
        ["Membership" * 3],
    ),
]


def _child(label: str, inherits: bool) -> object:
    base = _model(label, "Base", meta={"abstract": True, "db_table": "set_" + "y" * 80})
    bases = (base.Meta,) if inherits else ()  # type: ignore[attr-defined]
    meta = type("Meta", bases, {"app_label": label})
    return type("Child" * 10, (base,), {"__module__": __name__, "Meta": meta})


def _through(label: str) -> object:
    group = _model(label, "Ab")
    member = _model(label, "Ac")
    through = _model(
        label,
        "Membership" * 3,
        fields={
            "group": models.ForeignKey(group, on_delete=models.CASCADE),
            "member": models.ForeignKey(member, on_delete=models.CASCADE),
        },
    )
    return _model(
        label,
        "Ad",
        fields={"members": models.ManyToManyField(member, through=through)},
    )


@pytest.mark.spec("APP-25")
@pytest.mark.parametrize(
    ("case", "define", "shortened"), _TABLES, ids=[r[0] for r in _TABLES]
)
@pytest.mark.parametrize("label", ["archive", "plain_app"])
def test_a_table_name_the_default_database_shortened_is_refused(
    isolated: Apps,
    case: str,
    define: Callable[[str], object],
    shortened: list[str],
    label: str,
) -> None:
    with _limited(10):
        define(label)
    expected = [_SHORT.format("archive", name) for name in shortened]
    # An app with no declaration lives on default, whose limit it shares.
    assert table_name_problems(isolated.get_app_configs()) == (
        sorted(expected) if label == "archive" else []
    )


@pytest.mark.spec("APP-25")
@pytest.mark.parametrize(
    ("case", "define", "shortened"), _TABLES, ids=[r[0] for r in _TABLES]
)
def test_no_table_name_is_refused_under_a_default_with_no_limit(
    isolated: Apps, case: str, define: Callable[[str], object], shortened: list[str]
) -> None:
    with _limited(None):
        define("archive")
    assert table_name_problems(isolated.get_app_configs()) == []


def _names(registry: Apps) -> dict[str, str]:
    return {
        model._meta.label: model._meta.db_table
        for model in registry.get_app_config("archive").get_models(
            include_auto_created=True
        )
    }


_IDENTIFIER = st.from_regex(r"[A-Z][a-z0-9]{0,30}", fullmatch=True).filter(
    lambda name: name != "Target"
)


@pytest.mark.spec("APP-25")
@settings(max_examples=60, deadline=None)
@given(
    name=_IDENTIFIER,
    field=st.from_regex(r"[a-z][a-z0-9_]{0,30}", fullmatch=True).filter(
        lambda f: (
            not f.endswith("_") and "__" not in f and f not in ("id", "pk", "objects")
        )
    ),
    db_table=st.one_of(st.none(), st.from_regex(r"[a-z]{1,40}", fullmatch=True)),
    limit=st.sampled_from([10, 16, 30, 63]),
)
def test_an_app_that_passes_has_the_same_table_names_under_every_default(
    name: str, field: str, db_table: str | None, limit: int
) -> None:
    """The equation of ADR 0009 Decision 8: the check passes under a limit
    exactly when every table name equals the one a default with no limit
    gives, so an app that starts has one set of names in every host.
    """

    def define() -> None:
        target = _model("archive", "Target")
        meta = {} if db_table is None else {"db_table": db_table}
        _model(
            "archive",
            name,
            meta=meta,
            fields={field: models.ManyToManyField(target, related_name="+")},
        )

    with isolate_apps("tests.app_db.archive") as unlimited:
        assert unlimited is not None
        with _limited(None):
            define()
        whole = _names(unlimited)
        assert table_name_problems(unlimited.get_app_configs()) == []
    with isolate_apps("tests.app_db.archive") as limited:
        assert limited is not None
        with _limited(limit):
            define()
        passes = table_name_problems(limited.get_app_configs()) == []
        assert passes is (_names(limited) == whole)


# The checks through set-up: each refusal stops the host.

_SET_UP = """
import django
from django.core.exceptions import ImproperlyConfigured

try:
    django.setup()
except ImproperlyConfigured as error:
    print(str(error))
else:
    print("set up")
"""


@pytest.mark.spec("APP-24", "APP-25")
def test_set_up_stops_the_host_on_each_check(tmp_path: Path) -> None:
    printed = in_fresh_process(
        _SET_UP,
        [*_UNDECLARED, "tests.app_db.ledger", "tests.app_db.stray", "neolith"],
        {
            "DATABASES": {
                "default": {
                    "ENGINE": "tests.app_db.short_names",
                    "NAME": str(tmp_path / "default.sqlite3"),
                },
            },
            "DATABASE_ROUTERS": _ROUTER,
            "DEFAULT_AUTO_FIELD": "django.db.models.AutoField",
        },
    )
    assert printed.splitlines() == [
        "host: the settings are refused before the host serves:",
        "(neolith.E014) " + _MODELS_MESSAGE.format("stray"),
        "(neolith.E017) " + _SHORT.format("ledger", "Account"),
        "(neolith.E017) " + _SHORT.format("ledger", "Account.tags"),
        "(neolith.E017) " + _SHORT.format("ledger", "Entry"),
    ]
    # Nothing was opened: set-up reads no database.
    assert not (tmp_path / "default.sqlite3").exists()


def test_the_same_apps_set_up_under_a_default_with_no_limit(tmp_path: Path) -> None:
    printed = in_fresh_process(
        _SET_UP,
        [*_UNDECLARED, "tests.app_db.ledger", "neolith"],
        {
            "DATABASES": {
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": str(tmp_path / "default.sqlite3"),
                },
            },
            "DATABASE_ROUTERS": _ROUTER,
            "DEFAULT_AUTO_FIELD": "django.db.models.AutoField",
        },
    )
    assert printed.splitlines() == ["set up"]


def test_matrix_labels_cover_every_owner() -> None:
    owners = {
        "hosted with a database": {"ledger", "billing"},
        "hosted without one": {"notes"},
        "neolith's own app": {"neolith"},
        "no declaration": {"legacy"},
        "contrib": {"auth", "contenttypes"},
        "no installed app": {"django_cache", "unknown"},
    }
    assert set(itertools.chain.from_iterable(owners.values())) == set(LABELS)
