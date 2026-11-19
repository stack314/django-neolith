"""The host's settings: built from the declarations and the deployer's input
(spec 05, Configure; ADR 0022 Decisions 1 to 5; ADR 0009).

The rows of the builder's table are tested through the plain function
behind ``neolith.host.build_settings``, ``neolith._configure.configure``,
which records nothing, so one refused example leaves nothing behind for the
next. The entry itself, which records its first refusal and puts Django's
settings back to unconfigured, is tested through a settings module and
``manage.py``, each in a process of its own, since an import, the first
set-up and the recorded refusal happen once per process.

The checks of set-up (``neolith.E002`` to ``E005``, ``E015``, ``E016``) are
tested through their plain functions on built registries, and once each
through a set-up that refuses; the ``setting_changed`` receiver through
``override_settings`` on the suite's own host.
"""

import copy
import json
import os
import subprocess
import sys
import textwrap
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.apps import AppConfig
from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st

from neolith import DeclarationError, _checks, _host
from neolith._configure import configure
from neolith._declaration import declaration_of
from tests import host_apps
from tests.hosting import NEOLITH_KEYS, in_built_host, in_fresh_process
from tests.test_host import WriteApp
from tests.test_host import write_app as write_app

pytestmark = pytest.mark.spec("APP-16")

ROOT = "neolith.middleware.RootMiddleware"
PRINCIPAL = "neolith.middleware.PrincipalMiddleware"
SQLITE = "django.db.backends.sqlite3"
HOSTED = list(host_apps.HOSTED)
LEDGER = "tests.app_db.ledger"
BILLING = "tests.app_db.billing"
KEYS = NEOLITH_KEYS
UNDECLARED = 'NEOLITH["UNDECLARED_APPS"]'
DBS = 'NEOLITH["DATABASES"]'


def deployer(**changed: object) -> dict[str, Any]:
    """The smallest deployer's mapping the builder admits, changed."""
    base: dict[str, Any] = {"MIDDLEWARE": [ROOT, PRINCIPAL], "NEOLITH": {}}
    base.update(changed)
    return base


def sqlite(name: object) -> dict[str, object]:
    return {"ENGINE": SQLITE, "NAME": name}


def _with_key(key: object) -> dict[object, object]:
    given: dict[object, object] = {}
    given.update(deployer().items())
    given[key] = 2
    return given


def refusal(apps: object, given: object) -> str:
    with pytest.raises(ImproperlyConfigured) as raised:
        configure(apps, given)
    return str(raised.value)


def _declared(write_app: WriteApp, **fields: str) -> str:
    """A declared app with the class-body lines ``fields`` gives, written by
    the test; ``{package}`` stands for its package."""
    lines = [
        "from neolith import NeolithAppConfig",
        "",
        "",
        "class Config(NeolithAppConfig):",
        '    name = "{package}"',
        f"    label = {fields.pop('label', repr('{package}'))}",
        *(f"    {name} = {value}" for name, value in fields.items()),
    ]
    return write_app("\n".join(lines) + "\n")


# The builder's table: one refused value per row, and beside each the
# nearest input that is admitted.


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        (tuple(HOSTED), "host: apps: expected a list, got tuple"),
        ([], "host: no apps named"),
        ([*HOSTED, 1], f"host: apps: entry {len(HOSTED)}: expected a str, got int"),
        (
            [*HOSTED, "tests.host_apps.web"],
            "host: app tests.host_apps.web named twice",
        ),
        (
            [*HOSTED, "tests.plain_app"],
            "app tests.plain_app: has no declaration",
        ),
        (
            ["tests.host_apps.stock.apps.StockConfig", *HOSTED[1:]],
            "app tests.host_apps.stock.apps.StockConfig: name the app's package, "
            "not its config class",
        ),
    ],
)
def test_the_list_of_apps_has_one_form(apps: object, message: str) -> None:
    assert refusal(apps, deployer()) == message
    # The nearest admitted list.
    assert configure(HOSTED, deployer())["INSTALLED_APPS"][: len(HOSTED)] == HOSTED


def test_every_refusal_found_by_reading_the_apps_is_reported_together() -> None:
    """Only an app that cannot be read stops the walk; what is found by
    reading the others is reported with it in one diagnostic."""
    apps = [
        "django.contrib.humanize",
        "tests.host_apps.stock.apps.StockConfig",
        "django.contrib.sitemaps",
        *HOSTED[1:],
    ]
    assert refusal(apps, deployer()).splitlines() == [
        "app django.contrib.humanize: has no declaration",
        "app django.contrib.sitemaps: has no declaration",
        "app tests.host_apps.stock.apps.StockConfig: name the app's package, not "
        "its config class",
    ]


def test_refusals_of_reading_and_of_comparing_are_reported_together(
    write_app: WriteApp,
) -> None:
    first = _declared(write_app, label='"twin"')
    second = _declared(write_app, label='"twin"')
    found = refusal([*HOSTED, "django.contrib.humanize", first, second], deployer())
    assert found.splitlines() == [
        "app django.contrib.humanize: has no declaration",
        f"apps {first} and {second}: both have the label twin",
    ]


def test_an_app_that_cannot_be_read_is_refused_with_its_error() -> None:
    message = refusal([*HOSTED, "tests.no_such_app"], deployer())
    assert message.startswith(
        "app tests.no_such_app: cannot be read: ModuleNotFoundError: "
    )


def test_a_refused_declaration_passes_through_as_it_is(write_app: WriteApp) -> None:
    bad = _declared(write_app, resources='["database"]')
    with pytest.raises(DeclarationError) as raised:
        configure([*HOSTED, bad], deployer())
    assert "resources" in str(raised.value)


def test_a_declaration_that_names_another_package_is_refused(
    write_app: WriteApp,
) -> None:
    other = write_app(
        "from neolith import NeolithAppConfig\n\n\n"
        "class Config(NeolithAppConfig):\n"
        '    name = "tests.app_db.notes"\n'
        '    label = "notes"\n'
    )
    assert refusal([*HOSTED, other], deployer()) == (
        f"app {other}: its declaration names tests.app_db.notes; name the "
        "package the declaration names"
    )


def test_two_apps_with_one_label_are_refused(write_app: WriteApp) -> None:
    first = _declared(write_app, label='"twin"')
    second = _declared(write_app, label='"twin"')
    assert refusal([*HOSTED, first, second], deployer()) == (
        f"apps {first} and {second}: both have the label twin"
    )


@pytest.mark.parametrize("label", ["default", "django_cache"])
def test_a_label_the_host_reserves_is_refused(write_app: WriteApp, label: str) -> None:
    app = _declared(write_app, label=repr(label))
    assert refusal([*HOSTED, app], deployer()) == (
        f'app {label}: "{label}" is reserved for the host'
    )
    # A label beside them is admitted.
    near = _declared(write_app, label=repr(f"{label}s"))
    assert near in configure([*HOSTED, near], deployer())["INSTALLED_APPS"]


def test_a_battery_that_is_a_hosted_app_or_neolith_is_refused(
    write_app: WriteApp,
) -> None:
    app = _declared(write_app, batteries='("tests.host_apps.couriers", "neolith")')
    assert refusal([*HOSTED, app], deployer()).splitlines() == [
        f"app {app}: batteries: neolith is installed by the host",
        f"app {app}: batteries: tests.host_apps.couriers is a hosted app; a hosted "
        "app is reached through a contract",
    ]


def test_resolve_runs_in_the_builder() -> None:
    with pytest.raises(DeclarationError) as raised:
        configure(["tests.host_apps.web"], deployer())
    assert "requires: couriers.v3 has no provider" in str(raised.value)


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ([("NEOLITH", {})], "settings: expected a dict of setting names, got list"),
        (_with_key(1), "settings: a setting name is a str, got int"),
    ],
)
def test_the_deployer_s_mapping_is_a_dict_of_names(given: object, message: str) -> None:
    assert refusal(HOSTED, given) == message


def test_a_name_that_is_not_a_str_is_reported_with_the_other_refusals() -> None:
    given = _with_key(1)
    given["INSTALLED_APPS"] = []
    assert refusal(HOSTED, given).splitlines() == [
        'settings: INSTALLED_APPS is built by the host; see NEOLITH["UNDECLARED_APPS"]',
        "settings: a setting name is a str, got int",
    ]


def test_names_that_are_not_upper_case_are_ignored() -> None:
    built = configure(
        HOSTED, deployer(lower=1, Mixed=2, __name__="x", ALLOWED_HOSTS=[])
    )
    assert "lower" not in built
    assert "Mixed" not in built
    assert "__name__" not in built
    assert built["ALLOWED_HOSTS"] == []


@pytest.mark.parametrize(
    ("name", "instead"),
    [
        ("INSTALLED_APPS", 'NEOLITH["UNDECLARED_APPS"]'),
        ("DATABASES", 'NEOLITH["DATABASES"]'),
        ("DATABASE_ROUTERS", 'NEOLITH["DATABASES"]'),
    ],
)
@pytest.mark.spec("APP-28")
def test_a_setting_the_host_builds_is_refused_in_the_deployer_s_input(
    name: str, instead: str
) -> None:
    assert refusal(HOSTED, deployer(**{name: []})) == (
        f"settings: {name} is built by the host; see {instead}"
    )


@pytest.mark.parametrize(
    ("neolith", "message"),
    [
        (None, "settings: NEOLITH is required; it is {} when nothing is set"),
        ([], "NEOLITH: expected a dict, got list"),
        ({"OTHER": 1}, f"NEOLITH: unknown key 'OTHER'; {KEYS}"),
        (
            {"PRINCIPAL_BACKENDS": ["x"]},
            'NEOLITH["PRINCIPAL_BACKENDS"][0]: expected a dict with exactly the '
            "keys BACKEND and OPTIONS, got str",
        ),
        ({"DATABASES": {}}, f"{DBS}: is empty; leave the key out"),
        ({"UNDECLARED_APPS": []}, f"{UNDECLARED}: is empty; leave the key out"),
        (
            {"UNDECLARED_APPS": ("a",)},
            f"{UNDECLARED}: expected a list of str, got tuple",
        ),
        ({"UNDECLARED_APPS": [1]}, f"{UNDECLARED}: entry 0: expected a str, got int"),
        (
            {"UNDECLARED_APPS": ["django.contrib.humanize"] * 2},
            f"{UNDECLARED}: django.contrib.humanize listed twice",
        ),
        (
            {"UNDECLARED_APPS": ["tests.host_apps.web"]},
            f"{UNDECLARED}: tests.host_apps.web is a hosted app",
        ),
        (
            {"UNDECLARED_APPS": ["neolith"]},
            f"{UNDECLARED}: neolith is installed by the host",
        ),
        (
            {"REQUEST_DEADLINE": 0},
            'NEOLITH["REQUEST_DEADLINE"]: 0 is outside 0.001 to 9999999.999 seconds',
        ),
    ],
)
@pytest.mark.spec("APP-16")
def test_neolith_has_one_form_in_the_builder(neolith: object, message: str) -> None:
    given = deployer() if neolith is None else deployer(NEOLITH=neolith)
    if neolith is None:
        del given["NEOLITH"]
    assert refusal(HOSTED, given) == message


def test_an_undeclared_app_that_a_hosted_app_names_as_a_battery_is_refused(
    write_app: WriteApp,
) -> None:
    app = _declared(write_app, batteries='("django.contrib.humanize",)')
    given = deployer(NEOLITH={"UNDECLARED_APPS": ["django.contrib.humanize"]})
    assert refusal([*HOSTED, app], given) == (
        f"{UNDECLARED}: django.contrib.humanize is installed as a battery of {app}"
    )
    # Written another way, the entry is Django's to load, and set-up's
    # neolith.E003 judges what it loads.
    given = deployer(
        NEOLITH={"UNDECLARED_APPS": ["django.contrib.humanize.apps.HumanizeConfig"]}
    )
    assert configure([*HOSTED, app], given)["INSTALLED_APPS"][-3:] == [
        "django.contrib.humanize.apps.HumanizeConfig",
        "django.contrib.humanize",
        "neolith",
    ]


@pytest.mark.parametrize(
    ("middleware", "message"),
    [
        (None, f"MIDDLEWARE: is required; its first entry is {ROOT}"),
        ((ROOT,), "MIDDLEWARE: expected a list, got tuple"),
        (["x.Y", ROOT, PRINCIPAL], f"MIDDLEWARE: {ROOT} must be the first entry"),
        ([ROOT, ROOT, PRINCIPAL], f"MIDDLEWARE: {ROOT} is listed 2 times"),
    ],
)
def test_middleware_has_the_root_first_once_in_the_builder(
    middleware: object, message: str
) -> None:
    given = deployer(MIDDLEWARE=middleware)
    if middleware is None:
        del given["MIDDLEWARE"]
    assert refusal(HOSTED, given) == message


def test_the_builder_imports_no_middleware() -> None:
    """Before settings exist, an alias of the root middleware is not
    imported; set-up judges by class (``neolith.E006``)."""
    given = [ROOT, PRINCIPAL, "tests.request_site.Alias"]
    built = configure(HOSTED, deployer(MIDDLEWARE=given))
    assert built["MIDDLEWARE"] == given
    assert _checks.middleware_problems(built["MIDDLEWARE"]) == [
        f"MIDDLEWARE: {ROOT} is listed 2 times"
    ]


@pytest.mark.spec("APP-30")
@pytest.mark.parametrize(
    ("silenced", "message"),
    [
        (("neolith.W001",), "SILENCED_SYSTEM_CHECKS: expected a list, got tuple"),
        ([1], "SILENCED_SYSTEM_CHECKS: entry 0: expected a str, got int"),
        (
            ["neolith.E001"],
            "SILENCED_SYSTEM_CHECKS: 'neolith.E001' is a host error and cannot be "
            "silenced",
        ),
        (
            ["neolith.E009"],
            "SILENCED_SYSTEM_CHECKS: 'neolith.E009' is a host error and cannot be "
            "silenced",
        ),
    ],
)
def test_a_host_error_cannot_be_silenced(silenced: object, message: str) -> None:
    assert refusal(HOSTED, deployer(SILENCED_SYSTEM_CHECKS=silenced)) == message
    # A warning, and another app's error, may be silenced.
    near = ["neolith.W001", "admin.E408"]
    assert (
        configure(HOSTED, deployer(SILENCED_SYSTEM_CHECKS=near))[
            "SILENCED_SYSTEM_CHECKS"
        ]
        == near
    )


# NEOLITH["DATABASES"]: ADR 0009's rows before Django imports a model.


def _databases(**aliases: object) -> dict[str, Any]:
    return deployer(NEOLITH={"DATABASES": aliases})


def _db_refusal(**aliases: object) -> list[str]:
    return refusal([*HOSTED, LEDGER, BILLING], _databases(**aliases)).splitlines()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"notes": sqlite("n")}, "database notes: no hosted app declares it"),
        ({"billing": None}, "app billing: no connection settings for its database"),
        (
            {"billing": [("ENGINE", SQLITE)]},
            "database billing: expected a dict, got list",
        ),
        ({"billing": {"NAME": "b"}}, "database billing: ENGINE is required"),
        (
            {"billing": {"ENGINE": "django.db.backends.dummy", "NAME": "b"}},
            "database billing: ENGINE is required; the dummy backend is not a database",
        ),
        (
            {"billing": {"ENGINE": SQLITE}},
            "database billing: NAME is required; a service or option file alone is "
            "not admitted yet",
        ),
        (
            {"billing": sqlite(b"b")},
            "database billing: NAME: expected a str or a path, got bytes",
        ),
        (
            {"billing": {**sqlite("b"), "ATOMIC_REQUESTS": False}},
            "database billing: ATOMIC_REQUESTS would wrap other apps' views in this "
            "app's transaction",
        ),
        (
            {"billing": {**sqlite("b"), "TEST": []}},
            "database billing: TEST: expected a dict, got list",
        ),
        (
            {"billing": {**sqlite("b"), "TEST": {"MIRROR": None}}},
            "database billing: TEST.MIRROR joins two aliases into one connection in "
            "tests",
        ),
        (
            {"default": {**sqlite("d"), "TEST": {"MIRROR": "billing"}}},
            "database default: TEST.MIRROR joins two aliases into one connection in "
            "tests",
        ),
        (
            {"billing": {**sqlite("b"), "TEST": {"DEPENDENCIES": []}}},
            "database billing: TEST.DEPENDENCIES is set by the host",
        ),
        (
            {"billing": {**sqlite("b"), "TEST": {"NAME": "t"}}},
            "database billing: TEST.NAME is not admitted yet; Django names the test "
            "database from NAME",
        ),
    ],
)
def test_each_database_row_refuses_by_name(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    aliases: dict[str, object] = {
        "ledger": sqlite(str(tmp_path / "l")),
        "billing": sqlite(str(tmp_path / "b")),
    }
    aliases.update(change)
    aliases = {alias: entry for alias, entry in aliases.items() if entry is not None}
    assert _db_refusal(**aliases) == [message]


@pytest.mark.parametrize(
    "neolith", [{}, {"REQUEST_DEADLINE": 5}], ids=["empty", "other keys"]
)
def test_an_app_with_a_database_needs_its_connection_settings(
    neolith: dict[str, object],
) -> None:
    """Also when NEOLITH has no DATABASES key at all."""
    found = refusal([*HOSTED, LEDGER, BILLING], deployer(NEOLITH=neolith))
    assert found.splitlines() == [
        "app billing: no connection settings for its database",
        "app ledger: no connection settings for its database",
    ]


def test_a_non_str_alias_is_refused() -> None:
    given = deployer(NEOLITH={"DATABASES": {1: sqlite("x")}})
    assert refusal(HOSTED, given) == f"{DBS}: an alias is a str, got int"


@pytest.mark.parametrize(
    "entry",
    [
        {**sqlite("d"), "ATOMIC_REQUESTS": True},
        {**sqlite("d"), "TEST": {"DEPENDENCIES": [], "CHARSET": "utf8"}},
        {"ENGINE": "django.db.backends.postgresql", "NAME": "d", "HOST": "h"},
    ],
    ids=["atomic requests on default", "test settings on default", "a server"],
)
def test_the_nearest_admitted_database_settings_build(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    given = _databases(
        default=entry,
        ledger=sqlite(tmp_path / "l"),
        billing={**sqlite(str(tmp_path / "b")), "TEST": {"CHARSET": "utf8"}},
    )
    built = configure([*HOSTED, LEDGER, BILLING], given)["DATABASES"]
    assert list(built) == ["default", "ledger", "billing"]


def _pair(tmp_path: Path) -> list[tuple[str, object, object]]:
    target = tmp_path / "one.sqlite3"
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    relative = os.path.relpath(target)
    return [
        ("the same path", str(target), str(target)),
        ("a str and a path", str(target), target),
        ("a symbolic link", str(target), str(link)),
        ("a relative path", str(target), relative),
        ("a file URI", str(target), f"file:{target}"),
        ("a file URI with a query", str(target), f"file:{target}?mode=ro"),
        (
            "a percent-encoded URI",
            str(target),
            f"file:{str(target).replace('/', '%2F')}",
        ),
        ("a URI with an authority", str(target), f"file://localhost{target}"),
        (
            "a shared in-memory name",
            "file:x?mode=memory&cache=shared",
            "file:x?mode=memory&cache=shared",
        ),
        ("two private temporary databases", "file:", "file:"),
        ("an in-memory URI beside its file", str(target), f"file:{target}?mode=memory"),
    ]


def test_two_aliases_on_one_database_are_refused(tmp_path: Path) -> None:
    for case, first, second in _pair(tmp_path):
        aliases = {"ledger": sqlite(first), "billing": sqlite(second)}
        found = _db_refusal(**aliases)
        assert len(found) == 1, case
        assert found[0].startswith(
            f"databases ledger and billing: both name {SQLITE} "
        ), case


def test_two_server_aliases_on_one_database_are_refused() -> None:
    server = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "n",
        "HOST": "h",
        "PORT": "5",
    }
    assert _db_refusal(ledger=server, billing=dict(server)) == [
        "databases ledger and billing: both name django.db.backends.postgresql n on h:5"
    ]
    # Another NAME on the same server is another database.
    assert configure(
        [*HOSTED, LEDGER, BILLING],
        _databases(ledger=server, billing={**server, "NAME": "m"}),
    )


@pytest.mark.parametrize("name", [":memory:", Path(":memory:")], ids=["str", "path"])
def test_the_literal_memory_name_is_exempt_from_the_pair_check(name: object) -> None:
    built = configure(
        [*HOSTED, LEDGER, BILLING],
        _databases(ledger=sqlite(name), billing=sqlite(name)),
    )
    assert built["DATABASES"]["ledger"]["NAME"] == name


def test_default_is_compared_with_the_app_aliases(tmp_path: Path) -> None:
    path = str(tmp_path / "x")
    assert _db_refusal(
        default=sqlite(path), ledger=sqlite(path), billing=sqlite(path + "b")
    ) == [f"databases default and ledger: both name {SQLITE} {os.path.realpath(path)}"]


def test_every_database_refusal_reaches_the_diagnostic(tmp_path: Path) -> None:
    """One refusal per alias hides none behind it."""
    found = _db_refusal(
        default={"ENGINE": SQLITE},
        ledger={
            **sqlite(str(tmp_path / "l")),
            "ATOMIC_REQUESTS": True,
            "TEST": {"NAME": "t"},
        },
        notes=sqlite("n"),
    )
    assert found == sorted(
        [
            "app billing: no connection settings for its database",
            "database default: NAME is required; a service or option file alone is "
            "not admitted yet",
            "database ledger: ATOMIC_REQUESTS would wrap other apps' views in this "
            "app's transaction",
            "database ledger: TEST.NAME is not admitted yet; Django names the test "
            "database from NAME",
            "database notes: no hosted app declares it",
        ]
    )


def test_every_row_of_one_phase_reaches_the_diagnostic() -> None:
    found = refusal(
        HOSTED,
        {
            "INSTALLED_APPS": [],
            "NEOLITH": {"OTHER": 1, "REQUEST_DEADLINE": True},
            "MIDDLEWARE": [],
            "SILENCED_SYSTEM_CHECKS": ["neolith.E006"],
        },
    ).splitlines()
    # MIDDLEWARE has neither middleware class: two rows of one rule.
    assert len(found) == 6
    assert found == sorted(found)


# What the builder gives.


@pytest.mark.spec("APP-27")
def test_the_built_settings_follow_the_declarations(
    write_app: WriteApp, tmp_path: Path
) -> None:
    app = _declared(
        write_app, batteries='("django.contrib.humanize", "django.contrib.sitemaps")'
    )
    other = _declared(write_app, batteries='("django.contrib.sitemaps",)')
    neolith = {
        "DATABASES": {
            "default": sqlite(str(tmp_path / "d")),
            "ledger": {**sqlite(str(tmp_path / "l")), "TEST": {"CHARSET": "utf8"}},
        },
        "UNDECLARED_APPS": ["django.contrib.contenttypes", "tests.plain_app"],
        "REQUEST_DEADLINE": 5,
    }
    given = deployer(NEOLITH=neolith, TIME_ZONE="UTC")
    built = configure([*HOSTED, LEDGER, app, other], given)
    assert built["INSTALLED_APPS"] == [
        *HOSTED,
        LEDGER,
        app,
        other,
        "django.contrib.contenttypes",
        "tests.plain_app",
        "django.contrib.humanize",
        "django.contrib.sitemaps",
        "neolith",
    ]
    assert built["DATABASES"] == {
        "default": sqlite(str(tmp_path / "d")),
        "ledger": {
            **sqlite(str(tmp_path / "l")),
            "TEST": {"CHARSET": "utf8", "DEPENDENCIES": []},
        },
    }
    assert built["DATABASE_ROUTERS"] == ["neolith.routers.AppRouter"]
    # NEOLITH is the deployer's, unchanged; the aliases are copies of it.
    assert built["NEOLITH"] is neolith
    assert (
        built["DATABASES"]["ledger"]["TEST"]
        is not neolith["DATABASES"]["ledger"]["TEST"]
    )
    assert built["DATABASES"]["default"] is not neolith["DATABASES"]["default"]
    assert built["TIME_ZONE"] == "UTC"


@pytest.mark.spec("APP-27")
def test_a_closed_host_gets_an_empty_default() -> None:
    built = configure(HOSTED, deployer())
    assert built["DATABASES"] == {"default": {}}
    assert built["INSTALLED_APPS"] == [*HOSTED, "neolith"]


# Hypothesis: a mapping drawn from the admitted set builds, and one change
# outside it is refused by name.

_POOL = [*HOSTED, LEDGER, BILLING, "tests.app_db.notes"]
_UNDECLARED_POOL = [
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "tests.plain_app",
]
_MUTATIONS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "installed apps": (
        lambda d: d.update(INSTALLED_APPS=[]),
        "settings: INSTALLED_APPS",
    ),
    "routers": (lambda d: d.update(DATABASE_ROUTERS=[]), "settings: DATABASE_ROUTERS"),
    "unknown key": (lambda d: d["NEOLITH"].update(OTHER=1), "NEOLITH: unknown key"),
    "deadline": (
        lambda d: d["NEOLITH"].update(REQUEST_DEADLINE=True),
        'NEOLITH["REQUEST_DEADLINE"]',
    ),
    "silenced": (
        lambda d: d.update(SILENCED_SYSTEM_CHECKS=["neolith.E001"]),
        "SILENCED_SYSTEM_CHECKS",
    ),
    "middleware": (lambda d: d.update(MIDDLEWARE=["x.Y"]), "MIDDLEWARE"),
    "undeclared neolith": (
        lambda d: d["NEOLITH"].update(UNDECLARED_APPS=["neolith"]),
        UNDECLARED,
    ),
    "test mirror": (
        lambda d: (
            d["NEOLITH"]
            .setdefault("DATABASES", {})
            .update(default={**sqlite(":memory:"), "TEST": {"MIRROR": None}})
        ),
        "database default: TEST.MIRROR",
    ),
}


@st.composite
def _admitted(draw: st.DrawFn) -> tuple[list[str], dict[str, Any]]:
    apps = [
        *HOSTED,
        *draw(st.lists(st.sampled_from(_POOL[len(HOSTED) :]), unique=True)),
    ]
    neolith: dict[str, Any] = {}
    databases: dict[str, Any] = {
        label: sqlite(draw(st.sampled_from([":memory:", f"/{label}.sqlite3"])))
        for label in ("ledger", "billing")
        if f"tests.app_db.{label}" in apps
    }
    if draw(st.booleans()):
        databases["default"] = sqlite(":memory:")
    if databases:
        neolith["DATABASES"] = databases
    undeclared = draw(st.lists(st.sampled_from(_UNDECLARED_POOL), unique=True))
    if undeclared:
        neolith["UNDECLARED_APPS"] = undeclared
    deadline = draw(st.one_of(st.none(), st.just("absent"), st.integers(1, 100)))
    if deadline != "absent":
        neolith["REQUEST_DEADLINE"] = deadline
    given = deployer(NEOLITH=neolith)
    given.update(
        draw(
            st.dictionaries(
                st.sampled_from(["DEBUG", "USE_TZ", "X_ONE"]), st.booleans()
            )
        )
    )
    if draw(st.booleans()):
        given["SILENCED_SYSTEM_CHECKS"] = ["neolith.W001"]
    return apps, given


@pytest.mark.spec("APP-16", "APP-27", "APP-28")
@given(_admitted(), st.sampled_from(sorted(_MUTATIONS)))
def test_a_drawn_admitted_mapping_builds_and_one_change_outside_it_is_refused(
    drawn: tuple[list[str], dict[str, Any]], mutation: str
) -> None:
    apps, given = drawn
    built = configure(apps, copy.deepcopy(given))
    installed = built["INSTALLED_APPS"]
    assert installed[: len(apps)] == apps
    assert installed[-1] == "neolith"
    assert len(set(installed)) == len(installed)
    with_database = [a.rpartition(".")[2] for a in apps if a in (LEDGER, BILLING)]
    assert list(built["DATABASES"]) == ["default", *with_database]
    for name, value in given.items():
        if name != "NEOLITH":
            assert built[name] == value
    change, named = _MUTATIONS[mutation]
    changed = copy.deepcopy(given)
    change(changed)
    message = refusal(apps, changed)
    assert any(line.startswith(named) for line in message.splitlines()), message


# Set-up: the checks of the installed apps against NEOLITH, as plain
# functions over built registries.


def _configs(*installed: str) -> list[Any]:
    """Configs for ``installed``: the suite's own host's where it has the
    app, since a model class registers in Django's global registry and only
    there, and an isolated registry's for the rest."""
    own = [name for name in installed if not global_apps.is_installed(name)]
    isolated = {config.name: config for config in Apps(own).get_app_configs()}
    return [
        global_apps.get_app_config(name.rpartition(".")[2])
        if global_apps.is_installed(name)
        else isolated[name]
        for name in installed
    ]


@pytest.mark.parametrize(
    ("installed", "neolith", "declared", "stray"),
    [
        (
            [*HOSTED, "neolith"],
            {"UNDECLARED_APPS": ["tests.host_apps.couriers.apps.CouriersConfig"]},
            [
                "app couriers: has a declaration and is listed in "
                f"{UNDECLARED}; "
                "name it among the host's apps"
            ],
            [],
        ),
        (
            [*HOSTED, "django.contrib.contenttypes", "neolith"],
            {},
            [],
            [
                "app contenttypes: has models and no declaration, and this host has "
                "no default database"
            ],
        ),
        (
            [*HOSTED, "django.contrib.contenttypes", "neolith"],
            {"DATABASES": {"default": sqlite(":memory:")}},
            [],
            [],
        ),
        ([*HOSTED, "django.contrib.humanize", "neolith"], {}, [], []),
    ],
    ids=[
        "declared app as undeclared",
        "models in a closed host",
        "models on default",
        "no models",
    ],
)
def test_the_kinds_of_installed_app_are_judged(
    installed: list[str], neolith: dict[str, Any], declared: list[str], stray: list[str]
) -> None:
    configs = _configs(*installed)
    assert _checks.installed_problems(configs, neolith) == (declared, stray)


def test_a_declared_battery_and_a_battery_with_models_are_judged(
    write_app: WriteApp,
) -> None:
    app = _declared(
        write_app, batteries='("tests.host_apps.web", "django.contrib.contenttypes")'
    )
    configs = _configs(
        *HOSTED[:2],
        app,
        "tests.host_apps.web",
        "django.contrib.contenttypes",
        "neolith",
    )
    assert _checks.installed_problems(configs, {}) == (
        [
            f"app web: has a declaration and is named as a battery of {app}; name it "
            "among the host's apps"
        ],
        [
            "app contenttypes: has models and no declaration, and this host has no "
            f"default database; it is a battery of {app}"
        ],
    )


_DATABASE_CACHE = {
    "BACKEND": "django.core.cache.backends.db.DatabaseCache",
    "LOCATION": "t",
}


@pytest.mark.parametrize(
    ("installed", "neolith", "caches", "unused", "cached"),
    [
        (
            [*HOSTED, "neolith"],
            {"DATABASES": {"default": sqlite(":memory:")}},
            {},
            ["database default: nothing in this host lives on it"],
            [],
        ),
        (
            [*HOSTED, "django.contrib.contenttypes", "neolith"],
            {"DATABASES": {"default": sqlite(":memory:")}},
            {},
            [],
            [],
        ),
        (
            [*HOSTED, "neolith"],
            {"DATABASES": {"default": sqlite(":memory:")}},
            {"default": _DATABASE_CACHE},
            [],
            [],
        ),
        (
            [*HOSTED, "neolith"],
            {},
            {"c": _DATABASE_CACHE},
            [],
            [
                "cache c: a database cache needs a default database, and this host "
                "has none"
            ],
        ),
        (
            [*HOSTED, "neolith"],
            {},
            {"c": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
            [],
            [],
        ),
        ([*HOSTED, "neolith"], {}, {"c": {"BACKEND": "no.such.Cache"}}, [], []),
        (
            [*HOSTED, "neolith"],
            {},
            {"c": {"BACKEND": "tests.lite.cache.ProjectCache"}},
            [],
            [
                "cache c: a database cache needs a default database, and this host "
                "has none"
            ],
        ),
    ],
    ids=[
        "default unused",
        "an app on default",
        "a cache on default",
        "a database cache in a closed host",
        "another cache",
        "a cache that does not import",
        "a subclass of the database cache",
    ],
)
def test_what_lives_on_default_is_judged(
    installed: list[str],
    neolith: dict[str, Any],
    caches: object,
    unused: list[str],
    cached: list[str],
) -> None:
    configs = _configs(*installed)
    assert _checks.default_problems(configs, neolith, caches) == (unused, cached)


_SET_UP = """
import django
from django.core.exceptions import ImproperlyConfigured

try:
    django.setup()
except ImproperlyConfigured as error:
    print(error)
else:
    print("set up")
"""


@pytest.mark.parametrize(
    ("hosted", "neolith", "extra", "lines"),
    [
        (
            ["tests.host_apps.stock", "tests.host_apps.couriers"],
            {"UNDECLARED_APPS": ["tests.host_apps.web.apps.WebConfig"]},
            {},
            [
                "(neolith.E003) app web: has a declaration and is listed in "
                f"{UNDECLARED}; "
                "name it among the host's apps",
                "(neolith.E005) settings: INSTALLED_APPS differs from what the host "
                "builds: entry 2 is 'tests.host_apps.web.apps.WebConfig', the host "
                "builds 'tests.host_apps.web'; build settings with "
                "neolith.host.build_settings, and change them through NEOLITH",
            ],
        ),
        (
            HOSTED,
            {"UNDECLARED_APPS": ["django.contrib.contenttypes"]},
            {"CACHES": {"c": _DATABASE_CACHE}},
            [
                "(neolith.E004) app contenttypes: has models and no declaration, and "
                "this host has no default database",
                "(neolith.E016) cache c: a database cache needs a default database, "
                "and this host has none",
            ],
        ),
        (
            HOSTED,
            {"DATABASES": {"default": sqlite(":memory:")}},
            {},
            ["(neolith.E015) database default: nothing in this host lives on it"],
        ),
    ],
    ids=["E003", "E004 and E016", "E015"],
)
def test_set_up_stops_the_host_on_each_check(
    hosted: list[str], neolith: dict[str, Any], extra: dict[str, Any], lines: list[str]
) -> None:
    printed = in_built_host(_SET_UP, hosted, {"NEOLITH": neolith, **extra})
    assert printed.splitlines() == [
        "host: the settings are refused before the host serves:",
        *lines,
    ]


@pytest.mark.spec("APP-30")
def test_set_up_refuses_a_silenced_host_error() -> None:
    printed = in_fresh_process(
        _SET_UP, [*HOSTED, "neolith"], {"SILENCED_SYSTEM_CHECKS": ["neolith.E005"]}
    )
    assert printed.splitlines() == [
        "host: the settings are refused before the host serves:",
        "(neolith.E002) SILENCED_SYSTEM_CHECKS: 'neolith.E005' is a host error and "
        "cannot be silenced",
    ]


# The first set-up compares the settings Django holds with what the host
# builds (neolith.E005): hand-written settings that match are admitted.


@pytest.mark.spec("APP-29")
@pytest.mark.parametrize(
    ("settings_given", "difference"),
    [
        (
            {"DATABASE_ROUTERS": []},
            "DATABASE_ROUTERS differs from what the host builds: it has 0 entries, "
            "the host builds 1",
        ),
        (
            {"DATABASES": {"default": {}, "x": {}}},
            "DATABASES differs from what the host builds: alias 'x' is not one the "
            "host builds",
        ),
        (
            {"DATABASES": {"default": sqlite("/x")}},
            "DATABASES differs from what the host builds: ['default']['ENGINE'] is "
            "not the value the host builds",
        ),
    ],
    ids=["routers", "an alias", "a real default"],
)
def test_hand_written_settings_that_differ_are_refused_at_set_up(
    settings_given: dict[str, Any], difference: str
) -> None:
    printed = in_fresh_process(_SET_UP, [*HOSTED, "neolith"], settings_given)
    assert printed.splitlines() == [
        "host: the settings are refused before the host serves:",
        f"(neolith.E005) settings: {difference}; build settings with "
        "neolith.host.build_settings, and change them through NEOLITH",
    ]


class _Name(str):
    __slots__ = ()


# A value under an alias, which no message may show (K9).
_MARKER = "a value no message shows"
_LEDGER = {"ENGINE": SQLITE, "NAME": "/l", "TEST": {"DEPENDENCIES": []}}
_BUILT_DATABASES = {"default": {}, "ledger": _LEDGER}


@pytest.mark.parametrize(
    ("found", "difference"),
    [
        ({"default": {}}, "alias 'ledger' is missing"),
        ({**_BUILT_DATABASES, "x": {}}, "alias 'x' is not one the host builds"),
        (
            {"default": {}, "ledger": {**_LEDGER, "TEST": {}}},
            "['ledger']['TEST']['DEPENDENCIES'] is missing",
        ),
        (
            {"default": {}, "ledger": {**_LEDGER, "TEST": []}},
            "it cannot be read as Django reads it: AttributeError",
        ),
        ([("default", {})], "it is a list, the host builds a dict"),
        (
            {"default": {}, "ledger": {**_LEDGER, "NAME": b"/l"}},
            "['ledger']['NAME'] is a bytes, the host builds a str",
        ),
        (
            {"default": {}, "ledger": {**_LEDGER, "PASSWORD": _MARKER}},
            "['ledger']['PASSWORD'] is not the value the host builds",
        ),
        (
            {"default": {}, "ledger": {**_LEDGER, "ATOMIC_REQUESTS": True}},
            "['ledger']['ATOMIC_REQUESTS'] is not the value the host builds",
        ),
        (
            {
                "default": {},
                "ledger": {
                    **_LEDGER,
                    "TEST": {"DEPENDENCIES": [], "MIRROR": "default"},
                },
            },
            "['ledger']['TEST']['MIRROR'] is a str, the host builds a NoneType",
        ),
        (
            {"default": {}, "ledger": {**_LEDGER, "SHARDS": 2}},
            "['ledger']['SHARDS'] is not one the host builds",
        ),
        (
            {"default": {"ENGINE": SQLITE, "NAME": "/d"}, "ledger": _LEDGER},
            "['default']['ENGINE'] is not the value the host builds",
        ),
        (
            {
                "default": {"ENGINE": "django.db.backends.dummy"},
                "ledger": {
                    **_LEDGER,
                    "HOST": "",
                    "TEST": {"DEPENDENCIES": [], "CHARSET": None},
                },
            },
            None,
        ),
    ],
    ids=[
        "an alias missing",
        "an alias added",
        "a nested key missing",
        "a mapping replaced",
        "the setting replaced",
        "a value of another type",
        "a password",
        "a key Django refuses in NEOLITH",
        "a nested key Django refuses in NEOLITH",
        "a key of no one",
        "a real default in a closed host",
        "keys Django fills in, with their default values",
    ],
)
@pytest.mark.spec("APP-29")
def test_databases_are_compared_as_django_serves_with_them(
    found: object, difference: str | None
) -> None:
    from neolith._configure import databases_difference

    assert databases_difference(found, _BUILT_DATABASES) == difference


class _Opaque:
    """A value that compares by identity and refuses to be copied, as an
    SSL context does."""

    def __deepcopy__(self, memo: object) -> "_Opaque":
        raise TypeError("an opaque value is not copied")

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("an opaque value is not pickled")


def _places(value: object) -> list[dict[str, Any]]:
    """``DATABASES`` with ``value`` at each place a deployer can put one."""
    return [
        {"default": {}, "ledger": {**_LEDGER, "OPTIONS": {"ssl": value}}},
        {"default": {}, "ledger": {**_LEDGER, "OPTIONS": value}},
        {"default": {"ENGINE": SQLITE, "NAME": "/d", "OPTIONS": {"c": [value]}}},
        {
            "default": {},
            "ledger": {**_LEDGER, "TEST": {"DEPENDENCIES": [], "CHARSET": value}},
        },
    ]


@pytest.mark.spec("APP-29")
@pytest.mark.parametrize("place", range(4))
def test_every_normalisation_keeps_one_value_equal_to_itself(place: int) -> None:
    """E005 fills Django's defaults into both sides; a value given once is
    the same object on both sides, so it is never a difference, however it
    compares or copies."""
    from neolith._configure import _difference, databases_difference

    value = _Opaque()
    databases = _places(value)[place]
    assert databases_difference(databases, _places(value)[place]) is None
    assert _difference([value], [value]) is None


@pytest.mark.spec("APP-29")
def test_an_options_value_that_compares_by_identity_builds_and_sets_up(
    project: Callable[..., Path],
) -> None:
    """``OPTIONS`` values are compared as given, not copied, so a value that
    compares by identity, such as an SSL context, sets up."""
    directory = project(
        _BUILD.format(apps=HOSTED),
        above=textwrap.dedent(
            """\
            import ssl

            NEOLITH = {
                "DATABASES": {
                    "default": {
                        "ENGINE": "django.db.backends.sqlite3",
                        "NAME": "unused.sqlite3",
                        "OPTIONS": {"ssl": ssl.create_default_context()},
                    }
                },
                "UNDECLARED_APPS": ["django.contrib.contenttypes"],
            }
            """
        ),
    )
    ran = _run(directory, "manage.py", "check")
    assert ran.returncode == 0, ran.stderr[-2000:]


def _filled_keys() -> list[tuple[str, ...]]:
    """Every key Django fills into an alias, at every depth."""
    from django.db.utils import ConnectionHandler

    filled = ConnectionHandler().configure_settings({"default": {}})["default"]
    keys: list[tuple[str, ...]] = []
    for key, value in filled.items():
        if type(value) is dict and value:
            keys.extend((key, inner) for inner in value)
        else:
            keys.append((key,))
    return keys


@pytest.mark.spec("APP-29")
@pytest.mark.parametrize("path", _filled_keys(), ids=lambda path: ".".join(path))
def test_every_key_django_fills_in_is_compared(path: tuple[str, ...]) -> None:
    """A value other than Django's default, written after the builder, for
    every key ``configure_settings`` fills in."""
    from neolith._configure import databases_difference

    found: dict[str, Any] = copy.deepcopy(_BUILT_DATABASES)
    target: dict[str, Any] = found["ledger"]
    for part in path[:-1]:
        target = target.setdefault(part, {})
    target[path[-1]] = object()
    difference = databases_difference(found, _BUILT_DATABASES)
    assert difference is not None
    assert "".join(f"[{part!r}]" for part in path) in difference


def _marked(alias: str) -> dict[str, Any]:
    """Connection settings ``NEOLITH`` admits for ``alias``, with the marker
    in every leaf: the keys Django fills in, and an ``OPTIONS`` with a
    mapping and a list in it. Each leaf names its alias too, so that no two
    aliases name one database."""
    leaf = f"{_MARKER}, {alias}"
    keys = (
        "ENGINE",
        "NAME",
        "USER",
        "PASSWORD",
        "HOST",
        "PORT",
        "AUTOCOMMIT",
        "CONN_MAX_AGE",
        "CONN_HEALTH_CHECKS",
        "TIME_ZONE",
    )
    return {
        **dict.fromkeys(keys, leaf),
        "OPTIONS": {
            "sslkey": leaf,
            "pool": {"dsn": leaf},
            "servers": [leaf, {"dsn": leaf}],
        },
        "TEST": {"CHARSET": leaf, "COLLATION": leaf, "MIGRATE": leaf},
    }


def _paths(value: object, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """The path of every value in a mapping or a list, at every depth."""
    if type(value) is dict:
        items = list(value.items())
    elif type(value) is list:
        items = list(enumerate(value))
    else:
        return
    for key, inner in items:
        yield (*path, key)
        yield from _paths(inner, (*path, key))


def _changed(
    databases: dict[str, Any], path: tuple[Any, ...]
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Copies of ``databases`` changed at ``path``: the value replaced by
    text that holds the marker, the value removed, and, where it is a
    mapping or a list, one more value that holds the marker put into it."""
    for change in ("replaced", "removed", "added to"):
        found = copy.deepcopy(databases)
        holder: Any = found
        for part in path[:-1]:
            holder = holder[part]
        value = holder[path[-1]]
        if change == "replaced":
            holder[path[-1]] = f"{_MARKER}, changed"
        elif change == "removed":
            del holder[path[-1]]
        elif type(value) is dict:
            value["ADDED"] = _MARKER
        elif type(value) is list:
            value.append(_MARKER)
        else:
            continue
        yield change, found


@pytest.mark.spec("APP-29")
def test_no_value_under_an_alias_is_shown_in_a_difference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DATABASES`` as Django serves with it, built from a ``NEOLITH`` with
    the marker in every leaf, then changed at one place at a time, for
    every place at every depth. Each change that makes a difference is
    refused, and the message shows neither the value found nor the one the
    host builds (K9)."""
    from django.db.utils import ConnectionHandler

    from neolith import _configure

    configs = [AppConfig.create(name) for name in (*HOSTED, LEDGER)]
    neolith = {"DATABASES": {alias: _marked(alias) for alias in ("default", "ledger")}}
    built = _configure.built_settings(_checks.hosted_declarations(configs), neolith)
    served = ConnectionHandler().configure_settings(copy.deepcopy(built["DATABASES"]))
    for path in _paths(served):
        for change, found in _changed(served, path):
            held = types.SimpleNamespace(
                **{**built, "NEOLITH": neolith, "DATABASES": found}
            )
            monkeypatch.setattr(_configure, "settings", held)
            problems = _configure.settings_problems(configs)
            assert _MARKER not in "\n".join(problems), (path, change)
            if change == "removed":
                # A key Django fills in with its default is no difference.
                continue
            assert len(problems) == 1, (path, change)
            assert problems[0].startswith(
                "settings: DATABASES differs from what the host builds: "
            ), (path, change)


class _Loud:
    """A value whose comparison raises with text of its own."""

    def __eq__(self, other: object) -> bool:
        raise ValueError(_MARKER)


@pytest.mark.spec("APP-29")
def test_an_error_raised_while_defaults_are_filled_in_is_named_by_its_class() -> None:
    """Django compares ``ENGINE`` while it fills the defaults in. What a
    value's own code raises there may quote the value, so the message of
    the error is not shown."""
    from neolith._configure import databases_difference

    found = {"default": {}, "ledger": {**_LEDGER, "ENGINE": _Loud()}}
    assert databases_difference(found, _BUILT_DATABASES) == (
        "it cannot be read as Django reads it: ValueError"
    )


@pytest.mark.spec("APP-29")
@pytest.mark.parametrize(
    ("found", "expected", "difference"),
    [
        ({"PORT": "5432"}, {"PORT": 5432}, "['PORT'] is a str, the host builds a int"),
        ({"X": [True]}, {"X": [1]}, "['X'][0] is a bool, the host builds a int"),
    ],
)
def test_a_value_that_differs_only_in_type_names_the_two_types(
    found: object, expected: object, difference: str
) -> None:
    """PORT "5432" against 5432: the message names the types, never a value (K9)."""
    from neolith._configure import _difference

    message = _difference(found, expected, whole=True)
    assert message == difference
    assert "5432" not in str(message)


@pytest.mark.parametrize(
    ("found", "expected", "difference"),
    [
        (("neolith",), ["neolith"], "it is a tuple, the host builds a list"),
        (["neolith", "x"], ["neolith"], "it has 2 entries, the host builds 1"),
        ([1], ["1"], "entry 0 is 1, the host builds '1'"),
        (
            [_Name("neolith")],
            ["neolith"],
            "entry 0 is 'neolith', the host builds 'neolith'",
        ),
        (["neolith"], ["neolith"], None),
        ({"a": 1}, {"a": 2}, "['a'] is not the value the host builds"),
        ({"a": ["x"]}, {"a": ["y"]}, "['a'][0] is not the value the host builds"),
        ({"a": ["x", "y"]}, {"a": ["x"]}, "['a'] has 2 entries, the host builds 1"),
    ],
    ids=[
        "a tuple for a list",
        "an entry added",
        "an entry of another type",
        "an entry of a subclass",
        "equal",
        "a value in a mapping",
        "an entry of a list in a mapping",
        "an entry added to a list in a mapping",
    ],
)
@pytest.mark.spec("APP-29")
def test_the_first_difference_from_what_the_host_builds_is_named(
    found: object, expected: object, difference: str | None
) -> None:
    from neolith._configure import _difference

    assert _difference(found, expected) == difference


def test_a_default_with_test_settings_sets_up_again_unchanged(tmp_path: Path) -> None:
    """Django fills ``TEST`` in place in ``DATABASES``; the host gave it a
    copy, so ``NEOLITH`` still passes its own check at a later set-up."""
    printed = in_built_host(
        """
        import django
        from django.conf import settings
        from django.test.utils import override_settings

        django.setup()
        with override_settings(INSTALLED_APPS=list(settings.INSTALLED_APPS)):
            print(settings.NEOLITH["DATABASES"]["default"]["TEST"])
        """,
        HOSTED,
        {
            "NEOLITH": {
                "DATABASES": {
                    "default": {
                        **sqlite(str(tmp_path / "d")),
                        "TEST": {"CHARSET": "UTF8"},
                    }
                },
                "UNDECLARED_APPS": ["django.contrib.contenttypes"],
            }
        },
    )
    assert printed.splitlines() == ["{'CHARSET': 'UTF8'}"]


def test_hand_written_settings_that_match_are_admitted() -> None:
    assert in_fresh_process(_SET_UP, [*HOSTED, "neolith"]) == "set up\n"


def test_a_changed_key_of_an_alias_is_refused_at_set_up(tmp_path: Path) -> None:
    built = {
        "default": {},
        "ledger": {**sqlite(str(tmp_path / "other")), "TEST": {"DEPENDENCIES": []}},
    }
    printed = in_fresh_process(
        _SET_UP,
        [*HOSTED, LEDGER, "neolith"],
        {
            "NEOLITH": {"DATABASES": {"ledger": sqlite(str(tmp_path / "l"))}},
            "DATABASES": built,
        },
    )
    assert printed.splitlines()[1] == (
        "(neolith.E005) settings: DATABASES differs from what the host builds: "
        "['ledger']['NAME'] is not the value the host builds; build settings with "
        "neolith.host.build_settings, and change them through NEOLITH"
    )


# After set-up: an override of NEOLITH may change the request deadline only.


@pytest.fixture
def neolith() -> dict[str, Any]:
    """A copy of the test host's NEOLITH (tests/settings.py)."""
    return copy.deepcopy(settings.NEOLITH)


@pytest.mark.spec("APP-29")
@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda n: n.update(
                UNDECLARED_APPS=["django.contrib.contenttypes", "tests.plain_app"]
            ),
            'NEOLITH["UNDECLARED_APPS"]: cannot change after set-up',
        ),
        (
            lambda n: n.pop("UNDECLARED_APPS"),
            'NEOLITH["UNDECLARED_APPS"]: cannot change after set-up',
        ),
        (
            lambda n: n["DATABASES"]["default"].update(NAME="/elsewhere"),
            'NEOLITH["DATABASES"]: cannot change after set-up',
        ),
        (lambda n: n.update(OTHER=1), f"NEOLITH: unknown key 'OTHER'; {KEYS}"),
        (
            lambda n: n.update(REQUEST_DEADLINE=-1),
            'NEOLITH["REQUEST_DEADLINE"]: -1 is outside 0.001 to 9999999.999 seconds',
        ),
    ],
    ids=[
        "undeclared apps",
        "undeclared apps removed",
        "databases",
        "unknown key",
        "deadline out of range",
    ],
)
def test_an_override_of_neolith_after_set_up_is_refused_by_key(
    neolith: dict[str, Any], change: Callable[[dict[str, Any]], object], message: str
) -> None:
    kept = copy.deepcopy(settings.NEOLITH)
    change(neolith)
    before = _host._current()
    with (
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(NEOLITH=neolith),
    ):
        pytest.fail("the override was admitted")
    assert str(raised.value) == message
    assert kept == settings.NEOLITH
    assert _host._current() is before


@pytest.mark.parametrize("name", ["DATABASES", "DATABASE_ROUTERS"])
@pytest.mark.spec("APP-29")
def test_an_override_of_a_host_built_setting_after_set_up_is_refused(name: str) -> None:
    with (
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(**{name: getattr(settings, name)}),
    ):
        pytest.fail("the override was admitted")
    assert str(raised.value) == f"settings: {name} cannot change after set-up"


@pytest.mark.parametrize("deadline", [5, 0.5, None])
def test_the_request_deadline_follows_an_override_and_returns(
    neolith: dict[str, Any], deadline: float | None
) -> None:
    current = _host._current()
    assert current is not None
    assert current.request_deadline == 30.0
    with override_settings(NEOLITH={**neolith, "REQUEST_DEADLINE": deadline}):
        now = _host._current()
        assert now is not None
        assert now.request_deadline == (None if deadline is None else float(deadline))
    after = _host._current()
    assert after is not None
    assert after.request_deadline == 30.0


# The entry: a settings module, run by Django's own tools, each in a process
# of its own.


_MANAGE = """\
import sys

from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
"""


@pytest.fixture
def project(tmp_path: Path) -> Callable[..., Path]:
    """Write a settings module ``site_settings`` and ``manage.py`` into a
    directory of their own; return the directory."""

    def write(body: str, *, above: str = "", below: str = "") -> Path:
        (tmp_path / "manage.py").write_text(_MANAGE, encoding="utf-8")
        text = textwrap.dedent(
            """\
            from neolith.host import build_settings

            SECRET_KEY = "a key for one test process only"
            MIDDLEWARE = [
                "neolith.middleware.RootMiddleware",
                "neolith.middleware.PrincipalMiddleware",
            ]
            NEOLITH = {}
            ROOT_URLCONF = "tests.request_urls"
            ALLOWED_HOSTS = ["*"]
            """
        )
        text += textwrap.dedent(above) + textwrap.dedent(body) + textwrap.dedent(below)
        (tmp_path / "site_settings.py").write_text(text, encoding="utf-8")
        return tmp_path

    return write


def _run(
    directory: Path, *arguments: str, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=directory,
        timeout=timeout,
        env={
            "PYTHONPATH": os.pathsep.join([str(directory), *sys.path]),
            "DJANGO_SETTINGS_MODULE": "site_settings",
        },
    )


_BUILD = "globals().update(build_settings({apps!r}, globals()))\n"

_TWICE = """
from django.core.wsgi import get_wsgi_application

for attempt in (1, 2):
    try:
        get_wsgi_application()
    except Exception as error:
        print(attempt, type(error).__name__, str(error).splitlines()[0])
    else:
        print(attempt, "built")
"""


@pytest.mark.spec("APP-1", "APP-16")
@pytest.mark.parametrize(
    ("apps", "above", "message"),
    [
        (
            ["tests.settings_apps.reader"],
            "",
            "app tests.settings_apps.reader: reads Django settings at import; a "
            "declaration is read before settings exist",
        ),
        (
            ["tests.settings_apps.guarded"],
            "",
            "app tests.settings_apps.guarded: reads Django settings at import; a "
            "declaration is read before settings exist",
        ),
        (
            ["tests.settings_apps.broken"],
            "",
            "app tests.settings_apps.broken.apps.BrokenConfig: resources",
        ),
        (
            HOSTED,
            "from django.conf import settings as early\nearly.DEBUG\n"
            "NEOLITH = {'OTHER': 1}\n",
            "settings: a setting was read before build_settings was called",
        ),
    ],
    ids=[
        "reads a setting",
        "guards its read",
        "reads, then a refused declaration",
        "a read above the call",
    ],
)
def test_a_refusal_in_a_settings_module_is_final_for_the_process(
    project: Callable[..., Path], apps: list[str], above: str, message: str
) -> None:
    """``manage.py`` goes on after a settings module fails to import, with
    whatever settings Django holds (ADR 0022 fact 18); the refusal leaves it
    none, and every later read meets the same refusal."""
    directory = project(_BUILD.format(apps=apps), above=above)
    for command in (
        ["manage.py", "check"],
        ["manage.py", "runserver", "--noreload", "127.0.0.1:0"],
    ):
        ran = _run(directory, *command)
        assert ran.returncode != 0, (command, ran.stdout, ran.stderr[-2000:])
        assert message in ran.stderr, (command, ran.stderr[-2000:])
        assert "System check identified" not in ran.stdout + ran.stderr
    ran = _run(directory, "-c", _TWICE)
    lines = ran.stdout.splitlines()
    assert len(lines) == 2, ran.stderr[-2000:]
    for line in lines:
        kind = "DeclarationError" if "broken" in apps[0] else "ImproperlyConfigured"
        assert line.split(" ", 1)[1].startswith(kind), lines
        assert message in line, lines


_COMMAND_LINE = """
import json
import sys

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from neolith.host import build_settings

try:
    built = build_settings(
        json.loads(sys.argv[1]),
        {
            "MIDDLEWARE": [
                "neolith.middleware.RootMiddleware",
                "neolith.middleware.PrincipalMiddleware",
            ],
            "NEOLITH": {},
        },
    )
except ImproperlyConfigured as error:
    print(str(error).splitlines()[0])
else:
    print("built")
print(settings.configured)
"""


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("app", "outcome"),
    [
        (
            "tests.settings_apps.reader",
            "app tests.settings_apps.reader: cannot be read: ImproperlyConfigured: "
            "Requested setting DEBUG, but settings are not configured.",
        ),
        ("tests.settings_apps.guarded", "built"),
    ],
)
def test_a_read_at_import_is_refused_by_django_where_no_settings_exist(
    tmp_path: Path, app: str, outcome: str
) -> None:
    """With no settings and no settings module, as under the command line,
    Django refuses the read itself; a read the app guards falls back to the
    app's own value, the gap ADR 0022 Decision 1 states."""
    ran = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-W",
            "error",
            "-c",
            _COMMAND_LINE,
            json.dumps([*HOSTED, app]),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": os.pathsep.join(sys.path)},
    )
    lines = ran.stdout.splitlines()
    assert lines[0].startswith(outcome), ran.stderr[-2000:]
    assert lines[1] == "False"


@pytest.mark.spec("APP-29")
@pytest.mark.parametrize(
    ("below", "difference"),
    [
        (
            'INSTALLED_APPS = INSTALLED_APPS + ["django.contrib.humanize"]\n',
            "INSTALLED_APPS differs from what the host builds: it has "
            f"{len(HOSTED) + 2} entries, the host builds {len(HOSTED) + 1}",
        ),
        (
            'DATABASES["default"] = {"ENGINE": "django.db.backends.sqlite3", '
            '"NAME": "x"}\n',
            "DATABASES differs from what the host builds: ['default']['ENGINE'] is "
            "not the value the host builds",
        ),
        (
            'DATABASES["default"]["ATOMIC_REQUESTS"] = True\n',
            "DATABASES differs from what the host builds: "
            "['default']['ATOMIC_REQUESTS'] is not the value the host builds",
        ),
        (
            "DATABASE_ROUTERS = []\n",
            "DATABASE_ROUTERS differs from what the host builds: it has 0 entries, "
            "the host builds 1",
        ),
    ],
    ids=[
        "INSTALLED_APPS",
        "DATABASES",
        "a key of DATABASES the host did not write",
        "DATABASE_ROUTERS",
    ],
)
def test_a_settings_module_changed_after_the_builder_call_stops_the_host(
    project: Callable[..., Path], below: str, difference: str
) -> None:
    directory = project(_BUILD.format(apps=HOSTED), below=below)
    ran = _run(directory, "manage.py", "check")
    assert ran.returncode != 0
    assert (
        f"(neolith.E005) settings: {difference}; build settings with "
        "neolith.host.build_settings, and change them through NEOLITH"
    ) in ran.stderr


def test_a_settings_module_that_calls_the_builder_last_serves(
    project: Callable[..., Path],
) -> None:
    directory = project(_BUILD.format(apps=HOSTED))
    ran = _run(directory, "manage.py", "check")
    assert ran.returncode == 0, ran.stderr[-2000:]
    ran = _run(directory, "-c", _TWICE)
    assert ran.stdout.splitlines() == ["1 built", "2 built"], ran.stderr[-2000:]


def test_the_test_host_is_built_by_the_builder() -> None:
    """``tests/settings.py`` calls the builder, so the suite runs on what it
    builds."""
    assert list(settings.INSTALLED_APPS) == host_apps.INSTALLED
    assert settings.DATABASE_ROUTERS == ["neolith.routers.AppRouter"]
    assert all(
        declaration_of(type(c)) is not None
        for c in Apps(host_apps.HOSTED).get_app_configs()
    )


@pytest.fixture(autouse=True)
def _no_refusal_recorded() -> Iterator[None]:
    """The tests here call the plain function, which records nothing: the
    entry's record stays empty in this process."""
    from neolith import host

    yield
    assert host._refused is None
