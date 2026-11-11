"""Connectors and the in-process transport (spec 02).

A connector mirrors the actions of one contract version. It runs the caller's
request check (ADR 0017 Decision 9, step 2) and hands the request's JSON text
to a transport, which returns the reply's JSON text or raises a
``ContractError``. With the ``single`` routing policy a connector has one
provider and sends each call to it once: it never retries and never falls back.

The in-process transport runs the provider's action in the caller's thread. It
reads the request text with the declared request type, so the action gets a new
value, and writes the reply with the declared reply type; the connector reads
that text back, so caller and provider never share an object (ADR 0007).

Until the provider's host delivers errors (#10 task 5), every failure on the
provider's side becomes ``internal``, with fixed text and nothing of the
original, which the provider side logs: an ``Exception`` the action raises,
a ``ContractError`` included, and a reply that cannot be written or read. An
exception that is not an ``Exception`` propagates unchanged.
"""

import inspect
import logging
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any, Protocol, final

from pydantic import BaseModel

from neolith._contracts import Action, Contract, ContractError
from neolith._crossing import check_request, codec

__all__ = ["Connector", "InProcess", "Transport"]

_log = logging.getLogger("neolith")

# The message of an internal error says nothing of the failure behind it.
_INTERNAL = "the provider failed, or its reply could not be read"


class Transport(Protocol):
    """Carries one call of a connector to its provider.

    ``call`` takes the action and the request's JSON text, which passed the
    caller's request check, and returns the reply's JSON text, or ``None`` for
    an action whose reply is empty. Every outcome that is not a reply is a
    ``ContractError``.
    """

    def call(self, action: Action, request: bytes) -> bytes | None:
        """Carry the call and return the reply's text."""
        ...


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

    def call(self, action: Action, request: bytes) -> bytes | None:
        """Run the action in the caller's thread and return the reply's text."""
        if self._contract.actions.get(action.name) is not action:
            raise TypeError(
                f"{self._contract.identity} has no action {action.name!r} of "
                "this declaration"
            )
        try:
            value = codec(self._contract, action.request).read(request)
            reply = self._functions[action.name](self._implementation, value)
            if action.reply is None:
                if reply is not None:
                    raise TypeError(
                        f"the reply is {type(reply).__qualname__}, but the "
                        "action's reply is empty"
                    )
                return None
            return codec(self._contract, action.reply).write(reply, "the reply")
        except Exception:
            _log.exception(
                "%s, action %r: the provider failed",
                self._contract.identity,
                action.name,
            )
        # Raised outside the handler, so the original is not its context.
        raise ContractError("internal", _INTERNAL)


@final
class Connector:
    """Calls the actions of one contract version through one transport.

    Each action of the contract is an attribute taking one request, by
    position or as ``request=``, and returning the reply, or ``None`` for an
    empty reply. Anything else is a mistake in the calling code, so it raises
    ``AttributeError`` or ``TypeError``, never a ``ContractError``.
    """

    __slots__ = ("_contract", "_transport")

    def __init__(self, declaration: type[Any], transport: Transport) -> None:
        self._contract = Contract.of(declaration)
        self._transport = transport

    def __getattr__(self, name: str) -> Callable[..., BaseModel | None]:
        # Only reached for a name the instance does not have; a private name
        # is never an action, which also covers a copy probing for hooks.
        actions = (
            object.__getattribute__(self, "_contract").actions
            if not name.startswith("_")
            else {}
        )
        found = actions.get(name)
        if found is None:
            raise AttributeError(
                f"{self._describe()} has no action {name!r}"
                + (f"; its actions are {', '.join(actions)}" if actions else "")
            )
        return self._bound(found)

    def __dir__(self) -> list[str]:
        return sorted({*object.__dir__(self), *self._contract.actions})

    def __repr__(self) -> str:
        return f"<Connector {self._contract.identity}>"

    def _describe(self) -> str:
        try:
            contract = object.__getattribute__(self, "_contract")
        except AttributeError:
            return "this connector"
        return f"the connector to {contract.identity}"

    def _bound(self, action: Action) -> Callable[..., BaseModel | None]:
        def call(*args: object, **kwargs: object) -> BaseModel | None:
            if len(args) + len(kwargs) != 1 or (kwargs and "request" not in kwargs):
                raise TypeError(
                    f"{self._contract.identity}, action {action.name!r} takes one "
                    "request, by position or as request=; got "
                    f"{len(args)} positional and {sorted(kwargs) or 'no'} keyword"
                )
            request = args[0] if args else kwargs["request"]
            return self._call(action, request)

        call.__name__ = call.__qualname__ = action.name
        return call

    def _call(self, action: Action, request: object) -> BaseModel | None:
        text = check_request(self._contract, action, request)
        try:
            reply = self._transport.call(action, text)
            if action.reply is None:
                if reply is not None:
                    raise TypeError("a reply arrived for an empty reply")
                return None
            if type(reply) is not bytes:
                raise TypeError(f"the reply is {type(reply).__qualname__}, not text")
            return codec(self._contract, action.reply).read(reply)
        except ContractError:
            raise
        except Exception:
            _log.exception(
                "%s, action %r: the reply could not be read",
                self._contract.identity,
                action.name,
            )
        raise ContractError("internal", _INTERNAL)
