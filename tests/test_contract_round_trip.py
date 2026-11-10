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
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    TypeAdapter,
    create_model,
)

from neolith import Contract, ContractDefinitionError, action, contract
from neolith._portable import (
    portable_constructs,
    portable_scalars,
    portable_settings,
)

_HYPOTHESIS = settings(
    derandomize=True,
    database=None,
    max_examples=80,
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


# The generator draws from the same allowlist the checker admits
# (src/neolith/_portable.py): every constraint key, every validation-only key
# and every record setting, in combination, with defaults written in and out
# of form. test_generator_covers_the_allowlist fails if the checker admits a
# key this table does not draw.
_BOUNDS = {
    "gt": st.integers(-5, 0),
    "ge": st.integers(-5, 0),
    "lt": st.integers(6, 20),
    "le": st.integers(6, 20),
    "multiple_of": st.sampled_from([1, 2, 3]),
}
_KEYS: dict[str, dict[str, st.SearchStrategy[object]]] = {
    "bool": {},
    "int": dict(_BOUNDS),
    "float": {**_BOUNDS, "allow_inf_nan": st.booleans()},
    "str": {
        "min_length": st.integers(0, 2),
        "max_length": st.integers(3, 8),
        "pattern": st.sampled_from([r"^[a-z]*$", r"^[A-Za-z ]*$"]),
        "strip_whitespace": st.booleans(),
        "to_lower": st.booleans(),
        "to_upper": st.booleans(),
    },
    "bytes": {},
    "decimal": {},
    "date": {},
    # Set by Pydantic on every timestamp and duration, not by authors.
    "timedelta": {"microseconds_precision": st.just("truncate")},
    "datetime": {
        "tz_constraint": st.just("aware"),
        "microseconds_precision": st.just("truncate"),
    },
}
_TYPES_BY_KEY: dict[str, object] = {
    "bool": bool,
    "int": int,
    "float": float,
    "str": str,
    "bytes": bytes,
    "decimal": Decimal,
    "date": date,
    "timedelta": timedelta,
    "datetime": AwareDatetime,
}
_STRING_KEYS = {
    "strip_whitespace",
    "to_lower",
    "to_upper",
    "pattern",
    "min_length",
    "max_length",
}
_SETTING_VALUES: dict[str, st.SearchStrategy[object]] = {
    "title": st.just("Titled"),
    "revalidate_instances": st.sampled_from(["never", "always", "subclass-instances"]),
    "validate_default": st.booleans(),
    "from_attributes": st.booleans(),
    "loc_by_alias": st.booleans(),
    "hide_input_in_errors": st.booleans(),
    "validation_error_cause": st.booleans(),
    "str_strip_whitespace": st.booleans(),
    "str_to_lower": st.booleans(),
    "str_to_upper": st.booleans(),
}
# Defaults authors write, in form and out of it. The checker must refuse a
# default that does not read back as itself, so every pick is fair game.
_DEFAULTS: dict[str, list[object]] = {
    "bool": [False, True, 0],
    "int": [0, 3, True, 3.0, None, "3"],
    "float": [0.0, 1, 1.5, True],
    "str": ["", "abc", "ABC", " a ", "\u00df"],
    "bytes": [b"", b"ok", b"\xff"],
    "decimal": [Decimal("1.50"), 0, "2"],
    "date": [date(2026, 1, 1), "2026-01-01"],
    "timedelta": [timedelta(seconds=90), 1.5],
    "datetime": [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1)],
}


@st.composite
def _scalars(draw: st.DrawFn) -> tuple[object, list[object]]:
    """A scalar annotation with a random set of its admitted keys, and defaults."""
    kind = draw(st.sampled_from(sorted(_KEYS)))
    chosen = (
        draw(st.sets(st.sampled_from(sorted(_KEYS[kind])))) if _KEYS[kind] else set()
    )
    values = {key: draw(_KEYS[kind][key]) for key in chosen}
    values = {
        k: v
        for k, v in values.items()
        if k not in ("tz_constraint", "microseconds_precision")
    }
    markers: list[object] = []
    # Strict mode is refused, so it is drawn rarely. Hypothesis favours small
    # integers, so the rare value is one from the middle of the range.
    if draw(st.integers(0, 19)) == 13:
        markers.append(Strict())
    if kind == "str":
        markers.append(StringConstraints(**cast("Any", values)))
    elif values:
        markers.append(Field(**cast("Any", values)))
    annotation = _TYPES_BY_KEY[kind]
    if markers:
        annotation = Annotated[(annotation, *markers)]
    return annotation, _DEFAULTS[kind]


@st.composite
def _enums(draw: st.DrawFn) -> tuple[object, list[object]]:
    values = draw(
        st.lists(
            st.text("abcdef", min_size=1, max_size=4),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    made = cast("Any", enum.StrEnum)(
        _name("E"), {f"M{i}": v for i, v in enumerate(values)}
    )
    return made, [next(iter(made)), values[0]]


@st.composite
def _literals(draw: st.DrawFn) -> tuple[object, list[object]]:
    values = draw(
        st.lists(
            st.text("xyz", min_size=1, max_size=3), min_size=1, max_size=3, unique=True
        )
    )
    return Literal[tuple(values)], [values[0], "nope"]


def _records(
    children: st.SearchStrategy[tuple[object, list[object]]],
) -> st.SearchStrategy[tuple[object, list[object]]]:
    @st.composite
    def build(draw: st.DrawFn) -> tuple[object, list[object]]:
        fields = draw(st.lists(children, min_size=1, max_size=4))
        definitions: dict[str, Any] = {}
        for i, (drawn, defaults) in enumerate(fields):
            annotation = drawn
            # An after validator is refused wherever it appears, so it is drawn
            # rarely, to leave most contracts for the round trip.
            wrappers = draw(st.sets(st.sampled_from(["frozen", "function-before"])))
            if draw(st.integers(0, 39)) == 27:
                wrappers = {*wrappers, "function-after"}
            for wrapper in sorted(wrappers):
                annotation = _WRAPPERS[wrapper](annotation)
            use_default: bool = draw(st.booleans())
            default: object = ...
            if defaults and use_default:
                default = draw(st.sampled_from(defaults))
            definitions[f"f{i}"] = (cast("Any", annotation), default)
        chosen = draw(st.sets(st.sampled_from(sorted(_SETTING_VALUES))))
        config: dict[str, object] = {k: draw(_SETTING_VALUES[k]) for k in chosen}
        if draw(st.integers(0, 19)) == 13:
            config["strict"] = True  # refused; drawn rarely
        config["extra"] = "ignore"
        record = create_model(
            _name("R"), __config__=cast("ConfigDict", config), **definitions
        )
        return record, []

    return build()


_LENGTHS = {"min_length": st.integers(0, 1), "max_length": st.integers(2, 4)}
# Map keys: plain, with lengths, or converted (which the checker refuses).
_KEY_TYPES: list[object] = [
    str,
    Annotated[str, StringConstraints(min_length=1, max_length=6)],
    Annotated[str, StringConstraints(to_lower=True)],
]


def _with_lengths(annotation: object, lengths: Mapping[str, object]) -> object:
    return (
        Annotated[annotation, Field(**cast("Any", lengths))] if lengths else annotation
    )


def _lists(
    children: st.SearchStrategy[tuple[object, list[object]]],
) -> st.SearchStrategy[tuple[object, list[object]]]:
    @st.composite
    def build(draw: st.DrawFn) -> tuple[object, list[object]]:
        item = draw(children)
        lengths = draw(st.fixed_dictionaries({}, optional=_LENGTHS))
        return _with_lengths(list.__class_getitem__(item[0]), lengths), [[]]

    return build()


def _maps(
    children: st.SearchStrategy[tuple[object, list[object]]],
) -> st.SearchStrategy[tuple[object, list[object]]]:
    @st.composite
    def build(draw: st.DrawFn) -> tuple[object, list[object]]:
        item = draw(children)
        key = draw(st.sampled_from(_KEY_TYPES))
        lengths = draw(st.fixed_dictionaries({}, optional=_LENGTHS))
        return _with_lengths(dict.__class_getitem__((key, item[0])), lengths), [{}]

    return build()


# Field wrappers: frozen, and validators of each kind; after ones are refused.
_WRAPPERS: dict[str, Callable[[object], object]] = {
    "frozen": lambda t: Annotated[t, Field(frozen=True)],
    "function-before": lambda t: Annotated[t, BeforeValidator(_same)],
    "function-after": lambda t: Annotated[t, AfterValidator(_same)],
}


def _same(value: object) -> object:
    return value


def _optional(item: tuple[object, list[object]]) -> tuple[object, list[object]]:
    return cast("Any", item[0]) | None, [None, *item[1]]


_TYPES: st.SearchStrategy[tuple[object, list[object]]] = st.recursive(
    _scalars() | _enums() | _literals(),
    lambda children: (
        _lists(children)
        | _maps(children)
        | children.map(_optional)
        | _records(children)
    ),
    max_leaves=8,
)


def test_generator_covers_the_allowlist() -> None:
    """Every key and setting the checker admits is one the generator draws."""
    for kind, (_, bounds, other) in portable_scalars().items():
        assert set(bounds) | set(other) <= set(_KEYS[kind]), kind
    assert set(portable_settings()) <= set(_SETTING_VALUES)
    assert set(_KEYS) == set(portable_scalars())
    drawn = {
        "list": set(_LENGTHS),
        "dict": set(_LENGTHS),
        "model-field": {"frozen"},
        "validators": set(_WRAPPERS) - {"frozen"},
    }
    for construct, keys in portable_constructs().items():
        assert set(keys) <= drawn[construct], construct


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


def _round_trip_with_defaults(
    record: type[BaseModel], schema: dict[str, Any], value: object
) -> None:
    """The value, and the value with every defaulted field left to its default."""
    _round_trip(record, schema, value)
    assert isinstance(value, dict)
    required = schema["$defs"][record.__name__].get("required", [])
    _round_trip(record, schema, {k: v for k, v in value.items() if k in required})


@pytest.mark.spec("CON-16")
@_HYPOTHESIS
@given(st.data())
def test_generated_contracts_round_trip_through_their_description(
    data: st.DataObject,
) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    try:
        schema = _declare(record)
    except ContractDefinitionError:
        # Refused: the property is about contracts that are accepted.
        return
    value = data.draw(from_schema(schema, custom_formats=_FORMATS))
    _round_trip_with_defaults(record, schema, value)


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
@_HYPOTHESIS
@given(st.data())
def test_reference_contract_round_trips_through_its_description(
    data: st.DataObject,
) -> None:
    schema = _declare(Reference)
    value = data.draw(from_schema(schema, custom_formats=_FORMATS))
    _round_trip_with_defaults(Reference, schema, value)


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
