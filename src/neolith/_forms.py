"""Duration, Decimal128 and Binary: portable forms Pydantic writes otherwise.

Spec 01 fixes how each portable form is written in JSON (ADR 0019). Pydantic
writes three of them otherwise, and no setting of a record changes that: a
``timedelta`` as ISO 8601 text (``"PT1.5S"``) or a number, a ``Decimal`` as
Python's ``str`` (``"1E-7"``, ``"1E+2"``, ``"-0"``), and ``bytes`` as UTF-8
text, or as base64 that is read in either alphabet, padded or not. So each of
these forms is an ``Annotated`` type whose marker brings its own validator and
serializer. Values are the plain Python types.

- In JSON, each reads only its form, matched over the whole string, and
  writes it exactly.
- From Python, Duration and Decimal128 take a value of their type or a string
  in their form; Binary takes bytes only, since plain bytes read a string as
  its UTF-8 bytes.
- Its JSON Schema states its form's pattern, in validation and serialization
  modes alike, so Pydantic's own schema agrees with the wire.

The contract checker admits these markers' schemas by identity and refuses
the plain types, naming the type to use.
"""

import base64
import binascii
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, Rounded
from typing import Annotated, Any

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

from neolith import _naming
from neolith._frozen import once

# Each pattern is ASCII-only and anchored with ^...$, and matches the same
# strings under ECMA-262, Rust regex and Python's re.fullmatch (spec 01).
# Bytes are built from two parts, which the reader also checks one by one: a
# base64url character, and the last 4 characters when they hold padding.
_BASE64URL_CHAR = r"[A-Za-z0-9_\-]"
_BASE64URL_PADDED = (
    rf"{_BASE64URL_CHAR}[AQgw]==|{_BASE64URL_CHAR}{{2}}[AEIMQUYcgkosw048]="
)
BYTES_PATTERN = rf"^({_BASE64URL_CHAR}{{4}})*({_BASE64URL_PADDED})?$"
DECIMAL_PATTERN = r"^(0(\.[0-9]+)?|-?[1-9][0-9]*(\.[0-9]+)?|-?0\.0*[1-9][0-9]*)$"
DECIMAL_DIGITS_PATTERN = r"^-?[0-9](\.?[0-9]){0,33}$"
DURATION_PATTERN = (
    r"^(0|-?(0|[1-9][0-9]{0,10})\.([0-9]{3})?(00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})"
    r"|-?[1-9][0-9]{0,10})s$"
)

# A duration's magnitude is below 10**11 seconds, which its pattern states.
_DURATION_LIMIT = timedelta(seconds=10**11)
_MICROSECOND = timedelta(microseconds=1)
# A decimal has at most 34 digits, which its second pattern states.
_DECIMAL_DIGITS = 34
# Rounding to 34 digits, which flags Rounded when a value has more (#45).
_DECIMAL_ROUNDING = Context(
    prec=_DECIMAL_DIGITS, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[], flags=[]
)


# Python's re holds state for each repetition of BYTES_PATTERN's group, about
# 60 times the decoded size (#45). So bytes are checked against the same rules
# in two parts, neither holding any: every character but the last four is in
# the alphabet, and the last four end the text as the pattern's tail allows.
_BASE64URL = re.compile(f"{_BASE64URL_CHAR}*")
_BASE64URL_END = re.compile(f"{_BASE64URL_CHAR}{{4}}|{_BASE64URL_PADDED}")
_FROM_BASE64URL = str.maketrans("-_", "+/")


def _is_base64url(text: str) -> bool:
    """True if BYTES_PATTERN matches the whole of ``text``."""
    size = len(text)
    if size % 4:
        return False
    return size == 0 or (
        _BASE64URL.fullmatch(text, 0, size - 4) is not None
        and _BASE64URL_END.fullmatch(text, size - 4) is not None
    )


def fullmatch(pattern: str, text: str) -> bool:
    """True if ``pattern``, one of the forms' patterns, matches all of ``text``."""
    if pattern == BYTES_PATTERN:
        return _is_base64url(text)
    return re.fullmatch(pattern, text) is not None


def _read_duration(value: object) -> timedelta:
    if type(value) is str:
        if not fullmatch(DURATION_PATTERN, value):
            raise ValueError(
                "a duration is written as seconds with an 's' suffix and 0, 3 or "
                "6 fractional digits, as few as it needs, such as '1.500s'"
            )
        sign = -1 if value.startswith("-") else 1
        whole, _, fraction = value.lstrip("-").removesuffix("s").partition(".")
        micros = int(whole) * 10**6 + int(fraction.ljust(6, "0") or "0")
        return timedelta(microseconds=sign * micros)
    if type(value) is not timedelta:
        raise ValueError("a duration is a datetime.timedelta, or text such as '1.500s'")
    if abs(value) >= _DURATION_LIMIT:
        raise ValueError("a duration's magnitude is below 10**11 seconds")
    return value


def _write_duration(value: timedelta) -> str:
    if type(value) is not timedelta or abs(value) >= _DURATION_LIMIT:
        raise ValueError(
            "a duration is a datetime.timedelta below 10**11 seconds in magnitude"
        )
    micros = value // _MICROSECOND
    sign = "-" if micros < 0 else ""
    seconds, fraction = divmod(abs(micros), 10**6)
    if fraction == 0:
        return f"{sign}{seconds}s"
    if fraction % 1000 == 0:
        return f"{sign}{seconds}.{fraction // 1000:03d}s"
    return f"{sign}{seconds}.{fraction:06d}s"


def _has_more_digits(value: Decimal) -> bool:
    """True if a nonzero decimal's coefficient has more than 34 digits."""
    context = _DECIMAL_ROUNDING.copy()
    context.plus(value)
    return bool(context.flags[Rounded])


def _decimal_text(value: Decimal) -> str | None:
    """The decimal's form, or None if it has none.

    A positive exponent is written at exponent 0 and a zero without its sign,
    with the value kept (spec 01). Every value, zero included, has its size
    checked before it is written out, so a huge exponent of either sign cannot
    make a huge string.
    """
    # as_tuple holds each digit as an entry, about 24 times the value's own
    # size, so a value with no form is refused before it when that can be
    # told otherwise: a NaN, whose payload is digits too, or a coefficient of
    # more than 34 digits, as the text would hold them all.
    if not value.is_finite():
        return None
    if not value.is_zero() and _has_more_digits(value):
        return None
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # unreachable: the value is finite
        return None
    if -exponent > _DECIMAL_DIGITS:
        return None
    if value.is_zero():
        value = Decimal(0) if exponent > 0 else value.copy_abs()
    elif exponent > 0:
        if len(digits) + exponent > _DECIMAL_DIGITS:
            return None
        value = Decimal((sign, (*digits, *(0,) * exponent), 0))
    text = format(value, "f")
    if fullmatch(DECIMAL_PATTERN, text) and fullmatch(DECIMAL_DIGITS_PATTERN, text):
        return text
    return None


def _read_decimal(value: object) -> Decimal:
    if type(value) is str:
        if not (
            fullmatch(DECIMAL_PATTERN, value)
            and fullmatch(DECIMAL_DIGITS_PATTERN, value)
        ):
            raise ValueError(
                "a decimal is written as text of at most 34 digits with an "
                "optional '-' and point, no exponent and no leading zero, such "
                "as '-12.50'"
            )
        return Decimal(value)
    if type(value) is not Decimal:
        raise ValueError("a decimal is a decimal.Decimal, or text such as '-12.50'")
    text = _decimal_text(value)
    if text is None:
        raise ValueError("a decimal is finite, with at most 34 digits")
    return Decimal(text)


def _write_decimal(value: Decimal) -> str:
    text = _decimal_text(value) if type(value) is Decimal else None
    if text is None:
        raise ValueError("a decimal is a finite decimal.Decimal of at most 34 digits")
    return text


def _read_binary(value: object, info: core_schema.ValidationInfo) -> bytes:
    if type(value) is str:
        if info.mode != "json":
            # Outside JSON text (Python input, or model_validate_strings), bytes
            # would read a str as its UTF-8 bytes; reading it as base64url here
            # would change what the same call holds, so it is refused. The str
            # is either text meant as data or base64url from a parsed JSON body,
            # and the message names both, since the remedies differ.
            raise ValueError(
                "binary data from Python is bytes, such as text.encode() for "
                "text; to read base64url from JSON, validate the JSON text "
                "itself (model_validate_json), not a value parsed from it"
            )
        if not _is_base64url(value):
            raise ValueError(
                "bytes are written in base64url with padding and zero unused bits"
            )
        # urlsafe_b64decode would copy the text twice, as ASCII bytes and
        # then translated; a2b_base64 reads the translated str in place.
        return binascii.a2b_base64(value.translate(_FROM_BASE64URL))
    if type(value) is not bytes:
        raise ValueError("binary data is bytes, or base64url text")
    return value


def _write_binary(value: bytes) -> str:
    if type(value) is not bytes:
        raise ValueError("binary data is bytes")
    return base64.urlsafe_b64encode(value).decode("ascii")


# The core schema type of each form's plain type, and the keys Pydantic gives
# it with nothing added (a timedelta always carries microseconds_precision).
_KINDS: dict[type[Any], str] = {
    timedelta: "timedelta",
    Decimal: "decimal",
    bytes: "bytes",
}
_BARE_KEYS = frozenset({"type", "ref", "metadata", "microseconds_precision"})


@once
@dataclass(frozen=True, eq=False)
class _Form:
    """The marker of one form: its type, reader, writer and description."""

    name: str
    python_type: type[Any]
    read: Callable[..., Any]
    write: Callable[[Any], str]
    described: Mapping[str, Any] = field(repr=False)
    # The reader takes the validation info, to tell JSON from Python input.
    with_info: bool = False

    def core_schema(self) -> CoreSchema:
        serialization = core_schema.plain_serializer_function_ser_schema(
            self.write, when_used="json"
        )
        if self.with_info:
            return core_schema.with_info_plain_validator_function(
                self.read, serialization=serialization
            )
        return core_schema.no_info_plain_validator_function(
            self.read, serialization=serialization
        )

    def description(self) -> dict[str, Any]:
        """The form's JSON Schema, a new copy each time."""
        return {
            key: [dict(part) for part in value] if key == "allOf" else value
            for key, value in self.described.items()
        }

    def __get_pydantic_core_schema__(
        self, source: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        if source is not self.python_type:
            raise TypeError(
                f"neolith.{self.name} annotates {self.python_type.__name__}, "
                f"not {_naming.shown(source)}; use neolith.{self.name} itself"
            )
        # Pydantic applies whatever precedes this marker, such as a Field(...)
        # in the default slot, to the plain type first and hands the result
        # here. This schema replaces it, so anything there would be dropped
        # without a trace; it is refused instead.
        before = handler(source)
        added = sorted(set(before) - _BARE_KEYS)
        same = before.get("type") == _KINDS[self.python_type]
        if not same or added:
            built = ", ".join(added) if same else f"a {before.get('type')} node"
            raise TypeError(
                f"neolith.{self.name} admits no constraint, setting or validator "
                f"placed before it, as a Field(...) default places them (here: "
                f"{built}); its own reader and writer fix its form"
            )
        return self.core_schema()

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return self.description()


_DURATION = _Form(
    "Duration",
    timedelta,
    _read_duration,
    _write_duration,
    {"type": "string", "pattern": DURATION_PATTERN},
)
_DECIMAL = _Form(
    "Decimal128",
    Decimal,
    _read_decimal,
    _write_decimal,
    {
        "type": "string",
        "pattern": DECIMAL_PATTERN,
        "allOf": [{"pattern": DECIMAL_DIGITS_PATTERN}],
    },
)
_BINARY = _Form(
    "Binary",
    bytes,
    _read_binary,
    _write_binary,
    {"type": "string", "contentEncoding": "base64url", "pattern": BYTES_PATTERN},
    with_info=True,
)

#: A duration (spec 01): a ``timedelta`` below 10^11 seconds in magnitude,
#: written ``"1.500s"``.
Duration = Annotated[timedelta, _DURATION]
#: A decimal (spec 01): a finite ``Decimal`` of at most 34 digits, not
#: counting the sign and the point, so below 10^34 in magnitude, written
#: ``"-12.50"``.
Decimal128 = Annotated[Decimal, _DECIMAL]
#: Bytes (spec 01): written in base64url with padding, such as ``"_w=="``, and
#: read as base64url from JSON text only (ADR 0020); from anything else,
#: ``bytes`` only.
Binary = Annotated[bytes, _BINARY]

FORMS = (_DURATION, _DECIMAL, _BINARY)
# The plain Python type of each form, for the message that refuses it.
FORM_FOR_KIND = {"timedelta": _DURATION, "decimal": _DECIMAL, "bytes": _BINARY}


def form_of(node: Mapping[str, Any]) -> _Form | None:
    """The form whose marker built this core schema node, or None.

    Matched by identity: the node must be exactly the one the marker builds,
    with neolith's own reader and writer. ``node`` holds only the keys that
    change what travels; the caller leaves out those any node may carry, such
    as the reference Pydantic adds to a ``type`` alias.
    """
    if node.get("type") != "function-plain":
        return None
    for form in FORMS:
        if node == form.core_schema():
            return form
    return None


def form_in(node: Mapping[str, Any]) -> _Form | None:
    """The form whose reader or writer this node calls, whatever else it holds.

    A constraint, validator or serializer added to a neolith type changes its
    node, so ``form_of`` no longer matches; this names the type in the refusal.
    A plain validator replaces the reader and keeps the writer, and a
    serializer the converse.
    """
    if node.get("type") != "function-plain":
        return None
    calls = [
        part.get("function")
        for part in (node.get("function"), node.get("serialization"))
        if isinstance(part, Mapping)
    ]
    for form in FORMS:
        if any(call is form.read or call is form.write for call in calls):
            return form
    return None
