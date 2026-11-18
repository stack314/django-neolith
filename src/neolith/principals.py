"""Principal backends: on whose behalf a Django request acts (ADR 0023).

A deployer lists backends in ``NEOLITH["PRINCIPAL_BACKENDS"]``, in order::

    NEOLITH = {
        "PRINCIPAL_BACKENDS": [
            {
                "BACKEND": "neolith.principals.DjangoUserBackend",
                "OPTIONS": {"issuer": "https://accounts.example.com"},
            },
        ],
    }

The host builds each backend once, when it is set up, and asks them about a
request when the request's first contract call needs its principal: the
first backend to return a principal decides, ``None`` abstains, and one that
raises makes every call from that request fail with ``internal``. A
response whose request's principal was looked for varies on the request
headers the backends declare in ``request_inputs``. The host trusts that
declaration: a backend that reads a header it does not declare gives
responses a shared cache may serve to another user.
"""

import abc
import uuid
from typing import Any, Final

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from neolith import _naming
from neolith._context import Principal

__all__ = ["DjangoUserBackend", "PrincipalBackend"]

_ISSUER_LIMIT: Final = 255
_ADMITTED_KEYS: Final = "AutoField, BigAutoField, SmallAutoField or UUIDField"
_SESSION: Final = "django.contrib.sessions.middleware.SessionMiddleware"
_AUTHENTICATION: Final = "django.contrib.auth.middleware.AuthenticationMiddleware"


class PrincipalBackend(abc.ABC):
    """The base class of a principal backend (ADR 0023 Decision 2).

    A backend is built once per set-up of the host, with its ``OPTIONS`` as
    keyword arguments, and serves every request from every thread, so it
    keeps no state of one request on itself. A subclass defines
    ``resolve``, sets ``request_inputs``, and overrides ``aresolve`` when it
    has an async path of its own.

    Attributes:
        request_inputs: The request headers the backend reads, as a tuple of
            lower-case names such as ``("authorization",)``; ``()`` when its
            answer depends on the URL alone or on nothing of the request;
            ``("*",)`` when it reads something outside the URL and the
            headers, such as the client's address or the body. It has no
            default.
    """

    request_inputs: tuple[str, ...]

    @abc.abstractmethod
    def resolve(self, request: HttpRequest) -> Principal | None:
        """The principal of ``request``, or ``None`` to abstain.

        Called when the request's first call is synchronous, or when Django
        runs the principal middleware in sync mode. Raise to refuse: every
        call from the request then fails with ``internal``, and the host
        logs the cause. It may be called more than once for one request and
        gives the same answer each time.

        Args:
            request: The request.

        Returns:
            The principal, or ``None`` when the backend does not recognise
            the request.
        """

    async def aresolve(self, request: HttpRequest) -> Principal | None:
        """``resolve`` for an asynchronous caller.

        It runs ``resolve`` through ``sync_to_async``, as Django's
        ``BaseBackend.aauthenticate`` does; a backend with an async path of
        its own overrides it.

        Args:
            request: The request.

        Returns:
            The principal, or ``None`` when the backend does not recognise
            the request.
        """
        return await sync_to_async(self.resolve)(request)


class DjangoUserBackend(PrincipalBackend):
    """The user that ``AuthenticationMiddleware`` provides, as a principal.

    The principal's issuer is the ``issuer`` option, which names the user
    table: hosts that read one user table share an issuer, and hosts with
    different user tables have different ones. Its subject is the user's
    primary key as text, and it has no claims. A user who is not logged in
    gives no principal. A user that ``login``, ``logout``, ``alogin`` or
    ``alogout`` has put on the request is used as it is, so a view that logs
    out and then calls does not call as the user who left.

    The user model's primary key is an ``AutoField``, ``BigAutoField``,
    ``SmallAutoField`` or ``UUIDField``, or a subclass of one, whose values
    the database never gives twice; the deployer never gives a key to a
    second person under one issuer. ``SessionMiddleware`` and
    ``AuthenticationMiddleware`` come before the principal middleware.

    It declares ``("authorization", "cookie")``: a session is found by its
    cookie, and Django REST framework puts the user it authenticated from
    ``Authorization`` on the request. A project that sets ``request.user``
    from another input, such as a custom header or ``RemoteUserMiddleware``,
    subclasses this backend and sets ``request_inputs`` to match.

    Args:
        **options: ``issuer``, required: 1 to 255 visible ASCII characters,
            such as a URL the project controls. No other option is taken.

    Raises:
        ImproperlyConfigured: With every problem found: the options, the
            user model's key, ``django.contrib.auth`` not installed, or the
            middleware order.
    """

    request_inputs = ("authorization", "cookie")

    def __init__(self, **options: object) -> None:
        """Check the options and the project, and keep the issuer."""
        from django.apps import apps

        problems: list[str] = []
        issuer = options.pop("issuer", None)
        problems.extend(
            f"the option {name!r} is not taken (the one option is 'issuer')"
            for name in sorted(options)
        )
        if not _is_issuer(issuer):
            problems.append(
                "the option 'issuer' is required: 1 to 255 visible ASCII "
                "characters, no space, such as a URL the project controls "
                "(hosts that share a user table share the issuer)"
            )
        self._model: Any = None
        if not apps.is_installed("django.contrib.auth"):
            problems.append(
                "django.contrib.auth is not installed; add it to "
                'NEOLITH["UNDECLARED_APPS"]'
            )
        else:
            from django.contrib.auth import get_user_model

            self._model = get_user_model()
            problem = key_problem(self._model)
            if problem is not None:
                problems.append(problem)
        problems.extend(order_problems(getattr(settings, "MIDDLEWARE", None)))
        if problems:
            raise ImproperlyConfigured("; ".join(problems))
        self._issuer: str = issuer  # type: ignore[assignment]  # checked

    def resolve(self, request: HttpRequest) -> Principal | None:
        """The principal of the user on ``request``, or ``None``.

        Args:
            request: The request.

        Returns:
            The user's principal, or ``None`` when no user is logged in.

        Raises:
            LookupError: The request has no ``user``.
            TypeError: The user is not an instance of the user model, or
                its key is not exactly an ``int`` or a ``UUID``.
        """
        return self._principal(_user(request))

    async def aresolve(self, request: HttpRequest) -> Principal | None:
        """The principal of the user on ``request``, read with ``auser()``
        unless ``request.user`` is a plain instance already.

        Args:
            request: The request.

        Returns:
            The user's principal, or ``None`` when no user is logged in.

        Raises:
            LookupError: The request has no ``user`` or no ``auser``.
            TypeError: The user is not an instance of the user model, or
                its key is not exactly an ``int`` or a ``UUID``.
        """
        user = _user(request)
        # Judged by type(), which a lazy object does not proxy.
        if issubclass(type(user), SimpleLazyObject):
            auser = getattr(request, "auser", None)
            if auser is None:
                raise LookupError(
                    "the request has no auser: AuthenticationMiddleware did not "
                    "run for this request"
                )
            user = await auser()
        return self._principal(user)

    def _principal(self, user: Any) -> Principal | None:  # noqa: ANN401
        if user.is_authenticated is not True:
            return None
        model = self._model
        if not isinstance(user, model):
            raise TypeError(
                f"the authenticated user is a {_naming.type_name(user)}, not an "
                f"instance of the user model {model._meta.label}"
            )
        key = user.pk
        if type(key) is int or type(key) is uuid.UUID:
            return Principal(self._issuer, str(key))
        raise TypeError(
            f"the user's key is a {_naming.type_name(key)}, not exactly an int "
            "or a UUID"
        )


def _user(request: HttpRequest) -> Any:  # noqa: ANN401
    """``request.user``, not evaluated."""
    try:
        return request.user
    except AttributeError:
        raise LookupError(
            "the request has no user: AuthenticationMiddleware did not run for "
            "this request"
        ) from None


def _is_issuer(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= _ISSUER_LIMIT
        and all(0x21 <= ord(c) <= 0x7E for c in value)
    )


def key_problem(model: type) -> str | None:
    """Why ``model``'s primary key cannot give a subject, or ``None``.

    Admitted: ``AutoField``, ``BigAutoField``, ``SmallAutoField`` and
    ``UUIDField``, or a subclass of one (ADR 0023 Decision 9).
    """
    from django.db import models

    field = model._meta.pk  # type: ignore[attr-defined]
    admitted = (models.AutoField, models.BigAutoField, models.SmallAutoField)
    kind = type(field)
    if issubclass(kind, admitted) or issubclass(kind, models.UUIDField):
        return None
    inherited = (
        " (the key of a model that inherits from another model)"
        if issubclass(kind, models.OneToOneField)
        else ""
    )
    return (
        f"the user model {model._meta.label}'s key is a {_naming.qualname(kind)}"  # type: ignore[attr-defined]
        f"{inherited}; the subject is the key, which is an {_ADMITTED_KEYS}, "
        "or a subclass of one"
    )


def order_problems(middleware: object) -> list[str]:
    """``SessionMiddleware`` and ``AuthenticationMiddleware``, each as the
    class or a subclass, are listed before the principal middleware.

    ``AuthenticationMiddleware`` is imported only when
    ``django.contrib.auth`` is installed: its module imports auth's models,
    which Django refuses otherwise, and that refusal would hide the
    constructor's own (ADR 0023 Decision 10). Without the app only its own
    path is found, since no subclass of it can be imported either.
    ``SessionMiddleware``'s module imports no model.
    """
    from django.apps import apps
    from django.contrib.sessions.middleware import SessionMiddleware

    from neolith._principal import PRINCIPAL_MIDDLEWARE, first_index
    from neolith.middleware import PrincipalMiddleware

    if type(middleware) is not list:
        return []
    entries: list[object] = middleware
    principal = first_index(entries, PrincipalMiddleware)
    if principal is None:
        return []
    before = entries[:principal]
    if apps.is_installed("django.contrib.auth"):
        from django.contrib.auth.middleware import AuthenticationMiddleware

        authentication = first_index(before, AuthenticationMiddleware) is not None
    else:
        authentication = any(type(e) is str and e == _AUTHENTICATION for e in before)
    listed = (
        (_SESSION, first_index(before, SessionMiddleware) is not None),
        (_AUTHENTICATION, authentication),
    )
    return [
        f"MIDDLEWARE: {path}, or a subclass, is not listed before "
        f"{PRINCIPAL_MIDDLEWARE}; list it before, since the Django-user backend "
        "reads the user it provides"
        for path, found in listed
        if not found
    ]
