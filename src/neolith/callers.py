"""Caller backends: which app makes a contract call over HTTP (ADR 0027).

A deployer lists backends in ``NEOLITH["CALLER_BACKENDS"]``, in order, in
the entry form of ``NEOLITH["PRINCIPAL_BACKENDS"]``::

    NEOLITH = {
        "CALLER_BACKENDS": [
            {
                "BACKEND": "neolith.callers.SharedSecretBackend",
                "OPTIONS": {"secrets": {"orders": {"orders-2": ORDERS_KEY_2}}},
            },
        ],
        "SIGNING_SECRETS": {"orders": {"KEY_ID": "orders-2", "SECRET": ORDERS_KEY_2}},
    }

The host builds each backend once, when it is set up, and asks them in
order about each contract call that arrives over HTTP: the first that does
not abstain decides. Everything after the calling app is named belongs to
the host: the allow-list, the principal header, the context headers and the
body. ``PRINCIPAL_BACKENDS`` is the other kind of backend: it names on whose
behalf a Django request acts, not which app calls.

A backend's author keeps credentials out of every output: a refusal's
message is fixed text, and nothing the backend logs or raises holds a
credential, since the message of ``CredentialRefused`` reaches the caller
as it is.
"""

import abc
import re
import time
from collections.abc import Mapping
from typing import Final

from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

from neolith import _naming, _token
from neolith._contracts import _SEGMENT

__all__ = ["CallerBackend", "CredentialRefused", "SharedSecretBackend"]

_APP_NAME: Final = re.compile(_SEGMENT)
_APP_FORM: Final = "a lower-case letter, then lower-case letters, digits or '_'"
_KEY_ID: Final = "1 to 64 letters, digits, '-', '_' or '.'"


class CredentialRefused(Exception):  # the name ADR 0027 fixes
    """A backend recognized the credentials and refuses them.

    On a contract call over HTTP the call fails with ``unauthenticated`` and
    this message, which the host logs at ``WARNING``. Raised by a principal
    backend, every call from the request fails with ``internal``, and the
    host logs it at ``WARNING`` rather than ``ERROR``.

    Args:
        message: Fixed text that shows no part of a credential; it reaches
            the caller as it is.

    Raises:
        TypeError: If ``message`` is not a ``str``.
    """

    def __init__(self, message: str) -> None:
        """Keep ``message``, which is a ``str``."""
        if type(message) is not str:
            raise TypeError("a refusal's message is a str of fixed text")
        super().__init__(message)

    @property
    def message(self) -> str:
        """The refusal's text."""
        text: str = self.args[0]
        return text


class CallerBackend(abc.ABC):
    """The base class of a caller backend (ADR 0027 Decision 1).

    A backend is built once per set-up of the host, with its ``OPTIONS`` as
    keyword arguments, and serves every call from every thread, so it keeps
    no state of one call on itself. A subclass defines ``authenticate``, and
    overrides ``aauthenticate`` to run inline when it does no I/O, or with a
    native async version when it does. It never reads the request body: the
    host authenticates before it reads anything else of the call.
    """

    @abc.abstractmethod
    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        """The name of the app making the call, or ``None`` to abstain.

        Raise ``CredentialRefused`` to refuse credentials the backend
        recognizes; no later backend is then asked. Any other exception, or
        a return value that is not ``None`` or an app's name, fails the call
        with ``internal``.

        Args:
            request: The request.
            contract: The identity in the request path, such as
                ``"inventory.v1"``.

        Returns:
            The calling app's name, or ``None`` when the backend does not
            recognize the credentials.
        """

    async def aauthenticate(self, request: HttpRequest, contract: str) -> str | None:
        """``authenticate`` for an asynchronous host.

        It runs ``authenticate`` through ``sync_to_async``, on the request's
        own thread, which costs one thread hop per call.

        Args:
            request: The request.
            contract: The identity in the request path.

        Returns:
            The calling app's name, or ``None`` to abstain.
        """
        return await sync_to_async(self.authenticate)(request, contract)


class SharedSecretBackend(CallerBackend):
    """Spec 03's shared-secret token, for development (ADR 0027 Decision 2).

    It recognizes ``Authorization: Bearer`` with a token whose first part is
    a JSON object with the ``typ`` ``neolith+jwt``, and checks it with the
    standard library's HMAC SHA-256 against the keys of its ``secrets``
    option, in the order of ADR 0027 Decision 2, each failure with its own
    message. Every provider that accepts an app holds its secret and could
    mint tokens as that app, and a token seen on the network can be replayed
    until it expires; ``check --deploy`` warns that it is listed.

    Args:
        **options: ``secrets``, required: each calling app this host accepts
            calls from, by name, with one key, or two while it is rotated,
            each a key id mapped to a secret of 43 base64url characters (what
            ``secrets.token_urlsafe(32)`` gives). No other option is taken.

    Raises:
        ImproperlyConfigured: With every problem found; no message shows a
            secret.
    """

    def __init__(self, **options: object) -> None:
        """Check the options and keep each secret as a private object."""
        problems: list[str] = []
        given = options.pop("secrets", _MISSING)
        problems.extend(
            f"the option {name!r} is not taken (the one option is 'secrets')"
            for name in sorted(options)
        )
        keys, found = _accepted(given)
        problems.extend(found)
        if problems:
            raise ImproperlyConfigured("; ".join(problems))
        self._keys: Mapping[str, Mapping[str, _token.Secret]] = keys

    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        """The app named by a valid token, or ``None`` to abstain.

        Raises:
            CredentialRefused: For a recognized token that is not valid,
                with the message of the first rule it breaks.
            neolith._token.Failure: With fixed text, if the check itself
                fails.
        """
        failed = False
        try:
            app, refusal = _check(request, contract, self._keys)
        except Exception:
            failed = True
        if failed:
            raise _token.Failure("the shared-secret token could not be checked")
        if refusal is not None:
            raise CredentialRefused(refusal)
        return app

    async def aauthenticate(self, request: HttpRequest, contract: str) -> str | None:
        """``authenticate``, inline: the check is a few microseconds of
        computation, shorter than a thread hop.
        """
        return self.authenticate(request, contract)


_MISSING: Final = object()


def _check(
    request: HttpRequest,
    contract: str,
    keys: Mapping[str, Mapping[str, _token.Secret]],
) -> tuple[str | None, str | None]:
    """Read ``Authorization`` and check it; the token text stays in this
    frame and below it.
    """
    return _token.check(
        request.META.get("HTTP_AUTHORIZATION"), contract, keys, int(time.time())
    )


def _accepted(
    value: object,
) -> tuple[dict[str, dict[str, _token.Secret]], list[str]]:
    """The ``secrets`` option as a key table, and every problem found.

    The admitted form: a ``dict`` of one or more app names, each mapped to a
    ``dict`` of one or two key ids, each mapped to an admitted secret, with
    no secret under two pairs. Messages name app names and key ids that have
    their form, and never a secret.
    """
    where = "the option 'secrets'"
    if value is _MISSING:
        return {}, [
            f"{where} is required: each calling app this host accepts, mapped to "
            "its key ids and secrets"
        ]
    if type(value) is not dict:
        return {}, [f"{where}: expected a dict of app names, got a {_kind(value)}"]
    apps: dict[object, object] = value
    if not apps:
        return {}, [f"{where}: is empty; name each calling app this host accepts"]
    problems: list[str] = []
    table: dict[str, dict[str, _token.Secret]] = {}
    held: list[_token.Secret] = []
    for app, given in apps.items():
        if type(app) is not str or not _APP_NAME.fullmatch(app):
            problems.append(
                f"{where}: a key is not an app's name ({_APP_FORM}); it is not shown"
            )
            continue
        at = f"{where}: app {app}"
        if type(given) is not dict:
            problems.append(f"{at}: expected a dict of key ids, got a {_kind(given)}")
            continue
        entries: dict[object, object] = given
        if not entries:
            problems.append(f"{at}: has no key; give it one, or two while it rotates")
            continue
        if len(entries) > 2:
            problems.append(
                f"{at}: has {len(entries)} keys; it has one, or two while it rotates"
            )
            continue
        keys: dict[str, _token.Secret] = {}
        for key_id, text in entries.items():
            if type(key_id) is not str or not _token.KEY_ID.fullmatch(key_id):
                problems.append(f"{at}: a key id is not {_KEY_ID}; it is not shown")
                continue
            shown = _token.shown_key_id(key_id)
            problem = _token.secret_problem(text)
            if problem is not None:
                problems.append(f"{at}: key {shown}: the secret {problem}")
                continue
            secret = _token.secret(text, app, key_id)  # type: ignore[arg-type]
            problems.extend(
                f"{where}: one secret is under app {other.app} key "
                f"{_token.shown_key_id(other.key_id)} and app {app} key {shown}; "
                "give each key its own secret"
                for other in held
                if other.same(secret)
            )
            held.append(secret)
            keys[key_id] = secret
        table[app] = keys
    return table, problems


def _kind(value: object) -> str:
    return _naming.type_name(value)
