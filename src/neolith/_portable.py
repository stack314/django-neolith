"""The portable type system of spec 01, admitted from Pydantic's core schema.

Pydantic validates and serialises a record from its core schema, not from its
annotations: the model's validator and serializer are both built from
``__pydantic_core_schema__``. So the core schema is what travels. This module
walks it and admits only the node types and keys that the portable forms
produce. Anything else, whether a node type, a key on a node, a model setting
or a default that does not survive a JSON round trip, is a
``ContractDefinitionError`` that names the contract, the action and the field
(CON-17).

The same walk writes the neutral description (CON-16). It never reads
Pydantic's JSON Schema generator. ``WithJsonSchema``, ``json_schema_extra``,
``__get_pydantic_json_schema__`` and similar hooks only change what that
generator writes, so they cannot change the description. Records and enums
are described once each under ``$defs``, by class name; ``type`` aliases are
written out where they are used.
"""

import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel
from pydantic._internal._model_construction import init_private_attributes
from pydantic.errors import PydanticUndefinedAnnotation, PydanticUserError
from pydantic_core import (
    SchemaSerializer,
    SchemaValidator,
    core_schema,
)

# A record or enum name, as other languages' generated code will use it
# (CON-20).
_TYPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Keys any node may carry without changing what travels: Pydantic's id for the
# node, metadata (read only by Pydantic's JSON Schema generator), and strict
# mode (validation only).
_ANY_NODE = frozenset({"type", "ref", "metadata", "strict"})

_NUMBER_BOUNDS = {
    "gt": "exclusiveMinimum",
    "ge": "minimum",
    "lt": "exclusiveMaximum",
    "le": "maximum",
    "multiple_of": "multipleOf",
}
_STRING_BOUNDS = {
    "min_length": "minLength",
    "max_length": "maxLength",
    "pattern": "pattern",
}
_ITEM_BOUNDS = {"min_length": "minItems", "max_length": "maxItems"}
_ENTRY_BOUNDS = {"min_length": "minProperties", "max_length": "maxProperties"}

# The portable scalars: core schema type -> (description, constraint keys and
# the JSON Schema keyword each becomes, other keys admitted). A constraint
# without a JSON Schema keyword on the wire form, such as a decimal's
# max_digits, is not admitted, so that the description states every rule the
# provider enforces.
_SCALARS: dict[str, tuple[dict[str, str], Mapping[str, str], frozenset[str]]] = {
    "bool": ({"type": "boolean"}, {}, frozenset()),
    "int": ({"type": "integer"}, _NUMBER_BOUNDS, frozenset()),
    "float": ({"type": "number"}, _NUMBER_BOUNDS, frozenset({"allow_inf_nan"})),
    # strip_whitespace, to_lower and to_upper convert values on the way in.
    "str": (
        {"type": "string"},
        _STRING_BOUNDS,
        frozenset({"strip_whitespace", "to_lower", "to_upper"}),
    ),
    "bytes": ({"type": "string", "format": "binary"}, {}, frozenset()),
    "decimal": ({"type": "string", "format": "decimal"}, {}, frozenset()),
    "date": ({"type": "string", "format": "date"}, {}, frozenset()),
    "timedelta": (
        {"type": "string", "format": "duration"},
        {},
        frozenset({"microseconds_precision"}),
    ),
    "datetime": (
        {"type": "string", "format": "date-time"},
        {},
        frozenset({"tz_constraint", "microseconds_precision"}),
    ),
}

# Validators check or convert values on the way in and leave the wire form to
# the schema they wrap. A plain validator replaces that schema, so it is not
# admitted.
_VALIDATORS = frozenset({"function-after", "function-before", "function-wrap"})
# json_schema_input_schema only changes what Pydantic's JSON Schema generator
# writes in validation mode, which the description never reads.
_VALIDATOR_KEYS = frozenset({"function", "schema", "json_schema_input_schema"})

# Model settings that change validation only. Anything else that reaches the
# core config, such as ser_json_timedelta or val_json_bytes, changes the wire.
_SETTINGS = frozenset(
    {
        "title",
        "strict",
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

    def fail(self, message: str) -> Exception:
        return self.error(f"{self.where}: {message}")

    def root(
        self, annotation: object, role: Literal["request", "reply"]
    ) -> tuple[type[BaseModel], Ref]:
        """Admit a request or reply type; return it and its reference."""
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            model: type[BaseModel] = annotation
            try:
                node = self.resolve(self.build(model), model.__name__)
                # A wrap model_validator wraps the model node itself.
                while node.get("type") in _VALIDATORS:
                    self.keys(node, _VALIDATOR_KEYS, f"record {model.__name__}")
                    node = self.resolve(node["schema"], model.__name__)
                if node.get("type") == "model" and node.get("cls") is model:
                    return model, self.record(node, model.__name__)
            except (KeyError, AttributeError, TypeError) as error:
                if isinstance(error, self.error):
                    raise
                # Fail closed: a core schema shape this walk does not read is
                # refused, never passed.
                raise self.fail(
                    f"record {model.__name__} has a core schema shape that "
                    f"neolith does not read ({type(error).__name__}: {error})"
                ) from error
        empty = "" if role == "request" else ", or None for an empty reply"
        raise self.fail(
            f"the {role} type must be a record (a pydantic model with named "
            f"fields){empty}, not {getattr(annotation, '__qualname__', annotation)}"
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
                    f"record {model.__name__}, or a record it uses, refers to "
                    f"{error.name!r}, which the module of that record does not "
                    "define when the contract is declared; define "
                    f"{error.name} at module level there, before the contract, or "
                    f"call that record's model_rebuild() once {error.name} is "
                    "defined"
                ) from error
            except PydanticUserError as error:
                raise self.fail(
                    f"record {model.__name__} cannot be built: {error}"
                ) from error
            schema = model.__pydantic_core_schema__
        self.collect(schema)
        return schema

    def collect(self, schema: object) -> None:
        """Index every core schema definition by its reference."""
        pending = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if node.get("type") == "definitions":
                    for definition in node.get("definitions", []):
                        self.definitions[definition["ref"]] = definition
                pending.extend(node.values())
            elif isinstance(node, list):
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
        name = named.__name__
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
                f"{known.__module__}.{known.__qualname__} and "
                f"{named.__module__}.{named.__qualname__}"
            )
        self.named[name] = named
        return True

    def record(self, node: Mapping[str, Any], path: str) -> Ref:
        """Admit a record and describe it once under ``$defs``."""
        cls = node["cls"]
        subject = f"record {path}"
        admitted = {"cls", "schema", "config", "custom_init", "root_model"}
        if _only_initialises_private_attributes(cls):
            # Private attributes never travel; Pydantic sets post_init to
            # initialise them when the record has any.
            admitted.add("post_init")
        self.keys(node, frozenset(admitted), subject)
        if node.get("root_model"):
            raise self.fail(f"{subject} is a root model; a record has named fields")
        for key, value in node.get("config", {}).items():
            if key == "extra_fields_behavior":
                if value != "ignore":
                    raise self.fail(
                        f"{subject} sets extra={value!r}; a record must ignore "
                        "unknown fields so that adding a field stays compatible"
                    )
            elif key not in _SETTINGS:
                raise self.fail(
                    f"{subject} has {key} set, which changes how it travels"
                )
        reference = {"$ref": f"#/$defs/{cls.__name__}"}
        if not self.register(cls, "record", path):
            return reference
        self.defs[cls.__name__] = {}
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
        while fields_node.get("type") in _VALIDATORS:
            self.keys(fields_node, _VALIDATOR_KEYS, subject)
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
        self.defs[cls.__name__] = description
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
        if "default_factory" in node:
            if node.get("default_factory_takes_data"):
                raise self.fail(
                    f"field {path} has a default factory that takes the other fields"
                )
            try:
                made = node["default_factory"]()
            except Exception as error:  # the author's factory may raise anything
                raise self.fail(
                    f"field {path} has a default factory that raised "
                    f"{type(error).__name__}: {error}"
                ) from error
            self.round_trip(node["schema"], made, path)
            return description, True
        return {
            **description,
            "default": self.round_trip(node["schema"], node["default"], path),
        }, True

    def round_trip(self, schema: Mapping[str, Any], value: object, path: str) -> object:
        """Return a default's JSON form if it reads back as itself.

        Pydantic sends a default as written: it is not validated unless the
        field sets ``validate_default`` (pydantic/fields.py, ``FieldInfo``).
        So the default must already be in the form its type reads back,
        exactly, as JSON text: ``Decimal`` for a decimal, not ``0``, and
        ``1.0`` rather than ``1`` wherever ``1`` would not read back as ``1``.
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
                f"field {path} has the default {value!r}, which is not a value of "
                "its type; Pydantic keeps a default as written, so write one"
            ) from error
        if not _unchanged(held, value):
            raise self.fail(
                f"field {path} has the default {value!r}, which its validators "
                f"turn into {held!r}; write the default as {held!r}"
            )
        try:
            # Pydantic serialises a default it was given as written, with the
            # field's own serializer; warnings off, since an off-type value
            # only warns and the read-back below catches it.
            sent = serializer.to_json(value, warnings=False)
        except (ValueError, TypeError) as error:
            raise self.fail(
                f"field {path} has the default {value!r}, which cannot be written "
                "as JSON: a string with a lone surrogate never can, and bytes "
                "that are not UTF-8 need the base64 encoding that the in-process "
                "transport settles"
            ) from error
        try:
            read = serializer.to_json(validator.validate_json(sent), warnings=False)
        except (ValueError, TypeError) as error:
            raise self.fail(
                f"field {path} has the default {value!r}, which travels as "
                f"{sent.decode()} and does not read back as a value of its type"
            ) from error
        if sent != read:
            raise self.fail(
                f"field {path} has the default {value!r}, which travels as "
                f"{sent.decode()} but reads back as {read.decode()}; write the "
                "default so that it reads back as itself"
            )
        return json.loads(sent)

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
        if kind in _VALIDATORS:
            inner = node.get("schema", {})
            if inner.get("type") == "enum":
                self.enum(inner, path)
            self.keys(node, _VALIDATOR_KEYS, subject)
            return self.describe(inner, path, aliases)
        if kind == "model":
            return self.record(node, path)
        if kind == "enum":
            return self.enum(node, path)
        if kind in _SCALARS:
            base, bounds, other = _SCALARS[kind]
            self.keys(node, frozenset(bounds) | other, subject)
            if kind == "str":
                self.case(node, subject)
            if kind == "datetime" and node.get("tz_constraint") != "aware":
                raise self.fail(
                    f"{subject} is a timestamp, which carries an offset from UTC "
                    "(RFC 3339); annotate it as pydantic.AwareDatetime"
                )
            return {**base, **self.bounds(node, path, bounds)}
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
            key = self.describe(node["keys_schema"], f"{path} key", aliases)
            if key.get("type") != "string" or "format" in key:
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

    def case(self, node: Mapping[str, Any], subject: str) -> None:
        """Refuse a case conversion on a string the description constrains.

        pydantic-core checks a string's pattern and lengths before it converts
        the case, and strips whitespace before both. So a converted value can
        break the constraints the description states ("abc" passes
        ``^[a-z]+$`` and travels as "ABC"; "ß" passes a maximum length of 1
        and travels as "SS"). The conversion may come from the field or from
        the record's settings.
        """
        converts = any(node.get(key) for key in ("to_lower", "to_upper")) or any(
            self.config.get(key) for key in ("str_to_lower", "str_to_upper")
        )
        constrained = [
            key for key in ("pattern", "min_length", "max_length") if key in node
        ]
        if converts and constrained:
            raise self.fail(
                f"{subject} converts case after checking its {constrained[0]}, so "
                "a value it accepts can travel in a form it refuses; drop the "
                "case conversion or the constraint"
            )

    def bounds(
        self, node: Mapping[str, Any], path: str, keywords: Mapping[str, str]
    ) -> dict[str, Any]:
        """The node's constraints as JSON Schema keywords, or a named rejection."""
        try:
            return _bounds(node, keywords)
        except _Unstatable as error:
            raise self.fail(
                f"field {path} sets {error.key}={error.value!r}, which the "
                "description cannot state; use a finite int or float bound, or a "
                "string pattern"
            ) from error

    def enum(self, node: Mapping[str, Any], path: str) -> Ref:
        """Admit an enum of string values and describe it once under ``$defs``."""
        cls = node["cls"]
        members = node.get("members", [])
        if node.get("sub_type") != "str" or not all(
            isinstance(m.value, str) for m in members
        ):
            raise self.fail(
                f"enum {cls.__name__} at {path} must have string values only and "
                "subclass str, for example as an enum.StrEnum"
            )
        self.keys(
            node,
            # "missing" is the enum's _missing_ hook, which maps unknown input
            # on the way in; Pydantic 2.12 records it here and 2.14 does not.
            frozenset({"cls", "members", "sub_type", "missing"}),
            f"enum {cls.__name__} at {path}",
        )
        if self.register(cls, "enum", path):
            self.defs[cls.__name__] = {
                "type": "string",
                "enum": [m.value for m in members],
            }
        return {"$ref": f"#/$defs/{cls.__name__}"}


def portable_scalars() -> Mapping[
    str, tuple[Mapping[str, str], Mapping[str, str], frozenset[str]]
]:
    """The admitted scalars and their keys, for the property test's generator."""
    return _SCALARS


def portable_settings() -> frozenset[str]:
    """The admitted record settings, for the property test's generator."""
    return _SETTINGS


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


def _only_initialises_private_attributes(cls: type[Any]) -> bool:
    """True if the record's model_post_init is Pydantic's own, for private attributes.

    Pydantic sets ``model_post_init`` to ``init_private_attributes`` when a
    record has private attributes, and wraps it, keeping ``__wrapped__``, when
    a model_post_init already exists, such as one inherited from a record that
    has private attributes too (pydantic/_internal/_model_construction.py,
    ``ModelMetaclass.__new__``, at 2.12 and 2.14). Following ``__wrapped__``
    must end at ``init_private_attributes``; anything an author wrote ends
    elsewhere.
    """
    function = getattr(cls, "model_post_init", None)
    for _ in range(64):
        if function is init_private_attributes:
            return True
        wrapped = getattr(function, "__wrapped__", None)
        # functools.wraps copies the name, so the wrapper is known by its code.
        code = getattr(function, "__code__", None)
        if wrapped is None or getattr(code, "co_name", "") != "wrapped_model_post_init":
            return False
        function = wrapped
    return False


def _bounds(node: Mapping[str, Any], keywords: Mapping[str, str]) -> dict[str, Any]:
    """Map a node's constraints to JSON Schema keywords.

    Returns None for a constraint whose value the keyword cannot state: a
    pattern that is not a string, or a bound that is not a finite int or float
    (a Decimal, a bool or infinity), which the caller rejects.
    """
    mapped: dict[str, Any] = {}
    for key, keyword in keywords.items():
        if key not in node:
            continue
        value = node[key]
        if keyword == "pattern":
            if not isinstance(value, str):
                raise _Unstatable(key, value)
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise _Unstatable(key, value)
        mapped[keyword] = value
    return mapped


class _Unstatable(Exception):
    """A constraint whose value the description cannot state."""

    def __init__(self, key: str, value: object) -> None:
        super().__init__(key, value)
        self.key = key
        self.value = value
