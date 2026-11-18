"""Contracts, actions, the portable types and implementation checks (spec 01).

Everything here happens when a module defining a contract or an
implementation is loaded, before any transport exists, so these tests run
without one (T2 does not apply yet).
"""

import contextlib
import dataclasses
import enum
import functools
import inspect
import json
import re
import types
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Optional,
    Protocol,
    TypeAliasType,
    TypedDict,
    cast,
)
from uuid import UUID

import annotated_types
import pytest
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    GetPydanticSchema,
    ModelWrapValidatorHandler,
    PlainSerializer,
    PlainValidator,
    PrivateAttr,
    RootModel,
    SerializeAsAny,
    Strict,
    StringConstraints,
    ValidationInfo,
    WithJsonSchema,
    WrapSerializer,
    WrapValidator,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

from neolith import (
    Action,
    Binary,
    Contract,
    ContractDefinitionError,
    Decimal128,
    Duration,
    action,
    contract,
    implements,
)

# The descriptions of an integer and a number, with the range each form has on
# the wire (spec 01, ADR 0019).
INTEGER = {"type": "integer", "minimum": -(2**53 - 1), "maximum": 2**53 - 1}
NUMBER_RANGE = {"minimum": -1.7976931348623157e308, "maximum": 1.7976931348623157e308}


class Ping(BaseModel):
    text: str


class Pong(BaseModel):
    text: str
    count: int = 0


class Colour(enum.StrEnum):
    RED = "red"
    GREEN = "green"


class Level(enum.IntEnum):
    LOW = 1


class Line(BaseModel):
    sku: str
    quantity: int


class Node(BaseModel):
    name: str
    children: list["Node"] = []


def declaration(name: str, namespace: Mapping[str, object]) -> type[Any]:
    """Build a contract declaration class from a namespace."""
    return types.new_class(name, (Protocol,), exec_body=lambda ns: ns.update(namespace))


# Records written top-down, as with quoted annotations or
# "from __future__ import annotations": TopDown refers to Below, defined after
# it, so Pydantic leaves TopDown incomplete until it is rebuilt.
class TopDown(BaseModel):
    below: "Below"
    many: list["Below"] = []


class Below(BaseModel):
    value: int


# Refers to a name that only the checker's own locals would supply.
class Sneaky(BaseModel):
    x: "path"  # type: ignore[name-defined]  # noqa: F821


# Type aliases. Recursive ones are accepted by Pydantic but have no end when
# expanded; a quoted name inside one is never resolved.
type Nested = list[Nested]
type Tree = dict[str, Tree]
type Ping1 = list[Pong1]
type Pong1 = list[Ping1]
type QuotedBelow = list["Below"]
type BelowList = list[Below]
type Ints = list[int]
type IntPairs = dict[str, Ints]


class Page[T](BaseModel):
    items: list[T]


class Point(TypedDict):
    x: int


type Quantity = Annotated[int, Field(ge=0)]


def declare(identity: str = "echo.v1") -> type[Any]:
    @contract(identity)
    class Echo(Protocol):
        @action
        def ping(self, request: Ping) -> Pong: ...

    return Echo


def declare_with_field(annotation: object, identity: str = "shapes.v1") -> type[Any]:
    """Declare a contract whose request has one field of the given type."""
    request = create_model("Shape", value=(cast("Any", annotation), ...))

    @contract(identity)
    class Shapes(Protocol):
        @action
        def send(self, request: request) -> None:  # type: ignore[valid-type]
            ...

    return Shapes


# Identity


@pytest.mark.spec("CON-10")
@pytest.mark.parametrize(
    "identity",
    ["inventory.v1", "billing.invoices.v2", "a1_b.c2.v10", "x.v123", "x_.v1"],
)
def test_identity_in_the_grammar_is_accepted(identity: str) -> None:
    assert Contract.of(declare(identity)).identity == identity


@pytest.mark.spec("CON-10", "CON-17")
@pytest.mark.parametrize(
    "identity",
    [
        "",
        "inventory",
        "inventory.v0",
        "inventory.v01",
        "inventory.V1",
        "Inventory.v1",
        "inventory.v1 ",
        "inventory.v1\n",
        ".v1",
        "inventory..stock.v1",
        "inventory-stock.v1",
        "1inventory.v1",
        "_inventory.v1",
        "inventory.v-1",
        "inventory.vone",
        "invent\u00f6ry.v1",
        "inventory.v\u0661",
    ],
)
def test_identity_outside_the_grammar_is_rejected(identity: str) -> None:
    with pytest.raises(ContractDefinitionError, match="contract identity"):
        contract(identity)


@pytest.mark.spec("CON-10")
def test_identity_is_split_into_name_and_major() -> None:
    spec = Contract.of(declare("billing.invoices.v12"))
    assert (spec.name, spec.major) == ("billing.invoices", 12)
    assert repr(spec) == "<Contract billing.invoices.v12>"


@pytest.mark.spec("CON-3")
def test_several_major_versions_of_one_name_load_side_by_side() -> None:
    first, second = Contract.of(declare("echo.v1")), Contract.of(declare("echo.v2"))
    assert (first.name, first.major) == ("echo", 1)
    assert (second.name, second.major) == ("echo", 2)


def test_decorated_class_is_returned_unchanged() -> None:
    class Echo(Protocol):
        @action
        def ping(self, request: Ping) -> Pong: ...

    # mypy treats a Protocol class as abstract when it is passed as an argument
    # (python/mypy#4717); as a decorator, @contract is not flagged.
    assert contract("echo.v1")(Echo) is Echo  # type: ignore[type-abstract]


def test_declaration_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        declare()()


# Actions


def test_actions_are_read_in_declaration_order() -> None:
    @contract("orders.v1")
    class Orders(Protocol):
        @action(idempotent=True)
        def place(self, request: Ping) -> Pong: ...

        @action
        def cancel(self, request: Ping) -> None: ...

    actions = Contract.of(Orders).actions
    assert list(actions) == ["place", "cancel"]
    assert actions["place"] == Action("place", True, Ping, Pong, {})
    assert actions["cancel"] == Action("cancel", False, Ping, None, {})


def test_actions_are_non_idempotent_unless_declared() -> None:
    @contract("flags.v1")
    class Flags(Protocol):
        @action
        def bare(self, request: Ping) -> None: ...

        @action()
        def called(self, request: Ping) -> None: ...

        @action(idempotent=False)
        def explicit(self, request: Ping) -> None: ...

        @action(idempotent=True)
        def idempotent(self, request: Ping) -> None: ...

    flags = {n: a.idempotent for n, a in Contract.of(Flags).actions.items()}
    assert flags == {
        "bare": False,
        "called": False,
        "explicit": False,
        "idempotent": True,
    }


def test_idempotent_takes_a_bool_only() -> None:
    with pytest.raises(ContractDefinitionError, match="idempotent"):
        action(idempotent=1)  # type: ignore[call-overload]


def test_action_marks_functions_only() -> None:
    with pytest.raises(ContractDefinitionError, match="marks a function"):
        action(staticmethod(lambda request: None))


@pytest.mark.spec("CON-11", "CON-17")
@pytest.mark.parametrize("name", ["Ping", "pingPong", "_ping", "ping_\u00e9"])
def test_action_name_outside_the_grammar_is_rejected(name: str) -> None:
    def ping(self: object, request: Ping) -> Pong: ...

    namespace = {name: action(ping)}
    with pytest.raises(ContractDefinitionError, match=f"action name {name!r}"):
        contract("echo.v1")(declaration("Echo", namespace))


@pytest.mark.spec("CON-11")
def test_action_name_may_hold_digits_and_underscores() -> None:
    @contract("echo.v1")
    class Echo(Protocol):
        @action
        def ping_2(self, request: Ping) -> Pong: ...

    assert list(Contract.of(Echo).actions) == ["ping_2"]


@pytest.mark.spec("CON-12")
def test_a_contract_that_redefines_an_action_is_described_with_it_once() -> None:
    # A contract's actions are the methods of one class, whose namespace
    # holds a name once: the later definition replaces the earlier one.
    @contract("echo.v1")
    class Echo(Protocol):
        @action
        def ping(self, request: Ping) -> Pong: ...

        @action
        def other(self, request: Ping) -> None: ...

        # mypy reports the redefinition on the decorator's line or the def's,
        # depending on the version that runs it.
        @action(idempotent=True)  # type: ignore[no-redef,unused-ignore]
        def ping(self, request: Pong) -> Ping: ...  # type: ignore[no-redef,unused-ignore]  # noqa: F811

    found = Contract.of(Echo)
    assert list(found.actions) == ["ping", "other"]
    assert found.actions["ping"] == Action("ping", True, Pong, Ping, {})
    described = found.describe()["actions"]
    assert list(described) == ["ping", "other"]
    assert described["ping"]["idempotent"] is True


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(lambda self, request: None, id="undecorated function"),
        pytest.param(staticmethod(lambda request: None), id="staticmethod"),
        pytest.param(property(lambda self: None), id="property"),
        pytest.param(3, id="constant"),
    ],
)
def test_declaration_holds_actions_only(member: object) -> None:
    with pytest.raises(ContractDefinitionError, match="is not an action"):
        contract("echo.v1")(declaration("Echo", {"helper": member}))


def test_declaration_is_a_protocol() -> None:
    class Echo:
        @action
        def ping(self, request: Ping) -> Pong:
            return Pong(text=request.text)

    with pytest.raises(ContractDefinitionError, match=r"class Echo\(Protocol\)"):
        contract("echo.v1")(Echo)


def test_declaration_extends_protocol_only() -> None:
    class Base(Protocol):
        pass

    class Echo(Base, Protocol):
        @action
        def ping(self, request: Ping) -> Pong: ...

    with pytest.raises(ContractDefinitionError, match=r"class Echo\(Protocol\)"):
        contract("echo.v1")(Echo)  # type: ignore[type-abstract]


def test_declaration_is_a_class() -> None:
    with pytest.raises(ContractDefinitionError, match="decorates a class"):
        contract("echo.v1")(lambda: None)  # type: ignore[arg-type]


def test_declaration_is_marked_once() -> None:
    with pytest.raises(ContractDefinitionError, match="already"):
        contract("echo.v2")(declare("echo.v1"))


def _ping_signatures() -> list[Callable[..., object]]:
    def no_request(self: object) -> Pong: ...

    def extra(self: object, request: Ping, more: Ping) -> Pong: ...

    def star(self: object, *requests: Ping) -> Pong: ...

    def keyword(self: object, *, request: Ping) -> Pong: ...

    def default(self: object, request: Ping = Ping(text="")) -> Pong: ...  # noqa: B008

    return [no_request, extra, star, keyword, default]


@pytest.mark.parametrize("function", _ping_signatures(), ids=lambda f: f.__name__)
def test_action_takes_self_and_request(function: Callable[..., object]) -> None:
    with pytest.raises(ContractDefinitionError, match=r"\(self, request\)"):
        contract("echo.v1")(declaration("Echo", {"ping": action(function)}))


def test_action_annotates_its_request() -> None:
    def ping(self, request) -> Pong: ...  # type: ignore[no-untyped-def]  # noqa: ANN001

    with pytest.raises(ContractDefinitionError, match="annotate its request"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


def test_action_annotates_its_reply() -> None:
    def ping(self: object, request: Ping):  # type: ignore[no-untyped-def]  # noqa: ANN202
        ...

    with pytest.raises(ContractDefinitionError, match="annotate its reply"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


def test_action_types_are_defined_before_the_contract() -> None:
    def ping(self: object, request: "Later") -> Pong: ...  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(ContractDefinitionError, match="'Later'"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


def test_action_is_synchronous() -> None:
    async def ping(self: object, request: Ping) -> Pong: ...

    with pytest.raises(ContractDefinitionError, match="synchronous"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


# Request and reply types


@pytest.mark.spec("CON-13")
@pytest.mark.parametrize(
    "annotation",
    [
        bool,
        int,
        float,
        str,
        Binary,
        Decimal128,
        Duration,
        AwareDatetime,
        date,
        list[int],
        dict[str, int],
        Line,
        Colour,
        Literal["red", "green"],
        int | None,
        Optional[int],  # noqa: UP045
        Annotated[int, Field(gt=0)],
        list[Annotated[str, Field(min_length=1)]],
        dict[Annotated[str, Field(min_length=1)], list[Line | None]],
        Node,
        Quantity,
    ],
    ids=repr,
)
def test_portable_forms_are_accepted(annotation: object) -> None:
    declare_with_field(annotation)


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    "annotation",
    [
        Any,
        object,
        complex,
        bytes,
        Decimal,
        timedelta,
        UUID,
        type(None),
        tuple[int, ...],
        set[int],
        frozenset[int],
        list,
        dict,
        dict[int, str],
        int | str,
        int | str | None,
        Level,
        Literal[1, 2],
        Literal["red", 2],
        RootModel[int],
        Point,
        dataclasses.make_dataclass("Point", [("x", int)]),
        list[complex],
        dict[str, set[int]],
        list[int | str] | None,
    ],
    ids=repr,
)
def test_other_types_are_rejected(annotation: object) -> None:
    with pytest.raises(ContractDefinitionError, match=r"Shape\.value"):
        declare_with_field(annotation)


@pytest.mark.spec("CON-13", "CON-17")
def test_timestamp_carries_an_offset() -> None:
    with pytest.raises(ContractDefinitionError, match="AwareDatetime"):
        declare_with_field(datetime)


@pytest.mark.spec("CON-13", "CON-17")
def test_enum_values_are_strings() -> None:
    Mixed = enum.Enum("Mixed", {"A": "a", "B": 2})
    with pytest.raises(ContractDefinitionError, match="must have string values"):
        declare_with_field(Mixed)


@pytest.mark.spec("CON-17")
def test_rejection_names_contract_action_and_field() -> None:
    class Inner(BaseModel):
        when: complex

    class Outer(BaseModel):
        items: dict[str, list[Inner]]

    def place(self: object, request: Outer) -> None: ...

    expected = (
        "orders.v1, action 'place': field Outer.items{*}[*].when has type "
        "complex, which is not a portable form"
    )
    with pytest.raises(ContractDefinitionError, match=re.escape(expected)):
        contract("orders.v1")(declaration("Orders", {"place": action(place)}))


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize("request_type", [int, str, dict[str, int], Colour])
def test_request_type_is_a_record(request_type: object) -> None:
    def send(self: object, request: request_type) -> None: ...  # type: ignore[valid-type]

    with pytest.raises(ContractDefinitionError, match="request type must be a record"):
        contract("echo.v1")(declaration("Echo", {"send": action(send)}))


@pytest.mark.spec("CON-14", "CON-17")
@pytest.mark.parametrize("reply_type", [int, str, list[Pong], Pong | None])
def test_reply_type_is_a_record_or_empty(reply_type: object) -> None:
    def send(self: object, request: Ping) -> reply_type:  # type: ignore[valid-type]
        ...

    with pytest.raises(ContractDefinitionError, match="reply type must be a record"):
        contract("echo.v1")(declaration("Echo", {"send": action(send)}))


@pytest.mark.spec("CON-14")
def test_reply_may_be_empty() -> None:
    @contract("echo.v1")
    class Echo(Protocol):
        @action
        def ping(self, request: Ping) -> None: ...

    assert Contract.of(Echo).actions["ping"].reply is None


@pytest.mark.spec("CON-14", "CON-17")
def test_reply_fields_are_checked_too() -> None:
    class Odd(BaseModel):
        value: complex

    def ping(self: object, request: Ping) -> Odd: ...

    with pytest.raises(ContractDefinitionError, match=r"field Odd\.value"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


# Serialization: a record travels as its declared fields


def _computed() -> type[BaseModel]:
    class Tagged(BaseModel):
        name: str

        @computed_field  # type: ignore[prop-decorator]
        @property
        def tags(self) -> set[complex]:
            return set()

    return Tagged


def _field_serializer() -> type[BaseModel]:
    class Priced(BaseModel):
        cents: int

        @field_serializer("cents")
        def as_text(self, value: int) -> str:
            return str(value)

    return Priced


def _model_serializer() -> type[BaseModel]:
    class Packed(BaseModel):
        value: int

        @model_serializer
        def as_list(self) -> list[int]:
            return [self.value]

    return Packed


def _annotated_serializer(serializer: object) -> type[BaseModel]:
    return create_model("Wrapped", value=(Annotated[int, serializer], ...))


def _nested_annotated_serializer() -> type[BaseModel]:
    pairs = PlainSerializer(lambda v: {"v": [(v, v)]}, return_type=dict[str, Any])
    return create_model("Deep", values=(list[Annotated[int, pairs]], ...))


def _configured(config: ConfigDict) -> type[BaseModel]:
    return create_model(
        "Configured",
        __config__=config,
        when=(AwareDatetime, ...),
        day=(date, ...),
        span=(timedelta, ...),
        ratio=(float, ...),
    )


def _nested_record_serializer() -> type[BaseModel]:
    return create_model("Holder", inner=(_field_serializer(), ...))


_SERIALIZING = [
    pytest.param(_computed, "record Tagged has computed fields", id="computed_field"),
    pytest.param(
        _field_serializer,
        "field Priced.cents has a custom serializer",
        id="field_serializer",
    ),
    pytest.param(
        _model_serializer,
        "record Packed has a custom serializer",
        id="model_serializer",
    ),
    pytest.param(
        lambda: _annotated_serializer(PlainSerializer(str, return_type=str)),
        "field Wrapped.value has a custom serializer",
        id="PlainSerializer",
    ),
    pytest.param(
        lambda: _annotated_serializer(WrapSerializer(lambda v, h: h(v))),
        "field Wrapped.value has a custom serializer",
        id="WrapSerializer",
    ),
    pytest.param(
        _nested_annotated_serializer,
        "field Deep.values[*] has a custom serializer",
        id="nested PlainSerializer",
    ),
    pytest.param(
        _nested_record_serializer,
        "field Holder.inner.cents has a custom serializer",
        id="nested record",
    ),
    pytest.param(
        lambda: create_model(
            "Partial", keep=(int, ...), secret=(int, Field(exclude=True))
        ),
        "field Partial.secret has an exclusion from serialization",
        id="exclude",
    ),
    pytest.param(
        lambda: create_model(
            "Partial",
            keep=(int, ...),
            secret=(int, Field(exclude_if=lambda value: value == 0)),
        ),
        "field Partial.secret has an exclusion from serialization",
        id="exclude_if",
    ),
    *(
        pytest.param(
            lambda key=key, value=value: _configured(cast("ConfigDict", {key: value})),
            f"record Configured has {key} set",
            id=key,
        )
        for key, value in [
            ("ser_json_temporal", "milliseconds"),
            ("ser_json_timedelta", "float"),
            ("ser_json_inf_nan", "strings"),
            ("ser_json_bytes", "base64"),
        ]
    ),
]


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(("make", "message"), _SERIALIZING)
def test_request_record_with_custom_serialization_is_rejected(
    make: Callable[[], type[BaseModel]], message: str
) -> None:
    record = make()

    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        contract("echo.v1")(declaration("Echo", {"send": action(send)}))


@pytest.mark.spec("CON-14", "CON-17")
@pytest.mark.parametrize(("make", "message"), _SERIALIZING)
def test_reply_record_with_custom_serialization_is_rejected(
    make: Callable[[], type[BaseModel]], message: str
) -> None:
    record = make()

    def send(self: object, request: Ping) -> record:  # type: ignore[valid-type]
        ...

    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        contract("echo.v1")(declaration("Echo", {"send": action(send)}))


@pytest.mark.spec("CON-13")
def test_before_validators_are_accepted() -> None:
    class Checked(BaseModel):
        code: Annotated[str, BeforeValidator(str.upper)]
        count: int

        @field_validator("count", mode="before")
        @classmethod
        def positive(cls, value: int) -> int:
            return abs(value)

    declare_with_field(Checked)


# A validator that takes ValidationInfo can read info.data, the fields of its
# record validated before it, as the very values the record keeps. Each
# callable below notes whether pydantic-core handed it that info.
_VALIDATOR_INFO: list[bool] = []


def _value_only(value: object) -> object:
    _VALIDATOR_INFO.append(False)
    return value


def _value_and_info(value: object, info: ValidationInfo) -> object:
    _VALIDATOR_INFO.append(True)
    return value


def _info_optional(value: object, info: object = None) -> object:
    _VALIDATOR_INFO.append(info is not None)
    return value


class _CallTakesInfo:
    def __call__(self, value: object, info: ValidationInfo) -> object:
        _VALIDATOR_INFO.append(True)
        return value


_BEFORE_VALIDATORS: list[Callable[..., object]] = [
    _value_only,
    _value_and_info,
    _info_optional,
    lambda value: _value_only(value),
    lambda value, info: _value_and_info(value, info),
    functools.partial(_info_optional, info=None),
    _CallTakesInfo(),
]


@pytest.mark.spec("CON-13", "CON-16", "CON-17")
@pytest.mark.parametrize("validator", _BEFORE_VALIDATORS)
@pytest.mark.parametrize("place", ["field", "list item"])
def test_before_validator_is_refused_when_pydantic_hands_it_validation_info(
    validator: Callable[..., object], place: str
) -> None:
    checked: Any = Annotated[int, BeforeValidator(validator)]
    annotation: Any = checked if place == "field" else cast("Any", list)[checked]
    record = create_model("Later", items=(list[int], ...), y=(annotation, ...))
    # What the installed Pydantic hands the validator when it reads a value.
    _VALIDATOR_INFO.clear()
    record.model_validate({"items": [1], "y": 2 if place == "field" else [2]})
    assert len(_VALIDATOR_INFO) == 1
    if _VALIDATOR_INFO[0]:
        with pytest.raises(
            ContractDefinitionError,
            match=r"field Shape\.value\.y(\[\*\])? has a validator that takes "
            r"ValidationInfo",
        ):
            declare_with_field(record)
    else:
        declare_with_field(record)


class ChangesAnEarlierField(BaseModel):
    items: list[int]
    y: int

    @field_validator("y", mode="before")
    @classmethod
    def tamper(cls, value: object, info: ValidationInfo) -> object:
        info.data["items"].append("not an int")
        return value


@pytest.mark.spec("CON-13", "CON-16", "CON-17")
def test_before_validator_that_changes_an_earlier_field_is_refused() -> None:
    # The change travels: info.data holds the values the record keeps.
    made = ChangesAnEarlierField.model_validate_json('{"items": [1], "y": 2}')
    assert made.items == [1, "not an int"]
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(
            "field Shape.value.y has a validator that takes ValidationInfo, "
            "whose data holds the fields validated before it"
        ),
    ):
        declare_with_field(ChangesAnEarlierField)


class AfterModel(BaseModel):
    x: Annotated[int, Field(le=10)]

    @model_validator(mode="after")
    def bump(self) -> "AfterModel":
        self.x = 99
        return self


class WrapModel(BaseModel):
    x: Annotated[int, Field(le=10)]

    @model_validator(mode="wrap")
    @classmethod
    def bump(
        cls, data: object, handler: ModelWrapValidatorHandler["WrapModel"]
    ) -> "WrapModel":
        made = handler(data)
        made.x = 99
        return made


class AfterField(BaseModel):
    x: int

    @field_validator("x")
    @classmethod
    def as_text(cls, value: int) -> str:
        return str(value)


class OwnInit(BaseModel):
    x: Annotated[int, Field(ge=0, le=10)]

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        self.x = 100


def _identity(value: object) -> object:
    return value


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        pytest.param(
            Annotated[int, Field(ge=0, le=10), AfterValidator(lambda v: v * 100)],
            "has an after or wrap validator",
            id="after validator past a bound",
        ),
        pytest.param(
            Annotated[int, WrapValidator(lambda v, h: h(v) * 100)],
            "has an after or wrap validator",
            id="wrap validator",
        ),
        pytest.param(
            Annotated[int, AfterValidator(_identity), Field(ge=0)],
            "has an after or wrap validator",
            id="constraint written after a validator",
        ),
        pytest.param(
            Annotated[Decimal, AfterValidator(_identity), Field(max_digits=3)],
            "has an after or wrap validator",
            id="decimal constraint after a validator",
        ),
        pytest.param(
            AfterField, "has an after or wrap validator", id="field_validator"
        ),
        pytest.param(
            AfterModel, "has a validator on a record", id="after model_validator"
        ),
        pytest.param(
            WrapModel, "has a validator on a record", id="wrap model_validator"
        ),
        pytest.param(OwnInit, "defines __init__", id="custom __init__"),
    ],
)
def test_values_changed_after_the_check_are_refused(
    annotation: object, message: str
) -> None:
    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        declare_with_field(annotation)


def _custom_json_schema() -> type[BaseModel]:
    class Custom(BaseModel):
        value: int

        @classmethod
        def __get_pydantic_json_schema__(
            cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
        ) -> JsonSchemaValue:
            return {"type": "string"}

    return Custom


def _field_description(record: type[BaseModel], name: str) -> object:
    """The description of one field of a request record."""
    defs = Contract.of(declare_with_field(record)).describe()["$defs"]
    return defs[record.__name__]["properties"][name]


@pytest.mark.spec("CON-16")
@pytest.mark.parametrize(
    ("make", "field", "expected"),
    [
        pytest.param(
            lambda: create_model(
                "Shown",
                n=(
                    cast("Any", Annotated[int, WithJsonSchema({"type": "string"})]),
                    ...,
                ),
            ),
            "n",
            INTEGER,
            id="WithJsonSchema",
        ),
        pytest.param(
            lambda: create_model(
                "Shown",
                n=(
                    cast(
                        "Any", list[Annotated[int, WithJsonSchema({"type": "string"})]]
                    ),
                    ...,
                ),
            ),
            "n",
            {"type": "array", "items": INTEGER},
            id="nested WithJsonSchema",
        ),
        pytest.param(
            lambda: create_model(
                "Shown", n=(int, Field(json_schema_extra={"type": "string"}))
            ),
            "n",
            INTEGER,
            id="field json_schema_extra",
        ),
        pytest.param(
            lambda: create_model(
                "Shown",
                __config__=ConfigDict(
                    json_schema_extra={"properties": {"z": {"type": "string"}}}
                ),
                a=(int, ...),
            ),
            "a",
            INTEGER,
            id="config json_schema_extra",
        ),
        pytest.param(
            lambda: create_model(
                "Shown",
                __config__=ConfigDict(json_schema_mode_override="validation"),
                n=(
                    cast(
                        "Any",
                        Annotated[
                            int, BeforeValidator(int, json_schema_input_type=str)
                        ],
                    ),
                    ...,
                ),
            ),
            "n",
            INTEGER,
            id="json_schema_mode_override",
        ),
        pytest.param(
            _custom_json_schema,
            "value",
            INTEGER,
            id="__get_pydantic_json_schema__",
        ),
    ],
)
def test_json_schema_customisation_does_not_change_the_description(
    make: Callable[[], type[BaseModel]], field: str, expected: object
) -> None:
    assert _field_description(make(), field) == expected


class _OwnSchema:
    """An Annotated marker that builds its own core schema."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.str_schema()


def _record_hook(hook: str, wrapper: Callable[[Any], Any]) -> type[BaseModel]:
    def build(*args: object) -> object:
        return core_schema.str_schema()

    namespace = {"__annotations__": {"value": int}, hook: wrapper(build)}
    return cast(
        "type[BaseModel]",
        types.new_class(
            "Hooked", (BaseModel,), exec_body=lambda ns: ns.update(namespace)
        ),
    )


def _hooked_enum(hook: str) -> type[enum.Enum]:
    def build(*args: object) -> object:
        if hook == "__get_pydantic_core_schema__":
            return core_schema.int_schema()
        return {"type": "integer"}

    namespace = {"RED": "red", hook: classmethod(build)}
    return cast(
        "type[enum.Enum]",
        types.new_class(
            "Color", (enum.StrEnum,), exec_body=lambda ns: ns.update(namespace)
        ),
    )


@pytest.mark.spec("CON-13", "CON-16")
@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        pytest.param(
            Annotated[int, GetPydanticSchema(lambda t, h: core_schema.str_schema())],
            {"type": "string"},
            id="GetPydanticSchema",
        ),
        pytest.param(
            list[
                Annotated[int, GetPydanticSchema(lambda t, h: core_schema.str_schema())]
            ],
            {"type": "array", "items": {"type": "string"}},
            id="nested GetPydanticSchema",
        ),
        pytest.param(
            Annotated[int, _OwnSchema()],
            {"type": "string"},
            id="marker with its own schema",
        ),
        pytest.param(
            _hooked_enum("__get_pydantic_json_schema__"),
            {"$ref": "#/$defs/Color"},
            id="enum json schema hook",
        ),
        pytest.param(
            _hooked_enum("__get_pydantic_core_schema__"),
            INTEGER,
            id="enum core schema hook",
        ),
        pytest.param(
            _record_hook("__get_pydantic_core_schema__", classmethod),
            {"type": "string"},
            id="record core schema hook",
        ),
        pytest.param(
            _record_hook("__get_pydantic_json_schema__", staticmethod),
            {"$ref": "#/$defs/Hooked"},
            id="record json schema staticmethod",
        ),
    ],
)
def test_type_is_described_as_pydantic_builds_it(
    annotation: object, expected: object
) -> None:
    contract_class = declare_with_field(annotation)
    description = Contract.of(contract_class).describe()
    assert description["$defs"]["Shape"]["properties"]["value"] == expected
    if expected == {"$ref": "#/$defs/Color"}:
        assert description["$defs"]["Color"] == {"type": "string", "enum": ["red"]}


@pytest.mark.spec("CON-13", "CON-17")
def test_plain_validator_is_rejected() -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(
            "field Shape.value has type function-plain, which is not a portable form"
        ),
    ):
        declare_with_field(Annotated[int, PlainValidator(int)])


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    "config",
    [
        pytest.param(ConfigDict(val_json_bytes="base64"), id="val_json_bytes"),
        pytest.param(ConfigDict(str_max_length=3), id="str_max_length"),
    ],
)
def test_config_that_splits_description_and_wire_is_rejected(
    config: ConfigDict,
) -> None:
    record = create_model("Configured", __config__=config, b=(bytes, ...))
    key = next(iter(config))
    with pytest.raises(
        ContractDefinitionError, match=f"record Shape.value has {key} set"
    ):
        declare_with_field(record)


@pytest.mark.spec("CON-13", "CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(int, None, id="int = None"),
        pytest.param(int, "abc", id="int = 'abc'"),
        pytest.param(str, 3, id="str = 3"),
        pytest.param(AwareDatetime, "2026-01-01", id="timestamp = date text"),
        pytest.param(int, True, id="int = True"),
        pytest.param(float, True, id="float = True"),
        pytest.param(date, "2026-01-01", id="date = text"),
        pytest.param(list[int], [True], id="list[int] = [True]"),
        pytest.param(dict[str, int], {"a": True}, id="dict[str, int] = {'a': True}"),
        pytest.param(Colour, "red", id="enum = its value"),
        pytest.param(Annotated[int, Field(ge=0)], -1, id="ge=0 = -1"),
    ],
)
def test_default_outside_the_field_type_is_rejected(
    annotation: object, default: object
) -> None:
    record = create_model("Defaulted", n=(cast("Any", annotation), default))
    with pytest.raises(
        ContractDefinitionError, match="which is not a value of its type"
    ):
        declare_with_field(record)


def _split_commas(value: object) -> object:
    return value.split(",") if isinstance(value, str) else value


@pytest.mark.spec("CON-13", "CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "default", "becomes"),
    [
        pytest.param(
            Annotated[int, BeforeValidator(lambda v: 0 if v is None else v)],
            None,
            "turn into 0",
            id="BeforeValidator None",
        ),
        pytest.param(
            Annotated[list[int], BeforeValidator(lambda v: [] if v is None else v)],
            None,
            "turn into []",
            id="BeforeValidator list None",
        ),
        pytest.param(
            Annotated[int, BeforeValidator(int)],
            "3",
            "turn into 3",
            id="int('3')",
        ),
        pytest.param(
            Annotated[list[str], BeforeValidator(_split_commas)],
            "a,b",
            "turn into ['a', 'b']",
            id="split commas",
        ),
        pytest.param(
            Annotated[str, BeforeValidator(str.lower)],
            "ABC",
            "turn into 'abc'",
            id="lower",
        ),
        pytest.param(
            Annotated[int, BeforeValidator(int)],
            3.0,
            "turn into 3",
            id="int = 3.0",
        ),
        pytest.param(
            list[Annotated[int, BeforeValidator(int)]],
            [3.0],
            "turn into [3]",
            id="list[int] = [3.0]",
        ),
    ],
)
def test_default_that_does_not_read_back_as_itself_is_rejected(
    annotation: object, default: object, becomes: str
) -> None:
    record = create_model("Defaulted", n=(cast("Any", annotation), default))
    with pytest.raises(ContractDefinitionError, match=re.escape(becomes)):
        declare_with_field(record)


@pytest.mark.spec("CON-13", "CON-16")
def test_defaults_of_the_field_type_are_allowed() -> None:
    class Defaults(BaseModel):
        maybe: int | None = None
        colour: Colour = Colour.RED
        size: Literal["s", "m"] = "s"
        amount: Decimal128 = Decimal("1.50")
        raw: Binary = b"\xff"
        span: Duration = timedelta(seconds=90)
        ratio: float = 1
        lines: list[Line] = []
        counts: dict[str, int] = {"a": 1}
        line: Line = Line(sku="A1", quantity=1)
        day: date = date(2026, 1, 1)
        when: AwareDatetime = datetime(2026, 1, 1, tzinfo=UTC)
        positive: int | None = Field(None, gt=0)
        upper: Annotated[str, BeforeValidator(str.upper)] = "ABC"
        parsed: Annotated[int, BeforeValidator(int)] = 3
        made: list[int] = Field(default_factory=list)
        half: float = 1.5
        widened: Annotated[float, BeforeValidator(float)] = 1
        weights: dict[str, float] = {"a": 1}

    declare_with_field(Defaults)


@pytest.mark.spec("CON-16")
@pytest.mark.parametrize(
    ("annotation", "default", "described"),
    [
        pytest.param(float, 1, 1.0, id="float = 1"),
        pytest.param(float, 1.5, 1.5, id="float = 1.5"),
        pytest.param(int, 3, 3, id="int = 3"),
    ],
)
def test_default_is_described_as_it_travels(
    annotation: object, default: object, described: object
) -> None:
    record = create_model("Defaulted", n=(cast("Any", annotation), default))
    sent = json.loads(record().model_dump_json())["n"]
    assert _field_description(record, "n")["default"] == sent == described  # type: ignore[index]
    assert type(sent) is type(described)


@pytest.mark.spec("CON-16")
def test_documentation_and_constraints_are_allowed() -> None:
    class Documented(BaseModel):
        count: int = Field(
            ge=0, description="How many.", title="Count", examples=[1], deprecated=True
        )
        name: Annotated[str, Field(min_length=1, max_length=20)]
        code: Annotated[str, StringConstraints(min_length=1, max_length=4)]
        level: Annotated[int, annotated_types.Ge(0), annotated_types.Lt(10)]
        tags: Annotated[list[str], annotated_types.Len(0, 5)]
        lower: Annotated[str, BeforeValidator(str.lower)]
        trimmed: Annotated[str, StringConstraints(strip_whitespace=True)]

    class Derived(Documented):
        extra: bool = False

    declare_with_field(Derived)
    defs = Contract.of(declare_with_field(Documented, "docs.v1")).describe()["$defs"]
    assert defs["Documented"]["properties"]["count"]["minimum"] == 0


@pytest.mark.spec("CON-16", "CON-33")
@pytest.mark.parametrize(
    "field",
    [
        pytest.param(Field(alias="itemId"), id="alias"),
        pytest.param(Field(serialization_alias="itemId"), id="serialization_alias"),
        pytest.param(Field(validation_alias="itemId"), id="validation_alias"),
    ],
)
def test_field_alias_is_rejected(field: object) -> None:
    with pytest.raises(ContractDefinitionError, match="has an alias"):
        declare_with_field(Annotated[int, field])


@pytest.mark.spec("CON-16", "CON-33")
def test_alias_generator_is_rejected() -> None:
    camel = create_model(
        "Camel", __config__=ConfigDict(alias_generator=to_camel), item_id=(int, ...)
    )
    with pytest.raises(ContractDefinitionError, match=r"Shape\.value\.item_id"):
        declare_with_field(camel)


@pytest.mark.spec("CON-17")
@pytest.mark.parametrize("defer_build", [False, True])
def test_record_type_defined_after_the_contract_is_named(defer_build: bool) -> None:
    class Early(BaseModel):
        model_config = ConfigDict(defer_build=defer_build)
        later: "Later"  # type: ignore[name-defined]  # noqa: F821

    expected = (
        "record Early, or a record it uses, refers to 'Later', which the module of "
        "that record does not define when the contract is declared; define Later "
        "at module level there, before the contract, or call that record's "
        "model_rebuild() once Later is defined"
    )
    with pytest.raises(ContractDefinitionError, match=re.escape(expected)):
        contract("early.v1")(
            declaration("EarlyContract", {"send": action(_sender(Early))})
        )


@pytest.mark.spec("CON-13", "CON-16")
def test_records_written_top_down_are_resolved() -> None:
    @contract("layout.v1")
    class Layout(Protocol):
        @action
        def send(self, request: TopDown) -> None: ...

    assert set(Contract.of(Layout).describe()["$defs"]) == {"TopDown", "Below"}


@pytest.mark.spec("CON-17")
@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(str, "\ud800", id="lone surrogate"),
        pytest.param(list[str], ["\ud800"], id="in a list"),
        pytest.param(dict[str, str], {"a": "\ud800"}, id="in a dict"),
    ],
)
def test_default_that_cannot_be_written_as_json_is_named(
    annotation: object, default: object
) -> None:
    record = create_model("Raw", data=(cast("Any", annotation), default))
    with pytest.raises(
        ContractDefinitionError,
        match=r"field Shape\.value\.data has the default .*cannot be written as JSON",
    ):
        declare_with_field(record)


@pytest.mark.spec("CON-17")
def test_forward_reference_does_not_resolve_to_checker_locals() -> None:
    expected = "record Shape, or a record it uses, refers to 'path', which the module"
    with pytest.raises(ContractDefinitionError, match=re.escape(expected)):
        declare_with_field(Sneaky)


@pytest.mark.spec("CON-13", "CON-16")
def test_local_records_rebuilt_by_their_author_are_accepted() -> None:
    class Outer(BaseModel):
        inner: "Inner"

    class Inner(BaseModel):
        value: int

    Outer.model_rebuild()
    defs = Contract.of(declare_with_field(Outer)).describe()["$defs"]
    assert {"Outer", "Inner"} <= set(defs)


@pytest.mark.spec("CON-13", "CON-16")
def test_record_built_on_first_use_is_checked() -> None:
    lazy = create_model(
        "Lazy", __config__=ConfigDict(defer_build=True), value=(int, ...)
    )
    contract_class = declare_with_field(list[lazy])  # type: ignore[valid-type]
    defs = Contract.of(contract_class).describe()["$defs"]
    assert defs["Lazy"]["properties"]["value"]["type"] == "integer"


@pytest.mark.spec("CON-13", "CON-17")
def test_record_built_on_first_use_is_still_checked_for_serializers() -> None:
    class Lazy(BaseModel):
        model_config = ConfigDict(defer_build=True)
        value: int

        @model_serializer
        def as_list(self) -> list[int]:
            return [self.value]

    with pytest.raises(
        ContractDefinitionError, match="record Lazy has a custom serializer"
    ):
        contract("lazy.v1")(
            declaration("LazyContract", {"send": action(_sender(Lazy))})
        )


def _sender(record: type[BaseModel]) -> Callable[..., None]:
    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    return send


# Record names


def _record_named(name: str) -> type[BaseModel]:
    return create_model(name, value=(int, ...))


@pytest.mark.spec("CON-15", "CON-17")
def test_two_records_with_one_name_are_rejected() -> None:
    first, second = _record_named("Item"), _record_named("Item")

    def add(self: object, request: first) -> second:  # type: ignore[valid-type]
        ...

    with pytest.raises(ContractDefinitionError, match="two different records"):
        contract("cart.v1")(declaration("Cart", {"add": action(add)}))


@pytest.mark.spec("CON-15", "CON-17")
def test_record_names_are_unique_across_actions() -> None:
    first, second = _record_named("Item"), _record_named("Item")

    def add(self: object, request: first) -> None: ...  # type: ignore[valid-type]

    def remove(self: object, request: second) -> None: ...  # type: ignore[valid-type]

    namespace = {"add": action(add), "remove": action(remove)}
    with pytest.raises(ContractDefinitionError, match="'Item'"):
        contract("cart.v1")(declaration("Cart", namespace))


@pytest.mark.spec("CON-15")
def test_one_record_may_be_used_many_times() -> None:
    class Twice(BaseModel):
        first: Line
        second: list[Line]

    @contract("cart.v1")
    class Cart(Protocol):
        @action
        def add(self, request: Twice) -> Twice: ...

        @action
        def remove(self, request: Line) -> Line: ...

    assert set(Contract.of(Cart).actions) == {"add", "remove"}


def _enum_named(name: str) -> type[enum.Enum]:
    return cast("type[enum.Enum]", enum.StrEnum(name, {"A": "a"}))


@pytest.mark.spec("CON-15", "CON-17")
def test_two_enums_with_one_name_are_rejected() -> None:
    first, second = _enum_named("Kind"), _enum_named("Kind")
    record = create_model("Pair", first=(first, ...), second=(second, ...))
    with pytest.raises(
        ContractDefinitionError, match="records or enums are named 'Kind'"
    ):
        declare_with_field(record)


@pytest.mark.spec("CON-15", "CON-17")
def test_record_and_enum_with_one_name_are_rejected() -> None:
    status = _enum_named("Status")
    record = create_model("Status", value=(int, ...))

    def check(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    holder = create_model("Holder", status=(status, ...))

    def report(self: object, request: holder) -> None: ...  # type: ignore[valid-type]

    namespace = {"check": action(check), "report": action(report)}
    with pytest.raises(ContractDefinitionError, match="'Status'"):
        contract("audit.v1")(declaration("Audit", namespace))


@pytest.mark.spec("CON-15")
def test_one_enum_may_be_used_many_times() -> None:
    record = create_model("Pair", first=(Colour, ...), second=(list[Colour], ...))
    declare_with_field(record)


def _alias(name: str, value: object) -> Any:  # noqa: ANN401
    """Build a type alias at run time; to a type checker it is any type form."""
    return TypeAliasType(name, value)


@pytest.mark.spec("CON-15", "CON-16")
def test_type_aliases_are_written_out_and_never_named() -> None:
    first, second = _alias("Item", list[int]), _alias("Item", list[str])
    item = _record_named("Item")
    record = create_model("Pair", a=(first, ...), b=(second, ...), item=(item, ...))
    description = Contract.of(declare_with_field(record)).describe()
    assert set(description["$defs"]) == {"Shape", "Pair", "Item"}
    properties = description["$defs"]["Pair"]["properties"]
    assert properties["a"] == {"type": "array", "items": INTEGER}
    assert properties["b"] == {"type": "array", "items": {"type": "string"}}
    assert properties["item"] == {"$ref": "#/$defs/Item"}


@pytest.mark.spec("CON-15", "CON-16")
@pytest.mark.parametrize(
    ("alias", "named"),
    [
        pytest.param(_alias("InnerRef", Below), "Below", id="record"),
        pytest.param(_alias("Chain", _alias("InnerRef", Below)), "Below", id="chain"),
        pytest.param(_alias("Col", Colour), "Colour", id="enum"),
        pytest.param(
            _alias("Cols", list.__class_getitem__(_alias("Col", Colour))),
            "Colour",
            id="list of enum",
        ),
    ],
)
def test_alias_of_a_record_or_enum_describes_it_under_its_own_name(
    alias: object, named: str
) -> None:
    description = Contract.of(declare_with_field(alias)).describe()
    assert set(description["$defs"]) == {"Shape", named}


@pytest.mark.spec("CON-15", "CON-16")
def test_one_type_alias_may_be_used_many_times() -> None:
    record = create_model(
        "Counts", a=(Ints, ...), b=(Ints, ...), c=(list[Ints], ...), d=(IntPairs, ...)
    )
    description = Contract.of(declare_with_field(record)).describe()
    assert set(description["$defs"]) == {"Shape", "Counts"}
    ints = {"type": "array", "items": INTEGER}
    assert description["$defs"]["Counts"]["properties"]["d"] == {
        "type": "object",
        "additionalProperties": ints,
    }


@pytest.mark.spec("CON-17")
@pytest.mark.parametrize(
    ("alias", "at", "name"),
    [
        pytest.param(Nested, "Shape.value[*]", "Nested", id="list"),
        pytest.param(Tree, "Shape.value{*}", "Tree", id="map"),
        pytest.param(Ping1, "Shape.value[*][*]", "Ping1", id="pair"),
    ],
)
def test_recursive_type_alias_is_rejected(alias: object, at: str, name: str) -> None:
    expected = f"field {at} has a recursive type that is not a record"
    with pytest.raises(ContractDefinitionError, match=re.escape(expected)):
        declare_with_field(alias)


@pytest.mark.spec("CON-13", "CON-16")
def test_quoted_name_in_a_type_alias_is_resolved_by_pydantic() -> None:
    description = Contract.of(declare_with_field(QuotedBelow)).describe()
    assert description["$defs"]["Shape"]["properties"]["value"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/Below"},
    }


@pytest.mark.spec("CON-13", "CON-16")
def test_type_alias_of_a_record_defined_later_is_accepted() -> None:
    defs = Contract.of(declare_with_field(BelowList)).describe()["$defs"]
    assert "Below" in defs


@pytest.mark.spec("CON-15", "CON-17")
def test_parametrised_generic_record_is_rejected() -> None:
    with pytest.raises(ContractDefinitionError, match="is a parametrised generic"):
        declare_with_field(Page[int])


@pytest.mark.spec("CON-20", "CON-17")
@pytest.mark.parametrize(
    ("named", "kind"),
    [
        pytest.param(
            create_model("Gr\u00f6\u00dfe", value=(int, ...)), "record", id="record"
        ),
        pytest.param(enum.StrEnum("Gr\u00f6\u00dfe", {"A": "a"}), "enum", id="enum"),
        pytest.param(
            create_model("_Hidden", value=(int, ...)), "record", id="underscore"
        ),
    ],
)
def test_record_and_enum_names_are_ascii_identifiers(named: object, kind: str) -> None:
    with pytest.raises(
        ContractDefinitionError, match=f"{kind} name .* must start with an ASCII letter"
    ):
        declare_with_field(named)


@pytest.mark.spec("CON-20")
def test_record_and_enum_names_may_hold_digits_and_underscores() -> None:
    record = create_model(
        "Line_2", value=(cast("Any", enum.StrEnum("Kind_3", {"A": "a"})), ...)
    )
    names = set(Contract.of(declare_with_field(record)).describe()["$defs"])
    assert {"Line_2", "Kind_3"} <= names


class _Base(BaseModel):
    qty: int


class _WrapValidated(BaseModel):
    qty: int

    @model_validator(mode="before")
    @classmethod
    def passthrough(cls, data: object) -> object:
        return data


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        pytest.param(
            SerializeAsAny[_Base],
            "field Shape.value has a custom serializer",
            id="SerializeAsAny on a reference",
        ),
        pytest.param(
            Annotated[float, Field(ge=Decimal("0.5"))],
            "field Shape.value sets ge=Decimal('0.5'), which the description "
            "cannot state",
            id="Decimal bound",
        ),
        pytest.param(
            Annotated[float, Field(lt=float("inf"))],
            "field Shape.value sets lt=inf",
            id="infinite bound",
        ),
        pytest.param(
            Literal["a", None],
            "field Shape.value is a literal of values other than strings",
            id="literal with None",
        ),
        pytest.param(
            Annotated[date, Field(gt=date(2026, 1, 1))],
            "field Shape.value sets 'gt', which is outside the portable forms",
            id="constraint the description cannot state",
        ),
        pytest.param(
            Annotated[date, Field(gt=date(2026, 1, 1))],
            "field Shape.value sets 'gt'",
            id="date bound",
        ),
    ],
)
def test_every_core_schema_key_is_admitted_or_named(
    annotation: object, message: str
) -> None:
    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        declare_with_field(annotation)


@pytest.mark.spec("CON-13", "CON-16")
@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        pytest.param(_Base, {"$ref": "#/$defs/_Base"}, id="plain reference"),
        pytest.param(
            Annotated[float, Field(ge=0.5)],
            {"type": "number", **NUMBER_RANGE, "minimum": 0.5},
            id="float bound",
        ),
        pytest.param(Literal["a"] | None, None, id="literal or None"),
        pytest.param(
            Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True)],
            {"type": "string"},
            id="string transforms",
        ),
    ],
)
def test_nearest_legitimate_shapes_are_accepted(
    annotation: object, expected: object
) -> None:
    if annotation is _Base:
        # A leading underscore breaks CON-20; give the record a usable name.
        renamed = create_model("Named", __base__=cast("type[BaseModel]", annotation))
        described = Contract.of(declare_with_field(renamed)).describe()
        assert described["$defs"]["Shape"]["properties"]["value"] == {
            "$ref": "#/$defs/Named"
        }
        return
    described = Contract.of(declare_with_field(annotation)).describe()
    if expected is not None:
        assert described["$defs"]["Shape"]["properties"]["value"] == expected


class Lenient(enum.StrEnum):
    A = "a"

    @classmethod
    def _missing_(cls, value: object) -> "Lenient":
        return cls.A


@pytest.mark.spec("CON-13", "CON-16")
def test_enum_that_maps_unknown_input_is_accepted() -> None:
    # Pydantic 2.12 records _missing_ in the core schema and 2.14 does not;
    # either way it only maps input, so the enum is accepted on both.
    description = Contract.of(declare_with_field(Lenient)).describe()
    assert description["$defs"]["Lenient"] == {"type": "string", "enum": ["a"]}


@pytest.mark.spec("CON-17")
def test_default_factory_that_raises_is_named() -> None:
    record = create_model("Made", n=(int, Field(default_factory=lambda: 1 // 0)))
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(
            "field Shape.value.n has a default factory that raised ZeroDivisionError"
        ),
    ):
        declare_with_field(record)


@pytest.mark.spec("CON-16", "CON-17")
def test_before_model_validator_on_a_request_is_refused() -> None:
    renamed = create_model("Wrapped", __base__=_WrapValidated)

    def send(self: object, request: renamed) -> None: ...  # type: ignore[valid-type]

    with pytest.raises(ContractDefinitionError, match="has a validator on a record"):
        contract("wrap.v1")(declaration("Wrap", {"send": action(send)}))


# A node is judged with the record's settings and pydantic-core's order of
# work: whitespace is stripped, then the pattern and lengths are checked, then
# the case is converted.


def _strings(**constraints: object) -> object:
    return Annotated[str, StringConstraints(**cast("Any", constraints))]


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "config"),
    [
        pytest.param(
            _strings(to_upper=True, max_length=1), {}, id="upper and max length"
        ),
        pytest.param(
            _strings(to_lower=True, min_length=2), {}, id="lower and min length"
        ),
        pytest.param(
            _strings(strip_whitespace=True, min_length=2), {}, id="strip and min length"
        ),
        pytest.param(
            _strings(max_length=1), {"str_to_upper": True}, id="upper setting"
        ),
        pytest.param(
            _strings(max_length=3), {"str_to_lower": True}, id="lower setting"
        ),
        pytest.param(
            _strings(min_length=2), {"str_strip_whitespace": True}, id="strip setting"
        ),
    ],
)
def test_conversion_next_to_a_stated_length_is_refused(
    annotation: object, config: dict[str, object]
) -> None:
    record = create_model(
        "Cased", __config__=cast("ConfigDict", config), s=(cast("Any", annotation), ...)
    )
    with pytest.raises(ContractDefinitionError, match="converts its value"):
        declare_with_field(record)


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "config"),
    [
        pytest.param(
            dict.__class_getitem__((_strings(to_upper=True), int)), {}, id="field"
        ),
        pytest.param(dict[str, int], {"str_to_lower": True}, id="lower setting"),
        pytest.param(
            dict[str, int], {"str_strip_whitespace": True}, id="strip setting"
        ),
    ],
)
def test_converted_map_keys_are_refused(
    annotation: object, config: dict[str, object]
) -> None:
    record = create_model(
        "Keyed", __config__=cast("ConfigDict", config), m=(cast("Any", annotation), ...)
    )
    with pytest.raises(ContractDefinitionError, match="converts its keys"):
        declare_with_field(record)


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(_strings(pattern="^[a-z]+$"), id="field"),
        pytest.param(Annotated[str, Field(pattern=r"^\p{L}+$")], id="unicode property"),
        pytest.param(Annotated[str, Field(pattern="(?i)^abc$")], id="inline flag"),
        pytest.param(Annotated[str, Field(pattern=r"^\d+$")], id="digit class"),
        pytest.param(
            dict.__class_getitem__((_strings(pattern="^[a-z]+$"), int)), id="map key"
        ),
    ],
)
def test_patterns_are_refused(annotation: object) -> None:
    with pytest.raises(ContractDefinitionError, match="sets a pattern"):
        declare_with_field(annotation)


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize("step", [0.1, 1, 0.5])
def test_float_multiple_of_is_refused(step: float) -> None:
    with pytest.raises(ContractDefinitionError, match="multiple_of on a float"):
        declare_with_field(Annotated[float, Field(multiple_of=step)])


@pytest.mark.spec("CON-16")
def test_integer_multiple_of_is_accepted() -> None:
    described = Contract.of(
        declare_with_field(Annotated[int, Field(multiple_of=3)])
    ).describe()
    assert described["$defs"]["Shape"]["properties"]["value"] == {
        **INTEGER,
        "multipleOf": 3,
    }


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "config"),
    [
        pytest.param(Annotated[int, Strict()], {}, id="strict integer"),
        pytest.param(Annotated[str, Strict()], {}, id="strict string"),
        pytest.param(int, {"strict": True}, id="strict record"),
        pytest.param(str, {"strict": True}, id="strict record without integers"),
        pytest.param(
            Annotated[date, Strict(), BeforeValidator(_identity)],
            {},
            id="strict date behind a before validator",
        ),
    ],
)
def test_strict_mode_is_refused(annotation: object, config: dict[str, object]) -> None:
    record = create_model(
        "Strictly",
        __config__=cast("ConfigDict", config),
        n=(cast("Any", annotation), ...),
    )
    with pytest.raises(ContractDefinitionError, match="strict"):
        declare_with_field(record)


@pytest.mark.spec("CON-16")
@pytest.mark.parametrize(
    ("annotation", "config"),
    [
        pytest.param(_strings(strip_whitespace=True), {}, id="strip alone"),
        pytest.param(_strings(to_upper=True), {}, id="upper alone"),
        pytest.param(str, {"str_to_lower": True}, id="lower setting, no length"),
        pytest.param(_strings(min_length=1), {}, id="length without conversion"),
    ],
)
def test_conversions_without_a_stated_length_are_accepted(
    annotation: object, config: dict[str, object]
) -> None:
    record = create_model(
        "Cased", __config__=cast("ConfigDict", config), s=(cast("Any", annotation), ...)
    )
    declare_with_field(record)


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("config", "field", "becomes"),
    [
        pytest.param(
            {"str_to_lower": True}, (str, "ABC"), "turn into 'abc'", id="lower"
        ),
        pytest.param(
            {"str_to_upper": True}, (str, "abc"), "turn into 'ABC'", id="upper"
        ),
        pytest.param(
            {"str_strip_whitespace": True}, (str, " a "), "turn into 'a'", id="strip"
        ),
        pytest.param(
            {"str_strip_whitespace": True},
            (str, Field(" a ", min_length=2)),
            "converts its value",
            id="strip below min length",
        ),
    ],
)
def test_defaults_are_checked_under_the_record_settings(
    config: dict[str, object], field: tuple[object, object], becomes: str
) -> None:
    record = create_model(
        "Set", __config__=cast("ConfigDict", config), s=cast("Any", field)
    )
    with pytest.raises(ContractDefinitionError, match=re.escape(becomes)):
        declare_with_field(record)


@pytest.mark.spec("CON-16")
def test_default_already_in_the_record_settings_form_is_accepted() -> None:
    record = create_model(
        "Set", __config__=ConfigDict(str_to_lower=True), s=(str, "abc")
    )
    described = Contract.of(declare_with_field(record)).describe()
    assert described["$defs"]["Set"]["properties"]["s"]["default"] == "abc"


class WithPrivate(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)


class WithPlainPrivate(BaseModel):
    x: int
    _seen: dict[str, int] = {}


class InheritsPrivate(WithPrivate):
    y: int = 0


class InheritsPrivateTwice(InheritsPrivate):
    z: int = 0


class OwnPostInit(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)

    def model_post_init(self, context: object, /) -> None:
        self.x = 2


class InheritsOwnPostInit(OwnPostInit):
    y: int = 0


def wrapped_model_post_init(self: BaseModel, context: object, /) -> None:
    """An author's model_post_init, under the name of Pydantic's wrapper."""
    object.__setattr__(self, "x", 2)


setattr(wrapped_model_post_init, "__wrapped__", WithPrivate.model_post_init)  # noqa: B010


class NamedLikePydantics(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)

    model_post_init = wrapped_model_post_init


class Retargeted(OwnPostInit):
    pass


# Pydantic's wrapper now says it wraps Pydantic's own function; it still runs
# the author's.
setattr(Retargeted.model_post_init, "__wrapped__", WithPrivate.model_post_init)  # noqa: B010


class PostInitObject:
    """A callable with the code of Pydantic's wrapper, whose __wrapped__ raises."""

    __code__ = InheritsPrivate.model_post_init.__code__

    @property
    def __wrapped__(self) -> object:
        raise RuntimeError("__wrapped__ was read")

    def __call__(self, context: object, /) -> None:
        pass


class ObjectPostInit(BaseModel):
    x: int

    model_post_init: ClassVar[Any] = PostInitObject()


class PrivateObjectPostInit(ObjectPostInit):
    _cache: int = PrivateAttr(0)


def _forged(globals_: dict[str, Any], held: object) -> types.FunctionType:
    """A function with the code of Pydantic's wrapper."""
    wrapper = InheritsPrivate.model_post_init
    return types.FunctionType(
        wrapper.__code__, globals_, None, None, (types.CellType(held),)
    )


class ForgedClosure(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)

    # Pydantic's wrapper code and globals, holding the author's function.
    model_post_init = _forged(
        InheritsPrivate.model_post_init.__globals__, wrapped_model_post_init
    )


class ForgedGlobals(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)

    # Holding Pydantic's function, but calling the author's first.
    model_post_init = _forged(
        {
            **InheritsPrivate.model_post_init.__globals__,
            "init_private_attributes": wrapped_model_post_init,
        },
        WithPrivate.model_post_init,
    )


class HidesNamespace(type(BaseModel)):  # type: ignore[misc]
    """A metaclass whose __dict__ is a property, which getattr_static skips."""

    @property
    def __dict__(cls) -> Mapping[str, Any]:  # type: ignore[override]
        return cast("Mapping[str, Any]", type.__dict__["__dict__"].__get__(cls))


class PostInitBehindMetaclass(InheritsPrivate, metaclass=HidesNamespace):
    def model_post_init(self, context: object, /) -> None:
        self.x = 2


class UnhashableMeta(type):
    def __hash__(cls) -> int:
        raise RuntimeError("hashed")


class UnhashableMetaclass(type(BaseModel), metaclass=UnhashableMeta):  # type: ignore[misc]
    pass


class PrivateUnderUnhashableMetaclass(WithPrivate, metaclass=UnhashableMetaclass):
    pass


class NamespaceKey(str):
    """A key equal to nothing, which raises when compared once switched on."""

    raising = False
    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        if NamespaceKey.raising:
            raise RuntimeError("compared")
        return False


class OddNamespaceKey(WithPrivate):
    # Looking up "model_post_init" in this namespace now compares this key.
    locals()[NamespaceKey("model_post_init")] = WithPrivate.model_post_init

    def model_post_init(self, context: object, /) -> None:
        self.x = 2


NamespaceKey.raising = True


class RenamedPostInit(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)

    def other_hook(self, context: object, /) -> None:
        self.x = 2

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[BaseModel], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = handler(source)
        cast("dict[str, Any]", schema)["post_init"] = "other_hook"
        return schema


@pytest.mark.spec("CON-13", "CON-16")
@pytest.mark.parametrize(
    "record",
    [
        WithPrivate,
        WithPlainPrivate,
        InheritsPrivate,
        InheritsPrivateTwice,
    ],
)
def test_private_attributes_are_accepted(record: type[BaseModel]) -> None:
    # Pydantic generated a model_post_init, so the record's post_init was checked.
    assert record.model_post_init is not BaseModel.model_post_init
    described = Contract.of(declare_with_field(record)).describe()
    assert "_cache" not in described["$defs"][record.__name__]["properties"]


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    "record",
    [
        OwnPostInit,
        InheritsOwnPostInit,
        NamedLikePydantics,
        Retargeted,
        ObjectPostInit,
        PrivateObjectPostInit,
        ForgedClosure,
        ForgedGlobals,
    ],
)
def test_own_model_post_init_is_refused(record: type[BaseModel]) -> None:
    with pytest.raises(ContractDefinitionError, match="defines model_post_init"):
        declare_with_field(record)


@pytest.mark.spec("CON-17", "TRN-10")
@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (PostInitBehindMetaclass, "has the metaclass HidesNamespace, not Pydantic's"),
        (
            PrivateUnderUnhashableMetaclass,
            "has the metaclass UnhashableMetaclass, not Pydantic's",
        ),
        (OddNamespaceKey, "has a namespace key that is not text in OddNamespaceKey"),
    ],
)
def test_a_record_class_pydantic_cannot_read_plainly_is_refused(
    record: type[BaseModel], refusal: str
) -> None:
    # A metaclass, or a namespace key that compares by code of its own, can
    # change what Pydantic reads from the class.
    with pytest.raises(ContractDefinitionError, match=re.escape(refusal)):
        declare_with_field(record)


@pytest.mark.spec("CON-13", "CON-17")
def test_post_init_under_another_name_is_refused() -> None:
    with pytest.raises(
        ContractDefinitionError, match="runs a post-init other than model_post_init"
    ):
        declare_with_field(RenamedPostInit)


# Pydantic builds a record's private attributes after validation, in the
# model_post_init it generates. Each factory below notes whether Pydantic
# called it with arguments: from 2.13, unlike 2.12, a factory that takes one
# positional argument is handed the validated data, which it can change.
_FACTORY_ARGUMENTS: list[bool] = []
_NOTHING = object()


def _takes_data(data: dict[str, Any]) -> int:
    _FACTORY_ARGUMENTS.append(True)
    data["x"] = "not an int"
    return 0


def _takes_data_positionally(data: dict[str, Any], /) -> int:
    _FACTORY_ARGUMENTS.append(True)
    return 0


def _takes_two(data: dict[str, Any], other: object) -> int:
    _FACTORY_ARGUMENTS.append(True)
    return 0


def _takes_keyword(*, data: dict[str, Any]) -> int:
    _FACTORY_ARGUMENTS.append(True)
    return 0


def _takes_nothing() -> int:
    _FACTORY_ARGUMENTS.append(False)
    return 0


def _data_optional(data: object = _NOTHING) -> int:
    _FACTORY_ARGUMENTS.append(data is not _NOTHING)
    return 0


def _any_arguments(*args: object, **kwargs: object) -> int:
    _FACTORY_ARGUMENTS.append(bool(args or kwargs))
    return 0


def _both_given(first: object, second: object) -> int:
    # Called with arguments Pydantic adds, it would raise TypeError.
    _FACTORY_ARGUMENTS.append(False)
    return 0


class _CallTakesData:
    def __call__(self, data: dict[str, Any]) -> int:
        _FACTORY_ARGUMENTS.append(True)
        return 0


class _CallTakesNothing:
    def __call__(self) -> int:
        _FACTORY_ARGUMENTS.append(False)
        return 0


_PRIVATE_FACTORIES: list[Callable[..., int]] = [
    _takes_data,
    _takes_data_positionally,
    _takes_two,
    _takes_keyword,
    lambda data: _takes_data(data),
    functools.partial(_takes_two, other=None),
    functools.partial(_takes_two, {}),
    _CallTakesData(),
    _takes_nothing,
    _data_optional,
    _any_arguments,
    lambda: _takes_nothing(),
    _CallTakesNothing(),
    functools.partial(_both_given, 1, 2),
]


def _with_private_factory(factory: Callable[..., int]) -> type[BaseModel]:
    class Cached(BaseModel):
        x: int
        _made: int = PrivateAttr(default_factory=factory)

    return Cached


@pytest.mark.spec("CON-13", "CON-16", "CON-17")
@pytest.mark.parametrize("factory", _PRIVATE_FACTORIES)
def test_private_default_factory_is_refused_when_pydantic_would_pass_it_arguments(
    factory: Callable[..., int],
) -> None:
    record = _with_private_factory(factory)
    # What the installed Pydantic does with the factory when it builds a value.
    _FACTORY_ARGUMENTS.clear()
    try:
        record.model_validate({"x": 1})
    except TypeError:
        # It needs arguments Pydantic does not pass (always so before 2.13).
        takes_arguments = True
    else:
        assert len(_FACTORY_ARGUMENTS) == 1
        takes_arguments = _FACTORY_ARGUMENTS[0]
    if takes_arguments:
        with pytest.raises(
            ContractDefinitionError,
            match=r"record Shape\.value has the private attribute _made, whose "
            r"default factory takes arguments",
        ):
            declare_with_field(record)
    else:
        declare_with_field(record)


class TakesDataPrivate(BaseModel):
    items: list[int]
    _p: int = PrivateAttr(default_factory=_takes_data)


class InheritsTakesDataPrivate(TakesDataPrivate):
    y: int = 0


class _OwnPrivateAttr(type(PrivateAttr())):  # type: ignore[misc]
    """A private attribute that says how Pydantic must call its factory."""

    @property
    def default_factory_takes_validated_data(self) -> bool:
        return True


class OwnPrivateAttr(BaseModel):
    x: int
    _p: int = _OwnPrivateAttr(default_factory=lambda: 0)


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (
            TakesDataPrivate,
            "record Shape.value has the private attribute _p, "
            "whose default factory takes arguments",
        ),
        (
            InheritsTakesDataPrivate,
            "record Shape.value has the private attribute _p, "
            "whose default factory takes arguments",
        ),
        (
            OwnPrivateAttr,
            "record Shape.value has the private attribute _p, "
            "which is a _OwnPrivateAttr, not what Pydantic's PrivateAttr builds",
        ),
    ],
)
def test_private_attribute_pydantic_would_hand_the_data_is_refused(
    record: type[BaseModel], refusal: str
) -> None:
    with pytest.raises(ContractDefinitionError, match=re.escape(refusal)):
        declare_with_field(record)


# Unknown fields


@pytest.mark.spec("CON-6")
def test_unknown_request_fields_are_ignored() -> None:
    request = Contract.of(declare()).actions["ping"].request
    value = request.model_validate({"text": "hi", "added_later": 1})
    assert value.model_dump() == {"text": "hi"}


@pytest.mark.spec("CON-7")
def test_unknown_reply_fields_are_ignored() -> None:
    reply = Contract.of(declare()).actions["ping"].reply
    assert reply is not None
    value = reply.model_validate_json('{"text": "hi", "count": 2, "added_later": 1}')
    assert value.model_dump() == {"text": "hi", "count": 2}


def _refusing(extra: Literal["forbid", "allow"]) -> type[BaseModel]:
    return create_model("Strict", __config__=ConfigDict(extra=extra), text=(str, ...))


@pytest.mark.spec("CON-6", "CON-17")
@pytest.mark.parametrize("extra", ["forbid", "allow"])
def test_request_record_that_keeps_or_refuses_unknown_fields_is_rejected(
    extra: Literal["forbid", "allow"],
) -> None:
    strict = _refusing(extra)

    def ping(self: object, request: strict) -> None: ...  # type: ignore[valid-type]

    with pytest.raises(ContractDefinitionError, match="unknown fields"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


@pytest.mark.spec("CON-7", "CON-17")
@pytest.mark.parametrize("extra", ["forbid", "allow"])
def test_reply_record_that_keeps_or_refuses_unknown_fields_is_rejected(
    extra: Literal["forbid", "allow"],
) -> None:
    strict = _refusing(extra)

    def ping(self: object, request: Ping) -> strict:  # type: ignore[valid-type]
        ...

    with pytest.raises(ContractDefinitionError, match="unknown fields"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


@pytest.mark.spec("CON-6", "CON-17")
def test_nested_record_that_refuses_unknown_fields_is_rejected() -> None:
    with pytest.raises(ContractDefinitionError, match="unknown fields"):
        declare_with_field(list[_refusing("forbid")])  # type: ignore[misc]


# Description


@pytest.mark.spec("CON-16")
def test_description_gives_each_action_and_its_types() -> None:
    @contract("orders.v1")
    class Orders(Protocol):
        @action(idempotent=True)
        def place(self, request: Ping) -> Pong: ...

        @action
        def cancel(self, request: Ping) -> None: ...

    description = Contract.of(Orders).describe()
    assert description["contract"] == "orders.v1"
    assert description["jsonSchemaDialect"] == (
        "https://json-schema.org/draft/2020-12/schema"
    )
    assert description["actions"] == {
        "place": {
            "idempotent": True,
            "request": {"$ref": "#/$defs/Ping"},
            "reply": {"$ref": "#/$defs/Pong"},
            "errors": {},
        },
        "cancel": {
            "idempotent": False,
            "request": {"$ref": "#/$defs/Ping"},
            "reply": None,
            "errors": {},
        },
    }
    assert set(description["$defs"]) == {"Ping", "Pong"}
    assert description["$defs"]["Ping"]["properties"]["text"]["type"] == "string"
    json.dumps(description)


@pytest.mark.spec("CON-16")
def test_description_names_each_record_once() -> None:
    class Order(BaseModel):
        lines: list[Line]
        placed: AwareDatetime

    @contract("orders.v1")
    class Orders(Protocol):
        @action
        def place(self, request: Order) -> Line: ...

    defs = Contract.of(Orders).describe()["$defs"]
    assert set(defs) == {"Order", "Line"}
    properties = defs["Order"]["properties"]
    assert properties["lines"]["items"] == {"$ref": "#/$defs/Line"}
    assert properties["placed"]["format"] == "date-time"


# Implementations


@contract("greeter.v1")
class Greeter(Protocol):
    @action(idempotent=True)
    def greet(self, request: Ping) -> Pong: ...

    @action
    def forget(self, request: Ping) -> None: ...


@pytest.mark.spec("CON-18", "CON-19")
def test_implementation_with_every_action_is_accepted() -> None:
    class Impl:
        def greet(self, request: Ping) -> Pong:
            return Pong(text=request.text)

        def forget(self, request: Ping) -> None:
            return None

        def _helper(self) -> None:
            return None

    assert implements(Greeter)(Impl) is Impl


@pytest.mark.spec("CON-18", "CON-19")
def test_implementation_may_subclass_the_declaration() -> None:
    @implements(Greeter)
    class Impl(Greeter):
        def greet(self, request: Ping) -> Pong:
            return Pong(text=request.text)

        def forget(self, request: Ping) -> None:
            return None

    assert Impl().greet(Ping(text="hi")) == Pong(text="hi")


@pytest.mark.spec("CON-18", "CON-19")
def test_missing_action_is_rejected() -> None:
    class Impl:
        def greet(self, request: Ping) -> Pong:
            return Pong(text=request.text)

    with pytest.raises(ContractDefinitionError, match="action 'forget' is missing"):
        implements(Greeter)(Impl)


@pytest.mark.spec("CON-18", "CON-19")
def test_action_left_as_the_declaration_is_missing() -> None:
    class Impl(Greeter):
        def greet(self, request: Ping) -> Pong:
            return Pong(text=request.text)

    with pytest.raises(ContractDefinitionError, match="action 'forget' is missing"):
        implements(Greeter)(Impl)  # type: ignore[type-abstract]


@pytest.mark.spec("CON-18", "CON-19")
@pytest.mark.parametrize(
    ("request_type", "reply_type", "message"),
    [
        (Pong, Pong, "takes Pong, but greeter.v1 declares Ping"),
        (Ping, Ping, "returns Ping, but greeter.v1 declares Pong"),
        (Ping, None, "returns None, but greeter.v1 declares Pong"),
        (Ping, Pong | None, "but greeter.v1 declares Pong"),
    ],
)
def test_mismatched_types_are_rejected(
    request_type: object, reply_type: object, message: str
) -> None:
    def greet(self: object, request: request_type) -> reply_type:  # type: ignore[valid-type]
        ...

    def forget(self: object, request: Ping) -> None: ...

    impl = type("Impl", (), {"greet": greet, "forget": forget})
    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        implements(Greeter)(impl)


@pytest.mark.spec("CON-18", "CON-19")
def test_empty_reply_is_implemented_as_none() -> None:
    def greet(self: object, request: Ping) -> Pong: ...

    def forget(self: object, request: Ping) -> Pong: ...

    impl = type("Impl", (), {"greet": greet, "forget": forget})
    with pytest.raises(ContractDefinitionError, match="declares an empty reply"):
        implements(Greeter)(impl)


@pytest.mark.spec("CON-19")
def test_implementation_annotates_its_types() -> None:
    def greet(self, request):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
        ...

    def forget(self: object, request: Ping) -> None: ...

    impl = type("Impl", (), {"greet": greet, "forget": forget})
    with pytest.raises(ContractDefinitionError, match="annotate its request"):
        implements(Greeter)(impl)


@pytest.mark.spec("CON-19")
@pytest.mark.parametrize("function", _ping_signatures(), ids=lambda f: f.__name__)
def test_implementation_takes_self_and_request(function: Callable[..., object]) -> None:
    def forget(self: object, request: Ping) -> None: ...

    impl = type("Impl", (), {"greet": function, "forget": forget})
    with pytest.raises(ContractDefinitionError, match=r"\(self, request\)"):
        implements(Greeter)(impl)


@pytest.mark.spec("CON-19")
@pytest.mark.parametrize("wrapper", [staticmethod, classmethod])
def test_action_is_an_instance_method(wrapper: Callable[[Any], object]) -> None:
    def greet(request: Ping) -> Pong: ...

    def forget(self: object, request: Ping) -> None: ...

    impl = type("Impl", (), {"greet": wrapper(greet), "forget": forget})
    with pytest.raises(ContractDefinitionError, match="instance method"):
        implements(Greeter)(impl)


@pytest.mark.spec("CON-19")
def test_implementation_is_synchronous() -> None:
    async def greet(self: object, request: Ping) -> Pong: ...

    def forget(self: object, request: Ping) -> None: ...

    impl = type("Impl", (), {"greet": greet, "forget": forget})
    with pytest.raises(ContractDefinitionError, match="synchronous"):
        implements(Greeter)(impl)


def _generator(self: object, request: Ping) -> Pong:  # type: ignore[misc]
    yield Pong(text=request.text)


async def _async_generator(self: object, request: Ping) -> Pong:  # type: ignore[misc]
    yield Pong(text=request.text)


async def _coroutine(self: object, request: Ping) -> Pong:
    return Pong(text=request.text)


def _marked(self: object, request: Ping) -> Pong:
    return Pong(text=request.text)


def _greet(self: object, request: Ping) -> Pong:
    return Pong(text=request.text)


def _forget(self: object, request: Ping) -> None:
    return None


def _generator_forget(self: object, request: Ping) -> None:  # type: ignore[misc]
    yield


@types.coroutine
def _iterable_coroutine(self: object, request: Ping) -> Pong:
    yield Pong(text=request.text)


# Every kind of function whose call returns an object standing for the work,
# not the reply: a generator, an async generator, a coroutine, a generator
# made awaitable by types.coroutine, a plain function marked as a coroutine
# function, and contextlib's context-manager wrappers. For an empty reply the
# body would never even run.
_NOT_RETURNING = [
    pytest.param("greet", _generator, "generator", id="generator"),
    pytest.param("forget", _generator_forget, "generator", id="generator-empty"),
    pytest.param("greet", _iterable_coroutine, "generator", id="types-coroutine"),
    pytest.param("greet", _async_generator, "synchronous", id="async-generator"),
    pytest.param("greet", _coroutine, "synchronous", id="coroutine"),
    pytest.param(
        "greet", inspect.markcoroutinefunction(_marked), "synchronous", id="marked"
    ),
    pytest.param(
        "greet",
        contextlib.contextmanager(cast(Any, _generator)),
        "context manager",
        id="contextmanager",
    ),
    pytest.param(
        "greet",
        contextlib.asynccontextmanager(cast(Any, _async_generator)),
        "context manager",
        id="asynccontextmanager",
    ),
]


@pytest.mark.spec("CON-18", "CON-19")
@pytest.mark.parametrize(("name", "function", "message"), _NOT_RETURNING)
def test_action_whose_call_does_not_return_the_reply_is_rejected(
    name: str, function: Callable[..., object], message: str
) -> None:
    members = {"greet": _greet, "forget": _forget, name: function}
    impl = type("Impl", (), members)
    with pytest.raises(ContractDefinitionError, match=message) as raised:
        implements(Greeter)(impl)
    assert f"action {name!r}" in str(raised.value)


@pytest.mark.spec("CON-18", "CON-19")
@pytest.mark.parametrize(
    "member",
    [
        pytest.param(staticmethod(_greet), id="staticmethod"),
        pytest.param(classmethod(_greet), id="classmethod"),
        pytest.param(property(cast(Any, _greet)), id="property"),
        pytest.param(
            functools.cached_property(cast(Any, _greet)), id="cached_property"
        ),
        pytest.param(functools.partial(_greet), id="partial"),
        pytest.param(functools.partialmethod(_greet), id="partialmethod"),
        pytest.param(functools.singledispatchmethod(_greet), id="singledispatch"),
        pytest.param(len, id="builtin"),
        pytest.param(type("Call", (), {"__call__": _greet})(), id="callable"),
    ],
)
def test_action_that_is_not_a_function_is_rejected(member: object) -> None:
    impl = type("Impl", (), {"greet": member, "forget": _forget})
    with pytest.raises(
        ContractDefinitionError, match="action 'greet' must be an instance method"
    ):
        implements(Greeter)(impl)


@pytest.mark.spec("CON-18", "CON-19")
def test_action_that_builds_a_generator_inside_is_accepted() -> None:
    class Impl:
        def greet(self, request: Ping) -> Pong:
            return Pong(text="".join(c for c in request.text))

        def forget(self, request: Ping) -> None:
            return None

    assert implements(Greeter)(Impl) is Impl
    assert Impl().greet(Ping(text="hi")) == Pong(text="hi")


def test_declared_action_is_not_a_generator() -> None:
    def ping(self: object, request: Ping) -> Pong:  # type: ignore[misc]
        yield Pong(text=request.text)

    with pytest.raises(ContractDefinitionError, match=r"'ping'.*generator"):
        contract("echo.v1")(declaration("Echo", {"ping": action(ping)}))


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(lambda self: None, id="method"),
        pytest.param(staticmethod(lambda: None), id="staticmethod"),
        pytest.param(classmethod(lambda cls: None), id="classmethod"),
    ],
)
def test_extra_public_method_is_rejected(member: object) -> None:
    def greet(self: object, request: Ping) -> Pong: ...

    def forget(self: object, request: Ping) -> None: ...

    impl = type("Impl", (), {"greet": greet, "forget": forget, "extra": member})
    with pytest.raises(ContractDefinitionError, match="'extra' is not an action"):
        implements(Greeter)(impl)


def test_extra_public_method_in_a_base_class_is_rejected() -> None:
    class Base:
        def extra(self) -> None:
            return None

    class Impl(Base):
        def greet(self, request: Ping) -> Pong:
            return Pong(text=request.text)

        def forget(self, request: Ping) -> None:
            return None

    with pytest.raises(ContractDefinitionError, match="'extra' is not an action"):
        implements(Greeter)(Impl)


def test_implements_takes_a_contract() -> None:
    class Plain:
        pass

    with pytest.raises(ContractDefinitionError, match="is not a contract"):
        implements(Plain)


def test_subclass_of_a_contract_is_not_a_contract() -> None:
    class Sub(Greeter):
        pass

    with pytest.raises(ContractDefinitionError, match="is not a contract"):
        Contract.of(Sub)


def test_implements_decorates_a_class() -> None:
    with pytest.raises(ContractDefinitionError, match="decorates a class"):
        implements(Greeter)(lambda: None)  # type: ignore[arg-type]
