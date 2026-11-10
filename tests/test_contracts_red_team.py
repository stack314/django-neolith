"""Regression tests from the pre-merge red-team pass on #17 (probes p1 to p9).

Each probe either reproduced a value that travels against its description
(CON-16) or an error that escaped without naming the contract (CON-17). The
shapes are kept here as they were found, next to the nearest shape that must
still be accepted.
"""

import functools
import json
import types
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Protocol, cast
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    create_model,
    model_validator,
)

from neolith import Contract, ContractDefinitionError, action, contract, implements


def _declare(record: object) -> dict[str, Any]:
    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    namespace = {"send": action(send)}
    declared = types.new_class(
        "Probe", (Protocol,), exec_body=lambda ns: ns.update(namespace)
    )
    contract("probe.v1")(declared)
    described = Contract.of(declared).describe()
    name = cast("type[BaseModel]", record).__name__
    return {"$defs": described["$defs"], "$ref": f"#/$defs/{name}"}


def _identity(value: object) -> object:
    return value


class Upper(BaseModel):
    """p1: a case conversion split from its pattern by a validator."""

    s: Annotated[
        str,
        Field(pattern=r"^[a-z]+$"),
        AfterValidator(_identity),
        StringConstraints(to_upper=True),
    ]


class Bound(BaseModel):
    """p1: an after validator taking a value past its stated bound."""

    n: Annotated[int, Field(ge=0, le=10), AfterValidator(lambda v: v * 100)]


class Digits(BaseModel):
    """p1: a decimal constraint after a validator, which Pydantic moves into one."""

    d: Annotated[Decimal, AfterValidator(_identity), Field(max_digits=3)]


class Initialised(BaseModel):
    """p1: a custom __init__ setting a value past its bound."""

    x: Annotated[int, Field(ge=0, le=10)]

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        self.x = 100


class Bumped(BaseModel):
    """p7: an after model_validator setting a value past its bound."""

    x: Annotated[int, Field(le=10)]

    @model_validator(mode="after")
    def bump(self) -> "Bumped":
        self.x = 99
        return self


class Collapsing(BaseModel):
    """p2: keys that collapse under str_to_lower, below a stated min_length."""

    model_config = ConfigDict(str_to_lower=True)
    m: Annotated[dict[str, int], Field(min_length=2)]


class Stripped(BaseModel):
    """p2, p8, p9: stripping before a length check refuses "a "."""

    s: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2)]


class Anchored(BaseModel):
    """p8, p9: "$" before a final newline differs between Python re and Rust."""

    s: Annotated[str, StringConstraints(pattern=r"^[a-z]*$")]


class Unicode(BaseModel):
    """p3: Rust-only pattern syntax."""

    s: Annotated[str, Field(pattern=r"^\p{L}+$")]


class Stepped(BaseModel):
    """p3, p6: float multiple_of has a tolerance in pydantic-core only."""

    f: Annotated[float, Field(multiple_of=0.1)]


class StrictCount(BaseModel):
    """p3: a strict integer refuses JSON 1.0."""

    model_config = ConfigDict(strict=True)
    n: int


class Hidden(BaseModel):
    """p3: constraints after a validator, missing from the description."""

    n: Annotated[int, AfterValidator(_identity), Field(ge=0)]


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("record", "message"),
    [
        pytest.param(Upper, "has type chain", id="p1 case after validator"),
        pytest.param(Bound, "has an after or wrap validator", id="p1 after validator"),
        pytest.param(Digits, "has an after or wrap validator", id="p1 decimal"),
        pytest.param(Initialised, "defines __init__", id="p1 custom init"),
        pytest.param(Bumped, "has an after or wrap validator", id="p7 model validator"),
        pytest.param(Collapsing, "converts its keys", id="p2 collapsing keys"),
        pytest.param(Stripped, "converts its value", id="p2 strip then length"),
        pytest.param(Anchored, "sets a pattern", id="p8 anchor"),
        pytest.param(Unicode, "sets a pattern", id="p3 unicode property"),
        pytest.param(Stepped, "multiple_of on a float", id="p3 float multiple_of"),
        pytest.param(StrictCount, "reads in strict mode", id="p3 strict integer"),
        pytest.param(
            Hidden, "has an after or wrap validator", id="p3 hidden constraint"
        ),
    ],
)
def test_red_team_shapes_are_refused(record: type[BaseModel], message: str) -> None:
    with pytest.raises(ContractDefinitionError, match=message):
        _declare(record)


class Tree(BaseModel):
    """p2: a recursive record with a before model_validator is still accepted."""

    children: list["Tree"] = []

    @model_validator(mode="before")
    @classmethod
    def accept(cls, data: object) -> object:
        return data


@pytest.mark.spec("CON-16")
def test_recursive_record_with_a_before_validator_round_trips() -> None:
    schema = _declare(Tree)
    sent = Tree.model_validate_json('{"children":[{"children":[]}]}').model_dump_json()
    Draft202012Validator(schema).validate(json.loads(sent))


# p4: a timestamp whose offset has seconds, which RFC 3339 cannot write.


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    "zone",
    [
        pytest.param(timezone(timedelta(seconds=30)), id="30 seconds"),
        pytest.param(ZoneInfo("Europe/Amsterdam"), id="Amsterdam 1900, +00:19:32"),
    ],
)
def test_default_whose_offset_has_seconds_is_refused(zone: Any) -> None:  # noqa: ANN401
    default = datetime(1900, 1, 1, 12, 0, tzinfo=zone)
    record = create_model("Stamped", t=(AwareDatetime, default))
    with pytest.raises(ContractDefinitionError, match="the wire loses part of it"):
        _declare(record)


@pytest.mark.spec("CON-16")
def test_default_whose_offset_is_whole_minutes_is_accepted() -> None:
    default = datetime(
        2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    record = create_model("Stamped", t=(AwareDatetime, default))
    schema = _declare(record)
    assert schema["$defs"]["Stamped"]["properties"]["t"]["default"] == (
        "2026-01-01T12:00:00+05:30"
    )
    assert default.astimezone(UTC) == datetime(2026, 1, 1, 6, 30, tzinfo=UTC)


# p3, p7: errors from an author's code are named (CON-17).


def _raises(error: Exception) -> object:
    def check(value: object) -> object:
        raise error

    return check


@pytest.mark.spec("CON-17")
@pytest.mark.parametrize(
    "error",
    [RuntimeError("validator bug"), KeyError("lookup")],
    ids=["RuntimeError", "KeyError"],
)
def test_validator_raising_on_a_default_is_named(error: Exception) -> None:
    record = create_model(
        "Raising",
        x=(cast("Any", Annotated[int, AfterValidator(cast("Any", _raises(error)))]), 1),
    )
    with pytest.raises(ContractDefinitionError, match=r"probe\.v1, action 'send'"):
        _declare(record)


class Request(BaseModel):
    x: int


@pytest.mark.spec("CON-17")
def test_unparsable_annotation_in_a_declaration_is_named() -> None:
    def send(self: object, request: "Request[") -> None: ...  # type: ignore[valid-type]  # noqa: F722

    namespace = {"send": action(send)}
    declared = types.new_class(
        "Broken", (Protocol,), exec_body=lambda ns: ns.update(namespace)
    )
    with pytest.raises(ContractDefinitionError, match="cannot read its annotations"):
        contract("broken.v1")(declared)


@contract("inventory.v1")
class Inventory(Protocol):
    @action
    def get(self, request: Request) -> None: ...


@pytest.mark.spec("CON-19")
def test_unparsable_annotation_in_an_implementation_is_named() -> None:
    class Impl:
        def get(self, request: "Request[") -> None:  # type: ignore[valid-type]  # noqa: F722
            return None

    with pytest.raises(ContractDefinitionError, match="cannot read its annotations"):
        implements(Inventory)(Impl)


def _get(self: object, request: Request) -> None:
    return None


def _extra(self: object, value: object) -> None:
    return None


@pytest.mark.spec("CON-19")
@pytest.mark.parametrize(
    "member",
    [
        pytest.param(functools.partialmethod(_get), id="partialmethod"),
        pytest.param(functools.singledispatchmethod(_extra), id="singledispatchmethod"),
    ],
)
def test_public_method_like_attribute_is_refused(member: object) -> None:
    impl = type("Impl", (), {"get": _get, "helper": member})
    with pytest.raises(ContractDefinitionError, match="'helper' is not an action"):
        implements(Inventory)(impl)


@pytest.mark.spec("CON-19")
def test_public_property_and_constant_are_accepted() -> None:
    impl = type(
        "Impl", (), {"get": _get, "limit": 3, "name": property(lambda self: "x")}
    )
    assert implements(Inventory)(impl) is impl
