"""Connectors and the in-process transport (spec 02).

A connector mirrors the actions of one contract version. It runs the caller's
request check (ADR 0017 Decision 9, step 2) and hands the request's JSON text
to a transport, which returns the reply's JSON text or raises a ``Failure``:
the error's kind, message and detail as JSON text. With the ``single`` routing
policy a connector has one provider and never sends a call twice: it never
retries and never falls back.

The in-process transport runs the provider's action in the caller's thread, in
a copy of the caller's context, so that what it sets in a context variable
stays on its side. What the process shares, such as thread-local state, the
warnings filters, logging and the exception the caller is handling, the action
shares too, as a direct call would; the call context's own scope arrives with
#10 task 6. It
reads the request text with the declared request type, so the action gets a new
value, and writes the reply with the declared reply type; the connector reads
that text back, so caller and provider never share an object (ADR 0007).

The provider's side, the provider's host in-process, delivers only an error
the action raised itself, of a kind it may raise: a standard kind other than
``unavailable`` and ``timeout``, or a declared kind with its detail record,
whose text an untyped map can hold. Every other failure becomes ``internal``,
with fixed text and nothing of the original, which the provider side logs; an
exception that is not an ``Exception`` propagates unchanged (ADR 0008). The
connector marks every ``ContractError`` it raises, so an action that lets one
end it, however it re-raises it, gets ``internal`` too.

The connector reads an error the way it reads a reply: a declared kind's
detail with its record, an unknown kind's detail as an untyped map, and
anything it cannot read as ``internal``.
"""

import contextlib
import contextvars
import inspect
import json
import logging
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any, NoReturn, Protocol, final

from pydantic import BaseModel

from neolith._contracts import (
    _STANDARD_KINDS,
    Action,
    Contract,
    ContractError,
    is_received,
    mark_received,
    untyped_problem,
)
from neolith._crossing import check_request, codec

__all__ = ["Connector", "Failure", "InProcess", "Transport"]

_log = logging.getLogger("neolith")

# The message of an internal error says nothing of the failure behind it.
_INTERNAL = "the provider failed, or its reply could not be read"
# Kinds that only hosts and transports produce, never an action.
_TRANSPORT_KINDS = frozenset({"unavailable", "timeout"})


@final
class Failure(Exception):
    """A call that ended in a contract error, as a transport carries it.

    ``detail`` is the JSON text of the error's detail, or ``None``. The
    connector reads it into the ``ContractError`` its caller gets.
    """

    def __init__(self, kind: str, message: str, detail: bytes | None = None) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self.message = message
        self.detail = detail


class Transport(Protocol):
    """Carries one call of a connector to its provider.

    ``call`` takes an action of ``contract`` and the request's JSON text,
    which passed the caller's request check, and returns the reply's JSON
    text, or ``None`` for an action whose reply is empty. Every outcome of the
    call that is not a reply is a ``Failure``; an action of another
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


def _untyped(detail: bytes) -> dict[str, Any]:
    """Read an unknown kind's detail as an untyped map (spec 01, CON-30).

    UTF-8 only, a member name at most once in each map, and no number JSON
    cannot hold; the bounds are checked by ``untyped_problem``.
    """

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        found = dict(items)
        if len(found) != len(items):
            raise ValueError("a member name occurs twice in one map")
        return found

    def refuse(constant: str) -> NoReturn:
        raise ValueError(f"{constant} is not a JSON number")

    value = json.loads(
        detail.decode("utf-8"), object_pairs_hook=pairs, parse_constant=refuse
    )
    problem = untyped_problem(value)
    if problem is not None:
        raise ValueError(f"the detail is not an untyped map: {problem}")
    return typing.cast(dict[str, Any], value)


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
            raise Failure("internal", _INTERNAL)
        if isinstance(outcome, tuple):
            raise Failure(*outcome)
        return outcome

    def _serve(
        self, action: Action, request: bytes
    ) -> bytes | tuple[str, str, bytes | None] | _Failed | None:
        """Read the request, run the action and write its reply, or the error
        it delivers as kind, message and detail text.

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
        except ContractError as error:
            try:
                delivered = self._deliver(action, error)
            except Exception as problem:  # the detail cannot be sent
                failure: BaseException = problem
            else:
                if delivered is not None:
                    return delivered
                failure = error
        except Exception as error:
            failure = error
        # Logged outside the handler, so a failing log does not chain to it,
        # then dropped, so its traceback does not keep the provider's frames.
        _report(
            f"{self._contract.identity}, action {action.name!r}: the provider failed",
            failure,
        )
        del failure
        return _FAILED

    def _deliver(
        self, action: Action, error: ContractError
    ) -> tuple[str, str, bytes | None] | None:
        """The error as the provider's host delivers it, or ``None`` when it
        becomes ``internal``; raises when its detail cannot be sent."""
        if is_received(error) or error.kind in _TRANSPORT_KINDS:
            return None
        if error.kind in _STANDARD_KINDS:
            return error.kind, error.message, None
        record = action.errors.get(error.kind)
        if record is None or type(error.data) is not record:
            return None
        text = codec(self._contract, record).write(error.data, "the error's detail")
        # A caller that does not know the kind holds the detail as an untyped
        # map, so its value as it travels must fit one.
        problem = untyped_problem(json.loads(text))
        if problem is not None:
            raise ValueError(f"the error's detail is not an untyped map: {problem}")
        return error.kind, error.message, text


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
        outcome = self._carry(action, text)
        if isinstance(outcome, ContractError):
            # Marked, so a provider's host that sees it end an action knows
            # the action received it rather than raised it.
            mark_received(outcome)
            raise outcome
        return outcome

    def _carry(self, action: Action, text: bytes) -> BaseModel | ContractError | None:
        """Send the call and return its reply, or the error the caller gets.

        Never raises an ``Exception``, so the error the caller gets is built
        here, from the kind, message and detail text alone.
        """
        what = "the transport failed"
        try:
            reply = self._transport.call(action, text)
        except Failure as failed:
            try:
                return self._error(action, failed.kind, failed.message, failed.detail)
            except Exception as error:
                failure: BaseException = error
                what = "the error could not be read"
        except Exception as error:
            failure = error
        else:
            try:
                return self._read(action, reply)
            except Exception as error:
                failure = error
                what = "the reply could not be read"
        _report(f"{self._contract.identity}, action {action.name!r}: {what}", failure)
        del failure
        return ContractError("internal", _INTERNAL)

    def _error(
        self, action: Action, kind: object, message: object, detail: object
    ) -> ContractError:
        """The error a caller gets for one that arrived; raises when it cannot
        be read against the contract."""
        if type(kind) is not str or type(message) is not str:
            raise TypeError("an error's kind and message are text")
        if detail is not None and type(detail) is not bytes:
            raise TypeError(f"an error's detail is {type(detail).__qualname__}")
        if kind in _STANDARD_KINDS:
            if detail is not None:
                raise ValueError(f"a {kind!r} error arrived with a detail")
            return ContractError(kind, message)
        record = action.errors.get(kind)
        if record is not None:
            if detail is None:
                raise ValueError(f"a {kind!r} error arrived without its detail")
            return ContractError(
                kind, message, codec(self._contract, record).read(detail)
            )
        if any(kind in other.errors for other in self._contract.actions.values()):
            raise ValueError(f"a {kind!r} error arrived for an action without it")
        # A kind this version of the contract does not know (CON-28).
        return ContractError(
            kind, message, None if detail is None else _untyped(detail)
        )

    def _read(self, action: Action, reply: object) -> BaseModel | None:
        if action.reply is None:
            if reply is not None:
                raise TypeError("a reply arrived for an action whose reply is empty")
            return None
        if type(reply) is not bytes:
            raise TypeError(f"the reply is {type(reply).__qualname__}, not text")
        return codec(self._contract, action.reply).read(reply)
