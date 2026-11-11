"""How values cross a call: as JSON text, through the declared type (spec 02).

Every request, reply and error detail crosses as JSON text, in-process too
(ADR 0007). A sender takes a value of exactly its declared record, and writes
it with the serializer Pydantic built from the record's core schema, with
warnings as errors. A receiver reads the text with the validator built from
the same schema, so what it hands on shares no object with the sender. A
record's own methods, such as ``model_dump_json`` or ``model_validate_json``,
are never called, so they cannot change what crosses.

Both are captured from the record when the contract is declared, from the
same build whose core schema the description was written from.
"""

import typing
from typing import final

from pydantic import BaseModel
from pydantic_core import SchemaSerializer, SchemaValidator

if typing.TYPE_CHECKING:
    from neolith._contracts import Action, Contract

__all__ = ["Codec", "check_request", "codec"]


@final
class Codec[R: BaseModel]:
    """The serializer and validator of one record, as declared in a contract."""

    __slots__ = ("_serializer", "_validator", "record")

    def __init__(self, record: type[R]) -> None:
        # Read from the class itself, as Pydantic set them when it built the
        # record; anything else is refused, never used.
        serializer = vars(record).get("__pydantic_serializer__")
        validator = vars(record).get("__pydantic_validator__")
        if type(serializer) is not SchemaSerializer:
            raise TypeError(
                f"record {record.__name__} has no serializer built by Pydantic"
            )
        if type(validator) is not SchemaValidator and not _plugin_wrapper(validator):
            raise TypeError(
                f"record {record.__name__} has no validator built by Pydantic"
            )
        self.record = record
        self._serializer = serializer
        self._validator = typing.cast(SchemaValidator, validator)

    def write(self, value: object, subject: str) -> bytes:
        """Write a value of exactly the record's type as JSON text.

        ``subject`` names the value in a message, such as "the reply". A value
        of another type is a ``TypeError``. A value holding something its type
        does not hold at that place, or that JSON cannot hold, such as a
        record that contains itself, is a ``ValueError``.
        """
        if type(value) is not self.record:
            raise TypeError(
                f"{subject} is {_kind(value)}, not exactly a {self.record.__name__}"
            )
        try:
            return self._serializer.to_json(value, warnings="error")
        except Exception as error:
            raise ValueError(
                f"{subject} cannot be written as JSON by its type, "
                f"{self.record.__name__} ({type(error).__name__}; see the cause)"
            ) from error

    def read(self, text: bytes) -> R:
        """Read JSON text into a new value of the record, ignoring unknown fields.

        Raises what the validator raises: ``pydantic.ValidationError``, or an
        exception from a validator of the record.
        """
        return typing.cast(R, self._validator.validate_json(text))


def codec[R: BaseModel](contract: "Contract", record: type[R]) -> Codec[R]:
    """The codec a contract captured for one of its request, reply or detail records."""
    return typing.cast(Codec[R], contract._codecs[record])


def check_request(contract: "Contract", action: "Action", request: object) -> bytes:
    """Check a request before it is sent, and return its JSON text.

    This is the request check of ADR 0017 Decision 9, step 2, in its order:
    the request's exact type, writing it, and reading the text back with the
    request type. A failure is a mistake in the calling code, so it raises
    ``TypeError`` or ``ValueError``, never a ``ContractError``. An exception
    that is not an ``Exception`` propagates unchanged.
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


def _plugin_wrapper(validator: object) -> bool:
    """Whether a validator is Pydantic's own, wrapped for installed plugins.

    With a Pydantic plugin installed, such as an observability one, Pydantic
    wraps every record's validator so that the plugin sees each validation
    (``create_schema_validator`` in pydantic/plugin/_schema_validator.py, at
    2.12.0 and 2.14.3). That module is private, so the wrapper is known by its
    class's name and package. It validates with the ``SchemaValidator`` it
    builds from the record's core schema, and the plugin only observes.
    """
    kind = type(validator)
    return kind.__qualname__ == "PluggableSchemaValidator" and str(
        kind.__module__
    ).startswith("pydantic.plugin.")


def _kind(value: object) -> str:
    """Name what a value is, for a message, without reading the value."""
    if value is None:
        return "None"
    kind = type(value)
    if issubclass(kind, type):
        return f"the class {typing.cast(type, value).__qualname__}"
    return f"a {kind.__qualname__}"
