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
import types
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, TypedDict
from uuid import UUID

import pytest
from pydantic import AwareDatetime, BaseModel, ConfigDict, RootModel

from neolith import (
    Contract,
    ContractDefinitionError,
    ContractError,
    action,
    contract,
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


class Ping(BaseModel):
    text: str


class OutOfStock(BaseModel):
    sku: str
    available: int


class Expired(BaseModel):
    at: AwareDatetime


class Nothing(BaseModel):
    pass


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
@pytest.mark.parametrize("kind", [None, 7, b"conflict", Kind.CONFLICT])
def test_kind_is_plain_text(kind: object) -> None:
    with pytest.raises(TypeError, match="kind"):
        ContractError(kind, "text")  # type: ignore[arg-type]


@pytest.mark.spec("CON-26", "CON-29")
@pytest.mark.parametrize("kind", STANDARD_KINDS)
@pytest.mark.parametrize("data", [OutOfStock(sku="A1", available=0), {}, {"a": 1}])
def test_standard_kind_carries_no_detail(kind: str, data: object) -> None:
    with pytest.raises(TypeError, match="no detail"):
        ContractError(kind, "text", data)  # type: ignore[arg-type]


@pytest.mark.spec("CON-22", "CON-27")
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


@pytest.mark.spec("CON-27")
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"sku": "A1", "available": 0},
        {"nested": {"list": [1, 2.5, "x", True, None, {"deep": []}]}},
        {"big": 2**80, "negative": -1.5e300},
    ],
)
def test_other_kind_carries_an_untyped_map(data: dict[str, Any]) -> None:
    # The form a caller receives for a kind its version of the contract does
    # not declare (CON-28): the detail's fields as they arrived.
    error = ContractError("added_later", "text", data)
    assert error.data is data


@pytest.mark.spec("CON-27", "CON-29")
def test_other_kind_needs_a_detail() -> None:
    with pytest.raises(TypeError, match="detail"):
        ContractError("out_of_stock", "text")


@pytest.mark.spec("CON-27", "CON-29")
@pytest.mark.parametrize(
    "data",
    [
        "text",
        7,
        [1, 2],
        ("sku", "A1"),
        b"{}",
        OutOfStock,
        Ping.model_fields,
        {"sku": "A1"}.items(),
    ],
    ids=["str", "int", "list", "tuple", "bytes", "class", "mapping", "items"],
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


@pytest.mark.spec("CON-27", "CON-29")
@pytest.mark.parametrize(
    "data",
    [
        _Map(sku="A1"),
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
        {"record": OutOfStock(sku="A1", available=0)},
        _cycle(),
    ],
    ids=[
        "dict-subclass",
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
        "record",
        "cycle",
    ],
)
def test_untyped_map_holds_json_values_only(data: dict[Any, Any]) -> None:
    with pytest.raises(TypeError, match="untyped"):
        ContractError("out_of_stock", "text", data)


@pytest.mark.spec("CON-27")
def test_deeply_nested_untyped_map_is_checked_without_recursion() -> None:
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(5000):
        node["next"] = {}
        node = node["next"]
    ContractError("out_of_stock", "text", deep)
    node["bad"] = b"x"
    with pytest.raises(TypeError, match="untyped"):
        ContractError("out_of_stock", "text", deep)


@pytest.mark.spec("CON-27")
def test_shared_values_are_not_a_cycle() -> None:
    shared = {"sku": "A1"}
    ContractError("out_of_stock", "text", {"a": shared, "b": [shared, shared]})


def test_fields_are_read_only() -> None:
    error = ContractError("out_of_stock", "text", {"sku": "A1"})
    for name in ("kind", "message", "data"):
        with pytest.raises(AttributeError):
            setattr(error, name, "conflict")
    assert (error.kind, error.message) == ("out_of_stock", "text")


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
    ],
    ids=["standard", "record", "map"],
)
@pytest.mark.parametrize(
    "duplicate",
    # Pickles a value the test built itself, never untrusted data.
    [copy.copy, copy.deepcopy, lambda error: pickle.loads(pickle.dumps(error))],  # noqa: S301
    ids=["copy", "deepcopy", "pickle"],
)
def test_error_survives_copy_and_pickle(
    build: Callable[[], ContractError], duplicate: Callable[[Any], Any]
) -> None:
    error = build()
    error.add_note("logged with trace 4bf92f")
    again = duplicate(error)
    assert type(again) is ContractError
    assert (again.kind, again.message, again.data) == (
        error.kind,
        error.message,
        error.data,
    )
    assert again.__notes__ == ["logged with trace 4bf92f"]


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
@pytest.mark.parametrize("kind", [7, None, b"gone", Kind.OUT_OF_STOCK])
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
@pytest.mark.parametrize("annotation", [UUID, Decimal, set[str], tuple[int, int]])
def test_detail_fields_use_the_portable_forms(annotation: object) -> None:
    detail = type("Gone", (BaseModel,), {"__annotations__": {"value": annotation}})
    with pytest.raises(ContractDefinitionError) as caught:
        _declare({"gone": detail})
    message = str(caught.value)
    assert message.startswith("orders.v1, action 'place', error 'gone': ")
    assert "value" in message


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
