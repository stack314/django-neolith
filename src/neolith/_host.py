"""The host's set-up, and ``connect()`` (spec 05, ADR 0021, ADR 0022).

``neolith`` is an installed app. Its ``ready()`` runs once Django has loaded
every app's models, in Django's global registry only, and sets the host up:

- **Resolve.** Every contract a hosted app names has one declaration class in
  the host, at most one providing app, and every ``requires`` a provider.
- **Validate.** Every provided contract's implementation is imported by its
  dotted path, checked against the contract and built with no arguments.

Set-up reports every refusal of both, and every unknown identity a
``connect()`` recorded before it, in one ``DeclarationError``, logged before
it is raised. A set-up that passes publishes its table on the ``neolith`` app
config that ran it, so the table and the implementation instances belong to
that set-up: a later set-up in the same process builds its own, and when an
``override_settings`` ends, Django restores the earlier config and its table.

State the host owns (CONTRIBUTING K6), all of it guarded by one lock:

- the identities that ``connect()`` was asked for before a table existed,
  with the file and line of each ``connect()``, which set-up checks and
  then empties;
- the hosted apps of the first set-up that passed, since a process hosts one
  set of apps (ADR 0022 Decision 5).
"""

import contextlib
import importlib
import inspect
import logging
import threading
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn, final

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel

from neolith._connector import Connector, InProcess
from neolith._contracts import _IDENTITY, Contract
from neolith._declaration import (
    Declaration,
    DeclarationError,
    Provision,
    _clip,
    _show,
    declaration_of,
)

__all__ = ["HostConfig", "UsageError", "connect"]

_log = logging.getLogger("neolith.host")

_LABEL: Final = "neolith"
_HOST_ATTR: Final = "_neolith_host"


class UsageError(RuntimeError):
    """A mistake in the calling code about the host: its state or what it holds.

    Raised for a call made before the host is set up, and for a contract the
    host does not know. It is not a ``ContractError`` (spec 01, CON-29).
    """


@final
@dataclass(frozen=True, slots=True)
class Host:
    """The table one set-up built: what the host knows and who provides it."""

    known: frozenset[str]
    connectors: Mapping[str, Connector]


_LOCK: Final = threading.Lock()
# The identities connect() was asked for before a table existed: each with
# the places it was asked from, which only the message reads.
_RECORDED: dict[str, dict[str, None]] = {}
# The hosted apps of the first set-up that passed: label to config class.
_first: Mapping[str, type[AppConfig]] | None = None


@final
class HostConfig(AppConfig):
    """neolith's own app. Its ``ready()`` sets the host up."""

    name = _LABEL
    label = _LABEL
    default = True
    verbose_name = "neolith"

    def ready(self) -> None:
        set_up(self)


def _current() -> Host | None:
    """The table of the set-up Django's global registry holds now, if any.

    Read from the registry each time, so nothing outside a set-up keeps it.
    """
    config = global_apps.app_configs.get(_LABEL)
    if type(config) is not HostConfig:
        return None
    host = vars(config).get(_HOST_ATTR)
    return host if type(host) is Host else None


def set_up(config: AppConfig) -> None:
    """Resolve and Validate the hosted apps, then publish the table.

    Acts only in Django's global registry: any other registry, such as one
    ``isolate_apps`` builds, is not a host (ADR 0021 Decision 8).
    """
    global _first
    registry = config.apps
    if registry is not global_apps:
        return
    configs = registry.get_app_configs()
    hosted = {c.label: type(c) for c in configs if declaration_of(type(c)) is not None}
    host, refusals = build(registry)
    with _LOCK:
        for identity, places in sorted(_RECORDED.items()):
            if identity not in host.known:
                refusals.extend(_unknown_at(identity, place, host) for place in places)
        if refusals:
            _refuse(DeclarationError("\n".join(sorted(refusals))))
        if _first is not None and _first != hosted:
            _refuse(
                ImproperlyConfigured(
                    "host: the hosted apps changed after set-up: "
                    f"{_difference(_first, hosted)}; a process hosts one set of apps"
                )
            )
        _RECORDED.clear()
        if _first is None:
            _first = types.MappingProxyType(dict(hosted))
        setattr(config, _HOST_ATTR, host)


def _refuse(error: ImproperlyConfigured) -> NoReturn:
    """Log a refusal, then raise it: a server that loads the app again sees
    Django's "populate() isn't reentrant", and the log still has the cause."""
    with contextlib.suppress(Exception):
        _log.error("the host cannot be set up:\n%s", error)
    raise error


def _difference(
    first: Mapping[str, type[AppConfig]], now: Mapping[str, type[AppConfig]]
) -> str:
    parts = [f"{label} was added" for label in sorted(now.keys() - first.keys())]
    parts += [f"{label} was removed" for label in sorted(first.keys() - now.keys())]
    parts += [
        f"{label} has another config class"
        for label in sorted(first.keys() & now.keys())
        if first[label] is not now[label]
    ]
    return ", ".join(parts)


def _unknown_at(identity: str, place: str, host: Host) -> str:
    return (
        f"connect: {_quoted(identity)} at {place} is not a contract this host knows; "
        f"{_knows(host)}"
    )


def _knows(host: Host) -> str:
    if not host.known:
        return "no app of the host provides or requires a contract"
    return "it knows the contracts its apps provide or require, which are " + ", ".join(
        sorted(host.known)
    )


def build(registry: Apps) -> tuple[Host, list[str]]:
    """Resolve and Validate the declared apps of a loaded registry.

    Returns the table and every refusal; the table is used only when there is
    none. Every check runs whatever the others found, so one refusal hides
    none behind it.
    """
    hosted = [
        (config, found)
        for config in registry.get_app_configs()
        if (found := declaration_of(type(config))) is not None
    ]
    contracts, refusals = resolve([declaration for _, declaration in hosted])
    connectors: dict[str, Connector] = {}
    for config, declaration in hosted:
        for provision in declaration.provides:
            instance = validate(registry, config, provision, refusals)
            if instance is not None:
                connectors.setdefault(
                    provision.contract.identity,
                    Connector(
                        provision.contract.declaration,
                        InProcess(
                            provision.contract.declaration,
                            instance,
                            app=declaration.label,
                        ),
                    ),
                )
    return (
        Host(
            known=frozenset(contracts),
            connectors=types.MappingProxyType(connectors),
        ),
        refusals,
    )


def resolve(
    declarations: Sequence[Declaration],
) -> tuple[dict[str, Contract], list[str]]:
    """Resolve: the contracts the host knows by identity, and the refusals.

    Each identity keeps the first class named for it, in the order of the
    declarations; a later app that names another class with that identity,
    or provides it again, is refused naming the first.
    """
    refusals: list[str] = []
    named: dict[str, tuple[Contract, str]] = {}
    providers: dict[str, str] = {}
    for declaration in declarations:
        app = declaration.label
        named_here = [
            ("provides", provision.contract) for provision in declaration.provides
        ] + [("requires", contract) for contract in declaration.requires]
        for field, contract in named_here:
            first = named.setdefault(contract.identity, (contract, app))
            if first[0] is not contract:
                refusals.append(
                    f"app {app}: {field}: {contract.identity} is "
                    f"{_class(contract)} here and {_class(first[0])} in app "
                    f"{first[1]}; in a host, one identity has one contract class"
                )
        for provision in declaration.provides:
            identity = provision.contract.identity
            provider = providers.setdefault(identity, app)
            if provider != app:
                refusals.append(
                    f"app {app}: provides: {identity} is provided by app "
                    f"{provider} too; in a host, one app provides a contract"
                )
    for declaration in declarations:
        for contract in declaration.requires:
            if contract.identity not in providers:
                refusals.append(
                    f"app {declaration.label}: requires: {contract.identity} has "
                    "no provider; no app of this host provides it, so install "
                    "the app that does"
                )
    return {identity: found for identity, (found, _) in named.items()}, refusals


def _class(contract: Contract) -> str:
    declaration = contract.declaration
    return f"{declaration.__module__}.{declaration.__qualname__}"


def validate(
    registry: Apps, config: AppConfig, provision: Provision, refusals: list[str]
) -> object | None:
    """Validate one provided contract: import, check and build its implementation.

    Returns the instance, or ``None`` with the refusals added. Each check
    whose value can be read runs, whatever the others found.
    """
    app = config.label
    path = provision.implementation
    where = (
        f"app {app}: provides: {provision.contract.identity}: implementation "
        f"{_quoted(path)}"
    )
    module_name, _, attribute = path.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        refusals.append(f"{where} does not import: {_cause(error)}")
        _log_cause(where, error)
        # Nothing of the implementation can be read.
        return None
    found = vars(module).get(attribute, _NOTHING)
    if found is _NOTHING:
        refusals.append(
            f"{where} does not import: {_long(module_name)} defines no "
            f"{_long(attribute)}"
        )
        # Nothing is there to check.
        return None
    if not issubclass(type(found), type):
        refusals.append(f"{where} is {_show(found)}, not a class")
        # The checks below read a class.
        return None
    cls: type[Any] = found
    problem = False
    # Read statically: a metaclass cannot answer for the class.
    own_module = vars(cls).get("__module__")
    own_name: str = type.__dict__["__qualname__"].__get__(cls)
    if type(own_module) is not str:
        refusals.append(f"{where}: the class's module cannot be read")
        problem = True
    elif "." in own_name or "<" in own_name:
        refusals.append(
            f"{where}: the class is {_long(f'{own_module}.{own_name}')}, which is not "
            f"defined at the top level of its module; define it at the top "
            f"level of a module of app {app}"
        )
        problem = True
    elif f"{own_module}.{own_name}" != path:
        refusals.append(
            f"{where} names {_long(f'{own_module}.{own_name}')} by another path; a "
            "class is named by its own path, "
            f'implementation="{_long(f"{own_module}.{own_name}")}"'
        )
        problem = True
    # A module that cannot be read cannot be placed in an app.
    if type(own_module) is str:
        problem |= _belongs(registry, config, own_module, where, refusals)
    for found_problem in provision.contract._implementation_problems(cls):
        refusals.append(f"app {app}: provides: {found_problem}")
        problem = True
    # Built even when a check above failed, so that a failing __init__ is
    # reported beside the rest.
    try:
        instance = cls()
    except Exception as error:
        refusals.append(
            f"{where}: building it with no arguments raised {_cause(error)}"
        )
        _log_cause(where, error)
        return None
    return None if problem else instance


_NOTHING: Final = object()


def _belongs(
    registry: Apps, config: AppConfig, module: str, where: str, refusals: list[str]
) -> bool:
    """Refuse a module outside the app, or one read at Declare; True if refused."""
    container = registry.get_containing_app_config(module)
    if container is not config:
        owner = "no app" if container is None else f"app {container.label}"
        refusals.append(
            f"{where}: the class is in {_long(module)}, which belongs to {owner}; an "
            f"implementation belongs to the app that provides it, {config.name}"
        )
        return True
    declared_in = vars(type(config)).get("__module__")
    if module in (config.name, declared_in):
        refusals.append(
            f"{where}: the class is in {_long(module)}, which is read with the "
            "declaration; put the implementation in another module of the "
            f"app, such as {config.name}.services"
        )
        return True
    return False


# Names and paths are shown whole up to this length, so advice can be copied.
_LONG: Final = 160


def _long(text: str) -> str:
    return text if len(text) <= _LONG else f"{text[:_LONG]}..."


def _quoted(text: str) -> str:
    return repr(text) if len(text) <= _LONG else f"{text[:_LONG]!r}..."


def _cause(error: Exception) -> str:
    """An error's class and message, bounded, never raising."""
    try:
        text = str(error)
    except Exception:
        text = "<the message cannot be read>"
    return f"{type(error).__qualname__}: {_clip(text)}"


def _log_cause(where: str, error: Exception) -> None:
    with contextlib.suppress(Exception):
        _log.error("%s", where, exc_info=error)


def connect(identity: str, /) -> "Connected":
    """Return a connector to the contract version ``identity``.

    ``identity`` is ``<name>.v<N>``, a contract a hosted app provides or
    requires. ``connect()`` may run at import: before the host is set up, the
    identity is recorded, and set-up stops the host if no app knows it; after,
    an identity the host does not know is a ``UsageError`` at once. A call
    through the connector is a ``UsageError`` until set-up has finished.
    """
    if type(identity) is not str:
        raise TypeError(
            f"connect() takes a contract identity as a str, not "
            f"{type(identity).__qualname__}"
        )
    if not _IDENTITY.fullmatch(identity):
        raise ValueError(
            f"{_show(identity)} is not a contract identity: write <name>.v<N>, "
            "for example 'inventory.v1'"
        )
    with _LOCK:
        host = _current()
        if host is None:
            _RECORDED.setdefault(identity, {})[_place()] = None
        elif identity not in host.known:
            raise UsageError(
                f"connect({_quoted(identity)}): no app of this host provides or "
                f"requires {_long(identity)}; {_knows(host)}"
            )
    return Connected(identity)


def _place() -> str:
    """The file and line of the ``connect()`` call, for the message only."""
    frame = inspect.currentframe()
    try:
        caller = None if frame is None or frame.f_back is None else frame.f_back.f_back
        if caller is None:
            return "an unknown place"
        return f"{caller.f_code.co_filename}:{caller.f_lineno}"
    finally:
        del frame


def _not_set_up(identity: str) -> UsageError:
    return UsageError(
        f"connector to {_long(identity)}: the host is not set up yet. Calls begin once "
        "Django has loaded every app and neolith's set-up has passed, so a call "
        "cannot be made at import, in an app's ready() or in an "
        "implementation's __init__"
    )


@final
class Connected:
    """A connector returned by ``connect()``: it holds its identity, nothing else.

    Each action is an attribute, called as on any connector. Every use finds
    the current host's table by the identity.
    """

    __slots__ = ("_identity",)

    _identity: str

    def __init__(self, identity: str) -> None:
        object.__setattr__(self, "_identity", identity)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("a connector cannot be changed")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("a connector cannot be changed")

    def __getattr__(self, name: str) -> Callable[..., BaseModel | None]:
        if name.startswith("_"):
            # A private name is never an action, which also covers a copy
            # probing for hooks.
            raise AttributeError(name)
        identity = self._identity
        # Refuses a host that is not set up, and an action the contract does
        # not have, where the attribute is read.
        getattr(_connector(identity), name)

        def call(*args: object, **kwargs: object) -> BaseModel | None:
            # The current host's connector, found again on every call.
            action: Callable[..., BaseModel | None] = getattr(
                _connector(identity), name
            )
            return action(*args, **kwargs)

        call.__name__ = call.__qualname__ = name
        return call

    def __repr__(self) -> str:
        return f"<connector to {self._identity}>"

    def __copy__(self) -> "Connected":
        return self

    def __deepcopy__(self, memo: object) -> "Connected":
        return self

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(
            f"the connector to {self._identity} cannot be pickled: call "
            f"connect({self._identity!r}) where it is needed"
        )


def _connector(identity: str) -> Connector:
    host = _current()
    if host is None:
        raise _not_set_up(identity)
    found = host.connectors.get(identity)
    if found is None:
        # Known identities all have a provider once set-up passed; this is a
        # connector recorded in a process whose host knows other contracts.
        raise UsageError(
            f"connector to {identity}: no app of this host provides it; {_knows(host)}"
        )
    return found
