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
- From Python, each takes a value of its type or a string in its form.
- Its JSON Schema states its form's pattern, in validation and serialization
  modes alike, so Pydantic's own schema agrees with the wire.

The contract checker admits these markers' schemas by identity and refuses
the plain types, naming the type to use.
"""

import base64
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

# Each pattern is ASCII-only and anchored with ^...$, and matches the same
# strings under ECMA-262, Rust regex and Python's re.fullmatch (spec 01).
BYTES_PATTERN = (
    r"^([A-Za-z0-9_\-]{4})*"
    r"([A-Za-z0-9_\-][AQgw]==|[A-Za-z0-9_\-]{2}[AEIMQUYcgkosw048]=)?$"
)
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


def _fullmatch(pattern: str, text: str) -> bool:
    return re.fullmatch(pattern, text) is not None


def _read_duration(value: object) -> timedelta:
    if type(value) is str:
        if not _fullmatch(DURATION_PATTERN, value):
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


def _decimal_text(value: Decimal) -> str | None:
    """The decimal's form, or None if it has none.

    A positive exponent is written at exponent 0 and a zero without its sign,
    with the value kept (spec 01). The size is checked before the value is
    written out, so a huge exponent cannot make a huge string.
    """
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # infinity or NaN
        return None
    if value.is_zero():
        value = Decimal(0) if exponent > 0 else value.copy_abs()
    elif exponent > 0:
        if len(digits) + exponent > _DECIMAL_DIGITS:
            return None
        value = Decimal((sign, (*digits, *(0,) * exponent), 0))
    elif -exponent > _DECIMAL_DIGITS:
        return None
    text = format(value, "f")
    if _fullmatch(DECIMAL_PATTERN, text) and _fullmatch(DECIMAL_DIGITS_PATTERN, text):
        return text
    return None


def _read_decimal(value: object) -> Decimal:
    if type(value) is str:
        if not (
            _fullmatch(DECIMAL_PATTERN, value)
            and _fullmatch(DECIMAL_DIGITS_PATTERN, value)
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


def _read_binary(value: object) -> bytes:
    if type(value) is str:
        if not _fullmatch(BYTES_PATTERN, value):
            raise ValueError(
                "bytes are written in base64url with padding and zero unused bits"
            )
        return base64.urlsafe_b64decode(value)
    if type(value) is not bytes:
        raise ValueError("binary data is bytes, or base64url text")
    return value


def _write_binary(value: bytes) -> str:
    if type(value) is not bytes:
        raise ValueError("binary data is bytes")
    return base64.urlsafe_b64encode(value).decode("ascii")


@dataclass(frozen=True, eq=False)
class _Form:
    """The marker of one form: its type, reader, writer and description."""

    name: str
    python_type: type[Any]
    read: Callable[[Any], Any]
    write: Callable[[Any], str]
    described: Mapping[str, Any] = field(repr=False)

    def core_schema(self) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(
            self.read,
            serialization=core_schema.plain_serializer_function_ser_schema(
                self.write, when_used="json"
            ),
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
                f"not {source!r}; use neolith.{self.name} itself"
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
)

# A duration (spec 01): a timedelta below 10**11 seconds, written "1.500s".
Duration = Annotated[timedelta, _DURATION]
# A decimal (spec 01): a finite Decimal of at most 34 digits, written "-12.50".
Decimal128 = Annotated[Decimal, _DECIMAL]
# Bytes (spec 01): written in base64url with padding, such as "_w==".
Binary = Annotated[bytes, _BINARY]

FORMS = (_DURATION, _DECIMAL, _BINARY)
# The plain Python type of each form, for the message that refuses it.
FORM_FOR_KIND = {"timedelta": _DURATION, "decimal": _DECIMAL, "bytes": _BINARY}


def form_of(node: Mapping[str, Any]) -> _Form | None:
    """The form whose marker built this core schema node, or None.

    Matched by identity: the node must be exactly the one the marker builds,
    with neolith's own reader and writer, apart from the metadata Pydantic's
    JSON Schema generator reads.
    """
    if node.get("type") != "function-plain":
        return None
    own = {key: value for key, value in node.items() if key != "metadata"}
    for form in FORMS:
        if own == form.core_schema():
            return form
    return None
