"""Boot: the checks a host runs when a handler is built (spec 05, Boot;
ADR 0022 Decision 7; ADR 0009 Decision 7).

Boot keeps its outcome for the process, so its parts are tested as
functions. The state of each SQLite alias is judged by
``neolith._boot.state_messages`` before any migration is read, here with
the settings each test gives it. The migrations are read through Django's
own connections (``schema_messages``), so those tests run in a process of
their own that hosts the apps of ``tests.boot_apps`` on files, beside
``contenttypes``, an app with no declaration and with migrations, on a real
``default``. Boot itself, from a handler's constructor, runs in processes of
their own too.
"""

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.db.utils import ConnectionHandler
from django.test import override_settings

from neolith import UsageError, _boot, testing
from tests import host_apps
from tests.hosting import in_built_host

SQLITE = "django.db.backends.sqlite3"
NO_FILE = (
    "the database is in memory, or nothing has created it yet (the migrate step "
    "creates the aliases it migrates)"
)
IN_MEMORY = (
    "an in-memory SQLite database is empty on a new thread, or gone once its last "
    "connection closes; a host that serves needs a file or a server"
)
# The table Django's migration recorder creates, written without Django, so
# that a test needs no alias of its own to record a migration.
RECORDS = (
    "CREATE TABLE IF NOT EXISTS django_migrations (id INTEGER PRIMARY KEY "
    "AUTOINCREMENT, app varchar(255) NOT NULL, name varchar(255) NOT NULL, "
    "applied datetime NOT NULL)"
)


def record(path: Path, rows: list[tuple[str, str]]) -> None:
    """Record ``rows`` as applied migrations in the SQLite file ``path``."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(RECORDS)
        connection.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, "
            "'2026-01-01 00:00:00')",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def migrated(path: Path, *labels: str) -> None:
    """Record every migration of ``labels`` as applied in ``path``, as the
    migrate step leaves it."""
    loader = MigrationLoader(None)
    record(path, sorted(key for key in loader.disk_migrations if key[0] in labels))


def filled(**entry: object) -> dict[str, Any]:
    """Connection settings with Django's defaults filled in, as Boot reads
    them from Django."""
    found: dict[str, Any] = ConnectionHandler({"default": dict(entry)}).settings[
        "default"
    ]
    return found


def read(engine: str = SQLITE, **entry: object) -> list[tuple[str, str]]:
    found, readable = _boot.state_messages({"default": filled(ENGINE=engine, **entry)})
    assert readable == ([] if found else ["default"])
    return [(str(m.id), m.msg) for m in found]


@pytest.fixture(autouse=True)
def _unblocked(django_db_blocker: Any) -> Iterator[None]:  # noqa: ANN401
    """Boot opens connections of its own, to files the tests make."""
    with django_db_blocker.unblock():
        yield


# Each state of a SQLite alias gets one id.


@pytest.mark.spec("APP-32")
@pytest.mark.parametrize("name", [":memory:", Path(":memory:")], ids=["str", "path"])
def test_the_literal_memory_name_is_refused_without_a_connection(name: object) -> None:
    assert read(NAME=name) == [("neolith.E007", f"database default: {IN_MEMORY}")]


@pytest.mark.spec("APP-31", "APP-36")
def test_a_path_with_no_file_is_refused_and_left_without_one(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    assert read(NAME=str(path)) == [
        ("neolith.E018", f"database default: no database at {path}; {NO_FILE}")
    ]
    assert not path.exists()


@pytest.mark.spec("APP-31")
def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    """As ``runserver``'s migration check leaves on ``default``."""
    path = tmp_path / "empty.sqlite3"
    path.touch()
    assert read(NAME=str(path)) == [
        ("neolith.E018", f"database default: no database at {path}; {NO_FILE}")
    ]
    assert path.stat().st_size == 0


@pytest.mark.spec("APP-31")
def test_a_file_with_content_and_no_schema_is_refused(tmp_path: Path) -> None:
    """An ``init_command`` that sets WAL writes a header and nothing in
    ``sqlite_master``."""
    path = tmp_path / "wal.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    assert path.stat().st_size > 0
    wal = {"init_command": "PRAGMA journal_mode=WAL"}
    assert read(NAME=str(path), OPTIONS=wal) == [
        ("neolith.E018", f"database default: no database at {path}; {NO_FILE}")
    ]


def _not_a_database(path: Path) -> None:
    path.write_bytes(b"not a database, " * 512)


def _unopenable(path: Path) -> None:
    path.write_bytes(b"x" * 100)
    path.chmod(0)


@pytest.mark.parametrize(
    "make", [_not_a_database, _unopenable], ids=["not a database", "cannot be opened"]
)
def test_a_file_sqlite_cannot_read_is_refused_as_unreadable(
    tmp_path: Path, make: Callable[[Path], None]
) -> None:
    path = tmp_path / "bad.sqlite3"
    make(path)
    try:
        if make is _unopenable and os.access(path, os.R_OK):
            pytest.skip("this user reads a file of mode 000")
        found = read(NAME=str(path))
    finally:
        path.chmod(0o600)
    assert [id_ for id_, _ in found] == ["neolith.E012"]
    assert found[0][1].startswith("database default cannot be read: ")


@pytest.mark.spec("APP-32")
def test_an_in_memory_uri_that_names_a_real_file_is_refused_as_in_memory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "real.sqlite3"
    migrated(path, "contenttypes")
    assert read(NAME=f"file:{path}?mode=memory&cache=shared") == [
        ("neolith.E007", f"database default: {IN_MEMORY}")
    ]


@pytest.mark.spec("APP-31", "APP-32")
@pytest.mark.parametrize(
    "name",
    [
        "file:boot-x?mode=memory&cache=shared",
        "file::memory:?cache=shared",
        "file:/boot-x?vfs=memdb",
        "file:",
    ],
)
def test_an_in_memory_uri_that_names_no_file_is_refused_as_no_database(
    name: str,
) -> None:
    [(found, message)] = read(NAME=name)
    assert found == "neolith.E018"
    assert message.endswith(NO_FILE)


@pytest.mark.spec("APP-36")
def test_a_file_with_a_schema_is_read_and_left_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "default.sqlite3"
    migrated(path, "contenttypes")
    before = path.read_bytes()
    assert read(NAME=str(path)) == []
    assert read(NAME=f"file:{path}?mode=ro") == []
    assert path.read_bytes() == before


@pytest.mark.spec("APP-31", "APP-36")
def test_a_zero_byte_file_is_refused_before_any_connection(tmp_path: Path) -> None:
    """An ``init_command`` would write to the file on connecting; Boot does
    not connect to it."""
    path = tmp_path / "empty.sqlite3"
    path.touch()
    wal = {"init_command": "PRAGMA journal_mode=WAL"}
    assert read(NAME=str(path), OPTIONS=wal)[0][0] == "neolith.E018"
    assert path.stat().st_size == 0
    assert not (tmp_path / "empty.sqlite3-wal").exists()


@pytest.mark.spec("APP-31", "APP-32", "APP-36")
def test_a_project_s_own_sqlite_backend_is_judged_as_sqlite(tmp_path: Path) -> None:
    """SQLite is known by the backend's vendor, not by its name."""
    lite = "tests.lite"
    missing = tmp_path / "missing.sqlite3"
    assert read(lite, NAME=str(missing)) == [
        ("neolith.E018", f"database default: no database at {missing}; {NO_FILE}")
    ]
    assert not missing.exists()
    assert read(lite, NAME=":memory:") == [
        ("neolith.E007", f"database default: {IN_MEMORY}")
    ]
    real = tmp_path / "real.sqlite3"
    migrated(real, "contenttypes")
    assert read(lite, NAME=f"file:{real}?mode=memory&cache=shared") == [
        ("neolith.E007", f"database default: {IN_MEMORY}")
    ]
    assert read(lite, NAME=str(real)) == []


@pytest.mark.parametrize(
    "engine",
    [
        "django.db.backends.postgresql",
        "django.db.backends.mysql",
        "django.db.backends.oracle",
        "django.contrib.gis.db.backends.postgis",
    ],
)
def test_an_alias_on_another_backend_is_left_to_the_migration_read(
    engine: str,
) -> None:
    """Only SQLite has the states before a schema that APP-31 names; an
    alias of any other vendor is judged by its migrations (APP-33), and no
    connection is opened here."""
    entry = filled(ENGINE=engine, NAME="x", HOST="nowhere")
    assert _boot.state_messages({"default": entry}) == ([], ["default"])


# Migrations, in a process that hosts the apps of tests.boot_apps.

_HOSTED = [
    f"tests.boot_apps.{app}" for app in ("fresh", "bare", "forked", "leaning", "empty")
]

_MIGRATIONS = """
import json
import sqlite3
import sys
import threading

import django

django.setup()

from django.db import connections

from neolith import _boot

ROOT, RECORDS = sys.argv[3:5]


def record(name, rows, *, again=False):
    connection = sqlite3.connect(ROOT + "/" + name + ".sqlite3")
    connection.execute(RECORDS)
    if again:
        connection.execute("DELETE FROM django_migrations")
    connection.executemany(
        "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, "
        "'2026-01-01 00:00:00')",
        rows,
    )
    connection.commit()
    connection.close()


def content(name):
    with open(ROOT + "/" + name + ".sqlite3", "rb") as file:
        return file.read()


seen = []
original = _boot._read


def spy(databases, *args):
    # On the thread Boot reads on, once it is done: which connections of
    # that thread are still open.
    found = original(databases, *args)
    seen.append(
        [
            threading.current_thread().name,
            [alias for alias in databases if connections[alias].connection is not None],
        ]
    )
    return found


_boot._read = spy


def read():
    return sorted([m.id, m.msg] for m in _boot.schema_messages())


found = {}
other = [("ledger", "0001_initial"), ("forked", "0002_a")]
record("fresh", [("fresh", "0001_initial"), *other])
record("bare", [])
record("forked", [("forked", "0001_initial")])
record("leaning", [])
record("empty", [])
record("default", [("elsewhere", "0001_initial")])
before = {name: content(name) for name in ("fresh", "default", "bare")}
found["each"] = read()
found["unchanged"] = all(content(name) == kept for name, kept in before.items())
found["closed"] = seen[-1]
record("fresh", [("fresh", "0002_more"), ("fresh", "9999_ahead")])
record(
    "default",
    [
        ("contenttypes", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
    ],
)
found["current"] = [m for m in read() if "app fresh" in m[1] or "contenttypes" in m[1]]
record("fresh", [("fresh", "0002_more")], again=True)
found["gap"] = [m for m in read() if "app fresh" in m[1]]
print(json.dumps(found))
"""


@pytest.fixture(scope="module")
def migrations(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("boot")
    labels = ["default", *(app.rpartition(".")[2] for app in _HOSTED)]
    databases = {
        label: {"ENGINE": SQLITE, "NAME": str(root / f"{label}.sqlite3")}
        for label in labels
    }
    body = _MIGRATIONS.replace("sys.argv[3:5]", repr([str(root), RECORDS]))
    printed = in_built_host(
        body,
        _HOSTED,
        {
            "NEOLITH": {
                "DATABASES": databases,
                "UNDECLARED_APPS": ["django.contrib.contenttypes"],
            }
        },
    )
    found: dict[str, Any] = json.loads(printed.splitlines()[-1])
    return found


@pytest.mark.spec("APP-33", "APP-34", "APP-35")
def test_each_state_of_an_app_s_migrations_has_its_id(
    migrations: dict[str, Any],
) -> None:
    assert migrations["each"] == sorted(
        [
            ["neolith.E008", "app bare: has a database and no migrations"],
            ["neolith.E008", "app empty: has a database and no migrations"],
            ["neolith.E011", "app forked: conflicting migrations 0002_a, 0002_b"],
            [
                "neolith.E009",
                "app fresh: database fresh has 1 unapplied migrations, the first is "
                "0002_more; run the migrate step",
            ],
            ["neolith.E013", "app leaning: migration 0001_initial depends on fresh"],
            [
                "neolith.E009",
                "app leaning: database leaning has 1 unapplied migrations, the first "
                "is 0001_initial; run the migrate step",
            ],
            [
                "neolith.E009",
                "app contenttypes: database default has 2 unapplied migrations, the "
                "first is 0001_initial; run the migrate step",
            ],
        ]
    )


@pytest.mark.spec("APP-36")
def test_the_read_writes_nothing_and_closes_its_connections(
    migrations: dict[str, Any],
) -> None:
    assert migrations["unchanged"] is True
    assert migrations["closed"] == ["neolith-boot", []]


@pytest.mark.spec("APP-33", "APP-37")
def test_a_current_schema_passes_beside_other_labels_rows(
    migrations: dict[str, Any],
) -> None:
    """Rows of other labels, and of a migration the app does not ship, are
    left alone: a database ahead of the code is admitted. An app with no
    declaration and with migrations is read on ``default``."""
    assert migrations["current"] == []


@pytest.mark.spec("APP-34")
def test_an_inconsistent_history_is_refused(migrations: dict[str, Any]) -> None:
    assert migrations["gap"] == [
        [
            "neolith.E010",
            "app fresh: database fresh: Migration fresh.0002_more is applied before "
            "its dependency fresh.0001_initial on database 'fresh'.",
        ]
    ]


# Boot from a handler: once per process, a refusal kept.

# Every migration of tests.boot_apps.fresh, which this process does not host.
FRESH = [("fresh", "0001_initial"), ("fresh", "0002_more")]

_HANDLER = """
import logging

import django
from django.core.handlers.wsgi import WSGIHandler

logged = []


class Keep(logging.Handler):
    def emit(self, record):
        logged.append(record.getMessage())


logging.getLogger("neolith.host").addHandler(Keep())
django.setup()
for attempt in (1, 2):
    try:
        WSGIHandler()
    except Exception as error:
        print(attempt, type(error).__name__, *str(error).splitlines(), sep="|")
    else:
        print(attempt, "built", sep="|")
    if attempt == 1:
        FIX
print("logged", len(logged), sep="|")
"""


def _handler(
    root: Path, fix: str = "pass", *, extra: str = "", **deployer: object
) -> list[list[str]]:
    labels = ["fresh", *([extra] if extra else [])]
    neolith = {
        "DATABASES": {
            label: {"ENGINE": SQLITE, "NAME": str(root / f"{label}.sqlite3")}
            for label in labels
        }
    }
    printed = in_built_host(
        _HANDLER.replace("FIX", fix),
        [f"tests.boot_apps.{label}" for label in labels],
        {"NEOLITH": neolith, "ROOT_URLCONF": "tests.empty_urls", **deployer},
    )
    return [line.split("|") for line in printed.splitlines()]


@pytest.mark.spec("APP-1", "APP-31")
def test_boot_stops_a_handler_and_keeps_its_refusal(tmp_path: Path) -> None:
    """The second handler is refused alike, though the database now exists:
    Boot runs once per process."""
    fix = (
        "from django.core.management import call_command; "
        "call_command('migrate', 'fresh', database='fresh', verbosity=0)"
    )
    lines = _handler(tmp_path, fix)
    refusal = [
        "ImproperlyConfigured",
        "host: the host cannot boot:",
        f"(neolith.E018) database fresh: no database at {tmp_path / 'fresh.sqlite3'}; "
        f"{NO_FILE}",
    ]
    assert lines[0] == ["1", *refusal]
    assert lines[1] == ["2", *refusal]
    assert (tmp_path / "fresh.sqlite3").exists()
    # Logged once, by the one run.
    assert lines[2] == ["logged", "1"]


@pytest.mark.spec("APP-33")
def test_boot_passes_on_a_current_schema(tmp_path: Path) -> None:
    record(tmp_path / "fresh.sqlite3", FRESH)
    assert _handler(tmp_path)[:2] == [["1", "built"], ["2", "built"]]


@pytest.mark.spec("APP-33", "APP-1")
def test_a_pending_migration_stops_a_handler(tmp_path: Path) -> None:
    record(tmp_path / "fresh.sqlite3", [("fresh", "0001_initial")])
    lines = _handler(tmp_path)
    assert lines[0] == [
        "1",
        "ImproperlyConfigured",
        "host: the host cannot boot:",
        "(neolith.E009) app fresh: database fresh has 1 unapplied migrations, the "
        "first is 0002_more; run the migrate step",
    ]


@pytest.mark.parametrize(
    ("urlconf", "message"),
    [
        (
            None,
            "settings: ROOT_URLCONF is not set; this host is about to serve requests",
        ),
        (["x"], "settings: ROOT_URLCONF: expected a str, got list"),
        (
            "tests.no_such_urls",
            "settings: ROOT_URLCONF tests.no_such_urls cannot be imported: "
            "ModuleNotFoundError: No module named 'tests.no_such_urls'",
        ),
    ],
    ids=["unset", "not a str", "does not import"],
)
def test_root_urlconf_is_set_and_imports(urlconf: object, message: str) -> None:
    with override_settings():
        if urlconf is None:
            del settings.ROOT_URLCONF
        else:
            settings.ROOT_URLCONF = urlconf
        assert _boot._urlconf_problems() == [message]
    assert _boot._urlconf_problems() == []


def test_root_urlconf_stops_a_handler(tmp_path: Path) -> None:
    record(tmp_path / "fresh.sqlite3", FRESH)
    lines = _handler(tmp_path, ROOT_URLCONF="tests.no_such_urls")
    assert lines[0][:3] == ["1", "ImproperlyConfigured", "host: the host cannot boot:"]
    assert lines[0][3].startswith("settings: ROOT_URLCONF tests.no_such_urls cannot")
    assert lines[1][:2] == ["2", "ImproperlyConfigured"]


def test_concurrent_handlers_share_one_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[bool] = []
    entered = threading.Barrier(4)

    def run(*, schema: bool) -> str | None:
        runs.append(schema)
        return "host: the host cannot boot:\nrefused"

    monkeypatch.setattr(_boot, "_kept", None)
    monkeypatch.setattr(_boot, "_run", run)
    outcomes: list[str] = []

    def construct() -> None:
        entered.wait()
        try:
            _boot.boot()
        except ImproperlyConfigured as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=construct) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert runs == [True]
    assert outcomes == ["host: the host cannot boot:\nrefused"] * 4


# The warning again, once the URLconf's modules are loaded.

_ADMIN = """
import logging

import django

logged = []


class Keep(logging.Handler):
    def emit(self, record):
        logged.append(record.getMessage())


logging.getLogger("neolith.host").addHandler(Keep())
django.setup()
print("set-up", len(logged))
from neolith import testing

testing.boot()
print(*logged, sep="\\n")
"""


@pytest.mark.spec("APP-26")
def test_boot_warns_about_an_admin_class_the_urlconf_registered() -> None:
    printed = in_built_host(
        _ADMIN,
        host_apps.HOSTED,
        {"NEOLITH": host_apps.NEOLITH, "ROOT_URLCONF": "tests.boot_urls"},
    )
    assert printed.splitlines() == [
        "set-up 0",
        "(neolith.W001) tests.boot_urls.ProjectAdmin: admin class in no installed "
        "app; its views have no calling app, and a contract call from them is "
        "refused",
    ]


# neolith.testing.boot.


def test_testing_boot_is_called_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's session fixture called it already."""
    with pytest.raises(UsageError, match="has booted already"):
        testing.boot()
    monkeypatch.setattr(_boot, "_kept", None)
    with (
        override_settings(INSTALLED_APPS=[]),
        pytest.raises(UsageError, match="not set up yet"),
    ):
        testing.boot()
    assert _boot._kept is None


def test_the_test_session_booted_without_reading_a_schema() -> None:
    kept = _boot._kept
    assert kept is not None
    assert kept.tests
    assert kept.refusal is None
    # The suite's default is in memory, which Boot's read would refuse; once
    # a test asks for the database, pytest-django names its test database.
    assert connections["default"].is_in_memory_db()


# No refusal hides another.

_ALL = """
import json
import sqlite3
import sys

import django

django.setup()

from neolith import _boot

ROOT, RECORDS = sys.argv[3:5]


def record(name, rows, schema=RECORDS):
    connection = sqlite3.connect(ROOT + "/" + name + ".sqlite3")
    connection.execute(schema)
    if rows:
        connection.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, "
            "'2026-01-01 00:00:00')",
            rows,
        )
    connection.commit()
    connection.close()


FILL
print(json.dumps(sorted([m.id, m.msg] for m in _boot.schema_messages())))
"""


def _schema(tmp_path: Path, hosted: list[str], fill: str) -> list[list[str]]:
    databases = {
        app.rpartition(".")[2]: {
            "ENGINE": SQLITE,
            "NAME": str(tmp_path / f"{app.rpartition('.')[2]}.sqlite3"),
        }
        for app in hosted
    }
    body = _ALL.replace("sys.argv[3:5]", repr([str(tmp_path), RECORDS])).replace(
        "FILL", fill
    )
    printed = in_built_host(body, hosted, {"NEOLITH": {"DATABASES": databases}})
    found: list[list[str]] = json.loads(printed.splitlines()[-1])
    return found


@pytest.mark.spec("APP-31", "APP-34", "APP-35")
def test_a_refused_alias_hides_no_check_of_the_code(tmp_path: Path) -> None:
    """E008 and E013 need no database, so an alias with no database still
    gets them, beside its own E018."""
    found = _schema(
        tmp_path,
        ["tests.boot_apps.fresh", "tests.boot_apps.bare", "tests.boot_apps.leaning"],
        'record("fresh", [("fresh", "0001_initial"), ("fresh", "0002_more")])',
    )
    assert [id_ for id_, _ in found] == [
        "neolith.E008",
        "neolith.E013",
        "neolith.E018",
        "neolith.E018",
    ]
    assert found[1][1] == "app leaning: migration 0001_initial depends on fresh"


@pytest.mark.spec("APP-34")
def test_one_app_s_broken_migrations_hide_no_other_app(tmp_path: Path) -> None:
    """Each app whose migrations do not load, or break the graph, gets its
    own E010; an app that depends on one keeps its own E013; the others are
    read as if the broken apps were absent."""
    apps = [
        "fresh",
        "empty",
        "dangling",
        "leandang",
        "importbroken",
        "noclass",
        "unknowndep",
    ]
    fill = "\n".join(f"record({app!r}, [])" for app in apps[1:])
    found = _schema(
        tmp_path,
        [f"tests.boot_apps.{app}" for app in apps],
        f'record("fresh", [("fresh", "0001_initial")])\n{fill}',
    )
    assert [(id_, msg.split(":")[0]) for id_, msg in found] == [
        ("neolith.E008", "app empty"),
        ("neolith.E009", "app fresh"),
        ("neolith.E009", "app leandang"),
        ("neolith.E010", "app dangling"),
        ("neolith.E010", "app importbroken"),
        ("neolith.E010", "app noclass"),
        ("neolith.E010", "app unknowndep"),
        ("neolith.E013", "app leandang"),
    ]
    causes = {msg.split(":")[0]: msg for id_, msg in found if id_ == "neolith.E010"}
    assert "NodeNotFoundError" in causes["app dangling"]
    assert "a migration module that does not import" in causes["app importbroken"]
    assert "BadMigrationError" in causes["app noclass"]
    assert "Dependency on unknown app: nosuchapp" in causes["app unknowndep"]
    assert [
        "neolith.E013",
        "app leandang: migration 0001_initial depends on dangling",
    ] in found


@pytest.mark.spec("APP-34")
def test_a_graph_no_app_can_be_given_stops_every_migration_read(
    tmp_path: Path,
) -> None:
    """Two apps whose migrations depend on each other: no one label is to
    blame, so one E010 stands for the graph, and no migration is read."""
    found = _schema(
        tmp_path,
        ["tests.boot_apps.cyca", "tests.boot_apps.cycb", "tests.boot_apps.fresh"],
        'record("fresh", [])\nrecord("cyca", [])\nrecord("cycb", [])',
    )
    assert [id_ for id_, _ in found] == ["neolith.E010"]
    assert found[0][1].startswith(
        "host: the migrations cannot be loaded: CircularDependencyError: "
    )


def test_a_database_that_cannot_be_read_is_refused_for_each_label_on_it(
    tmp_path: Path,
) -> None:
    found = _schema(
        tmp_path,
        ["tests.boot_apps.fresh", "tests.boot_apps.forked"],
        'record("fresh", [], schema="CREATE TABLE django_migrations (x INTEGER)")\n'
        'record("forked", [("forked", "0001_initial")])',
    )
    assert found[0] == [
        "neolith.E011",
        "app forked: conflicting migrations 0002_a, 0002_b",
    ]
    assert found[1][0] == "neolith.E012"
    assert found[1][1].startswith("app fresh: database fresh cannot be read: ")


# E010 is Django's own judgement, on the rows Django reads.

_SQUASHED = """
import itertools
import json
import sqlite3
import sys

import django

django.setup()

from django.db import connections
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

from neolith import _boot

ROOT, RECORDS = sys.argv[3:5]
PATH = ROOT + "/squashed.sqlite3"
NAMES = ["0001_initial", "0002_two", "0001_squashed_0002_two", "0003_three"]
found = []
for size in range(len(NAMES) + 1):
    for rows in itertools.combinations(NAMES, size):
        connection = sqlite3.connect(PATH)
        connection.execute(RECORDS)
        connection.execute("DELETE FROM django_migrations")
        connection.executemany(
            "INSERT INTO django_migrations (app, name, applied) VALUES "
            "('squashed', ?, '2026-01-01 00:00:00')",
            [(name,) for name in rows],
        )
        connection.commit()
        connection.close()
        boot = [
            [m.id, m.msg]
            for m in _boot.schema_messages()
            if m.id in ("neolith.E009", "neolith.E010")
        ]
        connection = connections["squashed"]
        try:
            MigrationLoader(connection).check_consistent_history(connection)
        except InconsistentMigrationHistory as error:
            django_says = [
                ["neolith.E010", "app squashed: database squashed: " + str(error)]
            ]
        else:
            executor = MigrationExecutor(connection)
            plan = [
                m.name
                for m, backwards in executor.migration_plan(
                    executor.loader.graph.leaf_nodes("squashed")
                )
                if not backwards
            ]
            django_says = [
                [
                    "neolith.E009",
                    f"app squashed: database squashed has {len(plan)} unapplied "
                    f"migrations, the first is {plan[0]}; run the migrate step",
                ]
            ] if plan else []
        connection.close()
        found.append([list(rows), boot, django_says])
print(json.dumps(found))
"""


@pytest.mark.spec("APP-33", "APP-34")
def test_boot_judges_a_history_as_django_does_over_every_set_of_rows(
    tmp_path: Path,
) -> None:
    """Every subset of a squashed app's rows: Boot refuses exactly the
    histories Django's ``check_consistent_history`` refuses (E010), and
    finds unapplied exactly what Django's own planner would apply (E009)."""
    path = tmp_path / "squashed.sqlite3"
    record(path, [])
    body = _SQUASHED.replace("sys.argv[3:5]", repr([str(tmp_path), RECORDS]))
    printed = in_built_host(
        body,
        ["tests.boot_apps.squashed"],
        {"NEOLITH": {"DATABASES": {"squashed": {"ENGINE": SQLITE, "NAME": str(path)}}}},
    )
    cases = json.loads(printed.splitlines()[-1])
    assert len(cases) == 16
    for rows, boot, django_says in cases:
        assert boot == django_says, rows
    # Every outcome occurs, so the comparison is not empty.
    ids = {found[0] for _, boot, _ in cases for found in boot}
    assert ids == {"neolith.E009", "neolith.E010"}
    assert any(not boot for _, boot, _ in cases)


# Boot cannot hang, and always keeps an outcome.


def test_a_handler_built_while_the_urlconf_loads_is_refused(tmp_path: Path) -> None:
    record(tmp_path / "fresh.sqlite3", FRESH)
    lines = _handler(tmp_path, ROOT_URLCONF="tests.reentrant_urls")
    assert lines[0][:3] == ["1", "ImproperlyConfigured", "host: the host cannot boot:"]
    assert lines[0][3] == (
        "settings: ROOT_URLCONF tests.reentrant_urls cannot be imported: "
        "ImproperlyConfigured: host: a handler was built while the host was "
        "booting, such as by the URLconf as it was imported; build handlers once "
        "Boot has ended"
    )


def test_a_handler_built_by_a_migration_module_is_refused(tmp_path: Path) -> None:
    """The migrations are read on Boot's own thread, which the guard covers
    too: the module's import fails, and only its app is refused for it."""
    lines = _handler(tmp_path, extra="handlerbuilt")
    assert lines[0][:3] == ["1", "ImproperlyConfigured", "host: the host cannot boot:"]
    assert any(
        line.startswith(
            "(neolith.E010) app handlerbuilt: its migrations cannot be loaded: "
            "ImproperlyConfigured: host: a handler was built while the host was "
            "booting"
        )
        for line in lines[0][3:]
    ), lines[0]


def test_testing_boot_inside_boot_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def run(*, schema: bool) -> str | None:
        try:
            testing.boot()
        except ImproperlyConfigured as error:
            ran.append(str(error))
        return None

    monkeypatch.setattr(_boot, "_kept", None)
    monkeypatch.setattr(_boot, "_run", run)
    worker = threading.Thread(target=_boot.boot, daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), "Boot waited on itself"
    assert ran == [
        "host: a handler was built while the host was booting, such as by the "
        "URLconf as it was imported; build handlers once Boot has ended"
    ]


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_whatever_ends_boot_leaves_a_kept_outcome(
    monkeypatch: pytest.MonkeyPatch, raised: type[BaseException]
) -> None:
    def run(*, schema: bool) -> str | None:
        raise raised("stopped")

    monkeypatch.setattr(_boot, "_kept", None)
    monkeypatch.setattr(_boot, "_run", run)
    expected = ImproperlyConfigured if issubclass(raised, Exception) else raised
    with pytest.raises(expected):
        _boot.boot()
    kept = _boot._kept
    assert kept is not None
    assert kept.refusal is not None
    assert not kept.tests
    assert f"{raised.__qualname__}: stopped" in kept.refusal
    with pytest.raises(ImproperlyConfigured, match="stopped"):
        _boot.boot()


def test_a_stopped_test_boot_is_kept_as_a_test_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(*, schema: bool) -> str | None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_boot, "_kept", None)
    monkeypatch.setattr(_boot, "_run", run)
    with pytest.raises(KeyboardInterrupt):
        testing.boot()
    kept = _boot._kept
    assert kept is not None
    assert kept.tests


@pytest.mark.spec("APP-1")
def test_a_urlconf_refusal_hides_no_schema_refusal(tmp_path: Path) -> None:
    record(tmp_path / "fresh.sqlite3", [("fresh", "0001_initial")])
    lines = _handler(tmp_path, ROOT_URLCONF="tests.no_such_urls")
    assert lines[0][2:] == [
        "host: the host cannot boot:",
        "settings: ROOT_URLCONF tests.no_such_urls cannot be imported: "
        "ModuleNotFoundError: No module named 'tests.no_such_urls'",
        "(neolith.E009) app fresh: database fresh has 1 unapplied migrations, the "
        "first is 0002_more; run the migrate step",
    ]


# neolith.W001 at set-up and again at Boot: each registration warned once,
# or never when silenced.

_WARNED = """
import json
import logging

import django
from django.core.handlers.wsgi import WSGIHandler

logged = []


class Keep(logging.Handler):
    def emit(self, record):
        logged.append(record.getMessage())


logging.getLogger("neolith.host").addHandler(Keep())
django.setup()
at_set_up = list(logged)
WSGIHandler()
WSGIHandler()
print(json.dumps([at_set_up, logged[len(at_set_up):]]))
"""


@pytest.mark.spec("APP-17", "APP-26")
@pytest.mark.parametrize("silenced", [False, True])
def test_each_admin_registration_is_warned_once_or_silenced(
    tmp_path: Path, silenced: bool
) -> None:
    path = tmp_path / "default.sqlite3"
    migrated(path, "contenttypes")
    printed = in_built_host(
        _WARNED,
        host_apps.HOSTED,
        {
            "NEOLITH": {
                "DATABASES": {"default": {"ENGINE": SQLITE, "NAME": str(path)}},
                "UNDECLARED_APPS": ["django.contrib.contenttypes", "tests.boot_admin"],
            },
            "ROOT_URLCONF": "tests.boot_urls",
            "SILENCED_SYSTEM_CHECKS": ["neolith.W001"] if silenced else [],
        },
    )
    at_set_up, at_boot = json.loads(printed.splitlines()[-1])
    warning = (
        "(neolith.W001) tests.{}: admin class in no installed app; its views have "
        "no calling app, and a contract call from them is refused"
    )
    if silenced:
        assert (at_set_up, at_boot) == ([], [])
    else:
        assert at_set_up == [warning.format("boot_project_admin.LoadingAdmin")]
        assert at_boot == [warning.format("boot_urls.ProjectAdmin")]


@pytest.mark.spec("APP-34")
def test_conflicting_migrations_of_an_undeclared_app_on_default_are_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "default.sqlite3"
    migrated(path, "contenttypes")
    record(path, [("boot_undeclared", "0001_initial")])
    body = _ALL.replace("sys.argv[3:5]", repr([str(tmp_path), RECORDS])).replace(
        "FILL", ""
    )
    printed = in_built_host(
        body,
        host_apps.HOSTED,
        {
            "NEOLITH": {
                "DATABASES": {"default": {"ENGINE": SQLITE, "NAME": str(path)}},
                "UNDECLARED_APPS": [
                    "django.contrib.contenttypes",
                    "tests.boot_undeclared",
                ],
            }
        },
    )
    assert json.loads(printed.splitlines()[-1]) == [
        [
            "neolith.E011",
            "app boot_undeclared: conflicting migrations 0002_a, 0002_b",
        ]
    ]


# E013 reads every edge Django's graph holds between two labels.

_EDGES = """
import json

import django

django.setup()

from neolith import _boot

edges = [m.msg for m in _boot.schema_messages() if m.id == "neolith.E013"]
print(json.dumps(sorted(edges)))
"""


@pytest.mark.spec("APP-35")
def test_every_edge_between_two_apps_is_refused(tmp_path: Path) -> None:
    """Each source of an edge in Django's graph: a dependency by name, by
    ``__latest__``, by ``__first__`` and through a swappable setting, and a
    ``run_before``, from either side; a squash keeps to its own app. A
    dependency on an app with no migrations makes no edge, and is not one."""
    apps = ["fresh", "pusher", "latest", "firstdep", "swapper", "unmigdep", "squashed"]
    printed = in_built_host(
        _EDGES,
        [f"tests.boot_apps.{app}" for app in apps],
        {
            "NEOLITH": {
                "DATABASES": {
                    alias: {"ENGINE": SQLITE, "NAME": str(tmp_path / alias)}
                    for alias in ["default", *apps]
                },
                "UNDECLARED_APPS": [
                    "django.contrib.contenttypes",
                    "django.contrib.auth",
                    "tests.plain_app",
                ],
            },
        },
    )
    assert json.loads(printed.splitlines()[-1]) == [
        "app firstdep: migration 0001_initial depends on fresh",
        "app fresh: migration 0001_initial depends on pusher",
        "app latest: migration 0001_initial depends on fresh",
        "app pusher: migration 0001_initial runs before fresh",
        "app swapper: migration 0001_initial depends on auth",
    ]
