"""The host's checks of its settings and its request entry (ADR 0022).

Each rule is a plain function over the value it judges, which returns every
problem it finds, so one function serves every place that checks the same
input: set-up, through the system checks registered under the tag
``neolith``, and the root middleware's constructor. The admitted set of each
input is stated in its function; anything else is a problem, never a pass.

The ids are the ones this step fixes from ADR 0022's working numbers:
``neolith.E001`` for ``NEOLITH``, ``neolith.E002`` for
``SILENCED_SYSTEM_CHECKS``, ``neolith.E003`` for a declared app installed
as an undeclared app or a battery, ``neolith.E004`` for an undeclared app
with models in a closed host, ``neolith.E006`` for ``MIDDLEWARE``,
``neolith.E014`` for a hosted app with models and no database,
``neolith.E015`` for a ``default`` that nothing lives on, ``neolith.E016``
for a database cache in a closed host, ``neolith.E017`` for a table name
that ``default`` shortened (ADR 0009), ``neolith.E019`` for a principal
backend that set-up could not build and ``neolith.E020`` for Django's cache
middleware listed after the principal middleware (ADR 0023),
``neolith.E021`` for a caller backend or signing key that set-up
refused (ADR 0027), and for a hosted app that requires a remote provider's
contract with no signing key (ADR 0027 Decision 9), ``neolith.W001`` for an
admin class in no installed app, ``neolith.W002`` for each remote
provider entry at a plain ``http`` url that is not loopback (ADR 0014, with
the entry's index from ADR 0031), and ADR 0027's
warnings ``neolith.W004`` (no caller backend), ``neolith.W005`` (a
principal backend that declares a ``neolith-`` header) and, under
``check --deploy``, ``neolith.W006`` (the shared-secret backend listed).
``neolith.E005``, the comparison of the settings the host builds,
runs at the first set-up only (``neolith._configure``), and Boot's ids are
in ``neolith._boot``.
"""

import ipaddress
import math
import sys
from collections.abc import Collection, Iterable, Sequence
from typing import Any, Final, cast

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.conf import settings
from django.core import checks
from django.db.backends.utils import strip_quotes
from django.utils.module_loading import import_string

from neolith import _caller, _databases, _http_transport, _naming, _principal
from neolith._contracts import _IDENTITY
from neolith._declaration import Declaration, declaration_of
from neolith._serving import admin_code

__all__ = [
    "DEFAULT_REQUEST_DEADLINE",
    "KEYS",
    "LOCAL_FIRST",
    "MISSING",
    "PRINCIPAL_MIDDLEWARE",
    "REMOTE",
    "ROOT_MIDDLEWARE",
    "ROUTING",
    "TAG",
    "admin_check",
    "batteries",
    "caller_check",
    "caller_deploy_check",
    "database_check",
    "database_problems",
    "default_check",
    "default_problems",
    "hosted_declarations",
    "installed_check",
    "installed_problems",
    "local_first",
    "middleware_check",
    "middleware_problems",
    "neolith_check",
    "neolith_problems",
    "principal_check",
    "register",
    "remote_check",
    "remote_identities",
    "remote_problems",
    "remote_urls",
    "remote_warnings",
    "request_deadline",
    "routed",
    "routing_problems",
    "silenced_check",
    "silenced_problems",
    "table_name_check",
    "table_name_problems",
    "unsigned_problems",
    "with_database",
]

TAG: Final = "neolith"
ROOT_MIDDLEWARE: Final = "neolith.middleware.RootMiddleware"
PRINCIPAL_MIDDLEWARE: Final = _principal.PRINCIPAL_MIDDLEWARE
# Where set-up records what building the principal backends refused, on the
# neolith app config that ran it, for neolith.E019.
PRINCIPAL_REFUSALS: Final = "_neolith_principal_refusals"
# The request deadline when NEOLITH has no REQUEST_DEADLINE (ADR 0017
# Decision 4), in seconds.
DEFAULT_REQUEST_DEADLINE: Final = 30.0
# The bounds of a configured request deadline, in seconds: 1 ms to
# 9999999999 ms, the budget cap (ADR 0022 Decision 3).
_SHORTEST: Final = 0.001
_LONGEST: Final = 9999999.999
_DEADLINE: Final = 'NEOLITH["REQUEST_DEADLINE"]'
# The key table of NEOLITH (ADR 0022 Decision 3, ADR 0023 for the principal
# backends, ADR 0027 for the caller backends and signing keys, ADR 0014 for
# the remote providers, and ADR 0031 for the routing policies).
KEYS: Final = (
    "CALLER_BACKENDS",
    "DATABASES",
    "PRINCIPAL_BACKENDS",
    "REMOTE_PROVIDERS",
    "REQUEST_DEADLINE",
    "ROUTING_POLICIES",
    "SIGNING_SECRETS",
    "UNDECLARED_APPS",
)
# Stands for a setting that is absent.
MISSING: Final = object()
_MISSING: Final = MISSING
_UNDECLARED: Final = 'NEOLITH["UNDECLARED_APPS"]'
_SHOWN: Final = 80


def _type(value: object) -> str:
    return _naming.type_name(value)


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


def middleware_problems(value: object, *, by_class: bool = True) -> list[str]:
    """Judge ``MIDDLEWARE``: a ``list`` of ``str`` whose first entry is the
    root middleware, which is listed once, and which lists the principal
    middleware once (ADR 0022, the builder's table; ADR 0023 Decision 6).
    Pass ``MISSING`` for a setting that is absent.

    The builder, set-up (``neolith.E006``) and both middleware classes'
    constructors run this one function. "Once" is judged by class: every
    entry that imports as the middleware, under any path, or as a subclass
    of it, counts. An entry that does not import is Django's to
    report when it builds the handler. The builder passes ``by_class=False``
    and counts the root middleware's own path only, since it runs before
    settings exist and imports nothing a deployer listed; set-up judges by
    class before the host serves. Every problem found is returned.
    """
    if value is MISSING:
        return [f"MIDDLEWARE: is required; its first entry is {ROOT_MIDDLEWARE}"]
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
    count = sum(
        _is(entry, ROOT_MIDDLEWARE) if by_class else entry == ROOT_MIDDLEWARE
        for entry in entries
    )
    if count > 1:
        problems.append(f"MIDDLEWARE: {ROOT_MIDDLEWARE} is listed {count} times")
    count = sum(
        _is(entry, PRINCIPAL_MIDDLEWARE) if by_class else entry == PRINCIPAL_MIDDLEWARE
        for entry in entries
    )
    if count == 0:
        problems.append(
            f"MIDDLEWARE: {PRINCIPAL_MIDDLEWARE} is required, listed once after "
            f"{ROOT_MIDDLEWARE}; with no principal backend it does nothing"
        )
    elif count > 1:
        problems.append(f"MIDDLEWARE: {PRINCIPAL_MIDDLEWARE} is listed {count} times")
    return problems


def _is(entry: object, path: str) -> bool:
    """Whether ``entry`` imports as the class at ``path`` or a subclass."""
    if type(entry) is not str:
        return False
    if entry == path:
        return True
    # Imported here: neolith.middleware imports this module.
    from neolith import middleware

    base = getattr(middleware, path.rpartition(".")[2])
    try:
        found = import_string(entry)
    except Exception:  # Django reports it when it builds the handler
        return False
    return issubclass(type(found), type) and issubclass(found, base)


def _deadline_problems(value: object) -> list[str]:
    if value is None:
        return []
    if not _naming.one_of(type(value), (int, float)):
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


def neolith_problems(
    value: object = _MISSING, hosted: Sequence[Declaration] = ()
) -> list[str]:
    """Judge ``NEOLITH``: present, a ``dict`` whose keys are in the key
    table, each with its one form (ADR 0022 Decision 3), against the
    declarations of the hosted apps. Leave ``value`` out when the setting is
    absent.
    """
    if value is _MISSING:
        return ["settings: NEOLITH is required; it is {} when nothing is set"]
    if type(value) is not dict:
        return [f"NEOLITH: expected a dict, got {_type(value)}"]
    problems: list[str] = []
    # Items are read, never looked up, so no key's own hooks run.
    entries: dict[object, object] = value
    for key, item in entries.items():
        if type(key) is not str or key not in KEYS:
            problems.append(
                f"NEOLITH: unknown key {_shown_key(key)}; the keys are "
                + ", ".join(sorted(KEYS))
            )
        elif key == "REQUEST_DEADLINE":
            problems.extend(_deadline_problems(item))
        elif key == "DATABASES":
            problems.extend(_databases.databases_problems(item, with_database(hosted)))
        elif key == "UNDECLARED_APPS":
            problems.extend(_undeclared_problems(item, hosted))
        elif key == _principal.KEY:
            problems.extend(_principal.shape_problems(item))
        elif key == _caller.KEY:
            problems.extend(_caller.shape_problems(item))
        elif key == _caller.SIGNING:
            problems.extend(_caller.signing_problems(item, [d.label for d in hosted]))
        elif key == _REMOTE_KEY:
            problems.extend(remote_problems(item, local_first(entries)))
        elif key == _ROUTING_KEY:
            problems.extend(routing_problems(item))
    if not _naming.holds(entries, "DATABASES"):
        problems.extend(
            f"app {label}: no connection settings for its database"
            for label in with_database(hosted)
        )
    return problems


def with_database(hosted: Iterable[Declaration]) -> list[str]:
    """The labels of the hosted apps that declare the ``database``
    resource, in order.
    """
    return [d.label for d in hosted if "database" in d.resources]


def batteries(hosted: Iterable[Declaration]) -> dict[str, str]:
    """Each battery, in the order the hosted apps name them, with the label
    of the first app that names it.
    """
    found: dict[str, str] = {}
    for declaration in hosted:
        for battery in declaration.batteries:
            found.setdefault(battery, declaration.label)
    return found


def _undeclared_problems(value: object, hosted: Sequence[Declaration]) -> list[str]:
    """``NEOLITH["UNDECLARED_APPS"]``: a ``list`` of one or more ``str``,
    each an ``INSTALLED_APPS`` entry, none twice, none a hosted app, a
    battery or ``neolith``, each compared as written.
    """
    if type(value) is not list:
        return [f"{_UNDECLARED}: expected a list of str, got {_type(value)}"]
    entries: list[object] = value
    if not entries:
        return [f"{_UNDECLARED}: is empty; leave the key out"]
    names = {d.name for d in hosted}
    named = batteries(hosted)
    problems: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if type(entry) is not str:
            problems.append(
                f"{_UNDECLARED}: entry {index}: expected a str, got {_type(entry)}"
            )
            continue
        if entry in seen:
            problems.append(f"{_UNDECLARED}: {entry} listed twice")
            continue
        seen.add(entry)
        if entry == TAG:
            problems.append(f"{_UNDECLARED}: neolith is installed by the host")
        elif entry in names:
            problems.append(f"{_UNDECLARED}: {entry} is a hosted app")
        elif entry in named:
            problems.append(
                f"{_UNDECLARED}: {entry} is installed as a battery of {named[entry]}"
            )
    return problems


# NEOLITH["REMOTE_PROVIDERS"] (ADR 0014, the admitted set).
_REMOTE_KEY: Final = "REMOTE_PROVIDERS"
REMOTE: Final = f'NEOLITH["{_REMOTE_KEY}"]'
_TRANSPORT: Final = "neolith.transports.HTTPTransport"
_ENTRY_KEYS: Final = ("BACKEND", "OPTIONS")
_URL: Final = "url"


def remote_problems(value: object, routed: Collection[str] = ()) -> list[str]:
    """Judge the form of ``NEOLITH["REMOTE_PROVIDERS"]`` (ADR 0014, the
    builder's rows, as ADR 0031 widens them): a ``dict`` of one or more
    contract identities, each mapped to a ``list`` of one or more entries,
    two or more only for an identity in ``routed``, the identities
    ``NEOLITH["ROUTING_POLICIES"]`` names ``local-first`` for (``local_first``).
    Each entry is a ``dict`` with exactly ``BACKEND``,
    ``"neolith.transports.HTTPTransport"`` as written, and ``OPTIONS``, a
    ``dict`` with exactly ``url``, an origin in its one spelling, and no two
    entries of one identity have one ``url``, compared as written. Every
    container is exactly the type named.

    Every problem found is returned, those under a refused key included. A
    refused ``url`` is never shown, in whole or in part (ADR 0014 Decision
    3). A refused key or ``BACKEND`` is shown as the record's table writes
    it: an exact ``str`` by its bounded ``repr``, anything else by its type
    alone, so no author code runs.
    """
    if type(value) is not dict:
        return [f"{REMOTE}: expected a dict of contract identities, got {_type(value)}"]
    entries: dict[object, object] = value
    if not entries:
        return [f"{REMOTE}: is empty; leave the key out"]
    problems: list[str] = []
    # Items are read, never looked up, so no key's own hooks run.
    for key, providers in entries.items():
        if type(key) is str and _IDENTITY.fullmatch(key):
            where, named = f'{REMOTE}["{key}"]', key
        else:
            shown = _shown_key(key)
            problems.append(
                f"{REMOTE}: key {shown}: expected a contract identity, such as "
                "<contract>.v1"
            )
            where, named = f"{REMOTE}[{shown}]", "it"
        ordered = type(key) is str and key in routed
        problems.extend(_providers_problems(where, named, providers, ordered))
    return problems


def _providers_problems(
    where: str, named: str, value: object, ordered: bool
) -> list[str]:
    if type(value) is not list:
        return [f"{where}: expected a list of providers, got {_type(value)}"]
    providers: list[object] = value
    if not providers:
        return [f"{where}: is empty; leave {named} out"]
    problems: list[str] = []
    if len(providers) > 1 and not ordered:
        problems.append(
            f"{where}: lists {len(providers)} providers; with the single routing "
            "policy a contract has one provider; to try them in order, name "
            f"{LOCAL_FIRST} for {named} in {ROUTING}"
        )
    first: dict[str, int] = {}
    for index, entry in enumerate(providers):
        problems.extend(_entry_problems(f"{where}[{index}]", entry))
        url = _url_of(entry)
        if url is None:
            continue
        # Compared as written: one string is one entry listed twice.
        earlier = first.setdefault(url, index)
        if earlier != index:
            problems.append(
                f'{where}[{index}]["OPTIONS"]["url"]: is the url of entry '
                f"{earlier} too; an entry is listed once"
            )
    return problems


def _url_of(entry: object) -> str | None:
    """An entry's ``url``, when it is an exact ``str`` where the form puts
    it, whatever else the entry holds; ``None`` otherwise.
    """
    if type(entry) is not dict:
        return None
    options = _naming.entry(cast("dict[object, object]", entry), "OPTIONS")
    if type(options) is not dict:
        return None
    url = _naming.entry(cast("dict[object, object]", options), _URL)
    return url if type(url) is str else None


def _entry_problems(where: str, value: object) -> list[str]:
    if type(value) is not dict:
        return [
            f"{where}: expected a dict with the keys BACKEND and OPTIONS, got "
            f"{_type(value)}"
        ]
    entry: dict[object, object] = value
    problems = _keys_problems(
        where, entry, _ENTRY_KEYS, "the keys are BACKEND, OPTIONS"
    )
    backend = _naming.entry(entry, "BACKEND", MISSING)
    if backend is not MISSING:
        if type(backend) is not str:
            problems.append(f'{where}["BACKEND"]: expected a str, got {_type(backend)}')
        elif backend != _TRANSPORT:
            problems.append(
                f'{where}["BACKEND"]: {_shown_key(backend)} is not a transport of '
                f"this host; the one remote transport in 0.1 is {_TRANSPORT}"
            )
    options = _naming.entry(entry, "OPTIONS", MISSING)
    if options is not MISSING:
        problems.extend(_options_problems(f'{where}["OPTIONS"]', options))
    return problems


def _options_problems(where: str, value: object) -> list[str]:
    if type(value) is not dict:
        return [f"{where}: expected a dict, got {_type(value)}"]
    options: dict[object, object] = value
    problems = _keys_problems(
        where, options, (_URL,), "the HTTP transport's options are url"
    )
    url = _naming.entry(options, _URL, MISSING)
    if url is MISSING:
        return problems
    if type(url) is not str:
        problems.append(f'{where}["url"]: expected a str, got {_type(url)}')
        return problems
    reason = _http_transport.origin_problem(url)
    if reason is not None:
        problems.append(f'{where}["url"]: is not an origin: {reason}')
    return problems


def _keys_problems(
    where: str, held: dict[object, object], keys: Sequence[str], advice: str
) -> list[str]:
    """Each key of ``held`` outside ``keys``, and each key of ``keys`` it
    does not hold.
    """
    problems = [
        f"{where}: unknown key {_shown_key(key)}; {advice}"
        for key in held
        if type(key) is not str or key not in keys
    ]
    problems.extend(
        f"{where}: missing key {key}" for key in keys if not _naming.holds(held, key)
    )
    return problems


# NEOLITH["ROUTING_POLICIES"] (ADR 0031, the admitted set).
_ROUTING_KEY: Final = "ROUTING_POLICIES"
ROUTING: Final = f'NEOLITH["{_ROUTING_KEY}"]'
LOCAL_FIRST: Final = "local-first"
_SINGLE: Final = "single"


def routing_problems(value: object) -> list[str]:
    """Judge the form of ``NEOLITH["ROUTING_POLICIES"]`` (ADR 0031, the
    builder's rows): a ``dict`` of one or more contract identities, each
    mapped to exactly the ``str`` ``"local-first"``, compared as written.
    ``single`` is what an identity left out means, so it is not a value.

    Every problem found is returned, those under a refused key included. A
    refused value is never shown; a refused key is shown as
    ``remote_problems`` shows one.
    """
    if type(value) is not dict:
        return [
            f"{ROUTING}: expected a dict of contract identities, got {_type(value)}"
        ]
    entries: dict[object, object] = value
    if not entries:
        return [f"{ROUTING}: is empty; leave the key out"]
    problems: list[str] = []
    # Items are read, never looked up, so no key's own hooks run.
    for key, policy in entries.items():
        if type(key) is str and _IDENTITY.fullmatch(key):
            where, named = f'{ROUTING}["{key}"]', key
        else:
            shown = _shown_key(key)
            problems.append(
                f"{ROUTING}: key {shown}: expected a contract identity, such as "
                "<contract>.v1"
            )
            where, named = f"{ROUTING}[{shown}]", "it"
        if type(policy) is not str:
            problems.append(f"{where}: expected a str, got {_type(policy)}")
        elif policy == _SINGLE:
            problems.append(
                f"{where}: {_SINGLE} is what leaving {named} out means; leave it out"
            )
        elif policy != LOCAL_FIRST:
            problems.append(
                f"{where}: is not a routing policy of this host; the one policy "
                f"to name is {LOCAL_FIRST}, and leaving {named} out means "
                f"{_SINGLE}"
            )
    return problems


def _routing(neolith: object) -> dict[object, object]:
    """``NEOLITH["ROUTING_POLICIES"]`` when both are a ``dict``; an empty
    ``dict`` otherwise.
    """
    if type(neolith) is not dict:
        return {}
    value = _naming.entry(cast("dict[object, object]", neolith), _ROUTING_KEY)
    return cast("dict[object, object]", value) if type(value) is dict else {}


def local_first(neolith: object) -> frozenset[str]:
    """The contract identities ``NEOLITH["ROUTING_POLICIES"]`` names
    ``local-first`` for, exactly as written, whatever the rest of
    ``neolith`` holds.
    """
    return frozenset(
        key
        for key, policy in _routing(neolith).items()
        if type(key) is str
        and _IDENTITY.fullmatch(key)
        and type(policy) is str
        and policy == LOCAL_FIRST
    )


def routed(neolith: object) -> dict[str, int | None]:
    """Each contract identity ``NEOLITH["ROUTING_POLICIES"]`` names,
    whatever its policy, as Resolve reads them (ADR 0031, the Resolve rows):
    for one named ``local-first`` as written, the number of entries
    ``NEOLITH["REMOTE_PROVIDERS"]`` lists for it, 0 when it has no list;
    for any other value, which its own row refuses, ``None``.
    """
    listed = _listed(neolith)
    ordered = local_first(neolith)
    return {
        key: (listed[key][0] if key in listed else 0) if key in ordered else None
        for key in _routing(neolith)
        if type(key) is str and _IDENTITY.fullmatch(key)
    }


def remote_identities(neolith: object) -> frozenset[str]:
    """The contract identities ``NEOLITH["REMOTE_PROVIDERS"]`` names, as
    Resolve reads them whatever the rest of the key holds: the keys that are
    contract identities, of a ``dict`` under that key of a ``dict``
    ``neolith``; none otherwise.
    """
    if type(neolith) is not dict:
        return frozenset()
    value = _naming.entry(neolith, _REMOTE_KEY)
    if type(value) is not dict:
        return frozenset()
    entries: dict[object, object] = value
    return frozenset(
        key for key in entries if type(key) is str and _IDENTITY.fullmatch(key)
    )


def remote_urls(neolith: object) -> dict[str, tuple[str, ...]]:
    """The ``url`` of each remote provider, in the order listed, by
    identity, for each identity whose entries are all in the admitted form,
    whatever the rest of ``neolith`` holds; in a ``NEOLITH`` that passed
    ``neolith_problems``, every one.
    """
    return {
        identity: tuple(url for _, url in listed)
        for identity, (count, listed) in _listed(neolith).items()
        if count and len(listed) == count
    }


def _remote(neolith: object) -> dict[object, object]:
    """``NEOLITH["REMOTE_PROVIDERS"]`` when both are a ``dict``; an empty
    ``dict`` otherwise.
    """
    if type(neolith) is not dict:
        return {}
    value = _naming.entry(cast("dict[object, object]", neolith), _REMOTE_KEY)
    return cast("dict[object, object]", value) if type(value) is dict else {}


def _listed(neolith: object) -> dict[str, tuple[int, list[tuple[int, str]]]]:
    """For each key of ``NEOLITH["REMOTE_PROVIDERS"]`` that is a contract
    identity and whose value is a ``list``: the number of entries it lists,
    and each entry in the admitted form, with its index and ``url``. An
    entry whose ``url`` an earlier entry has is refused, so it is left out.
    """
    found: dict[str, tuple[int, list[tuple[int, str]]]] = {}
    for key, providers in _remote(neolith).items():
        if type(key) is str and _IDENTITY.fullmatch(key) and type(providers) is list:
            listed: list[object] = providers
            admitted: list[tuple[int, str]] = []
            seen: set[str] = set()
            for index, entry in enumerate(listed):
                url = _url_of(entry)
                if url is None:
                    continue
                if url not in seen and not _entry_problems("", entry):
                    admitted.append((index, url))
                seen.add(url)
            found[key] = (len(listed), admitted)
    return found


def _loopback(url: str) -> bool:
    """Whether an admitted ``url``'s host is ``localhost`` or a loopback
    address; a name under ``.localhost`` is not (ADR 0014 Decision 3).
    """
    authority = url.partition("://")[2]
    if authority.startswith("["):
        host = authority[1 : authority.index("]")]
    else:
        host = authority.partition(":")[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def remote_warnings(neolith: object) -> list[str]:
    """``neolith.W002``: each remote provider entry at a plain ``http`` url
    whose host is not loopback, among the entries in the admitted form,
    named by its identity and its index (ADR 0031). An admitted url holds no
    credential, so it is shown.
    """
    return [
        f'{REMOTE}["{identity}"][{index}]: {url} is plain HTTP to a host that '
        "is not loopback; anyone who can watch the network can read and replay "
        "the calling app's token (spec 03)"
        for identity, (_, listed) in sorted(_listed(neolith).items())
        for index, url in listed
        if url.startswith("http://") and not _loopback(url)
    ]


def unsigned_problems(neolith: object, hosted: Sequence[Declaration]) -> list[str]:
    """Each hosted app that requires a contract ``NEOLITH["REMOTE_PROVIDERS"]``
    names and has no entry in ``NEOLITH["SIGNING_SECRETS"]``, with the
    message its call would get (ADR 0027 Decision 9). Nothing when
    ``SIGNING_SECRETS`` is not a ``dict``, which ``neolith.E001`` reports,
    since which apps it names cannot be read.
    """
    remote = remote_identities(neolith)
    if not remote:
        return []
    given: dict[object, object] = cast("dict[object, object]", neolith)
    signing = _naming.entry(given, _caller.SIGNING, {})
    if type(signing) is not dict:
        return []
    named: dict[object, object] = signing
    return sorted(
        _caller.no_signing_key(declaration.label, contract.identity)
        for declaration in hosted
        if not _naming.holds(named, declaration.label)
        for contract in declaration.requires
        if contract.identity in remote
    )


def remote_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.W002``: a remote provider at a plain ``http`` url that is
    not loopback; and ``neolith.E021``: a hosted app that requires a
    contract of a remote provider and has no signing key, so set-up stops
    rather than every such call failing with ``unauthenticated``. Each is
    judged on the entries it can read, whatever ``neolith.E001`` finds in
    the rest of ``NEOLITH``, so neither hides behind it.
    """
    if not _hosting():
        return []
    neolith = getattr(settings, "NEOLITH", MISSING)
    hosted = hosted_declarations(global_apps.get_app_configs())
    found: list[checks.CheckMessage] = [
        checks.Error(problem, id="neolith.E021")
        for problem in unsigned_problems(neolith, hosted)
    ]
    return found + [
        checks.Warning(problem, id="neolith.W002")
        for problem in remote_warnings(neolith)
    ]


def silenced_problems(value: object) -> list[str]:
    """Judge ``SILENCED_SYSTEM_CHECKS``: absent, or a ``list`` of ``str`` with
    no id of a host error, which stops the host and cannot be silenced (ADR
    0022 Decision 2). Pass ``MISSING`` for a setting that is absent.
    """
    if value is MISSING:
        return []
    if type(value) is not list:
        return [f"SILENCED_SYSTEM_CHECKS: expected a list, got {_type(value)}"]
    problems: list[str] = []
    entries: list[object] = value
    for index, entry in enumerate(entries):
        if type(entry) is not str:
            problems.append(
                f"SILENCED_SYSTEM_CHECKS: entry {index}: expected a str, got "
                f"{_type(entry)}"
            )
        elif entry.startswith("neolith.E"):
            problems.append(
                f"SILENCED_SYSTEM_CHECKS: {_shown_key(entry)} is a host error and "
                "cannot be silenced"
            )
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


def principal_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E019``: what set-up recorded when it built the principal
    backends, so a later run builds nothing again; and ``neolith.E020``:
    with a backend configured, Django's cache middleware listed after the
    principal middleware.
    """
    if not _hosting():
        return []
    config = global_apps.get_app_config(TAG)
    found: list[checks.CheckMessage] = [
        checks.Error(problem, id="neolith.E019")
        # safe read: neolith's own app config.
        for problem in vars(config).get(PRINCIPAL_REFUSALS, ())
    ]
    neolith = _checked_neolith()
    if neolith is not None and _principal.KEY in neolith:
        found += [
            checks.Error(problem, id="neolith.E020")
            for problem in _principal.cache_problems(
                getattr(settings, "MIDDLEWARE", None)
            )
        ]
    return found


def caller_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E021``: what set-up refused when it built the caller
    backends and the signing keys (ADR 0027); ``neolith.W004``: a host that
    provides contracts with no caller backend, so every call over HTTP fails
    with ``unauthenticated``; and ``neolith.W005``: a principal backend that
    declares a ``neolith-`` header.
    """
    if not _hosting():
        return []
    # safe read: neolith's own app config.
    findings = vars(global_apps.get_app_config(TAG)).get(_caller.FINDINGS)
    found: list[checks.CheckMessage] = []
    if type(findings) is _caller.Findings:
        found += [checks.Error(p, id="neolith.E021") for p in findings.refusals]
        found += [
            checks.Warning(p, id="neolith.W005") for p in findings.principal_headers
        ]
    neolith = _checked_neolith()
    hosted = hosted_declarations(global_apps.get_app_configs())
    if (
        neolith is not None
        and _caller.KEY not in neolith
        and any(d.provides for d in hosted)
    ):
        found.append(
            checks.Warning(
                f"this host provides contracts and {_caller.SETTING} lists no "
                "caller backend, so every call to them over HTTP fails with "
                "unauthenticated",
                id="neolith.W004",
            )
        )
    return found


def caller_deploy_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.W006``, under ``check --deploy``: the shared-secret backend
    is listed, which is for development (ADR 0027, spec 03).
    """
    if not _hosting():
        return []
    # safe read: neolith's own app config.
    findings = vars(global_apps.get_app_config(TAG)).get(_caller.FINDINGS)
    if type(findings) is not _caller.Findings or findings.shared is None:
        return []
    return [
        checks.Warning(
            f"{findings.shared}: the shared-secret backend is for development: "
            "every provider that accepts an app holds its secret and can sign as "
            "it, and a token seen on the network can be replayed until it expires",
            id="neolith.W006",
        )
    ]


def hosted_declarations(configs: Iterable[AppConfig]) -> list[Declaration]:
    """The declarations of the installed apps that have one, in order."""
    return [
        found
        for config in configs
        if (found := declaration_of(type(config))) is not None
    ]


def neolith_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E001``: the ``NEOLITH`` setting."""
    if not _hosting():
        return []
    hosted = hosted_declarations(global_apps.get_app_configs())
    return [
        checks.Error(problem, id="neolith.E001")
        for problem in neolith_problems(getattr(settings, "NEOLITH", _MISSING), hosted)
    ]


def silenced_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E002``: ``SILENCED_SYSTEM_CHECKS`` names no host error."""
    if not _hosting():
        return []
    return [
        checks.Error(problem, id="neolith.E002")
        for problem in silenced_problems(
            getattr(settings, "SILENCED_SYSTEM_CHECKS", MISSING)
        )
    ]


def _checked_neolith() -> dict[str, Any] | None:
    """``settings.NEOLITH`` when its form is one the checks below can read,
    else ``None``, and ``neolith.E001`` reports it.
    """
    value = getattr(settings, "NEOLITH", MISSING)
    if type(value) is not dict:
        return None
    hosted = hosted_declarations(global_apps.get_app_configs())
    if neolith_problems(value, hosted):
        return None
    checked: dict[str, Any] = value
    return checked


def _has_models(config: AppConfig) -> bool:
    return any(config.get_models(include_auto_created=True, include_swapped=True))


def installed_problems(
    configs: Iterable[AppConfig], neolith: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Judge the kinds of installed app against a checked ``NEOLITH``
    (ADR 0022, the set-up table): ``neolith.E003`` problems, an app with a
    declaration installed as an undeclared app or as a battery, and
    ``neolith.E004`` problems, an app with models and no declaration in a
    closed host, a host whose ``NEOLITH["DATABASES"]`` has no ``default``.
    """
    configs = list(configs)
    hosted = hosted_declarations(configs)
    named = batteries(hosted)
    undeclared = set(neolith.get("UNDECLARED_APPS", ()))
    closed = _databases.DEFAULT not in neolith.get("DATABASES", {})
    declared: list[str] = []
    stray: list[str] = []
    for config in configs:
        cls = type(config)
        if declaration_of(cls) is not None:
            entries = {config.name, _naming.full_name(cls, None)}
            if entries & undeclared:
                declared.append(
                    f"app {config.label}: has a declaration and is listed in "
                    f"{_UNDECLARED}; name it among the host's apps"
                )
            if config.name in named:
                declared.append(
                    f"app {config.label}: has a declaration and is named as a "
                    f"battery of {named[config.name]}; name it among the host's apps"
                )
        elif closed and config.label != TAG and _has_models(config):
            battery = (
                f"; it is a battery of {named[config.name]}"
                if config.name in named
                else ""
            )
            stray.append(
                f"app {config.label}: has models and no declaration, and this host "
                f"has no default database{battery}"
            )
    return sorted(declared), sorted(stray)


def installed_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E003`` and ``neolith.E004``: the kinds of installed app."""
    if not _hosting():
        return []
    neolith = _checked_neolith()
    if neolith is None:
        return []
    declared, stray = installed_problems(global_apps.get_app_configs(), neolith)
    found: list[checks.CheckMessage] = [
        checks.Error(p, id="neolith.E003") for p in declared
    ]
    return found + [checks.Error(p, id="neolith.E004") for p in stray]


def _database_caches(caches: object) -> list[str]:
    """The aliases of ``CACHES`` whose backend is Django's database cache or
    a subclass of it. A backend that does not import is Django's to report
    when the cache is first used, and is not one found here.
    """
    if type(caches) is not dict:
        return []
    from django.core.cache.backends.db import DatabaseCache

    found: list[str] = []
    entries: dict[object, object] = caches
    for alias, entry in entries.items():
        if type(alias) is not str or type(entry) is not dict:
            continue
        backend = entry.get("BACKEND")
        if type(backend) is not str:
            continue
        try:
            cls = import_string(backend)
        except Exception:  # noqa: S112  # Django reports it at the cache's first use
            continue
        if issubclass(type(cls), type) and issubclass(cls, DatabaseCache):
            found.append(alias)
    return found


def default_problems(
    configs: Iterable[AppConfig], neolith: dict[str, Any], caches: object
) -> tuple[list[str], list[str]]:
    """Judge ``default`` against what lives on it (ADR 0009, the admitted
    set): ``neolith.E015`` problems, a real ``default`` that no undeclared
    app with models and no database cache lives on, and ``neolith.E016``
    problems, a database cache in a closed host.
    """
    configs = list(configs)
    cached = _database_caches(caches)
    if _databases.DEFAULT in neolith.get("DATABASES", {}):
        used = cached or any(
            declaration_of(type(config)) is None and _has_models(config)
            for config in configs
        )
        unused = [] if used else ["database default: nothing in this host lives on it"]
        return unused, []
    return [], [
        f"cache {alias}: a database cache needs a default database, and this host "
        "has none"
        for alias in sorted(cached)
    ]


def default_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E015`` and ``neolith.E016``: what lives on ``default``."""
    if not _hosting():
        return []
    neolith = _checked_neolith()
    if neolith is None:
        return []
    unused, cached = default_problems(
        global_apps.get_app_configs(), neolith, getattr(settings, "CACHES", None)
    )
    found: list[checks.CheckMessage] = [
        checks.Error(p, id="neolith.E015") for p in unused
    ]
    return found + [checks.Error(p, id="neolith.E016") for p in cached]


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
    held = _naming.module_dict(sites) or {}
    for site in list(_naming.entry(held, "all_sites", ())):  # type: ignore[call-overload]
        registry = _naming.entry(_naming.instance_dict(site) or {}, "_registry")
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
                if _naming.entry(_naming.instance_dict(field) or {}, "db_table"):
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
        silenced_check,
        installed_check,
        middleware_check,
        principal_check,
        caller_check,
        remote_check,
        admin_check,
        database_check,
        default_check,
        table_name_check,
    ):
        checks.register(check, TAG)
    checks.register(caller_deploy_check, TAG, deploy=True)
