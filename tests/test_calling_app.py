"""The calling app, on both sides of an in-process call (spec 02, The calling app).

The caller refuses a call outside its app's ``requires`` before it resolves
the principal (ADR 0021 Decision 9, step 1a). The provider's side refuses a
call from no app with ``unauthenticated``, and a principal from a calling app
not on the contract's allow-list with ``permission_denied``, before it checks
the deadline or reads the request (ADR 0017 Decisions 6 and 9, ADR 0010
Decision 7). Time is a clock the tests set, never a sleep.
"""

import itertools
from collections.abc import Callable
from typing import Any, Protocol

import pytest
from django.apps import AppConfig
from django.apps import apps as global_apps
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from neolith import (
    CallContext,
    Contract,
    ContractError,
    Principal,
    UsageError,
    _context,
    action,
    connect,
    contract,
)
from neolith._connector import Connector, Failure, InProcess
from neolith._context import Sent
from neolith._contracts import Action
from neolith._declaration import declaration_of
from neolith._host import _current
from tests import host_apps
from tests.declaration_contracts import Level, Sku
from tests.host_apps.couriers import services as courier_services
from tests.host_apps.couriers.apps import CouriersConfig
from tests.host_apps.stock.apps import StockConfig
from tests.host_apps.web.apps import WebConfig

ALICE = Principal("https://id.example", "alice")


class Ask(BaseModel):
    n: int = 0


class Told(BaseModel):
    n: int = 0


@contract("front.v1")
class Front(Protocol):
    @action(idempotent=True)
    def ask(self, request: Ask) -> Told: ...


FRONT = Contract.of(Front)
ASK = FRONT.actions["ask"]


class Recording:
    """An implementation that keeps each context it is handed, and may call on."""

    def __init__(self, then: Callable[[Ask, CallContext], None] | None = None) -> None:
        self.seen: list[CallContext] = []
        self._then = then

    def ask(self, request: Ask, ctx: CallContext) -> Told:
        self.seen.append(ctx)
        if self._then is not None:
            self._then(request, ctx)
        return Told(n=request.n)


def _front(
    principal_from: tuple[str, ...] = (),
    *,
    then: Callable[[Ask, CallContext], None] | None = None,
    requires: tuple[str, ...] | None = None,
) -> tuple[Any, Recording]:
    provider = Recording(then)
    transport = InProcess(
        Front, provider, app="front", requires=requires, principal_from=principal_from
    )
    return Connector(Front, transport), provider


def _caller(
    app: str | None = "shop",
    requires: tuple[str, ...] | None = ("front.v1",),
    **given: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    return _context.root(app, requires=requires, **given)


class Clock:
    """A clock the test sets: each reading returns ``time``."""

    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    made = Clock()
    monkeypatch.setattr(_context, "now", made)
    return made


# The caller: step 1a, a call outside the calling app's requires.


@pytest.mark.spec("TRN-39")
def test_a_call_from_an_app_that_requires_the_contract_is_sent() -> None:
    front, provider = _front()
    with _caller():
        assert front.ask(Ask(n=3)) == Told(n=3)
    assert [ctx.caller for ctx in provider.seen] == ["shop"]


@pytest.mark.spec("TRN-39", "CON-29")
@pytest.mark.parametrize("requires", [(), ("other.v1", "desk.v2")])
def test_a_call_outside_the_calling_apps_requires_is_a_usage_error(
    requires: tuple[str, ...],
) -> None:
    front, provider = _front()
    with _caller(requires=requires), pytest.raises(UsageError) as raised:
        front.ask(Ask())
    message = str(raised.value)
    assert message.startswith(
        "app shop calls front.v1, which its declaration does not require; it "
        f"requires {', '.join(sorted(requires)) or 'no contract'}."
    )
    assert message.endswith('opens neolith.command_scope("<its label>")')
    assert isinstance(raised.value, RuntimeError)
    assert ContractError not in type(raised.value).__mro__
    assert provider.seen == []


@pytest.mark.spec("TRN-39", "CON-29")
def test_an_app_with_no_declaration_requires_nothing() -> None:
    front, provider = _front()
    with _caller(requires=None), pytest.raises(UsageError) as raised:
        front.ask(Ask())
    message = str(raised.value)
    assert message.startswith(
        "app shop calls front.v1, but it has no declaration, so it requires nothing."
    )
    assert "name, label and requires" in message
    assert provider.seen == []


@pytest.mark.spec("TRN-39", "TRN-42")
def test_a_scope_of_no_app_passes_the_caller_and_is_refused_by_the_provider() -> None:
    front, provider = _front()
    with _caller(app=None, requires=None), pytest.raises(ContractError) as raised:
        front.ask(Ask())
    assert raised.value.kind == "unauthenticated"
    assert provider.seen == []


@pytest.mark.spec("TRN-39", "TRN-22")
def test_the_scope_named_by_context_decides_the_app_and_its_requires() -> None:
    front, provider = _front()
    refusal = r"^app clerk calls front\.v1, which its declaration does not require"
    with _caller() as allowed, _caller("clerk", requires=()):
        # Made in the clerk's scope, a call naming the shop's is the shop's.
        assert front.ask(Ask(n=1), context=allowed.context) == Told(n=1)
        with pytest.raises(UsageError, match=refusal):
            front.ask(Ask(n=2))
    with _caller("clerk", requires=()) as refused, _caller():
        # Made in the shop's scope, a call naming the clerk's is the clerk's.
        with pytest.raises(UsageError, match=refusal):
            front.ask(Ask(n=3), context=refused.context)
        assert front.ask(Ask(n=4)) == Told(n=4)
    assert [ctx.caller for ctx in provider.seen] == ["shop", "shop"]


@pytest.mark.spec("TRN-39", "TRN-43")
@pytest.mark.parametrize("own_requires", [False, True])
def test_a_providing_app_calls_its_own_contract_only_when_it_requires_it(
    own_requires: bool,
) -> None:
    outcomes: list[object] = []
    holder: list[Any] = []

    def again(request: Ask, ctx: CallContext) -> None:
        if request.n > 0:
            try:
                outcomes.append(holder[0].ask(Ask(n=request.n - 1)))
            except Exception as error:  # the test reads it
                outcomes.append(error)

    front, _ = _front(then=again, requires=("front.v1",) if own_requires else ())
    holder.append(front)
    with _caller():
        front.ask(Ask(n=1))
    (outcome,) = outcomes
    if own_requires:
        assert outcome == Told(n=0)
    else:
        assert type(outcome) is UsageError
        assert str(outcome).startswith("app front calls front.v1")


# The caller: step 1a comes before the principal and the request.


@pytest.mark.spec("TRN-40")
def test_requires_is_checked_before_the_principal_is_resolved() -> None:
    front, provider = _front(principal_from=("shop",))
    bad = Principal("https://id.example", "")
    with _caller(requires=(), principal=bad) as scope:
        with pytest.raises(UsageError):
            front.ask(Ask())
        assert scope._form is None
    assert provider.seen == []


@pytest.mark.spec("TRN-40", "TRN-34")
def test_requires_is_checked_before_the_request() -> None:
    front, provider = _front()
    with _caller(requires=()), pytest.raises(UsageError):
        front.ask(Told())
    with _caller(), pytest.raises(TypeError):
        front.ask(Told())
    assert provider.seen == []


@pytest.mark.spec("TRN-40", "TRN-32")
def test_a_refused_call_does_not_count_as_made() -> None:
    front, provider = _front()
    with _context.opened(
        principal=None,
        trace_id="0af7651916cd43dd8448eb211c80319c",
        tracestate="",
        deadline=None,
        idempotency_key="parent-key",
        caller=None,
        app="shop",
        requires=frozenset(),
    ) as scope:
        for _ in range(2):
            with pytest.raises(UsageError):
                front.ask(Ask())
        assert scope._counts == {}
    assert provider.seen == []


@pytest.mark.spec("TRN-23")
def test_a_missing_or_closed_scope_is_a_usage_error() -> None:
    front, _ = _front()
    with pytest.raises(UsageError, match="none is active here"):
        front.ask(Ask())
    with _caller() as scope:
        pass
    with pytest.raises(UsageError, match="names no open scope"):
        front.ask(Ask(), context=scope.context)


# The provider: a call from no app, and a principal from an unlisted caller.


@pytest.mark.spec("TRN-42")
@pytest.mark.parametrize("principal", [None, ALICE], ids=["none", "alice"])
def test_a_call_from_no_app_is_unauthenticated_and_its_action_never_runs(
    principal: Principal | None,
) -> None:
    front, provider = _front(principal_from=("shop",))
    with (
        _caller(app=None, requires=None, principal=principal),
        pytest.raises(ContractError) as raised,
    ):
        front.ask(Ask())
    assert raised.value.kind == "unauthenticated"
    assert raised.value.message == (
        "a call to front.v1 has no calling app; a call is made from a scope "
        "that belongs to an app"
    )
    assert provider.seen == []


def _sent(**changes: Any) -> Sent:  # noqa: ANN401
    fields: dict[str, Any] = {
        "principal": None,
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "tracestate": "",
        "budget": None,
        "idempotency_key": "parent-key",
        "caller": "shop",
    }
    fields.update(changes)
    return Sent(**fields)


# What the provider's later steps would refuse: a principal that does not
# read, a deadline that has passed when the call arrives, and a request that
# does not read. Each arrives alone and with the others.
_LATER: dict[str, dict[str, Any]] = {
    "principal": {"principal": b"\xff not a principal"},
    "deadline": {"budget": 0},
    "request": {},
}


def _later(names: tuple[str, ...]) -> tuple[dict[str, Any], bytes]:
    changes: dict[str, Any] = {}
    for name in names:
        changes.update(_LATER[name])
    return changes, b"not json" if "request" in names else b"{}"


_COMBINATIONS = [
    names
    for size in range(1, len(_LATER) + 1)
    for names in itertools.combinations(_LATER, size)
]


@pytest.mark.spec("TRN-42", "TRN-35")
@pytest.mark.parametrize("names", _COMBINATIONS, ids="+".join)
def test_unauthenticated_comes_before_the_providers_later_steps(
    clock: Clock, names: tuple[str, ...]
) -> None:
    provider = Recording()
    changes, request = _later(names)
    with pytest.raises(Failure) as raised:
        InProcess(Front, provider, principal_from=("shop",)).call(
            ASK, request, _sent(caller=None, **changes)
        )
    assert raised.value.kind == "unauthenticated"
    assert provider.seen == []


@pytest.mark.spec("TRN-43", "TRN-35")
@pytest.mark.parametrize("names", _COMBINATIONS, ids="+".join)
def test_permission_denied_comes_before_the_providers_later_steps(
    clock: Clock, names: tuple[str, ...]
) -> None:
    provider = Recording()
    changes, request = _later(names)
    changes.setdefault("principal", b'{"issuer":"i","subject":"s"}')
    with pytest.raises(Failure) as raised:
        InProcess(Front, provider, principal_from=("other",)).call(
            ASK, request, _sent(**changes)
        )
    assert raised.value.kind == "permission_denied"
    assert provider.seen == []


@pytest.mark.spec("TRN-43", "TRN-35")
@pytest.mark.parametrize("name", list(_LATER))
def test_a_listed_caller_reaches_the_later_step_that_refuses(
    clock: Clock, name: str
) -> None:
    provider = Recording()
    changes, request = _later((name,))
    changes.setdefault("principal", b'{"issuer":"i","subject":"s"}')
    with pytest.raises(Failure) as raised:
        InProcess(Front, provider, principal_from=("shop",)).call(
            ASK, request, _sent(**changes)
        )
    assert raised.value.kind == ("timeout" if name == "deadline" else "internal")
    assert provider.seen == []


# Who may assert a principal: every combination, then generated lists.

_CALLERS = ["listed", "unlisted", "front"]
_LISTS: list[tuple[str, ...]] = [(), ("listed",), ("listed", "other")]


@pytest.mark.spec("TRN-43", "TRN-42")
@pytest.mark.parametrize("principal", [None, ALICE], ids=["none", "alice"])
@pytest.mark.parametrize("allow", _LISTS, ids=repr)
@pytest.mark.parametrize("caller", _CALLERS)
def test_only_a_principal_from_an_unlisted_caller_is_refused(
    caller: str, allow: tuple[str, ...], principal: Principal | None
) -> None:
    front, provider = _front(principal_from=allow)
    refused = principal is not None and caller not in allow
    with _caller(caller, principal=principal):
        if refused:
            with pytest.raises(ContractError) as raised:
                front.ask(Ask())
            assert raised.value.kind == "permission_denied"
            assert raised.value.message == (
                f"app {caller} may not assert a principal on a call to front.v1: "
                "the contract's allow-list does not name it"
            )
            assert provider.seen == []
        else:
            assert front.ask(Ask(n=5)) == Told(n=5)
            (seen,) = provider.seen
            assert (seen.caller, seen.principal) == (caller, principal)


_NAMES = st.sampled_from(["a", "b", "shop", "shop_2", "front", "x1"])


@pytest.mark.spec("TRN-43")
@given(
    allow=st.lists(_NAMES, unique=True).map(tuple),
    caller=_NAMES,
    principal=st.booleans(),
)
def test_the_list_refuses_exactly_a_principal_from_a_caller_it_does_not_name(
    allow: tuple[str, ...], caller: str, principal: bool
) -> None:
    provider = Recording()
    form = b'{"issuer":"i","subject":"s"}' if principal else None
    transport = InProcess(Front, provider, principal_from=allow)
    try:
        transport.call(ASK, b"{}", _sent(caller=caller, principal=form))
    except Failure as failure:
        outcome: str | None = failure.kind
    else:
        outcome = None
    expected = "permission_denied" if principal and caller not in allow else None
    assert outcome == expected
    assert len(provider.seen) == (0 if expected else 1)


class _LateRefusal:
    """Passes a call on, and moves the clock past the caller's deadline."""

    def __init__(self, inner: InProcess, clock: Clock) -> None:
        self.contract = inner.contract
        self._inner = inner
        self._clock = clock

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        try:
            return self._inner.call(action, request, context)
        finally:
            self._clock.time += 10


@pytest.mark.spec("TRN-38", "TRN-42", "TRN-43")
@pytest.mark.parametrize("caller", [None, "unlisted"])
def test_a_refusal_that_comes_back_after_the_callers_deadline_is_a_timeout(
    clock: Clock, caller: str | None
) -> None:
    provider = Recording()
    transport = _LateRefusal(InProcess(Front, provider, principal_from=()), clock)
    connector: Any = Connector(Front, transport)
    with (
        _caller(caller, principal=ALICE, timeout=5),
        pytest.raises(ContractError) as raised,
    ):
        connector.ask(Ask())
    assert raised.value.kind == "timeout"
    # Within the deadline, the same refusal is what the caller gets.
    with _caller(caller, principal=ALICE), pytest.raises(ContractError) as raised:
        connector.ask(Ask())
    assert raised.value.kind == (
        "unauthenticated" if caller is None else "permission_denied"
    )


@pytest.mark.spec("TRN-42", "TRN-43")
def test_a_transport_built_with_no_declaration_lists_no_caller() -> None:
    provider = Recording()
    with pytest.raises(Failure) as raised:
        InProcess(Front, provider).call(
            ASK, b"{}", _sent(principal=b'{"issuer":"i","subject":"s"}')
        )
    assert raised.value.kind == "permission_denied"
    assert InProcess(Front, provider).call(ASK, b"{}", _sent()) == b'{"n":0}'


@pytest.mark.parametrize(
    ("given", "error"),
    [
        ({"principal_from": "shop"}, "principal_from takes a tuple of app names"),
        ({"principal_from": ["shop"]}, "principal_from takes a tuple of app names"),
        ({"principal_from": (b"shop",)}, "principal_from takes a tuple of app names"),
        ({"requires": "front.v1"}, "requires takes a tuple of contract identities"),
        ({"requires": (Front,)}, "requires takes a tuple of contract identities"),
    ],
)
def test_the_transport_refuses_lists_of_another_form(
    given: dict[str, Any], error: str
) -> None:
    with pytest.raises(TypeError, match=error):
        InProcess(Front, Recording(), app="front", **given)


# In the host: ADR 0017's allow-list case, in-process.

_CONFIGS: dict[str, type[AppConfig]] = {
    "web": WebConfig,
    "stock": StockConfig,
    "couriers": CouriersConfig,
}


def _hosted(label: str, **given: Any) -> Any:  # noqa: ANN401
    """A root for a hosted app, with the requires its declaration holds."""
    declaration = declaration_of(_CONFIGS[label])
    assert declaration is not None
    requires = tuple(c.identity for c in declaration.requires)
    return _context.root(label, requires=requires, **given)


@pytest.mark.spec("TRN-39", "TRN-43")
def test_a_principal_crosses_where_each_caller_is_listed() -> None:
    stock = connect("stock.v1")
    with (
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
        _hosted("web", principal=ALICE),
    ):
        assert stock.level(Sku(sku="abc")) == Level(on_hand=3)
    # Web is listed by stock, and stock by couriers.
    assert courier_services.SEEN[-1] == ("stock", ALICE)


@pytest.mark.spec("TRN-43")
def test_a_principal_from_a_caller_the_contract_does_not_list_is_refused() -> None:
    couriers = connect("couriers.v3")
    before = len(courier_services.SEEN)
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        with _hosted("web", principal=ALICE), pytest.raises(ContractError) as raised:
            couriers.quote(Sku(sku="abc"))
        assert raised.value.kind == "permission_denied"
        assert len(courier_services.SEEN) == before
        # The same call with no principal is not refused by the list.
        with _hosted("web"):
            assert couriers.quote(Sku(sku="abc")) == Level(on_hand=3)
    assert courier_services.SEEN[-1] == ("web", None)


@pytest.mark.spec("TRN-39", "TRN-42")
def test_in_the_host_a_call_outside_requires_or_from_no_app_is_refused() -> None:
    stock = connect("stock.v1")
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        with _hosted("couriers"), pytest.raises(UsageError) as usage:
            stock.level(Sku(sku="a"))
        assert str(usage.value).startswith(
            "app couriers calls stock.v1, which its declaration does not require; "
            "it requires no contract."
        )
        with _context.root(), pytest.raises(ContractError) as raised:
            stock.level(Sku(sku="a"))
        assert raised.value.kind == "unauthenticated"


@pytest.mark.spec("TRN-39", "TRN-43")
def test_each_hosted_transport_holds_exactly_its_apps_declaration() -> None:
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        host = _current()
        assert host is not None
        checked = []
        for config in global_apps.get_app_configs():
            declaration = declaration_of(type(config))
            if declaration is None:
                continue
            for provision in declaration.provides:
                identity = provision.contract.identity
                transport = host.connectors[identity]._transport
                assert type(transport) is InProcess
                assert transport._app == declaration.label
                assert transport._allowed == frozenset(provision.principal_from)
                assert transport._requires == frozenset(
                    contract.identity for contract in declaration.requires
                )
                checked.append(identity)
    assert sorted(checked) == ["couriers.v3", "stock.v1"]


@pytest.mark.spec("TRN-43")
def test_a_hosted_app_its_own_list_does_not_name_cannot_assert_to_itself() -> None:
    stock = connect("stock.v1")
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        # Stock requires its own contract, but lists only web.
        with _hosted("stock", principal=ALICE), pytest.raises(ContractError) as raised:
            stock.level(Sku(sku="ab"))
        assert raised.value.kind == "permission_denied"
        assert raised.value.message.startswith("app stock may not assert")
        with _hosted("stock"):
            assert stock.level(Sku(sku="ab")) == Level(on_hand=2)
