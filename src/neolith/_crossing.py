"""How values cross a call: as JSON text, through the declared type (spec 02).

Every request, reply and error detail crosses as JSON text, in-process too
(ADR 0007). When a contract is declared, neolith builds a serializer and a
validator for each of its records from the core schema the description was
written from, with the record's config, as Pydantic does. A sender takes a
value of exactly its declared record and writes it with that serializer, with
warnings as errors. A receiver reads the text with that validator, so what it
hands on shares no object with the sender. Nothing set on a record class
afterwards, and none of its own methods, such as ``model_dump_json``, change
what crosses.

pydantic-core reuses, for each record a schema names, the serializer and
validator Pydantic stored on that record's class, when the class is complete
(``src/common/prebuilt.rs``, at 2.41.1 and in Pydantic 2.14.3). So a record
is admitted only when what it would reuse was built from the record's own
core schema.
"""

import math
import typing
from collections.abc import Iterator, Mapping
from typing import Any, final

from pydantic import BaseModel
from pydantic_core import SchemaSerializer, SchemaValidator

if typing.TYPE_CHECKING:
    from neolith._contracts import Action, Contract

__all__ = ["Codec", "check_request", "codec"]

# Core schema nodes whose values can be floats: a number, or an untyped value.
_FLOAT_NODES = frozenset({"float", "any"})


@final
class Codec[R: BaseModel]:
    """The serializer and validator of one record, as declared in a contract."""

    __slots__ = ("_floats", "_serializer", "_validator", "record")

    def __init__(
        self,
        record: type[R],
        schema: Mapping[str, Any],
        config: Mapping[str, Any] | None,
    ) -> None:
        """Build from the record's core schema and the config of its model node.

        Raises ``TypeError`` when a record the schema names would bring a
        serializer or validator not built from its own core schema.
        """
        floats = False
        for node in _nodes(schema):
            kind = node.get("type")
            if kind in ("model", "dataclass"):
                _check_reused(node.get("cls"))
            floats = floats or kind in _FLOAT_NODES
        try:
            core: Any = schema
            settings: Any = config
            serializer = SchemaSerializer(core, settings)
            validator = SchemaValidator(core, settings)
        except Exception as error:
            raise TypeError(
                f"record {record.__name__} cannot be built from its core schema "
                f"({type(error).__name__}: {error})"
            ) from error
        self.record = record
        self._serializer = serializer
        self._validator = validator
        self._floats = floats

    def write(self, value: object, subject: str) -> bytes:
        """Write a value of exactly the record's type as JSON text.

        ``subject`` names the value in a message, such as "the reply". A value
        of another type is a ``TypeError``. A value holding something its type
        does not hold at that place, or that JSON cannot hold, such as a
        record that contains itself or a number that is not finite, is a
        ``ValueError``.
        """
        if type(value) is not self.record:
            raise TypeError(
                f"{subject} is {_kind(value)}, not exactly a {self.record.__name__}"
            )
        try:
            text = self._serializer.to_json(value, warnings="error")
            # pydantic-core writes a number that is not finite as null, so
            # the value as the serializer sees it is checked for one.
            found = (
                _non_finite(self._serializer.to_python(value, warnings=False))
                if self._floats
                else None
            )
        except Exception as error:
            raise ValueError(
                f"{subject} cannot be written as JSON by its type, "
                f"{self.record.__name__} ({type(error).__name__}; see the cause)"
            ) from error
        if found is not None:
            raise ValueError(
                f"{subject} cannot be written as JSON by its type, "
                f"{self.record.__name__}: it holds {found}, and a number "
                "must be finite"
            )
        return text

    def read(self, text: bytes) -> R:
        """Read JSON text into a new value of the record, ignoring unknown fields.

        Raises what the validator raises: ``pydantic.ValidationError``, or an
        exception from a validator of the record.
        """
        return typing.cast(R, self._validator.validate_json(text))


def codec[R: BaseModel](contract: "Contract", record: type[R]) -> Codec[R]:
    """The codec a contract built for one of its request, reply or detail records."""
    return typing.cast(Codec[R], contract._codecs[record])


def check_request(contract: "Contract", action: "Action", request: object) -> bytes:
    """Check a request before it is sent, and return its JSON text.

    This is the request check of ADR 0017 Decision 9, step 2, in its order,
    without the description check, which comes with #10 task 3: the request's
    exact type, writing it, and reading the text back with the request type.
    A failure is a mistake in the calling code, so it raises ``TypeError`` or
    ``ValueError``, never a ``ContractError``. An exception that is not an
    ``Exception`` propagates unchanged.
    """
    request_codec = codec(contract, action.request)
    subject = f"{contract.identity}, action {action.name!r}: the request"
    text = request_codec.write(request, subject)
    try:
        request_codec.read(text)
    except Exception as error:
        raise ValueError(
            f"{subject} does not read back as a {action.request.__name__} "
            f"({type(error).__name__}; see the cause)"
        ) from error
    return text


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


def _check_reused(cls: object) -> None:
    """Refuse a record whose stored serializer or validator pydantic-core would
    reuse although it was not built from the record's own core schema.

    pydantic-core reuses them only from a class whose namespace marks it
    complete. It takes the serializer when it is a ``SchemaSerializer``, and
    the validator when it is a ``SchemaValidator`` or, from Pydantic 2.14,
    when it exposes one as ``__pydantic_schema_validator__``, as the wrapper
    Pydantic builds for its plugins does. What each was built from is read
    from its pickled form.
    """
    if not issubclass(type(cls), type):
        return
    name = typing.cast(type, cls).__name__
    own = vars(typing.cast(type, cls))
    if own.get("__pydantic_complete__") is not True:
        return
    schema = own.get("__pydantic_core_schema__")
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


def _non_finite(value: object) -> str | None:
    """Name the first number in a plain value that is not finite, or None."""
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        kind = type(item)
        if issubclass(kind, float):
            if not math.isfinite(typing.cast(float, item)):
                return repr(item)
        elif kind in (list, tuple, set, frozenset, dict):
            if id(item) in seen:
                continue
            seen.add(id(item))
            if kind is dict:
                pending.extend(typing.cast(dict[object, object], item).values())
            else:
                pending.extend(typing.cast(list[object], item))
    return None


def _kind(value: object) -> str:
    """Name what a value is, for a message, without reading the value."""
    if value is None:
        return "None"
    kind = type(value)
    if issubclass(kind, type):
        return f"the class {typing.cast(type, value).__qualname__}"
    return f"a {kind.__qualname__}"
