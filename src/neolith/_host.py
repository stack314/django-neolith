"""The host's set-up, and ``connect()`` (spec 05, ADR 0021, ADR 0022).

``neolith`` is an installed app. Its ``ready()`` runs once Django has loaded
every app's models, in Django's global registry only, and sets the host up:

- **Resolve.** Every contract a hosted app names has one declaration class in
  the host, at most one providing app, and every ``requires`` a provider.
- **Validate.** Every provided contract's implementation is imported by its
  dotted path, checked against the contract and built with no arguments.

Set-up reports every refusal of both, and every unknown identity a
``connect()`` recorded before it, in one ``DeclarationError``, logged before
it is raised. It builds the principal backends of
``NEOLITH["PRINCIPAL_BACKENDS"]`` once (ADR 0023), recording what it cannot
build for ``neolith.E019``. Then it runs the host's system checks, those tagged
``neolith`` (``neolith._checks``): every error among them is raised in one
``ImproperlyConfigured``, whatever ``SILENCED_SYSTEM_CHECKS`` says, and each
warning is logged. A set-up that passes publishes its table, with the
request deadline read from ``NEOLITH``, on the ``neolith`` app config that
ran it, so the table and the implementation instances belong to
that set-up: a later set-up in the same process builds its own, and when an
``override_settings`` ends, Django restores the earlier config and its table.

State the host owns (CONTRIBUTING K6), all of it guarded by one lock:

- the identities that ``connect()`` was asked for before a table existed,
  with the file and line of each ``connect()``, each tied to the apps whose
  loading step asked from it; a set-up checks those tied to an app it
  installs, and only a set-up that passes drops them, so an unknown one stays
  and the next set-up that installs its app reports it again (ADR 0029);
- the hosted apps of the first set-up that passed, since a process hosts one
  set of apps (ADR 0022 Decision 5);
- the ``NEOLITH`` that first set-up checked, without ``REQUEST_DEADLINE``,
  which an override of ``NEOLITH`` is compared with: only the request
  deadline may change after set-up (ADR 0022 Decision 3).

The first set-up in a process also compares the settings Django holds with
what the host builds for the installed declarations and ``settings.NEOLITH``
(``neolith.E005``), so a change made after the builder ran stops the host.
A receiver of ``setting_changed`` refuses an override of ``DATABASES`` or
``DATABASE_ROUTERS`` after set-up, and an override of ``NEOLITH`` that the
builder's table refuses or that changes a key other than
``REQUEST_DEADLINE``; the request deadline follows an override that passes.
"""

import contextlib
import contextvars
import dataclasses
import importlib
import inspect
import itertools
import logging
import sys
import threading
import types
import weakref
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn, cast, final

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from pydantic import BaseModel

from neolith import (
    _caller,
    _checks,
    _configure,
    _context,
    _http,
    _http_transport,
    _naming,
    _principal,
)
from neolith._connector import Connector as Bound
from neolith._connector import InProcess
from neolith._context import Principal, UsageError
from neolith._contracts import _IDENTITY, Contract
from neolith._declaration import _SHOWN as _CLIPPED
from neolith._declaration import (
    Declaration,
    DeclarationError,
    Provision,
    _show,
    declaration_of,
)
from neolith._frozen import once

__all__ = ["HostConfig", "command_scope", "connect"]

_log = logging.getLogger("neolith.host")

_LABEL: Final = "neolith"
_HOST_ATTR: Final = "_neolith_host"
_LOADER_ATTR: Final = "_neolith_loader"
# The code of Django's Apps.populate, whose frame names the app loading.
_POPULATE: Final = Apps.populate.__code__


@final
@once
@dataclass(frozen=True, slots=True)
class Host:
    """The table one set-up built: what the host knows and who provides it."""

    known: frozenset[str]
    connectors: Mapping[str, Bound]
    # Each hosted app's label, with the identities its declaration requires.
    apps: Mapping[str, tuple[str, ...]]
    # The deadline of a request root, in seconds from the request's start, or
    # None for none: NEOLITH["REQUEST_DEADLINE"] as set-up checked it.
    request_deadline: float | None = _checks.DEFAULT_REQUEST_DEADLINE
    # The warnings set-up logged, which Boot does not log again.
    warned: frozenset[str] = frozenset()
    # The principal backends set-up built, in order (ADR 0023), and the
    # union of their request inputs, which a response varies on.
    principals: tuple[_principal.Built, ...] = ()
    vary: tuple[str, ...] = ()
    # The caller backends and signing keys set-up built (ADR 0027).
    callers: _caller.Callers = _caller.NO_CALLERS


_LOCK: Final = threading.Lock()
# The identities connect() was asked for before a table existed: each with
# the places it was asked from, which only the message reads, and for each
# place what it is tied to: an app's name or config class path, a key of
# _PENDING until the next set-up names it, or None for no app, which every
# set-up checks.
_RECORDED: dict[str, dict[str, dict[str | None, None]]] = {}
# The requests made while Django imported an entry of INSTALLED_APPS, before
# that entry's config existed, which the next set-up ties (_name_pending):
# for each, the module that asked if it asked as it was imported, the entry,
# a copy of the load's entries, and the dict of app configs the load fills,
# one per entry in order, which outlives the load even when it stops before
# its set-up.
_PENDING: dict[
    str, tuple[str | None, str, tuple[object, ...], dict[str, AppConfig]]
] = {}
_PENDING_KEYS: Final = itertools.count()
# The names of the apps installed at any set-up of this process.
_ONCE_INSTALLED: set[str] = set()
# The app whose implementation Validate imports and builds now, if any.
_VALIDATING: Final = contextvars.ContextVar[str | None]("_VALIDATING", default=None)
# The hosted apps of the first set-up that passed: label to config class.
_first: Mapping[str, type[AppConfig]] | None = None
# The NEOLITH the first set-up checked, without REQUEST_DEADLINE.
_first_neolith: Mapping[str, object] | None = None
_DEADLINE_KEY: Final = "REQUEST_DEADLINE"
# The settings that cannot change after set-up, besides NEOLITH's keys.
_FIXED: Final = ("DATABASES", "DATABASE_ROUTERS")


@final
class HostConfig(AppConfig):
    """neolith's own app. Its ``ready()`` sets the host up."""

    name = _LABEL
    label = _LABEL
    default = True
    verbose_name = "neolith"

    def __init__(self, app_name: str, app_module: types.ModuleType | None) -> None:
        super().__init__(app_name, app_module)
        # Django creates the config in the thread that loads the registry.
        setattr(self, _LOADER_ATTR, threading.get_ident())

    def ready(self) -> None:
        _checks.register()
        setting_changed.connect(_setting_changed, dispatch_uid="neolith.host")
        set_up(self)


def _current() -> Host | None:
    """The table calls run on: the published table of the set-up that
    Django's global registry holds now, if any.
    """
    host, installed = _table()
    return host if installed else None


def _table() -> tuple[Host | None, bool]:
    """The table that exists now, and whether neolith's app is installed.

    Judged from the state found, the same way for ``connect()`` and calls:

    - the registry holds neolith's app and its set-up has published a table:
      that table;
    - the registry holds neolith's app without a table: its set-up has not
      ended, so ``connect()`` records and that set-up checks;
    - the registry does not hold neolith's app, loaded or still loading: the
      table of the registry Django restores when the override or
      ``set_available_apps`` ends, which ``connect()`` checks at once and
      calls wait for; with none, no set-up has passed, and ``connect()``
      records. A later set-up's own load reaches this case for a module
      imported before neolith's app is created, so that ``connect()`` is
      checked against the table the set-up would restore. ``connect()``
      from a thread other than the one loading a later set-up is checked
      against that table too (``_restored_elsewhere``).
    """
    config = global_apps.app_configs.get(_LABEL)
    host = _host_of(config)
    if host is not None:
        return host, True
    if config is not None:
        return None, True
    for stored in reversed(global_apps.stored_app_configs):
        config = stored.get(_LABEL)
        if config is not None:
            return _host_of(config), False
    return None, False


def _restored_elsewhere() -> Host | None:
    """While another thread loads a later set-up: the table Django restores
    when that set-up ends, if any.

    A ``connect()`` from this thread then is not caused by that set-up's
    loading, so it is checked at once against the table this thread's calls
    run on, as it is outside any loading, instead of being recorded.
    """
    config = global_apps.app_configs.get(_LABEL)
    if type(config) is not HostConfig:
        return None
    # safe read: a HostConfig of neolith's
    if vars(config).get(_LOADER_ATTR) == threading.get_ident():
        return None
    for stored in reversed(global_apps.stored_app_configs):
        found = stored.get(_LABEL)
        if found is not None:
            return _host_of(found)
    return None


def _host_of(config: object) -> Host | None:
    if type(config) is not HostConfig:
        return None
    host = vars(config).get(_HOST_ATTR)  # safe read: a HostConfig of neolith's
    return host if type(host) is Host else None


def set_up(config: AppConfig) -> None:
    """Resolve and Validate the hosted apps, then publish the table.

    Acts only in Django's global registry: any other registry, such as one
    ``isolate_apps`` builds, is not a host (ADR 0021 Decision 8).
    """
    global _first, _first_neolith
    registry = config.apps
    if registry is not global_apps:
        return
    configs = registry.get_app_configs()
    hosted = {c.label: type(c) for c in configs if declaration_of(type(c)) is not None}
    host, refusals = build(registry)
    # Built once per set-up, after app loading; what is refused is recorded
    # for neolith.E019, which the checks below report (ADR 0023 Decision 10).
    principals = _principals(config)
    callers = _caller.set_up(config, principals)
    # Run outside the lock, which a check must never need; judged inside it,
    # after Resolve and Validate, so their refusals come first.
    found = checks.run_checks(tags=[_checks.TAG])
    with _LOCK:
        first = _first is None
    if first:
        # Only at the first set-up: Django changes DATABASES in place later
        # (ADR 0022 fact 11).
        found += [
            checks.Error(problem, id="neolith.E005")
            for problem in _configure.settings_problems(configs)
        ]
    with _LOCK:
        _name_pending(list(configs))
        _ONCE_INSTALLED.update(c.name for c in configs)
        installed = frozenset(c.name for c in configs) | {
            _naming.full_name(type(c), None) for c in configs
        }
        checked = _checked_by(installed)
        for identity, places in sorted(checked.items()):
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
        deadline = _judge(found)
        if _first is None:
            _first = types.MappingProxyType(dict(hosted))
            neolith: dict[str, object] = getattr(settings, "NEOLITH", {})
            _first_neolith = types.MappingProxyType(
                {k: v for k, v in neolith.items() if k != _DEADLINE_KEY}
            )
        warned = frozenset(
            m.msg for m in found if m.level == checks.WARNING and not m.is_silenced()
        )
        published = dataclasses.replace(
            host,
            connectors=_remote(config, host.connectors, configs, callers),
            request_deadline=deadline,
            warned=warned,
            principals=principals,
            vary=_principal.vary_of(principals),
            callers=callers,
        )
        # Only a set-up that passes is done with the places it checked, all
        # of which its table knows. A refused one drops none, whatever its
        # table knew, so the next set-up of the same apps reports an unknown
        # identity again (ADR 0029).
        for identity, places in checked.items():
            for place in places:
                loads = _RECORDED[identity][place]
                # Its tie to the apps below its module goes with it: this
                # set-up checked the request that tie stands for.
                for app in [
                    a
                    for a in loads
                    if _tied(a, installed) or (a is not None and a.startswith("\1"))
                ]:
                    del loads[app]
                if not loads:
                    del _RECORDED[identity][place]
            if not _RECORDED[identity]:
                del _RECORDED[identity]
        setattr(config, _HOST_ATTR, published)


def _checked_by(installed: frozenset[str]) -> dict[str, list[str]]:
    """The places of each record that a set-up installing ``installed``
    checks: ``installed`` holds each app's name and its config class's path.

    A place is checked when an app this set-up installs asked from it during
    its loading step, or when it was asked from outside any app's loading
    step. A place only other apps asked from, such as one an override added
    and the first apps restored after it do not install, is left for a
    set-up that installs one of them (ADR 0029).
    """
    checked: dict[str, list[str]] = {}
    for identity, places in _RECORDED.items():
        for place, loads in places.items():
            if any(_tied(app, installed) for app in loads):
                checked.setdefault(identity, []).append(place)
    return checked


def _ties(module: str, at_import: bool) -> set[str | None]:
    """What a request from a ``connect()`` in ``module`` is tied to, made now
    in this thread; ``at_import`` when it runs at the module's top level, as
    the module is imported.

    The request belongs to the app whose loading step runs: the app whose
    implementation Validate imports and builds, or else the app the frame of
    Django's ``Apps.populate`` is at, which is the app whose models are
    imported or whose ``ready()`` runs, or the entry of ``INSTALLED_APPS``
    being imported, which ``_name_pending`` ties at the next set-up. Python
    imports a module once, so a request made as a module is imported also
    belongs to the module's own app, or to no app (``_module_ties``). The
    same at Django 5.2 and 6.1.
    """
    validating = _VALIDATING.get()
    loading = None if validating is not None else _populating()
    if validating is not None:
        ties: set[str | None] = {validating}
    elif loading is None:
        return {None}
    elif global_apps.apps_ready:
        found = loading.get("app_config")
        ties = {cast(AppConfig, found).name if _naming.is_a(found, AppConfig) else None}
    else:
        entry = loading.get("entry")
        entries = loading.get("installed_apps")
        if _naming.is_a(entry, AppConfig):
            ties = {cast(AppConfig, entry).name}
        # Django loads an entry of a str subclass as its text.
        # Its real type, not its __class__: str.__str__ reads it below, and a
        # proxy that only claims to be a str is left to no app.
        elif _naming.derives(type(entry), str) and isinstance(entries, (list, tuple)):
            key = f"\0{next(_PENDING_KEYS)}"
            _PENDING[key] = (
                module if at_import else None,
                cast(str, entry),
                tuple(entries),
                global_apps.app_configs,
            )
            return {key}
        else:
            return {None}
    if at_import:
        ties |= _module_ties(module, list(global_apps.app_configs.values()))
    return ties


def _below(importing: tuple[str, ...]) -> set[str | None]:
    """A tie to every app at or below each module whose import was under way
    when a request was made: Python imports every package above a module it
    imports, so such an app's loading imports those modules again in a fresh
    process, and the request with them.
    """
    return {f"\1{module}" for module in importing}


def _tied(tie: str | None, installed: frozenset[str]) -> bool:
    """Whether a set-up that installs ``installed``, its apps' names and
    config class paths, checks ``tie``.
    """
    if tie is None:
        return True
    if tie.startswith("\1"):
        return any(_within(key, tie[1:]) for key in installed)
    return tie in installed


def _module_ties(module: str, *loads: Sequence[AppConfig]) -> set[str | None]:
    """What a request made as ``module`` was imported belongs to besides the
    app whose loading imported it: the app Django's own rule finds for the
    module among the configs of ``loads``, in turn; nothing more when the
    module lies in an app installed at an earlier set-up but not found here;
    else no app, so that every set-up checks it (ADR 0029).
    """
    if not module:
        return {None}
    for configs in loads:
        found = _containing(module, configs)
        if found is not None:
            return {found.name}
    if any(_within(module, app) for app in _ONCE_INSTALLED):
        return set()
    return {None}


def _containing(module: str, configs: Sequence[AppConfig]) -> AppConfig | None:
    """The config whose ``name`` is the longest that ``module`` lies in, as
    Django's ``Apps.get_containing_app_config`` finds it.
    """
    found = [c for c in configs if _within(module, c.name)]
    return max(found, key=lambda c: len(c.name), default=None)


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _config_key(config: AppConfig) -> str:
    """How a tie names a config made from an entry: its class, which every
    spelling of the entry makes, since an app's name would also match the app
    a config with another name points at, installed alone. Django's own
    ``AppConfig`` is shared by many apps, so its apps go by name.
    """
    kind = type(config)
    if kind is AppConfig:
        return config.name
    return _naming.full_name(kind, None)


def _name_pending(configs: Sequence[AppConfig]) -> None:
    """Tie each request made while an entry of ``INSTALLED_APPS`` was
    imported, now that its load has made the configs it could; ``configs``
    are this set-up's.

    A request belongs to the config its entry made, or to no app when the
    load stopped at that entry. A request made as a module was imported also
    belongs to that config when the module is the entry's own (the entry, or
    the module that defines the entry's class) or lies below it, which keeps
    a config whose ``name`` points elsewhere; otherwise to what
    ``_module_ties`` finds among that load's configs, then this set-up's.
    """
    named: dict[str, set[str | None]] = {}
    for key, (module, entry, entries, made) in _PENDING.items():
        loaded = list(made.values())
        # By identity: an entry's own __eq__ never runs (#100), and the copy
        # of the entries holds the same objects.
        at = next((i for i, e in enumerate(entries) if e is entry), len(loaded))
        config = loaded[at] if at < len(loaded) else None
        own: str | None = None if config is None else _config_key(config)
        ties: set[str | None] = {own}
        text = str.__str__(entry)
        home = text if text in sys.modules else text.rpartition(".")[0]
        if module is not None and (own is None or not _within(module, home)):
            ties |= _module_ties(module, loaded, configs)
        named[key] = ties
    _PENDING.clear()
    for places in _RECORDED.values():
        for place, held in places.items():
            if any(tie in named for tie in held if tie is not None):
                tied: dict[str | None, None] = {}
                for tie in held:
                    tied.update(
                        dict.fromkeys(
                            named[tie] if tie is not None and tie in named else {tie}
                        )
                    )
                places[place] = tied


def _populating() -> Mapping[str, object] | None:
    """The local variables of ``Apps.populate`` for Django's global registry,
    if this thread runs it.
    """
    frame = inspect.currentframe()
    try:
        while frame is not None:
            if frame.f_code is _POPULATE and frame.f_locals.get("self") is global_apps:
                return frame.f_locals
            frame = frame.f_back
        return None
    finally:
        del frame


def _principals(config: AppConfig) -> tuple[_principal.Built, ...]:
    """Build the backends of ``NEOLITH["PRINCIPAL_BACKENDS"]`` when its form
    is one ``neolith.E001`` admits, and record on ``config`` what was refused.
    """
    refusals: list[str] = []
    built: tuple[_principal.Built, ...] = ()
    neolith = getattr(settings, "NEOLITH", None)
    if type(neolith) is dict:
        # Looked up among exact str keys only: a key's own __eq__ never runs.
        value: Any = _naming.entry(neolith, _principal.KEY)
        if value is not None and not _principal.shape_problems(value):
            built, refusals = _principal.build(value)
    setattr(config, _checks.PRINCIPAL_REFUSALS, tuple(refusals))
    return built


def _remote(
    config: AppConfig,
    connectors: Mapping[str, Bound],
    configs: Iterable[AppConfig],
    callers: _caller.Callers,
) -> Mapping[str, Bound]:
    """``connectors`` with a connector over HTTP to each remote provider of
    ``settings.NEOLITH``, which set-up's checks have passed: under
    ``single``, a contract's one provider is the app of the host that
    provides it, else its remote provider (ADR 0014 Decision 2). The
    transports share one pool per origin and open nothing until a call.

    The pools belong to the set-up that ``config`` ran, and close when the
    config is gone, such as when Django drops the registry of an override
    that set the host up again, or when the process exits.
    """
    urls = _checks.remote_urls(getattr(settings, "NEOLITH", {}))
    if not urls:
        return connectors
    required = {
        contract.identity: contract
        for declaration in _checks.hosted_declarations(configs)
        for contract in declaration.requires
    }
    pools = _http.Pools()
    chosen = dict(connectors)
    for identity, url in urls.items():
        declaration = required[identity].declaration
        pool = pools.pool(_http.Origin.parse(url))
        transport = _http_transport.HTTPTransport(declaration, pool, callers)
        chosen.setdefault(identity, Bound(declaration, transport))
    weakref.finalize(config, pools.close)
    return types.MappingProxyType(chosen)


def _setting_changed(
    *, setting: str, value: object, enter: bool, **kwargs: object
) -> None:
    """Keep the host's view of ``NEOLITH`` in step with an override, and
    refuse an override of what cannot change after set-up (ADR 0022
    Decision 3). Django ends an override whose receiver raises on entry.
    """
    if setting != "NEOLITH" and setting not in _FIXED:
        return
    with _LOCK:
        baseline = _first_neolith
    if baseline is None:
        return
    if setting in _FIXED:
        if enter:
            raise ImproperlyConfigured(
                f"settings: {setting} cannot change after set-up"
            )
        return
    hosted = _checks.hosted_declarations(global_apps.get_app_configs())
    problems = _checks.neolith_problems(value, hosted)
    if not problems:
        given: dict[str, object] = value  # type: ignore[assignment]  # checked
        others = {k: v for k, v in given.items() if k != _DEADLINE_KEY}
        problems = [
            f'NEOLITH["{key}"]: cannot change after set-up'
            for key in sorted(baseline.keys() | others.keys())
            if key not in baseline
            or key not in others
            or not _same(baseline[key], others[key])
        ]
    if problems:
        if enter:
            raise ImproperlyConfigured("\n".join(sorted(problems)))
        return
    config = global_apps.app_configs.get(_LABEL)
    host = _host_of(config)
    deadline = _checks.request_deadline(value)  # type: ignore[arg-type]
    if host is not None and host.request_deadline != deadline:
        setattr(
            config, _HOST_ATTR, dataclasses.replace(host, request_deadline=deadline)
        )


def _same(first: object, second: object) -> bool:
    try:
        return type(first) is type(second) and bool(first == second)
    except Exception:
        return False


def _judge(found: list[checks.CheckMessage]) -> float | None:
    """Judge what the host's system checks found; return the checked request
    deadline.

    Errors are raised together, sorted, whatever ``SILENCED_SYSTEM_CHECKS``
    says: silencing hides a message from ``manage.py check``, and a host that
    serves runs no command. Warnings are logged, each once per set-up, unless
    silenced, and ``manage.py check`` still prints them.
    """
    errors = sorted(f"({m.id}) {m.msg}" for m in found if m.level >= checks.ERROR)
    if errors:
        _refuse(
            ImproperlyConfigured(
                "host: the settings are refused before the host serves:\n"
                + "\n".join(errors)
            )
        )
    for message in sorted(found, key=lambda m: (str(m.id), m.msg)):
        if message.level >= checks.WARNING and not message.is_silenced():
            with contextlib.suppress(Exception):
                _log.warning("(%s) %s", message.id, message.msg)
    return _checks.request_deadline(getattr(settings, "NEOLITH", {}))


def _refuse(error: ImproperlyConfigured) -> NoReturn:
    """Log a refusal, then raise it: a server that loads the app again sees
    Django's "populate() isn't reentrant", and the log still has the cause.
    """
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
    none behind it. Resolve reads the remote providers that
    ``settings.NEOLITH`` names; when it refuses, every problem of
    ``NEOLITH``, and each app that requires a remote provider's contract
    with no signing key, comes with its refusals. Set-up adds the remote
    providers' transports to the table once its checks have passed
    (``_remote``).
    """
    hosted = [
        (config, found)
        for config in registry.get_app_configs()
        if (found := declaration_of(type(config))) is not None
    ]
    neolith = getattr(settings, "NEOLITH", _checks.MISSING)
    declarations = [declaration for _, declaration in hosted]
    contracts, refusals = resolve(declarations, _checks.remote_identities(neolith))
    if refusals:
        # Resolve reads NEOLITH, so a requirement it finds unmet may come
        # from a problem there: those, and its remote providers' callers
        # that have no signing key, are reported with its refusals.
        refusals += _checks.neolith_problems(neolith, declarations)
        refusals += _checks.unsigned_problems(neolith, declarations)
    connectors: dict[str, Bound] = {}
    for config, declaration in hosted:
        for provision in declaration.provides:
            try:
                validating = _VALIDATING.set(config.name)
                try:
                    instance = validate(registry, config, provision, refusals)
                finally:
                    _VALIDATING.reset(validating)
            except Exception as error:  # a read validate() did not foresee
                refusals.append(
                    f"app {config.label}: provides: {provision.contract.identity}: "
                    f"the implementation cannot be checked: {_cause(error)}"
                )
                instance = None
            if instance is not None:
                connectors.setdefault(
                    provision.contract.identity,
                    Bound(
                        provision.contract.declaration,
                        InProcess(
                            provision.contract.declaration,
                            instance,
                            app=declaration.label,
                            requires=tuple(
                                required.identity for required in declaration.requires
                            ),
                            principal_from=provision.principal_from,
                        ),
                    ),
                )
    return (
        Host(
            known=frozenset(contracts),
            connectors=types.MappingProxyType(connectors),
            apps=types.MappingProxyType(
                {
                    declaration.label: tuple(
                        required.identity for required in declaration.requires
                    )
                    for _, declaration in hosted
                }
            ),
        ),
        refusals,
    )


def resolve(
    declarations: Sequence[Declaration], remote: Collection[str] = ()
) -> tuple[dict[str, Contract], list[str]]:
    """Resolve: the contracts the host knows by identity, and the refusals.

    Each identity keeps the first class named for it, in the order of the
    declarations; a later app that names another class with that identity,
    or provides it again, is refused naming the first. A requirement is
    met by the app that provides it or by a remote provider, one of the
    identities ``remote`` names; a remote identity that no app requires, or
    that an app provides, is refused (ADR 0014, the Resolve rows).
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
    required = {c.identity for d in declarations for c in d.requires}
    for identity in sorted(remote):
        if identity not in required:
            refusals.append(
                f"{_checks.REMOTE}: {identity} is required by no hosted app; remove it"
            )
        if identity in providers:
            refusals.append(
                f"{_checks.REMOTE}: {identity} is provided by app "
                f"{providers[identity]} in this host; a contract provided in the "
                "host and remotely at once needs a routing policy other than "
                "single, which arrives with step 6 (ADR 0016)"
            )
    for declaration in declarations:
        for contract in declaration.requires:
            if contract.identity not in providers and contract.identity not in remote:
                refusals.append(
                    f"app {declaration.label}: requires: {contract.identity} has "
                    "no provider; no app of this host provides it and "
                    f"{_checks.REMOTE} has no entry for it"
                )
    return {identity: found for identity, (found, _) in named.items()}, refusals


def _class(contract: Contract) -> str:
    declaration = contract.declaration
    return _naming.full_name(declaration, None)


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
    if not path.startswith(f"{config.name}."):
        refusals.append(
            f"{where} is outside the app's package, {_long(config.name)}; an "
            "implementation belongs to the app that provides it, so its path "
            f'starts with "{_long(config.name)}."'
        )
        # A path outside the package cannot name a class of the app, so
        # nothing outside it is imported or built.
        return None
    module_name, _, attribute = path.rpartition(".")
    container = registry.get_containing_app_config(module_name)
    if container is not config:
        owner = "no app" if container is None else f"app {container.label}"
        refusals.append(
            f"{where}: {_long(module_name)} belongs to {owner}; an "
            f"implementation belongs to the app that provides it, {config.name}"
        )
        # Another app's module, such as a nested app's, is not imported.
        return None
    try:
        module = importlib.import_module(module_name)
        # A lazily imported module runs its code as it is read, so it is
        # read here, as the import is.
        held = _naming.module_dict(module)
    except Exception as error:
        refusals.append(f"{where} does not import: {_cause(error)}")
        _log_cause(where, error)
        # Nothing of the implementation can be read.
        return None
    if held is None:
        refusals.append(
            f"{where} does not import: {_long(module_name)} is not a module "
            f"whose names can be read (it is a {_naming.type_name(module)})"
        )
        return None
    found: Any = _naming.entry(held, attribute, _NOTHING)
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
    own_module = _naming.module(cls)
    own_name = _naming.qualname(cls, None)
    # Only a class proven to be the app's own is built.
    foreign = True
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
        foreign = _outside(registry, config, own_module, where, refusals)
        problem |= foreign or _declared_with(config, own_module, where, refusals)
    # The check adds one problem for any step that raises, so it returns.
    found_problems = provision.contract._implementation_problems(cls)
    for found_problem in found_problems:
        refusals.append(f"app {app}: provides: {found_problem}")
        problem = True
    if foreign:
        # A class of another app, or of none, is never built here.
        return None
    # Built even when another check above failed, so that a failing __init__
    # is reported beside the rest.
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


def _outside(
    registry: Apps, config: AppConfig, module: str, where: str, refusals: list[str]
) -> bool:
    """Refuse a class whose module belongs to another app or to none."""
    container = registry.get_containing_app_config(module)
    if container is config:
        return False
    owner = "no app" if container is None else f"app {container.label}"
    refusals.append(
        f"{where}: the class is in {_long(module)}, which belongs to {owner}; an "
        f"implementation belongs to the app that provides it, {config.name}"
    )
    return True


def _declared_with(
    config: AppConfig, module: str, where: str, refusals: list[str]
) -> bool:
    """Refuse a class in a module that is read with the declaration."""
    declared_in = _naming.module(type(config))
    if module not in (config.name, declared_in):
        return False
    refusals.append(
        f"{where}: the class is in {_long(module)}, which is read with the "
        "declaration; put the implementation in another module of the "
        f"app, such as {config.name}.services"
    )
    return True


# Names and paths are shown whole up to this length, so advice can be copied.
_LONG: Final = 160


def _long(text: str) -> str:
    return text if len(text) <= _LONG else f"{text[:_LONG]}..."


def _quoted(text: str) -> str:
    return repr(text) if len(text) <= _LONG else f"{text[:_LONG]!r}..."


def _cause(error: Exception) -> str:
    """An error's class and message, bounded, never raising."""
    return _naming.cause(error, _CLIPPED + 3)


def _log_cause(where: str, error: Exception) -> None:
    with contextlib.suppress(Exception):
        _log.error("%s", where, exc_info=error)


def connect(identity: str, /) -> "Connector":
    """Return a connector to the contract version ``identity``.

    ``identity`` is ``<name>.v<N>``, a contract a hosted app provides or
    requires. ``connect()`` may run at import: before the host is set up, the
    identity is recorded, and set-up stops the host if no app knows it; after,
    an identity the host does not know is a ``UsageError`` at once.

    A call through the connector raises ``UsageError``, never a contract
    error, when it is made before set-up has finished or while an override
    leaves neolith's app out of the registry; when no app of the host
    provides the contract; when it is made in no open scope or in one that
    has closed; when it names with ``context=`` a scope that has closed or
    that this host did not issue; and when the calling app's declaration does
    not require the contract, or the app has none.

    Args:
        identity: The contract version, ``<name>.v<N>``.

    Returns:
        A connector that holds only ``identity``.

    Raises:
        TypeError: If ``identity`` is not a ``str``.
        ValueError: If ``identity`` is not of the form ``<name>.v<N>``.
        UsageError: If the host is set up and knows no contract ``identity``.
    """
    if type(identity) is not str:
        raise TypeError(
            f"connect() takes a contract identity as a str, not "
            f"{_naming.type_name(identity)}"
        )
    if not _IDENTITY.fullmatch(identity):
        raise ValueError(
            f"{_show(identity)} is not a contract identity: write <name>.v<N>, "
            "for example 'inventory.v1'"
        )
    with _LOCK:
        host, _ = _table()
        if host is None:
            host = _restored_elsewhere()
        if host is None:
            where, module, at_import, importing = _place()
            place = _RECORDED.setdefault(identity, {}).setdefault(where, {})
            place.update(dict.fromkeys(_ties(module, at_import) | _below(importing)))
        elif identity not in host.known:
            raise UsageError(
                f"connect({_quoted(identity)}): no app of this host provides or "
                f"requires {_long(identity)}; {_knows(host)}"
            )
    return Connector._made(identity)


def _place() -> tuple[str, str, bool, tuple[str, ...]]:
    """The file and line of the ``connect()`` call, for the message only; the
    name of its module, or ``""``; whether it runs as the module is imported,
    at the module's top level; and the modules whose import is under way in
    the loading step that asked, read from the frames between the call and
    ``Apps.populate``, or none when no load runs.
    """
    frame = inspect.currentframe()
    try:
        caller = None if frame is None or frame.f_back is None else frame.f_back.f_back
        if caller is None:
            return "an unknown place", "", False, ()
        module = caller.f_globals.get("__name__")
        importing: list[str] = []
        step: types.FrameType | None = caller
        while step is not None and step.f_code is not _POPULATE:
            name = step.f_globals.get("__name__")
            if step.f_code.co_name == "<module>" and type(name) is str:
                importing.append(name)
            step = step.f_back
        return (
            f"{caller.f_code.co_filename}:{caller.f_lineno}",
            module if type(module) is str else "",
            caller.f_code.co_name == "<module>",
            tuple(importing) if step is not None else (),
        )
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
class Connector:
    """A connector to one contract version, as ``connect()`` returns it.

    It holds its identity and nothing else. Each action of the contract is an
    attribute, called with one request; every use finds the current host's
    table by the identity. Only ``connect()`` makes one, so that its identity
    is always checked or recorded for set-up (spec 05).

    Raises:
        TypeError: The class is called or subclassed.
    """

    __slots__ = ("_identity",)

    _identity: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "a connector is made by neolith.connect(), not by its class: "
            "write connect('<contract>.v<N>')"
        )

    def __init_subclass__(cls, **kwargs: object) -> NoReturn:
        raise TypeError("neolith.Connector cannot be subclassed")

    @classmethod
    def _made(cls, identity: str) -> "Connector":
        made = object.__new__(cls)
        object.__setattr__(made, "_identity", identity)
        return made

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

    def __copy__(self) -> "Connector":
        return self

    def __deepcopy__(self, memo: object) -> "Connector":
        return self

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(
            f"the connector to {self._identity} cannot be pickled: call "
            f"connect({self._identity!r}) where it is needed"
        )


def _connector(identity: str) -> Bound:
    host, installed = _table()
    if host is None:
        raise _not_set_up(identity)
    if not installed:
        raise UsageError(
            f"connector to {_long(identity)}: neolith's app is not in Django's "
            "registry while an override or set_available_apps leaves it out; "
            "calls resume when that ends"
        )
    # A test that entered over_http sends this identity's calls over HTTP.
    found = _http_transport.seamed(identity) or host.connectors.get(identity)
    if found is None:
        # Known identities all have a provider once set-up passed; this is a
        # connector recorded in a process whose host knows other contracts.
        raise UsageError(
            f"connector to {identity}: no app of this host provides it; {_knows(host)}"
        )
    return found


def command_scope(
    app: str,
    *,
    principal: Principal | None = None,
    timeout: float | None = None,
) -> contextlib.AbstractContextManager[None]:
    """Open a root for a command or background work, run for a hosted app.

    Use it once, with ``with``, in sync and async code alike. Calls made in
    the block call as ``app``, are checked against what its declaration
    requires, and carry ``principal``. The root takes nothing from a scope it
    is opened in: it starts a new trace, and has a deadline only when
    ``timeout`` is given, counted from entry. The root closes on exit,
    however the block ends, so a call made in it afterwards, or naming its
    context, is a ``UsageError``.

    An ``await`` inside the block is fine. A generator that yields inside
    the block must finish, or be closed, where the scope is still current,
    in the context that entered it; roots opened in the block close first.
    Then the enclosing scope is current again on exit. A scope that finds itself
    not current where its block ends, or cannot be reset there, still closes,
    and its exit raises ``UsageError``, with the block's own exception, if
    any, as its context; calls in the context it was left current in are
    refused, since it has closed. A signal to stop that the block ends with
    (cancellation, ``KeyboardInterrupt``, ``SystemExit``) is never replaced:
    it propagates, with the report added to it as a note and logged on the
    ``neolith.host`` logger.

    The arguments are checked when it is called, and the host on entry.

    Args:
        app: The label of a hosted app: an installed app whose config class
            declares it.
        principal: On whose behalf the calls are made, or ``None``.
        timeout: The root's deadline, in seconds from entry, or ``None`` for
            none. Zero or less gives a root whose calls fail with
            ``timeout``.

    Returns:
        A context manager whose ``with`` block is the root; it yields
        nothing.

    Raises:
        TypeError: If ``app`` is not a ``str``, ``principal`` not a
            ``Principal`` or ``None``, or ``timeout`` not an ``int``, a
            ``float`` or ``None`` (a ``bool`` is refused).
        ValueError: If ``timeout`` is not finite.
        UsageError: On entry, if the host is not set up, if ``app`` is not
            the label of a hosted app, or if the scope was opened before; a
            refused entry opens nothing, and can be tried again. On exit,
            if the scope is not the one current where its block ends, or is
            current there but in another context than it was entered in;
            unless the block ends with a signal to stop.
    """
    if type(app) is not str:
        raise TypeError(
            "command_scope() takes an app's label as a str, not "
            f"{_naming.type_name(app)}"
        )
    if principal is not None and type(principal) is not Principal:
        raise TypeError(
            f"principal= takes a Principal, not {_naming.type_name(principal)}"
        )
    return _CommandScope._made(app, principal, _context.timeout_option(timeout))


@final
class _CommandScope:
    """One command scope, entered at most once."""

    __slots__ = (
        "_app",
        "_entered",
        "_lock",
        "_principal",
        "_scope",
        "_timeout",
        "_token",
    )

    _app: str
    _principal: Principal | None
    _timeout: float | None
    _entered: bool
    _lock: threading.Lock
    _scope: _context.Scope | None
    _token: contextvars.Token[_context.Scope | None] | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("a command scope is made by neolith.command_scope()")

    @classmethod
    def _made(
        cls, app: str, principal: Principal | None, timeout: float | None
    ) -> "_CommandScope":
        made = object.__new__(cls)
        made._app = app
        made._principal = principal
        made._timeout = timeout
        made._entered = False
        made._lock = threading.Lock()
        made._scope = None
        made._token = None
        return made

    def __enter__(self) -> None:
        # One lock around the checks and the opening, so that of two threads
        # entering one scope, exactly one opens it. A refused entry opens
        # nothing and leaves the scope as it was.
        with self._lock:
            if self._entered:
                raise UsageError(
                    f"{self._where()}: a command scope is entered once, and this "
                    "one was entered before; call command_scope() again for a "
                    "new root"
                )
            fields = _context.root_fields(
                self._app,
                requires=self._requires(),
                principal=self._principal,
                timeout=self._timeout,
            )
            self._scope, self._token = _context.begin(**fields)
            self._entered = True

    def _where(self) -> str:
        return f"command_scope({_quoted(self._app)})"

    def _requires(self) -> tuple[str, ...]:
        """What the named app requires, from the table of the set-up that
        Django's global registry holds now; ``UsageError`` when there is no
        such table or the app is not hosted.
        """
        with _LOCK:
            host, installed = _table()
        if host is None:
            raise UsageError(
                f"{self._where()}: the host is not set up yet. A command scope "
                "opens once Django has loaded every app and neolith's set-up "
                "has passed"
            )
        if not installed:
            raise UsageError(
                f"{self._where()}: the host is not set up: neolith's app is not in "
                "Django's registry while an override or set_available_apps "
                "leaves it out"
            )
        requires = host.apps.get(self._app)
        if requires is None:
            hosted = ", ".join(sorted(host.apps)) or "none"
            raise UsageError(
                f"{self._where()}: {_quoted(self._app)} is not a hosted app; the "
                f"hosted apps are {hosted}. A scope is named by an app's label, "
                "and an app is hosted when its config class declares it (a "
                "NeolithAppConfig subclass)"
            )
        return requires

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        scope, token = self._scope, self._token
        self._scope = self._token = None
        if scope is None or token is None:
            return
        current = _context._CURRENT.get() is scope
        _context.close(scope)
        if current:
            try:
                _context._CURRENT.reset(token)
            except ValueError:
                found = (
                    "the scope is current where its block ends, but in another "
                    "context than the one it was entered in"
                )
            else:
                return
        else:
            found = (
                "the scope is not the current scope where its block ends: a "
                "root opened in it is still open, its block was left in "
                "another context than it was entered in, or another scope was "
                "made current after it was entered, such as around a generator "
                "suspended inside it"
            )
        # The state found is not the one the block opened: say so, rather
        # than leave a context where calls fail later for no visible reason.
        report = (
            f"{self._where()}: {found}, so the scope it was opened in cannot be "
            "made current again there. The scope has closed. Open and close a "
            "command scope in one block that does not yield, and close the "
            "roots opened in it first"
        )
        if error is not None and not isinstance(error, Exception):
            # A signal to stop, such as cancellation, KeyboardInterrupt or
            # SystemExit, is never replaced: it goes on, carrying the report.
            # It is logged too, since a GeneratorExit that closes a generator
            # is swallowed with its notes.
            error.add_note(f"neolith.UsageError: {report}")
            with contextlib.suppress(Exception):
                _log.error("%s", report)
            return
        raise UsageError(report)

    def __repr__(self) -> str:
        return f"<command scope for {_quoted(self._app)}>"

    def __copy__(self) -> NoReturn:
        raise TypeError(self._unique())

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError(self._unique())

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(self._unique())

    def _unique(self) -> str:
        return (
            "a command scope is one root and cannot be copied or pickled: "
            "call command_scope() again where a root is needed"
        )
