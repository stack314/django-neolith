"""Boot: the checks a host runs when a handler is built (ADR 0022 Decision 7).

The root middleware's constructor calls ``boot()`` after checking its own
place. Boot runs once per process: it checks that ``ROOT_URLCONF`` is set and
imports, runs ``neolith.W001`` again now that the URLconf's modules are
loaded, and reads every schema (``schema_messages``). The outcome is kept, a
refusal included, so a later handler gets the same answer without a second
read, and concurrent constructions share one run behind a lock.
``neolith.testing.boot()`` runs Boot without the schema read.

The schema read, with the ids of ADR 0022's Boot table:

- ``neolith.E007``: a SQLite database held in memory or in a temporary file;
- ``neolith.E008``: a hosted app with a database and no migrations;
- ``neolith.E009``: unapplied migrations of a label on its alias;
- ``neolith.E010``: an inconsistent migration history, read per label;
- ``neolith.E011``: conflicting migrations;
- ``neolith.E012``: a database that cannot be read;
- ``neolith.E013``: a hosted app's migration that depends on another label;
- ``neolith.E018``: a SQLite alias with no database, or one with no schema.

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
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.utils import load_backend

from neolith import _checks, _compat, _databases
from neolith._context import UsageError
from neolith._declaration import declaration_of

__all__ = ["boot", "boot_for_tests", "schema_messages", "state_messages"]

_log = logging.getLogger("neolith.host")
_LOCK: Final = threading.Lock()
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
        self.refusal = refusal
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
        ImproperlyConfigured: Boot refused, now or earlier in this process.
    """
    global _kept
    with _LOCK:
        if _kept is None:
            _kept = _Kept(_run(schema=True), tests=False)
        kept = _kept
    if kept.refusal is not None:
        raise ImproperlyConfigured(kept.refusal)


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
    with _LOCK:
        if _kept is not None:
            raise UsageError(
                "neolith.testing.boot(): the host has booted already in this "
                "process; call it once, before the first handler is built"
            )
        _kept = _Kept(_run(schema=False), tests=True)
        kept = _kept
    if kept.refusal is not None:
        raise ImproperlyConfigured(kept.refusal)


def _run(*, schema: bool) -> str | None:
    """Run Boot's checks; return the refusal's message, or ``None``."""
    problems = _urlconf_problems()
    found: list[checks.CheckMessage] = []
    if not problems and schema:
        found = schema_messages()
    _warn_again()
    errors = sorted(f"({m.id}) {m.msg}" for m in found if m.level >= checks.ERROR)
    lines = sorted(problems) + errors
    if not lines:
        return None
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
    messages, readable = state_messages(databases)
    try:
        executors: dict[str, MigrationExecutor | str] = {}
        for label in hosted:
            if label in readable:
                messages.extend(_label(label, label, executors, hosted=True))
        if _databases.DEFAULT in readable:
            for label in undeclared:
                messages.extend(
                    _label(label, _databases.DEFAULT, executors, hosted=False)
                )
    finally:
        # Django keeps a connection per alias per thread: these are this
        # thread's, which no other code uses.
        for alias in databases:
            with contextlib.suppress(Exception):
                connections[alias].close()
    return messages


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


def _executor(
    alias: str, executors: dict[str, MigrationExecutor | str]
) -> MigrationExecutor | str:
    """One migration executor per alias, or why it cannot be made, made
    once and shared by the labels read there.
    """
    if alias not in executors:
        try:
            executors[alias] = MigrationExecutor(connections[alias])
        except Exception as error:
            executors[alias] = _cause(error)
    return executors[alias]


def _label(
    label: str,
    alias: str,
    executors: dict[str, MigrationExecutor | str],
    *,
    hosted: bool,
) -> list[checks.CheckMessage]:
    """Read one label's schema on its alias (ADR 0009 Decision 7)."""
    where = f"app {label}"
    executor = _executor(alias, executors)
    if not isinstance(executor, MigrationExecutor):
        return [
            checks.Error(
                f"{where}: database {alias} cannot be read: {executor}",
                id="neolith.E012",
            )
        ]
    loader = executor.loader
    if label not in loader.migrated_apps:
        if hosted:
            return [
                checks.Error(
                    f"{where}: has a database and no migrations", id="neolith.E008"
                )
            ]
        return []
    messages: list[checks.CheckMessage] = []
    graph = loader.graph
    if hosted:
        for key in sorted(k for k in graph.nodes if k[0] == label):
            for parent in sorted(graph.node_map[key].parents, key=lambda n: n.key):
                if parent.key[0] != label:
                    messages.append(
                        checks.Error(
                            f"{where}: migration {key[1]} depends on {parent.key[0]}",
                            id="neolith.E013",
                        )
                    )
    conflicts = loader.detect_conflicts()
    if label in conflicts:
        names = ", ".join(sorted(conflicts[label]))
        messages.append(
            checks.Error(f"{where}: conflicting migrations {names}", id="neolith.E011")
        )
        return messages
    try:
        applied = loader.applied_migrations
        inconsistent = _inconsistency(loader, label, alias, applied)
        if inconsistent is not None:
            messages.append(
                checks.Error(
                    f"{where}: database {alias}: {inconsistent}", id="neolith.E010"
                )
            )
            return messages
        plan = [
            migration
            for migration, _ in executor.migration_plan(graph.leaf_nodes(label))
            if migration.app_label == label
        ]
    except Exception as error:
        messages.append(
            checks.Error(
                f"{where}: database {alias} cannot be read: {_cause(error)}",
                id="neolith.E012",
            )
        )
        return messages
    if plan:
        messages.append(
            checks.Error(
                f"{where}: database {alias} has {len(plan)} unapplied migrations, "
                f"the first is {plan[0].name}; run the migrate step",
                id="neolith.E009",
            )
        )
    return messages


def _inconsistency(
    loader: MigrationLoader,
    label: str,
    alias: str,
    applied: Any,  # noqa: ANN401
) -> str | None:
    """Django's ``check_consistent_history``, read for one label: an applied
    migration of the label whose dependency is not applied. Rows of other
    labels are left alone (ADR 0009 Decision 7).
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
