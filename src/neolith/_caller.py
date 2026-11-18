"""The calling app of a contract call over HTTP: the backends, the signing
keys and the search (ADR 0027).

The deployer lists caller backends in ``NEOLITH["CALLER_BACKENDS"]`` and
each hosted app's signing key in ``NEOLITH["SIGNING_SECRETS"]``. These are
plain functions of what they work on, as in ``neolith._principal``:

- ``shape_problems`` and ``signing_problems`` judge the two keys' shapes and
  import nothing; the builder and set-up (``neolith.E001``) run them;
- ``build`` imports and constructs each backend of a checked setting;
  ``set_up`` builds the backends and the signing keys once per set-up, and
  records every refusal (``neolith.E021``) and what the warnings
  ``neolith.W005`` and ``neolith.W006`` report;
- ``identify`` and ``aidentify`` ask the built backends, in order, about
  one call (Decision 1), and ``Callers.mint`` makes the token an app sends.
"""

import inspect
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, final

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.utils.module_loading import import_string

from neolith import _naming, _principal, _token
from neolith._contracts import _SEGMENT
from neolith._declaration import declaration_of
from neolith._frozen import once
from neolith.callers import CallerBackend, CredentialRefused, SharedSecretBackend

__all__ = [
    "FINDINGS",
    "KEY",
    "SIGNING",
    "Built",
    "Callers",
    "Failed",
    "Findings",
    "Refused",
    "aidentify",
    "build",
    "identify",
    "set_up",
    "shape_problems",
    "signing_problems",
]

KEY: Final = "CALLER_BACKENDS"
SETTING: Final = f'NEOLITH["{KEY}"]'
SIGNING: Final = "SIGNING_SECRETS"
_SIGNING: Final = f'NEOLITH["{SIGNING}"]'
_BASE: Final = "neolith.callers.CallerBackend"
_SHARED: Final = "neolith.callers.SharedSecretBackend"
_SIGNING_KEYS: Final = frozenset({"KEY_ID", "SECRET"})
# Where set-up records what it found, on the neolith app config that ran it.
FINDINGS: Final = "_neolith_caller_findings"
_APP_NAME: Final = re.compile(_SEGMENT)
_HEADER_PREFIX: Final = "neolith-"

# The messages of Decision 8, step 1.
NO_BACKEND: Final = "this host has no caller backend, so it accepts no calls over HTTP"
NO_AUTHORIZATION: Final = (
    "the call carries no Authorization header; if the caller sent one, a server "
    "or gateway in front of this host may have removed it"
)
UNRECOGNIZED: Final = "the call carries no credential this host recognizes"


def _type(value: object) -> str:
    return _naming.type_name(value)


def shape_problems(value: object) -> list[str]:
    """Judge the shape of ``NEOLITH["CALLER_BACKENDS"]``, importing nothing:
    0023's entry form, under this key.
    """
    return _principal.shape_problems(value, SETTING, _SHARED)


def signing_problems(value: object, labels: Sequence[str]) -> list[str]:
    """Judge ``NEOLITH["SIGNING_SECRETS"]``: a ``dict`` of one or more hosted
    apps' labels, each mapped to a ``dict`` with exactly ``KEY_ID``, a key id,
    and ``SECRET``, an admitted secret. No message shows a secret, or a label
    or key id outside its form.
    """
    if type(value) is not dict:
        return [
            f"{_SIGNING}: expected a dict of hosted apps' labels, got {_type(value)}"
        ]
    entries: dict[object, object] = value
    if not entries:
        return [f"{_SIGNING}: is empty; leave the key out when no app signs"]
    problems: list[str] = []
    for label, entry in entries.items():
        if type(label) is not str or not _APP_NAME.fullmatch(label):
            problems.append(
                f"{_SIGNING}: a key is not a hosted app's label; it is not shown"
            )
            continue
        where = f"{_SIGNING}[{label!r}]"
        if label not in labels:
            problems.append(f"{where}: {label} is not a hosted app")
        if type(entry) is not dict:
            problems.append(
                f"{where}: expected a dict with exactly the keys KEY_ID and SECRET, "
                f"got {_type(entry)}"
            )
            continue
        items: dict[object, object] = entry
        if len(items) != len(_SIGNING_KEYS) or any(
            type(k) is not str or k not in _SIGNING_KEYS for k in items
        ):
            problems.append(f"{where}: expected exactly the keys KEY_ID and SECRET")
            continue
        key_id = items["KEY_ID"]
        if type(key_id) is not str or not _token.KEY_ID.fullmatch(key_id):
            problems.append(
                f"{where}: KEY_ID is not 1 to 64 letters, digits, '-', '_' or '.'; "
                "it is not shown"
            )
        problem = _token.secret_problem(items["SECRET"])
        if problem is not None:
            problems.append(f"{where}: SECRET {problem}")
    return problems


@final
@once
@dataclass(frozen=True, slots=True)
class Built:
    """One caller backend that set-up built: its place, path and instance."""

    position: int
    path: str
    backend: CallerBackend

    @property
    def where(self) -> str:
        """How refusals and logs name the entry: its position and path."""
        return f"{SETTING}[{self.position}] ({self.path})"


@final
@once
@dataclass(frozen=True, slots=True)
class Findings:
    """What set-up found, for the checks to report without building again."""

    refusals: tuple[str, ...] = ()
    # Principal backends that declare a neolith- header (neolith.W005).
    principal_headers: tuple[str, ...] = ()
    # The shared-secret backend's entry, when it is listed (neolith.W006).
    shared: str | None = None


@final
@once
@dataclass(frozen=True, slots=True)
class Callers:
    """The caller backends and signing keys of one set-up."""

    backends: tuple[Built, ...] = ()
    # Each hosted app's one signing key, named by the app it signs for.
    signing: tuple[_token.Secret, ...] = ()

    def mint(self, app: str, identity: str) -> _token.Token | None:
        """A fresh token of ``app`` for a call to ``identity``, or ``None``
        when ``app`` has no signing key (Decision 9).

        Raises:
            neolith._token.Failure: With fixed text, if signing fails.
        """
        for secret in self.signing:
            if secret.app == app:
                return _token.mint(secret, identity, int(time.time()))
        return None


# A table with no caller backend and no signing key.
NO_CALLERS: Final = Callers()


def no_signing_key(app: str, identity: str) -> str:
    """The refusal of a call from an app with no signing key (Decision 9)."""
    return (
        f"app {app} has no signing key in {_SIGNING}, so it cannot call "
        f"{identity} over HTTP"
    )


def build(value: list[dict[str, Any]]) -> tuple[tuple[Built, ...], list[str]]:
    """Import and construct every backend of a checked setting, in order.

    Returns the backends built and every refusal (0023 Decision 10's rows,
    with ``CallerBackend`` as the base), each naming the entry's position
    and path. One entry's refusal stops no other entry.
    """
    built: list[Built] = []
    problems: list[str] = []
    for position, entry in enumerate(value):
        path: str = entry["BACKEND"]
        options: dict[str, object] = entry["OPTIONS"]
        where = f"{SETTING}[{position}] ({path})"
        try:
            found = import_string(path)
        except Exception as error:
            problems.append(f"{where}: does not import: {_principal._cause(error)}")
            continue
        if not issubclass(type(found), type):
            problems.append(
                f"{where}: is a {_type(found)}, not a class; a caller backend is a "
                f"subclass of {_BASE}"
            )
            continue
        if found is CallerBackend or not _naming.derives(found, CallerBackend):
            problems.append(
                f"{where}: is not a subclass of {_BASE}; a caller backend subclasses it"
            )
            continue
        own: list[str] = []
        if _principal._FLAGS.__get__(found) & inspect.TPFLAGS_IS_ABSTRACT:
            held = _naming.find(found, "__abstractmethods__", ())
            abstract = (
                [name for name in held if type(name) is str]
                if type(held) is frozenset
                else []
            )
            missing = ", ".join(sorted(abstract))
            own.append(f"{where}: is abstract; it does not define {missing}")
        if inspect.iscoroutinefunction(_principal._function(found, "authenticate")):
            own.append(
                f"{where}: authenticate is a coroutine function; it is a plain one"
            )
        if not inspect.iscoroutinefunction(
            _principal._function(found, "aauthenticate")
        ):
            own.append(f"{where}: aauthenticate is not a coroutine function")
        if own:
            problems.extend(own)
            continue
        try:
            backend = found(**options)
        except Exception as error:
            names = ", ".join(sorted(options)) or "none"
            cause = _principal._cause(
                error, bounded=not _naming.is_a(error, ImproperlyConfigured)
            )
            problems.append(
                f"{where}: the constructor raised, with the options {names}: {cause}"
            )
            continue
        built.append(Built(position, path, backend))
    return tuple(built), problems


def _shared(backends: Sequence[Built]) -> list[Built]:
    return [
        b for b in backends if _naming.derives(type(b.backend), SharedSecretBackend)
    ]


def _signing(value: object) -> dict[str, _token.Secret]:
    """The signing keys of a checked ``SIGNING_SECRETS``."""
    if type(value) is not dict:
        return {}
    return {
        label: _token.secret(entry["SECRET"], label, entry["KEY_ID"])
        for label, entry in value.items()
    }


def _accepted(backend: SharedSecretBackend) -> list[_token.Secret]:
    """The secrets a shared-secret backend holds, read from its own
    attributes only: a subclass that never ran the base constructor holds
    none.
    """
    table = _naming.entry(_naming.instance_dict(backend) or {}, "_keys")
    if type(table) is not dict:
        return []
    held: dict[object, object] = table
    return [
        secret
        for keys in held.values()
        if type(keys) is dict
        for secret in keys.values()
        if type(secret) is _token.Secret
    ]


def key_problems(
    signing: Mapping[str, _token.Secret], shared: SharedSecretBackend | None
) -> list[str]:
    """One secret, one key, across the host (Decision 3): a secret under at
    most one app and key id, and an app and key id with at most one secret.
    The same app, key id and secret on the signing and the accepting side
    is admitted.
    """
    accepted = [] if shared is None else _accepted(shared)
    problems: list[str] = []
    for mine in signing.values():
        for theirs in accepted:
            pair = (mine.app, mine.key_id) == (theirs.app, theirs.key_id)
            if pair and not mine.same(theirs):
                problems.append(
                    f"{_SIGNING}[{mine.app!r}]: app {mine.app} key "
                    f"{_token.shown_key_id(mine.key_id)} "
                    f"has another secret in {SETTING}; one app and key id name "
                    "one secret"
                )
            elif not pair and mine.same(theirs):
                problems.append(
                    f"{_SIGNING}[{mine.app!r}]: its secret is also under app "
                    f"{theirs.app} key {_token.shown_key_id(theirs.key_id)} in "
                    f"{SETTING}; give each "
                    "key its own secret"
                )
    signers = list(signing.values())
    problems.extend(
        f"{_SIGNING}: apps {a.app} and {b.app} sign with one secret; give each "
        "key its own secret"
        for index, a in enumerate(signers)
        for b in signers[index + 1 :]
        if a.same(b)
    )
    return problems


def set_up(config: AppConfig, principals: Sequence[_principal.Built]) -> Callers:
    """Build the caller backends and signing keys of a ``NEOLITH`` whose
    form ``neolith.E001`` admits, and record on ``config`` what set-up found:
    the refusals for ``neolith.E021``, the principal backends that declare a
    ``neolith-`` header for ``neolith.W005``, and the shared-secret backend's
    entry for ``neolith.W006``.
    """
    refusals: list[str] = []
    backends: tuple[Built, ...] = ()
    signing: dict[str, _token.Secret] = {}
    neolith = getattr(settings, "NEOLITH", None)
    if type(neolith) is dict:
        value: Any = _naming.entry(neolith, KEY)
        if value is not None and not shape_problems(value):
            backends, refusals = build(value)
        registry = config.apps
        labels = [
            c.label
            for c in (registry.get_app_configs() if registry is not None else ())
            if declaration_of(type(c)) is not None
        ]
        given: Any = _naming.entry(neolith, SIGNING)
        if given is not None and not signing_problems(given, labels):
            signing = _signing(given)
    shared = _shared(backends)
    refusals.extend(
        f"{b.where}: the shared-secret backend is listed more than once; list it once"
        for b in shared[1:]
    )
    held: Any = shared[0].backend if shared else None
    refusals.extend(key_problems(signing, held))
    headers = tuple(
        f"{b.where}: request_inputs names {name!r}; a principal backend never "
        "reads a neolith- header, which the host reads only from a calling app "
        "the contract's allow-list admits"
        for b in principals
        for name in b.inputs
        if name.startswith(_HEADER_PREFIX)
    )
    setattr(
        config,
        FINDINGS,
        Findings(tuple(refusals), headers, shared[0].where if shared else None),
    )
    return Callers(backends, tuple(signing.values()))


class Refused(Exception):
    """The call fails with ``unauthenticated`` and this message."""


class Failed(Exception):
    """A caller backend failed: the call fails with ``internal``, and the
    host logs this message at ``ERROR``.
    """


def _named(built: Built, method: str, found: object) -> str | None:
    if found is None:
        return None
    if type(found) is str and _APP_NAME.fullmatch(found):
        return found
    if inspect.iscoroutine(found):
        found.close()  # never awaited; closed so it does not warn
    raise Failed(
        f"{built.where}: {method} returned a {_type(found)}, not an app's name or None"
    )


def _refused(error: CredentialRefused) -> Refused:
    try:
        message = error.message
    except Exception:
        message = UNRECOGNIZED
    return Refused(message)


def _nobody(request: HttpRequest) -> Refused:
    present = "HTTP_AUTHORIZATION" in request.META
    return Refused(UNRECOGNIZED if present else NO_AUTHORIZATION)


def identify(backends: Sequence[Built], request: HttpRequest, contract: str) -> str:
    """The calling app of a call, from each backend's ``authenticate`` in
    order: the first that does not abstain decides (Decision 1).

    Raises:
        Refused: No backend, every backend abstaining, or one refusing.
        Failed: A backend raised anything else, or returned anything but
            ``None`` or an app's name.
    """
    if not backends:
        raise Refused(NO_BACKEND)
    for built in backends:
        try:
            found = built.backend.authenticate(request, contract)
        except CredentialRefused as error:
            raise _refused(error) from None
        except Exception as error:
            raise Failed(
                f"{built.where}: authenticate raised {_principal._cause(error)}"
            ) from error
        app = _named(built, "authenticate", found)
        if app is not None:
            return app
    raise _nobody(request)


async def aidentify(
    backends: Sequence[Built], request: HttpRequest, contract: str
) -> str:
    """``identify`` with each backend's ``aauthenticate``, awaited."""
    if not backends:
        raise Refused(NO_BACKEND)
    for built in backends:
        try:
            found = await built.backend.aauthenticate(request, contract)
        except CredentialRefused as error:
            raise _refused(error) from None
        except Exception as error:
            raise Failed(
                f"{built.where}: aauthenticate raised {_principal._cause(error)}"
            ) from error
        app = _named(built, "aauthenticate", found)
        if app is not None:
            return app
    raise _nobody(request)
