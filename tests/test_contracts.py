"""Contracts, actions, the portable types and implementation checks (spec 01).

Everything here happens when a module defining a contract or an
implementation is loaded, before any transport exists, so these tests run
without one (T2 does not apply yet).
"""

import dataclasses
import enum
import json
import re
import types
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, Optional, Protocol, TypedDict, cast
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
    PlainSerializer,
    PlainValidator,
    RootModel,
    Strict,
    StringConstraints,
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
    Contract,
    ContractDefinitionError,
    action,
    contract,
    implements,
)


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
    assert actions["place"] == Action("place", True, Ping, Pong)
    assert actions["cancel"] == Action("cancel", False, Ping, None)


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
        bytes,
        Decimal,
        AwareDatetime,
        date,
        timedelta,
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
    with pytest.raises(ContractDefinitionError, match="not a portable form"):
        declare_with_field(annotation)


@pytest.mark.spec("CON-13", "CON-17")
def test_timestamp_carries_an_offset() -> None:
    with pytest.raises(ContractDefinitionError, match="AwareDatetime"):
        declare_with_field(datetime)


@pytest.mark.spec("CON-13", "CON-17")
def test_enum_values_are_strings() -> None:
    Mixed = enum.Enum("Mixed", {"A": "a", "B": 2})
    with pytest.raises(ContractDefinitionError, match="not a portable form"):
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
        "field Deep.values has a custom serializer",
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
def test_validators_do_not_change_the_wire_form() -> None:
    class Checked(BaseModel):
        code: Annotated[str, AfterValidator(str.upper)]
        count: int

        @field_validator("count")
        @classmethod
        def positive(cls, value: int) -> int:
            return abs(value)

    declare_with_field(Checked)


def _custom_json_schema() -> type[BaseModel]:
    class Custom(BaseModel):
        value: int

        @classmethod
        def __get_pydantic_json_schema__(
            cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
        ) -> JsonSchemaValue:
            return {"type": "string"}

    return Custom


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("make", "message"),
    [
        pytest.param(
            lambda: create_model(
                "Shown",
                n=(
                    cast("Any", Annotated[int, WithJsonSchema({"type": "string"})]),
                    ...,
                ),
            ),
            "field Shown.n has a JSON Schema override",
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
            "field Shown.n has a JSON Schema override",
            id="nested WithJsonSchema",
        ),
        pytest.param(
            lambda: create_model(
                "Shown", n=(int, Field(json_schema_extra={"type": "string"}))
            ),
            "field Shown.n has a JSON Schema override",
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
            "record Shown has json_schema_extra set",
            id="config json_schema_extra",
        ),
        pytest.param(
            _custom_json_schema,
            "record Custom has __get_pydantic_json_schema__ defined",
            id="__get_pydantic_json_schema__",
        ),
    ],
)
def test_record_whose_description_is_rewritten_is_rejected(
    make: Callable[[], type[BaseModel]], message: str
) -> None:
    record = make()

    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        contract("echo.v1")(declaration("Echo", {"send": action(send)}))


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


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        pytest.param(
            Annotated[int, GetPydanticSchema(lambda t, h: core_schema.str_schema())],
            "field Shape.value uses GetPydanticSchema",
            id="GetPydanticSchema",
        ),
        pytest.param(
            list[
                Annotated[int, GetPydanticSchema(lambda t, h: core_schema.str_schema())]
            ],
            "field Shape.value[*] uses GetPydanticSchema",
            id="nested GetPydanticSchema",
        ),
        pytest.param(
            Annotated[int, _OwnSchema()],
            "field Shape.value uses _OwnSchema",
            id="marker with its own schema",
        ),
        pytest.param(
            Annotated[int, PlainValidator(int)],
            "field Shape.value uses PlainValidator",
            id="PlainValidator",
        ),
        pytest.param(
            _hooked_enum("__get_pydantic_json_schema__"),
            "enum Color at Shape.value defines __get_pydantic_json_schema__",
            id="enum json schema hook",
        ),
        pytest.param(
            _hooked_enum("__get_pydantic_core_schema__"),
            "enum Color at Shape.value defines __get_pydantic_core_schema__",
            id="enum core schema hook",
        ),
        pytest.param(
            _record_hook("__get_pydantic_core_schema__", classmethod),
            "record Shape.value has __get_pydantic_core_schema__ defined",
            id="record core schema hook",
        ),
        pytest.param(
            _record_hook("__get_pydantic_json_schema__", staticmethod),
            "record Shape.value has __get_pydantic_json_schema__ defined",
            id="record json schema staticmethod",
        ),
    ],
)
def test_type_that_builds_its_own_schema_is_rejected(
    annotation: object, message: str
) -> None:
    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        declare_with_field(annotation)


@pytest.mark.spec("CON-16", "CON-17")
@pytest.mark.parametrize(
    "config",
    [
        pytest.param(ConfigDict(val_json_bytes="base64"), id="val_json_bytes"),
        pytest.param(
            ConfigDict(json_schema_mode_override="validation"),
            id="json_schema_mode_override",
        ),
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
        pytest.param(int, True, id="int = True"),
        pytest.param(str, 3, id="str = 3"),
        pytest.param(Annotated[int, Field(ge=0)], -1, id="ge=0 = -1"),
    ],
)
def test_default_outside_the_field_type_is_rejected(
    annotation: object, default: object
) -> None:
    record = create_model("Defaulted", n=(cast("Any", annotation), default))
    with pytest.raises(ContractDefinitionError, match="is not a value of its type"):
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
            "0",
            id="BeforeValidator None",
        ),
        pytest.param(
            Annotated[int, WrapValidator(lambda v, h: h(0 if v is None else v))],
            None,
            "0",
            id="WrapValidator None",
        ),
        pytest.param(Annotated[int, BeforeValidator(int)], "3", "3", id="int('3')"),
        pytest.param(
            Annotated[list[str], BeforeValidator(_split_commas)],
            "a,b",
            "['a', 'b']",
            id="split commas",
        ),
        pytest.param(
            Annotated[str, AfterValidator(str.lower)], "ABC", "'abc'", id="lower"
        ),
        pytest.param(
            Annotated[Decimal, BeforeValidator(Decimal)],
            0,
            "Decimal('0')",
            id="Decimal = 0",
        ),
        pytest.param(
            Annotated[Decimal, BeforeValidator(Decimal)],
            3,
            "Decimal('3')",
            id="Decimal = 3",
        ),
        pytest.param(
            list[Annotated[Decimal, BeforeValidator(Decimal)]],
            [3],
            "[Decimal('3')]",
            id="list[Decimal] = [3]",
        ),
        pytest.param(Annotated[int, BeforeValidator(int)], True, "1", id="int = True"),
        pytest.param(
            Annotated[float, BeforeValidator(float)], True, "1.0", id="float = True"
        ),
        pytest.param(Annotated[int, BeforeValidator(int)], 3.0, "3", id="int = 3.0"),
        pytest.param(
            list[Annotated[int, BeforeValidator(int)]],
            [3.0],
            "[3]",
            id="list[int] = [3.0]",
        ),
    ],
)
def test_default_that_validators_change_is_rejected(
    annotation: object, default: object, becomes: str
) -> None:
    record = create_model("Defaulted", n=(cast("Any", annotation), default))
    with pytest.raises(
        ContractDefinitionError, match=re.escape(f"validators turn into {becomes}")
    ):
        declare_with_field(record)


@pytest.mark.spec("CON-13", "CON-16")
def test_defaults_of_the_field_type_are_allowed() -> None:
    class Defaults(BaseModel):
        maybe: int | None = None
        colour: Colour = Colour.RED
        size: Literal["s", "m"] = "s"
        price: Decimal = Decimal("1.50")
        span: timedelta = timedelta(seconds=90)
        raw: bytes = b""
        ratio: float = 1
        lines: list[Line] = []
        counts: dict[str, int] = {"a": 1}
        line: Line = Line(sku="A1", quantity=1)
        day: date = date(2026, 1, 1)
        when: AwareDatetime = datetime(2026, 1, 1, tzinfo=UTC)
        positive: int | None = Field(None, gt=0)
        upper: Annotated[str, AfterValidator(str.upper)] = "ABC"
        parsed: Annotated[int, BeforeValidator(int)] = 3
        made: list[int] = Field(default_factory=list)
        half: float = 1.5
        widened: Annotated[float, BeforeValidator(float)] = 1
        weights: dict[str, float] = {"a": 1}

    declare_with_field(Defaults)


@pytest.mark.spec("CON-16")
def test_documentation_and_constraints_are_allowed() -> None:
    class Documented(BaseModel):
        count: int = Field(
            ge=0, description="How many.", title="Count", examples=[1], deprecated=True
        )
        name: Annotated[str, Field(min_length=1, max_length=20)]
        code: Annotated[str, StringConstraints(pattern=r"^[A-Z]+$")]
        level: Annotated[int, annotated_types.Ge(0), annotated_types.Lt(10)]
        tags: Annotated[list[str], annotated_types.Len(0, 5)]
        exact: Annotated[int, Strict()]
        lower: Annotated[str, BeforeValidator(str.lower)]
        upper: Annotated[str, AfterValidator(str.upper)]
        trimmed: Annotated[str, WrapValidator(lambda v, h: h(v))]

        @model_validator(mode="after")
        def accept(self) -> "Documented":
            return self

    class Derived(Documented):
        extra: bool = False

    declare_with_field(Derived)
    defs = Contract.of(declare_with_field(Documented, "docs.v1")).describe()["$defs"]
    assert defs["Documented"]["properties"]["count"]["minimum"] == 0


@pytest.mark.spec("CON-16")
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


@pytest.mark.spec("CON-16")
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
        "field Early.later refers to 'Later', which is not defined when the "
        "contract is declared; define Later before the contract"
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
def test_default_that_cannot_be_written_as_json_is_named() -> None:
    record = create_model("Raw", data=(bytes, b"\xff"))
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape("field Shape.value.data has the default b'\\xff', which "),
    ):
        declare_with_field(record)


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
        },
        "cancel": {
            "idempotent": False,
            "request": {"$ref": "#/$defs/Ping"},
            "reply": None,
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
        total: Decimal

    @contract("orders.v1")
    class Orders(Protocol):
        @action
        def place(self, request: Order) -> Line: ...

    defs = Contract.of(Orders).describe()["$defs"]
    assert set(defs) == {"Order", "Line"}
    properties = defs["Order"]["properties"]
    assert properties["lines"]["items"] == {"$ref": "#/$defs/Line"}
    assert properties["placed"]["format"] == "date-time"
    assert properties["total"]["type"] == "string"


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
