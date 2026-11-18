"""Contract part: every portable form crosses alike (CNF-1, spec 01).

The cases call ``reference.v1``'s ``echo`` from the reference apps of the
test host, over the in-process and the HTTP transport. A value comes back as
it went, by its portable form and its exact types at every depth (a
decimal's trailing zeros, an enum member, a timestamp's instant), whichever
transport carried it, and a value a form does not admit is refused at the
caller alike over both. Each cites CNF-1, and HTTP-1: the HTTP transport,
with the in-process one, is what it runs over.
"""

import json
import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

import neolith
from tests.conformance.contract.conftest import HYPOTHESIS, OverBoth, form
from tests.host_apps.reference import document
from tests.host_apps.reference.contract import Colour, Nested, Values
from tests.strategies import admitted_by

CALLER = "reference_caller"

_DOCUMENT: dict[str, Any] = json.loads(document.PATH.read_text(encoding="utf-8"))
# The description of Values, with the document's components for its references.
_VALUES = {
    "$ref": "#/components/schemas/reference.v1.Values",
    "components": _DOCUMENT["components"],
}

PARITY = pytest.mark.spec("CNF-1", "HTTP-1")


def _base(**given: object) -> Values:
    """The fewest form, with the given fields in place of its own."""
    fields: dict[str, object] = {
        "boolean": False,
        "integer": 0,
        "number": 0.0,
        "string": "",
        "bytes": b"",
        "decimal": Decimal("0"),
        "timestamp": datetime(2027, 1, 1, tzinfo=UTC),
        "date": date(2027, 1, 1),
        "duration": timedelta(0),
        "list": [],
        "map": {},
        "record": Nested(label=""),
        "enum": Colour.RED,
    }
    return Values.model_construct(**cast("dict[str, Any]", {**fields, **given}))


def _echo(over_both: OverBoth, sent: Values) -> list[object]:
    """Echo ``sent`` over both transports; each leg's reply."""
    replies: list[object] = []

    def call() -> object:
        with neolith.command_scope(CALLER):
            replies.append(neolith.connect("reference.v1").echo(sent))
        return replies[-1]

    over_both(call)
    return replies


# Each admitted form at its edges (spec 01), with the nearest value either side
# in the next test.
_EDGES: dict[str, Values] = {
    "integer maximum": _base(integer=2**53 - 1, list=[2**53 - 1], map={"k": 2**53 - 1}),
    "integer minimum": _base(integer=-(2**53 - 1), list=[-(2**53 - 1)]),
    "number maximum": _base(number=1.7976931348623157e308),
    "number minimum": _base(number=-1.7976931348623157e308),
    "number smallest": _base(number=5e-324),
    "negative zero": _base(number=-0.0, decimal=Decimal("-0.00")),
    "number needing 17 digits": _base(number=0.1 + 0.2),
    "decimal scale kept": _base(
        decimal=Decimal("1.50"), optional_decimal=Decimal("0.00")
    ),
    "decimal of 34 digits": _base(decimal=Decimal("-" + "9" * 34)),
    "decimal of 34 digits and a point": _base(decimal=Decimal("0." + "1" * 33)),
    "decimal with trailing zeros": _base(decimal=Decimal("100.000")),
    "bytes of every value": _base(bytes=bytes(range(256))),
    "bytes needing padding": _base(bytes=b"\xfb\xff", optional_bytes=b"a"),
    "string of controls and astral": _base(
        string='\x00\x1f"\\/' + chr(0x2028) + "\U0001f600"
    ),
    "string of U+FFFF": _base(string="￿", map={"￿": 1, "": 2}),
    "timestamp at year 1": _base(timestamp=datetime(1, 1, 1, 0, 0, tzinfo=UTC)),
    "timestamp at year 9999": _base(
        timestamp=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    ),
    "timestamp with offsets": _base(
        timestamp=datetime(
            2027, 6, 1, 12, tzinfo=timezone(timedelta(hours=-23, minutes=-59))
        ),
        optional_timestamp=datetime(
            2027, 6, 1, 12, tzinfo=timezone(timedelta(hours=14))
        ),
    ),
    "date at the ends": _base(date=date(1, 1, 1), optional_date=date(9999, 12, 31)),
    "duration negative": _base(duration=timedelta(seconds=-1, microseconds=-1)),
    "duration at its bound": _base(
        duration=timedelta(seconds=99999999999, microseconds=999999)
    ),
    "duration of a microsecond": _base(duration=timedelta(microseconds=1)),
    "every enum member": _base(enum=Colour.GREEN, optional_enum=Colour.RED),
    "optional forms empty, not absent": _base(
        optional_boolean=False,
        optional_integer=0,
        optional_number=0.0,
        optional_string="",
        optional_bytes=b"",
        optional_list=[],
        optional_map={},
        optional_record=Nested(label=""),
    ),
    "nested containers": _base(
        list=list(range(500)), map={str(i): i for i in range(200)}
    ),
}


@PARITY
@pytest.mark.spec("TRN-1", "TRN-16", "HTTP-41", "CON-34", "CON-35", "CON-37")
@pytest.mark.conformance_case("value_at_its_edges")
@pytest.mark.parametrize("edge", list(_EDGES))
def test_a_value_at_the_edge_of_a_form_is_echoed_alike(
    over_both: OverBoth, edge: str
) -> None:
    sent = _EDGES[edge]
    replies = _echo(over_both, sent)
    # Each transport's own reply is the value sent, by its form and types.
    assert [form(reply) for reply in replies] == [form(sent)] * 2
    for reply in replies:
        assert isinstance(reply, Values)
        # A zero is written without its sign: -0 as 0 (CON-35), -0.00 as 0.00
        # (CON-37). form() calls them equal, so the sign is read here.
        if sent.number == 0:
            assert math.copysign(1.0, reply.number) == 1.0
        if sent.decimal.is_zero():
            assert not reply.decimal.is_signed()
            assert reply.decimal.as_tuple().exponent == sent.decimal.as_tuple().exponent


@PARITY
@pytest.mark.spec("TRN-1", "TRN-16", "HTTP-41", "CON-16", "CON-34")
@pytest.mark.conformance_case("value_the_description_admits")
@HYPOTHESIS
@given(st.data())
def test_every_value_the_description_admits_is_echoed_alike(
    over_both: OverBoth, data: st.DataObject
) -> None:
    drawn = data.draw(admitted_by(_VALUES))
    sent = Values.model_validate_json(json.dumps(drawn))
    replies = _echo(over_both, sent)
    assert [form(reply) for reply in replies] == [form(sent)] * 2


# Values a form does not admit, which the caller refuses before it sends.
_REFUSED: dict[str, Values] = {
    "integer above its range": _base(integer=2**53),
    "integer below its range": _base(integer=-(2**53)),
    "integer in a list above its range": _base(list=[2**53]),
    "number not finite": _base(number=math.nan),
    "number infinite": _base(number=math.inf),
    "decimal of 35 digits": _base(decimal=Decimal("1" * 35)),
    "decimal not finite": _base(decimal=Decimal("NaN")),
    "timestamp with no offset": _base(timestamp=datetime(2027, 1, 1)),
    "string holding a lone surrogate": _base(string="a\ud800"),
    "map key holding a lone surrogate": _base(map={"\udfff": 1}),
    "a boolean for an integer": _base(integer=True),
    "text for an integer": _base(integer="1"),
    "an enum member's text": _base(enum="red"),
    "a record of another kind": _base(record=Values.model_construct()),
}


@PARITY
@pytest.mark.spec("TRN-14", "TRN-15")
@pytest.mark.conformance_case("value_refused_at_the_caller")
@pytest.mark.parametrize("refused", list(_REFUSED))
def test_a_value_no_form_admits_is_refused_alike_at_the_caller(
    over_both: OverBoth, refused: str
) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            return neolith.connect("reference.v1").echo(_REFUSED[refused])

    outcome = over_both(call)
    assert outcome.raised
    assert not isinstance(outcome.error, neolith.ContractError)
