"""The depth scan every JSON text from a network passes before the json
module reads it: bodies at 256 levels (spec 01, CON-52), a token's header and
claims at 64 (spec 03, HTTP-7); issue #125.

The bound is counted by a scan that does not recurse, so these tests hold it
to two references: the depth of the value a JSON text holds, and a plain
character-by-character count on any text at all, JSON or not.
"""

import json
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neolith._nesting import DEPTH, deeper_than

# Deterministic, as every test is (CONTRIBUTING T3).
_SETTINGS = settings(derandomize=True, database=None, max_examples=300, deadline=None)

_STRINGS = st.text(
    alphabet=st.characters(exclude_categories=["Cs"]) | st.sampled_from('[]{}"\\'),
    max_size=12,
)
_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | _STRINGS,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(_STRINGS, inner, max_size=3)
    ),
    max_leaves=40,
)


def _depth(value: object) -> int:
    """How many arrays and objects a value nests, counted without recursion."""
    deepest = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (list, dict)):
            depth += 1
            deepest = max(deepest, depth)
            items = item.values() if isinstance(item, dict) else item
            pending.extend((inner, depth) for inner in items)
    return deepest


def _counted(text: bytes) -> int:
    """The deepest an opener takes the count, outside strings, one byte at a
    time: a string ends at its closing quote or at the end of the text, and a
    backslash in it escapes the next byte.
    """
    depth = deepest = 0
    quoted = escaped = False
    for byte in text:
        char = chr(byte)
        if escaped:
            escaped = False
        elif quoted:
            if char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


@pytest.mark.spec("CON-52")
@_SETTINGS
@given(_VALUES, st.booleans(), st.sampled_from([None, 1]))
def test_a_json_text_is_counted_as_deep_as_its_value(
    value: object, ascii_only: bool, indent: int | None
) -> None:
    text = json.dumps(value, ensure_ascii=ascii_only, indent=indent).encode()
    depth = _depth(value)
    assert not deeper_than(text, depth)
    assert depth == 0 or deeper_than(text, depth - 1)


@pytest.mark.spec("CON-52")
@_SETTINGS
@given(
    st.binary(max_size=60)
    | st.lists(
        st.sampled_from([b"[", b"]", b"{", b"}", b'"', b"\\", b"a", b"\xe2"])
    ).map(b"".join),
    st.integers(min_value=0, max_value=8),
)
def test_any_text_is_counted_as_a_plain_scan_counts_it(text: bytes, limit: int) -> None:
    assert deeper_than(text, limit) == (_counted(text) > limit)


@pytest.mark.spec("CON-52")
@pytest.mark.parametrize(
    ("text", "deeper"),
    [
        (b"[" * DEPTH + b"]" * DEPTH, False),
        (b"[" * (DEPTH + 1) + b"]" * (DEPTH + 1), True),
        (b"{" * (DEPTH + 1), True),
        # Brackets inside strings, escaped quotes included, do not count.
        (b'["' + b"[" * 1000 + b'\\"' + b"{" * 1000 + b'"]', False),
        (b'["\\\\"' + b"[" * (DEPTH + 1), True),
        # An unterminated string runs to the end of the text.
        (b'"' + b"[" * 1000, False),
        # Openers spread across siblings are not nesting.
        (b"[" + b"[]," * 1000 + b"[]]", False),
    ],
)
def test_the_bound_is_256_levels(text: bytes, deeper: bool) -> None:
    assert deeper_than(text) is deeper


def test_a_closer_with_no_opener_ends_the_reading_before_it_nests() -> None:
    # The count drops below zero, so the openers after it are counted short;
    # the json module refuses the text at its first byte, before any of them.
    text = b"]" * 1000 + b"[" * 1000
    assert not deeper_than(text)
    with pytest.raises(json.JSONDecodeError) as refused:
        json.loads(text)
    assert refused.value.pos == 0


_SETS_A_STACK_SIZE = re.compile(r"\bstack_size\s*\(")


def test_only_the_small_stack_harness_sets_a_thread_stack_size() -> None:
    # A 1 MB-stack check run in the test process ends the whole run on a
    # regression, so every such check goes through tests/small_stack.py.
    tests = Path(__file__).parent
    found = [
        f"{path.name}:{number}"
        for path in sorted(tests.rglob("*.py"))
        if path.name != "small_stack.py"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if _SETS_A_STACK_SIZE.search(line)
    ]
    assert found == []
