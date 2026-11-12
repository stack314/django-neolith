"""The JSON Canonicalization Scheme (RFC 8785), against Node's output.

``tests/data/jcs_vectors.json`` holds what Node.js writes for doubles given by
their bits and for whole values; ``tests/data/jcs_vectors.js`` wrote it. The
principal's sent form and the idempotency key of a nested call are built on
this (ADR 0017), so two implementations agree only if it is exact.
"""

import json
import math
import random
import struct
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neolith._jcs import _quick, canonical, canonical_text

_VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "data" / "jcs_vectors.json").read_text(encoding="utf-8")
)


def test_a_double_is_written_as_ecmascript_writes_it() -> None:
    numbers = _VECTORS["numbers"]
    assert len(numbers) > 600
    for bits, expected in numbers:
        (value,) = struct.unpack(">d", bytes.fromhex(bits))
        assert canonical(value) == expected.encode(), bits


def test_the_samples_of_rfc_8785_are_among_the_doubles() -> None:
    # RFC 8785, Appendix B.
    written = dict(_VECTORS["numbers"])
    assert written["44b52d02c7e14af6"] == "1e+23"
    assert written["3eb0c6f7a0b5ed8d"] == "0.000001"
    assert written["7fefffffffffffff"] == "1.7976931348623157e+308"
    assert written["8000000000000000"] == "0"
    assert written["0000000000000001"] == "5e-324"
    assert written["41b3de4355555555"] == "333333333.3333333"


_VALUES: list[list[str]] = _VECTORS["values"]


@pytest.mark.parametrize("pair", _VALUES, ids=range(len(_VALUES)))
def test_a_value_is_written_with_members_sorted_by_utf16_units(
    pair: list[str],
) -> None:
    given_text, expected = pair
    assert canonical(json.loads(given_text)) == expected.encode("utf-8")


def test_the_example_of_rfc_8785_is_written_as_the_rfc_writes_it() -> None:
    # RFC 8785, section 3.2.3: sorting is by UTF-16 code units, so the
    # supplementary-plane name comes before U+FB33.
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    assert list(json.loads(canonical(value))) == [
        "\r",
        "1",
        "\u0080",
        "ö",
        "€",
        "\U0001f600",
        "דּ",
    ]


def test_an_integer_is_written_as_the_double_it_is() -> None:
    assert canonical([0, -0, 1, -1, 2**53 - 1, -(2**53 - 1), 10**21, 2**70]) == (
        b"[0,0,1,-1,9007199254740991,-9007199254740991,1e+21,1.1805916207174113e+21]"
    )
    assert canonical([1.0, -0.0, 1e21, 123456789012345680000.0]) == (
        b"[1,0,1e+21,123456789012345680000]"
    )


@pytest.mark.parametrize(
    "value",
    [
        "\ud800",
        {"\udfff": 1},
        ["a", "b\ud83d"],
        math.nan,
        math.inf,
        -math.inf,
        2**53 + 1,
        10**400,
        {1: "a"},
        {None: "a"},
        (1, 2),
        b"bytes",
        {"a": {1, 2}},
        object(),
    ],
    ids=repr,
)
def test_a_value_that_is_not_i_json_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        canonical(value)


def test_a_subclass_of_a_json_type_is_refused() -> None:
    class Text(str):
        __slots__ = ()

    class Flag(int):
        pass

    for value in (Text("a"), Flag(1), {Text("a"): 1}):
        with pytest.raises(ValueError, match=r"not a JSON value|member name"):
            canonical(value)


def test_a_value_nested_deeper_than_the_stack_is_written() -> None:
    depth = 50_000
    value: list[Any] = []
    for _ in range(depth):
        value = [value]
    assert canonical(value) == b"[" * (depth + 1) + b"]" * (depth + 1)


_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53 - 1), 2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(st.characters(exclude_categories=["Cs"])),
    lambda inner: (
        st.lists(inner, max_size=4)
        | st.dictionaries(
            st.text(st.characters(exclude_categories=["Cs"]), max_size=4),
            inner,
            max_size=4,
        )
    ),
    max_leaves=12,
)


@given(_JSON, st.randoms(use_true_random=False))
def test_canonical_text_reads_back_and_does_not_depend_on_member_order(
    value: object, shuffle: random.Random
) -> None:
    def reordered(item: object) -> object:
        if type(item) is list:
            return [reordered(child) for child in item]
        if type(item) is dict:
            names = list(item)
            shuffle.shuffle(names)
            return {name: reordered(item[name]) for name in names}
        return item

    text = canonical(value)
    # Numbers are read as the doubles JCS writes, so 1.0, written "1", is 1.
    read = json.loads(text, parse_int=float)
    assert read == value
    assert canonical(read) == text
    assert canonical(reordered(value)) == text
    # One spelling: no whitespace, and text as UTF-8, never escaped as ASCII.
    assert b" " not in canonical(_without_text(value))
    assert text.decode("utf-8").isascii() or b"\\u" not in canonical(
        _without_controls(value)
    )


def _without_text(value: object) -> object:
    if type(value) is list:
        return [_without_text(child) for child in value]
    if type(value) is dict:
        return {str(index): _without_text(v) for index, v in enumerate(value.values())}
    return 0 if type(value) is str else value


def _without_controls(value: object) -> object:
    if type(value) is list:
        return [_without_controls(child) for child in value]
    if type(value) is dict:
        return {
            _without_controls(name): _without_controls(v) for name, v in value.items()
        }
    if type(value) is str:
        return "".join(c for c in value if ord(c) >= 0x20 and c != "\\")
    return value


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_a_double_reads_back_as_itself_in_its_shortest_digits(value: float) -> None:
    text = canonical(value).decode()
    assert float(text) == value
    # No shorter run of digits reads back as the same double.
    digits = text.lstrip("-").split("e")[0].replace(".", "").strip("0")
    assert len(digits) <= len(
        repr(abs(value)).split("e")[0].replace(".", "").strip("0")
    )


# The quick way for a JSON text gives the same bytes as the long way.

_NUMBERS = (
    st.integers(-(2**53) - 3, 2**53 + 3)
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.sampled_from(
        [1e-4, 9.999e-5, 1e-7, 1e15, 1e16, 9999999999999998.0, 1e21, 1e22, -0.0]
        + [float(2**53 + step) for step in (-2, -1, 0, 2, 4)]
        + [0.1234567890123456, 123456789012345.6, 5e-324, 1.7976931348623157e308]
    )
    | st.integers(-(10**6), 10**6).map(float)
    | st.integers(-(10**6), 10**6).map(lambda n: n / 8)
)
_STRINGS = st.text(max_size=6) | st.sampled_from(
    ["\U0001f600", "\uffff", "\ue000", "\U00010000", "1234567890123456", "\x00\x1f\x7f"]
)
_TEXTS = st.recursive(
    st.none() | st.booleans() | _NUMBERS | _STRINGS,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(_STRINGS, inner, max_size=4)
    ),
    max_leaves=12,
)


def _the_long_way(text: str) -> bytes | type[Exception]:
    """The canonical form with every number read as a double (RFC 8785)."""
    try:
        return canonical(json.loads(text, parse_int=float))
    except ValueError:
        return ValueError


@given(_TEXTS, st.booleans())
def test_the_quick_way_writes_what_the_long_way_writes(
    value: object, ascii_only: bool
) -> None:
    text = json.dumps(value, ensure_ascii=ascii_only)
    expected = _the_long_way(text)
    if expected is ValueError:
        # A lone surrogate, or a number beyond a double: refused both ways.
        assert _quick(text) is None
        with pytest.raises(ValueError, match=r"."):
            canonical_text(text)
        return
    assert canonical_text(text) == expected
    assert canonical_text(text.encode("utf-8", "surrogatepass")) == expected
    quick = _quick(text)
    assert quick is None or quick == expected
    # Canonical text is its own canonical form.
    assert isinstance(expected, bytes)
    assert canonical_text(expected) == expected


def test_the_vectors_are_written_the_same_from_their_text() -> None:
    taken = 0
    for bits, expected in _VECTORS["numbers"]:
        (value,) = struct.unpack(">d", bytes.fromhex(bits))
        assert canonical_text(repr(value)) == expected.encode()
        assert canonical_text(expected) == expected.encode()
        taken += _quick(repr(value)) is not None
    for given_text, expected in _VALUES:
        assert canonical_text(given_text) == expected.encode("utf-8")
        assert canonical_text(expected) == expected.encode("utf-8")
    # Both ways are exercised: some doubles are written the quick way.
    assert 0 < taken < len(_VECTORS["numbers"])


@pytest.mark.parametrize(
    "text",
    [
        "NaN",
        "[Infinity]",
        '{"a":-Infinity}',
        "1e400",
        "1" + "0" * 400,
        "1" * 5000,
        '"\\ud800"',
        '{"\\udfff":1}',
        "{",
        "",
        "[1,]",
        "[" * 100_000,
    ],
)
def test_a_text_that_is_not_i_json_is_refused_either_way(text: str) -> None:
    assert _quick(text) is None
    with pytest.raises((ValueError, RecursionError)):
        canonical_text(text)


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ('{"b":1.0,"a":-0.0}', b'{"a":0,"b":1}'),
        ("[0.5,100.0,1e2,1.5e3,0.0001]", b"[0.5,100,100,1500,0.0001]"),
        ('{"a":"\\u00e9\\u0000"}', '{"a":"\u00e9\\u0000"}'.encode()),
        (' { "a" : [ ] } ', b'{"a":[]}'),
        ('{"a":1,"a":2}', b'{"a":2}'),
    ],
)
def test_a_plain_text_is_written_the_quick_way(text: str, written: bytes) -> None:
    assert _quick(text) == written
    assert canonical(json.loads(text)) == written


@pytest.mark.parametrize(
    "text",
    [
        "1e-7",
        "0.00009",
        "1e21",
        "9007199254740992.0",
        "9007199254740992",
        "9007199254740993",
        "295147905179352830000",
        "1234567890123456",
        '"1234567890123456"',
        "0.1234567890123456",
        '{"\U0001f600":1,"\ufb33":2}',
        '["\U00010000"]',
    ],
)
def test_an_unusual_text_takes_the_long_way(text: str) -> None:
    assert _quick(text) is None
    assert canonical_text(text) == canonical(json.loads(text, parse_int=float))
