"""Regression tests from the pre-merge red-team pass on #17 (probes p1 to p9).

Each probe either reproduced a value that travels against its description
(CON-16) or an error that escaped without naming the contract (CON-17). The
shapes are kept here as they were found, next to the nearest shape that must
still be accepted.
"""

import enum
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
    BeforeValidator,
    ConfigDict,
    Field,
    GetPydanticSchema,
    StringConstraints,
    create_model,
    model_validator,
)
from pydantic_core import core_schema

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
        pytest.param(Bumped, "has a validator on a record", id="p7 model validator"),
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
    """p2: a recursive record round-trips; with a before validator it is refused."""

    children: list["Tree"] = []


class GuardedTree(BaseModel):
    children: list["GuardedTree"] = []

    @model_validator(mode="before")
    @classmethod
    def accept(cls, data: object) -> object:
        return data


@pytest.mark.spec("CON-16")
def test_recursive_record_round_trips() -> None:
    schema = _declare(Tree)
    sent = Tree.model_validate_json('{"children":[{"children":[]}]}').model_dump_json()
    Draft202012Validator(schema).validate(json.loads(sent))


@pytest.mark.spec("CON-16", "CON-17")
def test_recursive_record_with_a_before_validator_is_refused() -> None:
    with pytest.raises(ContractDefinitionError, match="has a validator on a record"):
        _declare(GuardedTree)


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


# Second pass (at 32c9e1b).


def _same(value: object) -> object:
    return value


class Positive(BaseModel):
    n: Annotated[int, Field(gt=0)]


def _constructed(value: object) -> object:
    return Positive.model_construct(n=-5)


def _assigned(value: object) -> object:
    made = Positive(n=1)
    made.n = -7
    return made


class ConstructedField(BaseModel):
    """p01 P2a: a before validator returning model_construct on a record field."""

    inner: Annotated[Positive, BeforeValidator(_constructed)]


class AssignedField(BaseModel):
    """p01 P2c: a before validator returning a record changed after validation."""

    inner: Annotated[Positive, BeforeValidator(_assigned)]


class ConstructedSelf(BaseModel):
    """p01 P2b: a before model_validator returning model_construct."""

    n: Annotated[int, Field(gt=0)]

    @model_validator(mode="before")
    @classmethod
    def build(cls, data: object) -> object:
        return cls.model_construct(n=-5)


type QuietKey = Annotated[str, BeforeValidator(_same)]


class KeyBehindValidator(BaseModel):
    """p01 P1b: a converted key behind a before validator."""

    model_config = ConfigDict(str_to_lower=True)
    m: Annotated[dict[Annotated[str, BeforeValidator(_same)], int], Field(min_length=2)]


class KeyStrippedBehindValidator(BaseModel):
    """p01 P1d: a stripped key behind a before validator."""

    model_config = ConfigDict(str_strip_whitespace=True)
    m: dict[Annotated[str, BeforeValidator(_same)], int]


class KeyThroughAlias(BaseModel):
    """p10 U2: a converted key behind a before validator in a type alias."""

    model_config = ConfigDict(str_to_lower=True)
    m: dict[QuietKey, int]


class FloatPast53(BaseModel):
    """p03: a float bound a float cannot hold."""

    x: Annotated[float, Field(lt=2**53 + 1)]


class FloatInt64(BaseModel):
    """p13: the int64 maximum as a float bound."""

    value: Annotated[float, Field(le=2**63 - 1)]


class Price(BaseModel):
    """p04: a decimal, whose accepted forms no pattern states exactly."""

    amount: Decimal


class Precise(BaseModel):
    """p08: microseconds_precision="error" refuses what RFC 3339 allows."""

    t: Annotated[
        AwareDatetime,
        GetPydanticSchema(
            lambda t, h: core_schema.datetime_schema(
                tz_constraint="aware", microseconds_precision="error"
            )
        ),
    ]


class Empty(enum.StrEnum):
    pass


class Nothing(BaseModel):
    """p07: an enum with no members."""

    c: Empty | None = None


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("record", "message"),
    [
        pytest.param(
            ConstructedField, "has a validator on a record", id="p01 construct"
        ),
        pytest.param(AssignedField, "has a validator on a record", id="p01 assigned"),
        pytest.param(ConstructedSelf, "has a validator on a record", id="p01 model"),
        pytest.param(KeyBehindValidator, "converts its keys", id="p01 key lower"),
        pytest.param(
            KeyStrippedBehindValidator, "converts its keys", id="p01 key strip"
        ),
        pytest.param(KeyThroughAlias, "converts its keys", id="p10 key alias"),
        pytest.param(
            FloatPast53, "which a float cannot hold exactly", id="p03 2**53+1"
        ),
        pytest.param(FloatInt64, "which a float cannot hold exactly", id="p13 int64"),
        pytest.param(Price, "is a decimal", id="p04 decimal"),
        pytest.param(Precise, "beyond microseconds", id="p08 precision"),
        pytest.param(Nothing, "has type is-instance", id="p07 empty enum"),
    ],
)
def test_second_pass_shapes_are_refused(record: type[BaseModel], message: str) -> None:
    with pytest.raises(ContractDefinitionError, match=message):
        _declare(record)


class OwnLengthKey(BaseModel):
    """p01 P1c: a key with its own length behind a before validator, no conversion."""

    m: dict[Annotated[str, Field(max_length=5), BeforeValidator(_same)], int]


class ExactFloatBounds(BaseModel):
    """p03: float bounds a float holds exactly, and a large integer bound."""

    x: Annotated[float, Field(gt=0.1, lt=2**53)]
    n: Annotated[int, Field(le=2**63)]


class Repeated(enum.StrEnum):
    """p07, p08: an alias member repeats a value."""

    A = "a"
    B = "a"


class WithRepeated(BaseModel):
    c: Repeated


@pytest.mark.spec("CON-16")
def test_second_pass_nearest_shapes_are_accepted() -> None:
    _declare(OwnLengthKey)
    bounds = _declare(ExactFloatBounds)["$defs"]["ExactFloatBounds"]["properties"]
    assert bounds["x"] == {
        "type": "number",
        "exclusiveMinimum": 0.1,
        "exclusiveMaximum": 2**53,
    }
    assert _declare(WithRepeated)["$defs"]["Repeated"] == {
        "type": "string",
        "enum": ["a"],
    }


@pytest.mark.spec("CON-19")
def test_public_types_on_an_implementation_are_accepted() -> None:
    class Meta:
        pass

    impl = type(
        "Impl",
        (),
        {"get": _get, "Meta": Meta, "NotFound": KeyError, "Kind": Repeated},
    )
    assert implements(Inventory)(impl) is impl
