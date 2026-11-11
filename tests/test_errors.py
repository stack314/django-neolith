"""Contract errors and the error kinds an action declares (spec 01, Errors).

The error type and the declaration checks exist before any transport, so
these tests run without one (T2 does not apply yet). How transports and the
provider's host produce errors is tested with them, in roadmap steps 3 to 5.
"""

import copy
import dataclasses
import enum
import json
import pickle
import re
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, TypedDict, cast
from uuid import UUID

import pydantic_core
import pytest
from pydantic import AwareDatetime, BaseModel, ConfigDict, RootModel

from neolith import (
    Contract,
    ContractDefinitionError,
    ContractError,
    action,
    contract,
    implements,
)

STANDARD_KINDS = [
    "invalid_argument",
    "not_found",
    "unauthenticated",
    "permission_denied",
    "conflict",
    "unavailable",
    "timeout",
    "internal",
]

# Outside the rule "segment" of spec 01: empty, upper case, a hyphen, a space,
# a leading digit or underscore, a dot, non-ASCII letters and digits, and a
# final newline (which "$" in a Python pattern would let through).
BAD_KINDS = [
    "",
    "OutOfStock",
    "out-of-stock",
    "out of stock",
    "1st",
    "_hidden",
    "out.of_stock",
    "caf\u00e9",
    "n\u0661",
    "out_of_stock\n",
]

GOOD_KINDS = ["out_of_stock", "x", "limit2", "a_b_c_1"]

RESERVED_KINDS = [
    "cancelled",
    "unknown",
    "deadline_exceeded",
    "already_exists",
    "resource_exhausted",
    "failed_precondition",
    "aborted",
    "out_of_range",
    "unimplemented",
    "data_loss",
]


class Ping(BaseModel):
    text: str


class OutOfStock(BaseModel):
    sku: str
    available: int


class Expired(BaseModel):
    at: AwareDatetime


class Nothing(BaseModel):
    pass


@contract("inventory.v1")
class Inventory(Protocol):
    @action(idempotent=True, errors={"out_of_stock": OutOfStock})
    def reserve(self, request: Ping) -> None: ...


class Kind(enum.StrEnum):
    CONFLICT = "conflict"
    OUT_OF_STOCK = "out_of_stock"


# The error type


@pytest.mark.spec("CON-25")
def test_error_carries_its_kind_and_message() -> None:
    error = ContractError("conflict", "sku A1 is already reserved")
    assert error.kind == "conflict"
    assert error.message == "sku A1 is already reserved"
    assert error.data is None
    assert str(error) == "conflict: sku A1 is already reserved"
    assert repr(error) == "ContractError('conflict', 'sku A1 is already reserved')"


@pytest.mark.spec("CON-25")
def test_error_is_raised_and_caught_as_one_type() -> None:
    with pytest.raises(ContractError) as caught:
        raise ContractError("not_found", "no order 7")
    assert caught.value.kind == "not_found"


@pytest.mark.spec("CON-25", "CON-29")
@pytest.mark.parametrize("message", [None, 7, b"text", ["text"]])
def test_message_is_text(message: object) -> None:
    with pytest.raises(TypeError, match="message"):
        ContractError("conflict", message)  # type: ignore[arg-type]


@pytest.mark.spec("CON-25", "CON-29")
def test_message_is_required() -> None:
    with pytest.raises(TypeError):
        ContractError("conflict")  # type: ignore[call-arg]


@pytest.mark.spec("CON-22", "CON-26")
@pytest.mark.parametrize("kind", STANDARD_KINDS)
def test_every_standard_kind_is_accepted_without_a_detail(kind: str) -> None:
    assert ContractError(kind, "text").kind == kind


@pytest.mark.spec("CON-22", "CON-29")
@pytest.mark.parametrize("kind", BAD_KINDS)
def test_kind_outside_the_grammar_is_refused(kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        ContractError(kind, "text", {"sku": "A1"})


@pytest.mark.spec("CON-22", "CON-29")
@pytest.mark.parametrize(
    "kind", [None, 7, pytest.param(10**5000, id="huge-int"), b"conflict", Kind.CONFLICT]
)
def test_kind_is_plain_text(kind: object) -> None:
    with pytest.raises(TypeError, match="kind"):
        ContractError(kind, "text")  # type: ignore[arg-type]


@pytest.mark.spec("CON-26", "CON-29")
@pytest.mark.parametrize("kind", STANDARD_KINDS)
@pytest.mark.parametrize("data", [OutOfStock(sku="A1", available=0), {}, {"a": 1}])
def test_standard_kind_carries_no_detail(kind: str, data: object) -> None:
    with pytest.raises(TypeError, match="no detail"):
        ContractError(kind, "text", data)  # type: ignore[arg-type]


@pytest.mark.spec("CON-22")
@pytest.mark.parametrize("kind", GOOD_KINDS)
def test_other_kind_carries_its_detail_record(kind: str) -> None:
    detail = OutOfStock(sku="A1", available=0)
    error = ContractError(kind, "sku A1 is out of stock", detail)
    assert error.kind == kind
    assert error.data is detail
    assert repr(error) == (
        f"ContractError({kind!r}, 'sku A1 is out of stock', "
        "data=OutOfStock(sku='A1', available=0))"
    )


@pytest.mark.spec("CON-30")
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"sku": "A1", "available": 0},
        {"nested": {"list": [1, 2.5, "x", True, None, {"deep": []}]}},
        {"edges": [2**63 - 1, -(2**63), -1.5e300, "￿", "\U0001f600"]},
    ],
)
def test_other_kind_carries_an_untyped_map(data: dict[str, Any]) -> None:
    # The form a caller holds for a kind it does not know (CON-28): the
    # detail's fields as they arrived.
    error = ContractError("added_later", "text", data)
    assert error.data == data
    assert error.data is not data
    assert json.loads(json.dumps(error.data)) == data
    assert json.loads(pydantic_core.to_json(error.data)) == data


@pytest.mark.parametrize("kind", ["resource_exhausted", "added_later"])
def test_unknown_kind_may_come_without_a_detail(kind: str) -> None:
    # A standard kind added by a later version of the specification carries no
    # detail, and a receiver that does not know it holds it as it came (CON-28).
    error = ContractError(kind, "busy")
    assert (error.kind, error.data) == (kind, None)


@pytest.mark.spec("CON-29")
@pytest.mark.parametrize(
    "data",
    [
        "text",
        7,
        [1, 2],
        ("sku", "A1"),
        b"{}",
        OutOfStock,
        Ping.model_fields.items(),
        {"sku": "A1"}.items(),
    ],
    ids=["str", "int", "list", "tuple", "bytes", "class", "field-items", "items"],
)
def test_detail_is_a_record_or_a_map(data: object) -> None:
    with pytest.raises(TypeError, match="detail"):
        ContractError("out_of_stock", "text", data)  # type: ignore[arg-type]


class _Map(dict[str, Any]):
    pass


def _cycle() -> dict[str, Any]:
    inner: dict[str, Any] = {}
    inner["self"] = {"back": inner}
    return {"loop": inner}


def _nest(levels: int) -> dict[str, Any]:
    """A map holding maps, ``levels`` deep in all, the outermost included."""
    outer: dict[str, Any] = {}
    node = outer
    for _ in range(levels - 1):
        node["next"] = {}
        node = node["next"]
    return outer


@pytest.mark.spec("CON-30", "CON-29")
@pytest.mark.parametrize(
    "data",
    [
        {1: "one"},
        {"sku": b"A1"},
        {"at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"price": Decimal("1.5")},
        {"id": UUID(int=1)},
        {"pair": (1, 2)},
        {"set": {1}},
        {"nan": float("nan")},
        {"inf": [float("inf")]},
        {"kind": Kind.CONFLICT},
        {"list": _Map(sku="A1")},
        {"record": OutOfStock(sku="A1", available=0)},
        {"big": 2**63},
        {"small": [-(2**63) - 1]},
        {"huge": 10**5000},
        {"text": "\ud800"},
        {"\udc00": 1},
        {"deep": ["\ud800"]},
        _cycle(),
        _nest(33),
        [_nest(32)],
    ],
    ids=[
        "int-key",
        "bytes",
        "datetime",
        "decimal",
        "uuid",
        "tuple",
        "set",
        "nan",
        "inf",
        "str-enum",
        "dict-subclass",
        "record",
        "int64-above",
        "int64-below",
        "huge-int",
        "surrogate-value",
        "surrogate-key",
        "surrogate-nested",
        "cycle",
        "depth-33",
        "depth-33-in-list",
    ],
)
def test_untyped_map_holds_portable_json_values_only(data: object) -> None:
    with pytest.raises(TypeError, match="untyped"):
        ContractError("out_of_stock", "text", {"wrapped": data})


@pytest.mark.spec("CON-30", "CON-29")
def test_untyped_detail_is_a_plain_dict() -> None:
    with pytest.raises(TypeError, match="detail"):
        ContractError("out_of_stock", "text", _Map(sku="A1"))


def _deep_stack(levels: int, then: Callable[[], object]) -> object:
    return then() if levels == 0 else _deep_stack(levels - 1, then)


@pytest.mark.spec("CON-30")
def test_untyped_map_at_the_depth_limit_copies_pickles_and_writes() -> None:
    at_limit = _nest(32)
    ContractError("out_of_stock", "text", at_limit)
    # The outermost map counts, so wrapping it once more passes the limit.
    with pytest.raises(TypeError, match="32 levels"):
        ContractError("out_of_stock", "text", {"outer": at_limit})
    error = ContractError("out_of_stock", "text", at_limit)
    # With half the recursion limit already in use, as in a deep call stack.
    for operation in (
        lambda: copy.deepcopy(error),
        lambda: pickle.loads(pickle.dumps(error)),  # noqa: S301 - built here
        lambda: pydantic_core.to_json(error.data),
        lambda: json.dumps(error.data),
        lambda: repr(error),
    ):
        _deep_stack(sys.getrecursionlimit() // 2, operation)


@pytest.mark.spec("CON-30")
def test_very_deep_untyped_map_is_refused_without_recursion() -> None:
    with pytest.raises(TypeError, match="32 levels"):
        ContractError("out_of_stock", "text", _nest(100_000))


@pytest.mark.spec("CON-30")
def test_shared_values_are_not_a_cycle() -> None:
    shared = {"sku": "A1"}
    ContractError("out_of_stock", "text", {"a": shared, "b": [shared, shared]})


@pytest.mark.spec("CON-25", "CON-29")
@pytest.mark.parametrize("message", ["\ud800", "ok \udfff"])
def test_message_holds_no_lone_surrogate(message: str) -> None:
    with pytest.raises(ValueError, match="surrogate"):
        ContractError("conflict", message)


@pytest.mark.spec("CON-25")
def test_message_may_hold_any_unicode_scalar() -> None:
    assert ContractError("conflict", "\U0001f600 ￿ \x00").message.startswith(
        "\U0001f600"
    )


def test_untyped_detail_is_a_frozen_copy() -> None:
    data: dict[str, Any] = {"n": 1.0, "items": [{"sku": "A1"}]}
    error = ContractError("out_of_stock", "text", data)
    data["n"] = float("nan")
    data["items"].append({"bad": b"x"})
    data["self"] = data
    assert error.data == {"n": 1.0, "items": [{"sku": "A1"}]}
    held = error.data
    assert isinstance(held, dict)
    for change in (
        lambda: held.__setitem__("n", 2),
        lambda: held.update(n=2),
        lambda: held.pop("n"),
        lambda: held.clear(),
        lambda: held.setdefault("m", 1),
        lambda: held.__delitem__("n"),
        lambda: held["items"].append(1),
        lambda: held["items"].__setitem__(0, 1),
        lambda: held["items"][0].__setitem__("sku", "B2"),
    ):
        with pytest.raises(TypeError, match="read-only"):
            change()
    held.__init__({"n": 3})  # type: ignore[misc]
    assert error.data == {"n": 1.0, "items": [{"sku": "A1"}]}
    assert copy.copy(error).data == error.data
    hash(held)


def test_fields_are_read_only() -> None:
    error = ContractError("out_of_stock", "text", {"sku": "A1"})
    for name in ("kind", "message", "data"):
        with pytest.raises(AttributeError):
            setattr(error, name, "conflict")
    assert (error.kind, error.message) == ("out_of_stock", "text")


def test_error_is_built_once() -> None:
    error = ContractError("not_found", "no order 7")
    with pytest.raises(TypeError, match="already built"):
        error.__init__("internal", "changed")
    assert (error.kind, error.message, error.args) == (
        "not_found",
        "no order 7",
        ("not_found", "no order 7"),
    )


def test_error_type_cannot_be_subclassed() -> None:
    # One type for every error, told apart by kind (ADR 0008, Decision 1). A
    # subclass would be lost on the wire, so callers could tell the modes
    # apart.
    with pytest.raises(TypeError, match="subclass"):

        class OutOfStockError(ContractError):  # type: ignore[misc]
            pass


@pytest.mark.parametrize(
    "build",
    [
        lambda: ContractError("conflict", "text"),
        lambda: ContractError(
            "out_of_stock", "text", OutOfStock(sku="A1", available=0)
        ),
        lambda: ContractError("added_later", "text", {"sku": "A1", "n": [1, None]}),
        lambda: ContractError("resource_exhausted", "busy"),
    ],
    ids=["standard", "record", "map", "unknown-without-detail"],
)
@pytest.mark.parametrize(
    "duplicate",
    # Pickles a value the test built itself, never untrusted data.
    [copy.copy, copy.deepcopy, lambda error: pickle.loads(pickle.dumps(error))],  # noqa: S301
    ids=["copy", "deepcopy", "pickle"],
)
def test_copy_and_pickle_carry_kind_message_and_detail_only(
    build: Callable[[], ContractError], duplicate: Callable[[Any], Any]
) -> None:
    error = build()
    error.add_note("db=10.0.0.5")
    error.provider_detail = "host 7"  # type: ignore[attr-defined]
    again = duplicate(error)
    assert type(again) is ContractError
    assert (again.kind, again.message, again.data) == (
        error.kind,
        error.message,
        error.data,
    )
    assert again.args == error.args
    assert not hasattr(again, "__notes__")
    assert not hasattr(again, "provider_detail")
    if isinstance(again.data, dict):
        with pytest.raises(TypeError, match="read-only"):
            again.data["sku"] = "B2"


# Usage errors


@pytest.mark.spec("CON-29")
def test_usage_errors_and_contract_errors_are_disjoint() -> None:
    assert issubclass(ContractError, Exception)
    for usage in (TypeError, ValueError, ContractDefinitionError):
        assert not issubclass(ContractError, usage)
        assert not issubclass(usage, ContractError)


@pytest.mark.spec("CON-29")
def test_handling_contract_errors_does_not_swallow_a_usage_error() -> None:
    caught: Exception | None = None
    try:
        ContractError("Out Of Stock", "text", {})
    except ContractError:  # pragma: no cover - the point of the test
        pytest.fail("a usage error was caught as a contract error")
    except ValueError as error:
        caught = error
    assert caught is not None
    assert "kind" in str(caught)


@pytest.mark.spec("CON-29")
def test_definition_errors_are_not_contract_errors() -> None:
    with pytest.raises(ContractDefinitionError) as caught:
        contract("Not An Identity")
    assert type(caught.value) is ContractDefinitionError


# Declared error kinds


def _declare(errors: object) -> Contract:
    def place(self: object, request: Ping) -> None: ...

    namespace = {"place": action(errors=errors)(place)}  # type: ignore[call-overload]
    declared = types.new_class(
        "Orders", (Protocol,), exec_body=lambda ns: ns.update(namespace)
    )
    return Contract.of(contract("orders.v1")(declared))


@pytest.mark.spec("CON-8")
def test_action_declares_error_kinds_with_detail_records() -> None:
    @contract("stock.v1")
    class Stock(Protocol):
        @action(idempotent=True, errors={"out_of_stock": OutOfStock})
        def reserve(self, request: Ping) -> None: ...

        @action(errors={"expired": Expired, "out_of_stock": OutOfStock})
        def renew(self, request: Ping) -> None: ...

        @action
        def release(self, request: Ping) -> None: ...

    actions = Contract.of(Stock).actions
    assert dict(actions["reserve"].errors) == {"out_of_stock": OutOfStock}
    assert actions["reserve"].idempotent is True
    assert list(actions["renew"].errors) == ["expired", "out_of_stock"]
    assert dict(actions["release"].errors) == {}


def test_declared_kinds_are_read_only_and_copied() -> None:
    errors: dict[str, type[BaseModel]] = {"out_of_stock": OutOfStock}
    declared = _declare(errors)
    errors["expired"] = Expired
    held = declared.actions["place"].errors
    assert dict(held) == {"out_of_stock": OutOfStock}
    with pytest.raises(TypeError):
        held["expired"] = Expired  # type: ignore[index]


def test_action_with_declared_kinds_stays_hashable() -> None:
    assert hash(_declare({"out_of_stock": OutOfStock}).actions["place"]) is not None


@pytest.mark.parametrize(
    "duplicate",
    [
        copy.copy,
        copy.deepcopy,
        lambda value: pickle.loads(pickle.dumps(value)),  # noqa: S301 - built here
    ],
    ids=["copy", "deepcopy", "pickle"],
)
def test_action_copies_and_pickles(duplicate: Callable[[Any], Any]) -> None:
    # Action held only names, flags and classes before it held error kinds, so
    # it copied and pickled; the kinds keep that working.
    held = Contract.of(Inventory).actions["reserve"]
    again = duplicate(held)
    assert again == held
    assert dict(again.errors) == {"out_of_stock": OutOfStock}
    with pytest.raises(TypeError, match="read-only"):
        cast("dict[str, Any]", again.errors)["expired"] = Expired


def test_action_converts_to_a_dict() -> None:
    held = Contract.of(Inventory).actions["reserve"]
    assert dataclasses.asdict(held)["errors"] == {"out_of_stock": OutOfStock}


@pytest.mark.spec("CON-8")
def test_no_errors_argument_declares_no_kinds() -> None:
    assert dict(_declare(None).actions["place"].errors) == {}
    assert dict(_declare({}).actions["place"].errors) == {}


@pytest.mark.spec("CON-8", "CON-17")
@pytest.mark.parametrize(
    "errors",
    [
        [OutOfStock],
        ("out_of_stock", OutOfStock),
        {OutOfStock},
        OutOfStock,
        "out_of_stock",
        _Map(out_of_stock=OutOfStock),
    ],
    ids=["list", "tuple", "set", "class", "str", "dict-subclass"],
)
def test_declared_kinds_are_a_dict(errors: object) -> None:
    with pytest.raises(ContractDefinitionError, match=r"orders\.v1, action 'place'"):
        _declare(errors)


@pytest.mark.spec("CON-22", "CON-17")
@pytest.mark.parametrize("kind", BAD_KINDS)
def test_declared_kind_outside_the_grammar_is_rejected(kind: str) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(f"orders.v1, action 'place': error kind {kind!r}"),
    ):
        _declare({kind: OutOfStock})


@pytest.mark.spec("CON-22", "CON-17")
@pytest.mark.parametrize(
    "kind", [7, pytest.param(10**5000, id="huge-int"), None, b"gone", Kind.OUT_OF_STOCK]
)
def test_declared_kind_is_plain_text(kind: object) -> None:
    with pytest.raises(ContractDefinitionError, match="error kind"):
        _declare({kind: OutOfStock})


@pytest.mark.spec("CON-22")
@pytest.mark.parametrize("kind", GOOD_KINDS)
def test_declared_kind_in_the_grammar_is_accepted(kind: str) -> None:
    assert dict(_declare({kind: OutOfStock}).actions["place"].errors) == {
        kind: OutOfStock
    }


@pytest.mark.spec("CON-23", "CON-17")
@pytest.mark.parametrize("kind", STANDARD_KINDS)
def test_declared_kind_is_not_a_standard_kind(kind: str) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(f"orders.v1, action 'place': error kind {kind!r}")
        + ".*standard kind",
    ):
        _declare({kind: OutOfStock})


@pytest.mark.spec("CON-23", "CON-17")
@pytest.mark.parametrize("kind", RESERVED_KINDS)
def test_declared_kind_is_not_a_reserved_name(kind: str) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(f"orders.v1, action 'place': error kind {kind!r}")
        + ".*reserved",
    ):
        _declare({kind: OutOfStock})


@pytest.mark.spec("CON-23")
def test_reserved_names_are_not_standard_kinds() -> None:
    assert not set(RESERVED_KINDS) & set(STANDARD_KINDS)


@pytest.mark.spec("CON-23")
def test_declared_kind_may_extend_a_standard_name() -> None:
    errors = {"not_found_yet": Nothing, "conflicts": Nothing}
    assert list(_declare(errors).actions["place"].errors) == list(errors)


@dataclasses.dataclass
class _Plain:
    sku: str


class _Typed(TypedDict):
    sku: str


@pytest.mark.spec("CON-24", "CON-17")
@pytest.mark.parametrize(
    "detail",
    [
        None,
        int,
        dict,
        _Plain,
        _Typed,
        RootModel[int],
        OutOfStock(sku="A1", available=0),
        "OutOfStock",
    ],
    ids=[
        "none",
        "int",
        "dict",
        "dataclass",
        "typeddict",
        "root-model",
        "instance",
        "name",
    ],
)
def test_detail_is_a_record(detail: object) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape("orders.v1, action 'place', error 'gone': "),
    ):
        _declare({"gone": detail})


@pytest.mark.spec("CON-24", "CON-17")
def test_detail_rejection_says_a_record_is_needed() -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(
            "orders.v1, action 'place', error 'gone': the detail type must be a "
            "record (a pydantic model with named fields), not int"
        ),
    ):
        _declare({"gone": int})


@pytest.mark.spec("CON-24", "CON-17")
@pytest.mark.parametrize("annotation", [UUID, set[str], tuple[int, int]])
def test_detail_fields_use_the_portable_forms(annotation: object) -> None:
    detail = type("Gone", (BaseModel,), {"__annotations__": {"value": annotation}})
    with pytest.raises(ContractDefinitionError) as caught:
        _declare({"gone": detail})
    message = str(caught.value)
    assert message.startswith("orders.v1, action 'place', error 'gone': ")
    assert "value" in message


@pytest.mark.spec("CON-21")
@pytest.mark.parametrize("annotation", [Decimal, list[Decimal], Decimal | None])
def test_decimal_in_a_detail_record_is_refused(annotation: object) -> None:
    detail = type("Money", (BaseModel,), {"__annotations__": {"amount": annotation}})
    with pytest.raises(ContractDefinitionError, match="decimal") as caught:
        _declare({"over_limit": detail})
    assert str(caught.value).startswith(
        "orders.v1, action 'place', error 'over_limit': "
    )


@pytest.mark.spec("CON-24", "CON-17")
def test_detail_record_shares_the_contracts_names() -> None:
    # Another record named Ping, used as a detail, collides with the request's
    # Ping (CON-15).
    other = type("Ping", (BaseModel,), {"__annotations__": {"code": int}})
    with pytest.raises(ContractDefinitionError, match="two different records"):
        _declare({"gone": other})


@pytest.mark.spec("CON-24", "CON-17")
def test_detail_record_name_is_portable() -> None:
    odd = type("Gone\u00e9", (BaseModel,), {"__annotations__": {"code": int}})
    with pytest.raises(ContractDefinitionError, match="must start with an ASCII"):
        _declare({"gone": odd})


@pytest.mark.spec("CON-24", "CON-17")
def test_detail_record_ignores_unknown_fields() -> None:
    strict = type(
        "Strict",
        (BaseModel,),
        {
            "__annotations__": {"code": int},
            "model_config": ConfigDict(extra="forbid"),
        },
    )
    with pytest.raises(ContractDefinitionError, match="unknown fields"):
        _declare({"gone": strict})


@pytest.mark.spec("CON-24")
def test_detail_record_may_be_shared_and_empty() -> None:
    @contract("stock.v1")
    class Stock(Protocol):
        @action(errors={"gone": Ping, "empty": Nothing, "also_gone": Ping})
        def reserve(self, request: Ping) -> Ping: ...

        @action(errors={"gone": Ping})
        def release(self, request: Ping) -> None: ...

    description = Contract.of(Stock).describe()
    assert set(description["$defs"]) == {"Ping", "Nothing"}


# Description


@pytest.mark.spec("CON-9", "CON-16")
def test_description_gives_each_declared_kind_and_its_detail() -> None:
    @contract("stock.v1")
    class Stock(Protocol):
        @action(idempotent=True, errors={"out_of_stock": OutOfStock})
        def reserve(self, request: Ping) -> None: ...

        @action
        def release(self, request: Ping) -> None: ...

    description = Contract.of(Stock).describe()
    assert description["actions"] == {
        "reserve": {
            "idempotent": True,
            "request": {"$ref": "#/$defs/Ping"},
            "reply": None,
            "errors": {"out_of_stock": {"$ref": "#/$defs/OutOfStock"}},
        },
        "release": {
            "idempotent": False,
            "request": {"$ref": "#/$defs/Ping"},
            "reply": None,
            "errors": {},
        },
    }
    assert description["$defs"]["OutOfStock"] == {
        "type": "object",
        "properties": {
            "sku": {"type": "string"},
            "available": {"type": "integer"},
        },
        "required": ["sku", "available"],
    }
    json.dumps(description)


@pytest.mark.spec("CON-16")
def test_description_does_not_list_the_standard_kinds() -> None:
    description = _declare({"out_of_stock": OutOfStock}).describe()
    listed = description["actions"]["place"]["errors"]
    assert list(listed) == ["out_of_stock"]
    assert not set(listed) & set(STANDARD_KINDS)


# Marks


@pytest.mark.parametrize(
    "stack",
    [
        [action(idempotent=True), action(errors={"out_of_stock": OutOfStock})],
        [action(errors={"out_of_stock": OutOfStock}), action(idempotent=True)],
        [action, action(idempotent=True)],
        [action(errors={"a_kind": Nothing}), action(errors={"b_kind": Nothing})],
    ],
    ids=["errors-under-idempotent", "idempotent-under-errors", "bare", "two-sets"],
)
def test_action_marks_a_method_once(stack: list[Callable[[Any], Any]]) -> None:
    # Two marks would keep only the outer one's settings, silently.
    def reserve(self: object, request: Ping) -> None: ...

    inner, outer = stack[1], stack[0]
    marked = inner(reserve)
    with pytest.raises(ContractDefinitionError, match="twice"):
        outer(marked)


@pytest.mark.parametrize(
    "mark",
    [
        action(errors={"Not A Kind!": int}),  # type: ignore[dict-item]
        action(errors={"out_of_stock": OutOfStock}),
        action(idempotent=True),
        action,
    ],
    ids=["invalid-errors", "same-errors", "idempotent", "bare"],
)
def test_implementation_methods_are_not_marked(mark: Callable[[Any], Any]) -> None:
    # The contract declares actions; a mark on the implementation would be
    # ignored, so it is refused rather than read as a declaration.
    def reserve(self: object, request: Ping) -> None: ...

    impl = type("InventoryImpl", (), {"reserve": mark(reserve)})
    with pytest.raises(ContractDefinitionError, match="marked with @action"):
        implements(Inventory)(impl)


def test_implementation_helper_is_not_marked_either() -> None:
    def reserve(self: object, request: Ping) -> None: ...

    def _helper(self: object, request: Ping) -> None: ...

    impl = type("InventoryImpl", (), {"reserve": reserve, "_helper": action(_helper)})
    with pytest.raises(ContractDefinitionError, match="'_helper' is marked"):
        implements(Inventory)(impl)


def test_unmarked_implementation_is_accepted() -> None:
    def reserve(self: object, request: Ping) -> None: ...

    impl = type("InventoryImpl", (), {"reserve": reserve})
    assert implements(Inventory)(impl) is impl
