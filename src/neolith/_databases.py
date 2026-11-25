"""The deployer's databases, ``NEOLITH["DATABASES"]`` (ADR 0009, ADR 0022).

The rows of ADR 0009's admitted set that can be judged before Django
imports a model: which aliases exist, the connection settings of each, and
that no two aliases name one database. One function judges the mapping, and
the builder and set-up both run it; another builds ``DATABASES`` from a
mapping that passed.

What is compared for "one database" (ADR 0009 Decision 5):

- SQLite, a backend whose vendor is ``sqlite``: a path. A plain ``NAME``,
  a ``str`` or a path, is made absolute from the working directory, with
  symbolic links followed. A ``file:`` URI gives its path, percent-decoded
  and resolved the same way, with the query ignored, since Django opens
  SQLite with ``uri=True``. Only the literal ``:memory:`` is exempt, being
  one database per connection.
- Any other backend: ``(ENGINE, HOST, PORT, NAME)``, with ``HOST`` and
  ``PORT`` read as Django's backends read them (ADR 0034 Decision 2).

A refusal names the alias, or the two aliases, and the rule. It never shows
a value under an alias, in whole or in part (ADR 0033 Decision 7, ADR 0034).
"""

import os
import pathlib
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any, Final, cast

from neolith import _naming

__all__ = [
    "DEFAULT",
    "MEMORY",
    "built",
    "databases_problems",
    "is_sqlite",
    "sqlite_path",
]

DEFAULT: Final = "default"
MEMORY: Final = ":memory:"
_DUMMY: Final = frozenset({"django.db.backends.dummy", "django.db.backends."})
_KEY: Final = 'NEOLITH["DATABASES"]'
# The refusal of two aliases on one database names the rule, never a value
# compared (ADR 0034): those are connection settings.
_ONE_FILE: Final = "both name one SQLite file; each alias needs a database of its own"
_ONE_SERVER: Final = (
    "both name one database, with the same ENGINE, HOST, PORT and NAME; each "
    "alias needs a database of its own"
)


def _type(value: object) -> str:
    return _naming.type_name(value)


def _shown(value: object) -> str:
    """An alias, a key the deployer chose, for a message: an exact ``str``
    within a bound, and anything else by its type, so no author code runs.
    """
    if type(value) is str:
        text: str = value
        return text if len(text) <= 160 else f"{text[:160]}..."
    return f"a value of type {_type(value)}"


def is_sqlite(engine: object) -> bool:
    """Whether ``engine`` names a backend that opens a SQLite file: one whose
    connection class says its vendor is ``sqlite``, as Django's own, GeoDjango's
    and a project's subclass of either do. The backend is imported, not
    connected. An engine that cannot be imported is Django's to report when
    it connects, and is compared as a server.
    """
    if type(engine) is not str:
        return False
    from django.db.utils import load_backend

    try:
        wrapper = load_backend(engine).DatabaseWrapper
    except Exception:  # Django reports it at the first connection
        return False
    return getattr(wrapper, "vendor", None) == "sqlite"


def _is_memory(name: object) -> bool:
    """The literal ``:memory:``, as a ``str`` or a path."""
    if type(name) is str:
        return name == MEMORY
    return _naming.is_a(name, pathlib.PurePath) and str(name) == MEMORY


def sqlite_path(name: object) -> str | None:
    """The path a SQLite ``NAME`` opens, as ADR 0009 Decision 5 compares it,
    or ``None`` for the literal ``:memory:``.

    A ``file:`` URI with an empty path gives ``""``, SQLite's private
    temporary database, which names no file.
    """
    if _is_memory(name):
        return None
    text = str(name) if _naming.is_a(name, pathlib.PurePath) else name
    if type(text) is not str:
        return ""
    if text.startswith("file:"):
        rest = text[len("file:") :].partition("#")[0].partition("?")[0]
        if rest.startswith("//"):
            # file://<authority>/<path>: the authority is empty or localhost.
            rest = "/" + rest[2:].partition("/")[2]
        path = urllib.parse.unquote(rest)
        if not path:
            return ""
        return os.path.realpath(os.path.abspath(path))
    return os.path.realpath(os.path.abspath(text))


def _host(value: object) -> str | None:
    """``HOST`` as Django's backends read it: ``""`` for one not set, as an
    absent key, ``None`` and an empty ``str`` read (``if settings_dict["HOST"]``),
    and a ``str`` of any subclass as an exact ``str``, made by ``str.__str__``
    so no code of the subclass runs. ``None`` for any other value, which is
    not compared.
    """
    if value is None:
        return ""
    if issubclass(type(value), str):
        return str.__str__(cast("str", value))
    return None


def _port(value: object) -> object:
    """``PORT`` as Django's backends read it: ``""`` for one not set, as
    ``None`` and every false ``str``, ``int`` or ``float`` read
    (``if settings_dict["PORT"]``); otherwise the number ``int()`` reads, as
    MySQL's and Oracle's backends do, or the value as its base type reads it
    when ``int()`` reads none. Each is read through the base type's own
    method, so no code of a subclass runs. ``None`` for a value of any other
    type, which is not compared.
    """
    if value is None:
        return ""
    kind = type(value)
    plain: str | float
    if issubclass(kind, str):
        plain = str.__str__(cast("str", value))
    elif issubclass(kind, int):
        plain = int.__int__(cast("int", value))
    elif issubclass(kind, float):
        plain = float.__float__(cast("float", value))
    else:
        return None
    if not plain:
        return ""
    try:
        return int(plain)
    except (ValueError, OverflowError):  # a str int() refuses, nan, inf
        return plain


def _identity(engine: str, entry: Mapping[str, Any]) -> tuple[object, ...] | None:
    """What two aliases on one database share, led by the rule their refusal
    states; ``None`` for one exempt, or for a server alias whose ``HOST`` or
    ``PORT`` is of a type not compared (ADR 0034 Decision 2).
    """
    name = entry.get("NAME")
    if is_sqlite(engine):
        path = sqlite_path(name)
        return None if path is None else (_ONE_FILE, path)
    host = _host(entry.get("HOST", ""))
    port = _port(entry.get("PORT", ""))
    if host is None or port is None:
        # Not compared (ADR 0034 Decision 2): its backend meets the value
        # when it connects, and no code of a HOST or PORT value runs here.
        return None
    return (_ONE_SERVER, engine, host, port, name)


def databases_problems(value: object, with_database: Iterable[str]) -> list[str]:
    """Judge ``NEOLITH["DATABASES"]`` against the labels of the hosted apps
    that declare the ``database`` resource (ADR 0009, the admitted set, the
    rows checked before Django imports a model). Every problem is returned.
    """
    if type(value) is not dict:
        return [f"{_KEY}: expected a dict, got {_type(value)}"]
    entries: dict[object, object] = value
    if not entries:
        return [f"{_KEY}: is empty; leave the key out"]
    labels = list(dict.fromkeys(with_database))
    problems: list[str] = []
    compared: dict[tuple[object, ...], str] = {}
    for alias, entry in entries.items():
        if type(alias) is not str:
            problems.append(f"{_KEY}: an alias is a str, got {_type(alias)}")
            continue
        if alias != DEFAULT and alias not in labels:
            problems.append(f"database {_shown(alias)}: no hosted app declares it")
            continue
        found = _entry_problems(alias, entry)
        problems.extend(found)
        if found:
            continue
        settings: dict[str, Any] = entry  # type: ignore[assignment]  # judged above
        engine: str = settings["ENGINE"]
        identity = _identity(engine, settings)
        if identity is None:
            continue
        first = compared.setdefault(identity, alias)
        if first != alias:
            problems.append(f"databases {first} and {alias}: {identity[0]}")
    problems.extend(
        f"app {label}: no connection settings for its database"
        for label in labels
        if label not in entries
    )
    return problems


def _entry_problems(alias: str, entry: object) -> list[str]:
    """The connection settings of one alias: Django's own mapping, passed on
    unchanged, within the rows of ADR 0009's admitted set.
    """
    where = f"database {alias}"
    if type(entry) is not dict:
        return [f"{where}: expected a dict, got {_type(entry)}"]
    settings: dict[object, object] = entry
    # Before any key is looked up, so that no key's own __eq__ runs.
    for key in settings:
        if type(key) is not str:
            return [f"{where}: a key of type {_type(key)} is not a setting's name"]
    problems: list[str] = []
    engine = settings.get("ENGINE")
    if type(engine) is not str or not engine:
        problems.append(f"{where}: ENGINE is required")
    elif engine in _DUMMY:
        problems.append(
            f"{where}: ENGINE is required; the dummy backend is not a database"
        )
    name = settings.get("NAME", "")
    if type(name) is not str and not _naming.is_a(name, pathlib.PurePath):
        problems.append(f"{where}: NAME: expected a str or a path, got {_type(name)}")
    elif type(name) is str and not name:
        problems.append(
            f"{where}: NAME is required; a service or option file alone is not "
            "admitted yet"
        )
    app = alias != DEFAULT
    if app and "ATOMIC_REQUESTS" in settings:
        problems.append(
            f"{where}: ATOMIC_REQUESTS would wrap other apps' views in this app's "
            "transaction"
        )
    if "TEST" in settings:
        test = settings["TEST"]
        if type(test) is not dict:
            problems.append(f"{where}: TEST: expected a dict, got {_type(test)}")
        else:
            if "MIRROR" in test:
                problems.append(
                    f"{where}: TEST.MIRROR joins two aliases into one connection "
                    "in tests"
                )
            if app and "DEPENDENCIES" in test:
                problems.append(f"{where}: TEST.DEPENDENCIES is set by the host")
            if "NAME" in test:
                problems.append(
                    f"{where}: TEST.NAME is not admitted yet; Django names the test "
                    "database from NAME"
                )
    return problems


def built(
    value: Mapping[str, Any] | None, with_database: Iterable[str]
) -> dict[str, Any]:
    """``DATABASES`` for a ``NEOLITH["DATABASES"]`` that passed, or ``None``
    when the key is absent: ``default`` first, the deployer's or ``{}``, then
    one alias per label in order, each with ``TEST["DEPENDENCIES"] = []``.

    Each alias gets a copy of its mapping, and of its ``TEST``, so Django's
    defaults, which it writes into ``DATABASES`` in place, never reach
    ``NEOLITH``.
    """
    given = dict(value or {})
    databases: dict[str, Any] = {DEFAULT: _copied(given.get(DEFAULT, {}))}
    for label in dict.fromkeys(with_database):
        entry = _copied(given[label])
        entry["TEST"] = {**entry.get("TEST", {}), "DEPENDENCIES": []}
        databases[label] = entry
    return databases


def _copied(entry: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(entry)
    if "TEST" in copied:
        copied["TEST"] = dict(copied["TEST"])
    return copied
