"""The boundary pass's rules on calls, read from source once the apps are
loaded (spec 02, TRN-41; spec 05, APP-66 and APP-68; ADR 0030,
``neolith.E108`` to ``neolith.E111`` and ``neolith.W103``).

In each module of a hosted app ``<a>``, through the names its file binds by
top-level imports alone (``neolith._check_source.bound``), each matched by
the object it resolves to, never by its spelling:

- ``neolith.E108``: ``connect("<id>")`` names a contract version that is
  not in ``<a>``'s ``requires``.
- ``neolith.E109``: a database alias written as a literal, or as
  ``DEFAULT_DB_ALIAS``, which reads as ``"default"``, names ``default`` or
  another hosted app's alias, in these places only: any keyword
  ``using=``, the place of ``using`` in a call of a function of
  ``django.db.transaction``, the first argument of a method call
  ``.using(...)`` or ``.db_manager(...)``, and the subscript of
  ``django.db.connections``. And the forms that mean ``default`` by leaving
  the alias out: a function of ``django.db.transaction`` that takes
  ``using`` called with no alias or with ``None``, as a call or as a bare
  decorator, which calls it with the decorated function alone; ``atomic``
  or ``non_atomic_requests`` used as a decorator, bare or applied by a call
  to a ``lambda``, which both read as ``default``; ``django.db.connection``
  at its import or at an attribute access. An alias the host does not
  hold, the alias of a hosted app without a database among them, is
  Django's error at run time, and a computed alias is not judged: so is
  any name in the place of ``using``, a view passed by name to ``atomic``
  or ``non_atomic_requests`` included, since source alone cannot tell a
  function from an alias held in a name.
- ``neolith.E110``: ``command_scope("<label>")`` names another installed app.
- ``neolith.E111``: a model or the config of another installed app is named
  by a string: ``get_model(...)`` and ``get_app_config("<label>")`` on any
  receiver, a migration's registry parameter included, ``get_model`` with
  a literal label whatever its model name, computed included; any keyword
  ``sender="<label>.<Model>"``; and ``get_user_model()`` when
  ``AUTH_USER_MODEL`` names a model of another installed app. A label no
  installed app has is not reported: Django refuses it at run time.

Each finding is on the line of what was written: the alias, identity,
label or model as a literal, or, for a form that writes none, the name it
calls or loads (for an attribute, the line of the attribute's own name).

And ``neolith.W103``: every first-party module of the map, in an app or in
no app, other than a test module, that calls ``neolith.testing.boot()``.

A computed argument to these calls is ``neolith.W101``, which reading
source reports, beside the E111 of a literal label. Nothing is imported
and no code of an app runs.
"""

import ast
import dataclasses
from collections.abc import Iterator, Mapping
from typing import Final, NamedTuple

import django.db.transaction
from django.core import checks

from neolith._check import Finding, place, shown
from neolith._check_source import (
    _TRANSACTION,
    COMMAND_SCOPE,
    CONNECT,
    Module,
    Source,
    _argument,
    _binds,
    _iterate,
    _text,
    canonical,
    is_test_module,
    resolved,
)
from neolith._declaration import declaration_of

__all__ = ["USING", "App", "Judged", "booted", "findings", "judged"]

DEFAULT: Final = "default"
# The place of ``using`` in each public function of django.db.transaction
# that takes it, as inspect.signature reads the installed module: the first
# parameter, or the second after a callback or a savepoint id. Django 6.1
# adds savepoint_create, so the set holds the names the installed module
# defines (tests/test_check_aliases.py compares it with the module).
_SECOND: Final = frozenset(
    {
        "on_commit",
        "savepoint_commit",
        "savepoint_rollback",
        "set_autocommit",
        "set_rollback",
    }
)
USING: Final[Mapping[str, int]] = {
    f"django.db.transaction.{name}": 1 if name in _SECOND else 0
    for name in _TRANSACTION
    if callable(getattr(django.db.transaction, name, None))
}
# The two that read a callable in the place of using as the function they
# decorate, on the alias default (``callable(using)``, db/transaction.py).
_BARE: Final = frozenset(
    {"django.db.transaction.atomic", "django.db.transaction.non_atomic_requests"}
)
_CONNECTION: Final = "django.db.connection"
_CONNECTIONS: Final = "django.db.connections"
_DEFAULT_ALIAS: Final = "django.db.utils.DEFAULT_DB_ALIAS"
_USER_MODEL: Final = "django.contrib.auth.get_user_model"
_BOOT: Final = "neolith.testing.boot"

_REQUIRES_HINT: Final = "list the contract in the app's requires"
_RECORDS_HINT: Final = "reach that app's records through a contract it provides"
_SCOPE_HINT: Final = (
    "open the scope for the app's own label, {own}, and reach another app "
    "through a contract it provides"
)
_ALIAS_HINT: Final = (
    "name the app's own alias, {own}, and reach another app's data through "
    "a contract it provides"
)
_NO_DATABASE_HINT: Final = (
    "the app has no database of its own; reach another app's data through a "
    "contract it provides"
)
_BOOT_HINT: Final = (
    "call it from a test module only: conftest.py, tests.py, test_*.py, "
    "*_test.py, or a file under a directory named tests"
)


@dataclasses.dataclass(slots=True)
class App:
    """What one hosted app's code is judged against.

    Attributes:
        label: The app's label, which is also its alias when it has a
            database.
        requires: The identities of the contracts its declaration requires.
        aliases: The aliases of the other hosted apps with a database.
        database: Whether the app has a database of its own.
        installed: The labels of every installed app.
        user_model: ``AUTH_USER_MODEL``, or ``None`` when it is not a
            ``str``.
    """

    label: str
    requires: frozenset[str]
    aliases: frozenset[str]
    database: bool
    installed: frozenset[str]
    user_model: str | None


class Judged(NamedTuple):
    """One finding in a module: its line, id, what was found and the hint."""

    line: int
    id: str
    what: str
    hint: str


def _object(node: ast.AST, followed: Mapping[str, str]) -> str | None:
    """The listed object or dotted name an expression stands for."""
    if not isinstance(node, ast.expr):
        return None
    name = resolved(node, followed)
    return None if name is None else canonical(name)


def _short(name: str) -> str:
    return name.rpartition(".")[2]


def _none(argument: object) -> bool:
    """Whether an argument is left out or is the literal ``None``."""
    return argument is None or (
        isinstance(argument, ast.Constant) and argument.value is None
    )


def _line(node: ast.expr) -> int:
    """The line of the name a call calls or an expression loads: for an
    attribute, the line of its own name, which is where a traceback places a
    method call spread over lines.
    """
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute) and node.end_lineno is not None:
        return node.end_lineno
    return node.lineno


def _written(argument: object, call: ast.Call) -> int:
    """The line of an argument as written, or of the call without one."""
    return argument.lineno if isinstance(argument, ast.expr) else _line(call)


def _model(text: str | None) -> tuple[str, str] | None:
    """``<label>.<Model>`` split as Django splits it (``str.split(".")``
    into two, in ``Apps.get_model`` and ``make_model_tuple``), with neither
    part empty.
    """
    if text is None:
        return None
    parts = text.split(".")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def judged(
    tree: ast.Module, followed: Mapping[str, str], package: str, app: App
) -> list[Judged]:
    """``neolith.E108`` to ``neolith.E111`` in one module of ``app``, whose
    file binds ``followed`` by top-level imports alone and whose relative
    imports resolve against ``package``.
    """
    found: list[Judged] = []
    for node in _iterate(tree):
        if isinstance(node, ast.Call):
            found += _call(node, followed, app)
        elif isinstance(node, ast.Subscript):
            if _object(node.value, followed) == _CONNECTIONS:
                found += _alias(
                    node.slice, node.slice.lineno, "connections[...]", followed, app
                )
        elif isinstance(node, ast.Attribute):
            if _object(node, followed) == _CONNECTION:
                found.append(_connection(_line(node), app))
        elif isinstance(node, ast.ImportFrom):
            # One bound name for each alias but *, in order: the line of the
            # name imported, which may not be the line of from.
            names = [alias for alias in node.names if alias.name != "*"]
            for alias, (_, dotted) in zip(names, _binds(node, package), strict=True):
                if dotted is not None and canonical(dotted) == _CONNECTION:
                    found.append(_connection(alias.lineno, app))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                target = _object(decorator, followed)
                if target is not None and _decorated(target):
                    found.append(_default(_line(decorator), f"@{_short(target)}", app))
    return found


def _decorated(target: str) -> bool:
    """Whether a function of transaction, as a bare decorator, runs on
    ``default``: it is called with the decorated function alone, which
    leaves ``using`` out when ``using`` is not its first parameter, and
    which ``atomic`` and ``non_atomic_requests`` read as the function they
    decorate.
    """
    return target in _BARE or USING.get(target, 0) > 0


def _alias_hint(app: App) -> str:
    if app.database:
        return _ALIAS_HINT.format(own=shown(repr(app.label)))
    return _NO_DATABASE_HINT


def _default(line: int, what: str, app: App) -> Judged:
    return Judged(
        line,
        "neolith.E109",
        f"{what} names no database alias, so it uses the database 'default'",
        _alias_hint(app),
    )


def _connection(line: int, app: App) -> Judged:
    return Judged(
        line,
        "neolith.E109",
        "django.db.connection is the connection to the database 'default'",
        _alias_hint(app),
    )


def _alias(
    node: ast.AST | object,
    line: int,
    where: str,
    followed: Mapping[str, str],
    app: App,
) -> list[Judged]:
    """``neolith.E109`` for an alias written in ``where``: a literal, or
    ``DEFAULT_DB_ALIAS``, that names ``default`` or another hosted app's
    alias.
    """
    if isinstance(node, ast.Constant):
        if type(node.value) is not str:
            return []
        alias: str = node.value
        written = shown(repr(alias))
    elif isinstance(node, ast.AST) and _object(node, followed) == _DEFAULT_ALIAS:
        alias = DEFAULT
        written = "DEFAULT_DB_ALIAS"
    else:
        return []
    if alias == DEFAULT:
        whose = "the database 'default'"
    elif alias in app.aliases:
        whose = f"the database of app {shown(alias)}"
    else:
        return []
    return [
        Judged(
            line,
            "neolith.E109",
            f"{where} names {written}, {whose}",
            _alias_hint(app),
        )
    ]


def _call(node: ast.Call, followed: Mapping[str, str], app: App) -> list[Judged]:
    found: list[Judged] = []
    callee = _object(node.func, followed)
    method = node.func.attr if isinstance(node.func, ast.Attribute) else None
    # Each place an alias is read, once: a keyword using=, and the place of
    # the alias in .using(), .db_manager() and the functions of transaction.
    places: dict[int, tuple[ast.expr | object | None, str]] = {}
    for keyword in node.keywords:
        if keyword.arg == "using":
            places[id(keyword.value)] = (keyword.value, "using=")
    line = _line(node)
    if method == "using":
        argument = _argument(node, 0, "alias")
        places.setdefault(id(argument), (argument, ".using()"))
    elif method == "db_manager":
        argument = _argument(node, 0, "using")
        places.setdefault(id(argument), (argument, ".db_manager()"))
    if callee is not None and callee in USING:
        name = f"{_short(callee)}()"
        given = _argument(node, USING[callee], "using")
        if _none(given) or (callee in _BARE and isinstance(given, ast.Lambda)):
            found.append(_default(_written(given, node), name, app))
        else:
            places.setdefault(id(given), (given, name))
    for written, where in places.values():
        found += _alias(written, _written(written, node), where, followed, app)
    if callee == CONNECT:
        argument = _argument(node, 0, None)
        identity = _text(argument)
        if identity is not None and identity not in app.requires:
            found.append(
                Judged(
                    _written(argument, node),
                    "neolith.E108",
                    f"connect({shown(repr(identity))}) names a contract version "
                    "the app does not require",
                    _REQUIRES_HINT,
                )
            )
    elif callee == COMMAND_SCOPE:
        argument = _argument(node, 0, "app")
        label = _text(argument)
        if label is not None and label != app.label and label in app.installed:
            found.append(
                Judged(
                    _written(argument, node),
                    "neolith.E110",
                    f"command_scope({shown(repr(label))}) opens a root for app "
                    f"{shown(label)}",
                    _SCOPE_HINT.format(own=shown(repr(app.label))),
                )
            )
    elif callee == _USER_MODEL:
        found += _user_model(line, app)
    found += _named(node, method, app)
    return found


def _user_model(line: int, app: App) -> list[Judged]:
    named = _model(app.user_model)
    if named is None or named[0] == app.label or named[0] not in app.installed:
        return []
    return [
        Judged(
            line,
            "neolith.E111",
            f"get_user_model() names AUTH_USER_MODEL "
            f"{shown(repr(app.user_model))}, a model of app {shown(named[0])}",
            _RECORDS_HINT,
        )
    ]


def _named(node: ast.Call, method: str | None, app: App) -> list[Judged]:
    """``neolith.E111`` for a model or a config named by a string: by
    ``get_model`` and ``get_app_config`` on any receiver, and by any keyword
    ``sender=``.
    """
    found: list[Judged] = []

    def other(label: str) -> bool:
        return label != app.label and label in app.installed

    if method == "get_model":
        reached = _get_model(node)
        if reached is not None and other(reached[0]):
            owner, model = reached
            what = (
                f"get_model() names a model of app {shown(owner)} by its label "
                f"{shown(repr(owner))}"
                if model is None
                else f"get_model() names {shown(repr(f'{owner}.{model}'))}, a model "
                f"of app {shown(owner)}"
            )
            found.append(
                Judged(
                    _written(_argument(node, 0, "app_label"), node),
                    "neolith.E111",
                    what,
                    _RECORDS_HINT,
                )
            )
    elif method == "get_app_config":
        argument = _argument(node, 0, "app_label")
        label = _text(argument)
        if label is not None and other(label):
            found.append(
                Judged(
                    _written(argument, node),
                    "neolith.E111",
                    f"get_app_config({shown(repr(label))}) names the config of "
                    f"app {shown(label)}",
                    _RECORDS_HINT,
                )
            )
    for keyword in node.keywords:
        if keyword.arg != "sender":
            continue
        sender = _text(keyword.value)
        named = _model(sender)
        if named is not None and other(named[0]):
            found.append(
                Judged(
                    keyword.value.lineno,
                    "neolith.E111",
                    f"sender={shown(repr(sender))} names a model of app "
                    f"{shown(named[0])}",
                    _RECORDS_HINT,
                )
            )
    return found


def _get_model(node: ast.Call) -> tuple[str, str | None] | None:
    """The label and model a call ``get_model(...)`` names, as
    ``Apps.get_model(app_label, model_name=None, ...)`` reads them: both, or
    ``"<label>.<Model>"`` alone, or a literal label with a computed model
    name, whose model is then ``None``: whatever model it picks, it is one
    of that label's app, as ``get_app_config`` of the label reaches. A model
    name alone, as ``AppConfig.get_model(model_name, ...)`` takes it, names
    no label, nor does a literal label beside an empty or non-``str`` model
    name, which Django refuses or reads as ``require_ready``.
    """
    label = _text(_argument(node, 0, "app_label"))
    if label is None:
        return None
    model = _argument(node, 1, "model_name")
    if _none(model):
        return _model(label)
    if isinstance(model, ast.Constant):
        name = _text(model)
        return (label, name) if name else None
    return label, None


def booted(tree: ast.Module, followed: Mapping[str, str]) -> list[int]:
    """The line of each call of ``neolith.testing.boot()`` in a module."""
    return [
        _line(node)
        for node in _iterate(tree)
        if isinstance(node, ast.Call) and _object(node.func, followed) == _BOOT
    ]


def findings(source: Source) -> list[Finding]:
    """``neolith.E108`` to ``neolith.E111`` in the hosted apps' modules, and
    ``neolith.W103`` in the first-party modules of the map.
    """
    from django.apps import apps as global_apps
    from django.conf import settings

    installed = frozenset(config.label for config in global_apps.get_app_configs())
    databases = getattr(settings, "DATABASES", None)
    aliases = frozenset(
        alias
        for alias in (databases if type(databases) is dict else ())
        if type(alias) is str
    )
    user_model = getattr(settings, "AUTH_USER_MODEL", None)
    found: list[Finding] = []
    for label, config in source.hosted.items():
        declaration = declaration_of(type(config))
        requires = (
            frozenset(held.identity for held in declaration.requires)
            if declaration is not None
            else frozenset()
        )
        app = App(
            label,
            requires,
            frozenset(other for other in source.hosted if other in aliases) - {label},
            label in aliases,
            installed,
            user_model if type(user_model) is str else None,
        )
        for module in source.modules(label):
            if module.tree is None:
                continue
            followed = source.bound(module)
            for line, kind, what, hint in judged(
                module.tree, followed, module.file.package, app
            ):
                message = checks.Error(
                    f"app {shown(label)}: module {shown(module.file.module)}: {what}",
                    hint=hint,
                    id=kind,
                )
                found.append(Finding(message, label, place(module.file.path), line))
    found += _boots(source)
    return found


def _first_party(source: Source) -> Iterator[Module]:
    """Each first-party module of the map with source, other than a test
    module, read once in the run.
    """
    for dotted, placed in sorted(source.map().items()):
        if is_test_module(placed.parts()):
            continue
        module = source.placed(dotted)
        if module is not None:
            yield module


def _boots(source: Source) -> list[Finding]:
    from django.apps import apps as global_apps

    found: list[Finding] = []
    for module in _first_party(source):
        if module.tree is None:
            continue
        lines = booted(module.tree, source.bound(module))
        if not lines:
            continue
        name = module.file.module
        config = global_apps.get_containing_app_config(name)
        label = None if config is None else config.label
        where = "" if label is None else f"app {shown(label)}: "
        for line in lines:
            message = checks.Warning(
                f"{where}module {shown(name)}: calls neolith.testing.boot(), "
                "which turns Boot's schema read off",
                hint=_BOOT_HINT,
                id="neolith.W103",
            )
            hosted = label if label in source.hosted else None
            found.append(Finding(message, hosted, place(module.file.path), line))
    return found
