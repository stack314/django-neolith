"""The wire encodings of spec 01 (ADR 0019), in the description and the types.

Each portable form carries a pattern or a range in the published description,
so a JSON Schema validator in any language checks it without format
extensions (CON-34). Pydantic writes a duration, a decimal and bytes in other
forms, so those are neolith's own types, Duration, Decimal128 and Binary,
which read and write exactly their forms; the plain types are refused. A
default is part of the description, so one that would travel outside its form
is refused when the contract is declared. Sending and receiving are checked by
the transports (#10).
"""

import json
import re
import types
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Optional, Protocol, TypeAliasType, cast

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    Field,
    GetCoreSchemaHandler,
    PlainSerializer,
    PlainValidator,
    ValidationError,
    WrapValidator,
    create_model,
)
from pydantic_core import CoreSchema, PydanticSerializationError, core_schema

from neolith import (
    Binary,
    Contract,
    ContractDefinitionError,
    Decimal128,
    Duration,
    action,
    contract,
)
from neolith._forms import (
    BYTES_PATTERN,
    DECIMAL_DIGITS_PATTERN,
    DECIMAL_PATTERN,
    DURATION_PATTERN,
)
from neolith._portable import DATE_PATTERN, TIMESTAMP_PATTERN
from tests.strategies import admitted_by

SPEC = Path(__file__).resolve().parent.parent / "spec" / "01-contracts.md"
SAFE = 2**53 - 1
DOUBLE_MAX = 1.7976931348623157e308
INTEGER = {"type": "integer", "minimum": -SAFE, "maximum": SAFE}
NUMBER = {"type": "number", "minimum": -DOUBLE_MAX, "maximum": DOUBLE_MAX}
BYTES = {"type": "string", "contentEncoding": "base64url", "pattern": BYTES_PATTERN}
DECIMAL = {
    "type": "string",
    "pattern": DECIMAL_PATTERN,
    "allOf": [{"pattern": DECIMAL_DIGITS_PATTERN}],
}
DURATION = {"type": "string", "pattern": DURATION_PATTERN}
TIMESTAMP = {"type": "string", "format": "date-time", "pattern": TIMESTAMP_PATTERN}
DATE = {"type": "string", "format": "date", "pattern": DATE_PATTERN}
_FORMATS = FormatChecker(formats=("date", "date-time"))
_HYPOTHESIS = settings(
    derandomize=True,
    database=None,
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# The TypeAliasType spelling is what the "type" statement builds; both are
# tested, as authors write either.
Span = TypeAliasType("Span", Duration)  # noqa: UP040
Money = TypeAliasType("Money", Decimal128)  # noqa: UP040
Blob = TypeAliasType("Blob", Binary)  # noqa: UP040
type SpanAlias = Duration
type MoneyAlias = Decimal128
type BlobAlias = Binary


class Carrier(BaseModel):
    """The request of a contract whose record under test is a detail record."""


def _declare(record: type[BaseModel], role: str = "request") -> dict[str, Any]:
    """Declare a contract around ``record``; return its description."""

    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    def fail(self: object, request: Carrier) -> None: ...

    marked = action(send) if role == "request" else action(errors={"e": record})(fail)
    declared = types.new_class(
        "Wire", (Protocol,), exec_body=lambda ns: ns.update({"send": marked})
    )
    contract("wire.v1")(declared)
    return Contract.of(declared).describe()


def _record(annotation: object, default: object = ...) -> type[BaseModel]:
    return create_model("Holder", v=(cast("Any", annotation), default))


def _field(annotation: object, default: object = ...) -> dict[str, Any]:
    """The description of field ``v`` of a record."""
    described = _declare(_record(annotation, default))
    return cast("dict[str, Any]", described["$defs"]["Holder"]["properties"]["v"])


def _is_valid(schema: dict[str, Any], value: object) -> bool:
    return Draft202012Validator(schema, format_checker=_FORMATS).is_valid(value)


def _written(annotation: object, value: object) -> object:
    """What a record writes for ``value`` in its field ``v``."""
    return json.loads(_record(annotation)(v=value).model_dump_json())["v"]


def _read(annotation: object, text: object) -> object:
    """What a record reads from JSON ``{"v": text}``."""
    read: Any = _record(annotation).model_validate_json(json.dumps({"v": text}))
    return read.v


# The spec and the code state the same patterns.


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    "pattern",
    [
        BYTES_PATTERN,
        DECIMAL_PATTERN,
        DECIMAL_DIGITS_PATTERN,
        DURATION_PATTERN,
        TIMESTAMP_PATTERN,
        DATE_PATTERN,
    ],
)
def test_spec_states_each_pattern_the_code_uses(pattern: str) -> None:
    assert f"`{pattern}`" in SPEC.read_text(encoding="utf-8")


# What the description states for each form.


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "described"),
    [
        pytest.param(int, INTEGER, id="integer"),
        pytest.param(float, NUMBER, id="number"),
        pytest.param(Binary, BYTES, id="bytes"),
        pytest.param(Decimal128, DECIMAL, id="decimal"),
        pytest.param(Duration, DURATION, id="duration"),
        pytest.param(AwareDatetime, TIMESTAMP, id="timestamp"),
        pytest.param(date, DATE, id="date"),
        pytest.param(list[Binary], {"type": "array", "items": BYTES}, id="list"),
        pytest.param(
            dict[str, Decimal128],
            {"type": "object", "additionalProperties": DECIMAL},
            id="map",
        ),
        pytest.param(
            Duration | None, {"anyOf": [DURATION, {"type": "null"}]}, id="optional"
        ),
        pytest.param(
            Annotated[Duration, BeforeValidator(lambda v: v)],
            DURATION,
            id="behind a before validator",
        ),
    ],
)
def test_description_states_each_forms_encoding(
    annotation: object, described: dict[str, Any]
) -> None:
    assert _field(annotation) == described


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize("annotation", [Binary, Decimal128, Duration])
def test_pydantics_own_schema_of_a_neolith_form_states_its_pattern(
    annotation: object,
) -> None:
    # model_json_schema, and anything built on it, agrees with the wire.
    record = _record(annotation)
    expected = _field(annotation)
    for mode in ("validation", "serialization"):
        own = record.model_json_schema(mode=mode)["properties"]["v"]
        assert {key: value for key, value in own.items() if key != "title"} == expected


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "described"),
    [
        pytest.param(
            Annotated[int, Field(ge=-(2**60), le=5)],
            {"type": "integer", "minimum": -SAFE, "maximum": 5},
            id="int: the range below, the field above",
        ),
        pytest.param(
            Annotated[int, Field(gt=0, lt=2**60)],
            {**INTEGER, "exclusiveMinimum": 0, "exclusiveMaximum": 2**60},
            id="int: exclusive bounds kept",
        ),
        pytest.param(
            Annotated[float, Field(ge=0.5)],
            {**NUMBER, "minimum": 0.5},
            id="float: the field's minimum",
        ),
    ],
)
def test_description_keeps_the_narrower_bound(
    annotation: object, described: dict[str, Any]
) -> None:
    assert _field(annotation) == described


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("described", "admitted", "refused"),
    [
        pytest.param(INTEGER, [SAFE, -SAFE, 0], [SAFE + 1, -SAFE - 1], id="integer"),
        pytest.param(
            NUMBER,
            [DOUBLE_MAX, -DOUBLE_MAX, 0.0, 1.5],
            [json.loads("1e400"), json.loads("-1e400")],
            id="number",
        ),
        pytest.param(
            BYTES,
            ["", "_w==", "__4=", "__79", "-_8=", "AAECAwQFBgcICQ=="],
            ["_w", "/w==", "_x==", "__5=", " _w==", "AA==AA=="],
            id="bytes",
        ),
        pytest.param(
            DECIMAL,
            ["0", "0.00", "1.50", "-12.5", "100", "0.0000001", "-" + "9" * 34],
            ["-0", "-0.00", "1E+2", "1e2", "01", ".5", "1.", "+1", "9" * 35, "NaN"],
            id="decimal",
        ),
        pytest.param(
            DURATION,
            ["0s", "-2s", "1.500s", "0.000001s", "-0.100001s", "99999999999.999999s"],
            [
                "-0s",
                "1.5s",
                "1.000s",
                "1.500000s",
                "0.0000001s",
                "100000000000s",
                "PT1.5S",
                "01s",
                "1",
            ],
            id="duration",
        ),
        pytest.param(
            TIMESTAMP,
            [
                "2026-01-01T00:00:00Z",
                "0001-01-01T00:00:00.000001+01:00",
                "9999-12-31T23:59:59.999999-23:59",
            ],
            [
                "2026-01-01t00:00:00z",
                "2026-01-01 00:00:00Z",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00.1234567Z",
                "2026-01-01T00:00:60Z",
                "2026-01-01T00:00:00+01:00:30",
                "0000-01-01T00:00:00Z",
                "2027-02-29T00:00:00Z",
            ],
            id="timestamp",
        ),
        pytest.param(
            DATE,
            ["0001-01-01", "2028-02-29", "9999-12-31"],
            ["2027-02-29", "0000-01-01", "2026-1-1", "20260101"],
            id="date",
        ),
    ],
)
def test_description_admits_each_form_and_refuses_the_rest(
    described: dict[str, Any], admitted: list[object], refused: list[object]
) -> None:
    # A JSON Schema validator in another language reads the description;
    # python-jsonschema stands in for one, with its format checks on.
    for value in admitted:
        assert _is_valid(described, value), value
    for value in refused:
        assert not _is_valid(described, value), value


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("pattern", "value"),
    [
        (BYTES_PATTERN, "_w=="),
        (DECIMAL_PATTERN, "1.5"),
        (DECIMAL_DIGITS_PATTERN, "1.5"),
        (DURATION_PATTERN, "1.500s"),
        (TIMESTAMP_PATTERN, "2026-01-01T00:00:00Z"),
        (DATE_PATTERN, "2026-01-01"),
    ],
)
def test_patterns_matched_over_the_whole_string_refuse_a_final_newline(
    pattern: str, value: str
) -> None:
    # "$" matches before a final newline in Python, so a validator that
    # matches with re.search, as python-jsonschema does, admits one; spec 01
    # matches over the whole string, as neolith's types do.
    assert re.fullmatch(pattern, value)
    assert not re.fullmatch(pattern, value + "\n")
    assert re.search(pattern, value + "\n")


# The plain types are refused, naming neolith's.


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    ("plain", "named"),
    [(bytes, "Binary"), (Decimal, "Decimal128"), (timedelta, "Duration")],
)
@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(lambda t: t, id="field"),
        pytest.param(lambda t: list[t], id="list"),  # type: ignore[valid-type]
        pytest.param(lambda t: t | None, id="optional"),
        pytest.param(lambda t: dict[str, t], id="map"),  # type: ignore[valid-type]
    ],
)
@pytest.mark.parametrize("role", ["request", "detail"])
def test_plain_type_is_refused_naming_neoliths(
    plain: type, named: str, wrap: Callable[[type], object], role: str
) -> None:
    where = "wire.v1, action 'send'" + (", error 'e'" if role == "detail" else "")
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(where)
        + rf": field Holder\.v\S* is a plain {plain.__name__}, which Pydantic does "
        rf"not write in its wire form; declare it as neolith\.{named}$",
    ):
        _declare(_record(wrap(plain)), role)


@pytest.mark.spec("CON-13", "CON-17")
def test_plain_duration_in_a_reply_is_refused() -> None:
    reply = create_model("Waited", span=(timedelta, ...))

    def wait(self: object, request: Carrier) -> reply: ...  # type: ignore[valid-type]

    declared = types.new_class(
        "Waits", (Protocol,), exec_body=lambda ns: ns.update({"wait": action(wait)})
    )
    with pytest.raises(
        ContractDefinitionError,
        match=r"^waits\.v1, action 'wait': field Waited\.span is a plain timedelta",
    ):
        contract("waits.v1")(declared)


def _look_alike(value: object) -> object:
    return value


_LOOK_ALIKE = Annotated[
    timedelta,
    PlainValidator(_look_alike),
    PlainSerializer(lambda value: "1s", when_used="json"),
]


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        pytest.param(_LOOK_ALIKE, "has type function-plain", id="own plain validator"),
    ],
)
def test_only_neolith_forms_own_schema_is_admitted(
    annotation: object, message: str
) -> None:
    with pytest.raises(ContractDefinitionError, match=re.escape(message)):
        _field(annotation)


def _same(value: object) -> object:
    return value


_ADDED: dict[str, Callable[[object], object]] = {
    "constraint": lambda t: Annotated[t, Field(min_length=1)],
    "bound": lambda t: Annotated[t, Field(ge=0)],
    "pattern": lambda t: Annotated[t, Field(pattern="^a")],
    "after validator": lambda t: Annotated[t, AfterValidator(_same)],
    "plain validator": lambda t: Annotated[t, PlainValidator(_same)],
    "wrap validator": lambda t: Annotated[t, WrapValidator(lambda v, h: h(v))],
    "serializer": lambda t: Annotated[t, PlainSerializer(str, when_used="json")],
    "serializer behind a before validator": lambda t: Annotated[
        t, BeforeValidator(_same), PlainSerializer(str, when_used="json")
    ],
}


_NAMED_FORMS = {
    "Duration": (Duration, "Duration"),
    "Decimal128": (Decimal128, "Decimal128"),
    "Binary": (Binary, "Binary"),
    "type Duration": (SpanAlias, "Duration"),
    "TypeAliasType Binary": (Blob, "Binary"),
}


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    ("form", "name", "added"),
    [
        pytest.param(form, name, added, id=f"{shown}-{added}")
        for shown, (form, name) in _NAMED_FORMS.items()
        for added in _ADDED
        # Pydantic has no ge on bytes.
        if not (added == "bound" and name == "Binary")
    ],
)
def test_anything_added_to_a_neolith_form_is_refused_naming_it(
    form: object, name: str, added: str
) -> None:
    # Pydantic applies a constraint in an after validator around the form, so
    # the message names the type, not a validator the author never wrote.
    annotation = _ADDED[added](form)
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(
            f"adds a constraint, a validator or a serializer to neolith.{name}; "
            "only a before validator is admitted there"
        ),
    ):
        _field(list[annotation])  # type: ignore[valid-type]


# A Field(...) default puts its constraints before the type's marker, which
# Pydantic applies to the plain type and hands to the marker; so does a Field
# placed before the marker in Annotated.
_SET_BEFORE: dict[str, tuple[object, Callable[[object, object], object]]] = {
    "constraint": (Field(min_length=1), lambda t, m: t),
    "bound": (Field(ge=0), lambda t, m: t),
    "strict": (Field(strict=True), lambda t, m: t),
    "Field before the marker": (
        ...,
        lambda t, m: Annotated[t.__origin__, Field(strict=True), m],  # type: ignore[attr-defined]
    ),
}


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    ("form", "name", "added"),
    [
        pytest.param(form, name, added, id=f"{name}-{added}")
        for name, form in (
            ("Duration", Duration),
            ("Decimal128", Decimal128),
            ("Binary", Binary),
        )
        for added in _SET_BEFORE
        # Pydantic has no ge on bytes.
        if not (added == "bound" and name == "Binary")
    ],
)
def test_a_constraint_set_before_a_neolith_form_is_refused(
    form: object, name: str, added: str
) -> None:
    default, place = _SET_BEFORE[added]
    marker = form.__metadata__[0]  # type: ignore[attr-defined]
    with pytest.raises(
        TypeError,
        match=rf"^neolith\.{name} admits no constraint, setting or validator placed",
    ):
        _record(place(form, marker), default)


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize("form", [Duration, Decimal128, Binary])
def test_a_validator_before_a_neolith_forms_marker_is_refused(form: object) -> None:
    marker = form.__metadata__[0]  # type: ignore[attr-defined]
    plain: Any = form.__origin__  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="here: a function-before node"):
        _record(Annotated[plain, BeforeValidator(_same), marker])


@pytest.mark.spec("CON-13", "CON-17")
@pytest.mark.parametrize(
    "alias",
    [SpanAlias, Blob, Binary | None],
    ids=["type", "TypeAliasType", "Optional"],
)
def test_a_constraint_default_on_an_alias_of_a_neolith_form_is_refused(
    alias: object,
) -> None:
    # Behind an alias or Optional, Pydantic applies it after the form instead.
    with pytest.raises(ContractDefinitionError, match="adds a constraint"):
        _field(alias, Field(min_length=1))


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    "default",
    [
        Field(description="held as bytes"),
        Field(json_schema_extra={"example": "_w=="}),
        Field(default=b""),
        Field(title="Raw"),
    ],
    ids=["description", "json_schema_extra", "default value", "title"],
)
def test_a_field_default_without_constraints_on_a_neolith_form_is_admitted(
    default: object,
) -> None:
    assert _record(Binary, default).model_json_schema()["properties"]
    assert _declare(_record(Binary, default))["$defs"]["Holder"]["properties"]


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("form", "described"),
    [
        pytest.param(Duration, DURATION, id="Duration"),
        pytest.param(Decimal128, DECIMAL, id="Decimal128"),
        pytest.param(Binary, BYTES, id="Binary"),
    ],
)
def test_a_before_validator_on_a_neolith_form_is_admitted(
    form: object, described: dict[str, Any]
) -> None:
    # The nearest legitimate shape: the form's own reader still checks the
    # value a before validator returns.
    assert _field(Annotated[form, BeforeValidator(_same)]) == described


@pytest.mark.spec("CON-13", "CON-17")
def test_a_copy_of_a_forms_schema_with_other_functions_is_refused() -> None:
    class Twin:
        @classmethod
        def __get_pydantic_core_schema__(
            cls, source: object, handler: GetCoreSchemaHandler
        ) -> CoreSchema:
            return core_schema.no_info_plain_validator_function(
                _look_alike,
                serialization=core_schema.plain_serializer_function_ser_schema(
                    str, when_used="json"
                ),
            )

    with pytest.raises(ContractDefinitionError, match="has type function-plain"):
        _field(Annotated[timedelta, Twin])


def test_a_forms_marker_on_another_type_is_refused() -> None:
    marker = Duration.__metadata__[0]  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match=r"neolith\.Duration annotates timedelta"):
        _record(Annotated[int, marker])


# Each type reads only its form from JSON, and writes it exactly.


@pytest.mark.spec("CON-37")
@pytest.mark.parametrize(
    ("annotation", "value", "written"),
    [
        pytest.param(Duration, timedelta(0), "0s", id="zero duration"),
        pytest.param(Duration, timedelta(seconds=-2), "-2s", id="whole seconds"),
        pytest.param(Duration, timedelta(seconds=1.5), "1.500s", id="milliseconds"),
        pytest.param(
            Duration, timedelta(microseconds=-100001), "-0.100001s", id="micros"
        ),
        pytest.param(Decimal128, Decimal("1.50"), "1.50", id="trailing zero kept"),
        pytest.param(Decimal128, Decimal("1E+2"), "100", id="positive exponent"),
        pytest.param(Decimal128, Decimal("-0.00"), "0.00", id="negative zero"),
        pytest.param(Decimal128, Decimal("0E+5"), "0", id="zero, positive exponent"),
        pytest.param(Decimal128, Decimal("1E-7"), "0.0000001", id="small"),
        pytest.param(Decimal128, Decimal("0E-7"), "0.0000000", id="small zero"),
        pytest.param(Decimal128, Decimal("1E+33"), "1" + "0" * 33, id="34 digits"),
        pytest.param(Binary, b"\xff", "_w==", id="bytes"),
        pytest.param(Binary, b"\xfb\xff", "-_8=", id="url alphabet"),
    ],
)
def test_each_form_is_written_in_its_one_spelling(
    annotation: object, value: object, written: str
) -> None:
    assert _written(annotation, value) == written
    assert _read(annotation, written) == value


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "text"),
    [
        *((Duration, t) for t in ["PT1.5S", "1.5s", "1.000s", "-0s", "1", "1e3s"]),
        *((Decimal128, t) for t in ["1E+2", "-0", "01", "1.", " 1", "1.5\n", "NaN"]),
        (Decimal128, "9" * 35),
        *((Binary, t) for t in ["_w", "/w==", "_x==", "_w==\n", " _w==", "ok"]),
        (Duration, 1.5),
        (Decimal128, 1.5),
        (Decimal128, 1),
        (Binary, None),
    ],
)
def test_each_form_reads_only_its_form_from_json(
    annotation: object, text: object
) -> None:
    with pytest.raises(ValidationError):
        _read(annotation, text)


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (Duration, timedelta(seconds=10**11)),
        (Duration, -timedelta(seconds=10**11)),
        (Duration, 1.5),
        (Decimal128, Decimal("NaN")),
        (Decimal128, Decimal("-Infinity")),
        (Decimal128, Decimal("9" * 35)),
        (Decimal128, Decimal("1E+34")),
        (Decimal128, Decimal("1E-34")),
        (Decimal128, Decimal("1E+999999999")),
        (Decimal128, Decimal("1E-999999999")),
        (Decimal128, Decimal("0E-999999999")),
        (Decimal128, Decimal("-0E-999999999")),
        (Decimal128, 3),
        (Decimal128, 1.5),
        (Binary, bytearray(b"x")),
        (Binary, "not base64"),
    ],
    ids=repr,
)
def test_each_form_refuses_a_python_value_outside_it(
    annotation: object, value: object
) -> None:
    with pytest.raises(ValidationError):
        _record(annotation)(v=value)


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "text", "value"),
    [
        (Duration, "1.500s", timedelta(seconds=1.5)),
        (Decimal128, "-12.50", Decimal("-12.50")),
    ],
)
def test_each_form_takes_its_text_from_python(
    annotation: object, text: str, value: object
) -> None:
    record: Any = _record(annotation)(v=text)
    held = record.v
    assert held == value
    assert type(held) is type(value)


# Part of CON-38; a sender's refusal is in tests/test_send_side_check.py.
@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (Duration, timedelta(seconds=10**11)),
        (Decimal128, Decimal("NaN")),
        (Decimal128, Decimal("9" * 35)),
        (Binary, "_w=="),
    ],
    ids=repr,
)
def test_a_value_set_after_validation_outside_its_form_is_not_written(
    annotation: object, value: object
) -> None:
    record = _record(annotation).model_construct(v=value)
    with pytest.raises(PydanticSerializationError):
        record.model_dump_json()


@pytest.mark.spec("CON-34", "CON-37")
@_HYPOTHESIS
@given(
    st.timedeltas(
        min_value=timedelta(seconds=1 - 10**11), max_value=timedelta(seconds=10**11 - 1)
    )
)
def test_durations_round_trip_in_their_form(value: timedelta) -> None:
    written = _written(Duration, value)
    assert _is_valid(DURATION, written)
    assert _read(Duration, written) == value


@pytest.mark.spec("CON-34", "CON-37")
@_HYPOTHESIS
@given(st.decimals(allow_nan=False, allow_infinity=False))
def test_decimals_round_trip_in_their_form(value: Decimal) -> None:
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    fixed = (
        len(digits) + max(exponent, 0)
        if exponent >= 0
        else max(len(digits), -exponent + 1)
    )
    assume(fixed <= 34)
    written = _written(Decimal128, value)
    assert _is_valid(DECIMAL, written)
    assert _read(Decimal128, written) == value


@pytest.mark.spec("CON-34")
@_HYPOTHESIS
@given(st.binary(max_size=64))
def test_bytes_round_trip_in_their_form(value: bytes) -> None:
    written = _written(Binary, value)
    assert _is_valid(BYTES, written)
    assert _read(Binary, written) == value


@pytest.mark.spec("CON-16", "CON-34")
@_HYPOTHESIS
@given(
    st.datetimes(
        min_value=datetime(1, 1, 2),
        max_value=datetime(9999, 12, 30),
        timezones=st.just(UTC),
    ),
    st.integers(-(24 * 60 - 1), 24 * 60 - 1),
)
def test_timestamps_travel_in_the_form_the_description_states(
    instant: datetime, offset_minutes: int
) -> None:
    # Offsets in whole minutes travel unchanged; the sender converts the rest
    # to UTC (CON-36), tested in tests/test_send_side_check.py.
    value = instant.astimezone(timezone(timedelta(minutes=offset_minutes)))
    record = create_model("Stamped", at=(AwareDatetime, ...), day=(date, ...))
    sent = json.loads(record(at=value, day=value.date()).model_dump_json())
    assert _is_valid(TIMESTAMP, sent["at"])
    assert _is_valid(DATE, sent["day"])
    assert record.model_validate_json(json.dumps(sent)) == record(
        at=value, day=value.date()
    )


@pytest.mark.spec("CON-16", "CON-34")
@_HYPOTHESIS
@given(st.floats(allow_nan=False, allow_infinity=False))
def test_finite_numbers_travel_in_the_range_the_description_states(
    number: float,
) -> None:
    # -0.0 is written as 0 by the sender (CON-35), tested in
    # tests/test_send_side_check.py.
    assume(not (number == 0 and str(number).startswith("-")))
    sent = _written(float, number)
    assert _is_valid(NUMBER, sent)
    assert sent == number


@pytest.mark.spec("CON-16", "CON-34")
@pytest.mark.parametrize(
    ("annotation", "described"),
    [
        pytest.param(int, INTEGER, id="integer"),
        pytest.param(float, NUMBER, id="number"),
        pytest.param(Binary, BYTES, id="bytes"),
        pytest.param(Decimal128, DECIMAL, id="decimal"),
        pytest.param(Duration, DURATION, id="duration"),
        pytest.param(AwareDatetime, TIMESTAMP, id="timestamp"),
        pytest.param(date, DATE, id="date"),
    ],
)
@settings(_HYPOTHESIS, max_examples=100)
@given(data=st.data())
def test_each_form_round_trips_from_its_description(
    annotation: object, described: dict[str, Any], data: st.DataObject
) -> None:
    value = data.draw(admitted_by(described))
    # python-jsonschema and hypothesis read "$" as Python does, so a final
    # newline can be drawn; spec 01 refuses it (see above).
    assume(not (isinstance(value, str) and value.endswith("\n")))
    read = _read(annotation, value)
    sent = _written(annotation, read)
    assert _is_valid(described, sent)
    assert _read(annotation, sent) == read
    if annotation in (Binary, Decimal128, Duration):
        # One spelling per value: what was read is written back unchanged.
        assert sent == value


# Defaults travel as written, so they must already be in their form.


@pytest.mark.spec("CON-34", "CON-37")
@pytest.mark.parametrize(
    ("annotation", "default", "described"),
    [
        pytest.param(Decimal128, Decimal("1.50"), "1.50", id="decimal"),
        pytest.param(Decimal128, Decimal("1E+2"), "100", id="decimal, exponent"),
        pytest.param(Decimal128, Decimal("1E-7"), "0.0000001", id="small decimal"),
        pytest.param(Duration, timedelta(seconds=90), "90s", id="duration"),
        pytest.param(Binary, b"\xff", "_w==", id="bytes"),
    ],
)
def test_default_of_a_neolith_form_is_described_in_its_form(
    annotation: object, default: object, described: str
) -> None:
    assert _field(annotation, default)["default"] == described


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(Decimal128, "2", id="decimal as text"),
        pytest.param(Decimal128, 2, id="decimal as int"),
        pytest.param(Duration, "90s", id="duration as text"),
        pytest.param(Duration, timedelta(seconds=10**11), id="duration too long"),
        pytest.param(Binary, "_w==", id="bytes as text"),
    ],
)
def test_default_of_a_neolith_form_not_of_its_type_is_refused(
    annotation: object, default: object
) -> None:
    with pytest.raises(
        ContractDefinitionError, match=r"field Holder\.v has the default"
    ):
        _field(annotation, default)


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(int, 2**53, id="above"),
        pytest.param(int, -(2**53), id="below"),
        pytest.param(list[int], [1, 2**63], id="in a list"),
        pytest.param(dict[str, int], {"k": -(2**53)}, id="in a map"),
        pytest.param(int, 10**5000, id="huge"),
    ],
)
def test_integer_default_outside_the_safe_range_is_refused(
    annotation: object, default: object
) -> None:
    with pytest.raises(ContractDefinitionError, match=f"outside ±{SAFE}"):
        _field(annotation, default)


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize("default", [SAFE, -SAFE])
def test_integer_default_at_the_safe_bounds_is_accepted(default: int) -> None:
    assert _field(int, default)["default"] == default


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(float, -0.0, id="number"),
        pytest.param(list[float], [1.0, -0.0], id="in a list"),
        pytest.param(dict[str, float], {"k": -0.0}, id="in a map"),
        pytest.param(float | None, -0.0, id="optional"),
    ],
)
def test_negative_zero_default_is_refused(annotation: object, default: object) -> None:
    with pytest.raises(ContractDefinitionError, match=r"-0\.0 travels as 0"):
        _field(annotation, default)


@pytest.mark.spec("CON-34")
def test_zero_default_is_accepted() -> None:
    assert _field(float, 0.0)["default"] == 0.0


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    "default",
    [
        pytest.param(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))), id="0000"),
        pytest.param(
            datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-1))),
            id="10000",
        ),
    ],
)
def test_timestamp_default_outside_the_utc_range_is_refused(default: datetime) -> None:
    with pytest.raises(
        ContractDefinitionError, match="is outside years 0001 to 9999 in UTC"
    ):
        _field(AwareDatetime, default)


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    "default",
    [
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    ],
    ids=["first instant", "first instant at +01:00", "last instant"],
)
def test_timestamp_default_at_the_utc_range_edges_is_accepted(
    default: datetime,
) -> None:
    assert _is_valid(TIMESTAMP, _field(AwareDatetime, default)["default"])


@pytest.mark.spec("CON-34")
def test_a_neolith_form_as_a_map_key_is_refused() -> None:
    with pytest.raises(ContractDefinitionError, match="keys are not strings"):
        _field(dict[Duration, int])


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize("text", ["_w==", "abcd", "data", ""])
def test_binary_refuses_a_python_str(text: str) -> None:
    # A plain bytes field reads a str as its UTF-8 bytes, so reading the same
    # call's str as base64url would change what it holds; it is refused.
    with pytest.raises(ValidationError) as raised:
        _record(Binary)(v=text)
    # The message names both ways to get here: text meant as data, and
    # base64url from a JSON body parsed before validation.
    assert raised.value.errors()[0]["msg"] == (
        "Value error, binary data from Python is bytes, such as text.encode() "
        "for text; to read base64url from JSON, validate the JSON text itself "
        "(model_validate_json), not a value parsed from it"
    )
    assert _read(Binary, "_w==") == b"\xff"


@pytest.mark.spec("CON-34")
def test_binary_is_read_from_json_text_only() -> None:
    # ADR 0020: a receiver validates the body's text (validate_json). Text
    # parsed into Python values first holds base64url as a str, refused above.
    record = _record(Binary)
    body = '{"v": "_w=="}'
    sent = record(v=b"\xff")
    assert record.model_validate_json(body) == record(v=b"\xff")
    assert record.model_validate_json(sent.model_dump_json()) == sent
    assert record.model_validate(sent.model_dump()) == sent
    with pytest.raises(ValidationError, match="validate the JSON text itself"):
        record.model_validate(json.loads(body))
    with pytest.raises(ValidationError, match="validate the JSON text itself"):
        record.model_validate(sent.model_dump(mode="json"))
    # Strings mode is not JSON text either: a plain bytes field holds "_w==" as
    # its UTF-8 bytes there.
    plain = _record(bytes).model_validate_strings({"v": "_w=="})
    assert plain.model_dump() == {"v": b"_w=="}
    with pytest.raises(ValidationError, match="validate the JSON text itself"):
        record.model_validate_strings({"v": "_w=="})


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize("value", [Decimal("0E-100000000"), Decimal("-0E-999999999")])
def test_a_zero_decimal_with_a_huge_exponent_is_refused_without_writing_it(
    value: Decimal,
) -> None:
    # Its size is checked before it is written out, as for any other decimal.
    with pytest.raises(ValidationError):
        _record(Decimal128)(v=value)
    with pytest.raises(PydanticSerializationError):
        _record(Decimal128).model_construct(v=value).model_dump_json()


def _optional(described: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [described, {"type": "null"}]}


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("alias", "described"),
    [
        pytest.param(SpanAlias, DURATION, id="type Duration"),
        pytest.param(MoneyAlias, DECIMAL, id="type Decimal128"),
        pytest.param(BlobAlias, BYTES, id="type Binary"),
        pytest.param(Span, DURATION, id="TypeAliasType Duration"),
        pytest.param(Money, DECIMAL, id="TypeAliasType Decimal128"),
        pytest.param(Blob, BYTES, id="TypeAliasType Binary"),
    ],
)
@pytest.mark.parametrize(
    ("wrap", "shape"),
    [
        pytest.param(lambda t: t, lambda d: d, id="field"),
        pytest.param(lambda t: Optional[t], _optional, id="Optional"),  # noqa: UP045
        pytest.param(
            lambda t: list[t],  # type: ignore[valid-type]
            lambda d: {"type": "array", "items": d},
            id="list",
        ),
        pytest.param(
            lambda t: dict[str, t],  # type: ignore[valid-type]
            lambda d: {"type": "object", "additionalProperties": d},
            id="dict",
        ),
    ],
)
def test_an_alias_of_a_neolith_form_is_admitted(
    alias: object,
    described: dict[str, Any],
    wrap: Callable[[object], object],
    shape: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    # Pydantic adds a reference to an alias's node, which any node may carry.
    assert _field(wrap(alias)) == shape(described)
