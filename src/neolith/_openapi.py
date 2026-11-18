"""The OpenAPI document a provider's host serves (spec 03, Projection).

ADR 0011 Decision 7: set-up builds one OpenAPI 3.1.2 document for every
contract version the host provides, from ``Contract.describe()`` only, and
keeps its text as fixed bytes, so equal hosts serve equal bytes.

Every schema of a description is copied unchanged, apart from its references,
which move from ``#/$defs/<Record>`` to ``#/components/schemas/<key>``. The
copy walks each schema by the JSON Schema 2020-12 vocabulary: a keyword that
holds schemas is walked, a keyword that holds data (``default``, ``const``,
``enum``, ``examples``) is copied untouched, so data that looks like a
reference is never rewritten, and any other keyword is refused. A description
that cannot be projected is a fault of the package, never of an app, and
raises ``ValueError``.
"""

import json
import re
import types
from collections.abc import Iterable, Mapping
from typing import Any, Final

from neolith._contracts import _DIALECT, Contract

__all__ = ["KIND_STATUS", "document"]

_OPENAPI: Final = "3.1.2"
# The version of the specification this implementation conforms to (spec
# README, Versioning).
_SPEC_VERSION: Final = "0.1"
_TITLE: Final = "neolith contracts"
_MEDIA: Final = "application/json"
_SCHEME: Final = "bearer"

# Spec 03, Errors: the status of each standard kind, in the table's order.
KIND_STATUS: Final[Mapping[str, int]] = types.MappingProxyType(
    {
        "invalid_argument": 400,
        "unauthenticated": 401,
        "permission_denied": 403,
        "not_found": 404,
        "conflict": 409,
        "timeout": 504,
        "unavailable": 503,
        "internal": 500,
    }
)
_DECLARED_STATUS: Final = 422

# OpenAPI 3.1.2, section 4.8.7.1: the pattern every component key matches.
_COMPONENT_KEY: Final = re.compile(r"[a-zA-Z0-9\.\-_]+")
_LOCAL_REF: Final = re.compile(r"#/\$defs/(?P<name>[A-Za-z][A-Za-z0-9_]*)")

# JSON Schema 2020-12 keywords, by what their value holds.
_SCHEMA: Final = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_MAP: Final = frozenset({"dependentSchemas", "patternProperties", "properties"})
_SCHEMA_LIST: Final = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_DATA: Final = frozenset({"const", "default", "enum", "examples"})
_PLAIN: Final = frozenset(
    {
        "$comment",
        "contentEncoding",
        "contentMediaType",
        "dependentRequired",
        "deprecated",
        "description",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "readOnly",
        "required",
        "title",
        "type",
        "uniqueItems",
        "writeOnly",
    }
)

# Spec 03, Call context, and ADR 0027's admitted set: each header's form, as
# far as a pattern or a length states it. traceparent and tracestate are
# never refused (ADR 0017 Decision 4), so they state no form.
_HEADERS: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    (
        "Neolith-Principal",
        {"type": "string", "pattern": r"^[A-Za-z0-9_\-]*$", "maxLength": 4096},
    ),
    ("Neolith-Timeout", {"type": "string", "pattern": r"^[1-9][0-9]{0,9}$"}),
    (
        "Idempotency-Key",
        # An RFC 9651 String of 1 to 255 characters after unescaping: each
        # repetition is one unescaped character or one escape.
        {"type": "string", "pattern": r'^"([ !#-\[\]-~]|\\["\\]){1,255}"$'},
    ),
    ("traceparent", {"type": "string"}),
    ("tracestate", {"type": "string"}),
)


def document(contracts: Iterable[Contract]) -> bytes:
    """Return the OpenAPI document for the provided contract versions.

    Contract versions are ordered by identity, code point by code point, so
    the order they are given in does not change the bytes.

    Raises:
        ValueError: If two contracts share an identity, or a description
            holds something the projection does not admit.
    """
    by_identity: dict[str, Contract] = {}
    for given in contracts:
        if given.identity in by_identity:
            raise ValueError(f"{given.identity} is given twice")
        by_identity[given.identity] = given
    paths: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    for identity in sorted(by_identity):
        _project(by_identity[identity], paths, schemas)
    tree = {
        "openapi": _OPENAPI,
        "info": {"title": _TITLE, "version": _SPEC_VERSION},
        "jsonSchemaDialect": _DIALECT,
        "paths": paths,
        "components": {
            "schemas": schemas,
            "responses": {
                kind: {
                    "description": f"The call failed with {kind}.",
                    "content": {_MEDIA: {"schema": _error_body(kind)}},
                }
                for kind in KIND_STATUS
            },
            "parameters": {
                name: {"name": name, "in": "header", "schema": schema}
                for name, schema in _HEADERS
            },
            "securitySchemes": {_SCHEME: {"type": "http", "scheme": "bearer"}},
        },
        "security": [{_SCHEME: []}],
    }
    return json.dumps(
        tree, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _project(
    contract: Contract, paths: dict[str, Any], schemas: dict[str, Any]
) -> None:
    identity = contract.identity
    description = contract.describe()
    if description.get("contract") != identity:
        raise ValueError(f"{identity}: the description names another contract")
    if description.get("jsonSchemaDialect") != _DIALECT:
        raise ValueError(f"{identity}: the description has another dialect")
    defs = description["$defs"]
    for record, schema in defs.items():
        key = f"{identity}.{record}"
        if _COMPONENT_KEY.fullmatch(key) is None:
            raise ValueError(f"{identity}: {record!r} is not a component name")
        schemas[key] = _copy(identity, defs, schema)
    prefix = f"/{contract.name}/v{contract.major}/"
    for name, action in description["actions"].items():
        responses: dict[str, Any] = {}
        if action["reply"] is None:
            responses["204"] = {"description": "The reply is empty."}
        else:
            responses["200"] = {
                "description": "The reply.",
                "content": {_MEDIA: {"schema": _copy(identity, defs, action["reply"])}},
            }
        errors = action["errors"]
        statuses = sorted(
            [(status, kind) for kind, status in KIND_STATUS.items()]
            + ([(_DECLARED_STATUS, "")] if errors else [])
        )
        for status, kind in statuses:
            if kind:
                responses[str(status)] = {"$ref": f"#/components/responses/{kind}"}
            else:
                responses[str(status)] = {
                    "description": "The call failed with a kind the action declares.",
                    "content": {
                        _MEDIA: {
                            "schema": {
                                "oneOf": [
                                    _error_body(kind, _copy(identity, defs, detail))
                                    for kind, detail in errors.items()
                                ]
                            }
                        }
                    },
                }
        paths[prefix + name] = {
            "post": {
                "operationId": f"{identity}.{name}",
                "tags": [identity],
                "x-neolith-idempotent": action["idempotent"],
                "parameters": [
                    {"$ref": f"#/components/parameters/{header}"}
                    for header, _ in _HEADERS
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        _MEDIA: {"schema": _copy(identity, defs, action["request"])}
                    },
                },
                "responses": responses,
            }
        }


def _error_body(kind: str, data: object = None) -> dict[str, Any]:
    """Spec 03's error body for one kind, with ``data`` for a declared kind."""
    properties: dict[str, Any] = {
        "kind": {"const": kind},
        "message": {"type": "string"},
    }
    required = ["kind", "message"]
    if data is not None:
        properties["data"] = data
        required.append("data")
    return {"type": "object", "properties": properties, "required": required}


def _copy(identity: str, defs: Mapping[str, Any], schema: object) -> object:
    """The schema with each reference moved to its component; nothing else."""
    if type(schema) is bool:
        return schema
    if type(schema) is not dict:
        raise ValueError(f"{identity}: a schema is not an object")
    copied: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword == "$ref":
            found = _LOCAL_REF.fullmatch(value) if type(value) is str else None
            if found is None or found["name"] not in defs:
                raise ValueError(f"{identity}: a reference names no record")
            copied[keyword] = f"#/components/schemas/{identity}.{found['name']}"
        elif keyword in _SCHEMA:
            copied[keyword] = _copy(identity, defs, value)
        elif keyword in _SCHEMA_MAP:
            if type(value) is not dict:
                raise ValueError(f"{identity}: {keyword} is not an object")
            copied[keyword] = {
                name: _copy(identity, defs, item) for name, item in value.items()
            }
        elif keyword in _SCHEMA_LIST:
            if type(value) is not list:
                raise ValueError(f"{identity}: {keyword} is not a list")
            copied[keyword] = [_copy(identity, defs, item) for item in value]
        elif keyword in _DATA or keyword in _PLAIN:
            copied[keyword] = value
        else:
            raise ValueError(f"{identity}: the keyword {keyword!r} is not projected")
    return copied
