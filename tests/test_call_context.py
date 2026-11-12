"""The call context across the connector and the in-process transport.

A call derives its context from an open scope, in the caller's order of ADR
0017 Decision 9, and the provider's side runs the action in a scope that holds
what arrived (spec 02, The call context across calls). The deadline is checked
before the action is run and when its outcome is ready, and a late outcome is
discarded (ADR 0008 Decision 2). Time is a clock the tests set, never a sleep.
"""

import base64
import contextlib
import contextvars
import copy
import dataclasses
import hashlib
import json
import logging
import pickle
import secrets
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, cast
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

import neolith
from neolith import (
    CallContext,
    Contract,
    ContractDefinitionError,
    ContractError,
    Principal,
    _connector,
    _context,
    _crossing,
    action,
    contract,
    implements,
)
from neolith._connector import (
    Connector,
    Failure,
    InProcess,
    internal_message,
    mark,
)
from neolith._context import Scope, Sent, read_principal
from neolith._contracts import Action, is_received


class Ask(BaseModel):
    n: int = 0
    x: float = 0.0


class Told(BaseModel):
    n: int = 0


class Gone(BaseModel):
    n: int


@contract("desk.v1")
class Desk(Protocol):
    @action(idempotent=True, errors={"gone": Gone})
    def ask(self, request: Ask) -> Told: ...

    @action
    def tell(self, request: Ask) -> None: ...


DESK = Contract.of(Desk)
ASK = DESK.actions["ask"]


class Seeing:
    """An implementation that keeps each context it is handed."""

    def __init__(
        self, then: Callable[[Ask, CallContext], object] | None = None
    ) -> None:
        self.seen: list[CallContext] = []
        self.told: list[Ask] = []
        self._then = then

    def ask(self, request: Ask, ctx: CallContext) -> Told:
        self.seen.append(ctx)
        made = self._then(request, ctx) if self._then is not None else None
        return Told(n=request.n) if made is None else cast("Told", made)

    def tell(self, request: Ask) -> None:
        self.told.append(request)


class Carrying:
    """A transport that keeps what it is handed, then passes it on or answers."""

    def __init__(self, inner: InProcess | None = None, reply: bytes = b"{}") -> None:
        self.contract = DESK
        self.sent: list[Sent] = []
        self.requests: list[bytes] = []
        self._inner = inner
        self._reply = reply

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        self.sent.append(context)
        self.requests.append(request)
        if self._inner is None:
            return self._reply if action.reply is not None else None
        return self._inner.call(action, request, context)


def _desk(
    then: Callable[[Ask, CallContext], object] | None = None, app: str | None = "desk"
) -> tuple[Any, Seeing, Carrying]:
    provider = Seeing(then)
    carrying = Carrying(InProcess(Desk, provider, app=app))
    return Connector(Desk, carrying), provider, carrying


class Clock:
    """A clock the test sets. ``steps`` move it on before the next readings."""

    def __init__(self) -> None:
        self.time = 1000.0
        self.steps: list[float] = []
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        if self.steps:
            self.time += self.steps.pop(0)
        return self.time


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    made = Clock()
    monkeypatch.setattr(_context, "now", made)
    return made


def _sent(**changes: Any) -> Sent:  # noqa: ANN401
    fields: dict[str, Any] = {
        "principal": None,
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "tracestate": "",
        "budget": None,
        "idempotency_key": "parent-key",
        "caller": None,
    }
    return Sent(**{**fields, **changes})


# Deterministic, as every test is (CONTRIBUTING T3): the same examples on each
# run, and no time limit that a busy machine could miss.
_SETTINGS = settings(derandomize=True, database=None, max_examples=200, deadline=None)

ALICE = Principal("https://id.example.com", "alice", {"roles": ["clerk"], "n": 1})
BOB = Principal("https://id.example.com", "bob")


# The public names.


def test_the_context_and_the_principal_are_public() -> None:
    assert neolith.CallContext is CallContext
    assert neolith.Principal is Principal
    assert {"CallContext", "Principal"} <= set(neolith.__all__)


# A call needs an open scope (TRN-22 to TRN-25).


@pytest.mark.spec("TRN-23", "CON-29")
def test_a_call_made_in_no_scope_is_a_usage_error_and_is_not_sent() -> None:
    connector, provider, carrying = _desk()
    with pytest.raises(RuntimeError, match="needs a call context") as raised:
        connector.ask(Ask())
    assert type(raised.value) is RuntimeError
    assert carrying.sent == []
    assert provider.seen == []
    # No context was started for it either.
    assert _context._OPEN == {}


@pytest.mark.spec("TRN-23", "CON-29")
def test_a_call_from_a_fresh_empty_context_is_a_usage_error() -> None:
    connector, provider, _ = _desk()

    def call() -> object:
        return connector.ask(Ask())

    with _context.root("shop"):
        # A thread the app starts may not inherit the context; an empty
        # Context gives the same on every build (ADR 0017, Consequences).
        with pytest.raises(RuntimeError, match="needs a call context"):
            contextvars.Context().run(call)
        refused: list[RuntimeError] = []

        def in_thread() -> None:
            try:
                contextvars.Context().run(call)
            except RuntimeError as error:
                refused.append(error)

        thread = threading.Thread(target=in_thread)
        thread.start()
        thread.join()
        assert len(refused) == 1
    assert provider.seen == []


@pytest.mark.spec("TRN-22")
def test_a_thread_calls_with_the_context_it_is_handed_or_a_copy() -> None:
    connector, provider, _ = _desk()
    with _context.root("shop", principal=ALICE) as scope, ThreadPoolExecutor(1) as pool:
        named = pool.submit(
            contextvars.Context().run, connector.ask, Ask(), context=scope.context
        )
        assert named.result() == Told()
        copied = pool.submit(contextvars.copy_context().run, connector.ask, Ask())
        assert copied.result() == Told()
    assert [ctx.principal for ctx in provider.seen] == [ALICE, ALICE]


@pytest.mark.spec("TRN-22", "TRN-31")
def test_a_named_scope_takes_the_place_of_the_ambient_one() -> None:
    connector, provider, _ = _desk()
    with _context.root("outer", principal=ALICE) as outer:
        with _context.root("inner", principal=BOB):
            connector.ask(Ask())
            connector.ask(Ask(), context=outer.context)
            connector.ask(Ask(), context=None)
        connector.ask(Ask())
    assert [(ctx.caller, ctx.principal) for ctx in provider.seen] == [
        ("inner", BOB),
        ("outer", ALICE),
        ("inner", BOB),
        ("outer", ALICE),
    ]
    assert provider.seen[1].trace_id == outer.context.trace_id


@pytest.mark.spec("TRN-24", "TRN-25", "CON-29")
@pytest.mark.parametrize("how", ["returns", "raises", "is stopped"])
def test_a_root_closes_however_it_ends(how: str) -> None:
    connector, provider, carrying = _desk()
    ended: dict[str, BaseException | None] = {
        "returns": None,
        "raises": ValueError("x"),
        "is stopped": KeyboardInterrupt(),
    }
    kept: list[tuple[CallContext, contextvars.Context]] = []
    try:
        with _context.root("shop") as scope:
            kept.append((scope.context, contextvars.copy_context()))
            connector.ask(Ask())
            if ended[how] is not None:
                raise cast("BaseException", ended[how])
    except (ValueError, KeyboardInterrupt):
        pass
    ((context, held),) = kept
    assert scope.closed is True
    assert _context._OPEN == {}
    with pytest.raises(RuntimeError, match="names no open scope") as raised:
        connector.ask(Ask(), context=context)
    assert type(raised.value) is RuntimeError
    # A context copied while the root was open still names it, closed.
    with pytest.raises(RuntimeError, match="has ended"):
        held.run(connector.ask, Ask())
    assert len(carrying.sent) == 1
    assert len(provider.seen) == 1


@pytest.mark.spec("TRN-24", "TRN-25")
@pytest.mark.parametrize(
    "ending",
    [
        None,
        ContractError("not_found", "no such desk"),
        ContractError("gone", "gone", Gone(n=1)),
        RuntimeError("the action failed"),
        KeyboardInterrupt(),
    ],
    ids=["a reply", "a standard error", "a declared error", "a failure", "a stop"],
)
def test_an_actions_scope_closes_however_the_action_ends(
    ending: BaseException | None, caplog: pytest.LogCaptureFixture
) -> None:
    held: list[contextvars.Context] = []

    def then(request: Ask, ctx: CallContext) -> None:
        held.append(contextvars.copy_context())
        if ending is not None and len(held) == 1:
            raise ending

    connector, provider, _ = _desk(then)
    with _context.root("shop") as scope, caplog.at_level(logging.CRITICAL, "neolith"):
        with contextlib.suppress(ContractError, KeyboardInterrupt):
            connector.ask(Ask())
        (kept,) = provider.seen
        # Only the root is open once the call has ended.
        assert list(_context._OPEN) == [scope.context.scope_id]
        with pytest.raises(RuntimeError, match="names no open scope"):
            connector.ask(Ask(), context=kept)
        # Work that kept the action's context, as a thread it started would.
        with pytest.raises(RuntimeError, match="has ended"):
            held[0].run(connector.ask, Ask())
        # The caller's own scope is the ambient one again.
        assert connector.ask(Ask(n=2)) == Told(n=2)
    assert len(provider.seen) == 2


@pytest.mark.spec("TRN-26")
def test_a_context_value_only_names_its_scope() -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop", principal=ALICE, timeout=30) as scope:
        issued = scope.context
        changed = dataclasses.replace(
            issued,
            principal=BOB,
            trace_id="f" * 32,
            tracestate="other=1",
            deadline=None,
            idempotency_key="chosen",
            caller="admin",
        )
        values = [
            issued,
            changed,
            copy.copy(issued),
            copy.deepcopy(changed),
            pickle.loads(pickle.dumps(changed)),  # noqa: S301
        ]
        with contextvars.copy_context().run(_context.root, "elsewhere"):
            for value in values:
                connector.ask(Ask(), context=value)
    assert len(provider.seen) == len(values)
    for ctx, sent in zip(provider.seen, carrying.sent, strict=True):
        # Everything comes from the scope: nothing of the changed copy is read.
        assert ctx.principal == ALICE
        assert ctx.trace_id == issued.trace_id
        assert ctx.tracestate == ""
        assert ctx.caller == "shop"
        assert ctx.deadline is not None
        assert sent.budget is not None
        assert sent.budget <= 30_000
        assert sent.idempotency_key != "chosen"


@pytest.mark.spec("TRN-26")
def test_a_change_forced_into_a_context_value_reaches_no_call() -> None:
    inner, inner_provider, inner_carrying = _desk()

    def then(request: Ask, ctx: CallContext) -> None:
        # A frozen value can still be written to by going around it.
        object.__setattr__(ctx, "deadline", None)
        object.__setattr__(ctx, "principal", BOB)
        object.__setattr__(ctx, "idempotency_key", "chosen")
        object.__setattr__(ctx, "trace_id", "f" * 32)
        inner.ask(Ask())
        inner.ask(Ask(), context=ctx)

    outer, _, outer_carrying = _desk(then)
    with _context.root("shop", principal=ALICE, timeout=30) as scope:
        outer.ask(Ask())
    key = outer_carrying.sent[0].idempotency_key
    assert [ctx.principal for ctx in inner_provider.seen] == [ALICE, ALICE]
    assert [sent.idempotency_key for sent in inner_carrying.sent] == [
        _key(key, "desk.v1", "ask", '{"n":0,"x":0}', count) for count in (0, 1)
    ]
    for sent in inner_carrying.sent:
        assert sent.trace_id == scope.context.trace_id
        assert sent.budget is not None


@pytest.mark.spec("TRN-26", "CON-29")
def test_a_context_this_host_did_not_issue_names_no_scope() -> None:
    connector, provider, _ = _desk()
    with _context.root("shop") as scope:
        for scope_id in ("0" * 32, "", scope.context.scope_id.upper(), None, ["x"], 7):
            forged = dataclasses.replace(scope.context, scope_id=cast("str", scope_id))
            with pytest.raises(RuntimeError, match="names no open scope"):
                connector.ask(Ask(), context=forged)
    assert provider.seen == []


@pytest.mark.spec("TRN-16", "CON-29")
@pytest.mark.parametrize(
    ("option", "value", "error"),
    [
        ("context", "ctx", TypeError),
        ("context", {"scope_id": "x"}, TypeError),
        ("context", CallContext, TypeError),
        ("context", 0, TypeError),
        ("timeout", "1", TypeError),
        ("timeout", True, TypeError),
        ("timeout", [1], TypeError),
        ("timeout", 1 + 0j, TypeError),
        ("timeout", float("nan"), ValueError),
        ("timeout", float("inf"), ValueError),
        ("timeout", float("-inf"), ValueError),
    ],
    ids=repr,
)
def test_an_option_of_another_kind_is_a_usage_error(
    option: str, value: object, error: type[Exception]
) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop", timeout=30) as scope:
        with pytest.raises(error, match=f"{option}= takes") as raised:
            connector.ask(Ask(), **{option: value})
        assert type(raised.value) is error
        # The scope itself is not a context value.
        with pytest.raises(TypeError, match="context= takes a CallContext"):
            connector.ask(Ask(), context=scope)
    assert carrying.sent == []
    assert provider.seen == []


@pytest.mark.spec("TRN-16")
def test_the_options_go_with_a_request_passed_either_way(clock: Clock) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop") as scope:
        assert connector.ask(Ask(n=1), timeout=5, context=scope.context) == Told(n=1)
        assert connector.ask(request=Ask(n=2), timeout=5.5, context=None) == Told(n=2)
        assert connector.ask(Ask(n=3), timeout=None) == Told(n=3)
        assert connector.tell(Ask(n=4), timeout=1) is None
    assert [sent.budget for sent in carrying.sent] == [5000, 5500, None, 1000]
    assert provider.told == [Ask(n=4)]


# What a call carries (TRN-27 to TRN-33).


@pytest.mark.spec("TRN-2", "TRN-30", "TRN-31", "TRN-36", "TRN-37")
def test_an_action_receives_the_context_of_the_call(clock: Clock) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop", principal=ALICE, timeout=30) as scope:
        connector.ask(Ask())
    (ctx,) = provider.seen
    (sent,) = carrying.sent
    assert type(ctx) is CallContext
    # The principal is read from its sent form: an equal value, not the object.
    assert ctx.principal == ALICE
    assert ctx.principal is not ALICE
    assert sent.principal == _context.principal_form(ALICE)
    assert ctx.trace_id == scope.context.trace_id == sent.trace_id
    assert ctx.tracestate == ""
    assert ctx.caller == "shop"
    assert ctx.idempotency_key == sent.idempotency_key
    # The deadline is counted from the budget when the call arrived.
    assert sent.budget == 30_000
    assert ctx.deadline == 1030.0
    # A scope and a span of its own, on the same trace.
    assert ctx.scope_id != scope.context.scope_id
    assert ctx.span_id != scope.context.span_id
    assert len(ctx.trace_id) == 32
    assert len(ctx.span_id) == 16
    assert int(ctx.trace_id, 16) != 0
    assert int(ctx.span_id, 16) != 0
    # Equal, as ADR 0017 Decision 1 compares contexts, to what the caller sent.
    assert ctx == CallContext(
        principal=ALICE,
        trace_id=scope.context.trace_id,
        span_id="",
        tracestate="changed on the way",
        deadline=0.0,
        idempotency_key=sent.idempotency_key,
        caller="shop",
        scope_id="",
    )


@pytest.mark.spec("TRN-37")
def test_an_action_that_does_not_take_the_context_is_run_without_it() -> None:
    connector, provider, _ = _desk()
    with _context.root():
        assert connector.tell(Ask(n=3)) is None
    assert provider.told == [Ask(n=3)]


@pytest.mark.spec("TRN-30", "TRN-31", "TRN-36")
def test_a_nested_call_carries_its_parents_context_on() -> None:
    inner, inner_provider, inner_carrying = _desk(app="stock")

    def then(request: Ask, ctx: CallContext) -> None:
        inner.ask(Ask(n=request.n + 1))

    outer, outer_provider, _ = _desk(then, app="desk")
    with _context.root("shop", principal=ALICE, timeout=10) as scope:
        outer.ask(Ask())
    (first,) = outer_provider.seen
    (second,) = inner_provider.seen
    assert first.principal == second.principal == ALICE
    assert first.trace_id == second.trace_id == scope.context.trace_id
    # The calling app is the app of the scope each call was made from.
    assert (first.caller, second.caller) == ("shop", "desk")
    assert first.idempotency_key != second.idempotency_key
    assert len({scope.context.span_id, first.span_id, second.span_id}) == 3
    assert first.deadline is not None
    assert second.deadline is not None
    # A nested call's deadline is never later than its parent's (spec 00).
    assert second.deadline <= first.deadline <= cast("float", scope.context.deadline)
    assert inner_carrying.sent[0].caller == "desk"


@pytest.mark.spec("TRN-31")
def test_a_call_from_a_scope_of_no_app_carries_no_caller() -> None:
    inner, inner_provider, _ = _desk(app=None)
    outer, outer_provider, _ = _desk(lambda request, ctx: inner.ask(Ask()), app=None)
    with _context.root():
        outer.ask(Ask())
    assert outer_provider.seen[0].caller is None
    assert inner_provider.seen[0].caller is None


@pytest.mark.spec("TRN-30", "TRN-36")
def test_the_tracestate_that_arrived_is_passed_on() -> None:
    inner, inner_provider, inner_carrying = _desk()
    provider = Seeing(lambda request, ctx: inner.ask(Ask()))
    transport = InProcess(Desk, provider, app="desk")
    transport.call(ASK, b"{}", _sent(tracestate="vendor=1,other=2", caller="shop"))
    assert provider.seen[0].tracestate == "vendor=1,other=2"
    assert provider.seen[0].caller == "shop"
    assert provider.seen[0].idempotency_key == "parent-key"
    assert inner_carrying.sent[0].tracestate == "vendor=1,other=2"
    assert inner_provider.seen[0].tracestate == "vendor=1,other=2"
    assert inner_provider.seen[0].trace_id == "0af7651916cd43dd8448eb211c80319c"


def test_an_app_or_a_context_of_another_type_is_refused_by_the_transport() -> None:
    with pytest.raises(TypeError, match="an app is named by a str"):
        InProcess(Desk, Seeing(), app=cast("str", 7))
    with pytest.raises(TypeError, match="an app is named by a str"):
        _context.root(cast("str", b"shop"))
    with pytest.raises(TypeError, match="principal= takes a Principal"):
        _context.root(principal=cast("Principal", ("i", "s")))
    with pytest.raises(TypeError, match="sent as Sent"):
        InProcess(Desk, Seeing()).call(ASK, b"{}", cast("Sent", None))


# The deadline and the budget (TRN-27 to TRN-29).


@pytest.mark.spec("TRN-27")
@pytest.mark.parametrize(
    ("root", "own", "budget"),
    [
        (None, None, None),
        (10, None, 10_000),
        (None, 3, 3000),
        (10, 3, 3000),
        (10, 20, 10_000),
        (10, 10, 10_000),
        (0.5, 0.25, 250),
        (None, 0.001, 1),
        (None, 0.0015, 1),
        (None, 2**-10, 0.9765625),
    ],
)
def test_a_calls_deadline_is_the_earlier_of_its_parents_and_its_own(
    clock: Clock, root: float | None, own: float | None, budget: float | None
) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop", timeout=root):
        if budget is not None and budget < 1:
            with pytest.raises(ContractError, match="timeout"):
                connector.ask(Ask(), timeout=own)
            return
        connector.ask(Ask(), timeout=own)
    assert carrying.sent[0].budget == budget
    # The provider's deadline is the budget from when the call arrived.
    expected = None if budget is None else 1000.0 + budget / 1000
    assert provider.seen[0].deadline == expected


@pytest.mark.spec("TRN-27")
def test_a_timeout_is_counted_from_when_the_connector_is_called(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _crossing.check_request

    def slow_check(contract: Contract, action: Action, request: object) -> bytes:
        # The caller's own check of the request takes a quarter of a second.
        clock.time += 0.25
        return check(contract, action, request)

    monkeypatch.setattr(_connector, "check_request", slow_check)
    connector, _, carrying = _desk()
    with _context.root("shop"):
        connector.ask(Ask(), timeout=2)
        # The checks alone use the timeout up: counted as made, never sent.
        with pytest.raises(ContractError) as raised:
            connector.ask(Ask(), timeout=0.25)
    assert [sent.budget for sent in carrying.sent] == [1750]
    assert raised.value.message == "the deadline passed before the call was sent"


# The caller's own deadline (TRN-38).


@pytest.mark.spec("TRN-38", "TRN-4")
@pytest.mark.parametrize("ending", ["a reply", "a standard error", "a failure"])
def test_an_outcome_after_the_callers_deadline_is_discarded_by_the_caller(
    clock: Clock, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    def then(request: Ask, ctx: CallContext) -> object:
        clock.time += 0.75
        made = _ENDINGS[ending]()
        if isinstance(made, BaseException):
            raise made
        return made

    connector, provider, carrying = _desk(then)
    # The clock stands at 1000 for the call's start and its budget, and half
    # a second passes before the call arrives. The provider counts its second
    # from arrival, so it is in time at 1001.25; the caller's deadline was
    # 1001.
    clock.steps = [0, 0, 0.5]
    with (
        _context.root("shop") as scope,
        caplog.at_level(logging.ERROR, "neolith"),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask(), timeout=1)
    assert carrying.sent[0].budget == 1000
    assert provider.seen[0].deadline == 1001.5
    assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)
    assert raised.value.data is None
    assert is_received(raised.value)
    assert raised.value.__context__ is None
    if ending == "a standard error":
        # The caller's host records the error it discarded.
        (record,) = caplog.records
        assert "not_found" in record.getMessage()
        assert scope.context.trace_id in record.getMessage()


@pytest.mark.spec("TRN-38")
def test_an_outcome_just_inside_the_callers_deadline_is_delivered(
    clock: Clock,
) -> None:
    def then(request: Ask, ctx: CallContext) -> None:
        clock.time += 0.25

    connector, _, _ = _desk(then)
    with _context.root("shop", timeout=1):
        clock.steps = [0, 0, 0.5]
        assert connector.ask(Ask()) == Told()
        clock.steps = [0, 0, 0.5, 0, 0, 0.25]
        with pytest.raises(ContractError, match="timeout"):
            connector.ask(Ask())


@pytest.mark.spec("TRN-38")
def test_a_timeout_the_transport_gives_is_passed_on_as_it_is(clock: Clock) -> None:
    class Late:
        contract = DESK

        def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
            clock.time += 5
            raise Failure("timeout", "the provider stopped waiting")

    connector: Any = Connector(Desk, Late())
    with _context.root("shop"), pytest.raises(ContractError) as raised:
        connector.ask(Ask(), timeout=1)
    assert raised.value.message == "the provider stopped waiting"


@pytest.mark.spec("TRN-38", "TRN-4")
@_SETTINGS
@given(
    own=st.integers(1, 20),
    root=st.none() | st.integers(1, 20),
    before=st.integers(0, 6),
    transit=st.integers(0, 6),
    spent=st.integers(0, 40),
    back=st.integers(0, 6),
    raises=st.booleans(),
)
def test_no_outcome_reaches_the_caller_after_its_own_deadline(
    own: int,
    root: int | None,
    before: int,
    transit: int,
    spent: int,
    back: int,
    raises: bool,
) -> None:
    clock = Clock()
    # Halves of a second are exact. Time passes before the budget is read, on
    # the way to the provider, in the action and on the way back.
    deadline = 1000 + (own if root is None else min(own, root))

    def then(request: Ask, ctx: CallContext) -> None:
        clock.time += spent / 2
        if raises:
            raise ContractError("not_found", "no such desk")

    connector, provider, _ = _desk(then)
    with mock.patch.object(_context, "now", clock), _context.root("shop", timeout=root):
        clock.steps = [0, before / 2, transit / 2, 0, back / 2]
        try:
            reply = connector.ask(Ask(), timeout=own)
        except ContractError as error:
            outcome = error.kind
        else:
            assert reply == Told()
            outcome = "reply"
        ended = clock.time
    if ended >= deadline:
        assert outcome == "timeout"
    else:
        assert outcome == ("not_found" if raises else "reply")
        assert len(provider.seen) == 1


@pytest.mark.spec("TRN-27", "TRN-28")
def test_time_that_passes_in_the_parent_comes_off_the_budget(clock: Clock) -> None:
    connector, _, carrying = _desk()
    with _context.root("shop", timeout=10):
        clock.time += 4
        connector.ask(Ask())
        connector.ask(Ask(), timeout=7)
        connector.ask(Ask(), timeout=2)
        clock.time += 5.5
        connector.ask(Ask())
        clock.time += 0.5
        with pytest.raises(ContractError) as raised:
            connector.ask(Ask())
    assert [sent.budget for sent in carrying.sent] == [6000, 6000, 2000, 500]
    assert raised.value.kind == "timeout"


@pytest.mark.spec("TRN-28", "TRN-4")
@pytest.mark.parametrize("own", [0, 0.0, -1, -0.5, 0.0009, 2**-11, -(10**400)])
def test_a_call_with_no_budget_left_fails_with_timeout_and_is_not_sent(
    clock: Clock, own: float
) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop"), pytest.raises(ContractError) as raised:
        connector.ask(Ask(), timeout=own)
    assert raised.value.kind == "timeout"
    assert raised.value.message == "the deadline passed before the call was sent"
    assert raised.value.data is None
    # An action that lets it end the action gives its own caller internal.
    assert is_received(raised.value)
    assert carrying.sent == []
    assert provider.seen == []


@pytest.mark.spec("TRN-28")
def test_a_call_from_a_scope_whose_deadline_has_passed_is_not_sent(
    clock: Clock,
) -> None:
    connector, _, carrying = _desk()
    with _context.root("shop", timeout=5):
        # 1/512 of a second is left: a budget of 1 ms, rounded down.
        clock.time += 5 - 2**-9
        assert connector.ask(Ask()) == Told()
        clock.time += 2**-10
        for own in (None, 60):
            with pytest.raises(ContractError) as raised:
                connector.ask(Ask(), timeout=own)
            assert raised.value.kind == "timeout"
    assert [sent.budget for sent in carrying.sent] == [1]


@pytest.mark.spec("TRN-29")
@pytest.mark.parametrize(
    ("own", "budget"),
    [
        (9_999_999, 9_999_999_000),
        (9_999_999.5, 9_999_999_500),
        (10_000_000, 9_999_999_999),
        (1e12, 9_999_999_999),
        (1.7e308, 9_999_999_999),
        (10**400, 9_999_999_999),
    ],
)
def test_a_budget_is_capped(clock: Clock, own: float, budget: int) -> None:
    clock.time = 0.0
    connector, provider, carrying = _desk()
    with _context.root("shop"):
        connector.ask(Ask(), timeout=own)
    with _context.root("shop", timeout=own):
        connector.ask(Ask())
    assert [sent.budget for sent in carrying.sent] == [budget, budget]
    assert provider.seen[0].deadline == budget / 1000


# The deadline on the provider's side (TRN-4, TRN-19 to TRN-21).


@pytest.mark.spec("TRN-19", "TRN-20", "TRN-4")
def test_a_deadline_that_passes_before_dispatch_fails_with_timeout(
    clock: Clock,
) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop"):
        # Readings: the call's start, its budget, its arrival, then the
        # provider's check, by which time the one second has passed.
        clock.steps = [0, 0, 0, 1]
        with pytest.raises(ContractError) as raised:
            connector.ask(Ask(), timeout=1)
    assert raised.value.kind == "timeout"
    assert raised.value.message == "the deadline passed before the action was run"
    assert [sent.budget for sent in carrying.sent] == [1000]
    assert provider.seen == []
    assert _context._OPEN == {}


@pytest.mark.spec("TRN-19", "TRN-35")
def test_the_deadline_is_checked_before_the_request_is_read(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    provider = Seeing()
    transport = InProcess(Desk, provider)
    clock.steps = [0, 1]
    with caplog.at_level(logging.ERROR, "neolith"):
        with pytest.raises(Failure) as raised:
            transport.call(ASK, b"not json", _sent(budget=1000))
        # The step that failed is the deadline's: its message, and no failure
        # of the provider logged, since the request was never read.
        assert (raised.value.kind, raised.value.message) == (
            "timeout",
            "the deadline passed before the action was run",
        )
        assert caplog.records == []
        # In time, the same request fails the receive check instead.
        with pytest.raises(Failure) as raised:
            transport.call(ASK, b"not json", _sent(budget=1000))
        assert raised.value.kind == "internal"
        assert len(caplog.records) == 1
    assert provider.seen == []


@pytest.mark.spec("TRN-35")
def test_the_context_is_read_before_the_deadline_is_checked(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # In-process the principal's form has passed the caller's check, so one
    # that cannot be read is a failure of the host, found at the step where a
    # provider reads the headers: before the deadline.
    transport = InProcess(Desk, Seeing())
    with caplog.at_level(logging.ERROR, "neolith"):
        clock.steps = [0, 0.5]
        with pytest.raises(Failure) as raised:
            transport.call(ASK, b"{}", _sent(principal=b"{}", budget=1000))
        assert raised.value.kind == "internal"
        assert clock.reads == 2
        # With the deadline passed as well, the failure is still found first:
        # it is logged, and the call is late, not refused before it ran.
        clock.steps = [0, 5]
        with pytest.raises(Failure) as raised:
            transport.call(ASK, b"{}", _sent(principal=b"{}", budget=1000))
        assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)
    assert len(caplog.records) == 2
    assert all("the provider failed" in r.getMessage() for r in caplog.records)


_LATE = "the deadline passed before the call ended; the action might have run"

_ENDINGS: dict[str, Callable[[], object]] = {
    "a reply": lambda: Told(n=1),
    "a reply that cannot be sent": lambda: "not a reply",
    "a standard error": lambda: ContractError("not_found", "no such desk"),
    "a declared error": lambda: ContractError("gone", "gone", Gone(n=1)),
    "an error it may not raise": lambda: ContractError("unavailable", "down"),
    "an error it received": lambda: mark(ContractError("not_found", "elsewhere")),
    "a failure": lambda: RuntimeError("the action failed"),
}


@pytest.mark.spec("TRN-19", "TRN-20", "TRN-4")
@pytest.mark.parametrize("ending", list(_ENDINGS), ids=list(_ENDINGS))
def test_an_outcome_after_the_deadline_is_discarded(
    clock: Clock, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    def then(request: Ask, ctx: CallContext) -> object:
        clock.time += 2
        made = _ENDINGS[ending]()
        if isinstance(made, BaseException):
            raise made
        return made

    connector, provider, _ = _desk(then)
    with (
        _context.root("shop") as scope,
        caplog.at_level(logging.ERROR, "neolith"),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask(), timeout=2)
    error = raised.value
    assert (error.kind, error.message, error.data) == ("timeout", _LATE, None)
    assert error.__cause__ is None
    assert error.__context__ is None
    # The action ran: timeout leaves the outcome unknown.
    assert len(provider.seen) == 1
    trace = scope.context.trace_id
    if ending == "a reply":
        assert caplog.records == []
    else:
        # The provider's host still records what it discarded.
        (record,) = caplog.records
        assert trace in record.getMessage()
        assert "desk.v1, action 'ask'" in record.getMessage()


# The two checks of a late outcome are each tested alone, since either gives
# the same kind and message: the provider's side on the transport itself, and
# through a connector whose own deadline is later (TRN-19, TRN-20); the
# caller's with transports that check nothing (TRN-38).


def _ending(clock: Clock, spent: float, ending: str) -> Callable[..., object]:
    def then(request: Ask, ctx: CallContext) -> object:
        clock.time += spent
        made = _ENDINGS[ending]()
        if isinstance(made, BaseException):
            raise made
        return made

    return then


@pytest.mark.spec("TRN-19", "TRN-20", "TRN-4", "CON-43", "CON-44", "CON-48")
@pytest.mark.parametrize("ending", list(_ENDINGS), ids=list(_ENDINGS))
def test_the_transport_discards_an_outcome_after_its_deadline(
    clock: Clock, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    provider = Seeing(_ending(clock, 2, ending))
    transport = InProcess(Desk, provider)
    with caplog.at_level(logging.ERROR, "neolith"), pytest.raises(Failure) as raised:
        transport.call(ASK, b"{}", _sent(budget=2000))
    failed = raised.value
    assert (failed.kind, failed.message, failed.detail) == ("timeout", _LATE, None)
    assert failed.__context__ is None
    assert len(provider.seen) == 1
    messages = [record.getMessage() for record in caplog.records]
    if ending == "a reply":
        assert messages == []
        return
    # The provider's host records what it discarded, under the trace.
    (message,) = messages
    assert "0af7651916cd43dd8448eb211c80319c" in message
    discarded = {"a standard error": "not_found", "a declared error": "gone"}
    if ending in discarded:
        assert f"the action's {discarded[ending]} error was discarded" in message
    else:
        assert "the provider failed" in message


@pytest.mark.spec("TRN-20")
@pytest.mark.parametrize("ending", list(_ENDINGS), ids=list(_ENDINGS))
def test_the_transport_delivers_an_outcome_just_inside_its_deadline(
    clock: Clock, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    transport = InProcess(Desk, Seeing(_ending(clock, 1.9375, ending)))
    delivered = {"a standard error": "not_found", "a declared error": "gone"}
    with caplog.at_level(logging.CRITICAL, "neolith"):
        if ending == "a reply":
            assert transport.call(ASK, b"{}", _sent(budget=2000)) == b'{"n":1}'
            return
        with pytest.raises(Failure) as raised:
            transport.call(ASK, b"{}", _sent(budget=2000))
    assert raised.value.kind == delivered.get(ending, "internal")


@pytest.mark.spec("TRN-19", "TRN-20")
@pytest.mark.parametrize("ending", ["a reply", "a standard error", "a failure"])
def test_the_providers_check_holds_where_the_callers_cannot_fire(
    clock: Clock, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    # The budget is rounded down to 1000 ms, so the provider's deadline, at
    # 1001, is earlier than the caller's, at 1001 and 1/2048. An outcome
    # between the two is late for the provider's side alone.
    connector, provider, carrying = _desk(_ending(clock, 1 + 2**-12, ending))
    with (
        _context.root("shop"),
        caplog.at_level(logging.ERROR, "neolith"),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask(), timeout=1 + 2**-11)
    assert carrying.sent[0].budget == 1000
    assert provider.seen[0].deadline == 1001.0
    assert clock.time < 1001 + 2**-11
    assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)
    # Only the provider's side logged a discard, if there was one to log.
    assert not any("caller's deadline" in r.getMessage() for r in caplog.records)
    if ending == "a standard error":
        (record,) = caplog.records
        assert "the action's not_found error was discarded" in record.getMessage()


class Slow:
    """A transport with no deadline check of its own, which takes a while."""

    contract = DESK

    def __init__(self, clock: Clock, takes: float, outcome: Failure | None) -> None:
        self._clock = clock
        self._takes = takes
        self._outcome = outcome

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        self._clock.time += self._takes
        if self._outcome is not None:
            raise self._outcome
        return b'{"n":7}'


@pytest.mark.spec("TRN-38", "TRN-4")
@pytest.mark.parametrize(
    "outcome",
    [None, Failure("not_found", "no such desk"), Failure("gone", "gone", b'{"n":1}')],
    ids=["a reply", "a standard error", "a declared error"],
)
def test_the_caller_discards_what_a_transport_returns_late(
    clock: Clock, caplog: pytest.LogCaptureFixture, outcome: Failure | None
) -> None:
    late: Any = Connector(Desk, Slow(clock, 1, outcome))
    in_time: Any = Connector(Desk, Slow(clock, 0.9375, outcome))
    with _context.root("shop") as scope, caplog.at_level(logging.ERROR, "neolith"):
        if outcome is None:
            assert in_time.ask(Ask(), timeout=1) == Told(n=7)
        else:
            with pytest.raises(ContractError) as raised:
                in_time.ask(Ask(), timeout=1)
            assert raised.value.kind == outcome.kind
        assert caplog.records == []
        with pytest.raises(ContractError) as raised:
            late.ask(Ask(), timeout=1)
    assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)
    assert raised.value.data is None
    assert raised.value.__context__ is None
    if outcome is not None:
        (record,) = caplog.records
        assert "the caller's deadline passed" in record.getMessage()
        assert f"the {outcome.kind} error" in record.getMessage()
        assert scope.context.trace_id in record.getMessage()


# What comes back late is timeout, whatever the rules for what arrives would
# have made of it in time (TRN-38 comes before them).
_ARRIVING: dict[str, tuple[Failure | bytes, str]] = {
    "an unknown kind with a detail": (
        Failure("moved", "moved", b'{"to":"x"}'),
        "moved",
    ),
    "a standard kind with a detail": (
        Failure("not_found", "no such desk", b'{"n":1}'),
        "internal",
    ),
    "a declared kind without its detail": (Failure("gone", "gone"), "internal"),
    "a reply that cannot be read": (b'{"n":"seven"}', "internal"),
}


class Answering:
    """A transport with no check of its own that takes a while to answer."""

    contract = DESK

    def __init__(self, clock: Clock, takes: float, answer: Failure | bytes) -> None:
        self._clock = clock
        self._takes = takes
        self._answer = answer

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        self._clock.time += self._takes
        if isinstance(self._answer, Failure):
            raise self._answer
        return self._answer


@pytest.mark.spec("TRN-38", "TRN-18", "CON-28", "CON-49")
@pytest.mark.parametrize("case", list(_ARRIVING), ids=list(_ARRIVING))
def test_a_late_outcome_is_timeout_whatever_it_would_have_been_in_time(
    clock: Clock, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    answer, in_time = _ARRIVING[case]
    with _context.root("shop"), caplog.at_level(logging.CRITICAL, "neolith"):
        for takes, kind in ((0.5, in_time), (1, "timeout")):
            connector: Any = Connector(Desk, Answering(clock, takes, answer))
            with pytest.raises(ContractError) as raised:
                connector.ask(Ask(), timeout=1)
            assert raised.value.kind == kind
    assert raised.value.message == _LATE
    assert raised.value.data is None


@pytest.mark.spec("TRN-38", "CON-48")
def test_a_nested_error_that_ends_an_action_late_is_timeout(clock: Clock) -> None:
    def calling(spent: float) -> Any:  # noqa: ANN401
        missing: Any = Connector(Desk, Slow(clock, spent, Failure("not_found", "x")))
        # The action lets the error of its own call end it.
        return _desk(lambda request, ctx: missing.ask(Ask()))[0]

    with _context.root("shop"):
        with pytest.raises(ContractError) as raised:
            calling(0.5).ask(Ask(), timeout=1)
        assert raised.value.kind == "internal"
        with pytest.raises(ContractError) as raised:
            calling(1).ask(Ask(), timeout=1)
    assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)


@pytest.mark.spec("TRN-38", "TRN-27")
def test_the_callers_deadline_is_counted_from_when_the_connector_is_called(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _crossing.check_request

    def slow_check(contract: Contract, action: Action, request: object) -> bytes:
        # The caller's own check of the request takes a quarter of a second.
        clock.time += 0.25
        return check(contract, action, request)

    monkeypatch.setattr(_connector, "check_request", slow_check)
    # With the check, the call is back 1.125 s after it was asked for: past
    # its timeout of one second, though within a second of the budget's
    # reading.
    late: Any = Connector(Desk, Slow(clock, 0.875, None))
    in_time: Any = Connector(Desk, Slow(clock, 0.5, None))
    with _context.root("shop"):
        assert in_time.ask(Ask(), timeout=1) == Told(n=7)
        with pytest.raises(ContractError) as raised:
            late.ask(Ask(), timeout=1)
    assert (raised.value.kind, raised.value.message) == ("timeout", _LATE)


@pytest.mark.spec("TRN-29", "TRN-38")
def test_the_callers_deadline_is_capped_with_the_budget(clock: Clock) -> None:
    scope = Scope(
        CallContext(None, "1" * 32, "2" * 16, "", None, None, None, "3" * 32), None
    )
    clock.time = 0.0
    assert scope.budget(1e12, 0.0) == (9_999_999_999, 9_999_999.999)
    assert scope.budget(5.0, 0.0) == (5000, 5.0)
    assert scope.budget(None, 0.0) == (None, None)
    held = Scope(
        CallContext(None, "1" * 32, "2" * 16, "", 2e10, None, None, "3" * 32), None
    )
    assert held.budget(None, 0.0) == (9_999_999_999, 9_999_999.999)
    assert held.budget(3.0, 0.0) == (3000, 3.0)


@pytest.mark.spec("TRN-20")
@pytest.mark.parametrize("ending", list(_ENDINGS), ids=list(_ENDINGS))
def test_an_outcome_just_inside_the_deadline_is_delivered(
    clock: Clock, caplog: pytest.LogCaptureFixture, ending: str
) -> None:
    def then(request: Ask, ctx: CallContext) -> object:
        clock.time += 1.9375
        made = _ENDINGS[ending]()
        if isinstance(made, BaseException):
            raise made
        return made

    connector, _, _ = _desk(then)
    kinds = {
        "a reply": None,
        "a standard error": "not_found",
        "a declared error": "gone",
    }
    with _context.root("shop"), caplog.at_level(logging.CRITICAL, "neolith"):
        if ending == "a reply":
            assert connector.ask(Ask(), timeout=2) == Told(n=1)
            return
        with pytest.raises(ContractError) as raised:
            connector.ask(Ask(), timeout=2)
    assert raised.value.kind == kinds.get(ending, "internal")


@pytest.mark.spec("TRN-21", "CON-45")
@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_a_signal_to_stop_after_the_deadline_is_not_discarded(
    clock: Clock, stop: type[BaseException]
) -> None:
    def then(request: Ask, ctx: CallContext) -> None:
        clock.time += 60
        raise stop()

    connector, _, _ = _desk(then)
    with _context.root("shop") as scope:
        with pytest.raises(stop):
            connector.ask(Ask(), timeout=1)
        assert list(_context._OPEN) == [scope.context.scope_id]


@pytest.mark.spec("TRN-20", "TRN-28")
def test_calls_an_action_makes_after_its_deadline_fail_with_timeout(
    clock: Clock,
) -> None:
    inner, inner_provider, inner_carrying = _desk()
    caught: list[ContractError] = []

    def then(request: Ask, ctx: CallContext) -> None:
        inner.ask(Ask(n=1))
        clock.time += 3
        try:
            inner.ask(Ask(n=2))
        except ContractError as error:
            caught.append(error)
            raise

    outer, _, _ = _desk(then)
    with _context.root("shop"), pytest.raises(ContractError) as raised:
        outer.ask(Ask(), timeout=3)
    # The nested call was not sent, and the outer call is late too.
    assert [error.kind for error in caught] == ["timeout"]
    assert len(inner_carrying.sent) == 1
    assert len(inner_provider.seen) == 1
    assert raised.value.kind == "timeout"
    assert raised.value is not caught[0]


@pytest.mark.spec("TRN-4", "TRN-20")
@_SETTINGS
@given(
    budget=st.integers(1, 20),
    spent=st.integers(0, 40),
    root=st.none() | st.integers(1, 20),
    raises=st.booleans(),
)
def test_no_outcome_reaches_the_caller_after_the_deadline(
    budget: int, spent: int, root: int | None, raises: bool
) -> None:
    clock = Clock()
    # Halves of a second are exact, so the comparison with the deadline is.
    limit = budget if root is None else min(budget, root)

    def then(request: Ask, ctx: CallContext) -> None:
        clock.time += spent / 2
        if raises:
            raise ContractError("not_found", "no such desk")

    connector, provider, _ = _desk(then)
    with mock.patch.object(_context, "now", clock), _context.root("shop", timeout=root):
        try:
            reply = connector.ask(Ask(), timeout=budget)
        except ContractError as error:
            outcome = error.kind
        else:
            assert reply == Told()
            outcome = "reply"
    assert len(provider.seen) == 1
    if spent / 2 >= limit:
        assert outcome == "timeout"
    else:
        assert outcome == ("not_found" if raises else "reply")


# The idempotency key (TRN-32, TRN-33).


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _key(parent: str, contract_id: str, name: str, request: str, count: int) -> str:
    """ADR 0017's derivation, written with the json module for ASCII values."""
    request_hash = _b64(hashlib.sha256(request.encode()).digest())
    text = json.dumps(
        [parent, contract_id, name, request_hash, count], separators=(",", ":")
    )
    return _b64(hashlib.sha256(text.encode()).digest())


def _children(
    calls: Callable[[Any, CallContext], None], sent: Sent | None = None
) -> list[tuple[bytes, str]]:
    """Each request and key of the calls an action makes, run once as ``sent``."""
    inner, _, carrying = _desk()
    provider = Seeing(lambda request, ctx: calls(inner, ctx))
    InProcess(Desk, provider, app="desk").call(ASK, b"{}", sent or _sent())
    return [
        (request, context.idempotency_key)
        for request, context in zip(carrying.requests, carrying.sent, strict=True)
    ]


@pytest.mark.spec("TRN-32")
def test_a_childs_key_is_derived_from_its_parents() -> None:
    def calls(inner: Any, ctx: CallContext) -> None:  # noqa: ANN401
        inner.ask(Ask(n=1))
        inner.ask(Ask(n=1))
        inner.ask(Ask(n=2))
        inner.tell(Ask(n=1))
        inner.ask(Ask(n=1))

    made = _children(calls)
    one, two = '{"n":1,"x":0.0}', '{"n":2,"x":0.0}'
    assert [request.decode() for request, _ in made] == [one, one, two, one, one]
    # The request is hashed in its canonical form, where 0.0 is written 0.
    assert [key for _, key in made] == [
        _key("parent-key", "desk.v1", "ask", '{"n":1,"x":0}', 0),
        _key("parent-key", "desk.v1", "ask", '{"n":1,"x":0}', 1),
        _key("parent-key", "desk.v1", "ask", '{"n":2,"x":0}', 0),
        _key("parent-key", "desk.v1", "tell", '{"n":1,"x":0}', 0),
        _key("parent-key", "desk.v1", "ask", '{"n":1,"x":0}', 2),
    ]


@contract("inner.v1")
class Inner(Protocol):
    @action
    def ask(self, request: Gone) -> None: ...


@pytest.mark.spec("TRN-32")
def test_a_derived_key_is_the_one_node_derives() -> None:
    # The keys Node.js gives for the parent key "parent-key", the contract
    # "inner.v1", the action "ask" and the requests {"n":1}, twice, and {"n":2}.
    class Keeping:
        contract = Contract.of(Inner)
        keys: list[str] = []  # noqa: RUF012

        def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
            self.keys.append(context.idempotency_key)
            return None

    inner: Any = Connector(Inner, Keeping())

    def then(request: Ask, ctx: CallContext) -> None:
        for n in (1, 1, 2):
            inner.ask(Gone(n=n))

    InProcess(Desk, Seeing(then)).call(ASK, b"{}", _sent())
    assert Keeping.keys == [
        "yYarvmlgPLZ_l-4R8NOtDgoat1r_TaAgA25SRncxskk",
        "7jgALsjqFQLbHmYFAexPE5jR2be4TwxBUcP-lWxLD0g",
        "-jbMvBSF_CpoZKKJf9cQs-XGJWihywiSBNv_j2c0YOM",
    ]


@pytest.mark.spec("TRN-32")
def test_requests_that_travel_as_one_value_share_a_count() -> None:
    def calls(inner: Any, ctx: CallContext) -> None:  # noqa: ANN401
        inner.ask(Ask(n=1, x=0.0))
        inner.ask(Ask(n=1, x=-0.0))
        inner.ask(Ask(x=0, n=1))

    keys = [key for _, key in _children(calls)]
    assert keys == [
        _key("parent-key", "desk.v1", "ask", '{"n":1,"x":0}', count)
        for count in range(3)
    ]


@pytest.mark.spec("TRN-32", "TRN-34")
def test_only_a_call_that_passes_the_callers_checks_counts(clock: Clock) -> None:
    def calls(inner: Any, ctx: CallContext) -> None:  # noqa: ANN401
        inner.ask(Ask(n=1))
        # Refused by the request check: not counted.
        for wrong in (Told(n=1), {"n": 1}, Ask(n=2**60)):
            with pytest.raises((TypeError, ValueError)):
                inner.ask(wrong)
        with pytest.raises(TypeError):
            inner.ask(Ask(n=1), timeout="soon")
        with pytest.raises(RuntimeError):
            inner.ask(Ask(n=1), context=dataclasses.replace(ctx, scope_id="none"))
        # Passed the checks and then found late: counted, though never sent.
        with pytest.raises(ContractError, match="timeout"):
            inner.ask(Ask(n=1), timeout=0)
        inner.ask(Ask(n=1))

    assert [key for _, key in _children(calls)] == [
        _key("parent-key", "desk.v1", "ask", '{"n":1,"x":0}', 0),
        _key("parent-key", "desk.v1", "ask", '{"n":1,"x":0}', 2),
    ]


@pytest.mark.spec("TRN-32")
def test_a_retried_parent_reproduces_its_childrens_keys_under_fan_out() -> None:
    requests = [Ask(n=n % 5) for n in range(60)]

    def calls(inner: Any, ctx: CallContext) -> None:  # noqa: ANN401
        # Each task runs in a worker with no context of its own, and names
        # the action's; some go through the ambient scope instead.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(inner.ask, request, context=ctx)
                for request in requests[:40]
            ]
            futures += [
                pool.submit(contextvars.copy_context().run, inner.ask, request)
                for request in requests[40:]
            ]
            for future in futures:
                future.result()

    first, second = Counter(_children(calls)), Counter(_children(calls))
    assert first == second
    assert sum(first.values()) == len(first) == 60
    # The keys are those of the same calls made one after another.
    expected = Counter(
        (
            f'{{"n":{n},"x":0.0}}'.encode(),
            _key("parent-key", "desk.v1", "ask", f'{{"n":{n},"x":0}}', count),
        )
        for n in range(5)
        for count in range(12)
    )
    assert first == expected
    # Another parent key, or another parent, gives other keys.
    other = Counter(_children(calls, _sent(idempotency_key="another-key")))
    assert set(other).isdisjoint(first)


@pytest.mark.spec("TRN-33")
def test_a_parent_with_no_key_gives_its_children_random_keys() -> None:
    connector, _, carrying = _desk()
    with _context.root("shop") as scope:
        assert scope.context.idempotency_key is None
        for _ in range(200):
            connector.ask(Ask())
    keys = [sent.idempotency_key for sent in carrying.sent]
    assert len(set(keys)) == 200
    for key in keys:
        # 128 bits, base64url without padding.
        assert len(key) == 22
        assert len(base64.urlsafe_b64decode(key + "==")) == 16


@pytest.mark.spec("TRN-33")
def test_a_random_key_comes_from_the_secure_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def token_bytes(size: int) -> bytes:
        asked.append(size)
        return bytes(range(size))

    scope = Scope(
        CallContext(None, "1" * 32, "2" * 16, "", None, None, None, "3" * 32), None
    )
    monkeypatch.setattr(secrets, "token_bytes", token_bytes)
    assert scope.child_key("desk.v1", "ask", b"{}") == _b64(bytes(range(16)))
    assert asked == [16]


# The caller's order (TRN-34) and the principal (CON-51).


@pytest.mark.spec("TRN-34", "CON-29")
def test_the_context_is_checked_before_the_request() -> None:
    connector, _, carrying = _desk()
    with pytest.raises(RuntimeError, match="needs a call context"):
        connector.ask("not a request", timeout=0)
    with _context.root("shop") as scope:
        pass
    with pytest.raises(RuntimeError, match="names no open scope"):
        connector.ask("not a request", context=scope.context)
    assert carrying.sent == []


@pytest.mark.spec("TRN-34", "CON-51")
def test_the_principal_is_checked_before_the_request_and_the_deadline(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    connector, _, carrying = _desk()
    with (
        _context.root("shop", principal=Principal("i", ""), timeout=1),
        caplog.at_level(logging.CRITICAL, "neolith"),
    ):
        clock.time += 5
        for request in ("not a request", Ask()):
            with pytest.raises(ContractError) as raised:
                connector.ask(request)
            assert raised.value.kind == "internal"
    assert carrying.sent == []


@pytest.mark.spec("TRN-34", "TRN-14")
def test_the_request_is_checked_before_the_deadline(clock: Clock) -> None:
    connector, _, carrying = _desk()
    with _context.root("shop", timeout=1):
        clock.time += 5
        with pytest.raises(TypeError, match="the request"):
            connector.ask("not a request")
        with pytest.raises(TypeError, match="the request"):
            connector.ask(Told(), timeout=0)
        with pytest.raises(ContractError, match="timeout"):
            connector.ask(Ask())
    assert carrying.sent == []


_FITS = 3072 - len(b'{"issuer":"i","subject":""}')


@pytest.mark.spec("CON-51", "CON-46")
@pytest.mark.parametrize(
    "principal",
    [
        Principal("i", ""),
        Principal("", "s"),
        Principal("i", "s\ud800"),
        Principal("i", "s", {"a": ["\udfff"]}),
        Principal("i", "s", {"a": 2**53}),
        Principal("i", "s" * (_FITS + 1)),
    ],
    ids=["empty subject", "empty issuer", "surrogate", "in a list", "integer", "4097"],
)
def test_a_call_whose_principal_is_not_valid_fails_at_the_caller_with_internal(
    principal: Principal, caplog: pytest.LogCaptureFixture
) -> None:
    connector, provider, carrying = _desk()
    with (
        _context.root("shop", principal=principal) as scope,
        caplog.at_level(logging.ERROR, "neolith"),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask())
    trace = scope.context.trace_id
    error = raised.value
    # Fixed text and the trace identifier, and nothing of the cause.
    assert (error.kind, error.message, error.data) == (
        "internal",
        internal_message(trace),
        None,
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    assert type(error) is ContractError
    assert is_received(error)
    assert carrying.sent == []
    assert provider.seen == []
    # The caller's host records the cause under the trace identifier.
    (record,) = caplog.records
    assert trace in record.getMessage()
    assert "principal is not valid" in record.getMessage()
    assert record.exc_info is not None
    assert type(record.exc_info[1]) is ValueError


@pytest.mark.spec("CON-51", "TRN-30", "TRN-36")
@pytest.mark.parametrize(
    "principal",
    [
        Principal("i", "s" * _FITS),
        Principal("i￿", "￿", {"￿": ["￿"]}),
        Principal("i", "s", {"a": 2**53 - 1, "b": True, "c": [], "d": ""}),
    ],
    ids=["exactly 4096", "U+FFFF", "every kind of claim"],
)
def test_a_valid_principal_reaches_the_provider_read_from_its_sent_form(
    principal: Principal,
) -> None:
    connector, provider, carrying = _desk()
    with _context.root("shop", principal=principal):
        connector.ask(Ask())
    (ctx,) = provider.seen
    assert ctx.principal == principal
    assert ctx.principal is not principal
    form = carrying.sent[0].principal
    assert form is not None
    assert read_principal(form) == principal


@pytest.mark.spec("CON-51", "TRN-30")
def test_a_call_with_no_principal_carries_none() -> None:
    inner, inner_provider, inner_carrying = _desk()
    outer, outer_provider, _ = _desk(lambda request, ctx: inner.ask(Ask()))
    with _context.root("shop"):
        outer.ask(Ask())
    assert outer_provider.seen[0].principal is None
    assert inner_provider.seen[0].principal is None
    assert inner_carrying.sent[0].principal is None


@pytest.mark.spec("CON-51")
def test_a_principal_is_judged_once_for_its_scope(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    judged: list[Principal] = []
    judge = _context.principal_form

    def counting(principal: Principal) -> bytes:
        judged.append(principal)
        return judge(principal)

    monkeypatch.setattr(_context, "principal_form", counting)
    connector, provider, _ = _desk()
    bad = Principal("i", "")
    with caplog.at_level(logging.CRITICAL, "neolith"):
        with (
            _context.root("shop", principal=ALICE) as scope,
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            list(
                pool.map(
                    lambda n: connector.ask(Ask(n=n), context=scope.context),
                    range(20),
                )
            )
        with _context.root("shop", principal=bad):
            for _ in range(3):
                with pytest.raises(ContractError, match="internal"):
                    connector.ask(Ask())
    assert judged == [ALICE, bad]
    assert len(provider.seen) == 20


@pytest.mark.spec("CON-46", "CON-47")
def test_an_internal_error_carries_the_trace_identifier(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def then(request: Ask, ctx: CallContext) -> None:
        raise RuntimeError("s3cret")

    connector, _, _ = _desk(then)
    unreadable: Any = Connector(Desk, Carrying(reply=b"not json"))
    with _context.root("shop") as scope, caplog.at_level(logging.ERROR, "neolith"):
        trace = scope.context.trace_id
        for call in (connector.ask, unreadable.ask):
            with pytest.raises(ContractError) as raised:
                call(Ask())
            assert raised.value.kind == "internal"
            assert raised.value.message == internal_message(trace)
            assert raised.value.message == (
                "the call failed inside a host, which logged the cause under "
                f"trace {trace}"
            )
    # Each host that failed logged the cause under that identifier.
    assert len(caplog.records) == 2
    assert all(trace in record.getMessage() for record in caplog.records)


# Invariant: the context crosses intact (TRN-2).

_NAMES = st.text(st.characters(min_codepoint=97, max_codepoint=122), max_size=5)
_TEXT = st.text(st.characters(exclude_categories=["Cs"]), min_size=1, max_size=5)
_CLAIMS = st.dictionaries(
    _TEXT,
    _TEXT
    | st.booleans()
    | st.integers(-(2**53) + 1, 2**53 - 1)
    | st.lists(_TEXT, max_size=3),
    max_size=3,
)
_PRINCIPALS = st.none() | st.builds(Principal, _TEXT, _TEXT, _CLAIMS)


@pytest.mark.spec("TRN-2", "TRN-30", "TRN-31", "TRN-36", "TRN-37")
@_SETTINGS
@given(
    principal=_PRINCIPALS,
    app=st.none() | _NAMES,
    provider_app=st.none() | _NAMES,
    root=st.none() | st.integers(1, 100),
    own=st.none() | st.integers(1, 100),
    depth=st.integers(1, 3),
)
def test_the_context_an_action_receives_equals_the_one_its_caller_derived(
    principal: Principal | None,
    app: str | None,
    provider_app: str | None,
    root: int | None,
    own: int | None,
    depth: int,
) -> None:
    # A chain of calls: each action calls the next connector, at a depth.
    providers: list[Seeing] = []
    carried: list[Carrying] = []
    connectors: list[Any] = []
    for level in range(depth):

        def then(request: Ask, ctx: CallContext, level: int = level) -> None:
            if level + 1 < depth:
                connectors[level + 1].ask(Ask(n=level + 1))

        connector, provider, carrying = _desk(then, app=provider_app)
        connectors.append(connector)
        providers.append(provider)
        carried.append(carrying)
    with _context.root(app, principal=principal, timeout=root) as scope:
        connectors[0].ask(Ask(), timeout=own)
    deadlines = [scope.context.deadline]
    for level in range(depth):
        (ctx,) = providers[level].seen
        (sent,) = carried[level].sent
        derived = CallContext(
            principal=None
            if sent.principal is None
            else read_principal(sent.principal),
            trace_id=sent.trace_id,
            span_id="",
            tracestate=sent.tracestate,
            deadline=None if sent.budget is None else 0.0,
            idempotency_key=sent.idempotency_key,
            caller=sent.caller,
            scope_id="",
        )
        assert ctx == derived
        assert hash(ctx) == hash(derived)
        # And what the caller derived is what its parent scope held.
        assert ctx.principal == principal
        assert ctx.trace_id == scope.context.trace_id
        assert ctx.caller == (app if level == 0 else provider_app)
        assert (ctx.deadline is None) == (root is None and own is None)
        if ctx.deadline is not None and deadlines[-1] is not None:
            assert ctx.deadline <= deadlines[-1]
        deadlines.append(ctx.deadline)
    assert _context._OPEN == {}


# Concurrent callers of one scope.


@pytest.mark.spec("TRN-36", "TRN-6")
def test_concurrent_callers_each_run_in_a_scope_of_their_own() -> None:
    barrier = threading.Barrier(8)
    scopes: list[tuple[int, str, int]] = []

    def then(request: Ask, ctx: CallContext) -> None:
        barrier.wait(timeout=30)
        ambient = _context._CURRENT.get()
        assert ambient is not None
        assert ambient.context is ctx
        scopes.append((request.n, ctx.scope_id, threading.get_ident()))

    connector, provider, _ = _desk(then)
    with _context.root("shop", principal=ALICE) as scope:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(
                pool.map(
                    lambda n: connector.ask(Ask(n=n), context=scope.context), range(8)
                )
            )
        assert list(_context._OPEN) == [scope.context.scope_id]
    assert replies == [Told(n=n) for n in range(8)]
    assert len({scope_id for _, scope_id, _ in scopes}) == 8
    assert len({thread for _, _, thread in scopes}) == 8
    assert {ctx.trace_id for ctx in provider.seen} == {scope.context.trace_id}


# The value (ADR 0017 Decision 1).


def _context_value(**changes: Any) -> CallContext:  # noqa: ANN401
    fields: dict[str, Any] = {
        "principal": ALICE,
        "trace_id": "1" * 32,
        "span_id": "2" * 16,
        "tracestate": "",
        "deadline": 5.0,
        "idempotency_key": "key",
        "caller": "shop",
        "scope_id": "3" * 32,
    }
    return CallContext(**{**fields, **changes})


@pytest.mark.parametrize(
    ("changes", "equal"),
    [
        ({"span_id": "4" * 16}, True),
        ({"tracestate": "vendor=1"}, True),
        ({"deadline": 99.0}, True),
        ({"scope_id": "5" * 32}, True),
        ({"principal": Principal(ALICE.issuer, ALICE.subject, ALICE.claims)}, True),
        ({"deadline": None}, False),
        ({"principal": None}, False),
        ({"principal": BOB}, False),
        ({"trace_id": "6" * 32}, False),
        ({"idempotency_key": None}, False),
        ({"idempotency_key": "other"}, False),
        ({"caller": None}, False),
        ({"caller": "desk"}, False),
    ],
    ids=repr,
)
def test_contexts_are_equal_as_the_record_compares_them(
    changes: dict[str, Any], equal: bool
) -> None:
    one, other = _context_value(), _context_value(**changes)
    assert (one == other) is equal
    assert (one != other) is not equal
    if equal:
        assert hash(one) == hash(other)
    assert one != dataclasses.astuple(one)


def test_a_context_is_frozen_and_copies_as_an_equal_value() -> None:
    value = _context_value()
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.deadline = None  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        value.more = 1  # type: ignore[attr-defined]
    assert not hasattr(value, "__dict__")
    for made in (
        copy.copy(value),
        copy.deepcopy(value),
        pickle.loads(pickle.dumps(value)),  # noqa: S301
        dataclasses.replace(value),
    ):
        assert made == value
        assert made.scope_id == value.scope_id
        assert made.principal == ALICE
    assert dataclasses.asdict(value)["principal"] == ALICE
    assert {value: 1}[_context_value(span_id="9" * 16)] == 1
    # The scope id names a scope on this host, so it stays out of the repr.
    assert value.scope_id not in repr(value)
    assert "alice" in repr(value)


# The two shapes of an implementation (ADR 0017 Decision 2).


def test_an_implementation_takes_the_context_under_any_name() -> None:
    @implements(Desk)
    class Quoted:
        def ask(self, request: Ask, context: "CallContext") -> Told:
            return Told(n=1 if context.caller == "shop" else 0)

        def tell(self, request: Ask, call: neolith.CallContext) -> None:
            raise ContractError("not_found", str(call.caller))

    connector: Any = Connector(Desk, InProcess(Desk, Quoted()))
    with _context.root("shop"):
        assert connector.ask(Ask()) == Told(n=1)
        with pytest.raises(ContractError, match="not_found: shop"):
            connector.tell(Ask())


def _taking(signature: str) -> type[Any]:
    namespace: dict[str, Any] = {
        "Ask": Ask,
        "Told": Told,
        "CallContext": CallContext,
        "Any": Any,
    }
    exec(  # noqa: S102
        "class Made:\n"
        f"    def ask({signature}) -> Told: ...\n"
        "    def tell(self, request: Ask) -> None: ...\n",
        namespace,
    )
    return cast("type[Any]", namespace["Made"])


@pytest.mark.spec("CON-19")
@pytest.mark.parametrize(
    "signature",
    [
        "self, request: Ask, ctx",
        "self, request: Ask, ctx: Any",
        "self, request: Ask, ctx: object",
        "self, request: Ask, ctx: CallContext | None",
        "self, request: Ask, ctx: 'Told'",
        "self, request: Ask, ctx: type[CallContext]",
        "self, request: Ask, ctx: CallContext = None",
        "self, request: Ask, *, ctx: CallContext",
        "self, request: Ask, *ctx: CallContext",
        "self, request: Ask, **ctx: CallContext",
        "self, request: Ask, ctx: CallContext, more: CallContext",
        "self, ctx: CallContext, request: Ask",
        "self, ctx: CallContext",
        "self",
    ],
)
def test_an_implementation_of_any_other_shape_is_refused(signature: str) -> None:
    made = _taking(signature)
    with pytest.raises(ContractDefinitionError, match="action 'ask'") as raised:
        implements(Desk)(made)
    assert "desk.v1, implementation Made" in str(raised.value)
    with pytest.raises(ContractDefinitionError, match="action 'ask'"):
        InProcess(Desk, made())
    # The refusal's advice leads to a shape that is admitted.
    if "ctx: CallContext" in str(raised.value):
        implements(Desk)(_taking("self, request: Ask, ctx: CallContext"))
    implements(Desk)(_taking("self, request: Ask"))


@pytest.mark.spec("CON-17")
def test_a_declaration_does_not_take_the_context() -> None:
    with pytest.raises(
        ContractDefinitionError, match=r"exactly \(self, request\), not"
    ):

        @contract("takes.v1")
        class Takes(Protocol):
            @action
            def ask(self, request: Ask, ctx: CallContext) -> Told: ...


def _scopes() -> Iterator[Scope]:
    yield from _context._OPEN.values()


def test_no_scope_is_left_open_by_this_module() -> None:
    assert list(_scopes()) == []
    assert _context._CURRENT.get() is None
