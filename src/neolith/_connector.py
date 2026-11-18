"""Connectors and the in-process transport (spec 02).

A connector mirrors the actions of one contract version. It runs the caller's
checks in the order of ADR 0017 Decision 9: the call context and its
principal, the request, the idempotency key, and the deadline. It then hands
the request's JSON text and the context to a transport, which returns the
reply's JSON text or raises a ``Failure``: the error's kind, message and
detail as JSON text. With the ``single`` routing policy a connector has one
provider and never sends a call twice: it never retries and never falls back.

The in-process transport runs the provider's action in the caller's thread, in
a copy of the caller's context, so that what it sets in a context variable
stays on its side. What the process shares, such as thread-local state, the
warnings filters, logging and the exception the caller is handling, the action
shares too, as a direct call would. It opens a scope around the action, which
holds the context that arrived and closes when the action ends, so the
action's own calls derive from it. It
reads the request text with the declared request type, so the action gets a new
value, and writes the reply with the declared reply type; the connector reads
that text back, so caller and provider never share an object (ADR 0007).

The deadline crosses as a budget, which each transport reads from the call's
deadline when it sends the call, and is counted from when the call arrives. It
is checked before the request is read and again when the action's outcome is
ready to return; an outcome that is late is discarded, and the call fails with
``timeout``. The action is never interrupted (ADR 0008 Decision 2). The
connector checks the caller's own deadline once more when the outcome is back,
since time can pass between the budget's reading and the call's arrival.

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
anything it cannot read as ``internal``. Text that a transport received from
a network gets the network receive check first (ADR 0007 Decision 3), as the
transport's ``receiver`` says.
"""

import contextlib
import contextvars
import enum
import inspect
import json
import logging
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any, NoReturn, Protocol, final

from pydantic import BaseModel

from neolith import _context, _naming
from neolith._context import Arrived, Principal, Sent
from neolith._contracts import (
    _STANDARD_KINDS,
    Action,
    Contract,
    ContractError,
    is_received,
    mark_received,
    untyped_problem,
)
from neolith._crossing import Receiver, Refusal, _interchange, check_request, codec

__all__ = ["Connector", "Failure", "InProcess", "Provider", "Transport"]

_log = logging.getLogger("neolith")

# The messages of a timeout: where the deadline was found to have passed.
_NOT_SENT = "the deadline passed before the call was sent"
_NOT_RUN = "the deadline passed before the action was run"
_LATE = "the deadline passed before the call ended; the action might have run"
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


def no_calling_app(identity: str) -> str:
    """The refusal of a call to ``identity`` that has no calling app, the
    same over every transport (TRN-42, ADR 0027 Decision 9).
    """
    return (
        f"a call to {identity} has no calling app; a call is made from a "
        "scope that belongs to an app"
    )


def internal_message(trace_id: str) -> str:
    """The message of an ``internal`` error that stands for another failure.

    Fixed text and the call's trace identifier, under which the host that
    failed logs the cause; nothing of the failure itself (ADR 0008).
    """
    return (
        f"the call failed inside a host, which logged the cause under trace {trace_id}"
    )


class Transport(Protocol):
    """Carries one call of a connector to its provider.

    ``call`` takes an action of ``contract``, the request's JSON text and the
    call's context as it is sent, all of which passed the caller's checks,
    and returns the reply's JSON text, or ``None`` for an action whose reply
    is empty. Every outcome of the call that is not a reply is a ``Failure``;
    an action of another contract is a ``TypeError``, a mistake in the code
    that called it. ``receiver`` says where the text it returns comes from,
    which decides how the connector reads it.
    """

    @property
    def contract(self) -> Contract:
        """The contract version whose calls this transport carries."""
        ...

    @property
    def receiver(self) -> Receiver:
        """Where the reply's and an error's text come from."""
        ...

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        """Carry the call and return the reply's text."""
        ...


class _Ended(enum.Enum):
    """How a call ended on the provider's side without an outcome to send."""

    FAILED = enum.auto()  # the provider failed: internal
    EXPIRED = enum.auto()  # the deadline had passed before the action was run


def _untyped(detail: bytes) -> dict[str, Any]:
    """Read an unknown kind's detail as an untyped map (spec 01, CON-30).

    The text is read as a network receiver reads it (CON-42), its nesting
    judged before the json module reads it: UTF-8 only, a member name at most
    once in each object, no lone surrogate and no number outside the range of
    a double. It is a JSON object, within the bounds ``untyped_problem``
    checks.
    """
    value, broken, _ = _interchange(detail)
    if broken is not None:
        raise ValueError(f"the detail {broken}")
    if type(value) is not dict:
        raise ValueError("the detail is not a JSON object")
    problem = untyped_problem(value)
    if problem is not None:
        raise ValueError(f"the detail is not an untyped map: {problem}")
    return typing.cast(dict[str, Any], value)


def mark(error: ContractError) -> ContractError:
    """Mark an error as one a connector raised, and return it.

    A provider's host that sees a marked error end an action knows the action
    received it rather than raised it (ADR 0008 Decision 4).
    """
    mark_received(error)
    return error


def _received(kind: str, message: str) -> ContractError:
    """A new error of a standard kind that a connector raises."""
    return mark(ContractError(kind, message))


def _report(message: str, error: BaseException) -> None:
    """Log a failure, never raising: a failing log must not carry it on."""
    with contextlib.suppress(Exception):
        _log.error(message, exc_info=error)


class Provider:
    """The provider's side of every transport, for one implementation of a
    contract (ADR 0011 Decision 3).

    ``app`` names the app the implementation belongs to, which is the caller
    of the calls its actions make, and ``requires`` the identities that app's
    declaration requires, or ``None`` when it has none. ``principal_from`` is
    the contract's allow-list: the calling apps that may assert a principal
    on a call to it. The host builds each from the providing app's
    declaration; left out, they fail closed: an action of no app, which every
    call it makes is refused from, and an empty list.

    Each transport runs the provider's first three steps (ADR 0017 Decision
    9) itself, with ``_refusal`` for the first two, and hands the rest to
    ``serve``: the deadline, the request's receive check, the action in a
    scope of its own, the rules on what an action may deliver (ADR 0008),
    and the deadline again once the action has ended.
    """

    __slots__ = (
        "_allowed",
        "_app",
        "_contract",
        "_functions",
        "_implementation",
        "_reads",
        "_requires",
    )

    def __init__(
        self,
        declaration: type[Any],
        implementation: object,
        *,
        app: str | None = None,
        requires: tuple[str, ...] | None = None,
        principal_from: tuple[str, ...] = (),
    ) -> None:
        """Check the implementation's class against the contract, as
        ``@implements`` does, and keep the function of each action.
        """
        contract = Contract.of(declaration)
        cls = type(implementation)
        contract._check_implementation(cls)
        if app is not None and type(app) is not str:
            raise TypeError(f"an app is named by a str, not {_naming.type_name(app)}")
        self._requires = _context.requires_option(requires)
        try:
            self._allowed = frozenset(_context.names(principal_from, "app names"))
        except TypeError as error:
            raise TypeError(f"principal_from {error}") from None
        # The class's own functions, read statically: an attribute set on the
        # instance, or a __getattr__, never stands in for an action.
        self._functions: Mapping[str, Callable[..., object]] = types.MappingProxyType(
            {
                name: typing.cast("Callable[..., object]", _naming.find(cls, name))
                for name in contract.actions
            }
        )
        # The actions that take the call context after the request; the
        # check above admitted that shape and (self, request) only.
        self._reads = frozenset(
            name
            for name, function in self._functions.items()
            if len(inspect.signature(function).parameters) == 3
        )
        self._contract = contract
        self._implementation = implementation
        self._app = app

    @property
    def contract(self) -> Contract:
        """The contract version the implementation provides."""
        return self._contract

    def _refusal(self, caller: str | None, asserts: bool) -> Failure | None:
        """The provider's checks of the calling app, steps 1 and 2, or
        ``None`` when the call passes them.

        ``caller`` is the calling app, which a network transport has
        authenticated and the in-process one is told: the app the call's
        parent scope belongs to. ``asserts`` says whether the call carries a
        principal, which is judged by its presence alone, whatever it holds.
        """
        identity = self._contract.identity
        if caller is None:
            return Failure("unauthenticated", no_calling_app(identity))
        if asserts and caller not in self._allowed:
            return Failure(
                "permission_denied",
                f"app {caller} may not assert a principal on a call to {identity}: "
                "the contract's allow-list does not name it",
            )
        return None

    def serve(
        self, action: Action, request: bytes, arrived: Arrived, receiver: Receiver
    ) -> bytes | None:
        """Run the provider's steps from the deadline on, and return the
        reply's text, or ``None`` for an empty reply.

        ``arrived`` is the call's context once the transport has run the
        first three steps, and ``receiver`` says where the request's text
        comes from. Text from a network gets the network receive check, and
        text it refuses fails with ``invalid_argument``, before the action
        runs; in-process the text has passed the sender's check, so text
        that cannot be read is a failure of the host. Every outcome that is
        not a reply is a ``Failure``; an action of another contract is a
        ``TypeError``.
        """
        if self._contract.actions.get(action.name) is not action:
            raise TypeError(
                f"{self._contract.identity} has no action {action.name!r} of "
                "this declaration"
            )
        if type(arrived) is not Arrived:
            raise TypeError(
                "a call is served from its context as it arrived, not "
                f"{_naming.type_name(arrived)}"
            )
        if type(receiver) is not Receiver:
            raise TypeError(
                f"a call's receiver is a Receiver, not {_naming.type_name(receiver)}"
            )
        outcome = self._serve(action, request, arrived, receiver)
        if isinstance(outcome, Failure):
            raise outcome
        return self._finish(action, arrived, outcome)

    def _finish(
        self,
        action: Action,
        arrived: Arrived,
        outcome: bytes | tuple[str, str, bytes | None] | _Ended | None,
    ) -> bytes | None:
        """Check the deadline again, now the outcome is ready, and return the
        reply's text or raise the ``Failure`` the outcome is.
        """
        deadline = arrived.deadline
        if outcome is _Ended.EXPIRED:
            raise Failure("timeout", _NOT_RUN)
        if deadline is not None and _context.now() >= deadline:
            # Whatever the action ended with is discarded: the caller's
            # deadline has passed, as it would have over a network.
            if isinstance(outcome, tuple):
                with contextlib.suppress(Exception):
                    _log.error(
                        "%s, action %r: the deadline passed, so the action's %s "
                        "error was discarded (trace %s)",
                        self._contract.identity,
                        action.name,
                        outcome[0],
                        arrived.trace_id,
                    )
            raise Failure("timeout", _LATE)
        if outcome is _Ended.FAILED:
            raise Failure("internal", internal_message(arrived.trace_id))
        if isinstance(outcome, tuple):
            raise Failure(*outcome)
        return outcome

    def _failed(self, action: Action, trace_id: str, failure: BaseException) -> None:
        """Log a failure of the provider, never raising."""
        _report(
            f"{self._contract.identity}, action {action.name!r}: the provider "
            f"failed (trace {trace_id})",
            failure,
        )

    def _serve(
        self, action: Action, request: bytes, arrived: Arrived, receiver: Receiver
    ) -> bytes | tuple[str, str, bytes | None] | _Ended | Failure | None:
        """Read the request, run the action and write its reply, or the error
        it delivers as kind, message and detail text.

        The steps follow the provider's order (ADR 0017 Decision 9): the
        deadline, the request, then the action, in a scope that closes when
        the action ends. A request a network receiver refuses is returned as
        its ``Failure``. Never raises an ``Exception``: a failure is logged
        and returned as ``FAILED``, so that once this returns, no frame holds
        the provider's values.
        """
        failure: BaseException
        try:
            scope = _context.received(arrived, self._app, self._requires)
        except Exception as error:  # a failure of the host, whatever it is
            failure = error
        else:
            if arrived.deadline is not None and _context.now() >= arrived.deadline:
                return _Ended.EXPIRED
            refused: str | None = None
            value: BaseModel | None = None
            reading = codec(self._contract, action.request)
            try:
                if receiver is Receiver.NETWORK:
                    value = reading.receive(request, "the request")
                else:
                    value = reading.read(request)
            except Refusal as error:
                refused = str(error)
            except Exception as error:
                failure = error
            if refused is not None:
                return Failure("invalid_argument", refused)
            if value is not None:
                try:
                    # In a copy of the caller's context, so that what the
                    # action sets in a context variable, and the scope it runs
                    # in, stay on the provider's side.
                    reply = contextvars.copy_context().run(
                        self._run, scope, action.name, value
                    )
                except ContractError as error:
                    # Only an error the action itself ends with is delivered.
                    try:
                        delivered = self._deliver(action, error)
                    except Exception as problem:  # the detail cannot be sent
                        failure = problem
                    else:
                        if delivered is not None:
                            return delivered
                        failure = error
                except Exception as error:
                    failure = error
                else:
                    try:
                        return self._write(action, reply)
                    except Exception as error:
                        failure = error
        # Logged outside the handler, so a failing log does not chain to it,
        # then dropped, so its traceback does not keep the provider's frames.
        self._failed(action, arrived.trace_id, failure)
        del failure
        return _Ended.FAILED

    def _run(
        self,
        scope: contextlib.AbstractContextManager[_context.Scope],
        name: str,
        value: BaseModel,
    ) -> object:
        """Run the action inside its scope, which closes however it ends."""
        with scope as opened:
            function = self._functions[name]
            if name in self._reads:
                return function(self._implementation, value, opened.context)
            return function(self._implementation, value)

    def _write(self, action: Action, reply: object) -> bytes | None:
        """The reply's text, or ``None`` for an empty reply."""
        if action.reply is None:
            if reply is not None:
                raise TypeError(
                    f"the reply is {_naming.type_name(reply)}, but the action's "
                    "reply is empty"
                )
            return None
        return codec(self._contract, action.reply).send(reply, "the reply")

    def _deliver(
        self, action: Action, error: ContractError
    ) -> tuple[str, str, bytes | None] | None:
        """The error as the provider's host delivers it, or ``None`` when it
        becomes ``internal``; raises when its detail cannot be sent.
        """
        if is_received(error) or error.kind in _TRANSPORT_KINDS:
            return None
        if error.kind in _STANDARD_KINDS:
            return error.kind, error.message, None
        record = action.errors.get(error.kind)
        if record is None or type(error.data) is not record:
            return None
        text = codec(self._contract, record).send(error.data, "the error's detail")
        # A caller that does not know the kind holds the detail as an untyped
        # map, so its value as it travels must fit one.
        problem = untyped_problem(json.loads(text))
        if problem is not None:
            raise ValueError(f"the error's detail is not an untyped map: {problem}")
        return error.kind, error.message, text


@final
class InProcess(Provider):
    """The in-process transport to one implementation of a contract.

    It is the provider's side (``Provider``), called in the caller's thread.
    Its arguments are the provider side's.

    The provider's side first checks the calling app (ADR 0017 Decision 9,
    steps 1 and 2): a call from no app fails with ``unauthenticated``, and
    one that carries a principal from an app the list does not name with
    ``permission_denied``, before the deadline is checked or the request
    read, so the action never runs.
    """

    __slots__ = ()

    #: The reply's text was written by a sender's check in this process.
    receiver = Receiver.IN_PROCESS

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        """Run the action in the caller's thread and return the reply's text.

        An action of another contract is a ``TypeError``: a connector never
        passes one, since it is built for this transport's contract.
        """
        arrived = _context.now()
        if self._contract.actions.get(action.name) is not action:
            raise TypeError(
                f"{self._contract.identity} has no action {action.name!r} of "
                "this declaration"
            )
        if type(context) is not Sent:
            raise TypeError(
                f"a call's context is sent as Sent, not {_naming.type_name(context)}"
            )
        # The calling app comes first: a call it refuses is refused before
        # anything else is read, and its action never runs.
        refusal = self._refusal(context.caller, context.principal is not None)
        if refusal is not None:
            raise refusal
        caller = typing.cast(str, context.caller)
        # The budget is read as the call is dispatched, and the deadline
        # counted from it, from when the call arrived (spec 03). On the
        # caller's own clock, it is never later than the caller's deadline.
        budget = _context.budget(context.deadline, arrived)
        deadline = (
            None
            if budget is None or context.deadline is None
            else min(arrived + budget / 1000, context.deadline)
        )
        # Step 3. The principal's form has passed the caller's check, so one
        # that cannot be read is a failure of this host, not of the call.
        problem: Exception | None = None
        principal: Principal | None = None
        try:
            if context.principal is not None:
                principal = _context.read_principal(context.principal)
        except Exception as error:
            problem = error
        incoming = Arrived(
            principal=principal,
            trace_id=context.trace_id,
            tracestate=context.tracestate,
            deadline=deadline,
            idempotency_key=context.idempotency_key,
            caller=caller,
        )
        if problem is not None:
            self._failed(action, context.trace_id, problem)
            del problem
            return self._finish(action, incoming, _Ended.FAILED)
        return self.serve(action, request, incoming, Receiver.IN_PROCESS)


@final
class Connector:
    """Calls the actions of one contract version through one transport.

    Each action of the contract is an attribute taking one request, by
    position or by the name the declaration gives it, and returning the
    reply, or ``None`` for an empty reply. A call takes two options (ADR 0017
    Decision 3). ``timeout=`` is a number of seconds from the call's start,
    which can bring the call's deadline forward and never puts it back.
    ``context=`` is a ``CallContext`` that names the scope to derive the
    call's context from, in place of the one the call is made in, as in a
    thread the action started.

    A call made in no open scope and naming none, or naming one that has
    closed, raises ``neolith.UsageError``, as does a call from a scope whose
    app does not require the contract. Any other mistake in the calling code
    raises ``AttributeError``, ``TypeError`` or ``ValueError`` (a request
    that fails the request check). None of these is a ``ContractError``. A
    ``ContractError`` the caller gets is a new one, raised from the
    connector, so nothing else of the transport's crosses.
    """

    __slots__ = ("_contract", "_network", "_parameters", "_transport")

    def __init__(self, declaration: type[Any], transport: Transport) -> None:
        contract = Contract.of(declaration)
        if transport.contract is not contract:
            raise TypeError(
                f"the transport carries calls of {transport.contract.identity}, "
                f"not of {contract.identity}"
            )
        receiver = transport.receiver
        if type(receiver) is not Receiver:
            kind = _naming.type_name(receiver)
            raise TypeError(f"a transport's receiver is a Receiver, not {kind}")
        self._contract = contract
        self._transport = transport
        # Text from a network gets the network receive check (ADR 0007).
        self._network = receiver is Receiver.NETWORK
        # The name each action's declaration gives its request, which a call
        # may pass it by, as it could to the declared method.
        self._parameters = types.MappingProxyType(
            {
                name: list(
                    inspect.signature(
                        typing.cast(
                            Callable[..., object],
                            _naming.find(declaration, name),
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

        def call(
            *args: object,
            context: object = None,
            timeout: object = None,
            **kwargs: object,
        ) -> BaseModel | None:
            if len(args) + len(kwargs) != 1 or (kwargs and parameter not in kwargs):
                raise TypeError(
                    f"{self._contract.identity}, action {action.name!r} takes one "
                    f"request, by position or as {parameter}=, and the options "
                    f"context= and timeout=; got {len(args)} positional and "
                    f"{sorted(kwargs) or 'no'} other keyword"
                )
            return self._call(
                action, args[0] if args else kwargs[parameter], context, timeout
            )

        call.__name__ = call.__qualname__ = action.name
        return call

    def _call(
        self, action: Action, request: object, context: object, timeout: object
    ) -> BaseModel | None:
        """Run the caller's checks in order, then send the call once.

        The call's own timeout is counted from here, where the connector is
        called, so the time its own checks take comes off it.
        """
        started = _context.now()
        seconds = _context.timeout_option(timeout)
        # Step 1: the context, and its principal's sent form.
        scope = _context.parent(context)
        # Step 1a: the calling app requires the contract (ADR 0021 Decision 9).
        caller = _context.check_required(scope, self._contract.identity)
        trace_id = scope.held.trace_id
        problem: Exception | None = None
        try:
            principal = scope.principal()
        except ValueError as error:
            problem = error
        if problem is not None:
            # A failure of this host, not of the calling code: the principal
            # comes from the end user's data. Raised outside the handler, so
            # the cause is logged and nothing of it is chained.
            _report(
                f"{self._contract.identity}, action {action.name!r}: the call's "
                f"principal is not valid, or could not be resolved (trace {trace_id})",
                problem,
            )
            del problem
            raise _received("internal", internal_message(trace_id))
        # Step 2: the request.
        text = check_request(self._contract, action, request)
        # Step 3: the key. The call counts as made from here on.
        key = scope.child_key(self._contract.identity, action.name, text)
        # Step 4: the deadline, the earlier of the scope's and the call's own.
        budget, deadline = scope.budget(seconds, started)
        if budget is not None and budget < 1:
            raise _received("timeout", _NOT_SENT)
        sent = Sent(
            principal=principal,
            trace_id=trace_id,
            tracestate=scope.held.tracestate,
            deadline=deadline,
            idempotency_key=key,
            caller=caller,
        )
        outcome = self._carry(action, text, sent)
        # The caller's own deadline, on its own clock: over a network it
        # stops waiting then, so whatever comes back later is discarded here
        # too. A timeout the transport gave says where the deadline passed.
        late = deadline is not None and _context.now() >= deadline
        if isinstance(outcome, ContractError):
            if not late or outcome.kind == "timeout":
                raise outcome
            with contextlib.suppress(Exception):
                _log.error(
                    "%s, action %r: the caller's deadline passed, so the %s error "
                    "that came back was discarded (trace %s)",
                    self._contract.identity,
                    action.name,
                    outcome.kind,
                    trace_id,
                )
        elif not late:
            return outcome
        del outcome
        raise _received("timeout", _LATE)

    def _carry(
        self, action: Action, text: bytes, sent: Sent
    ) -> BaseModel | ContractError | None:
        """Send the call and return its reply, or the error the caller gets.

        Never raises an ``Exception``, so the error the caller gets is built
        here, from the kind, message and detail text alone.
        """
        what = "the transport failed"
        try:
            reply = self._transport.call(action, text, sent)
        except Failure as failed:
            try:
                return mark(
                    self._error(action, failed.kind, failed.message, failed.detail)
                )
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
        _report(
            f"{self._contract.identity}, action {action.name!r}: {what} "
            f"(trace {sent.trace_id})",
            failure,
        )
        del failure
        return _received("internal", internal_message(sent.trace_id))

    def _error(
        self, action: Action, kind: object, message: object, detail: object
    ) -> ContractError:
        """The error a caller gets for one that arrived; raises when it cannot
        be read against the contract.
        """
        if type(kind) is not str or type(message) is not str:
            raise TypeError("an error's kind and message are text")
        if detail is not None and type(detail) is not bytes:
            raise TypeError(f"an error's detail is {_naming.type_name(detail)}")
        if kind in _STANDARD_KINDS:
            if detail is not None:
                raise ValueError(f"a {kind!r} error arrived with a detail")
            return ContractError(kind, message)
        record = action.errors.get(kind)
        if record is not None:
            if detail is None:
                raise ValueError(f"a {kind!r} error arrived without its detail")
            return ContractError(
                kind, message, self._received(record, detail, "the error's detail")
            )
        # A kind this version of the contract does not declare for the action
        # (CON-28): a later version may have added it, even if another action
        # declares it here (CON-1).
        return ContractError(
            kind, message, None if detail is None else _untyped(detail)
        )

    def _read(self, action: Action, reply: object) -> BaseModel | None:
        if action.reply is None:
            if reply is not None:
                raise TypeError("a reply arrived for an action whose reply is empty")
            return None
        if type(reply) is not bytes:
            raise TypeError(f"the reply is {_naming.type_name(reply)}, not text")
        return self._received(action.reply, reply, "the reply")

    def _received[R: BaseModel](self, record: type[R], text: bytes, subject: str) -> R:
        """Read text that arrived with ``record``: from a network, after the
        network receive check, whose ``Refusal`` the caller gets as
        ``internal``; in-process, with the validator alone.
        """
        reading = codec(self._contract, record)
        if self._network:
            return reading.receive(text, subject)
        return reading.read(text)
