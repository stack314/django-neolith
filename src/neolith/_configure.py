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
   ``DeclarationError``, with the remote providers that the deployer's
   ``NEOLITH["REMOTE_PROVIDERS"]`` names (ADR 0014) and the routing
   policies ``NEOLITH["ROUTING_POLICIES"]`` names (ADR 0031). Resolve reads
   ``NEOLITH``, so its refusals come with every problem found there, or
   with the refusal of settings that are not a ``dict``.
4. **Configure.** The deployer's mapping, row by row.

``settings_problems`` is the same Configure, run at the first set-up on the
installed declarations and ``settings.NEOLITH``, and compared with the
settings Django holds (``neolith.E005``).
"""

from collections.abc import Iterable, Sequence
from typing import Any, Final, cast

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import ConnectionHandler

from neolith import _checks, _databases, _naming
from neolith._declaration import Declaration, DeclarationError, declaration_of

__all__ = [
    "HOST_NAMES",
    "ROUTER",
    "EarlyRead",
    "built_settings",
    "configure",
    "databases_difference",
    "settings_problems",
]

ROUTER: Final = "neolith.routers.AppRouter"
# The settings the host builds, each with where the deployer says it instead.
HOST_NAMES: Final = {
    "INSTALLED_APPS": 'NEOLITH["UNDECLARED_APPS"]',
    "DATABASES": 'NEOLITH["DATABASES"]',
    "DATABASE_ROUTERS": 'NEOLITH["DATABASES"]',
}
# Labels the host reserves (ADR 0009, the admitted set).
_RESERVED: Final = ("default", "django_cache")
_SHOWN: Final = 160


class EarlyRead(ImproperlyConfigured):
    """A setting was read while the command line read the deployer's module
    or an app's declaration, before settings exist: ``neolith._unset``, the
    settings module Django is pointed at meanwhile, raises it.
    """

    def __init__(self) -> None:
        super().__init__("a setting was read before settings exist")


def _type(value: object) -> str:
    return _naming.type_name(value)


def _shown(text: str) -> str:
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


def _cause(error: BaseException) -> str:
    return _naming.cause(error, _SHOWN + 3)


def _refuse(problems: list[str]) -> None:
    if problems:
        raise ImproperlyConfigured("\n".join(sorted(problems)))


def configure(
    apps: object, deployer: object, *, closed: bool = False
) -> dict[str, Any]:
    """Run Declare, Resolve and Configure; return the whole settings mapping.

    ``closed`` is for a host named on the command line, which hosts declared
    apps only in 0.1 (ADR 0022 Decision 4): Configure then also refuses a
    ``default`` in ``NEOLITH["DATABASES"]``.

    Raises:
        DeclarationError: A hosted app's declaration is refused when its
            class is defined, or Resolve refuses the hosted apps, the
            remote providers or the routing policies, with any problem of
            ``NEOLITH``.
        ImproperlyConfigured: Any other row of the builder's table refuses
            its input.
    """
    paths = _listed(apps)
    read = [(path, _read(path)) for path in paths]
    problems = [found for _, found in read if type(found) is str]
    pairs = [(path, found) for path, found in read if type(found) is Declaration]
    _refuse(problems + _compared(pairs))
    declarations = [declaration for _, declaration in pairs]
    from neolith._host import resolve

    settings_given: dict[object, object] | None = (
        deployer if type(deployer) is dict else None
    )
    neolith = (
        None
        if settings_given is None
        else _naming.entry(settings_given, "NEOLITH", _checks.MISSING)
    )
    _, refusals = resolve(
        declarations, _checks.remote_identities(neolith), _checks.routed(neolith)
    )
    if refusals:
        # Resolve reads NEOLITH, so a requirement it finds unmet may come
        # from a problem there, or from settings that are not a dict: those
        # are reported with its refusals.
        if settings_given is None:
            refusals.append(_not_settings(deployer))
        else:
            refusals += _checks.neolith_problems(neolith, declarations)
    if refusals:
        raise DeclarationError("\n".join(sorted(refusals)))
    names, problems = _names(deployer)
    problems += _deployer_problems(names, declarations)
    if closed:
        problems += _open_problems(names.get("NEOLITH"))
    _refuse(problems)
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


def _read(path: str) -> Declaration | str:
    """Read one hosted app's declaration through Django's ``AppConfig.create``,
    noting whether the import read a Django setting.

    An app that cannot be read stops the build, since Python stops there:
    an error from the import, 0021's ``DeclarationError``, or a read of a
    setting. What is found by comparing what was read, such as a config
    with no declaration, is returned as a refusal, so it is reported with
    the others of its phase.
    """
    where = f"app {_shown(path)}"
    was = settings.configured
    try:
        config = AppConfig.create(path)
    except DeclarationError:
        raise
    except EarlyRead as error:
        raise ImproperlyConfigured(_reads(where)) from error
    except Exception as error:
        raise ImproperlyConfigured(
            f"{where}: cannot be read: {_cause(error)}"
        ) from error
    if not was and settings.configured:
        raise ImproperlyConfigured(_reads(where))
    cls = type(config)
    declaration = declaration_of(cls)
    if declaration is None:
        return f"{where}: has no declaration"
    if declaration.name != path:
        if path == _naming.full_name(cls, None):
            return f"{where}: name the app's package, not its config class"
        return (
            f"{where}: its declaration names {_shown(declaration.name)}; name the "
            "package the declaration names"
        )
    return declaration


def _reads(where: str) -> str:
    return (
        f"{where}: reads Django settings at import; a declaration is read before "
        "settings exist"
    )


def _compared(pairs: Sequence[tuple[str, Declaration]]) -> list[str]:
    problems: list[str] = []
    labels: dict[str, str] = {}
    paths = [path for path, _ in pairs]
    for path, declaration in pairs:
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


def _names(deployer: object) -> tuple[dict[str, Any], list[str]]:
    """The deployer's settings, the upper-case names of the mapping as
    Django reads a settings module, and the refusal of each name that is not
    a ``str``, which Configure reports with its other refusals.
    """
    if type(deployer) is not dict:
        raise ImproperlyConfigured(_not_settings(deployer))
    entries: dict[object, object] = deployer
    problems = [
        f"settings: a setting name is a str, got {_type(name)}"
        for name in entries
        if type(name) is not str
    ]
    names = {
        name: value
        for name, value in entries.items()
        if type(name) is str and name.isupper()
    }
    return names, problems


def _not_settings(deployer: object) -> str:
    return f"settings: expected a dict of setting names, got {_type(deployer)}"


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


def _open_problems(neolith: object) -> list[str]:
    """The refusal of a ``default`` database in a host that must be closed."""
    databases = neolith.get("DATABASES") if type(neolith) is dict else None
    if type(databases) is dict and _databases.DEFAULT in databases:
        return [
            f"database {_databases.DEFAULT}: neolith run hosts declared apps only "
            "in 0.1"
        ]
    return []


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

    ``INSTALLED_APPS`` and ``DATABASE_ROUTERS`` are compared whole.
    ``DATABASES`` is compared once Django's defaults are filled into both
    sides, as ``ConnectionHandler.configure_settings`` fills them, by every
    key at every depth: the keys Django fills in are equal on both sides
    (ADR 0022 fact 11), and any other key added after the builder is
    refused. A difference inside it is named by its path, and neither value
    is shown: an alias holds connection settings, a password among them
    (K9). A ``NEOLITH`` that its own check refuses is reported there and
    not here.
    """
    hosted = _checks.hosted_declarations(list(configs))
    neolith = getattr(settings, "NEOLITH", None)
    if type(neolith) is not dict or _checks.neolith_problems(neolith, hosted):
        return []
    expected = built_settings(hosted, neolith)
    problems: list[str] = []
    for name in ("INSTALLED_APPS", "DATABASES", "DATABASE_ROUTERS"):
        found = getattr(settings, name, _checks.MISSING)
        if name == "DATABASES":
            difference = databases_difference(found, expected[name])
        else:
            difference = _difference(found, expected[name])
        if difference is not None:
            problems.append(
                f"settings: {name} differs from what the host builds: {difference}; "
                "build settings with neolith.host.build_settings, and change them "
                "through NEOLITH"
            )
    return problems


def _filled(databases: dict[str, Any]) -> dict[str, Any]:
    """``databases`` as Django serves with it, with the defaults
    ``ConnectionHandler.configure_settings`` fills in.

    Only the levels that function writes are copied: each alias's dict, and
    its ``TEST`` dict when that is a dict. Every other value, ``OPTIONS``
    among them, is the one given, so a value that compares by identity, or
    cannot be copied, such as an SSL context, is still equal to itself.
    """
    copied: dict[str, Any] = {}
    for alias, entry in databases.items():
        if type(entry) is dict:
            entry = dict(entry)
            if type(entry.get("TEST")) is dict:
                entry["TEST"] = dict(entry["TEST"])
        copied[alias] = entry
    filled: dict[str, Any] = ConnectionHandler().configure_settings(copied)
    return filled


def databases_difference(found: object, built: dict[str, Any]) -> str | None:
    """The first difference between ``DATABASES`` and what the host builds,
    with Django's defaults filled into both, or ``None``.

    No value under an alias is shown, on either side (K9). An error raised
    while the defaults are filled in is named by its class alone, since its
    message may quote a value.
    """
    if found is _checks.MISSING:
        return "it is absent"
    if type(found) is not dict:
        return f"it is a {_type(found)}, the host builds a dict"
    entries: dict[object, object] = found
    # Before any key is looked up, so that no key's own __eq__ runs.
    for alias in entries:
        if type(alias) is not str:
            return f"alias {_repr(alias)} is not one the host builds"
    missing = [alias for alias in built if alias not in entries]
    if missing:
        return f"alias {missing[0]!r} is missing"
    extra = [alias for alias in entries if alias not in built]
    if extra:
        return f"alias {_repr(extra[0])} is not one the host builds"
    try:
        have = _filled(found)
    except Exception as error:
        return f"it cannot be read as Django reads it: {_type(error)}"
    return _difference(have, _filled(built), whole=True)


def _repr(value: object) -> str:
    return _naming.shown(value, _SHOWN + 3)


def _difference(
    found: object, expected: object, where: str = "", *, whole: bool = False
) -> str | None:
    """The first difference between a setting and what the host builds, or
    ``None``. A mapping the host built is compared by its own keys only,
    unless ``whole``, when a key on either side alone is a difference.

    A value inside a mapping is named by its path, ``where``, and is never
    shown, on either side: the mapping the host builds is ``DATABASES``,
    whose values are connection settings (K9). An entry of a list that is
    the setting itself, an app or a router, is shown.
    """
    if found is _checks.MISSING:
        return "it is absent"
    if type(expected) is dict:
        if not _naming.is_a(found, dict):
            return f"{where or 'it'} is a {_type(found)}, the host builds a dict"
        found = cast("dict[Any, Any]", found)
        # Before any key is looked up, so that no key's own code runs.
        for key in found:
            if type(key) is not str:
                return f"{where}[{_repr(key)}] is not one the host builds"
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
            difference = _difference(found[key], value, inner, whole=whole)
            if difference is not None:
                return difference
        if whole:
            for key in found:
                if key not in own:
                    return f"{where}[{_repr(key)}] is not one the host builds"
        return None
    if type(expected) is list:
        if type(found) is not list:
            return f"{where or 'it'} is a {_type(found)}, the host builds a list"
        items: list[object] = found
        for index, (have, want) in enumerate(zip(items, expected, strict=False)):
            if type(have) is not type(want) or have != want:
                if where:
                    return _inside(f"{where}[{index}]", have, want)
                return f"entry {index} is {_repr(have)}, the host builds {want!r}"
        if len(items) != len(expected):
            return (
                f"{where or 'it'} has {len(items)} entries, the host builds "
                f"{len(expected)}"
            )
        return None
    try:
        same = type(found) is type(expected) and found == expected
    except Exception:
        same = False
    if not same:
        if where:
            return _inside(where, found, expected)
        return "it is not the value the host builds"
    return None


def _inside(where: str, found: object, expected: object) -> str:
    """A difference at ``where``, inside a mapping the host builds. A value
    that differs only in type names the two types, never the values.
    """
    if type(found) is not type(expected):
        return f"{where} is a {_type(found)}, the host builds a {_type(expected)}"
    return f"{where} is not the value the host builds"
