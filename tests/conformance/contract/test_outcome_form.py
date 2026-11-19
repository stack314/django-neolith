"""Contract part: two outcomes are the same by their portable form and their
types at every depth, not by ``==`` (ADR 0032 Decision 4, #152).

``==`` blurs what a portable form keeps (spec 01): ``Decimal("1.50")`` equals
``Decimal("1.5")`` though trailing zeros are kept, a ``StrEnum`` member equals
its text, ``1`` equals ``True`` and ``1.0``, and ``0.0`` equals ``-0.0``. A
transport that gave back any of these in place of the other must fail the
parity check, at any depth: in a reply, in a record, in a list or a map, and
in an error's detail.
"""

import enum
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from neolith import ContractError
from tests.conformance.contract.conftest import Outcome, form, same


class Colour(enum.StrEnum):
    RED = "red"


class Number(enum.IntEnum):
    ONE = 1


class Holder(BaseModel):
    value: Any


class Outer(BaseModel):
    inner: Holder
    items: list[Any] = []
    table: dict[str, Any] = {}


def _surround(value: object) -> dict[str, object]:
    """``value`` at each depth a reply or a detail holds it."""
    return {
        "bare": value,
        "record": Holder.model_construct(value=value),
        "nested record": Outer.model_construct(
            inner=Holder.model_construct(value=value)
        ),
        "in a list": [value],
        "in a map": {"k": value},
        "in a tuple": (value,),
        "in a list in a record": Outer.model_construct(
            inner=Holder.model_construct(value=None), items=[[value]]
        ),
        "in a map in a record": Outer.model_construct(
            inner=Holder.model_construct(value=None), table={"k": {"j": value}}
        ),
    }


def _outcomes(value: object) -> dict[str, Outcome]:
    """``value`` at each depth, as a reply and, where a detail may hold it, as
    the detail of an error.
    """
    where = _surround(value)
    return {
        **{f"reply {name}": Outcome(False, held) for name, held in where.items()},
        **{
            f"detail {name}": Outcome(True, ContractError("gone", "m", held))
            for name, held in where.items()
            # A typed detail is a record; an untyped one holds JSON only,
            # which has its own test.
            if isinstance(held, BaseModel)
        },
    }


# Pairs that compare equal and are not the same value on the wire.
BLURRED = {
    "decimal scale": (Decimal("1.50"), Decimal("1.5")),
    "decimal scale of zero": (Decimal("0.00"), Decimal("0")),
    "decimal scale of an integer": (Decimal("100"), Decimal("100.0")),
    "enum member and its text": (Colour.RED, "red"),
    "int enum member and its number": (Number.ONE, 1),
    "bool and int": (True, 1),
    "int and number": (1, 1.0),
    "number and decimal": (Decimal("1"), 1.0),
}


@pytest.mark.parametrize("pair", BLURRED)
@pytest.mark.parametrize("where", list(_outcomes(0)))
def test_outcomes_that_equal_but_differ_in_form_are_not_the_same(
    pair: str, where: str
) -> None:
    one, other = BLURRED[pair]
    assert one == other, "the pair must be one that == cannot tell apart"
    first, second = _outcomes(one)[where], _outcomes(other)[where]
    assert not same(first, second)
    assert not same(second, first)


@pytest.mark.parametrize("where", list(_outcomes(0)))
def test_a_list_is_not_a_tuple_at_any_depth(where: str) -> None:
    assert not same(_outcomes([1])[where], _outcomes((1,))[where])


@pytest.mark.parametrize("where", list(_outcomes(0)))
def test_outcomes_of_one_form_are_the_same_at_every_depth(where: str) -> None:
    # The nearest legitimate shape: the same data, written alike.
    for value in (Decimal("1.50"), Colour.RED, True, 1, 1.0, "red", None):
        assert same(_outcomes(value)[where], _outcomes(value)[where])


@pytest.mark.parametrize(
    ("one", "other"),
    [
        (Decimal("1E+2"), Decimal("100")),
        (Decimal("1.50"), Decimal("1.50")),
        (Decimal("-0.00"), Decimal("0.00")),
        (0.0, -0.0),
    ],
)
def test_a_value_is_judged_by_its_wire_text(one: object, other: object) -> None:
    # 1E+2 is written 100 and -0.00 as 0.00 (CON-37), and -0 as 0 (CON-35),
    # so each pair is one value on the wire.
    assert form(one) == form(other)


def test_a_map_is_the_same_whatever_the_order_of_its_members() -> None:
    assert form({"a": 1, "b": 2}) == form({"b": 2, "a": 1})
    assert form({"a": 1}) != form({"a": 1.0})


def test_a_timestamp_is_judged_by_its_instant() -> None:
    instant = datetime(2027, 1, 1, 12, tzinfo=UTC)
    assert form(instant) == form(instant.astimezone(timezone(timedelta(hours=-5))))
    assert form(instant) != form(instant + timedelta(microseconds=1))


@pytest.mark.parametrize(
    ("one", "other"),
    [(1, 1.0), (1, True), ({"a": 1}, {"a": 1.0}), ([1], [True])],
)
def test_an_untyped_detail_is_judged_by_the_types_of_its_json(
    one: object, other: object
) -> None:
    def refused(detail: object) -> Outcome:
        error = ContractError("gone", "m", {"x": detail})
        return Outcome(True, error)

    assert not same(refused(one), refused(other))
    assert same(refused(one), refused(one))
