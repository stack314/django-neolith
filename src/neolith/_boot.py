"""Boot: the checks a host runs when a handler is built (ADR 0022 Decision 7).

The root middleware's constructor calls ``boot()`` after checking its own
place. Boot runs once per process: it checks that ``ROOT_URLCONF`` is set and
imports, runs ``neolith.W001`` again now that the URLconf's modules are
loaded, warning about each admin that set-up did not warn about, and reads
every schema (``schema_messages``). Each check runs whatever the others
found, and every refusal is reported together. The outcome is kept, a
refusal included, whatever ends the run, so a later handler gets the same
answer without a second read, and concurrent constructions share one run
behind a lock; a handler built on the thread that is booting, such as by the
URLconf as it is imported, is refused. ``neolith.testing.boot()`` runs Boot
without the schema read.

The schema read, with the ids of ADR 0022's Boot table:

- ``neolith.E007``: a SQLite database held in memory or in a temporary file;
- ``neolith.E008``: a hosted app with a database and no migrations, a
  migrations package with none in it included;
- ``neolith.E009``: unapplied migrations of a label on its alias;
- ``neolith.E010``: an inconsistent migration history, judged as Django
  judges it and read per label, or an app's migrations that cannot be
  loaded;
- ``neolith.E011``: conflicting migrations;
- ``neolith.E012``: a database that cannot be read;
- ``neolith.E013``: a hosted app's migration that depends on another label;
- ``neolith.E018``: a SQLite alias with no database, or one with no schema.

The checks of the code (E008, E011, E013) need no database and run for every
hosted app; an app whose own migrations break Django's graph is left out of
the graph, so it hides no other app. SQLite is known by the backend's
vendor, so a project's subclass of Django's backend is judged the same.

It runs on a thread of its own and closes, on that thread, every connection
it opened. It writes nothing of its own: for a SQLite alias it opens no
connection to a path that holds no file, or an empty one, and it judges a
SQLite alias's state on a connection of its own before Django's connection
to the alias reads any migration.

State the module owns (CONTRIBUTING K6): the kept outcome, behind one lock.
"""

import contextlib
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any, Final

from django.apps import apps as global_apps
from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.migrations.exceptions import NodeNotFoundError
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.utils import load_backend

from neolith import _checks, _compat, _databases
from neolith._context import UsageError
from neolith._declaration import declaration_of

__all__ = ["boot", "boot_for_tests", "schema_messages", "state_messages"]

_log = logging.getLogger("neolith.host")
_LOCK: Final = threading.Lock()
# Set on the thread that runs Boot, so a handler built while Boot runs on
# that thread, such as one the URLconf builds at import, is refused instead
# of waiting on the lock its own thread holds.
_BOOTING: Final = threading.local()
_SHOWN: Final = 300
_NO_FILE: Final = (
    "the database is in memory, or nothing has created it yet (the migrate "
    "step creates the aliases it migrates)"
)
_IN_MEMORY: Final = (
    "an in-memory SQLite database is empty on a new thread, or gone once its "
    "last connection closes; a host that serves needs a file or a server"
)


class _Kept:
    """The outcome of the process's one Boot: the refusal's message, or
    ``None`` when Boot passed, and whether the schema read was left out.
    """

    __slots__ = ("refusal", "tests")

    def __init__(self, refusal: str | None, *, tests: bool) -> None:
        self.refusal = refusal or None
        self.tests = tests


_kept: _Kept | None = None


def _cause(error: BaseException) -> str:
    try:
        text = str(error)
    except Exception:
        text = "<the message cannot be read>"
    if len(text) > _SHOWN:
        text = f"{text[:_SHOWN]}..."
    return f"{type(error).__qualname__}: {text}"


def boot() -> None:
    """Boot the host, once per process, and raise the kept refusal.

    Raises:
        ImproperlyConfigured: Boot refused, now or earlier in this process,
            or a handler is built while Boot runs on the same thread.
    """
    global _kept
    _not_booting()
    with _LOCK:
        if _kept is None:
            _kept = _Kept(_kept_run(schema=True), tests=False)
        kept = _kept
    if kept.refusal is not None:
        raise ImproperlyConfigured(kept.refusal)


def _not_booting() -> None:
    if getattr(_BOOTING, "active", False):
        raise ImproperlyConfigured(
            "host: a handler was built while the host was booting, such as by "
            "the URLconf as it was imported; build handlers once Boot has ended"
        )


def _kept_run(*, schema: bool) -> str:
    """Run Boot and return its refusal, or ``""`` when it passed. Whatever
    ends the run, an outcome is kept: an unforeseen error, or a signal to
    stop, becomes the refusal, and the signal goes on.
    """
    _BOOTING.active = True
    try:
        return _run(schema=schema) or ""
    except Exception as error:
        return _refusal([f"host: Boot failed: {_cause(error)}"])
    except BaseException as error:
        global _kept
        _kept = _Kept(
            _refusal([f"host: Boot was stopped: {_cause(error)}"]), tests=not schema
        )
        raise
    finally:
        _BOOTING.active = False


def boot_for_tests() -> None:
    """Boot without the schema read, for a test session.

    Raises:
        UsageError: The host is not set up, or Boot has run in this process.
        ImproperlyConfigured: The checks Boot still runs refused.
    """
    global _kept
    from neolith._host import _current

    if _current() is None:
        raise UsageError(
            "neolith.testing.boot(): the host is not set up yet; call it once "
            "Django has loaded the apps, such as in a session fixture"
        )
    _not_booting()
    with _LOCK:
        if _kept is not None:
            raise UsageError(
                "neolith.testing.boot(): the host has booted already in this "
                "process; call it once, before the first handler is built"
            )
        _kept = _Kept(_kept_run(schema=False), tests=True)
        kept = _kept
    if kept.refusal is not None:
        raise ImproperlyConfigured(kept.refusal)


def _run(*, schema: bool) -> str | None:
    """Run every check of Boot, each whatever the others found; return the
    refusal's message, or ``None``.
    """
    problems = _urlconf_problems()
    _warn_again()
    found = schema_messages() if schema else []
    errors = sorted(f"({m.id}) {m.msg}" for m in found if m.level >= checks.ERROR)
    lines = sorted(problems) + errors
    return _refusal(lines) if lines else None


def _refusal(lines: list[str]) -> str:
    refusal = "host: the host cannot boot:\n" + "\n".join(lines)
    with contextlib.suppress(Exception):
        _log.error("%s", refusal)
    return refusal


def _urlconf_problems() -> list[str]:
    value = getattr(settings, "ROOT_URLCONF", None)
    if value is None:
        return [
            "settings: ROOT_URLCONF is not set; this host is about to serve requests"
        ]
    if type(value) is not str:
        return [
            f"settings: ROOT_URLCONF: expected a str, got {type(value).__qualname__}"
        ]
    shown = value if len(value) <= _SHOWN else f"{value[:_SHOWN]}..."
    try:
        import_module(value)
    except Exception as error:
        return [f"settings: ROOT_URLCONF {shown} cannot be imported: {_cause(error)}"]
    return []


def _warn_again() -> None:
    """Log each ``neolith.W001`` that set-up did not log, unless silenced: an
    admin registered while the URLconf's modules loaded.
    """
    from neolith._host import _current

    host = _current()
    logged = host.warned if host is not None else frozenset()
    for message in _checks.admin_check():
        if message.msg in logged or message.is_silenced():
            continue
        with contextlib.suppress(Exception):
            _log.warning("(%s) %s", message.id, message.msg)


def schema_messages() -> list[checks.CheckMessage]:
    """Read each database of ``NEOLITH["DATABASES"]`` and each schema, and
    return what is refused.

    The hosted apps and the apps without a declaration are those installed
    in Django's global registry. Runs on a thread of its own and closes, on
    that thread, every connection it opened.
    """
    neolith = getattr(settings, "NEOLITH", {})
    named = neolith.get("DATABASES", {}) if type(neolith) is dict else {}
    databases = {alias: dict(connections.settings[alias]) for alias in named}
    hosted: list[str] = []
    undeclared: list[str] = []
    for config in global_apps.get_app_configs():
        declaration = declaration_of(type(config))
        if declaration is None:
            undeclared.append(config.label)
        elif "database" in declaration.resources:
            hosted.append(config.label)
    outcome: list[list[checks.CheckMessage]] = []
    failure: list[BaseException] = []

    def read() -> None:
        try:
            outcome.append(_read(databases, hosted, undeclared))
        except BaseException as error:  # handed to the caller's thread
            failure.append(error)

    thread = threading.Thread(target=read, name="neolith-boot")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return outcome[0]


def state_messages(
    databases: Mapping[str, Mapping[str, Any]],
) -> tuple[list[checks.CheckMessage], list[str]]:
    """Judge the state of each alias before any migration is read: what is
    refused, and the aliases whose migrations can be read.

    ``databases`` maps each alias to its connection settings, with Django's
    defaults filled in. Each SQLite alias is opened, when it is opened at
    all, on a connection of its own that is closed before this returns.
    """
    messages: list[checks.CheckMessage] = []
    readable = [
        alias
        for alias, entry in databases.items()
        if _state(alias, dict(entry), messages)
    ]
    return messages, readable


def _read(
    databases: Mapping[str, dict[str, Any]],
    hosted: Sequence[str],
    undeclared: Sequence[str],
) -> list[checks.CheckMessage]:
    """Every check of the schema read, each whatever the others found: the
    checks of the code first, which need no database, then each alias's
    state, then the migrations recorded on each alias that can be read.
    """
    messages: list[checks.CheckMessage] = []
    try:
        code, broken, failure = _code_graph()
        if failure is not None:
            messages.append(
                checks.Error(
                    f"host: the migrations cannot be loaded: {failure}",
                    id="neolith.E010",
                )
            )
        for label in hosted:
            if label in broken:
                messages.append(_unloadable(label, broken[label]))
            elif code is not None:
                messages.extend(_code_messages(code, label))
        found, readable = state_messages(databases)
        messages.extend(found)
        if code is None:
            return messages
        for alias in readable:
            if alias == _databases.DEFAULT:
                labels = [
                    label
                    for label in undeclared
                    if label not in broken and _has_nodes(code, label)
                ]
                for label in undeclared:
                    if label in broken:
                        messages.append(_unloadable(label, broken[label]))
                for label in labels:
                    messages.extend(_conflicts(code, label))
            else:
                labels = [alias] if alias in hosted and alias not in broken else []
                labels = [label for label in labels if _has_nodes(code, label)]
            messages.extend(_recorded(alias, labels, frozenset(broken)))
    finally:
        # Django keeps a connection per alias per thread: these are this
        # thread's, which no other code uses.
        for alias in databases:
            _close(connections[alias])
    return messages


class _Loader(MigrationLoader):
    """Django's migration loader, leaving out the migrations of the labels
    in ``excluded``, so that one app's broken graph hides no other app.
    """

    def __init__(self, connection: Any, excluded: frozenset[str]) -> None:  # noqa: ANN401
        self._excluded = excluded
        super().__init__(connection)

    def load_disk(self) -> None:
        super().load_disk()
        for key in [k for k in self.disk_migrations if k[0] in self._excluded]:
            del self.disk_migrations[key]


def _origin(error: BaseException) -> str | None:
    """The label of the migration whose dependency is missing, from Django's
    ``NodeNotFoundError`` or the one it was raised from.
    """
    for found in (error, error.__cause__):
        origin = getattr(found, "origin", None)
        label = getattr(origin, "app_label", None)
        if type(label) is str:
            return label
        if type(origin) is tuple and origin and type(origin[0]) is str:
            first: str = origin[0]
            return first
    return None


def _code_graph() -> tuple[MigrationLoader | None, dict[str, str], str | None]:
    """The migration graph of the installed apps, read from the code alone;
    the labels left out because their own migrations break the graph, each
    with the cause; and a cause that no label can be given, which leaves no
    graph.
    """
    broken: dict[str, str] = {}
    for _ in range(len(list(global_apps.get_app_configs())) + 1):
        try:
            return _Loader(None, frozenset(broken)), broken, None
        except NodeNotFoundError as error:
            label = _origin(error)
            if label is None or label in broken:
                return None, broken, _cause(error)
            broken[label] = _cause(error)
        except Exception as error:
            return None, broken, _cause(error)
    return None, broken, "the graph could not be built"


def _unloadable(label: str, cause: str) -> checks.CheckMessage:
    return checks.Error(
        f"app {label}: its migrations cannot be loaded: {cause}", id="neolith.E010"
    )


def _has_nodes(graph_of: MigrationLoader, label: str) -> bool:
    return any(key[0] == label for key in graph_of.graph.nodes)


def _code_messages(code: MigrationLoader, label: str) -> list[checks.CheckMessage]:
    """The checks of a hosted app's migrations that need no database: some
    exist (a package with no migration in it counts as none), none depends
    on another label, and no two conflict.
    """
    where = f"app {label}"
    if not _has_nodes(code, label):
        return [
            checks.Error(
                f"{where}: has a database and no migrations", id="neolith.E008"
            )
        ]
    graph = code.graph
    messages = [
        checks.Error(
            f"{where}: migration {key[1]} depends on {parent.key[0]}",
            id="neolith.E013",
        )
        for key in sorted(k for k in graph.nodes if k[0] == label)
        for parent in sorted(graph.node_map[key].parents, key=lambda n: n.key)
        if parent.key[0] != label
    ]
    return messages + _conflicts(code, label)


def _conflicts(code: MigrationLoader, label: str) -> list[checks.CheckMessage]:
    conflicts = code.detect_conflicts()
    if label not in conflicts:
        return []
    names = ", ".join(sorted(conflicts[label]))
    return [
        checks.Error(f"app {label}: conflicting migrations {names}", id="neolith.E011")
    ]


def _recorded(
    alias: str, labels: Sequence[str], broken: frozenset[str]
) -> list[checks.CheckMessage]:
    """Read the migrations recorded on ``alias`` for each of ``labels``:
    the history is consistent, judged as Django judges it on the recorded
    rows, and nothing is unapplied.
    """
    if not labels:
        return []
    connection = connections[alias]
    try:
        loader = _Loader(connection, broken)
        rows = MigrationRecorder(connection).applied_migrations()
    except Exception as error:
        return [
            checks.Error(
                f"app {label}: database {alias} cannot be read: {_cause(error)}",
                id="neolith.E012",
            )
            for label in labels
        ]
    messages: list[checks.CheckMessage] = []
    for label in labels:
        if label in loader.detect_conflicts():
            continue
        inconsistent = _inconsistency(loader, label, alias, rows)
        if inconsistent is not None:
            messages.append(
                checks.Error(
                    f"app {label}: database {alias}: {inconsistent}", id="neolith.E010"
                )
            )
            continue
        plan = _unapplied(loader, label)
        if plan:
            messages.append(
                checks.Error(
                    f"app {label}: database {alias} has {len(plan)} unapplied "
                    f"migrations, the first is {plan[0]}; run the migrate step",
                    id="neolith.E009",
                )
            )
    return messages


def _unapplied(loader: MigrationLoader, label: str) -> list[str]:
    """The names of the label's migrations that a forward migrate to its
    leaves would apply, in order, as ``MigrationExecutor.migration_plan``
    plans forwards: each node on the way to a leaf that is not applied.
    """
    applied = loader.applied_migrations
    plan: list[tuple[str, str]] = []
    for target in loader.graph.leaf_nodes(label):
        for key in loader.graph.forwards_plan(target):
            if key not in applied and key not in plan:
                plan.append(key)
    return [name for app, name in plan if app == label]


def _state(
    alias: str, entry: dict[str, Any], messages: list[checks.CheckMessage]
) -> bool:
    """Judge one alias before any migration is read; return whether its
    migrations can be read.
    """
    if not _databases.is_sqlite(entry.get("ENGINE")):
        return True
    name = entry.get("NAME")
    path = _databases.sqlite_path(name)
    if path is None:
        messages.append(
            checks.Error(f"database {alias}: {_IN_MEMORY}", id="neolith.E007")
        )
        return False
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        messages.append(
            checks.Error(
                f"database {alias}: no database at {path or repr(name)}; {_NO_FILE}",
                id="neolith.E018",
            )
        )
        return False
    wrapper = load_backend(entry["ENGINE"]).DatabaseWrapper(entry, alias)
    try:
        with wrapper.cursor() as cursor:
            cursor.execute("PRAGMA database_list")
            main = [row[2] for row in cursor.fetchall() if row[1] == "main"]
            if not main or not main[0]:
                messages.append(
                    checks.Error(f"database {alias}: {_IN_MEMORY}", id="neolith.E007")
                )
                return False
            cursor.execute("SELECT count(*) FROM sqlite_master")
            (entries,) = cursor.fetchone()
    except Exception as error:
        messages.append(_cannot_read(alias, error))
        return False
    finally:
        _close(wrapper)
    if not entries:
        messages.append(
            checks.Error(
                f"database {alias}: no database at {path}; {_NO_FILE}",
                id="neolith.E018",
            )
        )
        return False
    return True


def _close(wrapper: Any) -> None:  # noqa: ANN401
    """Close a connection of Boot's own. Django's SQLite backend leaves an
    in-memory database open on ``close()``, so the connection it holds is
    closed as well.
    """
    with contextlib.suppress(Exception):
        wrapper.close()
    raw = wrapper.connection
    if raw is not None:
        with contextlib.suppress(Exception):
            raw.close()
        wrapper.connection = None


def _cannot_read(alias: str, error: BaseException) -> checks.CheckMessage:
    return checks.Error(
        f"database {alias} cannot be read: {_cause(error)}", id="neolith.E012"
    )


def _inconsistency(
    loader: MigrationLoader,
    label: str,
    alias: str,
    applied: Any,  # noqa: ANN401
) -> str | None:
    """Django's ``check_consistent_history``, read for one label: an applied
    migration of the label whose dependency is not applied, judged on the
    recorded rows, as Django judges it. Rows of other labels are left alone
    (ADR 0009 Decision 7).
    """
    graph = loader.graph
    for key in applied:
        if key[0] != label or key not in graph.nodes:
            continue
        for parent in graph.node_map[key].parents:
            if parent.key in applied:
                continue
            if _compat.all_replaced_applied(loader, parent.key, applied):
                continue
            return (
                f"Migration {key[0]}.{key[1]} is applied before its dependency "
                f"{parent.key[0]}.{parent.key[1]} on database '{alias}'."
            )
    return None
