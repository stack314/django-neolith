"""The description check: a validator compiled from a contract's description.

A sender checks the JSON text of each value against the published
description (CON-16) with a strict checker derived from that description,
which adds no runtime dependency (ADR 0007 Decision 2). This module compiles
the description into a pydantic-core validator when the contract is declared,
so the description alone says what the check admits.

The compiler reads only what neolith's description writer emits
(``neolith._portable`` and ``neolith._forms``). Each keyword is taken out of
a copy of its schema by the code that enforces it, and a schema with anything
left over is refused, so a keyword is never read and then not enforced, and
one the check does not know is never skipped.

Three rules of ADR 0019 that the description cannot state are added (CON-40):
an integer is read strictly, so ``1.0`` is not one; a date or timestamp names
a day that exists; and a timestamp's instant falls in years 0001 to 9999 in
UTC. A number is read as the double a reader holds, as the declared type's
validator reads it. A ``-0`` is admitted: only a sender is bound to write
``0`` (CON-35), and it converts the value before it writes the text.
"""

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from pydantic_core import CoreSchema, PydanticCustomError, SchemaValidator, core_schema

from neolith import _naming
from neolith._forms import (
    BYTES_PATTERN,
    DECIMAL_DIGITS_PATTERN,
    DECIMAL_PATTERN,
    DURATION_PATTERN,
)
from neolith._portable import DATE_PATTERN, TIMESTAMP_PATTERN

__all__ = ["description_checker"]

# The patterns of the wire encodings (spec 01), which match the same strings
# under Rust regex, which pydantic-core runs, as under ECMA-262. No other
# pattern is read.
_PATTERNS = frozenset(
    {
        BYTES_PATTERN,
        DATE_PATTERN,
        DECIMAL_DIGITS_PATTERN,
        DECIMAL_PATTERN,
        DURATION_PATTERN,
        TIMESTAMP_PATTERN,
    }
)
_REFERENCE = "#/$defs/"
_NULL = {"type": "null"}


def _real(named: type) -> type:
    """The class of the datetime module that a name for it leads to."""
    return next(kind for kind in named.__mro__ if kind.__module__ == "datetime")


# The classes of a timestamp and a date. A tool that freezes time for a test,
# as freezegun does, binds every module-level name for ``datetime`` or
# ``date``, in every loaded module, to a fake of its own that subclasses the
# real class, and a module first imported meanwhile gets the fakes by those
# names. So the real classes are found once, through whatever the names lead
# to, and no code here reads those names while it runs.
TIMES: tuple[type[datetime], type[date]] = (_real(datetime), _real(date))


def description_checker(defs: Mapping[str, Any], name: str) -> SchemaValidator:
    """Compile the description of record ``name``, with the records and enums
    it refers to in ``defs``, into a validator of JSON text.

    Raises ``TypeError`` for a description this check does not read.
    """
    compiled: dict[str, CoreSchema] = {}
    pending = [name]
    try:
        while pending:
            current = pending.pop()
            if current in compiled:
                continue
            if current not in defs:
                raise TypeError(f"it refers to {current!r}, which it lacks")
            refs: list[str] = []
            schema: Any = {**_schema(defs[current], refs), "ref": current}
            compiled[current] = schema
            pending.extend(refs)
        return SchemaValidator(
            core_schema.definitions_schema(
                core_schema.definition_reference_schema(name), list(compiled.values())
            ),
            # A value refused at send time may be a secret, so an error of
            # this check names the place and the rule, never the value.
            {"hide_input_in_errors": True},
        )
    except Exception as error:
        # Fail closed: whatever the compiler or pydantic-core could not read
        # refuses the contract, under one name.
        raise TypeError(
            f"the description of {name} is not one the description check reads "
            f"({_naming.cause(error, None)})"
        ) from error


def _schema(description: object, refs: list[str]) -> CoreSchema:
    """Compile one schema, adding the names it refers to to ``refs``."""
    if type(description) is not dict:
        raise TypeError(f"{description!r} is not a schema")
    rest = dict(description)
    # A default says what a reader assumes for an absent field; it is not a
    # rule on the value.
    rest.pop("default", None)
    made: CoreSchema
    if "$ref" in rest:
        reference = rest.pop("$ref")
        if type(reference) is not str or not reference.startswith(_REFERENCE):
            raise TypeError(f"{reference!r} is not a reference to a definition")
        refs.append(reference.removeprefix(_REFERENCE))
        made = core_schema.definition_reference_schema(refs[-1])
    elif "anyOf" in rest:
        branches = rest.pop("anyOf")
        if type(branches) is not list or len(branches) != 2 or branches[1] != _NULL:
            raise TypeError(f"anyOf is read only as [a schema, null]: {branches!r}")
        made = core_schema.nullable_schema(_schema(branches[0], refs))
    else:
        made = _typed(rest, refs)
    if rest:
        raise TypeError(
            f"{', '.join(sorted(map(str, rest)))} in {description!r} is not read"
        )
    return made


def _typed(rest: dict[str, Any], refs: list[str]) -> CoreSchema:
    """Compile a schema by its ``type``, taking each keyword it enforces."""
    kind = rest.pop("type", None)
    if kind == "object" and "properties" in rest:
        properties = rest.pop("properties")
        required = rest.pop("required", [])
        if (
            type(properties) is not dict
            or type(required) is not list
            or not all(type(field) is str for field in (*properties, *required))
            or not set(required) <= set(properties)
        ):
            raise TypeError(f"a record's fields are not read: {properties!r}")
        return core_schema.typed_dict_schema(
            {
                field: core_schema.typed_dict_field(
                    _schema(part, refs), required=field in required
                )
                for field, part in properties.items()
            },
            # A record ignores unknown fields (CON-6, CON-7).
            extra_behavior="ignore",
        )
    if kind == "object":
        values = _schema(_taken(rest, "additionalProperties"), refs)
        names = rest.pop("propertyNames", {})
        if type(names) is not dict:
            raise TypeError(f"propertyNames is not read: {names!r}")
        left = dict(names)
        keys = _string(left)
        if left:
            raise TypeError(f"{', '.join(sorted(map(str, left)))} in {names!r}")
        return core_schema.dict_schema(
            keys,
            values,
            min_length=_count(rest, "minProperties"),
            max_length=_count(rest, "maxProperties"),
        )
    if kind == "array":
        return core_schema.list_schema(
            _schema(_taken(rest, "items"), refs),
            min_length=_count(rest, "minItems"),
            max_length=_count(rest, "maxItems"),
        )
    if kind == "string":
        return _string(rest)
    if kind == "integer":
        multiple = _whole(rest, "multipleOf")
        if multiple is not None and multiple <= 0:
            raise TypeError(f"multipleOf {multiple!r} is not above 0")
        # Strict, so a number written with a fraction or an exponent is not
        # an integer (CON-40).
        return core_schema.int_schema(
            strict=True,
            ge=_whole(rest, "minimum"),
            le=_whole(rest, "maximum"),
            gt=_whole(rest, "exclusiveMinimum"),
            lt=_whole(rest, "exclusiveMaximum"),
            multiple_of=multiple,
        )
    if kind == "number":
        return core_schema.float_schema(
            strict=True,
            allow_inf_nan=False,
            ge=_double(rest, "minimum"),
            le=_double(rest, "maximum"),
            gt=_double(rest, "exclusiveMinimum"),
            lt=_double(rest, "exclusiveMaximum"),
        )
    if kind == "boolean":
        return core_schema.bool_schema(strict=True)
    raise TypeError(f"type {kind!r} is not read")


def _string(rest: dict[str, Any]) -> CoreSchema:
    """Compile the keywords of a string, or of a map's keys, taking each."""
    if "enum" in rest:
        values = rest.pop("enum")
        if (
            type(values) is not list
            or not values
            or not all(type(value) is str for value in values)
        ):
            raise TypeError(f"enum is read only as a list of strings: {values!r}")
        return core_schema.literal_schema(values)
    patterns = [rest.pop("pattern")] if "pattern" in rest else []
    for part in rest.pop("allOf", []):
        if type(part) is not dict or set(part) != {"pattern"}:
            raise TypeError(f"allOf is read only as further patterns: {part!r}")
        patterns.append(part["pattern"])
    for pattern in patterns:
        if type(pattern) is not str or pattern not in _PATTERNS:
            raise TypeError(f"pattern {pattern!r} is not one of the wire encodings")
    steps: list[CoreSchema] = [
        core_schema.str_schema(
            strict=True,
            pattern=patterns[0] if patterns else None,
            min_length=_count(rest, "minLength"),
            max_length=_count(rest, "maxLength"),
        )
    ]
    steps += [core_schema.str_schema(strict=True, pattern=p) for p in patterns[1:]]
    # A format or an encoding only annotates; the pattern beside it states
    # the form, so each is read only with its own pattern.
    form = rest.pop("format", None)
    if form == "date" and DATE_PATTERN in patterns:
        steps.append(core_schema.no_info_plain_validator_function(_existing_day))
    elif form == "date-time" and TIMESTAMP_PATTERN in patterns:
        steps.append(core_schema.no_info_plain_validator_function(_instant))
    elif form is not None:
        raise TypeError(f"format {form!r} is not read without its pattern")
    encoding = rest.pop("contentEncoding", None)
    if encoding is not None and (
        encoding != "base64url" or BYTES_PATTERN not in patterns
    ):
        raise TypeError(f"contentEncoding {encoding!r} is not read without its pattern")
    return steps[0] if len(steps) == 1 else core_schema.chain_schema(steps)


def _taken(rest: dict[str, Any], keyword: str) -> object:
    if keyword not in rest:
        raise TypeError(f"{keyword} is missing")
    return rest.pop(keyword)


def _count(rest: dict[str, Any], keyword: str) -> int | None:
    """Take a length keyword: a whole number that is not negative."""
    value: object = rest.pop(keyword, None)
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    raise TypeError(f"{keyword} {value!r} is not a length")


def _whole(rest: dict[str, Any], keyword: str) -> int | None:
    """Take a bound or a multiple of an integer: a whole number, by exact
    type, since pydantic-core would read ``True`` or ``"3"`` as one. Pydantic
    keeps a whole float as an author wrote it, such as ``ge=1.0``.
    """
    value: object = rest.pop(keyword, None)
    if value is None:
        return None
    if type(value) is int:
        return value
    if type(value) is float and value.is_integer():
        return int(value)
    raise TypeError(f"{keyword} {value!r} is not a whole number")


def _double(rest: dict[str, Any], keyword: str) -> float | None:
    """Take a bound of a number: one a double holds exactly, by exact type,
    since a reader of the description compares against the number written.
    """
    value: object = rest.pop(keyword, None)
    if value is None:
        return None
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is int and float(value) == value:
        return float(value)
    raise TypeError(f"{keyword} {value!r} is not a number a double holds")


def _existing_day(text: str) -> str:
    """A date whose day exists (CON-40); its pattern has fixed its form."""
    try:
        TIMES[1].fromisoformat(text)
    except ValueError:
        raise PydanticCustomError(
            "day_exists", "the text names a day that does not exist"
        ) from None
    return text


def _instant(text: str) -> str:
    """A timestamp whose day exists, and whose instant falls in years 0001 to
    9999 in UTC (CON-40); its pattern has fixed its form.
    """
    try:
        TIMES[0].fromisoformat(text).astimezone(UTC)
    except OverflowError:
        raise PydanticCustomError(
            "instant_in_range", "the text is outside years 0001 to 9999 in UTC"
        ) from None
    except ValueError:
        raise PydanticCustomError(
            "day_exists", "the text names a day that does not exist"
        ) from None
    return text
