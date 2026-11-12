"""The send-side check: what a sender converts or refuses before a value
crosses (spec 02, TRN-10, TRN-11, TRN-15; spec 01, CON-35, CON-36, CON-38,
CON-41).

A sender reads a value once, into a copy of its own, converts the copy as
spec 01 says, writes it, checks the text against the contract's description
with a validator compiled from it (ADR 0007 Decision 2), reads the text back
with the declared type, and compares what it read with the copy. These tests
reach a value through every route that can change it after validation, and
hold the whole check to a JSON Schema validator.
"""

import enum
import json
import math
import os
import re
import subprocess
import sys
import traceback
import types
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, cast

import pytest
from hypothesis import assume, event, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

from neolith import (
    Binary,
    Contract,
    ContractDefinitionError,
    Decimal128,
    Duration,
    action,
    contract,
)
from neolith._checker import description_checker
from neolith._crossing import (
    _EXACT,
    _converted,
    _difference,
    _Fields,
    check_request,
    codec,
)
from tests.strategies import admitted_by
from tests.test_contract_round_trip import (
    _FORMAT_CHECKER,
    _HYPOTHESIS,
    _TYPES,
    Reference,
    RefPart,
    _name,
    _records,
)


class Level(enum.IntEnum):
    ONE = 1


class Colour(enum.StrEnum):
    RED = "red"
    GREEN = "green"


class OtherColour(enum.Enum):
    RED = "red"


class Leaves(BaseModel):
    """A field of every scalar form, and fields with bounds of their own."""

    flag: bool = False
    count: int = 0
    ratio: float = 0.0
    name: str = ""
    raw: Binary = b""
    amount: Decimal128 = Decimal("0")
    span: Duration = timedelta(0)
    when: AwareDatetime | None = None
    day: date | None = None
    colour: Colour = Colour.RED
    size: Literal["s", "m"] = "s"
    maybe: int | None = None
    small: int = Field(default=0, ge=0, le=5)
    label: str = Field(default="", max_length=3)
    few: list[int] = Field(default_factory=list, max_length=2)
    named: dict[str, float] = Field(default_factory=dict, max_length=2)


class Holder(BaseModel):
    """Holds a ``Leaves`` in each place a record can sit."""

    one: Leaves = Field(default_factory=Leaves)
    many: list[Leaves] = Field(default_factory=list)
    keyed: dict[str, Leaves] = Field(default_factory=dict)
    maybe: Leaves | None = None


@contract("sending.v1")
class Sending(Protocol):
    @action(errors={"refused": Leaves})
    def leaves(self, request: Leaves) -> Leaves: ...

    @action
    def holder(self, request: Holder) -> Holder: ...


SENDING = Contract.of(Sending)


class Loose(Leaves):
    """The same fields, each taking anything: a record a provider builds
    itself, which Pydantic keeps in a ``Leaves`` field without checking it."""

    flag: Any = False
    count: Any = 0
    ratio: Any = 0.0
    name: Any = ""
    raw: Any = b""
    amount: Any = Decimal("0")
    span: Any = timedelta(0)
    when: Any = None
    day: Any = None
    colour: Any = Colour.RED
    size: Any = "s"
    maybe: Any = None
    small: Any = 0
    label: Any = ""
    few: Any = Field(default_factory=list)
    named: Any = Field(default_factory=dict)


class LooksLikeLeaves:
    """Has every field of ``Leaves``, and nothing else of it."""

    def __init__(self, **fields: object) -> None:
        vars(self).update({**vars(Leaves()), **fields})


def _built(field: str, value: object) -> Leaves:
    """A ``Leaves`` built without validation, holding ``value`` in ``field``."""
    fields: dict[str, Any] = {field: value}
    return Leaves.model_construct(**fields)


# Every route by which a value can come to hold something after validation,
# or without it. Each takes a field of ``Leaves`` and what to put there, and
# returns the value to send and how to find that ``Leaves`` in what is read.
Placed = tuple[BaseModel, Callable[[Any], Leaves]]
Route = Callable[[str, object], Placed]


def _itself(read: Leaves) -> Leaves:
    return read


def _assigned(field: str, value: object) -> Placed:
    leaves = Leaves()
    setattr(leaves, field, value)
    return leaves, _itself


def _constructed(field: str, value: object) -> Placed:
    return _built(field, value), _itself


def _copied(field: str, value: object) -> Placed:
    return Leaves().model_copy(update={field: value}), _itself


def _written(field: str, value: object) -> Placed:
    leaves = Leaves()
    vars(leaves)[field] = value
    return leaves, _itself


def _nested(field: str, value: object) -> Placed:
    holder = Holder()
    setattr(holder.one, field, value)
    return holder, lambda read: read.one


def _appended(field: str, value: object) -> Placed:
    holder = Holder()
    holder.many.append(_built(field, value))
    return holder, lambda read: read.many[0]


def _replaced(field: str, value: object) -> Placed:
    holder = Holder(many=[Leaves(), Leaves()])
    holder.many[1] = _built(field, value)
    return holder, lambda read: read.many[1]


def _keyed(field: str, value: object) -> Placed:
    holder = Holder()
    holder.keyed["k"] = _built(field, value)
    return holder, lambda read: read.keyed["k"]


def _kept(field: str, value: object) -> Placed:
    # Validation keeps a record instance as it is given (revalidate_instances
    # defaults to "never"), so its fields are never checked.
    return Holder(one=_built(field, value)), lambda read: read.one


def _kept_in_a_list(field: str, value: object) -> Placed:
    return Holder(many=[_built(field, value)]), lambda read: read.many[0]


def _kept_in_a_map(field: str, value: object) -> Placed:
    return Holder(keyed={"k": _built(field, value)}), lambda read: read.keyed["k"]


def _kept_as_optional(field: str, value: object) -> Placed:
    return Holder(maybe=_built(field, value)), lambda read: read.maybe


def _looser(field: str, value: object) -> Placed:
    return Holder(one=Loose(**{field: value})), lambda read: read.one


def _look_alike(field: str, value: object) -> Placed:
    holder = Holder()
    holder.one = cast("Leaves", LooksLikeLeaves(**{field: value}))
    return holder, lambda read: read.one


_ROUTES: dict[str, Route] = {
    "assigned": _assigned,
    "built-without-validation": _constructed,
    "copied-with-an-update": _copied,
    "written-into-its-dict": _written,
    "assigned-in-a-nested-record": _nested,
    "appended-to-a-list": _appended,
    "replaced-in-a-list": _replaced,
    "set-in-a-map": _keyed,
    "record-kept-by-validation": _kept,
    "record-kept-in-a-list": _kept_in_a_list,
    "record-kept-in-a-map": _kept_in_a_map,
    "record-kept-as-an-optional": _kept_as_optional,
    "subclass-with-looser-fields": _looser,
    "nested-look-alike": _look_alike,
}


def _send(value: object) -> bytes:
    name = "leaves" if type(value) is Leaves else "holder"
    return check_request(SENDING, SENDING.actions[name], value)


def _read(value: BaseModel, text: bytes) -> Any:  # noqa: ANN401
    return codec(SENDING, type(value)).read(text)


class Whole(int):
    pass


class Ratio(float):
    pass


class Marked(str):
    __slots__ = ()


class OneName(str):
    """Two of these with the same text are two keys of one map."""

    __slots__ = ()

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


class Moment(datetime):
    """A timestamp of a subclass, as a clock a test freezes hands out."""


class Day(date):
    pass


class Items(list[int]):
    pass


class _ClocksGoBack(tzinfo):
    """A zone two hours ahead of UTC until 03:00 on 2025-10-26, and one hour
    ahead from then on, so each time from 02:00 to 03:00 that day comes twice
    (PEP 495), as in a zone with daylight saving time."""

    def utcoffset(self, moment: datetime | None) -> timedelta:
        assert moment is not None
        local = moment.replace(tzinfo=None)
        if datetime(2025, 10, 26, 2) <= local < datetime(2025, 10, 26, 3):
            return timedelta(hours=1 if moment.fold else 2)
        return timedelta(hours=2 if local < datetime(2025, 10, 26, 2) else 1)

    def dst(self, moment: datetime | None) -> None:
        return None

    def tzname(self, moment: datetime | None) -> str:
        return "local"


_LOCAL = _ClocksGoBack()
_NAN, _INF = float("nan"), float("inf")
_AMSTERDAM_1930 = timezone(timedelta(minutes=19, seconds=32))

# What each check refuses, by the words of its refusal.
_NOT_WRITTEN = "cannot be written as JSON by its type"
_NOT_FINITE = "and a number must be finite"
_NOT_DESCRIBED = "does not fit the contract's description"
_NOT_HELD = "which reads back as"

# TRN-11: something the declared type does not hold at that place, which the
# serializer refuses, or writes in the field's form so that only reading it
# back shows it. Each row: a field of Leaves, the value, and the refusal.
_KINDS = [
    pytest.param("count", True, _NOT_HELD, id="boolean-for-an-integer"),
    pytest.param("count", Level.ONE, _NOT_HELD, id="integer-enum-for-an-integer"),
    pytest.param("count", Whole(3), _NOT_HELD, id="integer-subclass"),
    pytest.param("count", "1", _NOT_WRITTEN, id="text-for-an-integer"),
    pytest.param("count", 1.0, _NOT_WRITTEN, id="number-for-an-integer"),
    pytest.param("maybe", True, _NOT_HELD, id="boolean-for-an-optional-integer"),
    pytest.param("ratio", True, _NOT_HELD, id="boolean-for-a-number"),
    pytest.param("ratio", 2**53 + 1, _NOT_HELD, id="integer-no-double-equals"),
    pytest.param("ratio", Ratio(1.5), _NOT_HELD, id="number-subclass"),
    pytest.param("ratio", Ratio(-0.0), _NOT_HELD, id="negative-zero-of-a-subclass"),
    pytest.param("ratio", Decimal("1.5"), _NOT_WRITTEN, id="decimal-for-a-number"),
    pytest.param("flag", 1, _NOT_WRITTEN, id="integer-for-a-boolean"),
    pytest.param("name", Marked("ab"), _NOT_HELD, id="text-subclass"),
    pytest.param("name", Colour.RED, _NOT_HELD, id="enum-member-for-text"),
    pytest.param("name", b"ab", _NOT_WRITTEN, id="bytes-for-text"),
    pytest.param("size", Marked("s"), _NOT_HELD, id="text-subclass-for-a-literal"),
    pytest.param("colour", "red", _NOT_WRITTEN, id="text-for-an-enum"),
    pytest.param("colour", OtherColour.RED, _NOT_WRITTEN, id="member-of-another-enum"),
    pytest.param(
        "day", datetime(2026, 1, 1, tzinfo=UTC), _NOT_WRITTEN, id="timestamp-for-a-date"
    ),
    pytest.param("day", "2026-01-01", _NOT_WRITTEN, id="text-for-a-date"),
    pytest.param("when", date(2026, 1, 1), _NOT_WRITTEN, id="date-for-a-timestamp"),
    pytest.param("raw", bytearray(b"ab"), _NOT_WRITTEN, id="bytearray-for-bytes"),
    pytest.param("raw", "YWI=", _NOT_WRITTEN, id="text-for-bytes"),
    pytest.param("amount", 5, _NOT_WRITTEN, id="integer-for-a-decimal"),
    pytest.param("amount", 1.5, _NOT_WRITTEN, id="number-for-a-decimal"),
    pytest.param("span", 1.5, _NOT_WRITTEN, id="number-for-a-duration"),
    pytest.param("few", (1, 2), _NOT_WRITTEN, id="tuple-for-a-list"),
    pytest.param("few", [True], _NOT_HELD, id="boolean-in-a-list"),
    pytest.param("named", {"a": True}, _NOT_HELD, id="boolean-in-a-map"),
    pytest.param(
        "named", {"a": 2**60 + 1}, _NOT_HELD, id="integer-no-double-equals-in-a-map"
    ),
    pytest.param("named", {1: 1.0}, _NOT_WRITTEN, id="integer-for-a-key"),
    pytest.param("named", {Marked("a"): 1.0}, _NOT_HELD, id="text-subclass-for-a-key"),
    pytest.param("named", {Colour.RED: 1.0}, _NOT_HELD, id="enum-member-for-a-key"),
]

# CON-38: a value outside its form's range, which no text can carry.
_FORMS = [
    pytest.param("ratio", _NAN, _NOT_FINITE, id="nan"),
    pytest.param("ratio", -_INF, _NOT_FINITE, id="infinity"),
    pytest.param("ratio", Ratio("inf"), _NOT_FINITE, id="infinity-of-a-subclass"),
    pytest.param("named", {"a": _INF}, _NOT_FINITE, id="infinity-in-a-map"),
    pytest.param(
        "span", timedelta(seconds=10**11), _NOT_WRITTEN, id="duration-past-its-range"
    ),
    pytest.param("amount", Decimal("1" * 35), _NOT_WRITTEN, id="decimal-of-35-digits"),
    pytest.param("amount", Decimal("NaN"), _NOT_WRITTEN, id="decimal-nan"),
    pytest.param("amount", Decimal("Infinity"), _NOT_WRITTEN, id="decimal-infinity"),
    pytest.param(
        "when",
        datetime(1, 1, 1, tzinfo=timezone(timedelta(seconds=1))),
        "a timestamp outside years 0001 to 9999 in UTC",
        id="offset-with-seconds-before-year-1-in-utc",
    ),
]

# CON-41: text outside I-JSON, which a sender never writes.
_BODIES = [
    pytest.param("name", "\ud800", _NOT_WRITTEN, id="lone-surrogate"),
    # pydantic-core writes a lone surrogate in a key as U+FFFD, three times,
    # and says nothing; only reading the text back shows the key changed.
    pytest.param("named", {"\udfff": 1.0}, _NOT_HELD, id="lone-surrogate-in-a-key"),
    pytest.param("ratio", 10**400, _NOT_WRITTEN, id="number-beyond-a-double"),
    pytest.param(
        "named",
        {OneName("a"): 1.0, OneName("a"): 2.0},
        _NOT_HELD,
        id="two-keys-that-write-one-member-name",
    ),
]

# TRN-15: text the description refuses, or that breaks a rule it cannot state.
_DESCRIBED = [
    pytest.param("count", 2**53, _NOT_DESCRIBED, id="integer-past-the-portable-range"),
    pytest.param("count", -(2**53), _NOT_DESCRIBED, id="integer-below-the-range"),
    pytest.param("few", [2**60], _NOT_DESCRIBED, id="integer-past-the-range-in-a-list"),
    pytest.param("small", 6, _NOT_DESCRIBED, id="past-a-maximum"),
    pytest.param("small", -1, _NOT_DESCRIBED, id="below-a-minimum"),
    pytest.param("label", "abcd", _NOT_DESCRIBED, id="text-past-its-length"),
    pytest.param("few", [1, 2, 3], _NOT_DESCRIBED, id="list-past-its-length"),
    pytest.param(
        "named", {"a": 1.0, "b": 2.0, "c": 3.0}, _NOT_DESCRIBED, id="map-past-its-size"
    ),
    pytest.param("size", "l", _NOT_DESCRIBED, id="text-outside-a-literal"),
    pytest.param("count", None, _NOT_DESCRIBED, id="nothing-for-an-integer"),
    pytest.param("name", None, _NOT_DESCRIBED, id="nothing-for-text"),
    pytest.param(
        "when", datetime(2026, 1, 1), _NOT_DESCRIBED, id="timestamp-without-an-offset"
    ),
    pytest.param(
        "when",
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        _NOT_DESCRIBED,
        id="timestamp-before-year-1-in-utc",
    ),
    pytest.param(
        "when",
        datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-1))),
        _NOT_DESCRIBED,
        id="timestamp-after-year-9999-in-utc",
    ),
]


def _refused(route: str, field: str, value: object, refusal: str) -> ValueError:
    sent, _ = _ROUTES[route](field, value)
    with pytest.raises(ValueError, match=re.escape(refusal)) as raised:
        _send(sent)
    # A mistake in the calling code, named by contract and action, and never
    # a contract error (CON-29).
    assert type(raised.value) is ValueError
    message = str(raised.value)
    assert message.startswith("sending.v1, action ")
    assert ": the request " in message
    return raised.value


@pytest.mark.spec("TRN-11", "TRN-13", "TRN-14", "CON-29")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(("field", "value", "refusal"), _KINDS)
def test_what_a_type_does_not_hold_is_refused_by_every_route(
    route: str, field: str, value: object, refusal: str
) -> None:
    error = _refused(route, field, value, refusal)
    assert (refusal == _NOT_HELD) == str(error).endswith(", so it is not sent")


@pytest.mark.spec("CON-38", "TRN-13", "TRN-14")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(("field", "value", "refusal"), _FORMS)
def test_a_value_outside_its_form_is_refused_by_every_route(
    route: str, field: str, value: object, refusal: str
) -> None:
    _refused(route, field, value, refusal)


@pytest.mark.spec("CON-41", "TRN-13", "TRN-14")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(("field", "value", "refusal"), _BODIES)
def test_text_outside_i_json_is_never_written_by_any_route(
    route: str, field: str, value: object, refusal: str
) -> None:
    _refused(route, field, value, refusal)


@pytest.mark.spec("TRN-15", "CON-38", "TRN-13", "TRN-14")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(("field", "value", "refusal"), _DESCRIBED)
def test_text_the_description_refuses_is_refused_by_every_route(
    route: str, field: str, value: object, refusal: str
) -> None:
    _refused(route, field, value, refusal)


# The nearest legitimate value to each refusal above: a field, what the
# sender holds there, and what the receiver reads.
_HELD = [
    pytest.param("count", 2**53 - 1, 2**53 - 1, id="largest-integer"),
    pytest.param("count", -(2**53 - 1), -(2**53 - 1), id="smallest-integer"),
    pytest.param("maybe", None, None, id="nothing-for-an-optional"),
    pytest.param("ratio", 3, 3.0, id="integer-a-double-equals"),
    pytest.param("ratio", 2**53, float(2**53), id="largest-such-integer-in-a-row"),
    pytest.param(
        "ratio",
        int(sys.float_info.max),
        sys.float_info.max,
        id="largest-integer-a-double-equals",
    ),
    pytest.param("ratio", sys.float_info.max, sys.float_info.max, id="largest-double"),
    pytest.param("ratio", 5e-324, 5e-324, id="smallest-double"),
    pytest.param("name", "\uffff", "\uffff", id="noncharacter"),
    pytest.param("colour", Colour.GREEN, Colour.GREEN, id="enum-member"),
    pytest.param("size", "m", "m", id="literal"),
    pytest.param("day", date(1, 1, 1), date(1, 1, 1), id="first-day"),
    pytest.param("day", Day(2026, 2, 28), date(2026, 2, 28), id="date-of-a-subclass"),
    pytest.param(
        "when",
        datetime(1, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        datetime(1, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        id="first-instant",
    ),
    pytest.param(
        "when",
        Moment(2026, 1, 1, 12, 0, 0, 5, tzinfo=timezone(timedelta(hours=-3))),
        datetime(2026, 1, 1, 12, 0, 0, 5, tzinfo=timezone(timedelta(hours=-3))),
        id="timestamp-of-a-subclass",
    ),
    pytest.param(
        "when",
        datetime(2025, 10, 26, 2, 30, tzinfo=_LOCAL),
        datetime(2025, 10, 26, 2, 30, tzinfo=timezone(timedelta(hours=2))),
        id="timestamp-the-first-time-its-hour-comes",
    ),
    pytest.param(
        "when",
        datetime(2025, 10, 26, 2, 30, fold=1, tzinfo=_LOCAL),
        datetime(2025, 10, 26, 2, 30, tzinfo=timezone(timedelta(hours=1))),
        id="timestamp-the-second-time-its-hour-comes",
    ),
    pytest.param(
        "when",
        datetime(2025, 10, 26, 3, 30, tzinfo=_LOCAL),
        datetime(2025, 10, 26, 3, 30, tzinfo=timezone(timedelta(hours=1))),
        id="timestamp-in-a-zone-with-two-offsets",
    ),
    pytest.param("raw", b"\xff\x00", b"\xff\x00", id="bytes"),
    pytest.param("amount", Decimal("1.50"), Decimal("1.50"), id="decimal"),
    pytest.param("amount", Decimal("1E+2"), Decimal("100"), id="decimal-exponent"),
    pytest.param("amount", Decimal("-0.00"), Decimal("0.00"), id="decimal-zero"),
    pytest.param(
        "amount", Decimal("9" * 34), Decimal("9" * 34), id="decimal-34-digits"
    ),
    pytest.param(
        "span",
        timedelta(seconds=10**11) - timedelta(microseconds=1),
        timedelta(seconds=10**11) - timedelta(microseconds=1),
        id="longest-duration",
    ),
    pytest.param("small", 5, 5, id="at-a-maximum"),
    pytest.param("small", 0, 0, id="at-a-minimum"),
    pytest.param("label", "abc", "abc", id="text-at-its-length"),
    pytest.param("few", [1, 2], [1, 2], id="list-at-its-length"),
    pytest.param("few", Items([1]), [1], id="list-of-a-subclass"),
    pytest.param(
        "named", {"a": 1.5, "b": 2}, {"a": 1.5, "b": 2.0}, id="map-at-its-size"
    ),
    pytest.param("named", OrderedDict(a=1.5), {"a": 1.5}, id="map-of-a-subclass"),
]


def _exactly(got: object, expected: object) -> bool:
    """Equal, and of the same types at every depth, offsets included."""
    if type(got) is not type(expected):
        return False
    if type(expected) is list:
        return len(cast("list[object]", got)) == len(expected) and all(
            _exactly(a, b)
            for a, b in zip(cast("list[object]", got), expected, strict=True)
        )
    if type(expected) is dict:
        mine = cast("dict[object, object]", got)
        return mine.keys() == expected.keys() and all(
            _exactly(mine[key], part) for key, part in expected.items()
        )
    if type(expected) is datetime:
        other = cast("datetime", got)
        return other == expected and other.utcoffset() == expected.utcoffset()
    if type(expected) is Decimal:
        return cast("Decimal", got).as_tuple() == expected.as_tuple()
    return got == expected


@pytest.mark.spec("TRN-11", "TRN-15", "TRN-13")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(("field", "value", "expected"), _HELD)
def test_the_nearest_legitimate_values_cross_by_every_route(
    route: str, field: str, value: object, expected: object
) -> None:
    sent, find = _ROUTES[route](field, value)
    read = find(_read(sent, _send(sent)))
    assert type(read) is Leaves
    assert _exactly(getattr(read, field), expected)


# CON-35 and CON-36: the sender's conversions.


def _is_negative_zero(number: object) -> bool:
    return type(number) is float and number == 0 and math.copysign(1.0, number) < 0


def _numbers(value: object) -> Iterator[object]:
    """Every number in a parsed JSON value."""
    if type(value) is dict:
        for part in value.values():
            yield from _numbers(part)
    elif type(value) is list:
        for part in value:
            yield from _numbers(part)
    elif type(value) in (int, float):
        yield value


def _has_negative_zero(value: object) -> bool:
    return any(map(_is_negative_zero, _numbers(value)))


@pytest.mark.spec("CON-35", "TRN-10")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("held_in", ["a-field", "a-map"])
def test_a_negative_zero_is_written_as_zero_by_every_route(
    route: str, held_in: str
) -> None:
    held = {"a": -0.0, "b": 1.0}
    sent, find = (
        _ROUTES[route]("ratio", held["a"])
        if held_in == "a-field"
        else _ROUTES[route]("named", held)
    )
    text = _send(sent)
    assert not _has_negative_zero(json.loads(text))
    read = find(_read(sent, text))
    zero = read.ratio if held_in == "a-field" else read.named["a"]
    assert type(zero) is float
    assert zero == 0
    assert not _is_negative_zero(zero)
    # The sender's own value is left as it was.
    assert _is_negative_zero(held["a"])


@pytest.mark.spec("CON-35", "TRN-10")
def test_a_validated_negative_zero_is_written_as_zero() -> None:
    # Validation holds -0.0 as it is given, so the conversion is the sender's.
    sent = Leaves(ratio=-0.0, named={"a": -0.0}, few=[0])
    assert _is_negative_zero(sent.ratio)
    written = json.loads(_send(sent))
    assert (written["ratio"], written["named"], written["few"]) == (0, {"a": 0}, [0])
    assert not _has_negative_zero(written)


@pytest.mark.spec("CON-36", "TRN-10")
@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize(
    "when",
    [
        datetime(1930, 1, 1, 12, tzinfo=_AMSTERDAM_1930),
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(seconds=-1))),
        datetime(
            2026, 6, 1, 0, 0, 0, 5, tzinfo=timezone(timedelta(hours=1, seconds=1))
        ),
        datetime(2026, 6, 1, tzinfo=timezone(timedelta(microseconds=1))),
        Moment(1930, 1, 1, 12, tzinfo=_AMSTERDAM_1930),
        datetime(1, 1, 1, 0, 0, 1, tzinfo=timezone(timedelta(seconds=1))),
        datetime(9999, 12, 31, 23, 59, 58, tzinfo=timezone(timedelta(seconds=-1))),
    ],
    ids=[
        "amsterdam-1930",
        "minus-one-second",
        "with-microseconds",
        "offset-of-a-microsecond",
        "of-a-subclass",
        "first-instant",
        "last-second",
    ],
)
def test_a_timestamp_offset_with_seconds_is_written_in_utc_by_every_route(
    route: str, when: datetime
) -> None:
    sent, find = _ROUTES[route]("when", when)
    text = _send(sent)
    read = find(_read(sent, text)).when
    assert type(read) is datetime
    assert read == when
    assert read.utcoffset() == timedelta(0)
    assert f'"{read.isoformat().replace("+00:00", "Z")}"'.encode() in text
    # The sender's own value keeps its offset.
    assert when.utcoffset() != timedelta(0)


@pytest.mark.spec("CON-36", "TRN-10")
def test_a_validated_timestamp_offset_with_seconds_is_written_in_utc() -> None:
    when = datetime(1930, 1, 1, 12, tzinfo=_AMSTERDAM_1930)
    sent = Leaves(when=when)
    assert json.loads(_send(sent))["when"] == "1930-01-01T11:40:28Z"
    assert sent.when is when


@pytest.mark.spec("CON-36", "TRN-10")
@pytest.mark.parametrize(
    ("offset", "written"),
    [
        (timedelta(hours=5, minutes=30), "2026-01-01T00:00:00+05:30"),
        (timedelta(minutes=-1), "2026-01-01T00:00:00-00:01"),
        (timedelta(0), "2026-01-01T00:00:00Z"),
        (timedelta(hours=23, minutes=59), "2026-01-01T00:00:00+23:59"),
    ],
)
def test_a_timestamp_offset_in_whole_minutes_is_kept(
    offset: timedelta, written: str
) -> None:
    # The nearest legitimate offsets are written as they are.
    sent = Leaves(when=datetime(2026, 1, 1, tzinfo=timezone(offset)))
    assert json.loads(_send(sent))["when"] == written


# Every sender: replies and error details.


@pytest.mark.spec("TRN-11", "TRN-15", "CON-38", "CON-41")
@pytest.mark.parametrize("subject", ["the reply", "the detail"])
@pytest.mark.parametrize(
    ("field", "value", "refusal"),
    [
        ("count", True, _NOT_HELD),
        ("count", 2**53, _NOT_DESCRIBED),
        ("ratio", _NAN, _NOT_FINITE),
        ("name", "\ud800", _NOT_WRITTEN),
        ("few", [1, 2, 3], _NOT_DESCRIBED),
    ],
)
def test_a_reply_and_an_error_detail_get_the_same_check(
    subject: str, field: str, value: object, refusal: str
) -> None:
    sender = codec(SENDING, Leaves)
    with pytest.raises(ValueError, match=re.escape(refusal)) as raised:
        sender.send(_built(field, value), subject)
    assert str(raised.value).startswith(f"{subject} ")
    assert sender.send(_built("count", 1), subject) == sender.send(
        Leaves(count=1), subject
    )


@pytest.mark.spec("CON-35", "CON-36", "TRN-10")
def test_a_reply_and_an_error_detail_get_the_same_conversions() -> None:
    reply = Leaves(ratio=-0.0, when=datetime(1930, 1, 1, 12, tzinfo=_AMSTERDAM_1930))
    sent = json.loads(codec(SENDING, Leaves).send(reply, "the reply"))
    assert (sent["ratio"], sent["when"]) == (0.0, "1930-01-01T11:40:28Z")
    assert not _is_negative_zero(sent["ratio"])


# What a refusal says.


@pytest.mark.spec("TRN-11")
@pytest.mark.parametrize(
    ("field", "value", "said"),
    [
        (
            "count",
            True,
            "the request holds a value of type bool at Leaves.count, which reads "
            "back as one of type int, so it is not sent",
        ),
        (
            "name",
            Marked("ab"),
            "holds a value of type Marked at Leaves.name, which reads back as one "
            "of type str",
        ),
        (
            "ratio",
            2**53 + 1,
            "holds a value of type int at Leaves.ratio, which reads back as one of "
            "type float",
        ),
        ("few", [1, True], "holds a value of type bool at Leaves.few[1], which reads"),
        ("named", {"a": True}, "of type bool at Leaves.named['a'], which reads back"),
        (
            "named",
            {Marked("a"): 1.0},
            "holds a key of type Marked at Leaves.named, which reads back as one "
            "of type str",
        ),
    ],
)
def test_a_refusal_names_the_place_and_both_kinds(
    field: str, value: object, said: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(said)):
        _send(_built(field, value))


_PRIVATE = "sk_live_51HxQ7cS3cr3tT0k3n"


class Login(BaseModel):
    """A record whose author keeps its values out of errors."""

    model_config = ConfigDict(hide_input_in_errors=True)

    token: Annotated[str, BeforeValidator(str.strip)] = ""
    few: list[str] = Field(default_factory=list, max_length=1)
    tokens: dict[str, str] = Field(default_factory=dict)
    pins: dict[str, int] = Field(default_factory=dict)
    pin: int = Field(default=0, lt=10**12)
    issued: AwareDatetime | None = None


class Open(Login):
    """The same fields, with Pydantic's own setting: inputs show in errors."""

    model_config = ConfigDict(hide_input_in_errors=False)


_UNSHOWN = [
    ("token", _PRIVATE + "\n", "which reads back as another value of type str"),
    ("token", Marked(_PRIVATE), _NOT_HELD),
    ("few", [_PRIVATE, _PRIVATE], _NOT_DESCRIBED),
    ("tokens", {Marked(_PRIVATE): "a"}, _NOT_HELD),
    ("tokens", {"a": Marked(_PRIVATE)}, _NOT_HELD),
    ("pin", 5111111111111, _NOT_DESCRIBED),
    (
        "issued",
        datetime(1, 1, 1, tzinfo=timezone(timedelta(seconds=51))),
        "a timestamp outside years 0001 to 9999 in UTC",
    ),
]
_SHOWN = (_PRIVATE, "5111111111111", "0001-01-01", "datetime(1, 1, 1")


def _whole_text(error: BaseException) -> str:
    """Everything an exception can put in a log: its own text and that of
    each exception behind it, chained or only kept as its context."""
    said = "".join(traceback.format_exception(error))
    behind = error.__cause__ or error.__context__
    return said + (_whole_text(behind) if behind is not None else "")


@pytest.mark.spec("TRN-11", "TRN-15")
@pytest.mark.parametrize(
    ("field", "value", "refusal"),
    [
        *_UNSHOWN,
        # A map's key, and what only the serializer's own error shows.
        ("tokens", {_PRIVATE: Marked("a")}, r"at Login\.tokens\[entry 0\], which "),
        ("pins", {"a": 1, _PRIVATE: True}, r"at Login\.pins\[entry 1\], which "),
        ("pin", _PRIVATE, _NOT_WRITTEN),
        ("few", [1, _PRIVATE], _NOT_WRITTEN),
        ("pins", {_PRIVATE: "a"}, _NOT_WRITTEN),
        ("pins", {_PRIVATE: 2**60}, _NOT_DESCRIBED),
    ],
)
def test_a_refusal_shows_nothing_a_record_that_hides_its_inputs_holds(
    field: str, value: object, refusal: str
) -> None:
    # A value held at send time can be a secret, and an exception's text is
    # logged. For a record whose config hides inputs in errors, a refusal
    # shows no value and no map key, in its own words or in those of any
    # exception behind it.
    declared, _ = _contract_for(Login)
    held = Login()
    setattr(held, field, value)
    with pytest.raises(ValueError, match=refusal) as refused:
        check_request(declared, declared.actions["send"], held)
    said = _whole_text(refused.value)
    for shown in _SHOWN:
        assert shown not in said


@pytest.mark.spec("TRN-11", "TRN-15")
@pytest.mark.parametrize(("field", "value", "refusal"), _UNSHOWN)
def test_a_refusal_in_neoliths_own_words_shows_no_value(
    field: str, value: object, refusal: str
) -> None:
    # Whatever the record's config, the message itself names the place and
    # the types and shows no value.
    declared, _ = _contract_for(Open)
    held = Open()
    setattr(held, field, value)
    with pytest.raises(ValueError, match=refusal) as refused:
        check_request(declared, declared.actions["send"], held)
    for shown in _SHOWN:
        assert shown not in str(refused.value)


@pytest.mark.spec("TRN-11")
def test_a_refusal_names_a_map_entry_by_its_key_as_pydantic_does() -> None:
    # Without the setting, a place in a map is named by its key, and the
    # serializer's error stays as the cause, for the author to find the field.
    declared, _ = _contract_for(Open)
    held = Open()
    held.pins = {"first": True}
    with pytest.raises(ValueError, match=r"at Open\.pins\['first'\], which reads"):
        check_request(declared, declared.actions["send"], held)
    held.pins = {"first": "text"}  # type: ignore[dict-item]
    with pytest.raises(ValueError, match=r"Open \(\w+; see the cause\)") as refused:
        check_request(declared, declared.actions["send"], held)
    assert refused.value.__cause__ is not None
    hidden = Login()
    hidden.pins = {"first": "text"}  # type: ignore[dict-item]
    declared, _ = _contract_for(Login)
    with pytest.raises(ValueError, match=r"Login \(\w+\)$") as refused:
        check_request(declared, declared.actions["send"], hidden)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None


@pytest.mark.spec("TRN-10")
@pytest.mark.parametrize(
    ("digits", "said"),
    [
        (5000, "holds a number too long to be read as JSON text"),
        (400, _NOT_DESCRIBED),
    ],
)
def test_a_number_too_long_to_read_is_refused_as_that(digits: int, said: str) -> None:
    # A JSON reader stops at an integer of more than 4300 digits, which is
    # not what its limit on depth refuses, nor a misfit with the description.
    with pytest.raises(ValueError, match=said) as refused:
        _send(_built("count", 10**digits))
    assert "nested" not in str(refused.value)


@pytest.mark.spec("TRN-11")
def test_a_refusal_names_a_place_inside_nested_records() -> None:
    sent, _ = _replaced("count", True)
    with pytest.raises(ValueError, match=r"at Holder\.many\[1\]\.count, which reads"):
        _send(sent)
    sent, _ = _keyed("named", {"a": True})
    with pytest.raises(ValueError, match=r"at Holder\.keyed\['k'\]\.named\['a'\], "):
        _send(sent)


@pytest.mark.spec("TRN-11")
def test_a_record_that_lacks_a_field_its_reader_fills_in_is_refused() -> None:
    # The text passes the description, since the field has a default, but the
    # receiver would read a value the sender does not hold.
    holder = Holder()
    holder.one = cast("Leaves", types.SimpleNamespace(**vars(Leaves())))
    del vars(holder.one)["count"]
    with pytest.raises(ValueError, match=r"lacks Holder\.one\.count, so it is not"):
        _send(holder)
    lacking = Leaves()
    del vars(lacking)["few"]
    with pytest.raises(ValueError, match=r"lacks Leaves\.few, so it is not sent"):
        _send(lacking)


@pytest.mark.spec("TRN-11")
@pytest.mark.parametrize(
    ("copy", "read"),
    [
        ([1], [1, 2]),
        ((1, 2), [1, 2]),
        (_Fields(a=1), {"b": 1}),
        (_Fields(a=1, b=2), {"a": 1}),
        ({"a": 1}, {"a": 1}),
        ({"sku": "a", "quantity": 1}, RefPart(sku="a")),
        ([1], _Fields(a=1)),
        (date(2026, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 1, 1, tzinfo=UTC), date(2026, 1, 1)),
        (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)),
        ("red", Colour.RED),
        (1, 1.5),
        (None, 0),
        (0, None),
        ((1,), (1,)),
        (frozenset(), frozenset()),
    ],
    ids=repr,
)
def test_the_comparison_fails_closed_on_what_no_serializer_writes(
    copy: object, read: object
) -> None:
    # The comparison does not lean on the serializer: a copy of another
    # shape than what was read, or a kind no declared type reads, differs.
    assert _difference(copy, read, "x") is not None


@pytest.mark.spec("TRN-11")
def test_the_comparison_passes_a_copy_equal_to_what_was_read() -> None:
    read = Holder(one=Leaves(named={"a": 1.5}), many=[Leaves(few=[1])])
    copy = _converted(Holder.__pydantic_serializer__.to_python(read))[0]
    assert _difference(copy, read, "Holder") is None


class Elsewhere(BaseModel):
    """A record of another type, one of whose fields has a name ``Leaves`` has."""

    ratio: float = 1.5
    other: str = ""


class Unrelated:
    """No record, and holds no field of one."""


_NO_RECORD: dict[str, Callable[[], object]] = {
    "object": Unrelated,
    "namespace": types.SimpleNamespace,
    "map-with-attributes": OrderedDict,
    "record-of-another-type": Elsewhere,
}


@pytest.mark.spec("TRN-9", "TRN-11")
@pytest.mark.parametrize("stand_in", _NO_RECORD)
@pytest.mark.parametrize("place", ["one", "maybe", "many", "keyed"])
def test_what_holds_no_field_of_a_record_never_stands_for_it(
    stand_in: str, place: str
) -> None:
    # pydantic-core writes any object that has attributes in a record's place,
    # from the fields it finds. Every field of Leaves has a default, so the
    # text for an object without them fits the description and would read
    # back as a Leaves the sender never held.
    value: Any = _NO_RECORD[stand_in]()
    holder = Holder()
    if place in ("one", "maybe"):
        setattr(holder, place, value)
    elif place == "many":
        holder.many.append(value)
    else:
        holder.keyed["k"] = value
    with pytest.raises(ValueError, match=r"lacks Holder\.\S+\.flag, so it is not sent"):
        _send(holder)
    # The nearest legitimate shape holds every field, and crosses as a Leaves.
    holder = Holder()
    holder.maybe = cast("Leaves", LooksLikeLeaves(count=3))
    assert _read(holder, _send(holder)).maybe == Leaves(count=3)


# The sender's copy.


class _Meddling(tzinfo):
    """An offset from UTC that changes a request whenever it is asked for."""

    def __init__(self) -> None:
        self.request: Leaves | None = None
        self.asked = 0

    def utcoffset(self, moment: datetime | None) -> timedelta:
        self.asked += 1
        if self.request is not None:
            self.request.count = True
            self.request.name = "changed"
        # A later answer than the first: an hour more each time it is asked.
        return timedelta(hours=self.asked)

    def dst(self, moment: datetime | None) -> None:
        return None

    def tzname(self, moment: datetime | None) -> str:
        return "UTC"


@pytest.mark.spec("TRN-10", "TRN-11")
def test_a_change_made_while_a_value_is_sent_does_not_reach_the_text() -> None:
    # The serializer reads the value once, into the sender's copy, and the
    # copy is what is converted, written, checked and compared. Asking a
    # timestamp for its offset runs the caller's code, which here changes the
    # request after it was read, and answers differently each time. The sender
    # asks once, while it makes its copy, so the text holds what was read and
    # the first answer.
    zone = _Meddling()
    request = Leaves(count=7, name="read", when=datetime(2026, 1, 1, tzinfo=zone))
    zone.request, zone.asked = request, 0
    sent = json.loads(_send(request))
    assert zone.asked == 1
    assert (sent["count"], sent["name"]) == (7, "read")
    assert sent["when"] == "2026-01-01T00:00:00+01:00"
    assert (request.count, request.name) == (True, "changed")
    # Sent again, the changed value is refused.
    with pytest.raises(ValueError, match=_NOT_HELD):
        _send(request)


class Chain(BaseModel):
    link: "Chain | None" = None
    ratio: float = 0.0


@contract("chains.v1")
class Chains(Protocol):
    @action
    def send(self, request: Chain) -> None: ...


def _descend(levels: int, call: Callable[[], bytes]) -> bytes:
    return call() if levels == 0 else _descend(levels - 1, call)


def _stack_depth() -> int:
    depth = 0
    frame: types.FrameType | None = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


@pytest.mark.spec("TRN-10", "TRN-11")
def test_a_deep_value_is_sent_from_a_deep_call_stack() -> None:
    # A value nested as deep as the reader goes is converted and compared
    # without recursion, so how deep the caller's own stack is does not decide
    # whether it is sent.
    declared = Contract.of(Chains)
    chain = Chain(ratio=-0.0)
    for _ in range(180):
        chain = Chain(link=chain)

    def send() -> bytes:
        return check_request(declared, declared.actions["send"], chain)

    room = sys.getrecursionlimit() - _stack_depth() - 60
    assert room > 200
    text = _descend(room, send)
    assert text == send()
    assert b"-0" not in text


# What the reader's own validator makes another value of the same type.


def _floored(given: object) -> object:
    """A timestamp without its seconds."""
    moment = datetime.fromisoformat(given) if type(given) is str else given
    return cast("datetime", moment).replace(second=0, microsecond=0)


def _first_of_its_month(given: object) -> object:
    day = date.fromisoformat(given) if type(given) is str else given
    return cast("date", day).replace(day=1)


class Clamped(BaseModel):
    """Each field's own before validator reads some values as another value
    of the same type, and reads what it returns as itself."""

    name: Annotated[str, BeforeValidator(str.lower)] = ""
    level: Annotated[int, BeforeValidator(lambda level: min(level, 9))] = 0
    flag: Annotated[bool, BeforeValidator(lambda flag: False)] = False
    ratio: Annotated[float, BeforeValidator(lambda ratio: round(ratio * 2) / 2)] = 0.0
    span: Annotated[
        Duration, BeforeValidator(lambda span: "1s" if type(span) is str else span)
    ] = timedelta(seconds=1)
    raw: Annotated[
        Binary, BeforeValidator(lambda raw: "YQ==" if type(raw) is str else raw)
    ] = b"a"
    when: Annotated[AwareDatetime, BeforeValidator(_floored)] = datetime(
        2026, 1, 1, tzinfo=UTC
    )
    day: Annotated[date, BeforeValidator(_first_of_its_month)] = date(2026, 1, 1)


_CLAMPED = [
    ("name", "ABC", "abc"),
    ("level", 10, 9),
    ("flag", True, False),
    ("ratio", 0.3, 0.5),
    ("span", timedelta(seconds=5), timedelta(seconds=1)),
    ("raw", b"abc", b"a"),
    (
        "when",
        datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC),
        datetime(2026, 1, 1, 12, tzinfo=UTC),
    ),
    ("day", date(2026, 1, 15), date(2026, 1, 1)),
]


@pytest.mark.spec("TRN-11")
@pytest.mark.parametrize(("field", "value", "kept"), _CLAMPED)
def test_a_value_read_back_as_another_of_its_type_is_refused(
    field: str, value: object, kept: object
) -> None:
    # The text fits the description, and what is read back has the type of
    # what was held, so only comparing the two values shows the change.
    declared, _ = _contract_for(Clamped)
    held = Clamped()
    setattr(held, field, value)
    with pytest.raises(ValueError, match="which reads back as another value of type"):
        check_request(declared, declared.actions["send"], held)
    setattr(held, field, kept)
    assert check_request(declared, declared.actions["send"], held)


def test_every_kind_compared_by_value_has_a_value_that_reads_back_changed() -> None:
    """Fails when the comparison learns a kind the table above lacks. A
    decimal has a test of its own, below."""
    assert {type(kept) for _, _, kept in _CLAMPED} | {Decimal} == _EXACT | {
        float,
        datetime,
        Decimal,
    }


def _in_local_time(given: object) -> object:
    """A timestamp in the zone whose clocks go back, as a reader that keeps
    local times hands it on."""
    if type(given) is not str:
        return given
    moment = datetime.fromisoformat(given).astimezone(UTC).replace(tzinfo=None)
    back = datetime(2025, 10, 26, 1)
    if moment < back:
        return (moment + timedelta(hours=2)).replace(tzinfo=_LOCAL)
    again = moment < back + timedelta(hours=1)
    return (moment + timedelta(hours=1)).replace(tzinfo=_LOCAL, fold=int(again))


class Zoned(BaseModel):
    when: Annotated[AwareDatetime, BeforeValidator(_in_local_time)]


@pytest.mark.spec("TRN-11")
@pytest.mark.parametrize("hour", [0, 1, 2])
def test_a_timestamp_its_reader_hands_back_in_a_zone_is_the_same_instant(
    hour: int,
) -> None:
    # The reader's own validator hands back a time in a zone where that time
    # comes twice, for which ``==`` across zones is False (PEP 495). An
    # instant is compared by subtraction, so the value is sent.
    declared, _ = _contract_for(Zoned)
    held = Zoned(when=datetime(2025, 10, 26, hour, 30, tzinfo=UTC))
    read = codec(declared, Zoned).read(
        check_request(declared, declared.actions["send"], held)
    )
    assert read.when.tzinfo is _LOCAL
    assert (read.when == held.when) is (hour == 2)
    assert read.when - held.when == timedelta(0)


class _Unnamed(type):
    def __getattribute__(cls, name: str) -> object:
        if name == "__qualname__":
            raise RuntimeError("no name")
        return super().__getattribute__(name)


class Nameless(str, metaclass=_Unnamed):
    __slots__ = ()


@pytest.mark.spec("TRN-11")
def test_a_comparison_that_fails_refuses_the_value() -> None:
    # Whatever goes wrong while the copy is compared with what was read
    # refuses the value, as every failure of a send does (ValueError).
    with pytest.raises(ValueError, match="cannot be compared with what it reads") as (
        refused
    ):
        _send(_built("name", Nameless("ab")))
    assert type(refused.value.__cause__) is RuntimeError


# A decimal's digits.


def _without_trailing_zeros(value: object) -> object:
    """A reader's own before validator: it keeps a decimal's value and drops
    its trailing zeros."""
    return Decimal(cast("str", value)).normalize()


class Rounded(BaseModel):
    amount: Annotated[Decimal128, BeforeValidator(_without_trailing_zeros)] = Decimal(
        "1.5"
    )


@pytest.mark.spec("TRN-11")
def test_a_decimal_that_reads_back_with_other_digits_is_refused() -> None:
    # Trailing zeros are kept on the wire (spec 01), so 1.50 and 1.5 are two
    # values, although Python calls them equal. Here the record's own reader
    # drops the zeros, so a sender that holds 1.50 would be read as 1.5.
    declared, _ = _contract_for(Rounded)
    held = Rounded()
    held.amount = Decimal("1.50")
    with pytest.raises(
        ValueError,
        match=r"at Rounded\.amount, which reads back as another value of type Dec",
    ):
        check_request(declared, declared.actions["send"], held)
    # The nearest legitimate values: one without trailing zeros, and one the
    # sender's own writer respells (CON-37), which is the same value.
    for amount, written in [(Decimal("2.5"), b'"2.5"'), (Decimal("1E+2"), b'"100"')]:
        held.amount = amount
        assert written in check_request(declared, declared.actions["send"], held)


# A tool that freezes time for a test.


class _Frozen(type):
    """As the fakes of freezegun are: an instance of the real class counts
    as an instance of the fake."""

    def __instancecheck__(cls, instance: object) -> bool:
        return type.__instancecheck__(cls.__mro__[1], instance)


class FrozenDatetime(datetime, metaclass=_Frozen):
    pass


class FrozenDate(date, metaclass=_Frozen):
    pass


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> int:
    """Swap every name in neolith's modules that is bound to ``datetime`` or
    ``date`` for a fake, as freezegun does in every loaded module while it
    freezes time. Returns how many names were swapped."""
    fakes = {id(datetime): FrozenDatetime, id(date): FrozenDate}
    swapped = 0
    for name, module in list(sys.modules.items()):
        if name == "neolith" or name.startswith("neolith."):
            for attribute, held in list(vars(module).items()):
                if id(held) in fakes:
                    monkeypatch.setattr(module, attribute, fakes[id(held)])
                    swapped += 1
    return swapped


@pytest.mark.spec("TRN-10", "TRN-11", "TRN-15", "CON-36")
def test_timestamps_and_dates_cross_while_a_tool_freezes_time(frozen: int) -> None:
    # The sender's own code must not read ``datetime`` or ``date`` through a
    # module-level name while it runs, since such a tool rebinds those.
    assert frozen >= 2
    whole = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=1)))
    odd = datetime(1930, 1, 1, 12, tzinfo=_AMSTERDAM_1930)
    fake = FrozenDatetime(2026, 3, 1, tzinfo=UTC)
    for when, written in [
        (whole, "2026-01-01T12:00:00+01:00"),
        (odd, "1930-01-01T11:40:28Z"),
        (fake, "2026-03-01T00:00:00Z"),
    ]:
        sent = Leaves(when=when, day=date(2026, 1, 1))
        assert type(sent.when) is type(when)
        text = _send(sent)
        assert json.loads(text)["when"] == written
        read = _read(sent, text)
        assert (read.when, read.day) == (when, date(2026, 1, 1))
        assert (type(read.when), type(read.day)) == (datetime, date)
    assert _read(Leaves(), _send(Leaves(day=FrozenDate(2026, 3, 1)))).day == date(
        2026, 3, 1
    )
    # What was refused is still refused.
    for field, value, refusal in [
        ("when", date(2026, 1, 1), _NOT_WRITTEN),
        ("day", whole, _NOT_WRITTEN),
        ("when", datetime(2026, 1, 1), _NOT_DESCRIBED),
        (
            "when",
            datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
            _NOT_DESCRIBED,
        ),
        ("when", datetime(1, 1, 1, tzinfo=timezone(timedelta(seconds=1))), "outside"),
        ("count", True, _NOT_HELD),
    ]:
        with pytest.raises(ValueError, match=re.escape(refusal)):
            _send(_built(field, value))


_IMPORTED_FROZEN = """
import datetime as module
from typing import Protocol

from pydantic import AwareDatetime, BaseModel


class _Frozen(type):
    def __instancecheck__(cls, instance):
        return type.__instancecheck__(cls.__mro__[1], instance)


class FrozenDatetime(module.datetime, metaclass=_Frozen):
    pass


class FrozenDate(module.date, metaclass=_Frozen):
    pass


real = module.datetime, module.date
module.datetime, module.date = FrozenDatetime, FrozenDate
from neolith import Contract, action, contract
from neolith._crossing import check_request
module.datetime, module.date = real


class Event(BaseModel):
    when: AwareDatetime
    day: module.date


@contract("frozen.v1")
class Events(Protocol):
    @action
    def send(self, request: Event) -> None: ...


declared = Contract.of(Events)
text = b'{"when":"2026-01-01T12:00:00+01:00","day":"2026-01-01"}'
event = Event.model_validate_json(text)
assert check_request(declared, declared.actions["send"], event) == text
print("sent")
"""


@pytest.mark.spec("TRN-10", "TRN-11")
def test_timestamps_and_dates_cross_when_neolith_was_imported_under_a_freeze() -> None:
    # A test that freezes time may be the first to import neolith. The names
    # ``datetime`` and ``date`` are then fakes while its modules run, and the
    # sender must still know the real classes, for the rest of the process.
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _IMPORTED_FROZEN],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert (ran.stdout.strip(), ran.stderr[-600:]) == ("sent", "")


@pytest.mark.spec("TRN-15")
def test_the_check_reads_days_and_instants_while_a_tool_freezes_time(
    frozen: int,
) -> None:
    assert frozen >= 2
    checker = _checker(_WORDED_SCHEMA)
    for value, admitted in [
        ({"day": "2028-02-29"}, True),
        ({"day": "2027-02-29"}, False),
        ({"when": "0001-01-01T00:00:00Z"}, True),
        ({"when": "0001-01-01T00:00:00+00:01"}, False),
        ({"when": "2027-02-29T00:00:00Z"}, False),
    ]:
        assert _admits(checker, {"inner": {"sku": "a"}, **value}) is admitted


# The sender's copy is written as the value itself would be.


def _contract_for(
    record: type[BaseModel], identity: str | None = None
) -> tuple[Contract, dict[str, Any]]:
    """Declare a contract whose one action takes and returns ``record``, and
    return it with the description of ``record``."""

    def send(self: object, request: record) -> record: ...  # type: ignore[valid-type]

    declared = types.new_class(
        _name("S"), (Protocol,), exec_body=lambda ns: ns.update(send=action(send))
    )
    contract(identity or f"{_name('sent')}.v1")(declared)
    found = Contract.of(declared)
    schema = {
        "$defs": found.describe()["$defs"],
        "$ref": f"#/$defs/{record.__name__}",
    }
    return found, schema


def _checker(schema: dict[str, Any]) -> Any:  # noqa: ANN401
    """The description check of the record a schema refers to."""
    return description_checker(schema["$defs"], schema["$ref"].rsplit("/", 1)[-1])


def _admits(checker: Any, value: object) -> bool:  # noqa: ANN401
    try:
        checker.validate_json(json.dumps(value))
    except ValueError:
        return False
    return True


_LEAVES_SCHEMA = {"$defs": SENDING.describe()["$defs"], "$ref": "#/$defs/Leaves"}
_REFERENCE, _REFERENCE_SCHEMA = _contract_for(Reference)


def _instant_in_range(text: object) -> bool:
    """A timestamp's instant falls in years 0001 to 9999 in UTC (CON-40)."""
    if type(text) is not str or not _FORMAT_CHECKER.conforms(text, "date-time"):
        return False
    try:
        datetime.fromisoformat(text).astimezone(UTC)
    except (OverflowError, ValueError):
        # Out of range, or a final newline, which the format check lets by.
        return False
    return True


def _whole_pattern(
    validator: Any,  # noqa: ANN401
    pattern: str,
    instance: object,
    schema: object,
) -> Iterator[SchemaValidationError]:
    if type(instance) is str and re.fullmatch(pattern, instance) is None:
        yield SchemaValidationError(f"{instance!r} does not match {pattern!r}")


def _written_as_an_integer(checker: object, instance: object) -> bool:
    return type(instance) is int


# The reference for the description check: a JSON Schema 2020-12 validator
# with format checks, and with the three rules of spec 01 that JSON Schema
# does not have. A pattern matches only the whole string (python-jsonschema
# searches, so "$" admits a final newline). An integer is written with no
# fraction or exponent, and a timestamp's instant falls in years 0001 to 9999
# in UTC (CON-40).
_FORMATS = FormatChecker(formats=("date",))
_FORMATS.checks("date-time")(_instant_in_range)
_Reference = validators.extend(  # type: ignore[no-untyped-call]  # its stub is untyped
    Draft202012Validator,
    validators={"pattern": _whole_pattern},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", _written_as_an_integer
    ),
)


def _fits(schema: dict[str, Any], value: object) -> bool:
    return bool(_Reference(schema, format_checker=_FORMATS).is_valid(value))


def test_the_reference_validator_checks_what_json_schema_leaves_out() -> None:
    """Guard the reference itself: each added rule refuses, and its nearest
    legitimate value passes."""
    pattern = {"type": "string", "pattern": "^a$"}
    assert _fits(pattern, "a")
    assert not _fits(pattern, "a\n")
    assert Draft202012Validator(pattern).is_valid("a\n")
    whole = {"type": "integer"}
    assert _fits(whole, 1)
    assert not _fits(whole, 1.0)
    assert not _fits(whole, True)
    assert _fits({"type": "number"}, 1)
    instant = {"type": "string", "format": "date-time"}
    assert _fits(instant, "0001-01-01T00:00:00Z")
    assert not _fits(instant, "0001-01-01T00:00:00+00:01")
    assert not _fits(instant, "2027-02-29T00:00:00Z")
    assert not _fits({"type": "string", "format": "date"}, "2027-02-29")


@pytest.mark.spec("TRN-10", "TRN-15")
@settings(parent=_HYPOTHESIS, max_examples=60)
@given(st.data())
def test_a_generated_value_is_written_as_its_own_serializer_writes_it(
    data: st.DataObject,
) -> None:
    # The text comes from the sender's copy. For a value that needs no
    # conversion it is, byte for byte, what the record's own serializer
    # writes for the value, and it fits the description.
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    try:
        found, schema = _contract_for(record)
    except ContractDefinitionError:
        return
    raw = data.draw(admitted_by(schema))
    assume(_fits(schema, raw))
    value = codec(found, record).read(json.dumps(raw).encode())
    text = check_request(found, found.actions["send"], value)
    assert _fits(schema, json.loads(text))
    if not _has_negative_zero(raw):
        assert text == record.__pydantic_serializer__.to_json(value)


# Invariants: whatever a sender does to a value after validation, what is
# sent fits the description and reads back as what the sender held.


def _leaf_values() -> list[object]:
    return [
        True,
        False,
        None,
        0,
        7,
        -1,
        2**53 - 1,
        2**53,
        2**53 + 1,
        Level.ONE,
        Whole(3),
        0.0,
        -0.0,
        1.5,
        _NAN,
        _INF,
        Ratio(2.5),
        "",
        "s",
        "red",
        "abcdefghij",
        "\ud800",
        Marked("m"),
        Colour.RED,
        b"\xff",
        bytearray(b"a"),
        Decimal("1.50"),
        Decimal("1E+2"),
        Decimal("NaN"),
        timedelta(seconds=90),
        timedelta(days=999999999),
        date(2026, 1, 1),
        Day(2026, 1, 1),
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(1930, 1, 1, tzinfo=_AMSTERDAM_1930),
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        Moment(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))),
        datetime(2025, 10, 26, 2, 30, fold=1, tzinfo=_LOCAL),
    ]


_ANYTHING: st.SearchStrategy[object] = st.deferred(
    lambda: (
        st.sampled_from(_leaf_values())
        | st.lists(_ANYTHING, max_size=3)
        | st.dictionaries(
            st.sampled_from(["a", "b", Marked("c"), Colour.RED, 1]),
            _ANYTHING,
            max_size=2,
        )
        | st.builds(
            lambda count: RefPart.model_construct(sku="p", quantity=count), _ANYTHING
        )
        | st.builds(lambda sku: RefPart.model_construct(sku=sku), _ANYTHING)
    )
)


def _places(value: object) -> list[tuple[Any, Any]]:
    """Every place in a value that holds something, as (what holds it, where)."""
    places: list[tuple[Any, Any]] = []
    pending = [value]
    while pending:
        item = pending.pop()
        holder: Any
        if isinstance(item, BaseModel):
            holder = vars(item)
        elif type(item) in (list, dict):
            holder = item
        else:
            continue
        keys = list(range(len(holder))) if type(holder) is list else list(holder)
        places.extend((holder, key) for key in keys)
        pending.extend(holder[key] for key in keys)
    return places


# The pairs of kinds TRN-11 lets a part be held as and read back as, beside
# an enum member, which is read back as the very member.
_READ_BACK_AS: dict[type, tuple[type, ...]] = {
    bool: (bool,),
    int: (int,),
    float: (float, int),
    str: (str,),
    bytes: (bytes,),
    Decimal: (Decimal,),
    timedelta: (timedelta,),
    type(None): (type(None),),
}


def _held_as(sent: object, read: object) -> bool:
    """The test's own statement of TRN-11: what was read is what was held."""
    if isinstance(read, BaseModel):
        return type(sent) is type(read) and all(
            name in vars(sent) and _held_as(vars(sent)[name], part)
            for name, part in vars(read).items()
        )
    if type(read) is list:
        return (
            isinstance(sent, list)
            and len(sent) == len(read)
            and all(_held_as(a, b) for a, b in zip(sent, read, strict=True))
        )
    if type(read) is dict:
        return (
            isinstance(sent, dict)
            and len(sent) == len(read)
            and all(type(key) is str for key in sent)
            and all(
                key in sent and _held_as(sent[key], part) for key, part in read.items()
            )
        )
    if isinstance(read, enum.Enum):
        return sent is read
    if type(read) is datetime:
        if not isinstance(sent, datetime):
            return False
        offset = sent.utcoffset()
        # By subtraction: ``==`` is False across zones for a time that comes
        # twice (PEP 495).
        if offset is None or sent - read != timedelta(0):
            return False
        # An offset in whole minutes is kept; any other is written in UTC.
        kept = timedelta(0) if offset % timedelta(minutes=1) else offset
        return read.utcoffset() == kept
    if type(read) is date:
        return (
            isinstance(sent, date)
            and not isinstance(sent, datetime)
            and date(sent.year, sent.month, sent.day) == read
        )
    return type(sent) in _READ_BACK_AS.get(type(read), ()) and sent == read


def _containers(value: object) -> Iterator[object]:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, BaseModel):
            yield item
            pending.extend(vars(item).values())
        elif isinstance(item, list | dict):
            yield item
            pending.extend(item.values() if isinstance(item, dict) else item)


@pytest.mark.spec("TRN-10", "TRN-11", "TRN-15", "CON-35", "CON-36", "CON-38", "CON-41")
@settings(parent=_HYPOTHESIS, max_examples=200)
@given(st.data())
def test_whatever_is_sent_fits_the_description_and_reads_back_as_it_was_held(
    data: st.DataObject,
) -> None:
    sender = codec(_REFERENCE, Reference)
    raw = data.draw(admitted_by(_REFERENCE_SCHEMA))
    assume(_fits(_REFERENCE_SCHEMA, raw))
    value = sender.read(json.dumps(raw).encode())
    # A value read from text that fits the description is always sent.
    untouched = sender.send(value, "the value")
    assert _fits(_REFERENCE_SCHEMA, json.loads(untouched))
    for _ in range(data.draw(st.integers(1, 3))):
        places = _places(value)
        holder, key = places[data.draw(st.integers(0, len(places) - 1))]
        holder[key] = data.draw(_ANYTHING)
    try:
        text = sender.send(value, "the value")
    except ValueError:
        event("refused")
        return
    event("sent")
    sent = json.loads(text)
    assert _fits(_REFERENCE_SCHEMA, sent)
    assert not _has_negative_zero(sent)
    read = sender.read(text)
    assert _held_as(value, read)
    assert not {id(c) for c in _containers(value)} & {id(c) for c in _containers(read)}


@pytest.mark.spec("TRN-11", "TRN-15")
def test_the_invariant_meets_values_that_are_sent_and_values_that_are_refused() -> None:
    # Guard the property above against passing for want of examples: of the
    # values it draws for one place, some are sent and some refused.
    sender = codec(_REFERENCE, Reference)
    sent, refused = 0, 0
    for name in ("count", "ratio", "name", "when", "day", "note", "totals", "parts"):
        for item in [*_leaf_values(), [], {}, {"a": 1}, [RefPart(sku="p")]]:
            value = Reference(
                when=datetime(2026, 1, 1, tzinfo=UTC), day=date(2026, 1, 1)
            )
            vars(value)[name] = item
            try:
                read = sender.read(sender.send(value, "the value"))
            except ValueError:
                refused += 1
            else:
                sent += 1
                assert _held_as(value, read)
    assert sent > 20
    assert refused > 100


# The description check itself.


class Inner(BaseModel):
    sku: str = Field(min_length=1)


class Worded(BaseModel):
    """A record whose description uses every keyword the writer emits."""

    whole: int = Field(default=4, gt=2, lt=10, multiple_of=2)
    part: float = Field(default=0.0, ge=-1.5, le=0.1)
    open: float = Field(default=0.5, gt=0.25, lt=0.75)
    code: str = Field(default="ab", min_length=1, max_length=3)
    flag: bool = False
    day: date | None = None
    when: AwareDatetime | None = None
    span: Duration = timedelta(0)
    amount: Decimal128 = Decimal("0")
    raw: Binary = b""
    colour: Colour = Colour.RED
    size: Literal["s", "m"] = "s"
    pair: list[int] = Field(default_factory=lambda: [1, 2], min_length=2, max_length=3)
    sized: dict[Annotated[str, StringConstraints(min_length=2, max_length=3)], int] = (
        Field(default_factory=lambda: {"ab": 1}, min_length=1, max_length=2)
    )
    keyed: dict[Literal["a", "b"], int] = Field(default_factory=dict)
    inner: Inner


_WORDED, _WORDED_SCHEMA = _contract_for(Worded)

# For each keyword, on each type of schema that carries it: what breaks it,
# and the nearest value that keeps it, each laid over a value that fits. A
# schema without a type is a reference, an ``anyOf``, a further pattern or
# what a map's keys fit. A default and a content encoding only annotate.
_BASE: dict[str, Any] = {"inner": {"sku": "a"}}
_ANNOTATIONS = {"default", "contentEncoding"}
_SAFE = 2**53 - 1
_KEYWORDS: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {
    ("type", "boolean"): ({"flag": 1}, {"flag": True}),
    ("type", "integer"): ({"whole": "4"}, {"whole": 4}),
    ("type", "number"): ({"part": "0.1"}, {"part": 0.1}),
    ("type", "string"): ({"code": 1}, {"code": "a"}),
    ("type", "array"): ({"pair": 1}, {"pair": [1, 2]}),
    ("type", "object"): ({"inner": 1}, {"inner": {"sku": "b"}}),
    ("type", "null"): ({"day": 1}, {"day": None}),
    ("properties", "object"): ({"inner": {"sku": 1}}, {"inner": {"sku": "b"}}),
    ("required", "object"): ({"inner": {}}, {"inner": {"sku": "b", "later": 1}}),
    ("$ref", ""): ({"colour": "blue"}, {"colour": "red"}),
    ("anyOf", ""): ({"day": 1}, {"day": None}),
    ("enum", "string"): ({"size": "l"}, {"size": "m"}),
    ("enum", ""): ({"keyed": {"c": 1}}, {"keyed": {"a": 1}}),
    ("minimum", "number"): ({"part": -1.75}, {"part": -1.5}),
    ("maximum", "number"): ({"part": 0.5}, {"part": 0.1}),
    ("exclusiveMinimum", "number"): ({"open": 0.25}, {"open": 0.3}),
    ("exclusiveMaximum", "number"): ({"open": 0.75}, {"open": 0.7}),
    ("minimum", "integer"): ({"pair": [1, -_SAFE - 1]}, {"pair": [1, -_SAFE]}),
    ("maximum", "integer"): ({"pair": [1, _SAFE + 1]}, {"pair": [1, _SAFE]}),
    ("exclusiveMinimum", "integer"): ({"whole": 2}, {"whole": 4}),
    ("exclusiveMaximum", "integer"): ({"whole": 10}, {"whole": 8}),
    ("multipleOf", "integer"): ({"whole": 3}, {"whole": 4}),
    ("minLength", "string"): ({"code": ""}, {"code": "a"}),
    ("maxLength", "string"): ({"code": "abcd"}, {"code": "abc"}),
    ("minLength", ""): ({"sized": {"a": 1}}, {"sized": {"ab": 1}}),
    ("maxLength", ""): ({"sized": {"abcd": 1}}, {"sized": {"abc": 1}}),
    ("pattern", "string"): ({"span": "1.5s"}, {"span": "1.500s"}),
    ("pattern", ""): ({"amount": "1" * 35}, {"amount": "1" * 34}),
    ("allOf", "string"): ({"amount": "1" * 35}, {"amount": "1" * 34}),
    ("format", "string"): ({"day": "2027-02-29"}, {"day": "2027-02-28"}),
    ("items", "array"): ({"pair": [1, "2"]}, {"pair": [1, 2]}),
    ("minItems", "array"): ({"pair": [1]}, {"pair": [1, 2]}),
    ("maxItems", "array"): ({"pair": [1, 2, 3, 4]}, {"pair": [1, 2, 3]}),
    ("additionalProperties", "object"): (
        {"sized": {"ab": "1"}},
        {"sized": {"ab": 1}},
    ),
    ("propertyNames", "object"): ({"sized": {"a": 1}}, {"sized": {"ab": 1}}),
    ("minProperties", "object"): ({"sized": {}}, {"sized": {"ab": 1}}),
    ("maxProperties", "object"): (
        {"sized": {"ab": 1, "cd": 2, "ef": 3}},
        {"sized": {"ab": 1, "cd": 2}},
    ),
}


def _schemas(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every schema in a description, at any depth."""
    yield schema
    for keyword, part in schema.items():
        if keyword in ("properties", "$defs"):
            for inner in part.values():
                yield from _schemas(inner)
        elif keyword in ("items", "additionalProperties", "propertyNames"):
            yield from _schemas(part)
        elif keyword in ("anyOf", "allOf"):
            for inner in part:
                yield from _schemas(inner)


def _keywords(schema: dict[str, Any]) -> set[tuple[str, str]]:
    """Every keyword a description uses, with the type of the schema it is
    on, apart from the annotations."""
    return {
        (keyword, inner.get("type", ""))
        for inner in _schemas(schema)
        for keyword in inner
        if keyword not in _ANNOTATIONS and keyword != "$defs"
    }


def _without(schema: dict[str, Any], keyword: str, kind: str) -> dict[str, Any]:
    """A description with one keyword taken out of every schema of one type."""
    kept: dict[str, Any] = {}
    for key, part in schema.items():
        if key == keyword and schema.get("type", "") == kind:
            continue
        if key in ("properties", "$defs"):
            kept[key] = {
                name: _without(inner, keyword, kind) for name, inner in part.items()
            }
        elif key in ("items", "additionalProperties", "propertyNames"):
            kept[key] = _without(part, keyword, kind)
        elif key in ("anyOf", "allOf"):
            kept[key] = [_without(inner, keyword, kind) for inner in part]
        else:
            kept[key] = part
    return kept


@pytest.mark.spec("TRN-15", "CON-16")
@pytest.mark.parametrize(("keyword", "kind"), _KEYWORDS)
def test_the_check_enforces_every_keyword_the_description_writes(
    keyword: str, kind: str
) -> None:
    # Each keyword is enforced with JSON Schema's meaning, on each type of
    # schema that carries it. The check refuses a value that the reference
    # validator refuses for that keyword alone, since it passes once the
    # keyword is taken out of the schemas of that type, and admits the nearest
    # value that keeps the keyword.
    checker = _checker(_WORDED_SCHEMA)
    breaks, keeps = ({**_BASE, **value} for value in _KEYWORDS[keyword, kind])
    assert _admits(checker, _BASE)
    assert not _admits(checker, breaks)
    assert not _fits(_WORDED_SCHEMA, breaks)
    assert _fits(_without(_WORDED_SCHEMA, keyword, kind), breaks)
    assert _admits(checker, keeps)
    assert _fits(_WORDED_SCHEMA, keeps)


def test_the_keywords_tested_are_the_keywords_written() -> None:
    """Fails when a description uses a keyword, on some type of schema, that
    the table above does not break."""
    written = _keywords(_WORDED_SCHEMA)
    assert written == set(_KEYWORDS)
    for schema in (_REFERENCE_SCHEMA, _LEAVES_SCHEMA):
        assert _keywords(schema) <= written
    # A bound that rounding or truncating would leave as it is hides a check
    # that rounds: each number's bound here has a fraction, and no bound is 0.
    bounds = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
    for name in ("whole", "part", "open"):
        field = _WORDED_SCHEMA["$defs"]["Worded"]["properties"][name]
        for bound in (field[keyword] for keyword in bounds if keyword in field):
            # Beside the range of its form, which the writer always states.
            if abs(bound) < 2**53 - 1:
                assert bound != 0
                assert field["type"] == "integer" or bound != int(bound)


@pytest.mark.spec("TRN-15")
@pytest.mark.parametrize(
    ("value", "admitted"),
    [
        ({"whole": 4}, True),
        ({"whole": 4.0}, False),
        ({"whole": True}, False),
        ({"whole": "4"}, False),
        ({"part": 0}, True),
        ({"part": -1}, True),
        ({"part": True}, False),
        ({"part": -0.0}, True),
        ({"flag": 0}, False),
        ({"code": 1}, False),
        ({"code": "a\n"}, True),
        ({"span": "1.500s\n"}, False),
        ({"day": "2028-02-29"}, True),
        ({"day": "2027-02-29"}, False),
        ({"day": "2027-02-28\n"}, False),
        ({"when": "2027-02-28T23:59:59.999999+23:59"}, True),
        ({"when": "2027-02-29T00:00:00Z"}, False),
        ({"when": "2027-04-31T00:00:00Z"}, False),
        ({"when": "0001-01-01T00:00:00Z"}, True),
        ({"when": "0001-01-01T00:00:00+00:01"}, False),
        ({"when": "9999-12-31T23:59:59.999999Z"}, True),
        ({"when": "9999-12-31T23:59:59-00:01"}, False),
        ({"added_later": {"anything": [1.0, None]}}, True),
    ],
)
def test_the_check_adds_the_rules_the_description_cannot_state(
    value: dict[str, Any], admitted: bool
) -> None:
    # An integer written with a fraction, a day that does not exist and an
    # instant outside years 0001 to 9999 in UTC are refused (CON-40); a
    # pattern matches the whole string; a field the description does not
    # name is ignored (CON-6, CON-7).
    checker = _checker(_WORDED_SCHEMA)
    assert _admits(checker, {**_BASE, **value}) is admitted
    assert _fits(_WORDED_SCHEMA, {**_BASE, **value}) is admitted


def _described(field: object, **defs: object) -> dict[str, Any]:
    return {"R": {"type": "object", "properties": {"x": field}}, **defs}


_PATTERN = _WORDED_SCHEMA["$defs"]["Worded"]["properties"]["span"]["pattern"]
_DAY = _LEAVES_SCHEMA["$defs"]["Leaves"]["properties"]["day"]["anyOf"][0]
_AMOUNT = _WORDED_SCHEMA["$defs"]["Worded"]["properties"]["amount"]
_BOOLEAN = {"type": "boolean"}


@pytest.mark.spec("TRN-15", "CON-16")
@pytest.mark.parametrize(
    "field",
    [
        {"type": "string", "not": {}},
        {"oneOf": [_BOOLEAN]},
        {"const": 1},
        {},
        True,
        {"type": ["string", "null"]},
        {"type": "null"},
        {"type": "boolean", "title": "Flag"},
        {"type": "string", "format": "uri"},
        {"type": "string", "format": "date"},
        {**_DAY, "format": "date-time"},
        {"type": "string", "pattern": "^a$"},
        {"type": "string", "pattern": 1},
        {"type": "string", "contentEncoding": "base64url"},
        {"type": "string", "pattern": _PATTERN, "contentEncoding": "base64url"},
        {"type": "string", "enum": ["a"], "minLength": 1},
        {"type": "string", "enum": []},
        {"type": "string", "enum": ["a", 1]},
        {"type": "string", "allOf": [{"minLength": 1}]},
        {**_AMOUNT, "allOf": [{**_AMOUNT["allOf"][0], "maxLength": 1}]},
        {"type": "string", "maxLength": True},
        {"type": "string", "maxLength": "3"},
        {"type": "string", "minLength": -1},
        {"type": "string", "maxLength": 2.0},
        {"type": "integer", "multipleOf": True},
        {"type": "integer", "multipleOf": "3"},
        {"type": "integer", "multipleOf": 0},
        {"type": "integer", "minimum": "1"},
        {"type": "integer", "maximum": True},
        {"type": "integer", "minimum": 1.5},
        {"type": "number", "maximum": float("inf")},
        {"type": "number", "maximum": 2**53 + 1},
        {"type": "number", "exclusiveMinimum": True},
        {"type": "number", "multipleOf": 0.5},
        {"type": "array"},
        {"type": "array", "items": _BOOLEAN, "uniqueItems": True},
        {"type": "array", "items": _BOOLEAN, "prefixItems": [_BOOLEAN]},
        {"type": "array", "items": _BOOLEAN, "maxItems": True},
        {"type": "object"},
        {"type": "object", "additionalProperties": False},
        {"type": "object", "additionalProperties": _BOOLEAN, "patternProperties": {}},
        {
            "type": "object",
            "additionalProperties": _BOOLEAN,
            "propertyNames": {"type": "string"},
        },
        {"type": "object", "properties": {"a": _BOOLEAN}, "required": ["b"]},
        {"type": "object", "properties": {"a": _BOOLEAN}, "required": "a"},
        {
            "type": "object",
            "properties": {"a": _BOOLEAN},
            "additionalProperties": _BOOLEAN,
        },
        {"$ref": "#/definitions/R"},
        {"$ref": "R"},
        {"anyOf": [_BOOLEAN, {"type": "string"}]},
        {"anyOf": [_BOOLEAN, {"type": "null"}], "type": "boolean"},
        {"$ref": "#/$defs/Missing"},
        {"$ref": "#/$defs/R", "type": "object"},
        {"anyOf": [_BOOLEAN]},
        {"anyOf": [{"type": "null"}, _BOOLEAN]},
        {"anyOf": [_BOOLEAN, {"type": "null"}, {"type": "string"}]},
    ],
    ids=repr,
)
def test_the_check_refuses_a_description_it_does_not_read(field: object) -> None:
    # Fail closed: a keyword the check does not enforce, or a value of one
    # that pydantic-core would read loosely, refuses the whole description.
    with pytest.raises(TypeError, match="the description of R is not one the"):
        description_checker(_described(field), "R")


@pytest.mark.spec("TRN-15", "CON-16")
def test_the_nearest_descriptions_are_read() -> None:
    for field in (
        _DAY,
        {"type": "string", "enum": ["a"]},
        {"type": "string", "maxLength": 3},
        {"type": "integer", "multipleOf": 3, "minimum": 1.0},
        {"type": "array", "items": _BOOLEAN, "maxItems": 0},
        {"type": "object", "additionalProperties": _BOOLEAN},
        {"type": "object", "properties": {"a": _BOOLEAN}, "required": ["a"]},
        {"$ref": "#/$defs/R"},
        {"anyOf": [_BOOLEAN, {"type": "null"}], "default": None},
    ):
        checker = description_checker(_described(field), "R")
        assert _admits(checker, {})


def test_a_description_the_check_does_not_read_refuses_the_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The check is compiled when the contract is declared (CONTRIBUTING K2),
    # so a description it cannot read never reaches a call.
    def unread(defs: object, name: str) -> object:
        raise TypeError(f"the description of {name} is not one the check reads")

    monkeypatch.setattr("neolith._crossing.description_checker", unread)

    class Fresh(BaseModel):
        n: int = 0

    with pytest.raises(
        ContractDefinitionError,
        match=r"unread\.v1, action 'send': the description of Fresh is not one",
    ):
        _contract_for(Fresh, identity="unread.v1")


def _every_field(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with every record field required, so that a value drawn
    from it holds every field for a change to reach."""
    defs = {
        name: {**described, "required": sorted(described["properties"])}
        if "properties" in described
        else described
        for name, described in schema["$defs"].items()
    }
    return {**schema, "$defs": defs}


def _near(value: object) -> list[object]:
    """Values next to a valid one, where a check is most likely to slip."""
    if type(value) is str:
        near: list[object] = [value + "\n", value[:-1], value + "x", "x" + value, ""]
        near += [value.upper(), value + value, 1]
        if value[:1].isdigit():
            # Past a decimal's 34 digits, onto a day that does not exist, and
            # out of years 0001 to 9999 in UTC.
            near += ["9" * 35, "9" * 34 + ".5", value[:8] + "31" + value[10:]]
            near += [value[:5] + "02-29" + value[10:], value[:5] + "02-30" + value[10:]]
            near += ["0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01"]
        return near
    if type(value) is bool:
        return [int(value), not value, None, str(value).lower()]
    if type(value) is int:
        return [value + 1, value - 1, float(value), -value, 2**53, True, str(value)]
    if type(value) is float:
        near = [-value, value + 0.5, value - 0.5, int(value), str(value), True]
        near += [math.nextafter(value, math.inf), math.nextafter(value, -math.inf)]
        return [n for n in near if type(n) is not float or math.isfinite(n)]
    if type(value) is list:
        return [[], [*value, *value[:1]], value[1:], [None], {}, [*value, *value]]
    if type(value) is dict:
        first = dict(list(value.items())[:1])
        return [
            {},
            {**value, "later": 1},
            {**value, "": 1},
            {**value, "a-long-key": 1},
            dict(list(value.items())[1:]),
            {**value, **{f"{key}{key}": part for key, part in first.items()}},
            [],
        ]
    return [0, "", [], {}, False]


def _paths(value: object, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Every part of a value, by its path, the value itself first."""
    found = [path]
    if type(value) is dict:
        for key, part in value.items():
            found += _paths(part, (*path, key))
    elif type(value) is list:
        for index, part in enumerate(value):
            found += _paths(part, (*path, index))
    return found


def _part(value: Any, path: tuple[Any, ...]) -> Any:  # noqa: ANN401
    for step in path:
        value = value[step]
    return value


def _with(value: Any, path: tuple[Any, ...], new: object) -> Any:  # noqa: ANN401
    if not path:
        return new
    if type(value) is dict:
        return {**value, path[0]: _with(value[path[0]], path[1:], new)}
    items = list(value)
    items[path[0]] = _with(items[path[0]], path[1:], new)
    return items


_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**60), 2**60)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=6),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=4), inner, max_size=3)
    ),
    max_leaves=8,
)


def _agrees(schema: dict[str, Any], data: st.DataObject) -> None:
    """For a value drawn from the description, and for each value made from
    it by replacing one part, at any depth, with a value next to that part or
    with any JSON value, the check admits what the reference validator admits.

    A number is read as the double a reader holds, which differs from the
    reference only where a bound lies between an integer and its double; the
    bounds drawn here are far below 2**53.
    """
    assert _keywords(schema) <= set(_KEYWORDS)
    checker = _checker(schema)
    reference = _Reference(schema, format_checker=_FORMATS)
    valid = data.draw(admitted_by(_every_field(schema)))
    paths = _paths(valid)
    chosen = data.draw(
        st.lists(st.sampled_from(paths), min_size=1, max_size=8, unique=True)
    )
    for path in chosen:
        for near in [*_near(_part(valid, path)), data.draw(_JSON)]:
            value = _with(valid, path, near)
            assert _admits(checker, value) == reference.is_valid(value), (path, near)


@pytest.mark.spec("TRN-15", "CON-16")
@pytest.mark.parametrize(
    "schema", [_REFERENCE_SCHEMA, _WORDED_SCHEMA], ids=["reference", "every-keyword"]
)
@settings(parent=_HYPOTHESIS, max_examples=40)
@given(data=st.data())
def test_the_check_agrees_with_a_json_schema_validator(
    schema: dict[str, Any], data: st.DataObject
) -> None:
    _agrees(schema, data)


@pytest.mark.spec("TRN-15", "CON-16")
@settings(parent=_HYPOTHESIS, max_examples=120)
@given(data=st.data())
def test_the_check_agrees_with_a_json_schema_validator_on_generated_contracts(
    data: st.DataObject,
) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    try:
        _, schema = _contract_for(record)
    except ContractDefinitionError:
        return
    _agrees(schema, data)
