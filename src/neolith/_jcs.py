"""The JSON Canonicalization Scheme, RFC 8785.

JCS writes a JSON value in one spelling: members sorted by the UTF-16 code
units of their names, no whitespace, strings escaped as ECMAScript's
``JSON.stringify`` escapes them, and numbers written as ECMAScript's
``Number.prototype.toString`` writes a double. ADR 0017 uses it for the
principal's sent form and for the idempotency key of a child call.

Only I-JSON values are canonical (RFC 8785, section 3.1): text holds no lone
surrogate, and a number is a finite double, so an integer must be one a
double holds exactly. Anything else raises ``ValueError``, whose message never
shows the value.
"""

import json
import math
import re
from decimal import Decimal
from typing import cast

__all__ = ["canonical", "canonical_text"]

# JSON.stringify's short escapes; other control characters take \u00XX.
_SHORT = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class _Unusual(Exception):
    """The text holds a number the json module does not write as JCS does."""


def _quick_number(literal: str) -> int | float:
    """A number written with a fraction or an exponent, as a value the json
    module writes the way ECMAScript does, or ``_Unusual``."""
    value = float(literal)
    if -(2**53) < value < 2**53:
        if value == int(value):
            return int(value)  # ECMAScript writes a whole double as its digits
        if abs(value) >= 1e-4:
            return value  # both write the shortest digits, with no exponent
    raise _Unusual


# A run of 16 digits may be an integer beyond 2^53, which JCS writes as a
# double. Found by turning every digit into one byte and looking for a run.
_DIGITS = bytes(0x30 if 0x30 <= byte <= 0x39 else 0x20 for byte in range(256))
_RUN = b"0" * 16
# A four-byte UTF-8 sequence is a character beyond U+FFFF, which JCS sorts by
# its UTF-16 code units and the json module by its code point.
_BEYOND = re.compile(rb"[\xf0-\xff]")


def _members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """An object's members, refusing a name that occurs twice (RFC 7493)."""
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("a member name occurs twice in one object")
    return members


def _quick(text: bytes | str) -> bytes | None:
    """The canonical form as the json module writes it, or ``None`` when the
    text holds something it writes otherwise, or is not I-JSON."""
    try:
        value = json.loads(text, parse_float=_quick_number, object_pairs_hook=_members)
        written = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (_Unusual, ValueError, OverflowError, RecursionError):
        return None  # UnicodeEncodeError, for a lone surrogate, is a ValueError
    if _RUN in written.translate(_DIGITS) or (
        not written.isascii() and _BEYOND.search(written) is not None
    ):
        return None
    return written


def _double(literal: str) -> int | float:
    """An integer literal as the double it denotes, kept an int when exact."""
    value = int(literal)
    return value if -(2**53) <= value <= 2**53 else float(literal)


def canonical_text(text: bytes | str) -> bytes:
    """The JCS serialization of the value a JSON text holds, as UTF-8.

    Every number is read as the double it denotes, as RFC 8785 reads JSON, so
    an integer written beyond 2^53 is rounded to one, and the result is the
    same for any spelling of a value. A text that holds only what the json
    module writes exactly as JCS does is written by it, which is several
    times quicker than ``canonical``; any other text takes the long way.

    Raises ``ValueError`` for a text that is not I-JSON: one that is not JSON,
    or holds a member name twice in one object, a lone surrogate or a number
    beyond a double, and for one nested deeper than the json module reads.
    """
    quick = _quick(text)
    if quick is not None:
        return quick
    try:
        value = json.loads(text, parse_int=_double, object_pairs_hook=_members)
    except RecursionError:
        value = _TOO_DEEP
    if value is _TOO_DEEP:
        # Raised outside the handler, so the deep traceback is not chained.
        raise ValueError("the text is nested deeper than the JSON reader goes")
    return canonical(value)


_TOO_DEEP = object()


def canonical(value: object) -> bytes:
    """The JCS serialization of a JSON value, as UTF-8.

    ``value`` is made of ``dict`` with ``str`` keys, ``list``, ``str``,
    ``int``, ``float``, ``bool`` and ``None``, as the json module reads them.
    """
    parts: list[str] = []
    # A stack of pending items: a value to write, or text to emit as is.
    pending: list[tuple[bool, object]] = [(False, value)]
    while pending:
        literal, item = pending.pop()
        if literal:
            parts.append(cast(str, item))
            continue
        kind = type(item)
        if item is None:
            parts.append("null")
        elif kind is bool:
            parts.append("true" if item else "false")
        elif kind is str:
            parts.append(_string(cast(str, item)))
        elif kind is int or kind is float:
            parts.append(_number(cast(int | float, item)))
        elif kind is list:
            items = cast(list[object], item)
            later: list[tuple[bool, object]] = [(True, "[")]
            for index, child in enumerate(items):
                if index:
                    later.append((True, ","))
                later.append((False, child))
            later.append((True, "]"))
            pending.extend(reversed(later))
        elif kind is dict:
            members = cast(dict[object, object], item)
            for key in members:
                if type(key) is not str:
                    raise ValueError(f"a member name is {type(key).__name__}")
            ordered = sorted(
                cast(dict[str, object], members),
                key=lambda name: name.encode("utf-16-be", "surrogatepass"),
            )
            later = [(True, "{")]
            for index, name in enumerate(ordered):
                if index:
                    later.append((True, ","))
                later.append((True, _string(name) + ":"))
                later.append((False, members[name]))
            later.append((True, "}"))
            pending.extend(reversed(later))
        else:
            raise ValueError(f"{kind.__name__} is not a JSON value")
    return "".join(parts).encode("utf-8")


def _string(text: str) -> str:
    out = ['"']
    for character in text:
        code = ord(character)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError("text holds a lone surrogate, which I-JSON excludes")
        if character in _SHORT:
            out.append(_SHORT[character])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(character)
    out.append('"')
    return "".join(out)


def _number(number: int | float) -> str:
    """A number as ECMAScript's Number.prototype.toString writes the double."""
    if type(number) is int:
        as_float = float(number) if abs(number) <= 2**1023 else math.inf
        if as_float != number:
            raise ValueError("an integer is not one a double holds exactly")
        value = as_float
    else:
        value = cast(float, number)
    if not math.isfinite(value):
        raise ValueError("a number is not finite")
    if value == 0:
        return "0"  # -0 too
    if value < 0:
        return "-" + _number(-value)
    # repr gives the shortest digits that read back as the same double, as
    # ECMAScript's algorithm does (ECMA-262, Number::toString, step 5).
    _, digit_tuple, exponent = Decimal(repr(value)).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    exponent = cast(int, exponent) + len(digit_tuple) - len(digits)
    k = len(digits)
    n = k + exponent  # the value is 0.digits times 10^n
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    power = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{mantissa}e{'+' if power > 0 else '-'}{abs(power)}"
