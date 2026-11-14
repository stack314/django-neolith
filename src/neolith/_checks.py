"""The host's checks of its settings and its request entry (ADR 0022).

Each rule is a plain function over the value it judges, which returns every
problem it finds, so one function serves every place that checks the same
input: set-up, through the system checks registered under the tag
``neolith``, and the root middleware's constructor. The admitted set of each
input is stated in its function; anything else is a problem, never a pass.

The ids are the ones this step fixes from ADR 0022's working numbers:
``neolith.E001`` for ``NEOLITH``, ``neolith.E006`` for ``MIDDLEWARE``,
``neolith.E014`` for a hosted app with models and no database,
``neolith.E017`` for a table name that ``default`` shortened (ADR 0009) and
``neolith.W001`` for an admin class in no installed app.
"""

import math
import sys
from collections.abc import Iterable
from typing import Any, Final

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.conf import settings
from django.core import checks
from django.db.backends.utils import strip_quotes
from django.utils.module_loading import import_string

from neolith._declaration import declaration_of
from neolith._serving import admin_code

__all__ = [
    "DEFAULT_REQUEST_DEADLINE",
    "ROOT_MIDDLEWARE",
    "TAG",
    "admin_check",
    "database_check",
    "database_problems",
    "middleware_check",
    "middleware_problems",
    "neolith_check",
    "neolith_problems",
    "register",
    "request_deadline",
    "table_name_check",
    "table_name_problems",
]

TAG: Final = "neolith"
ROOT_MIDDLEWARE: Final = "neolith.middleware.RootMiddleware"
# The request deadline when NEOLITH has no REQUEST_DEADLINE (ADR 0017
# Decision 4), in seconds.
DEFAULT_REQUEST_DEADLINE: Final = 30.0
# The bounds of a configured request deadline, in seconds: 1 ms to
# 9999999999 ms, the budget cap (ADR 0022 Decision 3).
_SHORTEST: Final = 0.001
_LONGEST: Final = 9999999.999
_DEADLINE: Final = 'NEOLITH["REQUEST_DEADLINE"]'
# The keys of NEOLITH that exist so far. Later steps add theirs (ADR 0022
# Decision 3).
_KEYS: Final = ("REQUEST_DEADLINE",)
_MISSING: Final = object()
_SHOWN: Final = 80


def _type(value: object) -> str:
    return type(value).__qualname__


def _shown_key(key: object) -> str:
    """A key for a message: an exact ``str`` by its content, within a bound,
    and anything else by its type alone, so no author code runs.
    """
    if type(key) is str:
        text: str = key
        return repr(text) if len(text) <= _SHOWN else f"{text[:_SHOWN]!r}..."
    return f"of type {_type(key)}"


def _shown_number(value: int | float) -> str:
    """A number for a message. ``str`` of a very large ``int`` raises, so
    such a one is shown by its size.
    """
    if type(value) is int and value.bit_length() > 128:
        return f"an int of {value.bit_length()} bits"
    return repr(value)


def middleware_problems(value: object) -> list[str]:
    """Judge ``MIDDLEWARE``: a ``list`` of ``str`` whose first entry is the
    root middleware, which is listed once (ADR 0022, the builder's table).

    Set-up, ``neolith.E006`` and the root middleware's constructor all run
    this one function. "Once" is judged by class: every entry that imports
    as the root middleware, under any path, or as a subclass of it, counts.
    An entry that does not import is Django's to report when it builds the
    handler. Every problem found is returned.
    """
    if type(value) is not list:
        return [f"MIDDLEWARE: expected a list, got {_type(value)}"]
    entries: list[object] = value
    problems = [
        f"MIDDLEWARE: entry {index}: expected a str, got {_type(entry)}"
        for index, entry in enumerate(entries)
        if type(entry) is not str
    ]
    first = bool(entries) and type(entries[0]) is str and entries[0] == ROOT_MIDDLEWARE
    if not first:
        problems.append(f"MIDDLEWARE: {ROOT_MIDDLEWARE} must be the first entry")
    count = sum(_is_root(entry) for entry in entries)
    if count > 1:
        problems.append(f"MIDDLEWARE: {ROOT_MIDDLEWARE} is listed {count} times")
    return problems


def _is_root(entry: object) -> bool:
    if type(entry) is not str:
        return False
    if entry == ROOT_MIDDLEWARE:
        return True
    # Imported here: neolith.middleware imports this module.
    from neolith.middleware import RootMiddleware

    try:
        found = import_string(entry)
    except Exception:  # Django reports it when it builds the handler
        return False
    return issubclass(type(found), type) and issubclass(found, RootMiddleware)


def _deadline_problems(value: object) -> list[str]:
    if value is None:
        return []
    if type(value) not in (int, float):
        return [
            f"{_DEADLINE}: expected a number of seconds or None, got {_type(value)}"
        ]
    number: int | float = value  # type: ignore[assignment]  # judged by type()
    if type(number) is float and not math.isfinite(number):
        return [f"{_DEADLINE}: {number!r} is not finite"]
    if number < _SHORTEST or number > _LONGEST:
        return [
            f"{_DEADLINE}: {_shown_number(number)} is outside {_SHORTEST} to "
            f"{_LONGEST} seconds"
        ]
    return []


def neolith_problems(value: object = _MISSING) -> list[str]:
    """Judge ``NEOLITH``: present, a ``dict`` whose keys are in the key
    table, each with its one form (ADR 0022 Decision 3). Leave ``value`` out
    when the setting is absent.
    """
    if value is _MISSING:
        return ["settings: NEOLITH is required; it is {} when nothing is set"]
    if type(value) is not dict:
        return [f"NEOLITH: expected a dict, got {_type(value)}"]
    problems: list[str] = []
    # Items are read, never looked up, so no key's own hooks run.
    entries: dict[object, object] = value
    for key, item in entries.items():
        if type(key) is not str or key not in _KEYS:
            problems.append(
                f"NEOLITH: unknown key {_shown_key(key)}; the keys are "
                + ", ".join(sorted(_KEYS))
            )
        elif key == "REQUEST_DEADLINE":
            problems.extend(_deadline_problems(item))
    return problems


def request_deadline(value: dict[str, Any]) -> float | None:
    """The request deadline, in seconds, of a ``NEOLITH`` that passed
    ``neolith_problems``: 30 when the key is absent, ``None`` for none.
    """
    for key, item in value.items():
        if key == "REQUEST_DEADLINE":
            return None if item is None else float(item)
    return DEFAULT_REQUEST_DEADLINE


def _hosting() -> bool:
    """Whether neolith's app is in Django's global registry: its checks
    judge a host, and a process that installs no host has none.
    """
    return global_apps.is_installed(TAG)


def middleware_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E006``: the root middleware's place in ``MIDDLEWARE``."""
    if not _hosting():
        return []
    return [
        checks.Error(problem, id="neolith.E006")
        for problem in middleware_problems(getattr(settings, "MIDDLEWARE", _MISSING))
    ]


def neolith_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E001``: the ``NEOLITH`` setting."""
    if not _hosting():
        return []
    return [
        checks.Error(problem, id="neolith.E001")
        for problem in neolith_problems(getattr(settings, "NEOLITH", _MISSING))
    ]


def admin_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.W001``: each admin registered on an admin site whose code
    is in no installed app, by the rule the root middleware binds with
    (``neolith._serving``). Its views run in a request root of no app, so a
    contract call from them is refused (ADR 0017 Decision 4).

    Only admin sites that exist are read, and the admin is not imported
    when nothing has imported it.
    """
    if not _hosting():
        return []
    sites = sys.modules.get("django.contrib.admin.sites")
    if sites is None:
        return []
    found: set[str] = set()
    for site in list(vars(sites).get("all_sites", ())):
        registry = vars(site).get("_registry")
        if type(registry) is not dict:
            continue
        for admin in list(registry.values()):
            label, code = admin_code(admin)
            if label is None and code is not None:
                found.add(code)
    return [
        checks.Warning(
            f"{code} in no installed app; its views have no calling app, and a "
            "contract call from them is refused",
            id="neolith.W001",
        )
        for code in sorted(found)
    ]


def _hosted(configs: Iterable[AppConfig]) -> list[tuple[AppConfig, bool]]:
    """The configs with a declaration, each with whether it declares the
    ``database`` resource. A registry without neolith's app holds none, since
    a declared app refuses to load there (ADR 0021 Decision 8), so the two
    database checks need no test of their own for a process with no host.
    """
    found: list[tuple[AppConfig, bool]] = []
    for config in configs:
        declaration = declaration_of(type(config))
        if declaration is not None:
            found.append((config, "database" in declaration.resources))
    return found


def database_problems(configs: Iterable[AppConfig]) -> list[str]:
    """Judge each hosted app's models against its ``database`` resource: an
    app that does not declare one has no model in the registry at all, of
    any kind, proxy, unmanaged or many-to-many table included (ADR 0009, the
    admitted set). An app with no declaration is not judged here.
    """
    return [
        f"app {config.label}: has models but declares no database resource"
        for config, has_database in _hosted(configs)
        if not has_database
        and any(config.get_models(include_auto_created=True, include_swapped=True))
    ]


def table_name_problems(configs: Iterable[AppConfig]) -> list[str]:
    """Judge each hosted app's table names: each is stated by the app or is
    the whole name Django derives, never one that ``default``'s backend
    shortened (ADR 0009 Decision 8).

    Django cuts a derived name to the limit of the ``default`` connection
    when it builds the class (``Options.contribute_to_class`` and
    ``ManyToManyField._get_m2m_db_table``), so the name it holds is compared
    with the whole one. A model's name is stated when its ``Meta`` sets a
    ``db_table``, inherited ones included; a many-to-many table's when its
    field sets ``db_table``. A proxy has the table of its concrete model,
    judged there, and a ``through`` model is judged as a model. An app with
    no declaration lives on ``default`` and is not judged.
    """
    problems = []
    for config, _ in _hosted(configs):
        for model in config.get_models():
            options = model._meta
            if options.proxy:
                continue
            if not options.original_attrs.get("db_table"):
                whole = f"{options.app_label}_{options.model_name}"
                if options.db_table != whole:
                    problems.append(_shortened(config, options.object_name))
            for field in options.local_many_to_many:
                # Resolved once the models are loaded; a string is a model
                # that never loaded, which Django's own checks report.
                through: Any = field.remote_field.through
                if (
                    not issubclass(type(through), type)
                    or not through._meta.auto_created
                ):
                    continue
                if vars(field).get("db_table"):
                    continue
                whole = f"{strip_quotes(options.db_table)}_{field.name}"
                if through._meta.db_table != whole:
                    problems.append(
                        _shortened(config, f"{options.object_name}.{field.name}")
                    )
    return sorted(problems)


def _shortened(config: AppConfig, name: object) -> str:
    return (
        f"app {config.label}: the table name of {name} was shortened to fit the "
        "default database; set db_table"
    )


def _configs(app_configs: object) -> Iterable[AppConfig]:
    """The configs Django asks a check about: those named, or every one."""
    if app_configs is None:
        return global_apps.get_app_configs()
    named: Iterable[AppConfig] = app_configs  # type: ignore[assignment]
    return named


def database_check(
    app_configs: object = None, **kwargs: object
) -> list[checks.CheckMessage]:
    """``neolith.E014``: a hosted app with models and no ``database``
    resource.
    """
    return [
        checks.Error(problem, id="neolith.E014")
        for problem in database_problems(_configs(app_configs))
    ]


def table_name_check(
    app_configs: object = None, **kwargs: object
) -> list[checks.CheckMessage]:
    """``neolith.E017``: a hosted app's table name that ``default``'s
    backend shortened.
    """
    return [
        checks.Error(problem, id="neolith.E017")
        for problem in table_name_problems(_configs(app_configs))
    ]


def register() -> None:
    """Register the host's checks under the tag ``neolith``; registering
    again changes nothing.
    """
    for check in (
        neolith_check,
        middleware_check,
        admin_check,
        database_check,
        table_name_check,
    ):
        checks.register(check, TAG)
