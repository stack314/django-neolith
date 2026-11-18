"""The portable type system of spec 01, admitted from Pydantic's core schema.

Pydantic validates and serialises a record from its core schema, not from its
annotations: the model's validator and serializer are both built from
``__pydantic_core_schema__``. So the core schema is what travels. This module
walks it and admits only the node types and keys that the portable forms
produce. Anything else, whether a node type, a key on a node, a model setting
or a default that does not survive a JSON round trip, is a
``ContractDefinitionError`` that names the contract, the action and the field
(CON-17).

The same walk writes the neutral description (CON-16), with each form's wire
encoding (ADR 0019) stated as a pattern or a range. It never reads
Pydantic's JSON Schema generator. ``WithJsonSchema``, ``json_schema_extra``,
``__get_pydantic_json_schema__`` and similar hooks only change what that
generator writes, so they cannot change the description. Records and enums
are described once each under ``$defs``, by class name; ``type`` aliases are
written out where they are used.
"""

import copy
import enum
import inspect
import json
import math
import re
import sys
import types
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, PrivateAttr
from pydantic.errors import PydanticUndefinedAnnotation, PydanticUserError
from pydantic_core import (
    SchemaSerializer,
    SchemaValidator,
    core_schema,
)

from neolith import _naming
from neolith._forms import FORM_FOR_KIND, form_in, form_of, fullmatch
from neolith._naming import shown

# A record or enum name, as other languages' generated code will use it
# (CON-20).
_TYPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# The wire encodings of spec 01 (ADR 0019) that Pydantic's own types write.
# Integers are those every JSON reader holds exactly; numbers are finite
# doubles. Each pattern is ASCII-only and anchored with ^...$, and matches the
# same strings under ECMA-262, Rust regex and Python's re.fullmatch. Durations,
# decimals and bytes are neolith's own types (neolith._forms).
SAFE_INTEGER_MAX = 2**53 - 1
DOUBLE_MAX = sys.float_info.max
_DATE = (
    r"(000[1-9]|00[1-9][0-9]|0[1-9][0-9]{2}|[1-9][0-9]{3})"
    r"-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
)
DATE_PATTERN = f"^{_DATE}$"
TIMESTAMP_PATTERN = (
    f"^{_DATE}"
    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?"
    r"(Z|[+\-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)

# Keys any node may carry without changing what travels: Pydantic's id for the
# node, and metadata (read only by Pydantic's JSON Schema generator). Strict
# mode is not admitted: it refuses JSON 1.0 for an integer, which the
# description admits, and behind a before validator it reads the JSON value by
# Python rules, which refuse text for bytes, dates and timestamps.
_ANY_NODE = frozenset({"type", "ref", "metadata"})

# A constraint is admitted only where pydantic-core and JSON Schema give it the
# same meaning. A float's multiple_of is not: pydantic-core allows a tolerance
# (1e-9) and JSON Schema divides exactly, so 1.0000000001 passes one and fails
# the other. A pattern is not either: pydantic-core matches it with Rust regex,
# JSON Schema with ECMA-262 and many validators with Python re, which differ on
# anchors (Python's "$" matches before a final newline), classes ("\d" is
# Unicode in Rust and ASCII in ECMA-262), Unicode properties and inline flags.
_RANGE_BOUNDS = {
    "gt": "exclusiveMinimum",
    "ge": "minimum",
    "lt": "exclusiveMaximum",
    "le": "maximum",
}
_INTEGER_BOUNDS = {**_RANGE_BOUNDS, "multiple_of": "multipleOf"}
_STRING_BOUNDS = {"min_length": "minLength", "max_length": "maxLength"}
_ITEM_BOUNDS = {"min_length": "minItems", "max_length": "maxItems"}
_ENTRY_BOUNDS = {"min_length": "minProperties", "max_length": "maxProperties"}

# The portable scalars: core schema type -> (description, constraint keys and
# the JSON Schema keyword each becomes, other keys admitted). A constraint is
# admitted only where it sits on the node it constrains: a constraint written
# after a validator becomes an after validator of its own
# (pydantic/_internal/_known_annotated_metadata.py), which is refused below. A
# constraint without a JSON Schema keyword of the same meaning, such as a
# decimal's max_digits, is not admitted, so that the description states every
# rule the provider enforces.
#
# Each description states the form's wire encoding. A duration, a decimal and
# bytes are not among them: Pydantic writes them otherwise, so they are
# neolith's own types (neolith._forms), and the plain types are refused.
_SCALARS: dict[str, tuple[dict[str, Any], Mapping[str, str], frozenset[str]]] = {
    "bool": ({"type": "boolean"}, {}, frozenset()),
    "int": (
        {"type": "integer", "minimum": -SAFE_INTEGER_MAX, "maximum": SAFE_INTEGER_MAX},
        _INTEGER_BOUNDS,
        frozenset(),
    ),
    # Infinity and NaN are admitted on the way in, as Pydantic reads them by
    # default; the description's range refuses them, and a sender does not
    # send them (spec 01).
    "float": (
        {"type": "number", "minimum": -DOUBLE_MAX, "maximum": DOUBLE_MAX},
        _RANGE_BOUNDS,
        frozenset({"allow_inf_nan"}),
    ),
    # strip_whitespace, to_lower and to_upper convert values on the way in;
    # they are refused next to a length (see Portable.transforms).
    "str": (
        {"type": "string"},
        _STRING_BOUNDS,
        frozenset({"strip_whitespace", "to_lower", "to_upper"}),
    ),
    "date": (
        {"type": "string", "format": "date", "pattern": DATE_PATTERN},
        {},
        frozenset(),
    ),
    "datetime": (
        {"type": "string", "format": "date-time", "pattern": TIMESTAMP_PATTERN},
        {},
        frozenset({"tz_constraint", "microseconds_precision"}),
    ),
}

# A before validator runs on the input, and the schema it wraps then checks
# what it returns, so the value that travels is still one the description
# states, with one exception: a record instance. pydantic-core keeps an
# instance of the record's class, or of a subclass, as it is, without
# checking it again (revalidate_instances defaults to "never"), so a before
# validator that returns one, for example from model_construct, passes it
# through unchecked. Before validators are therefore admitted on fields whose
# type contains no record, and refused on records and on fields whose type
# contains a record at any depth: a list item, a map value, an optional value
# or an alias. A record instance that a provider builds itself, such as a
# subclass with looser fields, is still kept unchecked; the sender's check
# is what refuses it (neolith._crossing, Codec.send). An after or
# wrap validator returns a value after the check, which nothing checks again,
# and a plain validator replaces the schema; both are refused. A before
# validator must also be idempotent: the value it returns travels, and is
# passed through it again when read back.
_VALIDATORS = frozenset({"function-before"})
_UNCHECKED_VALIDATORS = frozenset({"function-after", "function-wrap"})
# json_schema_input_schema only changes what Pydantic's JSON Schema generator
# writes in validation mode, which the description never reads.
_VALIDATOR_KEYS = frozenset({"function", "schema", "json_schema_input_schema"})
# The keys under which a node holds the nodes that check parts of its value.
_CHILDREN = ("schema", "items_schema", "keys_schema", "values_schema")
# The keys of the nodes that only step to another node.
_WRAPPER_KEYS = {
    "definitions": frozenset({"schema", "definitions"}),
    "definition-ref": frozenset({"schema_ref"}),
}

# Model settings that change validation only. Anything else that reaches the
# core config, such as ser_json_timedelta or val_json_bytes, changes the wire.
_SETTINGS = frozenset(
    {
        "title",
        "revalidate_instances",
        "validate_default",
        "from_attributes",
        "loc_by_alias",
        "hide_input_in_errors",
        "validation_error_cause",
        "str_strip_whitespace",
        "str_to_lower",
        "str_to_upper",
    }
)

# The description of a map key: plain text, maybe with lengths, or a literal.
_KEY_DESCRIPTION = frozenset({"type", "minLength", "maxLength", "enum"})

# What an unadmitted key means, for the message.
_KEY_MEANINGS = {
    "serialization": (
        "has a custom serializer (a field or model serializer, PlainSerializer, "
        "WrapSerializer, SerializeAsAny, or a setting such as use_enum_values)"
    ),
    "serialization_exclude": "has an exclusion from serialization",
    "serialization_exclude_if": "has an exclusion from serialization",
    "validation_alias": "has an alias",
    "serialization_alias": "has an alias",
    "post_init": "defines model_post_init, which can change values after validation",
    "generic_origin": "is a parametrised generic; declare a record for each use",
    "pattern": (
        "sets a pattern, which is not admitted: pydantic-core matches it with "
        "Rust regex and JSON Schema with ECMA-262, and the two differ"
    ),
    "strict": (
        "reads in strict mode, which refuses JSON values the description "
        "admits: 1.0 for an integer, and, behind a before validator, text for "
        "bytes, dates and timestamps"
    ),
    "multiple_of": (
        "sets multiple_of on a float, which pydantic-core checks with a "
        "tolerance and JSON Schema checks exactly"
    ),
}


Ref = dict[str, str]


class Portable:
    """Admit the request and reply types of one contract, and describe them.

    One instance serves one contract, so that record and enum names are checked
    across all its actions (CON-15).
    """

    def __init__(self, error: type[Exception]) -> None:
        self.error = error
        self.where = ""
        self.named: dict[str, type[Any]] = {}
        self.defs: dict[str, dict[str, Any]] = {}
        self.definitions: dict[str, Mapping[str, Any]] = {}
        # The core config of the record being walked. pydantic-core applies a
        # record's settings to every node of its fields, so a node is judged
        # together with it, not by its own keys alone.
        self.config: Mapping[str, Any] = {}
        # Each request, reply and detail record, with the core schema the
        # walk read for it and the config of its model node, which Pydantic
        # also passes when it builds the record's serializer and validator
        # (pydantic/_internal/_model_construction.py, complete_model_class).
        self.built: dict[
            type[BaseModel], tuple[Mapping[str, Any], Mapping[str, Any] | None]
        ] = {}

    def fail(self, message: str) -> Exception:
        return self.error(f"{self.where}: {message}")

    def root(
        self, annotation: object, role: Literal["request", "reply", "detail"]
    ) -> tuple[type[BaseModel], Ref]:
        """Admit a request, reply or detail type; return it and its reference."""
        # Judged by type() and the class's own method resolution order, so
        # that no code of the annotation, or of its metaclass, runs.
        if issubclass(type(annotation), type) and _naming.derives(
            cast(type, annotation), BaseModel
        ):
            model: type[BaseModel] = annotation  # type: ignore[assignment]  # as above
            record_name = _naming.name(model, None)
            try:
                schema = self.build(model)
                node = self.resolve(schema, record_name)
                # A record's before or wrap model_validator wraps the model
                # node itself.
                while node.get("type") in _VALIDATORS | _UNCHECKED_VALIDATORS:
                    self.validator(node, f"record {record_name}")
                    node = self.resolve(node["schema"], record_name)
                if node.get("type") == "model" and node.get("cls") is model:
                    reference = self.record(node, record_name)
                    self.built[model] = (schema, node.get("config"))
                    return model, reference
            except (KeyError, AttributeError, TypeError) as error:
                # Only this walk's own lookups raise these; an author's code
                # runs inside round_trip, which names its errors itself.
                if isinstance(error, self.error):
                    raise
                # Fail closed: a core schema shape this walk does not read is
                # refused, never passed.
                raise self.fail(
                    f"record {record_name} has a core schema shape that "
                    f"neolith does not read ({_naming.cause(error, None)})"
                ) from error
        empty = ", or None for an empty reply" if role == "reply" else ""
        raise self.fail(
            f"the {role} type must be a record (a pydantic model with named "
            f"fields){empty}, not {_annotation(annotation)}"
        )

    def build(self, model: type[BaseModel]) -> Mapping[str, Any]:
        """Return the model's core schema, building it first if Pydantic deferred it.

        ``model_rebuild`` resolves names in the caller's frame unless a types
        namespace is given (pydantic/main.py, ``BaseModel.model_rebuild``), so
        an empty one keeps resolution to the record's own module.
        """
        schema = model.__pydantic_core_schema__
        if not model.__pydantic_complete__ or not isinstance(schema, dict):
            try:
                model.model_rebuild(force=True, raise_errors=True, _types_namespace={})
            except PydanticUndefinedAnnotation as error:
                raise self.fail(
                    f"record {_naming.name(model)}, or a record it uses, refers to "
                    f"{error.name!r}, which the module of that record does not "
                    "define when the contract is declared; define "
                    f"{error.name} at module level there, before the contract, or "
                    f"call that record's model_rebuild() once {error.name} is "
                    "defined"
                ) from error
            except PydanticUserError as error:
                raise self.fail(
                    f"record {_naming.name(model)} cannot be built: "
                    f"{_naming.text(error, None)}"
                ) from error
            schema = model.__pydantic_core_schema__
        self.collect(schema)
        return schema

    def collect(self, schema: object) -> None:
        """Index every core schema definition by its reference."""
        pending = [schema]
        while pending:
            node = pending.pop()
            # Judged by type(): the schema holds each field's default, and
            # isinstance would read a default's own __class__.
            if type(node) is dict:
                if node.get("type") == "definitions":
                    for definition in node.get("definitions", []):
                        self.definitions[definition["ref"]] = definition
                pending.extend(node.values())
            elif type(node) is list:
                pending.extend(node)

    def resolve(self, node: Mapping[str, Any], path: str) -> Mapping[str, Any]:
        """Step through a ``definitions`` wrapper or a reference to its target.

        Both carry keys too: a reference can carry its own ``serialization``,
        as ``SerializeAsAny`` does, so their keys are checked like any node's.
        """
        while True:
            kind = node.get("type")
            if kind == "definitions":
                self.keys(node, frozenset({"schema", "definitions"}), f"field {path}")
                node = node["schema"]
            elif kind == "definition-ref":
                self.keys(node, frozenset({"schema_ref"}), f"field {path}")
                target = self.definitions.get(str(node.get("schema_ref")))
                if target is None:
                    raise self.fail(
                        f"field {path} refers to a type that cannot be found"
                    )
                return target
            else:
                return node

    def keys(
        self, node: Mapping[str, Any], admitted: frozenset[str], subject: str
    ) -> None:
        """Reject any key on a core schema node that the node's form does not admit."""
        for key in node:
            if key in _ANY_NODE or key in admitted:
                continue
            meaning = _KEY_MEANINGS.get(
                key, f"sets {key!r}, which is outside the portable forms"
            )
            raise self.fail(f"{subject} {meaning}")

    def register(self, named: type[Any], kind: str, path: str) -> bool:
        """Check a record or enum name; False if this class was seen already."""
        name = _naming.name(named, None)
        if not _TYPE_NAME.fullmatch(name):
            raise self.fail(
                f"{kind} name {name!r} at {path} must start with an ASCII letter, "
                "followed by ASCII letters, digits or underscores"
            )
        known = self.named.get(name)
        if known is named:
            return False
        if known is not None:
            raise self.fail(
                f"two different records or enums are named {name!r}: "
                f"{_naming.full_name(known)} and {_naming.full_name(named)}"
            )
        self.named[name] = named
        return True

    def record(self, node: Mapping[str, Any], path: str) -> Ref:
        """Admit a record and describe it once under ``$defs``."""
        cls = node["cls"]
        subject = f"record {path}"
        hook = _class_hook(cls)
        if hook is not None:
            raise self.fail(
                f"{subject} {hook}, which can change what is read from a value "
                "of it or built as one; a record keeps Pydantic's own"
            )
        admitted = {"cls", "schema", "config", "custom_init", "root_model"}
        if "post_init" in node and not (
            type(node["post_init"]) is str and node["post_init"] == "model_post_init"
        ):
            raise self.fail(
                f"{subject} runs a post-init other than model_post_init, which "
                "can change values after validation"
            )
        if _only_initialises_private_attributes(cls):
            # Private attributes never travel; Pydantic sets post_init to
            # initialise them when the record has any.
            admitted.add("post_init")
        self.keys(node, frozenset(admitted), subject)
        private = _private_attribute_problem(cls)
        if private is not None:
            raise self.fail(f"{subject} {private}")
        if node.get("root_model"):
            raise self.fail(f"{subject} is a root model; a record has named fields")
        if node.get("custom_init"):
            # pydantic-core calls a custom __init__ instead of building the
            # record itself, so whatever it sets after validation travels
            # unchecked.
            raise self.fail(
                f"{subject} defines __init__, which can change values after "
                "validation; use a before model_validator instead"
            )
        for key, value in node.get("config", {}).items():
            if key == "extra_fields_behavior":
                if value != "ignore":
                    raise self.fail(
                        f"{subject} sets extra={value!r}; a record must ignore "
                        "unknown fields so that adding a field stays compatible"
                    )
            elif key in _KEY_MEANINGS:
                raise self.fail(f"{subject} {_KEY_MEANINGS[key]}")
            elif key not in _SETTINGS:
                raise self.fail(
                    f"{subject} has {key} set, which changes how it travels"
                )
        reference = {"$ref": f"#/$defs/{_naming.name(cls, None)}"}
        if not self.register(cls, "record", path):
            return reference
        self.defs[_naming.name(cls, None)] = {}
        outer, self.config = self.config, node.get("config", {})
        try:
            return self.fields(node, cls, path, subject, reference)
        finally:
            self.config = outer

    def fields(
        self,
        node: Mapping[str, Any],
        cls: type[Any],
        path: str,
        subject: str,
        reference: Ref,
    ) -> Ref:
        """Describe a record's fields, under the record's own settings."""
        fields_node = node["schema"]
        while fields_node.get("type") in _VALIDATORS | _UNCHECKED_VALIDATORS:
            self.validator(fields_node, subject)
            fields_node = fields_node["schema"]
        if fields_node.get("type") != "model-fields":
            raise self.fail(f"{subject} is not built from named fields")
        self.keys(
            fields_node, frozenset({"fields", "model_name", "computed_fields"}), subject
        )
        if fields_node.get("computed_fields"):
            raise self.fail(f"{subject} has computed fields")
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, field in fields_node["fields"].items():
            field_path = f"{path}.{name}"
            self.keys(field, frozenset({"schema", "frozen"}), f"field {field_path}")
            properties[name], has_default = self.field(field["schema"], field_path)
            if not has_default:
                required.append(name)
        description: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            description["required"] = required
        self.defs[_naming.name(cls, None)] = description
        return reference

    def field(self, node: Mapping[str, Any], path: str) -> tuple[dict[str, Any], bool]:
        """Describe a field; True if it has a default."""
        if node.get("type") != "default":
            return self.describe(node, path, ()), False
        self.keys(
            node,
            frozenset(
                {
                    "schema",
                    "default",
                    "default_factory",
                    "default_factory_takes_data",
                    "validate_default",
                }
            ),
            f"field {path}",
        )
        description = self.describe(node["schema"], path, ())
        if "default" in node:
            default = node["default"]
            problem = _default_problem(default)
            if problem is not None:
                raise self.fail(
                    f"field {path} has a default holding {problem}, which "
                    "pydantic-core might not copy for each value read without "
                    "the field; build it from plain lists, dicts and sets, "
                    "immutable values and records that keep Pydantic's own "
                    "methods"
                )
            # pydantic-core copies a default only when it cannot be hashed
            # (src/validators/with_default.rs), so it hands a hashable one to
            # every value read without the field as the same object. That is
            # safe only for values that cannot change, and a record always
            # can: freezing it leaves its private attributes and its set of
            # fields given open.
            if _hashable(default) and not _unchanging(default):
                raise self.fail(
                    f"field {path} has a default that every value read without "
                    "the field would share, since it can be hashed, and that "
                    "holds a record, which can change; give the field a "
                    "default_factory that builds the record, such as a lambda"
                )
        if "default_factory" in node:
            if node.get("default_factory_takes_data"):
                raise self.fail(
                    f"field {path} has a default factory that takes the other fields"
                )
            factory = node["default_factory"]
            try:
                # pydantic-core calls the factory once for each value read
                # without the field, so two calls must build values that
                # share nothing that can change.
                made = factory()
                again = factory()
            except Exception as error:  # the author's factory may raise anything
                raise self.fail(
                    f"field {path} has a default factory that raised "
                    f"{_naming.cause(error, None)}"
                ) from error
            problem = _default_problem(made)
            if problem is not None:
                raise self.fail(
                    f"field {path} has a default factory that builds a value "
                    f"holding {problem}; build it from plain lists, dicts and "
                    "sets, immutable values and records that keep Pydantic's "
                    "own methods"
                )
            shared = _shared_between(made, again)
            if shared is not None:
                raise self.fail(
                    f"field {path} has a default factory that returns the same "
                    f"{shared} on every call, which every value read without "
                    "the field would share; make it build a new one on each call"
                )
            self.round_trip(node["schema"], made, path, description)
            return description, True
        return {
            **description,
            "default": self.round_trip(
                node["schema"], node["default"], path, description
            ),
        }, True

    def round_trip(
        self,
        schema: Mapping[str, Any],
        value: object,
        path: str,
        description: Mapping[str, Any],
    ) -> object:
        """Return a default's JSON form if it reads back as itself.

        Pydantic sends a default as written: it is not validated unless the
        field sets ``validate_default`` (pydantic/fields.py, ``FieldInfo``).
        So the default must already be in the form its type reads back,
        exactly, as JSON text: ``Decimal`` for a decimal, not ``0``, and
        ``1.0`` rather than ``1`` wherever ``1`` would not read back as ``1``.
        What Pydantic writes must also be in its wire encoding, which
        ``description`` states: ``0.0``, not ``-0.0``.
        """
        if self.definitions:
            schema = core_schema.definitions_schema(
                schema,
                list(self.definitions.values()),
            )
        # The record's settings apply to the default too, as they do when
        # Pydantic sends and reads the record.
        config: Any = dict(self.config) or None
        serializer = SchemaSerializer(schema, config)
        validator = SchemaValidator(schema, config)
        # Pydantic keeps a default as written in the record it builds, and may
        # revalidate that record strictly later (revalidate_instances), so the
        # default must already be a value of its type, unchanged by validation.
        # An int is a value of a float field in strict mode too.
        try:
            held = validator.validate_python(value, strict=True)
        except (ValueError, TypeError) as error:
            raise self.fail(
                f"field {path} has the default {shown(value)}, which is not a value of "
                "its type; Pydantic keeps a default as written, so write one"
            ) from error
        except Exception as error:  # an author's validator may raise anything
            raise self.fail(
                f"field {path} has the default {shown(value)}, on which its validators "
                f"raised {_naming.cause(error, None)}"
            ) from error
        if not _unchanged(held, value):
            raise self.fail(
                f"field {path} has the default {shown(value)}, which its validators "
                f"turn into {shown(held)}; write the default as {shown(held)}"
            )
        try:
            # Pydantic serialises a default it was given as written, with the
            # field's own serializer; warnings off, since an off-type value
            # only warns and the read-back below catches it.
            sent = serializer.to_json(value, warnings=False)
        except (ValueError, TypeError) as error:
            raise self.fail(
                f"field {path} has the default {shown(value)}, which cannot be written "
                "as JSON: a string with a lone surrogate never can"
            ) from error
        written: object = None
        problem: str | None
        try:
            written = json.loads(sent)
        except ValueError:
            # The json module refuses an integer of more than 4300 digits
            # (sys.set_int_max_str_digits), far outside the integer range.
            problem = f"it holds an integer outside ±{SAFE_INTEGER_MAX}"
        else:
            problem = json_value_problem(written)
        if problem is not None:
            raise self.fail(
                f"field {path} has the default {shown(value)}, which travels as a "
                f"JSON value outside the portable forms: {problem}"
            )
        problem = encoding_problem(description, self.defs, written)
        if problem is not None:
            raise self.fail(
                f"field {path} has the default {shown(value)}, which Pydantic "
                f"writes outside its wire encoding: {problem}"
            )
        try:
            read_value = validator.validate_json(sent)
            read = serializer.to_json(read_value, warnings=False)
        except Exception as error:  # ValidationError, or an author's validator
            raise self.fail(
                f"field {path} has the default {shown(value)}, which travels as "
                f"{sent.decode()} and does not read back as a value of its type"
            ) from error
        if not _unchanged(read_value, held):
            # The wire lost something the value holds, such as the seconds of
            # a timestamp's offset, which RFC 3339 cannot write.
            raise self.fail(
                f"field {path} has the default {shown(value)}, which travels as "
                f"{sent.decode()} and reads back as {shown(read_value)}, so the wire "
                "loses part of it"
            )
        if sent != read:
            raise self.fail(
                f"field {path} has the default {shown(value)}, which travels as "
                f"{sent.decode()} but reads back as {read.decode()}; write the "
                "default so that it reads back as itself"
            )
        return written

    def describe(
        self, node: Mapping[str, Any], path: str, aliases: tuple[str, ...]
    ) -> dict[str, Any]:
        """Admit one type and return its description.

        ``aliases`` holds the references written out on the way here, so that a
        recursive shape that is not a record is caught instead of expanded for
        ever.
        """
        kind = node.get("type")
        subject = f"field {path}"
        self.decorated(node, subject, path)
        if kind == "definitions":
            self.keys(node, frozenset({"schema", "definitions"}), subject)
            return self.describe(node["schema"], path, aliases)
        if kind == "definition-ref":
            reference = node.get("schema_ref", "")
            target = self.resolve(node, path)
            if target.get("type") == "model":
                return self.record(target, path)
            if reference in aliases:
                raise self.fail(
                    f"{subject} has a recursive type that is not a record; a "
                    "recursive shape must be a record"
                )
            return self.describe(target, path, (*aliases, reference))
        if kind in _VALIDATORS | _UNCHECKED_VALIDATORS:
            inner = node.get("schema", {})
            self.validator(node, subject, path)
            return self.describe(inner, path, aliases)
        if kind == "model":
            return self.record(node, path)
        if kind == "enum":
            return self.enum(node, path)
        # Judged without the keys any node may carry (_ANY_NODE), such as the
        # reference Pydantic adds to a type alias.
        form = form_of(
            {key: value for key, value in node.items() if key not in _ANY_NODE}
            | {"type": kind}
        )
        if form is not None:
            return form.description()
        if kind in FORM_FOR_KIND:
            named = FORM_FOR_KIND[kind]
            raise self.fail(
                f"{subject} is a plain {named.python_type.__name__}, which Pydantic "
                f"does not write in its wire form; declare it as neolith.{named.name}"
            )
        if kind in _SCALARS:
            base, bounds, other = _SCALARS[kind]
            self.keys(node, frozenset(bounds) | other, subject)
            if kind == "str":
                self.transforms(node, subject)
            if kind == "float" and "multiple_of" in node:
                raise self.fail(f"{subject} {_KEY_MEANINGS['multiple_of']}")
            if node.get("microseconds_precision", "truncate") != "truncate":
                # "error" refuses more than six fractional digits, which RFC
                # 3339 and the description allow.
                raise self.fail(
                    f"{subject} refuses fractional seconds beyond microseconds, "
                    "which the description admits"
                )
            if kind == "float":
                for name in _RANGE_BOUNDS:
                    bound = node.get(name)
                    if type(bound) is int and float(bound) != bound:
                        raise self.fail(
                            f"{subject} sets {name}={bound}, which a float cannot "
                            "hold exactly, so a reader of the description compares "
                            "against another number; use a bound a float holds"
                        )
            if kind == "datetime" and node.get("tz_constraint") != "aware":
                raise self.fail(
                    f"{subject} is a timestamp, which carries an offset from UTC "
                    "(RFC 3339); annotate it as pydantic.AwareDatetime"
                )
            return _narrowed(base, self.bounds(node, path, bounds))
        if kind == "list":
            self.keys(
                node, frozenset({"items_schema"}) | frozenset(_ITEM_BOUNDS), subject
            )
            if "items_schema" not in node:
                raise self.fail(
                    f"{subject} is a list without an item type, "
                    "which is not a portable form"
                )
            items = self.describe(node["items_schema"], f"{path}[*]", aliases)
            return {
                "type": "array",
                "items": items,
                **self.bounds(node, path, _ITEM_BOUNDS),
            }
        if kind == "dict":
            self.keys(
                node,
                frozenset({"keys_schema", "values_schema"}) | frozenset(_ENTRY_BOUNDS),
                subject,
            )
            if "keys_schema" not in node or "values_schema" not in node:
                raise self.fail(
                    f"{subject} is a map without key and value types, "
                    "which is not a portable form"
                )
            if any(
                reached.get("type") == "str" and self.converts(reached)
                for reached in self.reachable(node["keys_schema"], path)
            ):
                raise self.fail(
                    f"{subject} converts its keys, so two keys can become one; "
                    "map keys are not converted in this version"
                )
            key = self.describe(node["keys_schema"], f"{path} key", aliases)
            # A map's keys are strings (map<string, T>): plain text, maybe
            # with lengths, or a literal. A key of another form, such as a
            # date or a duration, is refused.
            if key.get("type") != "string" or not set(key) <= _KEY_DESCRIPTION:
                raise self.fail(
                    f"{subject} is a map whose keys are not strings, "
                    "which is not a portable form"
                )
            values = self.describe(node["values_schema"], f"{path}{{*}}", aliases)
            description: dict[str, Any] = {
                "type": "object",
                "additionalProperties": values,
            }
            names = {k: v for k, v in key.items() if k != "type"}
            if names:
                description["propertyNames"] = names
            return {**description, **self.bounds(node, path, _ENTRY_BOUNDS)}
        if kind == "nullable":
            self.keys(node, frozenset({"schema"}), subject)
            return {
                "anyOf": [
                    self.describe(node["schema"], path, aliases),
                    {"type": "null"},
                ]
            }
        if kind == "literal":
            self.keys(node, frozenset({"expected"}), subject)
            expected = node.get("expected", [])
            if expected and all(type(value) is str for value in expected):
                return {"type": "string", "enum": list(expected)}
            raise self.fail(
                f"{subject} is a literal of values other than strings, "
                "which is not a portable form"
            )
        raise self.fail(f"{subject} has type {kind}, which is not a portable form")

    def converts(self, node: Mapping[str, Any]) -> list[str]:
        """The conversions pydantic-core applies to a string node, with settings."""
        own = [
            key for key in ("strip_whitespace", "to_lower", "to_upper") if node.get(key)
        ]
        inherited = [
            key
            for key in ("str_strip_whitespace", "str_to_lower", "str_to_upper")
            if self.config.get(key)
        ]
        return own + inherited

    def transforms(self, node: Mapping[str, Any], subject: str) -> None:
        """Refuse a conversion on a string whose length the description states.

        pydantic-core strips whitespace, then checks the lengths, then converts
        the case. Neither order commutes with a length the description states:
        "a " has length 2 and is refused after stripping, and "ß" passes a
        maximum length of 1 and travels as "SS". The conversion may come from
        the field or from the record's settings.
        """
        converts = self.converts(node)
        constrained = [key for key in ("min_length", "max_length") if key in node]
        if converts and constrained:
            raise self.fail(
                f"{subject} converts its value ({converts[0]}) next to its "
                f"{constrained[0]}, so a value the description admits can be "
                "refused, or travel in a form it refuses; drop the conversion or "
                "the length"
            )

    def reachable(
        self, node: Mapping[str, Any], path: str
    ) -> Iterator[Mapping[str, Any]]:
        """Every node a value placed here can be checked by, at any depth.

        Walks through ``definitions`` wrappers, references, validators, list
        items, map keys and values, nullable and default nodes, keeping a
        seen-set so that a recursive type ends. A rule about whether a kind of
        node can be reached from here asks this walk, never the nearest node,
        so that wrapping the node in a container, an alias or another
        validator does not get around the rule.
        """
        seen: set[int] = set()
        pending = [node]
        while pending:
            current = self.resolve(pending.pop(), path)
            if id(current) in seen:
                continue
            seen.add(id(current))
            yield current
            if current.get("type") in ("model", "model-fields"):
                # A record's own fields are judged when the record is.
                continue
            for key in _CHILDREN:
                child = current.get(key)
                if isinstance(child, dict):
                    pending.append(child)

    def decorated(self, node: Mapping[str, Any], subject: str, path: str) -> None:
        """Refuse a neolith type with anything added to it, naming the type.

        Pydantic applies a constraint such as ``max_length`` in an after
        validator around the type's node, or a chain after it, and a plain
        validator or a serializer replaces part of the node. Only type aliases
        and before validators in front of the type's own node are admitted;
        anything else is refused here, so the message names the type instead
        of a validator or node kind the author never wrote.
        """
        changed = False
        seen: set[int] = set()
        current = node
        steps = _VALIDATORS | _UNCHECKED_VALIDATORS | set(_WRAPPER_KEYS) | {"chain"}
        while current.get("type") in steps and id(current) not in seen:
            seen.add(id(current))
            kind = current.get("type")
            # A serializer behind a type alias sits on the reference itself.
            admitted = _WRAPPER_KEYS.get(str(kind), _VALIDATOR_KEYS) | _ANY_NODE
            changed = (
                changed
                or kind in _UNCHECKED_VALIDATORS | {"chain"}
                or not set(current) <= admitted
            )
            if kind == "definition-ref":
                current = self.definitions.get(str(current.get("schema_ref")), {})
            elif kind == "chain":
                # Pydantic chains a constraint it cannot apply in place, such
                # as a pattern, after the type's own node.
                chained = current.get("steps") or [{}]
                current = chained[0] if isinstance(chained[0], Mapping) else {}
            else:
                current = current.get("schema", {})
        form = form_in(current)
        serialization = current.get("serialization")
        if form is None and isinstance(serialization, Mapping):
            # A plain validator over a type alias keeps the alias's serializer
            # by wrapping a reference to it.
            form, changed = form_in(self.unwrapped(serialization.get("schema"))), True
        if form is None:
            return
        bare = {key: value for key, value in current.items() if key not in _ANY_NODE}
        if changed or form_of(bare | {"type": "function-plain"}) is None:
            raise self.fail(
                f"{subject} adds a constraint, a validator or a serializer to "
                f"neolith.{form.name}; only a before validator is admitted there, "
                "since the type's own reader and writer fix its form"
            )

    def unwrapped(self, node: object) -> Mapping[str, Any]:
        """The node behind any ``definitions`` wrappers and references, or {}."""
        seen: set[int] = set()
        while (
            isinstance(node, Mapping)
            and node.get("type") in _WRAPPER_KEYS
            and id(node) not in seen
        ):
            seen.add(id(node))
            node = (
                self.definitions.get(str(node.get("schema_ref")), {})
                if node.get("type") == "definition-ref"
                else node.get("schema", {})
            )
        return node if isinstance(node, Mapping) else {}

    def validator(self, node: Mapping[str, Any], subject: str, path: str = "") -> None:
        """Admit a before validator on a type without records; refuse the rest."""
        if any(
            reached.get("type") in ("model", "model-fields")
            for reached in self.reachable(node.get("schema", {}), path)
        ):
            raise self.fail(
                f"{subject} has a validator on a record or on a type that "
                "contains one, so it can return a record instance that "
                "Pydantic keeps without checking it; validate the record's "
                "fields instead"
            )
        if node.get("type") in _UNCHECKED_VALIDATORS:
            raise self.fail(
                f"{subject} has an after or wrap validator, which returns its "
                "value after the type and constraints are checked, so that value "
                "travels unchecked against the description; check the input in a "
                "before validator (mode='before') instead"
            )
        # pydantic-core hands ValidationInfo to a "with-info" function, and its
        # data holds the record's fields validated so far: the very values the
        # record keeps, which the validator could change after they were
        # checked. Anything but "no-info" is refused.
        function = node.get("function")
        if not isinstance(function, Mapping) or function.get("type") != "no-info":
            raise self.fail(
                f"{subject} has a validator that takes ValidationInfo, whose "
                "data holds the fields validated before it, which it could "
                "change; take the value only"
            )
        self.keys(node, _VALIDATOR_KEYS, subject)

    def bounds(
        self, node: Mapping[str, Any], path: str, keywords: Mapping[str, str]
    ) -> dict[str, Any]:
        """The node's constraints as JSON Schema keywords, or a named rejection."""
        try:
            return _bounds(node, keywords)
        except _Unstatable as error:
            raise self.fail(
                f"field {path} sets {error.key}={error.value!r}, which the "
                "description cannot state; use a finite int or float bound"
            ) from error

    def enum(self, node: Mapping[str, Any], path: str) -> Ref:
        """Admit an enum of string values and describe it once under ``$defs``."""
        cls = node["cls"]
        members = node.get("members", [])
        if node.get("sub_type") != "str" or not all(
            isinstance(m.value, str) for m in members
        ):
            raise self.fail(
                f"enum {_naming.name(cls)} at {path} must have string values only and "
                "subclass str, for example as an enum.StrEnum"
            )
        self.keys(
            node,
            # "missing" is the enum's _missing_ hook, which maps unknown input
            # on the way in; Pydantic 2.12 records it here and 2.14 does not.
            frozenset({"cls", "members", "sub_type", "missing"}),
            f"enum {_naming.name(cls)} at {path}",
        )
        if not members:
            raise self.fail(
                f"enum {_naming.name(cls)} at {path} has no members, so no value "
                "fits it"
            )
        if self.register(cls, "enum", path):
            self.defs[_naming.name(cls, None)] = {
                "type": "string",
                # An alias member repeats a value; JSON Schema lists it once.
                "enum": list(dict.fromkeys(m.value for m in members)),
            }
        return {"$ref": f"#/$defs/{_naming.name(cls, None)}"}


def portable_scalars() -> Mapping[
    str, tuple[Mapping[str, str], Mapping[str, str], frozenset[str]]
]:
    """The admitted scalars and their keys, for the property test's generator."""
    return _SCALARS


def portable_constructs() -> Mapping[str, frozenset[str]]:
    """The other admitted keys and node kinds, for the property test's generator."""
    return {
        "list": frozenset(_ITEM_BOUNDS),
        "dict": frozenset(_ENTRY_BOUNDS),
        "model-field": frozenset({"frozen"}),
        "validators": _VALIDATORS,
    }


def portable_settings() -> frozenset[str]:
    """The admitted record settings, for the property test's generator."""
    return _SETTINGS


# The portable integers (spec 01), and those of an untyped detail (CON-30):
# the ones every JSON reader holds exactly (I-JSON, RFC 7493, section 2.2).
SAFE_INTEGERS = range(-SAFE_INTEGER_MAX, SAFE_INTEGER_MAX + 1)
# JSON's scalars, as the json module reads them. bool is a subclass of int,
# so each is matched by exact type, never with isinstance.
_JSON_SCALARS = frozenset({str, int, float, bool, type(None)})


def _annotation(annotation: object) -> str:
    """Name what was given as a request, reply or detail type, never raising."""
    if issubclass(type(annotation), type):
        return _naming.qualname(annotation)
    return _naming.text(annotation)


def text_problem(text: str) -> str | None:
    """Say why ``text`` cannot travel as JSON text: a lone surrogate.

    JSON text is UTF-8 (RFC 8259, section 8.1), which cannot encode a lone
    surrogate; pydantic-core refuses to write one, and the json module writes
    an escape that other readers refuse.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return f"{shown(text)} holds a lone surrogate, which JSON text cannot carry"
    return None


def json_value_problem(value: object, max_depth: int | None = None) -> str | None:
    """Say why ``value`` is not a portable JSON value, or return None.

    Admitted: ``dict`` with ``str`` keys, ``list``, ``str`` without a lone
    surrogate, ``int`` within ±(2**53 - 1), finite ``float``, ``bool`` and
    ``None``, each by exact type. With ``max_depth``, maps and lists nest at
    most that many levels, the outermost being the first. Walks without
    recursion, so a deep value cannot exhaust the stack, and refuses a value
    that contains itself.
    """
    pending: list[tuple[object, int, bool]] = [(value, 1, False)]
    open_containers: set[int] = set()
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            open_containers.discard(id(item))
            continue
        kind = type(item)
        if _naming.one_of(kind, _JSON_SCALARS):
            if kind is str:
                problem = text_problem(cast(str, item))
                if problem is not None:
                    return problem
            elif kind is int and item not in SAFE_INTEGERS:
                return f"it holds {shown(item)}, outside ±{SAFE_INTEGER_MAX}"
            elif kind is float and not math.isfinite(cast(float, item)):
                return f"{shown(item)} is not a JSON number"
            continue
        if kind is not dict and kind is not list:
            return f"it holds a {_naming.qualname(kind)}"
        if id(item) in open_containers:
            return "it contains itself"
        if max_depth is not None and depth > max_depth:
            return f"it nests maps and lists more than {max_depth} levels deep"
        open_containers.add(id(item))
        pending.append((item, depth, True))
        if kind is list:
            pending.extend((child, depth + 1, False) for child in cast(list[Any], item))
            continue
        for key, child in cast(dict[Any, Any], item).items():
            if type(key) is not str:
                return f"its key {shown(key)} is not a str"
            problem = text_problem(key)
            if problem is not None:
                return problem
            pending.append((child, depth + 1, False))
    return None


def encoding_problem(
    description: Mapping[str, Any], defs: Mapping[str, Any], value: object
) -> str | None:
    """Say which wire-encoding rule of spec 01 a JSON value breaks, or None.

    ``value`` is what Pydantic wrote for a value of the described type, read
    back with the json module, and ``defs`` holds the records and enums that
    ``description`` refers to. Only the encoding rules are checked: each
    string against its form's patterns, matched over the whole string; a
    number's zero, written without its sign; and a timestamp's instant, in
    years 0001 to 9999 in UTC. Integers and the field's own constraints are
    checked elsewhere.
    """
    pending: list[tuple[Mapping[str, Any], object]] = [(description, value)]
    while pending:
        schema, item = pending.pop()
        ref = schema.get("$ref")
        if isinstance(ref, str):
            pending.append((defs.get(ref.rsplit("/", 1)[-1], {}), item))
            continue
        if "anyOf" in schema:
            if item is not None:
                pending.extend(
                    (branch, item)
                    for branch in schema["anyOf"]
                    if branch.get("type") != "null"
                )
            continue
        if type(item) is dict:
            properties = schema.get("properties", {})
            others = schema.get("additionalProperties", {})
            pending.extend(
                (properties.get(key, others), child)
                for key, child in cast(dict[str, Any], item).items()
            )
        elif type(item) is list:
            items = schema.get("items", {})
            pending.extend((items, child) for child in item)
        elif type(item) is str:
            patterns = [schema.get("pattern")]
            patterns += [part.get("pattern") for part in schema.get("allOf", [])]
            for pattern in patterns:
                if pattern is not None and not fullmatch(pattern, item):
                    return f"{shown(item)} does not match {pattern}"
            if schema.get("format") == "date-time":
                try:
                    datetime.fromisoformat(item).astimezone(UTC)
                except OverflowError:
                    return f"{shown(item)} is outside years 0001 to 9999 in UTC"
                except ValueError:
                    return f"{shown(item)} is not a timestamp"
        elif type(item) is float and item == 0 and math.copysign(1.0, item) < 0:
            return "-0.0 travels as 0; write 0.0"
    return None


def _unchanged(held: object, written: object) -> bool:
    """True if validation left a written default as it was, at every depth.

    Equality alone is not enough: ``1 == True`` and ``Decimal(3) == 3``. So
    lists and maps are compared item by item, and scalars by type as well. An
    int may stand for a float, as it does in strict mode. A record compares as
    a whole, by equality.
    """
    if type(written) is list and type(held) is list:
        return len(held) == len(written) and all(
            _unchanged(h, w) for h, w in zip(held, written, strict=True)
        )
    if type(written) is dict and type(held) is dict:
        return held.keys() == written.keys() and all(
            _unchanged(held[key], written[key]) for key in written
        )
    if isinstance(written, BaseModel):
        return held == written
    if type(written) is int and type(held) is float:
        return held == written
    return type(held) is type(written) and held == written


# What pydantic-core reads from a record instance, or calls to build or copy
# one, that a record class could otherwise replace: attribute lookup, instance
# creation, the instance slots BaseModel declares, and the deep copy it makes
# of a default it cannot hash (src/validators/with_default.rs).
_RECORD_HOOKS = frozenset(
    {
        "__getattribute__",
        "__getattr__",
        "__new__",
        "__dict__",
        "__class__",
        "__deepcopy__",
        "__pydantic_extra__",
        "__pydantic_fields_set__",
        "__pydantic_private__",
    }
)


def _class_hook(cls: type[Any]) -> str | None:
    """Say how a record class replaces what Pydantic reads, or None.

    Every class in its method resolution order but ``BaseModel`` and
    ``object`` is read, so a mixin listed after ``BaseModel`` counts too. Each
    namespace is read through ``type``'s own descriptors, as in
    ``_from_classes``, so a metaclass cannot run code here.
    """
    if type(cls) is not type(BaseModel):
        return f"has the metaclass {_naming.type_name(cls)}, not Pydantic's"
    for klass in type.__dict__["__mro__"].__get__(cls):
        if klass is BaseModel or klass is object:
            continue
        for key in type.__dict__["__dict__"].__get__(klass):
            if type(key) is not str:
                return (
                    f"has a namespace key that is not text in {_naming.qualname(klass)}"
                )
            if key in _RECORD_HOOKS:
                where = "" if klass is cls else f" in {_naming.qualname(klass)}"
                return f"defines {key}{where}"
    return None


# Values pydantic-core may hand to every value read without the field, since
# none of them can change: these exact types, and enum members.
_IMMUTABLE = frozenset(
    {str, int, float, bool, bytes, type(None), Decimal, date, datetime, time, timedelta}
)


def _default_problem(default: object) -> str | None:
    """Name what in a default pydantic-core might not copy, or None.

    pydantic-core deep-copies a default it cannot hash, and ``copy.deepcopy``
    asks each object how to copy it, finding ``__deepcopy__`` on the instance
    before its class. So a default holds only exact lists, dicts, sets,
    tuples and frozensets, immutable scalars of exact types, enum members, and
    records that keep Pydantic's own copy and hold nothing but their fields.
    Anything else, such as a subclass of a list, is refused.
    """
    pending = [default]
    seen: set[int] = set()
    instance_dict = type.__dict__["__dict__"].__get__(BaseModel)["__dict__"]
    while pending:
        item = pending.pop()
        kind = type(item)
        if _naming.one_of(kind, _IMMUTABLE) or issubclass(kind, enum.Enum):
            continue
        if id(item) in seen:
            continue
        seen.add(id(item))
        if _naming.derives(kind, BaseModel):
            hook = _class_hook(kind)
            if hook is not None:
                return f"a {_naming.qualname(kind)}, which {hook}"
            fields = instance_dict.__get__(item)
            extra = set(fields) - set(cast("type[BaseModel]", kind).__pydantic_fields__)
            if extra:
                return (
                    f"a {_naming.qualname(kind)} with values outside its fields: "
                    f"{', '.join(sorted(map(shown, extra)))}"
                )
            pending.extend(fields.values())
        elif _naming.one_of(kind, (list, tuple, set, frozenset)):
            pending.extend(cast("list[object]", item))
        elif kind is dict:
            pending.extend(cast("dict[object, object]", item).items())
        else:
            return f"a {_naming.qualname(kind)}"
    return None


def _unchanging(value: object) -> bool:
    """Whether nothing in a default can change: immutable scalars, enum
    members, and tuples and frozensets of them. Never a record.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        kind = type(item)
        if _naming.one_of(kind, _IMMUTABLE) or issubclass(kind, enum.Enum):
            continue
        if _naming.one_of(kind, (tuple, frozenset)):
            pending.extend(cast("tuple[object, ...]", item))
        else:
            return False
    return True


def _changeable_parts(value: object) -> Iterator[object]:
    """Every object reachable from a value that is not an immutable scalar or
    an enum member, each once.

    A record is walked through its fields, its private attributes and its
    set of fields given, which Pydantic keeps beside them; tuples and
    frozensets are walked through; any other object is yielded as it is.
    """
    pending = [value]
    seen: set[int] = set()
    slots = type.__dict__["__dict__"].__get__(BaseModel)
    fields, private, given = (
        slots["__dict__"],
        slots["__pydantic_private__"],
        slots["__pydantic_fields_set__"],
    )
    while pending:
        item = pending.pop()
        kind = type(item)
        if (
            _naming.one_of(kind, _IMMUTABLE)
            or issubclass(kind, enum.Enum)
            or id(item) in seen
        ):
            continue
        seen.add(id(item))
        if _naming.one_of(kind, (tuple, frozenset)):
            pending.extend(cast("tuple[object, ...]", item))
            continue
        yield item
        if _naming.derives(kind, BaseModel):
            pending.extend(fields.__get__(item).values())
            pending.append(given.__get__(item))
            held = private.__get__(item)
            if held is not None:
                pending.append(held)
        elif _naming.one_of(kind, (list, set)):
            pending.extend(cast("list[object]", item))
        elif kind is dict:
            pending.extend(cast("dict[object, object]", item).values())


def _shared_between(first: object, second: object) -> str | None:
    """Name an object two values can both reach that can change, or None."""
    held = {id(part): part for part in _changeable_parts(first)}
    for part in _changeable_parts(second):
        if held.get(id(part)) is part:
            return _naming.type_name(part)
    return None


def _hashable(value: object) -> bool:
    """Whether ``hash`` takes a value, as pydantic-core asks of a default."""
    try:
        hash(value)
    except Exception:  # an author's __hash__ may raise anything
        return False
    return True


def _from_classes(cls: type[Any], name: str) -> object:
    """What an instance of ``cls`` finds for ``name`` in its classes, or None.

    The method resolution order and each class's namespace are read through
    ``type``'s own descriptors, as an instance lookup reads them, so a
    metaclass can neither run code here nor hide an entry. A namespace key
    that is not exactly a str could compare equal to ``name`` by code of its
    own, so it gives None.
    """
    for klass in type.__dict__["__mro__"].__get__(cls):
        found: list[object] = []
        for key, value in type.__dict__["__dict__"].__get__(klass).items():
            if type(key) is not str:
                return None
            if key == name:
                found.append(value)
        if found:
            return found[0]
    return None


def _closure(function: object) -> list[object]:
    """What a function's closure cells hold (None for an empty one)."""
    if type(function) is not types.FunctionType:
        return []
    held: list[object] = []
    for cell in function.__closure__ or ():
        try:
            held.append(cell.cell_contents)
        except ValueError:
            held.append(None)
    return held


class _Private(BaseModel):
    # Nothing validates with these records, so no schema is built for them and
    # no Pydantic plugin is called when neolith is imported.
    model_config = ConfigDict(defer_build=True)

    _x: int = PrivateAttr(0)


def _post_init(self: BaseModel, context: object, /) -> None:
    """Stands for an author's model_post_init, which Pydantic wraps."""


class _Wrapped(_Private):
    model_post_init = _post_init


# Pydantic's own model_post_init for a record with private attributes, and the
# wrapper it puts around an existing one, with the closure cell that holds the
# function it wraps. They are taken from records built here, not imported from
# pydantic._internal, whose names a minor release may change.
_INIT_PRIVATE_ATTRIBUTES = _from_classes(_Private, "model_post_init")
_WRAPPER = _from_classes(_Wrapped, "model_post_init")
_WRAPPER_CODE = getattr(_WRAPPER, "__code__", object())
_WRAPPER_GLOBALS = getattr(_WRAPPER, "__globals__", object())
_WRAPPER_CLOSURE = _closure(_WRAPPER)
_WRAPPED_CELLS = [
    index for index, held in enumerate(_WRAPPER_CLOSURE) if held is _post_init
]
_WRAPPED_CELL = _WRAPPED_CELLS[0] if len(_WRAPPED_CELLS) == 1 else None


def _only_initialises_private_attributes(cls: type[Any]) -> bool:
    """True if the record's model_post_init is Pydantic's own, for private attributes.

    Pydantic sets ``model_post_init`` to ``init_private_attributes`` when a
    record has private attributes. When a model_post_init already exists, such
    as one inherited from a record that has private attributes too, it sets a
    wrapper that runs ``init_private_attributes`` and then the existing one,
    held in a closure cell (pydantic/_internal/_model_construction.py,
    ``ModelMetaclass.__new__``, at 2.12 and 2.14). The walk follows what the
    wrapper runs: through functions with its code, globals and other cells, to
    the function each holds, it must end at ``init_private_attributes``. It
    never reads ``__wrapped__``, which anyone can set.
    """
    function = _from_classes(cls, "model_post_init")
    for _ in range(64):
        if function is _INIT_PRIVATE_ATTRIBUTES:
            return True
        held = _closure(function)
        if (
            _WRAPPED_CELL is None
            or type(function) is not types.FunctionType
            or function.__code__ is not _WRAPPER_CODE
            or function.__globals__ is not _WRAPPER_GLOBALS
            or len(held) != len(_WRAPPER_CLOSURE)
            or any(
                held[index] is not _WRAPPER_CLOSURE[index]
                for index in range(len(held))
                if index != _WRAPPED_CELL
            )
        ):
            return False
        function = held[_WRAPPED_CELL]
    return False


# What Pydantic's PrivateAttr builds, taken from it rather than imported.
_PRIVATE_ATTRIBUTE = type(PrivateAttr())

if sys.version_info >= (3, 14):
    from annotationlib import Format

    def _signature(function: Callable[..., object]) -> inspect.Signature:
        """A callable's signature, its annotations left unevaluated."""
        return inspect.signature(function, annotation_format=Format.FORWARDREF)

else:

    def _signature(function: Callable[..., object]) -> inspect.Signature:
        """A callable's signature, its annotations left unevaluated."""
        return inspect.signature(function)


def _private_attribute_problem(cls: type[Any]) -> str | None:
    """Say which private attribute could be handed the validated data, or None.

    Pydantic builds a record's private attributes after validation, in the
    model_post_init it generates (``init_private_attributes`` in
    pydantic/_internal/_model_construction.py). From 2.13 it hands the
    record's validated data, the very field values, to a default factory that
    Pydantic's ``default_factory_takes_validated_data`` says takes it (one
    positional parameter without a default), and that factory can change them
    after they were checked. At 2.12 it calls every factory with no
    arguments, so such a factory raises each time a value is read. So a
    private attribute is what ``PrivateAttr`` builds, which no record can
    override, and its default factory, if any, takes no arguments, whatever
    Pydantic would say of it.
    """
    attributes = _from_classes(cls, "__private_attributes__")
    if type(attributes) is not dict:
        return "has private attributes that neolith cannot read"
    for name, attribute in attributes.items():
        if type(attribute) is not _PRIVATE_ATTRIBUTE:
            return (
                f"has the private attribute {name}, which is a "
                f"{_naming.type_name(attribute)}, not what Pydantic's "
                "PrivateAttr builds"
            )
        factory = attribute.default_factory
        if factory is None:
            continue
        try:
            takes_data = (
                getattr(attribute, "default_factory_takes_validated_data", False)
                is not False
            )
            try:
                parameters = list(_signature(factory).parameters.values())
            except (ValueError, TypeError):
                # Pydantic cannot read it either, so calls it with none.
                parameters = []
        except Exception as error:  # an author's callable may raise anything
            return (
                f"has the private attribute {name}, whose default factory's "
                f"signature cannot be read ({_naming.cause(error, None)})"
            )
        if takes_data or any(
            parameter.default is inspect.Parameter.empty
            and parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for parameter in parameters
        ):
            return (
                f"has the private attribute {name}, whose default factory takes "
                "arguments; Pydantic calls it after validation and may hand it "
                "the validated data, which it could change. Give it a factory "
                "that takes none"
            )
    return None


def _narrowed(base: Mapping[str, Any], bounds: Mapping[str, Any]) -> dict[str, Any]:
    """A form's description with a field's own bounds, the narrower one kept.

    The form's range (an integer's or a number's) and a field's bound of the
    same kind cannot both be written under one keyword, so the tighter one is.
    """
    narrowed = {**copy.deepcopy(dict(base)), **bounds}
    if "minimum" in base and "minimum" in bounds:
        narrowed["minimum"] = max(base["minimum"], bounds["minimum"])
    if "maximum" in base and "maximum" in bounds:
        narrowed["maximum"] = min(base["maximum"], bounds["maximum"])
    return narrowed


def _bounds(node: Mapping[str, Any], keywords: Mapping[str, str]) -> dict[str, Any]:
    """Map a node's constraints to JSON Schema keywords.

    Raises _Unstatable for a constraint whose value the keyword cannot state:
    a bound that is not a finite int or float (a Decimal, a bool or infinity).
    """
    mapped: dict[str, Any] = {}
    for key, keyword in keywords.items():
        if key not in node:
            continue
        value = node[key]
        # An int is always finite and JSON Schema states it exactly, however
        # large; math.isfinite would raise OverflowError past the float range.
        if not _naming.one_of(type(value), (int, float)) or (
            type(value) is float and not math.isfinite(value)
        ):
            raise _Unstatable(key, value)
        mapped[keyword] = value
    return mapped


class _Unstatable(Exception):
    """A constraint whose value the description cannot state."""

    def __init__(self, key: str, value: object) -> None:
        super().__init__(key, value)
        self.key = key
        self.value = value
