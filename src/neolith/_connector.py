"""Connectors and the in-process transport (spec 02).

A connector mirrors the actions of one contract version. It runs the caller's
request check (ADR 0017 Decision 9, step 2) and hands the request's JSON text
to a transport, which returns the reply's JSON text or raises a
``ContractError``. With the ``single`` routing policy a connector has one
provider and sends each call to it once: it never retries and never falls back.

The in-process transport runs the provider's action in the caller's thread, in
a copy of the caller's context, so that what it sets in a context variable
stays on its side. What the process shares, such as thread-local state, the
warnings filters, logging and the exception the caller is handling, the action
shares too, as a direct call would; the call context's own scope arrives with
#10 task 6. It
reads the request text with the declared request type, so the action gets a new
value, and writes the reply with the declared reply type; the connector reads
that text back, so caller and provider never share an object (ADR 0007).

Until the provider's host delivers errors (#10 task 5), every failure on the
provider's side becomes ``internal``, with fixed text and nothing of the
original, which the provider side logs: an ``Exception`` the action raises,
a ``ContractError`` included, and a reply that cannot be written or read. An
exception that is not an ``Exception`` propagates unchanged.
"""

import contextlib
import contextvars
import inspect
import logging
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any, NoReturn, Protocol, final

from pydantic import BaseModel

from neolith._contracts import Action, Contract, ContractError
from neolith._crossing import check_request, codec

__all__ = ["Connector", "InProcess", "Transport"]

_log = logging.getLogger("neolith")

# The message of an internal error says nothing of the failure behind it.
_INTERNAL = "the provider failed, or its reply could not be read"


class Transport(Protocol):
    """Carries one call of a connector to its provider.

    ``call`` takes an action of ``contract`` and the request's JSON text,
    which passed the caller's request check, and returns the reply's JSON
    text, or ``None`` for an action whose reply is empty. Every outcome of the
    call that is not a reply is a ``ContractError``; an action of another
    contract is a ``TypeError``, a mistake in the code that called it.
    """

    @property
    def contract(self) -> Contract:
        """The contract version whose calls this transport carries."""
        ...

    def call(self, action: Action, request: bytes) -> bytes | None:
        """Carry the call and return the reply's text."""
        ...


class _Failed:
    """The outcome of a call that failed on the provider's side."""


_FAILED = _Failed()


def _report(message: str, error: BaseException) -> None:
    """Log a failure, never raising: a failing log must not carry it on."""
    with contextlib.suppress(Exception):
        _log.error(message, exc_info=error)


@final
class InProcess:
    """The in-process transport to one implementation of a contract."""

    __slots__ = ("_contract", "_functions", "_implementation")

    def __init__(self, declaration: type[Any], implementation: object) -> None:
        """Check the implementation's class against the contract, as
        ``@implements`` does, and keep the function of each action."""
        contract = Contract.of(declaration)
        cls = type(implementation)
        contract._check_implementation(cls)
        # The class's own functions, read statically: an attribute set on the
        # instance, or a __getattr__, never stands in for an action.
        self._functions: Mapping[str, Callable[..., object]] = types.MappingProxyType(
            {
                name: typing.cast(
                    Callable[..., object], inspect.getattr_static(cls, name)
                )
                for name in contract.actions
            }
        )
        self._contract = contract
        self._implementation = implementation

    @property
    def contract(self) -> Contract:
        """The contract version the implementation provides."""
        return self._contract

    def call(self, action: Action, request: bytes) -> bytes | None:
        """Run the action in the caller's thread and return the reply's text.

        An action of another contract is a ``TypeError``: a connector never
        passes one, since it is built for this transport's contract.
        """
        if self._contract.actions.get(action.name) is not action:
            raise TypeError(
                f"{self._contract.identity} has no action {action.name!r} of "
                "this declaration"
            )
        outcome = self._serve(action, request)
        if isinstance(outcome, _Failed):
            raise ContractError("internal", _INTERNAL)
        return outcome

    def _serve(self, action: Action, request: bytes) -> bytes | _Failed | None:
        """Read the request, run the action and write its reply.

        Never raises an ``Exception``: a failure is logged and returned as
        ``_FAILED``, so that once this returns, no frame holds the provider's
        values.
        """
        try:
            value = codec(self._contract, action.request).read(request)
            # In a copy of the caller's context, so that what the action sets
            # in a context variable stays on the provider's side.
            reply = contextvars.copy_context().run(
                self._functions[action.name], self._implementation, value
            )
            if action.reply is None:
                if reply is not None:
                    raise TypeError(
                        f"the reply is {type(reply).__qualname__}, but the "
                        "action's reply is empty"
                    )
                return None
            return codec(self._contract, action.reply).write(reply, "the reply")
        except Exception as error:
            failure: BaseException = error
        # Logged outside the handler, so a failing log does not chain to it,
        # then dropped, so its traceback does not keep the provider's frames.
        _report(
            f"{self._contract.identity}, action {action.name!r}: the provider failed",
            failure,
        )
        del failure
        return _FAILED


@final
class Connector:
    """Calls the actions of one contract version through one transport.

    Each action of the contract is an attribute taking one request, by
    position or by the name the declaration gives it, and returning the
    reply, or ``None`` for an empty reply. Anything else is a mistake in the
    calling code, so it raises ``AttributeError``, ``TypeError`` or
    ``ValueError`` (a request that fails the request check), never a
    ``ContractError``. A ``ContractError`` the caller gets is a new one,
    raised from the connector, so nothing else of the transport's crosses.
    """

    __slots__ = ("_contract", "_parameters", "_transport")

    def __init__(self, declaration: type[Any], transport: Transport) -> None:
        contract = Contract.of(declaration)
        if transport.contract is not contract:
            raise TypeError(
                f"the transport carries calls of {transport.contract.identity}, "
                f"not of {contract.identity}"
            )
        self._contract = contract
        self._transport = transport
        # The name each action's declaration gives its request, which a call
        # may pass it by, as it could to the declared method.
        self._parameters = types.MappingProxyType(
            {
                name: list(
                    inspect.signature(
                        typing.cast(
                            Callable[..., object],
                            inspect.getattr_static(declaration, name),
                        )
                    ).parameters
                )[1]
                for name in contract.actions
            }
        )

    def __getattr__(self, name: str) -> Callable[..., BaseModel | None]:
        # Only reached for a name the instance does not have; a private name
        # is never an action, which also covers a copy probing for hooks.
        actions = self._actions() if not name.startswith("_") else {}
        found = actions.get(name)
        if found is None:
            raise AttributeError(
                f"{self._describe()} has no action {name!r}"
                + (f"; its actions are {', '.join(actions)}" if actions else "")
            )
        return self._bound(found)

    def __dir__(self) -> list[str]:
        return sorted({*object.__dir__(self), *self._actions()})

    def __repr__(self) -> str:
        return f"<{self._describe()}>"

    def __copy__(self) -> "Connector":
        return self

    def __deepcopy__(self, memo: object) -> "Connector":
        # A connector holds no state of its own, so a copy is the same one.
        return self

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError(
            f"{self._describe()} cannot be pickled: it is bound to its provider "
            "in this process"
        )

    def _actions(self) -> Mapping[str, Action]:
        try:
            contract = object.__getattribute__(self, "_contract")
        except AttributeError:
            return {}
        return typing.cast(Mapping[str, Action], contract.actions)

    def _describe(self) -> str:
        try:
            contract = object.__getattribute__(self, "_contract")
        except AttributeError:
            return "connector"
        return f"connector to {contract.identity}"

    def _bound(self, action: Action) -> Callable[..., BaseModel | None]:
        parameter = self._parameters[action.name]

        def call(*args: object, **kwargs: object) -> BaseModel | None:
            if len(args) + len(kwargs) != 1 or (kwargs and parameter not in kwargs):
                raise TypeError(
                    f"{self._contract.identity}, action {action.name!r} takes one "
                    f"request, by position or as {parameter}=; got {len(args)} "
                    f"positional and {sorted(kwargs) or 'no'} keyword"
                )
            return self._call(action, args[0] if args else kwargs[parameter])

        call.__name__ = call.__qualname__ = action.name
        return call

    def _call(self, action: Action, request: object) -> BaseModel | None:
        text = check_request(self._contract, action, request)
        kind, message, value = self._carry(action, text)
        if kind is not None:
            raise ContractError(kind, message)
        return value

    def _carry(
        self, action: Action, text: bytes
    ) -> tuple[str | None, str, BaseModel | None]:
        """Send the call and return its error's kind and message, or its reply.

        Never raises an ``Exception``, so the error the caller gets is built
        here, from the kind and message alone.
        """
        what = "the transport failed"
        try:
            reply = self._transport.call(action, text)
        except ContractError as error:
            if error.data is None:
                return error.kind, error.message, None
            # A detail crosses as text with the host's error delivery (#10
            # task 5); until then, an error that carries one fails closed.
            failure: BaseException = error
            what = "an error with a detail arrived"
        except Exception as error:
            failure = error
        else:
            try:
                return None, "", self._read(action, reply)
            except Exception as error:
                failure = error
                what = "the reply could not be read"
        _report(f"{self._contract.identity}, action {action.name!r}: {what}", failure)
        del failure
        return "internal", _INTERNAL, None

    def _read(self, action: Action, reply: object) -> BaseModel | None:
        if action.reply is None:
            if reply is not None:
                raise TypeError("a reply arrived for an action whose reply is empty")
            return None
        if type(reply) is not bytes:
            raise TypeError(f"the reply is {type(reply).__qualname__}, not text")
        return codec(self._contract, action.reply).read(reply)
