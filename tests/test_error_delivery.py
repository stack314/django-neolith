"""How errors cross a call: the provider's host and the connector (spec 01).

The provider's host delivers only an error the action raised itself, of a kind
in its error set other than ``unavailable`` and ``timeout``, with a declared
kind's detail record. Everything else becomes ``internal``, with fixed text and
nothing of the original. The connector reads what arrives against its contract.
"""

import copy
import json
import logging
import pickle
import types
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Protocol, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import AwareDatetime, BaseModel, BeforeValidator

from neolith import (
    Contract,
    ContractDefinitionError,
    ContractError,
    _context,
    action,
    contract,
)
from neolith._connector import Connector, Failure, InProcess, internal_message
from neolith._context import Scope, Sent
from neolith._contracts import Action, is_received, untyped_problem
from neolith._crossing import Receiver, codec
from tests.strategies import admitted_by
from tests.test_contract_round_trip import _HYPOTHESIS, _TYPES, _name, _records
from tests.test_crossing import _shared


@pytest.fixture(autouse=True)
def call_scope() -> Iterator[Scope]:
    """A root for each test's calls: a connector call needs an open scope
    (spec 02, TRN-23). How a call's context is derived and crosses is tested
    in ``test_call_context.py``."""
    with _context.root("tests", requires=_REQUIRES) as scope:
        yield scope


# The contracts this module declares, which its calling app requires.
_REQUIRES = ("delivery.v1", "described.errors.v1", "downstream.v1", "guarded.v1")


def _internal() -> str:
    """The fixed message of an internal error for the test's trace."""
    scope = _context._CURRENT.get()
    assert scope is not None
    return internal_message(scope.context.trace_id)


class Order(BaseModel):
    order: int


class Done(BaseModel):
    ok: bool = True


class OutOfStock(BaseModel):
    sku: str
    left: list[int] = []


class Locked(BaseModel):
    until: str


@contract("delivery.v1")
class Shop(Protocol):
    @action(errors={"out_of_stock": OutOfStock})
    def reserve(self, request: Order) -> Done: ...

    @action(errors={"locked": Locked})
    def cancel(self, request: Order) -> Done: ...


SHOP = Contract.of(Shop)


class Raising:
    """An implementation whose actions end with what it is given."""

    def __init__(self, failure: Callable[[], BaseException]) -> None:
        self._failure = failure

    def reserve(self, request: Order) -> Done:
        raise self._failure()

    def cancel(self, request: Order) -> Done:
        raise self._failure()


def _call(failure: Callable[[], BaseException]) -> ContractError:
    connector: Any = Connector(Shop, InProcess(Shop, Raising(failure)))
    with pytest.raises(ContractError) as raised:
        connector.reserve(Order(order=1))
    return raised.value


def _assert_internal(error: ContractError) -> None:
    assert (error.kind, error.message, error.data) == ("internal", _internal(), None)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []


def _assert_clean(error: ContractError) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []


# CON-43: the action's own errors of its error set cross with kind, message
# and detail record.


@pytest.mark.spec("CON-43", "TRN-3")
@pytest.mark.parametrize(
    "kind",
    [
        "invalid_argument",
        "not_found",
        "unauthenticated",
        "permission_denied",
        "conflict",
        "internal",
    ],
)
def test_a_standard_kind_the_action_raises_reaches_the_caller(kind: str) -> None:
    sent = ContractError(kind, f"{kind} happened")
    sent.add_note("provider note")
    received = _call(lambda: sent)
    assert received is not sent
    assert (received.kind, received.message, received.data) == (
        kind,
        f"{kind} happened",
        None,
    )
    _assert_clean(received)


@pytest.mark.spec("CON-43", "CON-27", "TRN-3")
def test_a_declared_kind_reaches_the_caller_with_a_new_detail_record() -> None:
    detail = OutOfStock(sku="a", left=[1, 2])
    received = _call(lambda: ContractError("out_of_stock", "none left", detail))
    assert (received.kind, received.message) == ("out_of_stock", "none left")
    assert type(received.data) is OutOfStock
    assert received.data == detail
    assert _shared(detail, received.data) == set()
    _assert_clean(received)


@pytest.mark.spec("CON-43", "CON-48")
def test_every_error_a_connector_raises_is_marked_as_received() -> None:
    assert is_received(_call(lambda: ContractError("not_found", "gone")))
    assert not is_received(ContractError("not_found", "gone"))


# CON-44 and CON-46: everything else becomes internal, with nothing of it.


class Secret(Exception):
    """An exception whose text must not cross."""


def _many_values() -> OutOfStock:
    return OutOfStock(sku="a", left=list(range(10_000)))


_INTERNAL_CASES: dict[str, Callable[[], BaseException]] = {
    "own exception": lambda: Secret("password=hunter2"),
    "unavailable": lambda: ContractError("unavailable", "the action may not"),
    "timeout": lambda: ContractError("timeout", "the action may not"),
    "undeclared kind": lambda: ContractError("gone", "not declared"),
    "reserved kind": lambda: ContractError("aborted", "a later standard kind"),
    "kind of another action": lambda: ContractError(
        "locked", "declared on cancel", Locked(until="noon")
    ),
    "declared kind without detail": lambda: ContractError("out_of_stock", "none"),
    "declared kind with an untyped detail": lambda: ContractError(
        "out_of_stock", "none", {"sku": "a"}
    ),
    "declared kind with another record": lambda: ContractError(
        "out_of_stock", "none", Locked(until="noon")
    ),
    "declared kind with a subtype": lambda: ContractError(
        "out_of_stock", "none", type("Sub", (OutOfStock,), {})(sku="a")
    ),
    "declared kind with a broken record": lambda: ContractError(
        "out_of_stock", "none", OutOfStock.model_construct(sku=1)
    ),
    # A boolean in an integer list would travel as 1: the send check refuses.
    "declared kind with a value its type does not hold": lambda: ContractError(
        "out_of_stock", "none", OutOfStock.model_construct(sku="a", left=[True])
    ),
    # 10,002 values with the sku and the list: an untyped map holds 10,000.
    "detail beyond an untyped map": lambda: ContractError(
        "out_of_stock", "none", _many_values()
    ),
    "group": lambda: ExceptionGroup("g", [ContractError("not_found", "x")]),
}


@pytest.mark.spec("CON-44", "CON-46", "CON-31", "TRN-3", "TRN-15")
@pytest.mark.parametrize("case", list(_INTERNAL_CASES), ids=list(_INTERNAL_CASES))
def test_any_other_failure_reaches_the_caller_as_internal(case: str) -> None:
    _assert_internal(_call(_INTERNAL_CASES[case]))


@pytest.mark.spec("CON-43", "CON-31")
def test_a_detail_at_the_untyped_maps_bound_is_delivered() -> None:
    # The nearest legitimate shape: the sku, the list and its 9,998 items
    # make 10,000 values.
    detail = OutOfStock(sku="a", left=list(range(9_998)))
    received = _call(lambda: ContractError("out_of_stock", "none", detail))
    assert received.data == detail


@pytest.mark.spec("CON-47")
@pytest.mark.parametrize(
    "case", ["own exception", "undeclared kind", "detail beyond an untyped map"]
)
def test_the_provider_side_records_the_original(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    original = _INTERNAL_CASES[case]()
    with caplog.at_level(logging.ERROR, logger="neolith"):
        _assert_internal(_call(lambda: original))
    record = caplog.records[-1]
    assert "delivery.v1, action 'reserve': the provider failed" in record.getMessage()
    assert record.exc_info is not None
    logged = record.exc_info[1]
    # The original itself, or the reason its detail could not be sent, chained
    # to it.
    assert logged is original or logged.__context__ is original  # type: ignore[union-attr]


class Stop(BaseException):
    """A signal to stop, not a failure of the action."""


@pytest.mark.spec("CON-45")
@pytest.mark.parametrize(
    "signal",
    [Stop(), KeyboardInterrupt(), SystemExit(1)],
    ids=lambda s: type(s).__name__,
)
def test_a_signal_to_stop_is_not_converted(signal: BaseException) -> None:
    connector: Any = Connector(Shop, InProcess(Shop, Raising(lambda: signal)))
    with pytest.raises(type(signal)) as raised:
        connector.reserve(Order(order=1))
    assert raised.value is signal


# CON-48: an error received from the action's own call is not its own.


@contract("downstream.v1")
class Downstream(Protocol):
    @action
    def find(self, request: Order) -> Done: ...


class Missing:
    def find(self, request: Order) -> Done:
        raise ContractError("not_found", "no such order downstream")


_DOWNSTREAM: Any = Connector(Downstream, InProcess(Downstream, Missing()))
# Pickles an error the test built itself, never untrusted data.
_DUPLICATES: dict[str, Callable[[ContractError], ContractError]] = {
    "a copy": copy.copy,
    "a deep copy": copy.deepcopy,
    "a pickle": lambda error: pickle.loads(pickle.dumps(error)),  # noqa: S301
}
_KEPT: list[ContractError] = []


def _received() -> ContractError:
    try:
        _DOWNSTREAM.find(Order(order=1))
    except ContractError as error:
        return error
    raise AssertionError("the downstream call did not fail")


class Nesting:
    """An implementation that calls downstream and passes its error on."""

    def __init__(self, how: str) -> None:
        self._how = how

    def reserve(self, request: Order) -> Done:
        how = self._how
        if how == "kept from an earlier call":
            raise _KEPT[0]
        try:
            _DOWNSTREAM.find(request)
        except ContractError as error:
            if how == "bare":
                raise
            if how == "by name":
                raise error
            if how == "from None":
                raise error from None
            if how == "without its traceback":
                raise error.with_traceback(None) from None
            if how == "a new error":
                raise ContractError(error.kind, error.message) from error
            if how in _DUPLICATES:
                raise _DUPLICATES[how](error) from None
        raise AssertionError(how)

    def cancel(self, request: Order) -> Done:
        return Done()


@pytest.mark.spec("CON-48")
@pytest.mark.parametrize(
    "how",
    [
        "bare",
        "by name",
        "from None",
        "without its traceback",
        "kept from an earlier call",
        *_DUPLICATES,
    ],
)
def test_an_error_received_from_a_nested_call_reaches_the_caller_as_internal(
    how: str,
) -> None:
    _KEPT[:] = [_received()]
    connector: Any = Connector(
        Shop, InProcess(Shop, Nesting(how), app="tests", requires=_REQUIRES)
    )
    with pytest.raises(ContractError) as raised:
        connector.reserve(Order(order=1))
    _assert_internal(raised.value)


@pytest.mark.spec("CON-48", "CON-43")
def test_a_new_error_passing_a_nested_one_on_is_delivered() -> None:
    connector: Any = Connector(
        Shop, InProcess(Shop, Nesting("a new error"), app="tests", requires=_REQUIRES)
    )
    with pytest.raises(ContractError) as raised:
        connector.reserve(Order(order=1))
    assert (raised.value.kind, raised.value.message) == (
        "not_found",
        "no such order downstream",
    )


# CON-28 and CON-49: the caller reads what arrives against its contract.


class Arriving:
    """A transport that answers every call with one failure."""

    contract = SHOP
    receiver = Receiver.IN_PROCESS

    def __init__(self, failure: Failure) -> None:
        self._failure = failure

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        raise self._failure


def _arrives(
    kind: object, message: object = "it failed", detail: object = None
) -> ContractError:
    failure = Failure(cast("str", kind), cast("str", message), cast("bytes", detail))
    connector: Any = Connector(Shop, Arriving(failure))
    with pytest.raises(ContractError) as raised:
        connector.reserve(Order(order=1))
    return raised.value


@pytest.mark.spec("CON-28")
def test_an_unknown_kind_arrives_with_that_kind() -> None:
    received = _arrives("gone_for_good")
    assert (received.kind, received.message, received.data) == (
        "gone_for_good",
        "it failed",
        None,
    )


@pytest.mark.spec("CON-28", "CON-30")
def test_an_unknown_kinds_detail_arrives_as_an_untyped_map() -> None:
    sent = {"sku": "a", "left": [1, 2.5, None, True, {"x": "y"}]}
    received = _arrives("gone_for_good", detail=json.dumps(sent).encode())
    assert received.data == sent
    with pytest.raises(TypeError):
        received.data["sku"] = "b"


@pytest.mark.spec("CON-28")
def test_a_kind_another_action_declares_arrives_as_an_unknown_kind() -> None:
    # A later version may have added it to this action, a compatible change.
    received = _arrives("locked", detail=b'{"until": "noon"}')
    assert (received.kind, received.data) == ("locked", {"until": "noon"})


@pytest.mark.spec("CON-28", "CON-49")
def test_a_reserved_name_arrives_as_an_unknown_kind() -> None:
    # A later version of the specification may make it standard (CON-23).
    assert _arrives("aborted").kind == "aborted"


def _deep(levels: int) -> bytes:
    return ("[" * (levels - 1) + "]" * (levels - 1)).join(['{"a": ', "}"]).encode()


# Each case, what arrives, and the requirements it shows: CON-49 for every
# case, CON-28 where the kind is one the caller does not know, and CON-30
# where its detail breaks the untyped map's bounds.
_UNKNOWN = ("CON-49", "CON-28")
_BEYOND = ("CON-49", "CON-28", "CON-30")
_UNREADABLE: dict[str, tuple[tuple[object, object, object], tuple[str, ...]]] = {
    "standard kind with a detail": (("not_found", "x", b"{}"), ("CON-49",)),
    "declared kind without a detail": (("out_of_stock", "x", None), ("CON-49",)),
    "declared kind with an unreadable detail": (
        ("out_of_stock", "x", b'{"sku": 1}'),
        ("CON-49",),
    ),
    "declared kind with a detail that is not JSON": (
        ("out_of_stock", "x", b"{"),
        ("CON-49",),
    ),
    "standard kind with a lone surrogate in its message": (
        ("not_found", "\ud800", None),
        ("CON-49",),
    ),
    "standard kind with a message not text": (("not_found", b"x", None), ("CON-49",)),
    "malformed kind": (("Not-A-Kind", "x", None), _UNKNOWN),
    "kind not text": ((3, "x", None), _UNKNOWN),
    "unknown kind with a lone surrogate in its message": (
        ("brand_new", "\ud800", None),
        _UNKNOWN,
    ),
    "unknown kind with a detail not bytes": (("gone", "x", '{"a": 1}'), _UNKNOWN),
    "unknown detail too deep": (("gone", "x", _deep(33)), _BEYOND),
    "unknown detail too large": (
        ("gone", "x", json.dumps({"a": [0] * 10_000}).encode()),
        _BEYOND,
    ),
    "unknown detail with a large integer": (
        ("gone", "x", b'{"a": 9007199254740992}'),
        _BEYOND,
    ),
    "unknown detail with NaN": (("gone", "x", b'{"a": NaN}'), _BEYOND),
    "unknown detail with an infinite number": (("gone", "x", b'{"a": 1e400}'), _BEYOND),
    "unknown detail with a duplicate name": (
        ("gone", "x", b'{"a": 1, "a": 2}'),
        _UNKNOWN,
    ),
    "unknown detail with a lone surrogate": (
        ("gone", "x", b'{"a": "\\ud800"}'),
        _BEYOND,
    ),
    "unknown detail not UTF-8": (("gone", "x", '{"a": 1}'.encode("utf-16")), _UNKNOWN),
    "unknown detail not a map": (("gone", "x", b"[1]"), _BEYOND),
}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case, marks=pytest.mark.spec(*cited))
        for case, (_, cited) in _UNREADABLE.items()
    ],
)
def test_an_error_that_cannot_be_read_reaches_the_caller_as_internal(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="neolith"):
        _assert_internal(_arrives(*_UNREADABLE[case][0]))
    assert "the error could not be read" in caplog.text


@pytest.mark.spec("CON-28", "CON-30")
def test_an_unknown_detail_at_the_untyped_maps_bounds_arrives() -> None:
    # The nearest legitimate shapes: 32 levels, 10,000 values, the largest
    # portable integer.
    assert _arrives("gone", detail=_deep(32)).data is not None
    assert _arrives("gone", detail=json.dumps({"a": [0] * 9_999}).encode()).data
    received = _arrives("gone", detail=b'{"a": 9007199254740991}')
    assert received.data == {"a": 2**53 - 1}


@pytest.mark.spec("CON-49", "CON-43")
def test_a_declared_kinds_detail_arriving_is_read_with_its_record() -> None:
    received = _arrives("out_of_stock", detail=b'{"sku": "a", "unknown": 1}')
    assert type(received.data) is OutOfStock
    assert received.data == OutOfStock(sku="a")


# Invariant: a declared kind's detail generated from its description crosses
# equal and unshared, or as internal when it is beyond an untyped map.


def _declared(record: type[BaseModel]) -> tuple[Any, list[ContractError]]:
    def send(self: object, request: Order) -> None: ...

    declared = types.new_class(
        _name("F"),
        (Protocol,),
        exec_body=lambda ns: ns.update(send=action(errors={"failed": record})(send)),
    )
    contract(f"{_name('failing')}.v1")(declared)
    raised: list[ContractError] = []

    def fail(self: object, request: Order) -> None:
        raise raised[0]

    fail.__annotations__ = {"request": Order, "return": None}
    implementation = type(_name("Failing"), (), {"send": fail})()
    return Connector(declared, InProcess(declared, implementation)), raised


@pytest.mark.spec("CON-43", "CON-27", "CON-31")
@_HYPOTHESIS
@given(st.data())
def test_generated_details_cross_equal_and_unshared(data: st.DataObject) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    try:
        connector, raised = _declared(record)
    except ContractDefinitionError:
        return
    description = Contract.of(connector._contract.declaration).describe()
    schema = {"$defs": description["$defs"], "$ref": f"#/$defs/{record.__name__}"}
    detail = record.model_validate_json(json.dumps(data.draw(admitted_by(schema))))
    raised[:] = [ContractError("failed", "it failed", detail)]
    with (
        _context.root("tests", requires=(connector._contract.identity,)),
        pytest.raises(ContractError) as caught,
    ):
        connector.send(Order(order=1))
    sent = json.loads(codec(connector._contract, record).send(detail, "the detail"))
    if untyped_problem(sent) is not None:
        # Only a detail an untyped map cannot hold is refused.
        assert caught.value.kind == "internal"
        return
    assert type(caught.value.data) is record
    assert caught.value.data == detail
    assert _shared(detail, caught.value.data) == set()


_READS = [0]


def _second_read_refused(value: object) -> object:
    # Building the request and the caller's check read it first; the
    # provider's read is the third, where state the validator reads changed.
    _READS[0] += 1
    if _READS[0] == 3:
        raise ContractError("permission_denied", "refused while reading")
    return value


class Guarded(BaseModel):
    order: Annotated[int, BeforeValidator(_second_read_refused)]


@contract("guarded.v1")
class GuardedShop(Protocol):
    @action
    def reserve(self, request: Guarded) -> Done: ...


class GuardedImpl:
    def reserve(self, request: Guarded) -> Done:
        return Done()


@pytest.mark.spec("CON-44", "CON-43")
def test_an_error_from_reading_the_request_is_the_hosts_failure() -> None:
    # Only an error the action itself ends with is delivered; one raised while
    # the provider's host reads the request is a failure of the host.
    _READS[0] = 0
    connector: Any = Connector(GuardedShop, InProcess(GuardedShop, GuardedImpl()))
    with pytest.raises(ContractError) as raised:
        connector.reserve(Guarded(order=1))
    _assert_internal(raised.value)


# A declared kind's detail only the description refuses: Pydantic holds it,
# the send check does not (TRN-15), so the error becomes internal.


class WideDetail(BaseModel):
    """A detail record whose type holds values its description refuses."""

    n: int = 0
    kind: Literal["a", "b"] = "a"
    at: AwareDatetime = datetime(2000, 1, 1, tzinfo=UTC)


@contract("described.errors.v1")
class DescribedErrors(Protocol):
    @action(errors={"refused": WideDetail})
    def get(self, request: Order) -> Done: ...


def _detail_past_literal() -> WideDetail:
    detail = WideDetail()
    detail.kind = "c"  # type: ignore[assignment]  # set after validation
    return detail


_DETAILS_ONLY_THE_DESCRIPTION_REFUSES: dict[str, Callable[[], WideDetail]] = {
    "integer past the portable range": lambda: WideDetail(n=2**53),
    "negative integer past the portable range": lambda: WideDetail(n=-(2**53)),
    "value outside a literal": _detail_past_literal,
    "timestamp past year 9999 in UTC": lambda: WideDetail(
        at=datetime(9999, 12, 31, 23, tzinfo=timezone(-timedelta(hours=5)))
    ),
}


def _raise_detail(detail: WideDetail) -> ContractError:
    class Refusing:
        def get(self, request: Order) -> Done:
            raise ContractError("refused", "it was refused", detail)

    connector: Any = Connector(DescribedErrors, InProcess(DescribedErrors, Refusing()))
    with pytest.raises(ContractError) as raised:
        connector.get(Order(order=1))
    return raised.value


@pytest.mark.spec("CON-43", "CON-44", "TRN-15")
@pytest.mark.parametrize(
    "case",
    list(_DETAILS_ONLY_THE_DESCRIPTION_REFUSES),
    ids=list(_DETAILS_ONLY_THE_DESCRIPTION_REFUSES),
)
def test_a_detail_only_the_description_refuses_reaches_the_caller_as_internal(
    case: str, caplog: pytest.LogCaptureFixture
) -> None:
    detail = _DETAILS_ONLY_THE_DESCRIPTION_REFUSES[case]()
    with caplog.at_level(logging.ERROR, logger="neolith"):
        _assert_internal(_raise_detail(detail))
    assert "described.errors.v1, action 'get': the provider failed" in caplog.text


@pytest.mark.spec("CON-43", "TRN-15")
def test_a_detail_at_the_descriptions_bounds_is_delivered() -> None:
    # The nearest legitimate shapes: the largest portable integers, and the
    # last instant of year 9999 in UTC.
    for detail in (
        WideDetail(n=2**53 - 1),
        WideDetail(n=-(2**53 - 1), kind="b"),
        WideDetail(at=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)),
    ):
        received = _raise_detail(detail)
        assert (received.kind, received.data) == ("refused", detail)
