"""Declare, Resolve and Configure: the rows of the builder (ADR 0022).

``configure(apps, deployer)`` is the plain function behind
``neolith.host.build_settings``. It reads the hosted apps' declarations,
resolves them, judges the deployer's mapping and returns the whole settings
mapping, or raises the first phase's refusals together. It records nothing
and never touches Django's settings; the entry around it does both.

The phases, in order, each reported together and sorted, and a later phase
does not run:

1. **The list of apps.** A ``list`` of one or more ``str``, none twice.
2. **Declare.** Each app is read in order, by Django's own
   ``AppConfig.create``; the first that cannot be read stops the build, since
   Python stops there. Then what was read is compared: one label per app,
   no label the host reserves, no battery that is a hosted app or
   ``neolith``.
3. **Resolve.** ADR 0021's function, whose refusals are one
   ``DeclarationError``.
4. **Configure.** The deployer's mapping, row by row.

``settings_problems`` is the same Configure, run at the first set-up on the
installed declarations and ``settings.NEOLITH``, and compared with the
settings Django holds (``neolith.E005``).
"""

from collections.abc import Iterable, Sequence
from typing import Any, Final

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from neolith import _checks, _databases
from neolith._declaration import Declaration, DeclarationError, declaration_of

__all__ = ["HOST_NAMES", "ROUTER", "built_settings", "configure", "settings_problems"]

ROUTER: Final = "neolith.routers.AppRouter"
# The settings the host builds, each with where the deployer says it instead.
HOST_NAMES: Final = {
    "INSTALLED_APPS": 'NEOLITH["UNDECLARED_APPS"]',
    "DATABASES": 'NEOLITH["DATABASES"]',
    "DATABASE_ROUTERS": 'NEOLITH["DATABASES"]',
}
# Labels the host reserves (ADR 0009, the admitted set).
_RESERVED: Final = ("default", "django_cache")
_DUMMY: Final = "django.db.backends.dummy"
_SHOWN: Final = 160


def _type(value: object) -> str:
    return type(value).__qualname__


def _shown(text: str) -> str:
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


def _cause(error: BaseException) -> str:
    try:
        text = str(error)
    except Exception:
        text = "<the message cannot be read>"
    return f"{type(error).__qualname__}: {_shown(text)}"


def _refuse(problems: list[str]) -> None:
    if problems:
        raise ImproperlyConfigured("\n".join(sorted(problems)))


def configure(apps: object, deployer: object) -> dict[str, Any]:
    """Run Declare, Resolve and Configure; return the whole settings mapping.

    Raises:
        DeclarationError: A hosted app's declaration is refused when its
            class is defined, or Resolve refuses the hosted apps.
        ImproperlyConfigured: Any other row of the builder's table refuses
            its input.
    """
    paths = _listed(apps)
    declarations = [_read(path) for path in paths]
    _refuse(_compared(paths, declarations))
    from neolith._host import resolve

    _, refusals = resolve(declarations)
    if refusals:
        raise DeclarationError("\n".join(sorted(refusals)))
    names = _names(deployer)
    _refuse(_deployer_problems(names, declarations))
    return {**names, **built_settings(declarations, names["NEOLITH"])}


def _listed(apps: object) -> list[str]:
    if type(apps) is not list:
        raise ImproperlyConfigured(f"host: apps: expected a list, got {_type(apps)}")
    entries: list[object] = apps
    if not entries:
        raise ImproperlyConfigured("host: no apps named")
    problems: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if type(entry) is not str:
            problems.append(
                f"host: apps: entry {index}: expected a str, got {_type(entry)}"
            )
        elif entry in seen:
            problems.append(f"host: app {_shown(entry)} named twice")
        else:
            seen.add(entry)
    _refuse(problems)
    return [str(entry) for entry in entries]


def _read(path: str) -> Declaration:
    """Read one hosted app's declaration through Django's ``AppConfig.create``,
    noting whether the import read a Django setting.
    """
    where = f"app {_shown(path)}"
    was = settings.configured
    try:
        config = AppConfig.create(path)
    except DeclarationError:
        raise
    except Exception as error:
        raise ImproperlyConfigured(
            f"{where}: cannot be read: {_cause(error)}"
        ) from error
    if not was and settings.configured:
        raise ImproperlyConfigured(
            f"{where}: reads Django settings at import; a declaration is read "
            "before settings exist"
        )
    cls = type(config)
    declaration = declaration_of(cls)
    if declaration is None:
        raise ImproperlyConfigured(f"{where}: has no declaration")
    if declaration.name != path:
        if path == f"{cls.__module__}.{cls.__qualname__}":
            raise ImproperlyConfigured(
                f"{where}: name the app's package, not its config class"
            )
        raise ImproperlyConfigured(
            f"{where}: its declaration names {_shown(declaration.name)}; name the "
            "package the declaration names"
        )
    return declaration


def _compared(paths: Sequence[str], declarations: Sequence[Declaration]) -> list[str]:
    problems: list[str] = []
    labels: dict[str, str] = {}
    for path, declaration in zip(paths, declarations, strict=True):
        label = declaration.label
        first = labels.setdefault(label, path)
        if first != path:
            problems.append(f"apps {first} and {path}: both have the label {label}")
        if label in _RESERVED:
            problems.append(f'app {label}: "{label}" is reserved for the host')
        for battery in declaration.batteries:
            if battery == _checks.TAG:
                problems.append(
                    f"app {label}: batteries: neolith is installed by the host"
                )
            elif battery in paths:
                problems.append(
                    f"app {label}: batteries: {battery} is a hosted app; a hosted "
                    "app is reached through a contract"
                )
    return problems


def _names(deployer: object) -> dict[str, Any]:
    """The deployer's settings: the upper-case names of the mapping, as
    Django reads a settings module.
    """
    if type(deployer) is not dict:
        raise ImproperlyConfigured(
            f"settings: expected a dict of setting names, got {_type(deployer)}"
        )
    entries: dict[object, object] = deployer
    problems = [
        f"settings: a setting name is a str, got {_type(name)}"
        for name in entries
        if type(name) is not str
    ]
    _refuse(problems)
    return {str(name): value for name, value in entries.items() if str(name).isupper()}


def _deployer_problems(
    names: dict[str, Any], declarations: Sequence[Declaration]
) -> list[str]:
    problems = [
        f"settings: {name} is built by the host; see {instead}"
        for name, instead in HOST_NAMES.items()
        if name in names
    ]
    problems += _checks.neolith_problems(
        names.get("NEOLITH", _checks.MISSING), declarations
    )
    problems += _checks.middleware_problems(
        names.get("MIDDLEWARE", _checks.MISSING), by_class=False
    )
    problems += _checks.silenced_problems(
        names.get("SILENCED_SYSTEM_CHECKS", _checks.MISSING)
    )
    return problems


def built_settings(
    declarations: Sequence[Declaration], neolith: dict[str, Any]
) -> dict[str, Any]:
    """The settings the host builds from the hosted apps' declarations and a
    checked ``NEOLITH`` (ADR 0022 Decision 4, ADR 0009 Decision 6):
    ``INSTALLED_APPS`` (hosted apps as given, ``UNDECLARED_APPS`` as given,
    batteries, then ``neolith``), ``DATABASES`` and ``DATABASE_ROUTERS``.
    """
    installed = [
        *(d.name for d in declarations),
        *neolith.get("UNDECLARED_APPS", ()),
        *_checks.batteries(declarations),
        _checks.TAG,
    ]
    return {
        "INSTALLED_APPS": installed,
        "DATABASES": _databases.built(
            neolith.get("DATABASES"), _checks.with_database(declarations)
        ),
        "DATABASE_ROUTERS": [ROUTER],
    }


def settings_problems(configs: Iterable[AppConfig]) -> list[str]:
    """Compare the settings Django holds with what the host builds for the
    installed declarations and ``settings.NEOLITH`` (``neolith.E005``).

    ``INSTALLED_APPS`` and ``DATABASE_ROUTERS`` are compared whole. In
    ``DATABASES``, the aliases are compared, and every key the host wrote
    with its value; keys Django filled in are not (ADR 0022 fact 11). A
    ``NEOLITH`` that its own check refuses is reported there and not here.
    """
    hosted = _checks.hosted_declarations(list(configs))
    neolith = getattr(settings, "NEOLITH", None)
    if type(neolith) is not dict or _checks.neolith_problems(neolith, hosted):
        return []
    expected = built_settings(hosted, neolith)
    problems: list[str] = []
    databases = getattr(settings, "DATABASES", None)
    if (
        not expected["DATABASES"][_databases.DEFAULT]
        and type(databases) is dict
        and isinstance(databases.get(_databases.DEFAULT), dict)
    ):
        # A closed host's default is {}, into which Django writes the dummy
        # backend's ENGINE, and nothing else of a connection.
        default: dict[str, Any] = databases[_databases.DEFAULT]
        engine = default.get("ENGINE", _DUMMY)
        if type(engine) is not str or engine != _DUMMY:
            problems.append(
                "settings: DATABASES differs from what the host builds: "
                "['default'] is a database, the host builds {} since "
                f'{_databases.DEFAULT} is not in NEOLITH["DATABASES"]; build '
                "settings with neolith.host.build_settings, and change them "
                "through NEOLITH"
            )
            return problems
    for name in ("INSTALLED_APPS", "DATABASES", "DATABASE_ROUTERS"):
        found = getattr(settings, name, _checks.MISSING)
        difference = _difference(found, expected[name])
        if difference is not None:
            problems.append(
                f"settings: {name} differs from what the host builds: {difference}; "
                "build settings with neolith.host.build_settings, and change them "
                "through NEOLITH"
            )
    return problems


def _repr(value: object) -> str:
    try:
        return _shown(repr(value))
    except Exception:
        return f"a value of type {_type(value)}"


def _difference(found: object, expected: object, where: str = "") -> str | None:
    """The first difference between a setting and what the host builds, or
    ``None``. A mapping the host built is compared by its own keys only.
    """
    if found is _checks.MISSING:
        return "it is absent"
    if type(expected) is dict:
        if not isinstance(found, dict):
            return f"{where or 'it'} is a {_type(found)}, the host builds a dict"
        own: dict[str, Any] = expected
        if not where:
            extra = [k for k in found if k not in own]
            missing = [k for k in own if k not in found]
            if missing:
                return f"alias {missing[0]!r} is missing"
            if extra:
                return f"alias {_repr(extra[0])} is not one the host builds"
        for key, value in own.items():
            inner = f"{where}[{key!r}]"
            if key not in found:
                return f"{inner} is missing"
            difference = _difference(found[key], value, inner)
            if difference is not None:
                return difference
        return None
    if type(expected) is list:
        if type(found) is not list:
            return f"{where or 'it'} is a {_type(found)}, the host builds a list"
        items: list[object] = found
        for index, (have, want) in enumerate(zip(items, expected, strict=False)):
            if type(have) is not type(want) or have != want:
                return f"entry {index} is {_repr(have)}, the host builds {want!r}"
        if len(items) != len(expected):
            return f"it has {len(items)} entries, the host builds {len(expected)}"
        return None
    try:
        same = type(found) is type(expected) and found == expected
    except Exception:
        same = False
    if not same:
        return f"{where} is {_repr(found)}, the host builds {_repr(expected)}"
    return None
