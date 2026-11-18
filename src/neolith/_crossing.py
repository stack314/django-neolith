"""How values cross a call: as JSON text, through the declared type (spec 02).

Every request, reply and error detail crosses as JSON text, in-process too
(ADR 0007). When a contract is declared, neolith builds a serializer and a
validator for each of its records from the core schema the description was
written from, with the record's config, as Pydantic does, and compiles the
description itself into the description check (``neolith._checker``).

A sender (``Codec.send``) takes a value of exactly its declared record and
reads it once with that serializer, into a copy of its own. It converts the
copy as spec 01 says, writes it with the same serializer, with warnings as
errors, checks the text against the description, reads the text back with
the validator, and compares what it read with the copy. A receiver reads the
text with the validator, so what it hands on shares no object with the
sender. Nothing set on a record class afterwards, and none of its own
methods, such as ``model_dump_json``, change what crosses.

What a receiver checks depends on where the text comes from (``Receiver``).
In-process it reads only text that a sender's check passed, so it runs the
validator alone. A receiver that reads text from a network (``Codec.receive``)
first runs the network receive check of ADR 0007 Decision 3: the text is
I-JSON (spec 01, CON-42), and it passes the description check, with the
rules that check adds (CON-39, CON-40). It then reads the same text with the
validator.

pydantic-core reuses, for each record a schema names, the serializer and
validator Pydantic stored on that record's class, when the class is complete
(``src/common/prebuilt.rs``, at 2.41.1 and in Pydantic 2.14.3). So a record
is admitted only when what it would reuse was built from the record's own
core schema.
"""

import enum
import json
import math
import sys
import typing
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, NoReturn, final

from pydantic import BaseModel
from pydantic_core import SchemaSerializer, SchemaValidator, ValidationError

from neolith import _naming
from neolith._checker import TIMES, description_checker
from neolith._forms import FORM_FOR_KIND
from neolith._naming import shown
from neolith._portable import text_problem

if typing.TYPE_CHECKING:
    from neolith._contracts import Action, Contract

__all__ = ["Codec", "Receiver", "Refusal", "check_request", "codec"]


class Receiver(enum.Enum):
    """Where a receiver reads a value's text from (ADR 0007 Decision 3)."""

    #: Text that a sender's check passed in this process: the validator alone.
    IN_PROCESS = "in-process"
    #: Text from a network: the network receive check, then the validator.
    NETWORK = "network"


@final
class Refusal(Exception):
    """Text a network receiver refuses (``Codec.receive``).

    Its message names the rule the text breaks and, where there is one, the
    place in the value, never a value that arrived.
    """


@final
class Codec[R: BaseModel]:
    """The serializer, validator and description check of one record."""

    __slots__ = (
        "_checker",
        "_defs",
        "_hides",
        "_serializer",
        "_validator",
        "record",
    )

    def __init__(
        self,
        record: type[R],
        schema: Mapping[str, Any],
        config: Mapping[str, Any] | None,
        defs: Mapping[str, Any],
    ) -> None:
        """Build from the record's core schema and the config of its model node,
        and compile its description, in ``defs``, into the description check.

        Raises ``TypeError`` when a record the schema names would bring a
        serializer or validator not built from its own core schema, or when
        the description is not one the description check reads.
        """
        for node in _nodes(schema):
            if node.get("type") in ("model", "dataclass"):
                _check_reused(node)
        try:
            core: Any = schema
            settings: Any = config
            serializer = SchemaSerializer(core, settings)
            validator = SchemaValidator(core, settings)
        except Exception as error:
            raise TypeError(
                f"record {_naming.name(record)} cannot be built from its core "
                f"schema ({_naming.cause(error)})"
            ) from error
        self.record = record
        self._serializer = serializer
        self._validator = validator
        self._checker = description_checker(defs, _naming.name(record))
        self._defs = defs
        self._hides = bool(config and config.get("hide_input_in_errors"))

    def send(self, value: object, subject: str) -> bytes:
        """Check a value for sending and return its JSON text.

        ``subject`` names the value in a message, such as "the reply". The
        checks run in the order of ADR 0017 Decision 9, the caller's step 2:

        1. the value is exactly of the record's type;
        2. the serializer reads it, once, into a copy of the sender's own,
           where a ``-0`` and a timestamp offset with seconds are converted
           (CON-35, CON-36), each timestamp's offset is asked for once and
           fixed, and a number that is not finite is refused, and then
           writes the copy;
        3. the text passes the description check;
        4. the validator reads the text back, and each part it reads is the
           part of the copy it was written from: equal, and of the same kind
           (TRN-11).

        A value of another type is a ``TypeError``. Every other failure is a
        ``ValueError``, whose cause is the exception behind it where there is
        one. What is sent is the value as it was when the serializer read it;
        a change made to it meanwhile does not reach the text.

        A value held at send time may be a secret, and an exception's text is
        logged. A message written here names the place and the types of what
        is refused and shows no value, apart from a number that is not
        finite. It names a place in a map by its key, and keeps the exception
        behind it as its cause, as Pydantic's own errors do, unless the
        record's config hides inputs in errors (``hide_input_in_errors``).
        Then a place in a map is named by its position, and no cause is
        kept, since the serializer's error shows the value whatever the
        config, and any other may name a key.
        """
        if type(value) is not self.record:
            raise TypeError(
                f"{subject} is {_kind(value)}, "
                f"not exactly a {_naming.name(self.record)}"
            )
        name = _naming.name(self.record)
        problem: str | None = None
        failed: Exception | None = None
        text = b""
        try:
            copy = self._serializer.to_python(value, warnings="error")
            copy, problem = _converted(copy)
            if problem is None:
                text = self._serializer.to_json(copy, warnings="error")
        except Exception as error:
            failed = error
        if failed is not None:
            raise self._refusal(
                f"{subject} cannot be written as JSON by its type, {name}", failed
            )
        if problem is not None:
            raise ValueError(
                f"{subject} cannot be written as JSON by its type, {name}: it "
                f"holds {problem}"
            )
        try:
            self._checker.validate_json(text)
        except Exception as error:
            failed = error
        if failed is not None:
            # The serializer wrote the text, so text that is not JSON to the
            # reader is past a limit of the reader's, which the description
            # does not state.
            reason = (
                _reader_limit(failed)
                or f"does not fit the contract's description of {name}"
            )
            raise self._refusal(f"{subject} {reason}", failed)
        try:
            received = self._validator.validate_json(text)
        except Exception as error:
            failed = error
        if failed is not None:
            raise self._refusal(f"{subject} does not read back as a {name}", failed)
        changed: str | None = None
        try:
            changed = _difference(copy, received, name, by_position=self._hides)
        except Exception as error:
            failed = error
        if failed is not None:
            raise self._refusal(
                f"{subject} cannot be compared with what it reads back as", failed
            )
        if changed is not None:
            raise ValueError(f"{subject} {changed}, so it is not sent")
        return text

    def _refusal(self, said: str, behind: Exception) -> ValueError:
        """The refusal to raise for an exception, outside its handler, so that
        the exception is kept only as the cause, and not at all for a record
        that hides its inputs.
        """
        kind = _naming.type_name(behind)
        if self._hides:
            return ValueError(f"{said} ({kind})")
        refusal = ValueError(f"{said} ({kind}; see the cause)")
        refusal.__cause__ = behind
        return refusal

    def read(self, text: bytes) -> R:
        """Read JSON text into a new value of the record, ignoring unknown fields.

        Raises what the validator raises: ``pydantic.ValidationError``, or an
        exception from a validator of the record.
        """
        return typing.cast(R, self._validator.validate_json(text))

    def receive(self, text: bytes, subject: str) -> R:
        """Read JSON text that arrived from a network into a new value.

        ``subject`` names the value in a message, such as "the request". The
        network receive check (ADR 0007 Decision 3) runs first, in this
        order:

        1. the text is I-JSON (CON-42): UTF-8 JSON text, with no member name
           twice in one object, no lone surrogate, and no number outside the
           range of a double, and so no ``NaN`` or ``Infinity``;
        2. it passes the description check (CON-16, CON-39), with the rules
           that check adds (CON-40).

        The validator then reads the same text, ignoring unknown fields
        (TRN-12). A text refused at any of these raises ``Refusal``. Any other
        exception is one a validator of the record raised.
        """
        if type(text) is not bytes:
            raise TypeError(f"{subject} arrives as bytes, not {_kind(text)}")
        name = _naming.name(self.record)
        parsed = _interchange(text, subject)
        failed: ValidationError | None = None
        try:
            self._checker.validate_json(text)
        except ValidationError as error:
            failed = error
        if failed is not None:
            reason = _reader_limit(failed)
            if reason is None:
                first = failed.errors(include_url=False, include_input=False)[0]
                reason = (
                    f"does not fit the contract's description of {name}, at "
                    f"{self._place(parsed, first['loc'])}: {first['msg']}"
                )
            del failed
            raise Refusal(f"{subject} {reason}")
        try:
            return typing.cast(R, self._validator.validate_json(text))
        except ValidationError as error:
            failed = error
        first = failed.errors(include_url=False, include_input=False)[0]
        del failed
        # A message a validator of the record wrote may show a value, so only
        # the kind of the error is named.
        raise Refusal(
            f"{subject} does not read as a {name}, at "
            f"{self._place(parsed, first['loc'])} ({first['type']})"
        )

    def _place(self, value: object, loc: tuple[int | str, ...]) -> str:
        """Name the place in ``value`` that an error's location leads to.

        A record's field is named, a list's item counted, and a map's entry
        counted by where it comes in the text, so that no key is shown. A
        step that leads nowhere in the value ends the place there.
        """
        name = _naming.name(self.record)
        steps = [name]
        schema: object = {"$ref": f"#/$defs/{name}"}
        for step in loc:
            schema = self._resolved(schema)
            if type(schema) is not dict:
                break
            described = typing.cast(dict[str, Any], schema)
            fields = described.get("properties")
            if type(value) is dict and type(fields) is dict and step in fields:
                steps.append(f".{step}")
                schema = fields[step]
                value = typing.cast(dict[str, object], value).get(str(step))
            elif type(value) is dict and "additionalProperties" in described:
                entries = typing.cast(dict[str, object], value)
                if step == "[key]":
                    steps.append(" (its key)")
                    break
                if type(step) is not str or step not in entries:
                    break
                steps.append(f"[entry {list(entries).index(step)}]")
                schema, value = described["additionalProperties"], entries[step]
            elif type(value) is list and type(step) is int:
                items = typing.cast(list[object], value)
                if not 0 <= step < len(items):
                    break
                steps.append(f"[{step}]")
                schema, value = described.get("items"), items[step]
            else:
                break
        return "".join(steps)

    def _resolved(self, schema: object) -> object:
        """A schema with its reference followed and its null branch dropped."""
        for _ in range(len(self._defs) + 2):
            if type(schema) is not dict:
                return None
            described = typing.cast(dict[str, Any], schema)
            reference = described.get("$ref")
            branches = described.get("anyOf")
            if type(reference) is str:
                schema = self._defs.get(reference.rsplit("/", 1)[-1])
            elif type(branches) is list and branches:
                schema = branches[0]
            else:
                return schema
        return None


def codec[R: BaseModel](contract: "Contract", record: type[R]) -> Codec[R]:
    """The codec a contract built for one of its request, reply or detail records."""
    return typing.cast(Codec[R], contract._codecs[record])


def check_request(contract: "Contract", action: "Action", request: object) -> bytes:
    """Check a request before it is sent, and return its JSON text.

    This is the request check of ADR 0017 Decision 9, step 2 (``Codec.send``).
    A failure is a mistake in the calling code, so it raises ``TypeError`` or
    ``ValueError``, never a ``ContractError``. An exception that is not an
    ``Exception`` propagates unchanged.
    """
    subject = f"{contract.identity}, action {action.name!r}: the request"
    return codec(contract, action.request).send(request, subject)


class _Fields(dict[Any, Any]):
    """A record's fields, or a map's entries, in a sender's copy of a value.

    The serializer dumps a record and a map alike as a dict. To write the
    copy with the same serializer, each of its dicts is one of these:
    pydantic-core writes an object in a record's place from its ``__dict__``,
    whatever its class (``SerCheck::None`` in
    ``src/serializers/type_serializers/model.rs``, at 2.41.1 and 2.50.4), and
    writes a dict of any class in a map's place. A plain dict in a record's
    place is refused, so this class is what lets the copy stand for the value.
    """

    __slots__ = ()

    @property
    def __dict__(self) -> dict[Any, Any]:  # type: ignore[override]
        # A property where object has an attribute, on purpose: pydantic-core
        # reads it where the copy stands for a record.
        return self


_MINUTE = timedelta(minutes=1)


def _converted(dumped: object) -> tuple[object, str | None]:
    """Make a value dumped in Python mode the sender's copy, converted.

    Returns the copy and what it holds that cannot be sent, if anything. In
    the copy each dict is a ``_Fields``, and each date and timestamp is of
    exactly its class, with a fixed offset: the offset its ``tzinfo`` gave
    when asked here, once, or UTC where that offset is not a whole number of
    minutes (CON-36). So no date or timestamp in the copy runs a caller's
    code later, and a zone with two offsets has given one. The one thing in
    the copy that still can is a map key that is not exactly a ``str``: its
    own ``__hash__`` runs when the copy is walked and written, and the
    comparison then refuses it, whatever that code did. A number's ``-0``
    becomes ``0`` (CON-35). Walks without recursion, so a deep value cannot
    exhaust the stack.
    """
    timestamp, day = TIMES
    root: list[object] = [dumped]
    pending: list[tuple[Any, Any]] = [(root, 0)]
    while pending:
        holder, at = pending.pop()
        item = holder[at]
        kind = type(item)
        if kind is dict:
            fields = _Fields(item)
            holder[at] = fields
            pending.extend((fields, key) for key in reversed(fields))
        elif kind is list:
            pending.extend((item, index) for index in reversed(range(len(item))))
        elif isinstance(item, float):
            if not math.isfinite(item):
                return root[0], f"{shown(item)}, and a number must be finite"
            if kind is float and item == 0 and math.copysign(1.0, item) < 0:
                holder[at] = 0.0
        elif isinstance(item, timestamp):
            # Read through datetime's own methods, so that a subclass, which
            # Pydantic keeps as it is given, runs no code of its own here.
            offset = timestamp.utcoffset(item)
            local = timestamp.combine(
                timestamp.date(item), timestamp.time(item).replace(fold=0)
            )
            if offset is None:
                holder[at] = local
            elif offset % _MINUTE:
                try:
                    holder[at] = (local - offset).replace(tzinfo=UTC)
                except OverflowError:
                    return root[0], "a timestamp outside years 0001 to 9999 in UTC"
            else:
                holder[at] = local.replace(tzinfo=timezone(offset))
        elif isinstance(item, day) and kind is not day:
            holder[at] = day.fromordinal(day.toordinal(item))
    return root[0], None


# The largest double, as an integer: an integer written in JSON text above it
# in magnitude is outside the range of a double (CON-42).
_DOUBLE_MAX = int(sys.float_info.max)
# More digits than that has.
_DOUBLE_DIGITS = len(str(_DOUBLE_MAX))


def _interchange(text: bytes, subject: str) -> object:
    """Check that a text is I-JSON as spec 01 asks of a receiver (CON-42),
    and return the value it holds, or raise ``Refusal``.

    It is UTF-8 JSON text (RFC 8259), with no member name twice in one
    object, no lone surrogate in any string or member name, and no number
    outside the range of a double. ``NaN`` and ``Infinity`` are not JSON
    numbers, and a number written with a fraction or an exponent is outside
    the range when the double it reads as is not finite, as ``1e400``. A
    number written as an integer is compared with the largest double exactly.
    """

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        found = dict(items)
        if len(found) != len(items):
            raise Refusal(f"{subject} holds a member name twice in one object")
        return found

    def constant(literal: str) -> NoReturn:
        raise Refusal(f"{subject} holds NaN or Infinity, which are not JSON numbers")

    def outside() -> NoReturn:
        raise Refusal(f"{subject} holds a number outside the range of a double")

    def fraction(written: str) -> float:
        number = float(written)
        if not math.isfinite(number):
            outside()
        return number

    def integer(written: str) -> int:
        # Counted before it is read, so a long one costs nothing to refuse.
        if len(written.lstrip("-")) > _DOUBLE_DIGITS:
            outside()
        number = int(written)
        if abs(number) > _DOUBLE_MAX:
            outside()
        return number

    problem: str | None = None
    value: object = None
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        problem = "is not UTF-8 text"
    else:
        try:
            value = json.loads(
                decoded,
                object_pairs_hook=pairs,
                parse_constant=constant,
                parse_float=fraction,
                parse_int=integer,
            )
        except RecursionError:
            problem = "is nested too deeply to be read as JSON text"
        except json.JSONDecodeError:
            problem = "is not JSON text"
    if problem is not None:
        # Raised outside the handlers, so nothing of the reader's error, which
        # can quote the text, is chained to it.
        raise Refusal(f"{subject} {problem}")
    # Without recursion, so a deep value cannot exhaust the stack.
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            for key, part in item.items():
                if text_problem(key) is not None:
                    raise Refusal(f"{subject} holds a lone surrogate in a member name")
                pending.append(part)
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is str and text_problem(item) is not None:
            raise Refusal(f"{subject} holds a lone surrogate in a string")
    return value


def _reader_limit(error: Exception) -> str | None:
    """Which limit of the JSON reader a text is past, if a check refused it
    as not being JSON at all. pydantic-core's reader (jiter) names its limits
    only in its messages: on depth, and on the digits of an integer.
    """
    if not isinstance(error, ValidationError):
        return None
    said = [
        str(problem["msg"])
        for problem in error.errors(include_url=False, include_input=False)
        if problem["type"] == "json_invalid"
    ]
    if not said:
        return None
    if any("recursion limit" in message for message in said):
        return "is nested too deeply to be read as JSON text"
    if any("number out of range" in message for message in said):
        return "holds a number too long to be read as JSON text"
    return "is written as JSON text that its reader refuses"


# Read from the instance, never through its class's attribute lookup.
_FIELDS_OF = type.__dict__["__dict__"].__get__(BaseModel)["__dict__"]
# Kinds held by exact type and equality. A timestamp is not among them, since
# equality is not a comparison of instants, nor is a decimal, whose digits
# equality does not compare.
_EXACT = frozenset({str, int, bool, bytes, timedelta, TIMES[1]})
# A decimal as its own writer spells it: a positive exponent written out and
# a zero without its sign (CON-37).
_AS_WRITTEN = FORM_FOR_KIND["decimal"].read


def _difference(
    sent: object, received: object, name: str, *, by_position: bool = False
) -> str | None:
    """Say how the value read back differs from the sender's copy, or None.

    ``sent`` is the copy that was written, where each record and map is a
    ``_Fields``; ``received`` is what the record's validator read from the
    text. Each part must be equal and of the same kind (see ``_same``). A
    record must hold every field that was read, so that a default the reader
    fills in never stands for a value the sender lacks, and a map the same
    keys, each exactly a ``str``. A place in a map is named by its key, or
    with ``by_position`` by where the entry comes in the text, counted from
    0, so that no key is shown. Walks without recursion, in the order of the
    text.
    """
    pending: list[tuple[object, object, object]] = [(sent, received, (None, name))]
    while pending:
        mine, theirs, trail = pending.pop()
        kind = type(theirs)
        parts: list[tuple[object, object, object]]
        if isinstance(theirs, BaseModel):
            if type(mine) is not _Fields:
                return _reshaped(mine, theirs, trail)
            parts = []
            for key, part in _FIELDS_OF.__get__(theirs).items():
                if key not in mine:
                    return f"lacks {_path((trail, f'.{key}'))}"
                parts.append((mine[key], part, (trail, f".{key}")))
        elif kind is list:
            items = typing.cast(list[object], theirs)
            if type(mine) is not list or len(typing.cast(list[object], mine)) != len(
                items
            ):
                return _reshaped(mine, theirs, trail)
            parts = [
                (part, back, (trail, f"[{index}]"))
                for index, (part, back) in enumerate(
                    zip(typing.cast(list[object], mine), items, strict=True)
                )
            ]
        elif kind is dict:
            entries = typing.cast(dict[str, object], theirs)
            if type(mine) is not _Fields:
                return _reshaped(mine, theirs, trail)
            held = mine
            for key in held:
                # Before any key is looked up, so that no key's own code runs.
                if type(key) is not str:
                    return (
                        f"holds a key of type {_naming.type_name(key)} at "
                        f"{_path(trail)}, which reads back as one of type str"
                    )
            if len(held) != len(entries) or not all(key in held for key in entries):
                return _reshaped(mine, theirs, trail)
            parts = [
                (
                    held[key],
                    back,
                    (trail, f"[entry {at}]" if by_position else f"[{key!r}]"),
                )
                for at, (key, back) in enumerate(entries.items())
            ]
        elif _same(mine, theirs):
            continue
        else:
            return _reshaped(mine, theirs, trail)
        pending.extend(reversed(parts))
    return None


def _same(mine: object, theirs: object) -> bool:
    """Whether a part read back is the part the sender's copy holds.

    Each scalar is held by exact type and equality, so that no code of a
    subclass runs here and a boolean never stands for an integer. Two kinds
    are wider. An integer is held for a number when the double read back
    equals it exactly. A timestamp is an instant, so its offset is not
    compared (spec 01); the instants are compared by subtraction, since
    ``==`` is False across zones for a time that comes twice in one of them
    (PEP 495), which the reader's own validators may hand back. An enum
    member is the very member. One kind is narrower: a decimal keeps its
    digits, so ``1.50`` is not held where ``1.5`` is read.
    """
    kind = type(theirs)
    timestamp = TIMES[0]
    if theirs is None or isinstance(theirs, Enum):
        return mine is theirs
    if kind is float:
        return _naming.one_of(type(mine), (float, int)) and mine == theirs
    if kind is timestamp:
        if type(mine) is not timestamp:
            return False
        read = typing.cast("datetime", theirs)
        if (mine.utcoffset() is None) is not (read.utcoffset() is None):
            return False
        return not mine - read
    if kind is Decimal:
        digits = typing.cast(Decimal, theirs).as_tuple()
        return type(mine) is Decimal and _AS_WRITTEN(mine).as_tuple() == digits
    return _naming.one_of(kind, _EXACT) and type(mine) is kind and mine == theirs


def _reshaped(mine: object, theirs: object, trail: object) -> str:
    """Say that a part differs, by its place and the two types, without
    showing either value.
    """
    held, read = _type_name(mine), _type_name(theirs)
    other = f"one of type {read}" if held != read else f"another value of type {read}"
    return (
        f"holds a value of type {held} at {_path(trail)}, which reads back as {other}"
    )


def _type_name(value: object) -> str:
    kind = type(value)
    return "dict" if kind is _Fields else _naming.qualname(kind)


def _path(trail: object) -> str:
    """The path that a trail of (the trail before, a step) pairs spells."""
    steps: list[str] = []
    while trail is not None:
        trail, step = typing.cast(tuple[object, str], trail)
        steps.append(step)
    return "".join(reversed(steps))


def _nodes(schema: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every node of a core schema, each once."""
    pending: list[object] = [schema]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if type(item) is dict:
            yield item
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)


def _check_reused(node: Mapping[str, Any]) -> None:
    """Refuse a record whose stored serializer or validator pydantic-core would
    reuse although it was not built from the record's own core schema.

    pydantic-core reuses them only from a class whose namespace marks it
    complete. It takes the serializer when it is a ``SchemaSerializer``, and
    the validator when it is a ``SchemaValidator`` or, from Pydantic 2.14,
    when it exposes one as ``__pydantic_schema_validator__``, as the wrapper
    Pydantic builds for its plugins does. What each was built from is read
    from its pickled form.
    """
    cls = node.get("cls")
    if not issubclass(type(cls), type):
        return
    name = _naming.name(typing.cast(type, cls))
    own = _naming.namespace(cls)
    if _naming.entry(own, "__pydantic_complete__") is not True:
        return
    schema = _naming.entry(own, "__pydantic_core_schema__")
    # What pydantic-core reuses was built from the class's own schema, so the
    # class's model node there must be the node the description was written
    # from, apart from its reference name, which an alias changes.
    if _model_node(schema, cls) != _without_ref(node):
        raise TypeError(
            f"record {name} has a core schema that differs from the one built "
            "into a record that uses it, as after a rebuild; rebuild the "
            "records that use it after it"
        )
    serializer = own.get("__pydantic_serializer__")
    if (
        type(serializer) is not SchemaSerializer
        or _built_from(serializer) is not schema
    ):
        raise TypeError(
            f"record {name} has a serializer that Pydantic did not build from "
            "its core schema"
        )
    validator = own.get("__pydantic_validator__")
    if type(validator) is not SchemaValidator:
        try:
            validator = getattr(validator, "__pydantic_schema_validator__", None)
        except Exception:  # pydantic-core reuses nothing then
            return
    if type(validator) is SchemaValidator and _built_from(validator) is not schema:
        raise TypeError(
            f"record {name} has a validator that Pydantic did not build from "
            "its core schema"
        )


def _without_ref(node: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in node.items() if key != "ref"}


def _model_node(schema: object, cls: object) -> dict[str, Any] | None:
    """The model node for ``cls`` in a class's own core schema, without its ref."""
    if type(schema) is not dict:
        return None
    for node in _nodes(schema):
        if node.get("type") in ("model", "dataclass") and node.get("cls") is cls:
            return _without_ref(node)
    return None


def _built_from(built: SchemaSerializer | SchemaValidator) -> object:
    """The core schema a serializer or validator was built from.

    Both pickle as their class and the schema and config they were built from
    (``__reduce__``), at pydantic-core 2.41.1 and in Pydantic 2.14.3.
    """
    reduced = built.__reduce__()
    arguments = reduced[1] if type(reduced) is tuple and len(reduced) > 1 else None
    if type(arguments) is not tuple or not arguments:
        return None
    return arguments[0]


def _kind(value: object) -> str:
    """Name what a value is, for a message, without reading the value."""
    if value is None:
        return "None"
    kind = type(value)
    if issubclass(kind, type):
        return f"the class {_naming.qualname(value)}"
    return f"a {_naming.qualname(kind)}"
