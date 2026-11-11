"""The wire encodings of spec 01 (ADR 0019), as the description states them.

Each portable form carries a pattern or a range in the published description,
so a JSON Schema validator in any language checks it without format
extensions (CON-34). Defaults are part of the description, so a default that
Pydantic would write outside its form is refused when the contract is
declared. Sending and receiving are checked by the transports (#10).
"""

import json
import re
import types
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, create_model

from neolith import Contract, ContractDefinitionError, action, contract
from neolith._portable import (
    BYTES_PATTERN,
    DATE_PATTERN,
    DECIMAL_DIGITS_PATTERN,
    DECIMAL_PATTERN,
    DECIMAL_SMALL_PATTERN,
    TIMESTAMP_PATTERN,
)

SPEC = Path(__file__).resolve().parent.parent / "spec" / "01-contracts.md"
BASE64 = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
SAFE = 2**53 - 1
DOUBLE_MAX = 1.7976931348623157e308
INTEGER = {"type": "integer", "minimum": -SAFE, "maximum": SAFE}
NUMBER = {"type": "number", "minimum": -DOUBLE_MAX, "maximum": DOUBLE_MAX}
BYTES = {"type": "string", "contentEncoding": "base64url", "pattern": BYTES_PATTERN}
DECIMAL = {
    "type": "string",
    "pattern": DECIMAL_PATTERN,
    "allOf": [{"pattern": DECIMAL_DIGITS_PATTERN}, {"pattern": DECIMAL_SMALL_PATTERN}],
}
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


def _field(
    annotation: object, default: object = ..., **config: object
) -> dict[str, Any]:
    """The description of field ``v`` of a record built with ``config``."""
    record = create_model(
        "Holder",
        __config__=cast("ConfigDict", config or None),
        v=(cast("Any", annotation), default),
    )
    return cast(
        "dict[str, Any]", _declare(record)["$defs"]["Holder"]["properties"]["v"]
    )


class Carrier(BaseModel):
    """The request of a contract whose record under test is a detail record."""


def _is_valid(schema: dict[str, Any], value: object) -> bool:
    return Draft202012Validator(schema, format_checker=_FORMATS).is_valid(value)


# The spec and the code state the same patterns.


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    "pattern",
    [
        BYTES_PATTERN,
        DECIMAL_PATTERN,
        DECIMAL_DIGITS_PATTERN,
        DECIMAL_SMALL_PATTERN,
        TIMESTAMP_PATTERN,
        DATE_PATTERN,
    ],
)
def test_spec_states_each_pattern_the_description_uses(pattern: str) -> None:
    assert f"`{pattern}`" in SPEC.read_text(encoding="utf-8")


# What the description states for each form.


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    ("annotation", "described"),
    [
        pytest.param(int, INTEGER, id="integer"),
        pytest.param(float, NUMBER, id="number"),
        pytest.param(bytes, BYTES, id="bytes"),
        pytest.param(Decimal, DECIMAL, id="decimal"),
        pytest.param(AwareDatetime, TIMESTAMP, id="timestamp"),
        pytest.param(date, DATE, id="date"),
        pytest.param(list[bytes], {"type": "array", "items": BYTES}, id="list"),
        pytest.param(
            dict[str, Decimal],
            {"type": "object", "additionalProperties": DECIMAL},
            id="map",
        ),
        pytest.param(
            AwareDatetime | None,
            {"anyOf": [TIMESTAMP, {"type": "null"}]},
            id="optional",
        ),
    ],
)
def test_description_states_each_forms_encoding(
    annotation: object, described: dict[str, Any]
) -> None:
    assert _field(annotation, **BASE64) == described


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
            ["0", "0.00", "1.50", "-12.5", "100", "0.000001", "-" + "9" * 34],
            [
                "-0",
                "-0.00",
                "1E+2",
                "1e2",
                "01",
                ".5",
                "1.",
                "+1",
                "0.0000001",
                "0.0000000",
                "9" * 35,
                "NaN",
            ],
            id="decimal",
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
        (DECIMAL_SMALL_PATTERN, "1.5"),
        (TIMESTAMP_PATTERN, "2026-01-01T00:00:00Z"),
        (DATE_PATTERN, "2026-01-01"),
    ],
)
def test_patterns_matched_over_the_whole_string_refuse_a_final_newline(
    pattern: str, value: str
) -> None:
    # "$" matches before a final newline in Python, so a validator that
    # matches with re.search, as python-jsonschema does, admits one; spec 01
    # matches over the whole string.
    assert re.fullmatch(pattern, value)
    assert not re.fullmatch(pattern, value + "\n")
    assert re.search(pattern, value + "\n")


# Bytes travel as base64url only under the record's own settings.


@pytest.mark.spec("CON-16", "CON-34")
@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="no setting"),
        pytest.param({"ser_json_bytes": "base64"}, id="writing only"),
        pytest.param({"val_json_bytes": "base64"}, id="reading only"),
    ],
)
def test_bytes_without_both_base64_settings_are_refused(config: ConfigDict) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(
            "wire.v1, action 'send': field Holder.v is bytes, which travel as "
            "base64url; set ser_json_bytes='base64' and val_json_bytes='base64' "
            "in the model_config of record Holder"
        ),
    ):
        _field(bytes, **config)


@pytest.mark.spec("CON-16", "CON-34")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ser_json_bytes", "utf8"),
        ("ser_json_bytes", "hex"),
        ("val_json_bytes", "utf8"),
        ("val_json_bytes", "hex"),
    ],
)
def test_other_bytes_settings_are_refused_even_without_bytes(
    key: str, value: str
) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(f"record Holder sets {key}={value!r}; bytes travel as"),
    ):
        _field(int, **{**BASE64, key: value})


@pytest.mark.spec("CON-16", "CON-34")
def test_a_records_bytes_follow_its_own_settings_not_its_containers() -> None:
    # pydantic-core builds each record's fields with that record's settings, so
    # an inner record without them writes its bytes as UTF-8 text.
    inner = create_model("Inner", raw=(bytes, ...))
    outer = create_model("Outer", __config__=BASE64, inner=(inner, ...))
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape("field Outer.inner.raw is bytes, which travel as base64url"),
    ):
        _declare(outer)
    assert outer(inner=inner(raw=b"ok")).model_dump_json() == '{"inner":{"raw":"ok"}}'


@pytest.mark.spec("CON-34")
def test_bytes_settings_without_bytes_are_accepted() -> None:
    assert _field(int, **BASE64) == INTEGER


@pytest.mark.spec("CON-34")
def test_bytes_as_map_keys_are_refused() -> None:
    with pytest.raises(ContractDefinitionError, match="keys are not strings"):
        _field(dict[bytes, int], **BASE64)


@pytest.mark.spec("CON-16", "CON-34")
@_HYPOTHESIS
@given(st.binary(max_size=64))
def test_bytes_travel_in_the_form_the_description_states(raw: bytes) -> None:
    record = create_model("Blob", __config__=BASE64, raw=(bytes, ...))
    sent = json.loads(record(raw=raw).model_dump_json())["raw"]
    assert _is_valid(BYTES, sent)
    assert record.model_validate_json(json.dumps({"raw": sent})) == record(raw=raw)


# Decimals are admitted, without constraints, in their form.


@pytest.mark.spec("CON-34")
@pytest.mark.parametrize(
    "field",
    [
        pytest.param(Field(max_digits=5), id="max_digits"),
        pytest.param(Field(decimal_places=2), id="decimal_places"),
        pytest.param(Field(ge=Decimal(0)), id="ge"),
        pytest.param(Field(multiple_of=cast("Any", Decimal("0.5"))), id="multiple_of"),
        pytest.param(Field(allow_inf_nan=True), id="allow_inf_nan"),
    ],
)
def test_decimal_constraints_are_refused(field: object) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=r"field Holder\.v is a decimal with a constraint \(\w+\), which the "
        "description cannot state",
    ):
        _field(Annotated[Decimal, cast("Any", field)])


@pytest.mark.spec("CON-34", "CON-42")
@pytest.mark.parametrize(
    ("default", "written"),
    [
        pytest.param(Decimal("1.50"), "1.50", id="trailing zero kept"),
        pytest.param(Decimal("100"), "100", id="integer"),
        pytest.param(Decimal("0.000001"), "0.000001", id="smallest admitted"),
        pytest.param(Decimal("0.000000"), "0.000000", id="zero at scale 6"),
        pytest.param(Decimal("-" + "9" * 34), "-" + "9" * 34, id="34 digits"),
    ],
)
def test_decimal_default_in_its_form_is_described(
    default: Decimal, written: str
) -> None:
    assert _field(Decimal, default)["default"] == written


@pytest.mark.spec("CON-34", "CON-42")
@pytest.mark.parametrize(
    "default",
    [
        pytest.param(Decimal("1E+2"), id="positive exponent"),
        pytest.param(Decimal("-0"), id="negative zero"),
        pytest.param(Decimal("-0.00"), id="negative zero at a scale"),
        pytest.param(Decimal("1E-7"), id="below 10**-6"),
        pytest.param(Decimal("0E-7"), id="zero beyond scale 6"),
        pytest.param(Decimal("9" * 35), id="35 digits"),
    ],
)
def test_decimal_default_outside_its_form_is_refused(default: Decimal) -> None:
    with pytest.raises(
        ContractDefinitionError,
        match=r"field Holder\.v has the default .*, which Pydantic writes outside "
        "its wire encoding",
    ):
        _field(Decimal, default)


@pytest.mark.spec("CON-34", "CON-42")
@pytest.mark.parametrize("role", ["request", "detail"])
def test_decimals_are_admitted_in_requests_replies_and_details(role: str) -> None:
    record = create_model("Money", amount=(Decimal, ...), parts=(list[Decimal], []))
    assert _declare(record, role)["$defs"]["Money"]["properties"]["amount"] == DECIMAL


# Durations wait for a type that writes their form (CON-41).


@pytest.mark.spec("CON-41")
@pytest.mark.parametrize(
    "annotation",
    [timedelta, list[timedelta], timedelta | None, dict[str, timedelta]],
    ids=repr,
)
@pytest.mark.parametrize("role", ["request", "detail"])
def test_duration_is_refused_until_its_type_is_settled(
    annotation: object, role: str
) -> None:
    where = "wire.v1, action 'send'" + (", error 'e'" if role == "detail" else "")
    with pytest.raises(
        ContractDefinitionError,
        match=re.escape(where) + r": field Holder\.v\S* is a duration, which this "
        "version does not admit yet",
    ):
        _declare(create_model("Holder", v=(cast("Any", annotation), ...)), role)


@pytest.mark.spec("CON-41")
def test_duration_in_a_reply_is_refused() -> None:
    reply = create_model("Waited", span=(timedelta, ...))

    def wait(self: object, request: Carrier) -> reply: ...  # type: ignore[valid-type]

    declared = types.new_class(
        "Waits", (Protocol,), exec_body=lambda ns: ns.update({"wait": action(wait)})
    )
    with pytest.raises(
        ContractDefinitionError,
        match=r"^waits\.v1, action 'wait': field Waited\.span is a duration",
    ):
        contract("waits.v1")(declared)


# Defaults travel as written, so they must already be in their form.


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
    # to UTC (CON-35), with the send-side check of #10.
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
    # -0.0 is written as 0 by the sender (CON-35), with the send-side check.
    assume(not (number == 0 and str(number).startswith("-")))
    record = create_model("Measured", x=(float, ...))
    sent = json.loads(record(x=number).model_dump_json())["x"]
    assert _is_valid(NUMBER, sent)
    assert sent == number


@pytest.mark.spec("CON-16", "CON-34", "CON-42")
@pytest.mark.parametrize(
    ("annotation", "described"),
    [
        pytest.param(int, INTEGER, id="integer"),
        pytest.param(float, NUMBER, id="number"),
        pytest.param(bytes, BYTES, id="bytes"),
        pytest.param(Decimal, DECIMAL, id="decimal"),
        pytest.param(AwareDatetime, TIMESTAMP, id="timestamp"),
        pytest.param(date, DATE, id="date"),
    ],
)
@settings(_HYPOTHESIS, max_examples=100)
@given(data=st.data())
def test_each_form_round_trips_from_its_description(
    annotation: object, described: dict[str, Any], data: st.DataObject
) -> None:
    value = data.draw(from_schema(described))
    # python-jsonschema and hypothesis read "$" as Python does, so a final
    # newline can be drawn; spec 01 refuses it (see above).
    assume(not (isinstance(value, str) and value.endswith("\n")))
    record = create_model("One", __config__=BASE64, v=(cast("Any", annotation), ...))
    read = record.model_validate_json(json.dumps({"v": value}))
    sent = json.loads(read.model_dump_json())["v"]
    assert _is_valid(described, sent)
    assert record.model_validate_json(json.dumps({"v": sent})) == read
    if annotation is Decimal:
        # A decimal keeps its digits, trailing zeros too.
        assert sent == value
