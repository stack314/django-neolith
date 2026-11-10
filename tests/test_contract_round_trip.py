"""Round trips between a contract's records and its published description.

For every contract that is accepted, these properties hold over both
directions of the wire (CON-16):

- a value generated from the description validates into the record;
- what the record then puts on the wire validates against the description;
- reading that back gives an equal record.

The contracts themselves are generated too: records are built at random from
the portable forms, so the properties cover shapes nobody wrote down. Runs are
derandomized, so CI sees the same examples every time (CONTRIBUTING T3).
"""

import enum
import itertools
import json
import types
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator
from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter, create_model

from neolith import Contract, action, contract

_SETTINGS = settings(
    derandomize=True,
    database=None,
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

_DURATION = TypeAdapter(timedelta)
# Strategies for the formats the description uses that hypothesis-jsonschema
# does not know; it generates "date" and "date-time" (RFC 3339, always with an
# offset) itself. Durations are Pydantic's ISO 8601 text.
_FORMATS: dict[str, st.SearchStrategy[str]] = {
    "decimal": st.decimals(allow_nan=False, allow_infinity=False).map(str),
    "binary": st.text(),
    "duration": st.timedeltas(
        min_value=timedelta(days=-9999), max_value=timedelta(days=9999)
    ).map(lambda value: _DURATION.dump_python(value, mode="json")),
}

_names = itertools.count()


def _name(prefix: str) -> str:
    return f"{prefix}{next(_names)}"


_SCALARS: list[object] = [
    bool,
    int,
    float,
    str,
    bytes,
    Decimal,
    AwareDatetime,
    date,
    timedelta,
    Annotated[int, Field(ge=0, le=1000)],
    Annotated[str, Field(min_length=1, max_length=8)],
]


@st.composite
def _enums(draw: st.DrawFn) -> object:
    values = draw(
        st.lists(
            st.text("abcdef", min_size=1, max_size=4),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    return enum.StrEnum(_name("E"), {f"M{i}": v for i, v in enumerate(values)})


@st.composite
def _literals(draw: st.DrawFn) -> object:
    values = draw(
        st.lists(
            st.text("xyz", min_size=1, max_size=3), min_size=1, max_size=3, unique=True
        )
    )
    return Literal[tuple(values)]


def _records(children: st.SearchStrategy[object]) -> st.SearchStrategy[object]:
    @st.composite
    def build(draw: st.DrawFn) -> object:
        fields = draw(st.lists(children, min_size=1, max_size=4))
        definitions = {
            f"f{i}": (cast("Any", annotation), ...)
            for i, annotation in enumerate(fields)
        }
        return create_model(_name("R"), **definitions)  # type: ignore[call-overload]

    return build()


_TYPES: st.SearchStrategy[object] = st.recursive(
    st.sampled_from(_SCALARS) | _enums() | _literals(),
    lambda children: (
        children.map(lambda t: list[t])  # type: ignore[valid-type]
        | children.map(lambda t: dict[str, t])  # type: ignore[valid-type]
        | children.map(lambda t: t | None)
        | _records(children)
    ),
    max_leaves=8,
)


def _declare(record: type[BaseModel]) -> dict[str, Any]:
    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    namespace = {"send": action(send)}
    declared = types.new_class(
        _name("C"), (Protocol,), exec_body=lambda ns: ns.update(namespace)
    )
    contract(f"{_name('generated')}.v1")(declared)
    description = Contract.of(declared).describe()
    return {"$defs": description["$defs"], "$ref": f"#/$defs/{record.__name__}"}


def _round_trip(record: type[BaseModel], schema: dict[str, Any], value: object) -> None:
    instance = record.model_validate_json(json.dumps(value))
    sent = instance.model_dump_json()
    Draft202012Validator(schema).validate(json.loads(sent))
    assert record.model_validate_json(sent) == instance


@pytest.mark.spec("CON-16")
@_SETTINGS
@given(st.data())
def test_generated_contracts_round_trip_through_their_description(
    data: st.DataObject,
) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES)))
    schema = _declare(record)
    value = data.draw(from_schema(schema, custom_formats=_FORMATS))
    _round_trip(record, schema, value)


class RefColour(enum.StrEnum):
    RED = "red"
    GREEN = "green"


class RefPart(BaseModel):
    sku: Annotated[str, Field(min_length=1)]
    quantity: Annotated[int, Field(ge=0)] = 1


class Reference(BaseModel):
    """Every portable form, with defaults as authors write them."""

    flag: bool = False
    count: int = 0
    ratio: float = 1.0
    name: str = ""
    raw: bytes = b""
    price: Decimal = Decimal("1.50")
    when: AwareDatetime
    day: date
    span: timedelta = timedelta(seconds=90)
    parts: list[RefPart] = []
    totals: dict[str, int] = {}
    colour: RefColour = RefColour.RED
    size: Literal["s", "m"] = "s"
    note: str | None = None


@pytest.mark.spec("CON-16")
@_SETTINGS
@given(st.data())
def test_reference_contract_round_trips_through_its_description(
    data: st.DataObject,
) -> None:
    schema = _declare(Reference)
    value = data.draw(from_schema(schema, custom_formats=_FORMATS))
    _round_trip(Reference, schema, value)


class Tree(BaseModel):
    name: str
    children: list["Tree"] = []


@pytest.mark.spec("CON-16")
def test_recursive_record_round_trips_through_its_description() -> None:
    # hypothesis-jsonschema cannot generate from recursive references, so this
    # one value is written by hand.
    schema = _declare(Tree)
    value = {"name": "a", "children": [{"name": "b", "children": [{"name": "c"}]}]}
    _round_trip(Tree, schema, value)
    assert schema["$defs"]["Tree"]["properties"]["children"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/Tree"},
        "default": [],
    }
