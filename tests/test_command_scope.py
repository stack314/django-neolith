"""The command scope: an explicit root that names its app (spec 02, Explicit roots).

``neolith.command_scope(app, *, principal=None, timeout=None)`` opens a root
for a hosted app, with no principal and no deadline unless given, and a fresh
trace (ADR 0022 Decision 8, ADR 0017 Decision 4). It closes on exit, however
the block ends, and is used once. Time is a clock the tests set, never a
sleep.
"""

import asyncio
import contextvars
import copy
import decimal
import math
import pickle
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st

import neolith
from neolith import (
    CallContext,
    ContractError,
    Principal,
    UsageError,
    _context,
    command_scope,
    connect,
)
from neolith._connector import Connector, InProcess
from tests import host_apps
from tests.declaration_contracts import Sku
from tests.host_apps.couriers import services as courier_services
from tests.hosting import in_fresh_process
from tests.test_calling_app import FRONT, Ask, Front, Recording, Told
from tests.test_host import WriteApp, _apps_module
from tests.test_host import write_app as write_app

ALICE = Principal("https://id.example", "alice")
HOSTED = "couriers, stock, web"

couriers = connect("couriers.v3")
stock = connect("stock.v1")


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


@pytest.fixture
def host() -> Iterator[None]:
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        yield


def _quote() -> CallContext:
    """Call the couriers contract and return the context its action saw."""
    before = len(courier_services.CONTEXTS)
    couriers.quote(Sku(sku="ab"))
    assert len(courier_services.CONTEXTS) == before + 1
    return courier_services.CONTEXTS[-1]


def _ambient() -> _context.Scope | None:
    return _context._CURRENT.get()


# Opening a root that names its app.


@pytest.mark.spec("TRN-44", "TRN-46")
@pytest.mark.usefixtures("host")
@pytest.mark.parametrize("label", ["web", "stock"])
def test_a_command_scope_calls_as_the_app_it_names(label: str) -> None:
    with command_scope(label):
        seen = _quote()
    assert seen.caller == label
    assert seen.principal is None


@pytest.mark.spec("TRN-46", "TRN-39")
@pytest.mark.usefixtures("host")
def test_a_command_scope_is_checked_against_its_apps_requires() -> None:
    with command_scope("couriers"), pytest.raises(UsageError) as raised:
        stock.level(Sku(sku="a"))
    assert str(raised.value).startswith(
        "app couriers calls stock.v1, which its declaration does not require; "
        "it requires no contract."
    )


@pytest.mark.spec("TRN-47", "TRN-46")
@pytest.mark.usefixtures("host")
def test_a_command_scope_holds_the_principal_it_is_given() -> None:
    with command_scope("stock", principal=ALICE):
        assert _quote().principal == ALICE
    # couriers lists stock, not web, as a caller that may assert a principal.
    with command_scope("web", principal=ALICE), pytest.raises(ContractError) as raised:
        couriers.quote(Sku(sku="ab"))
    assert raised.value.kind == "permission_denied"
    with command_scope("web", principal=None):
        assert _quote().principal is None


@pytest.mark.spec("TRN-47")
@pytest.mark.usefixtures("host")
def test_a_principal_that_cannot_cross_fails_each_call_with_internal() -> None:
    with command_scope("stock", principal=Principal("", "alice")):
        for _ in range(2):
            with pytest.raises(ContractError) as raised:
                couriers.quote(Sku(sku="ab"))
            assert raised.value.kind == "internal"


# The deadline: only with a timeout, counted from entry.


@pytest.mark.spec("TRN-48")
@pytest.mark.usefixtures("host")
def test_a_command_scope_has_no_deadline_unless_given_a_timeout(clock: Clock) -> None:
    with command_scope("web"):
        clock.time += 10**9
        assert _quote().deadline is None


@pytest.mark.spec("TRN-48", "TRN-27")
@pytest.mark.usefixtures("host")
@pytest.mark.parametrize("timeout", [5, 5.0])
def test_the_timeout_is_counted_from_entry(clock: Clock, timeout: float) -> None:
    scope = command_scope("web", timeout=timeout)
    clock.time += 100  # building the scope starts nothing
    with scope:
        clock.time += 3
        seen = _quote()
    # 2 s of budget were left when the call was sent at 1103.
    assert seen.deadline == pytest.approx(1105.0)


@pytest.mark.spec("TRN-48", "TRN-28")
@pytest.mark.usefixtures("host")
@pytest.mark.parametrize("timeout", [0, 0.0, -1, -1e300, -(10**400), 0.0005])
def test_a_timeout_that_leaves_no_budget_fails_each_call_with_timeout(
    clock: Clock, timeout: float
) -> None:
    before = len(courier_services.CONTEXTS)
    with command_scope("web", timeout=timeout), pytest.raises(ContractError) as raised:
        couriers.quote(Sku(sku="ab"))
    assert raised.value.kind == "timeout"
    assert len(courier_services.CONTEXTS) == before


@pytest.mark.spec("TRN-48", "TRN-29")
@pytest.mark.usefixtures("host")
@pytest.mark.parametrize("timeout", [10**7, 1e300, 10**400])
def test_a_budget_above_the_cap_counts_as_the_cap(clock: Clock, timeout: float) -> None:
    with command_scope("web", timeout=timeout):
        seen = _quote()
    assert seen.deadline == pytest.approx(clock.time + 9_999_999.999)


# The trace and the key.


@pytest.mark.spec("TRN-49", "TRN-33")
@pytest.mark.usefixtures("host")
def test_each_command_scope_starts_a_new_trace_with_random_keys() -> None:
    seen = []
    for _ in range(2):
        with command_scope("web"):
            seen += [_quote(), _quote()]
    first, second = {c.trace_id for c in seen[:2]}, {c.trace_id for c in seen[2:]}
    assert len(first) == len(second) == 1
    assert first != second
    keys = [c.idempotency_key for c in seen]
    assert len(set(keys)) == len(keys)
    assert all(key is not None and len(key) == 22 for key in keys)
    assert {c.tracestate for c in seen} == {""}


# Nesting: a new root that takes nothing, and the outer one current again.


@pytest.mark.spec("TRN-50", "TRN-51")
@pytest.mark.usefixtures("host")
def test_a_nested_command_scope_takes_nothing_from_the_enclosing_one(
    clock: Clock,
) -> None:
    with command_scope("stock", principal=ALICE, timeout=5):
        outer = _quote()
        with command_scope("web"):
            inner = _quote()
        after = _quote()
    assert (outer.caller, outer.principal) == ("stock", ALICE)
    assert outer.deadline is not None
    assert (inner.caller, inner.principal, inner.deadline) == ("web", None, None)
    assert inner.trace_id != outer.trace_id
    assert (after.caller, after.principal, after.trace_id) == (
        "stock",
        ALICE,
        outer.trace_id,
    )
    assert after.deadline is not None
    assert _ambient() is None


@pytest.mark.spec("TRN-50", "TRN-51")
@pytest.mark.usefixtures("host")
def test_a_command_scope_inside_an_action_is_a_root_of_its_own() -> None:
    inside: list[CallContext] = []

    held: list[_context.Scope] = []

    def then(request: Ask, ctx: CallContext) -> None:
        assert ctx.idempotency_key is not None  # the enclosing scope has a key
        with command_scope("web"):
            current = _ambient()
            assert current is not None
            held.append(current)
            inside.append(_quote())
        # The action's own scope is current again: front's app is not hosted
        # and requires nothing, so its call is refused.
        with pytest.raises(UsageError, match=r"^app front calls couriers\.v3"):
            couriers.quote(Sku(sku="ab"))

    provider = Recording(then)
    front = Connector(
        Front,
        InProcess(Front, provider, app="front", requires=(), principal_from=("shop",)),
    )
    with _context.root("shop", requires=(FRONT.identity,), principal=ALICE, timeout=9):
        assert front.ask(Ask(n=1)) == Told(n=1)
    [action_ctx] = provider.seen
    [seen] = inside
    [root] = held
    # Every field a root sets, none taken from the action's keyed scope.
    assert (root.app, root.requires) == ("web", frozenset({"stock.v1", "couriers.v3"}))
    assert root.held.principal is None
    assert root.held.deadline is None
    assert root.held.idempotency_key is None
    assert root.held.caller is None
    assert root.held.tracestate == ""
    assert (seen.caller, seen.principal, seen.deadline) == ("web", None, None)
    assert seen.trace_id == root.held.trace_id != action_ctx.trace_id
    # A random key (TRN-33), not one derived from the action's key.
    assert seen.idempotency_key is not None
    assert len(seen.idempotency_key) == 22


@pytest.mark.spec("TRN-50", "TRN-51", "TRN-46")
@given(
    st.lists(
        # couriers lets stock alone assert a principal.
        st.tuples(
            st.sampled_from([("web", None), ("stock", None), ("stock", ALICE)]),
            st.sampled_from([None, 60, 3600.5]),
        ).map(lambda drawn: (*drawn[0], drawn[1])),
        min_size=1,
        max_size=5,
    )
)
def test_every_call_carries_the_innermost_open_scope(
    stack: list[tuple[str, Principal | None, float | None]],
) -> None:
    """Nest scopes in any order: a call made at each depth, on the way in
    and on the way out, carries exactly that depth's app, principal, trace
    and whether it has a deadline.
    """
    traces: list[str] = []

    def check(depth: int) -> None:
        label, principal, timeout = stack[depth]
        seen = _quote()
        assert (seen.caller, seen.principal) == (label, principal)
        assert (seen.deadline is None) == (timeout is None)
        if len(traces) <= depth:
            traces.append(seen.trace_id)
        assert seen.trace_id == traces[depth]

    def nest(depth: int) -> None:
        label, principal, timeout = stack[depth]
        with command_scope(label, principal=principal, timeout=timeout):
            check(depth)
            if depth + 1 < len(stack):
                nest(depth + 1)
                check(depth)

    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        nest(0)
    assert len(set(traces)) == len(traces)
    assert _ambient() is None


# Closed: on exit, however the block ends.


@pytest.mark.spec("TRN-24", "TRN-25")
@pytest.mark.usefixtures("host")
@pytest.mark.parametrize(
    "error",
    [
        KeyError("k"),
        ContractError("internal", "x"),
        KeyboardInterrupt(),
        asyncio.CancelledError(),
    ],
)
def test_a_command_scope_closes_when_its_block_raises(error: BaseException) -> None:
    found: list[_context.Scope | None] = []

    def fail() -> None:
        with command_scope("web"):
            found.append(_ambient())
            raise error

    with pytest.raises(type(error)) as raised:
        fail()
    assert raised.value is error
    [held] = found
    assert held is not None
    assert held.closed
    assert _ambient() is None
    with pytest.raises(UsageError, match="names no open scope"):
        couriers.quote(Sku(sku="ab"), context=held.context)


@pytest.mark.spec("TRN-25")
@pytest.mark.usefixtures("host")
def test_work_kept_past_its_command_scope_is_refused() -> None:
    with command_scope("web"):
        kept = contextvars.copy_context()
        held = _ambient()
    assert held is not None
    with pytest.raises(UsageError, match="has ended") as raised:
        kept.run(couriers.quote, Sku(sku="ab"))
    assert "neolith.command_scope(" in str(raised.value)
    with pytest.raises(UsageError, match="names no open scope") as raised:
        couriers.quote(Sku(sku="ab"), context=held.context)
    assert "neolith.command_scope(" in str(raised.value)


@pytest.mark.spec("TRN-25", "TRN-44")
@pytest.mark.usefixtures("host")
def test_work_that_outlives_its_call_opens_its_own_scope() -> None:
    """An action hands work to a thread that runs after the action ends: the
    action's context is refused there, and a command scope the work opens
    itself calls as its app.
    """
    kept: list[contextvars.Context] = []
    seen: list[object] = []

    def then(request: Ask, ctx: CallContext) -> None:
        kept.append(contextvars.copy_context())

    def later() -> None:
        try:
            couriers.quote(Sku(sku="ab"))
        except UsageError as error:
            seen.append(error)
        with command_scope("web"):
            seen.append(_quote().caller)

    provider = Recording(then)
    front = Connector(
        Front, InProcess(Front, provider, app="front", principal_from=("shop",))
    )
    with _context.root("shop", requires=(FRONT.identity,)):
        front.ask(Ask())
    worker = threading.Thread(target=kept[0].run, args=(later,))
    worker.start()
    worker.join()
    refused, caller = seen
    assert isinstance(refused, UsageError)
    assert "has ended" in str(refused)
    assert caller == "web"


# A root that closes where it is not current (TRN-51's condition, TRN-53).


def _isolated(test: Callable[[], None]) -> None:
    """Run ``test`` in a copy of the context, so a scope it leaves current
    stays there."""
    contextvars.copy_context().run(test)


@pytest.mark.spec("TRN-51")
@pytest.mark.usefixtures("host")
def test_a_generator_closed_where_it_was_opened_restores_the_enclosing_scope() -> None:
    def rows() -> Iterator[int]:
        with command_scope("web"):
            yield 1
            yield 2

    def run() -> None:
        with command_scope("stock"):
            for _ in rows():
                break  # the generator closes here, in this context
            assert _quote().caller == "stock"
        assert _ambient() is None

    _isolated(run)


@pytest.mark.spec("TRN-51")
@pytest.mark.usefixtures("host")
def test_an_async_generator_closed_in_its_task_restores_the_enclosing_scope() -> None:
    async def rows() -> Any:  # noqa: ANN401
        with command_scope("web"):
            yield 1
            yield 2

    async def main() -> str | None:
        with command_scope("stock"):
            found = rows()
            async for _ in found:
                break
            await found.aclose()
            return _quote().caller

    assert asyncio.run(main()) == "stock"


@pytest.mark.spec("TRN-53", "TRN-25")
@pytest.mark.usefixtures("host")
def test_an_async_generator_broken_out_of_is_reported_and_its_scope_refused() -> None:
    """Broken out of, the generator is closed later by the event loop, in
    another context: its scope closes there and its exit is reported, and the
    enclosing block, where it was left current, has its calls refused.
    """
    reported: list[object] = []

    async def rows() -> Any:  # noqa: ANN401
        with command_scope("web"):
            yield 1
            yield 2

    refused: list[UsageError] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, report: reported.append(report.get("exception"))
        )
        with command_scope("stock"):
            async for _ in rows():
                break
            for _ in range(5):
                await asyncio.sleep(0)  # the loop closes the generator
            with pytest.raises(UsageError, match="has ended") as raised:
                couriers.quote(Sku(sku="ab"))
            refused.append(raised.value)

    # The stock block's exit finds the closed web scope current: reported.
    with pytest.raises(UsageError) as raised:
        asyncio.run(main())
    assert str(raised.value).startswith(
        "command_scope('stock'): the scope closed where it is not"
    )
    assert len(refused) == 1
    [error] = reported
    assert isinstance(error, UsageError)
    assert str(error).startswith(
        "command_scope('web'): the scope closed where it is not"
    )


@pytest.mark.spec("TRN-53", "TRN-25")
@pytest.mark.usefixtures("host")
def test_a_scope_closed_out_of_order_is_reported_and_closes() -> None:
    def run() -> None:
        web, stock = command_scope("web"), command_scope("stock")
        web.__enter__()
        stock.__enter__()
        web_scope = _context._CURRENT.get()
        with pytest.raises(UsageError, match="not the current scope") as raised:
            web.__exit__(None, None, None)
        assert raised.value.args[0].startswith("command_scope('web'): ")
        # stock is still open and current; web has closed.
        assert _quote().caller == "stock"
        stock.__exit__(None, None, None)
        current = _ambient()
        assert current is not None
        assert current.closed
        assert web_scope is not None
        with pytest.raises(UsageError, match="has ended"):
            couriers.quote(Sku(sku="ab"))

    _isolated(run)


@pytest.mark.spec("TRN-53", "TRN-25")
@pytest.mark.usefixtures("host")
def test_a_scope_left_in_another_context_is_reported_and_refused_where_it_was() -> None:
    entered = contextvars.copy_context()
    scope = command_scope("web")
    entered.run(scope.__enter__)
    elsewhere = entered.copy()  # where web is current, but not entered there
    with pytest.raises(UsageError, match="not the current scope"):
        elsewhere.run(scope.__exit__, None, None, None)
    with pytest.raises(UsageError, match="has ended"):
        entered.run(couriers.quote, Sku(sku="ab"))


@pytest.mark.spec("TRN-53")
@pytest.mark.usefixtures("host")
def test_a_block_that_raises_where_its_scope_is_not_current_reports_both() -> None:
    def run() -> None:
        web = command_scope("web")

        def fail() -> None:
            with web:
                command_scope("stock").__enter__()
                raise KeyError("k")

        with pytest.raises(UsageError) as raised:
            fail()
        assert isinstance(raised.value.__context__, KeyError)

    _isolated(run)


# Opened while a set-up runs, where an earlier table exists (TRN-45).


@pytest.mark.spec("TRN-45")
def test_a_command_scope_in_an_implementations_init_during_set_up_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[str] = []
    real = courier_services.CourierService.__init__

    def init(self: courier_services.CourierService) -> None:
        try:
            with command_scope("web"):
                outcomes.append("opened")
        except UsageError as error:
            outcomes.append(str(error))
        real(self)

    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        monkeypatch.setattr(courier_services.CourierService, "__init__", init)
        with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
            pass
    assert outcomes == [
        "command_scope('web'): the host is not set up yet. A command scope "
        "opens once Django has loaded every app and neolith's set-up has passed"
    ]


@pytest.mark.spec("TRN-45")
def test_a_command_scope_in_ready_during_set_up_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.host_apps.web.apps import WebConfig

    outcomes: list[str] = []

    def ready(self: WebConfig) -> None:
        try:
            with command_scope("web"):
                outcomes.append("opened")
        except UsageError as error:
            outcomes.append(str(error))

    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        monkeypatch.setattr(WebConfig, "ready", ready, raising=False)
        with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
            pass
    assert len(outcomes) == 1
    assert outcomes[0].startswith("command_scope('web'): the host is not set up yet")


_AT_IMPORT = """
import sys

import django
from django.test import override_settings

django.setup()  # the first set-up imports the services module: one line
del sys.modules[sys.argv[2] + ".services"]
with override_settings(INSTALLED_APPS=json.loads(sys.argv[1])):
    pass  # a later set-up imports it again: a second line
"""

_SERVICES_OPENING_A_SCOPE = """
from neolith import UsageError, command_scope
from tests.declaration_contracts import Sku

try:
    with command_scope("{package}"):
        print("opened")
except UsageError as error:
    print(error)


class InvoiceService:
    def void(self, request: Sku) -> None:
        return None
"""


@pytest.mark.spec("TRN-45")
def test_a_command_scope_at_import_during_a_later_set_up_is_refused(
    write_app: WriteApp,
) -> None:
    package = write_app(
        _apps_module(provides=[("Invoices", "{package}.services.InvoiceService")]),
        services=_SERVICES_OPENING_A_SCOPE,
    )
    printed = in_fresh_process(
        _AT_IMPORT.replace("sys.argv[2]", repr(package)), [package, "neolith"]
    )
    refusal = f"command_scope('{package}'): the host is not set up yet."
    lines = printed.splitlines()
    assert len(lines) == 2
    assert all(line.startswith(refusal) for line in lines)


# Used once.


@pytest.mark.spec("TRN-52")
@pytest.mark.usefixtures("host")
def test_a_command_scope_is_entered_once() -> None:
    scope = command_scope("web")
    with scope:
        with pytest.raises(UsageError, match="entered once") as raised, scope:
            pass
        assert raised.value.args[0].startswith("command_scope('web'): ")
        assert _quote().caller == "web"  # the open one is untouched
    with pytest.raises(UsageError, match="entered once"), scope:
        pass
    assert _ambient() is None


@pytest.mark.spec("TRN-52")
@pytest.mark.usefixtures("host")
def test_threads_entering_one_command_scope_open_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first thread stops inside entry, after the once-only check, and
    only then does the second thread enter: it must not open the scope too.
    """
    scope = command_scope("web")
    in_entry, second_done = threading.Event(), threading.Event()
    real = _context.root_fields
    calls: list[None] = []

    def held_open(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        calls.append(None)
        if len(calls) == 1:
            in_entry.set()
            # Without the lock the second thread opens the scope meanwhile;
            # with it, the second thread waits here until this entry ends.
            second_done.wait(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(_context, "root_fields", held_open)
    outcomes: list[str] = []

    def enter(signal: threading.Event | None) -> None:
        try:
            scope.__enter__()
        except UsageError:
            outcomes.append("refused")
        else:
            outcomes.append("opened")
            scope.__exit__(None, None, None)
        finally:
            if signal is not None:
                signal.set()

    first = threading.Thread(target=enter, args=(None,))
    first.start()
    assert in_entry.wait(5)
    second = threading.Thread(target=enter, args=(second_done,))
    second.start()
    first.join()
    second.join()
    assert sorted(outcomes) == ["opened", "refused"]
    assert len(calls) == 1


@pytest.mark.usefixtures("host")
@pytest.mark.parametrize(
    "duplicate",
    [copy.copy, copy.deepcopy, lambda scope: pickle.dumps(scope)],
    ids=["copy", "deepcopy", "pickle"],
)
def test_a_command_scope_cannot_be_copied(duplicate: Callable[[Any], object]) -> None:
    scope = command_scope("web")
    with pytest.raises(TypeError, match="call command_scope"):
        duplicate(scope)


# Refusals: arguments at the call, the host's state on entry.


@pytest.mark.spec("CON-29")
@pytest.mark.parametrize(
    ("given", "error"),
    [
        ({"app": b"web"}, TypeError),
        ({"app": None}, TypeError),
        ({"app": 1}, TypeError),
        ({"principal": "alice"}, TypeError),
        ({"principal": {"issuer": "x", "subject": "y"}}, TypeError),
        ({"timeout": True}, TypeError),
        ({"timeout": "5"}, TypeError),
        ({"timeout": decimal.Decimal(5)}, TypeError),
        ({"timeout": math.nan}, ValueError),
        ({"timeout": math.inf}, ValueError),
        ({"timeout": -math.inf}, ValueError),
    ],
)
def test_an_argument_of_another_form_is_refused_at_the_call(
    given: dict[str, Any], error: type[Exception]
) -> None:
    arguments: dict[str, Any] = {"app": "web", **given}
    app = arguments.pop("app")
    # Raised before the host is read: no host is set up here.
    with pytest.raises(error) as raised:
        command_scope(app, **arguments)
    assert type(raised.value) is error
    assert ContractError not in type(raised.value).__mro__


def test_the_principal_and_timeout_are_keywords() -> None:
    with pytest.raises(TypeError):
        command_scope("web", ALICE)  # type: ignore[call-arg]


@pytest.mark.spec("TRN-45", "CON-29")
@pytest.mark.parametrize(
    "label",
    ["nowhere", "", "Web", "tests.host_apps.web", "neolith", "humanize", "web "],
)
def test_a_label_the_host_does_not_host_is_refused_on_entry(label: str) -> None:
    installed = ["django.contrib.humanize", *host_apps.INSTALLED]
    with override_settings(INSTALLED_APPS=installed):
        scope = command_scope(label)  # nothing is read yet
        with pytest.raises(UsageError) as raised, scope:
            pass
        assert str(raised.value) == (
            f"command_scope({label!r}): {label!r} is not a hosted app; the "
            f"hosted apps are {HOSTED}. A scope is named by an app's label, and "
            "an app is hosted when its config class declares it "
            "(a NeolithAppConfig subclass)"
        )
        assert _ambient() is None
        # No root opened: the call still finds none.
        with pytest.raises(UsageError, match="none is active here"):
            couriers.quote(Sku(sku="ab"))


@pytest.mark.spec("TRN-45")
def test_a_long_label_is_clipped_in_the_refusal() -> None:
    with (
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
        pytest.raises(UsageError) as raised,
        command_scope("x" * 10_000),
    ):
        pass
    assert len(str(raised.value)) < 1000


@pytest.mark.spec("TRN-45", "CON-29")
def test_a_command_scope_before_the_host_is_set_up_is_refused() -> None:
    with pytest.raises(UsageError) as raised, command_scope("web"):
        pass
    assert str(raised.value).startswith(
        "command_scope('web'): the host is not set up yet"
    )
    assert _ambient() is None


@pytest.mark.spec("TRN-45")
def test_a_command_scope_while_an_override_leaves_neolith_out_is_refused() -> None:
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        with (
            override_settings(INSTALLED_APPS=["django.contrib.humanize"]),
            pytest.raises(
                UsageError, match="neolith's app is not in Django's registry"
            ),
            command_scope("web"),
        ):
            pass
        with command_scope("web"):
            assert _quote().caller == "web"


@pytest.mark.spec("TRN-44", "TRN-45")
def test_a_scope_built_before_set_up_opens_once_the_host_is_set_up() -> None:
    scope = command_scope("web")
    # A refused entry opens nothing, so the same scope opens later.
    with pytest.raises(UsageError, match="not set up yet"), scope:
        pass
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED), scope:
        assert _quote().caller == "web"


# Sync and async.


@pytest.mark.spec("TRN-44", "TRN-25")
@pytest.mark.usefixtures("host")
def test_a_command_scope_in_a_coroutine_reaches_the_work_that_copies_its_context() -> (
    None
):
    async def main() -> list[object]:
        found: list[object] = []
        with command_scope("web"):
            found.append(_quote().caller)
            found.append((await asyncio.to_thread(_quote)).caller)
            found.append((await sync_to_async(_quote)()).caller)
            task = asyncio.ensure_future(_late())
        found.append(await task)
        return found

    async def _late() -> object:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            return _quote().caller
        except UsageError as error:
            return error

    caller, threaded, synced, late = asyncio.run(main())
    assert (caller, threaded, synced) == ("web", "web", "web")
    assert isinstance(late, UsageError)
    assert "has ended" in str(late)


@pytest.mark.spec("TRN-44", "TRN-50")
@pytest.mark.usefixtures("host")
def test_concurrent_tasks_each_call_as_their_own_scope() -> None:
    async def one(label: str) -> list[str | None]:
        found = []
        with command_scope(label):
            for _ in range(3):
                await asyncio.sleep(0)
                found.append(_quote().caller)
        return found

    async def main() -> list[list[str | None]]:
        return list(await asyncio.gather(one("web"), one("stock"), one("web")))

    assert asyncio.run(main()) == [["web"] * 3, ["stock"] * 3, ["web"] * 3]
    assert _ambient() is None


@pytest.mark.spec("TRN-44")
@pytest.mark.usefixtures("host")
def test_a_command_scope_opened_in_async_to_sync_code_calls_as_its_app() -> None:
    async def inner() -> str | None:
        with command_scope("stock"):
            return (await sync_to_async(_quote)()).caller

    assert async_to_sync(inner)() == "stock"
    assert _ambient() is None


@pytest.mark.usefixtures("host")
def test_a_command_scope_has_no_async_with_spelling() -> None:
    async def main() -> None:
        async with command_scope("web"):  # type: ignore[attr-defined]
            pass

    with pytest.raises(TypeError):
        asyncio.run(main())


@pytest.mark.usefixtures("host")
def test_with_yields_nothing() -> None:
    with command_scope("web") as entered:
        assert entered is None


# Each refusal's advice leads to a call that succeeds.


@pytest.mark.spec("TRN-23", "TRN-44")
@pytest.mark.usefixtures("host")
def test_the_advice_for_a_thread_with_no_scope_is_followed() -> None:
    seen: list[object] = []

    def work() -> None:
        try:
            couriers.quote(Sku(sku="ab"))
        except UsageError as error:
            seen.append(error)
        with command_scope("web"):
            seen.append(_quote().caller)

    with command_scope("web"):
        # A thread that does not inherit the context: before Python 3.14, or
        # without thread_inherit_context, every thread starts so.
        thread = threading.Thread(target=contextvars.Context().run, args=(work,))
        thread.start()
        thread.join()
    refused, caller = seen
    assert isinstance(refused, UsageError)
    assert "none is active here" in str(refused)
    assert 'opens one with neolith.command_scope("<label>")' in str(refused)
    assert caller == "web"


@pytest.mark.spec("TRN-39", "TRN-44")
@pytest.mark.usefixtures("host")
@pytest.mark.parametrize("requires", [None, ()], ids=["undeclared", "declared"])
def test_the_advice_for_a_call_outside_requires_is_followed(
    requires: tuple[str, ...] | None,
) -> None:
    with _context.root("admin", requires=requires):
        with pytest.raises(UsageError) as raised:
            couriers.quote(Sku(sku="ab"))
        assert str(raised.value).endswith(
            "code that runs in another app's scope on its own app's account opens "
            'neolith.command_scope("<its label>")'
        )
        with command_scope("web"):
            assert _quote().caller == "web"


def test_command_scope_is_public() -> None:
    assert neolith.command_scope is command_scope
    assert "command_scope" in neolith.__all__


# Every UsageError a call or a command scope raises is in the docstrings.


def _words(doc: str | None) -> str:
    return " ".join((doc or "").split())


def _no_scope() -> None:
    contextvars.Context().run(couriers.quote, Sku(sku="ab"))


def _closed_scope() -> None:
    with command_scope("web"):
        kept = contextvars.copy_context()
    kept.run(couriers.quote, Sku(sku="ab"))


def _closed_context() -> None:
    with command_scope("web"):
        held = _ambient()
    assert held is not None
    couriers.quote(Sku(sku="ab"), context=held.context)


def _foreign_context() -> None:
    made = CallContext(
        principal=None,
        trace_id="0" * 31 + "1",
        span_id="0" * 15 + "1",
        tracestate="",
        deadline=None,
        idempotency_key=None,
        caller=None,
        scope_id="x",
    )
    with command_scope("web"):
        couriers.quote(Sku(sku="ab"), context=made)


def _not_required() -> None:
    with command_scope("couriers"):
        stock.level(Sku(sku="a"))


def _undeclared() -> None:
    with _context.root("admin"):
        couriers.quote(Sku(sku="ab"))


def _not_provided() -> None:
    # web requires couriers.v3 and stock.v1; every known one has a provider,
    # so this case is reached by a connector recorded for another host.
    with command_scope("web"):
        neolith._host.Connector._made("nowhere.v1").quote(Sku(sku="ab"))


def _unknown_identity() -> None:
    connect("nowhere.v1")


def _not_hosted() -> None:
    with command_scope("nowhere"):
        pass


def _second_entry() -> None:
    scope = command_scope("web")
    with scope:
        pass
    with scope:
        pass


def _exit_not_current() -> None:
    def run() -> None:
        web = command_scope("web")
        web.__enter__()
        command_scope("stock").__enter__()
        web.__exit__(None, None, None)

    contextvars.copy_context().run(run)


@pytest.mark.usefixtures("host")
@pytest.mark.parametrize(
    ("trigger", "usage", "on_connect"),
    [
        (_no_scope, "made in no open scope", "made in no open scope"),
        (_closed_scope, "in one that has closed", "in one that has closed"),
        (_closed_context, "a scope that has closed or that this host did", None),
        (_foreign_context, "that this host did not issue", "did not issue"),
        (
            _not_required,
            "does not require the contract",
            "does not require",
        ),
        (_undeclared, "or whose app has no declaration", "or the app has none"),
        (_not_provided, "contract no app of the host provides", "no app of the host"),
        (_unknown_identity, "for a contract the host, once set up", None),
        (_not_hosted, "for an app that is not hosted", None),
        (_second_entry, "a second time", None),
        (_exit_not_current, "by its exit when it is not the current", None),
    ],
)
def test_every_usage_error_is_in_the_docstrings(
    trigger: Callable[[], None], usage: str, on_connect: str | None
) -> None:
    with pytest.raises(UsageError):
        trigger()
    assert usage in _words(UsageError.__doc__)
    if on_connect is not None:
        assert on_connect in _words(connect.__doc__)


def test_the_states_before_set_up_are_in_the_docstrings() -> None:
    with pytest.raises(UsageError, match="not set up yet"):
        couriers.quote(Sku(sku="ab"))
    with pytest.raises(UsageError, match="not set up yet"), command_scope("web"):
        pass
    for phrase in ("before set-up has finished", "leaves neolith's app out"):
        assert phrase in _words(UsageError.__doc__)
    assert "before set-up has finished" in _words(connect.__doc__)
    assert "neolith's app out" in _words(connect.__doc__)
