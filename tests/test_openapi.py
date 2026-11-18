"""The OpenAPI document a provider's host serves (spec 03, Projection).

The document is checked against the OpenAPI Initiative's published schema for
OpenAPI 3.1 documents, ``tests/data/openapi-3.1-schema-2026-11-05.json``. It is
the file served at https://spec.openapis.org/oas/3.1/schema/2026-11-05, copied
unchanged (SHA-256 4751a777...2b7d), from the repository OAI/spec.openapis.org
at commit 3cd33f3, under the Apache License 2.0, whose text is in
``tests/data/openapi-3.1-schema-2026-11-05.LICENSE``. That schema leaves Schema
Objects unchecked, so each component is also checked against JSON Schema
2020-12's own meta-schema, the dialect the document declares.
"""

import enum
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import types
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from pydantic import AwareDatetime, BaseModel, Field

from neolith import (
    Binary,
    Contract,
    ContractDefinitionError,
    Decimal128,
    Duration,
    action,
    contract,
)
from neolith._contracts import _STANDARD_KINDS
from neolith._openapi import _HEADERS, KIND_STATUS, _copy, document
from tests.strategies import admitted_by
from tests.test_contract_round_trip import (
    _FORMAT_CHECKER,
    _HYPOTHESIS,
    _TYPES,
    _name,
    _records,
)

_DATA = Path(__file__).parent / "data"
_SCHEMA_FILE = _DATA / "openapi-3.1-schema-2026-11-05.json"
_SCHEMA_SHA256 = "4751a7777357b26b09a9a61b9addd08081de2e65cf80d3ae0df2b4cd931b2d7b"


def _openapi_schema() -> dict[str, Any]:
    raw = _SCHEMA_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _SCHEMA_SHA256
    return cast("dict[str, Any]", json.loads(raw))


def _check(text: bytes) -> dict[str, Any]:
    """The document, read back after checking it as an OpenAPI 3.1 document."""
    tree = cast("dict[str, Any]", json.loads(text))
    Draft202012Validator(_openapi_schema()).validate(tree)
    for schema in tree["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    # Every reference to a component resolves inside the document.
    for ref in re.findall(rb'"\$ref":"(#/components/[^"]*)"', text):
        node: Any = tree
        for part in ref.decode().removeprefix("#/").split("/"):
            node = node[part]
    return tree


class Colour(enum.StrEnum):
    RED = "red"
    GREEN = "green"


class Part(BaseModel):
    sku: Annotated[str, Field(min_length=1)]
    quantity: Annotated[int, Field(ge=0)] = 1


class Order(BaseModel):
    """Every portable form, so every keyword the description writes appears."""

    flag: bool = False
    count: int = 0
    ratio: float = 1.0
    name: str = ""
    raw: Binary = b"\xff"
    amount: Decimal128 = Decimal("1.50")
    span: Duration = timedelta(seconds=90)
    when: AwareDatetime
    parts: list[Part] = []
    totals: dict[str, int] = {}
    colour: Colour = Colour.RED
    size: Literal["s", "m"] = "s"
    note: str | None = None


class Placed(BaseModel):
    order: str


class Missing(BaseModel):
    sku: str


class Tree(BaseModel):
    name: str
    children: list["Tree"] = []


_CANCEL_ERRORS: dict[str, type[BaseModel]] = {"out_of_stock": Missing, "held": Placed}


@contract("shop.orders.v1")
class OrdersV1(Protocol):
    @action(idempotent=True)
    def place(self, request: Order) -> Placed: ...

    @action(errors=_CANCEL_ERRORS)
    def cancel(self, request: Placed) -> None: ...

    @action
    def walk(self, request: Tree) -> Tree: ...


class Placed2(BaseModel):
    order: str
    at: AwareDatetime


@contract("shop.orders.v2")
class OrdersV2(Protocol):
    @action
    def place(self, request: Order) -> Placed2: ...


class Ping(BaseModel):
    pass


@contract("echo.v1")
class EchoV1(Protocol):
    @action
    def ping(self, request: Ping) -> None: ...


_ALL = (Contract.of(OrdersV1), Contract.of(OrdersV2), Contract.of(EchoV1))


@pytest.mark.spec("HTTP-20")
def test_document_is_a_valid_openapi_31_document() -> None:
    tree = _check(document(_ALL))
    assert tree["openapi"] == "3.1.2"


def test_document_with_no_contract_is_valid() -> None:
    tree = _check(document([]))
    assert tree["paths"] == {}
    assert tree["components"]["schemas"] == {}


def test_vendored_schema_refuses_what_it_should() -> None:
    """Guard against a schema file that admits anything."""
    validator = Draft202012Validator(_openapi_schema())
    good = json.loads(document([]))
    assert validator.is_valid(good)
    assert not validator.is_valid({**good, "openapi": "3.0.3"})
    assert not validator.is_valid({**good, "paths": {"no-slash": {}}})
    bad_key = {**good["components"], "schemas": {"a/b": {}}}
    assert not validator.is_valid({**good, "components": bad_key})


def _ref(key: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{key}"}


def _error(kind: str, data: object = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "kind": {"const": kind},
        "message": {"type": "string"},
    }
    if data is None:
        return {
            "type": "object",
            "properties": properties,
            "required": ["kind", "message"],
            "not": {"required": ["data"]},
        }
    return {
        "type": "object",
        "properties": {**properties, "data": data},
        "required": ["kind", "message", "data"],
    }


_STANDARD_RESPONSES = {
    str(status): {"$ref": f"#/components/responses/{kind}"}
    for kind, status in KIND_STATUS.items()
}
_PARAMETERS = [
    {"$ref": "#/components/parameters/Neolith-Principal"},
    {"$ref": "#/components/parameters/Neolith-Timeout"},
    {"$ref": "#/components/parameters/Idempotency-Key"},
    {"$ref": "#/components/parameters/traceparent"},
    {"$ref": "#/components/parameters/tracestate"},
]


def _operation(
    identity: str, action_name: str, *, idempotent: bool, request: str
) -> dict[str, Any]:
    return {
        "operationId": f"{identity}.{action_name}",
        "tags": [identity],
        "x-neolith-idempotent": idempotent,
        "parameters": _PARAMETERS,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _ref(request)}},
        },
    }


@pytest.mark.spec("HTTP-20")
def test_document_has_the_structure_spec_03_gives() -> None:
    tree = _check(document(_ALL))
    assert list(tree) == [
        "openapi",
        "info",
        "jsonSchemaDialect",
        "paths",
        "components",
        "security",
    ]
    assert tree["info"] == {"title": "neolith contracts", "version": "0.1"}
    assert tree["jsonSchemaDialect"] == "https://json-schema.org/draft/2020-12/schema"
    assert "servers" not in tree
    # Versions by identity, code point by code point; actions as declared.
    assert list(tree["paths"]) == [
        "/echo/v1/ping",
        "/shop.orders/v1/place",
        "/shop.orders/v1/cancel",
        "/shop.orders/v1/walk",
        "/shop.orders/v2/place",
    ]
    assert all(list(item) == ["post"] for item in tree["paths"].values())
    ops = {path: item["post"] for path, item in tree["paths"].items()}

    place = ops["/shop.orders/v1/place"]
    assert place == {
        **_operation(
            "shop.orders.v1", "place", idempotent=True, request="shop.orders.v1.Order"
        ),
        "responses": {
            "200": {
                "description": "The reply.",
                "content": {
                    "application/json": {"schema": _ref("shop.orders.v1.Placed")}
                },
            },
            **_STANDARD_RESPONSES,
        },
    }
    assert list(place["responses"]) == [
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "500",
        "503",
        "504",
    ]

    cancel = ops["/shop.orders/v1/cancel"]
    assert cancel == {
        **_operation(
            "shop.orders.v1",
            "cancel",
            idempotent=False,
            request="shop.orders.v1.Placed",
        ),
        "responses": {
            "204": {"description": "The reply is empty."},
            **_STANDARD_RESPONSES,
            "422": {
                "description": "The call failed with a kind the action declares.",
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                _error("out_of_stock", _ref("shop.orders.v1.Missing")),
                                _error("held", _ref("shop.orders.v1.Placed")),
                            ]
                        }
                    }
                },
            },
        },
    }
    assert list(cancel["responses"]) == [
        "204",
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
        "500",
        "503",
        "504",
    ]
    # No 422 where the action declares no kind.
    assert "422" not in ops["/shop.orders/v2/place"]["responses"]
    assert "422" not in ops["/echo/v1/ping"]["responses"]

    components = tree["components"]
    assert list(components) == [
        "schemas",
        "responses",
        "parameters",
        "securitySchemes",
    ]
    assert components["responses"] == {
        kind: {
            "description": f"The call failed with {kind}.",
            "content": {"application/json": {"schema": _error(kind)}},
        }
        for kind in KIND_STATUS
    }
    assert components["parameters"] == {
        name: {"name": name, "in": "header", "schema": schema}
        for name, schema in _HEADERS
    }
    assert [p["$ref"].rpartition("/")[2] for p in _PARAMETERS] == [
        name for name, _ in _HEADERS
    ]
    assert components["securitySchemes"] == {
        "bearer": {"type": "http", "scheme": "bearer"}
    }
    assert tree["security"] == [{"bearer": []}]


@pytest.mark.spec("HTTP-20")
def test_components_are_the_descriptions_defs_with_references_moved() -> None:
    tree = _check(document(_ALL))
    expected: dict[str, Any] = {}
    for identity, declaration in sorted((c.identity, c.declaration) for c in _ALL):
        defs = Contract.of(declaration).describe()["$defs"]
        text = json.dumps(defs).replace(
            '"#/$defs/', f'"#/components/schemas/{identity}.'
        )
        expected.update(
            {f"{identity}.{name}": schema for name, schema in json.loads(text).items()}
        )
    assert tree["components"]["schemas"] == expected
    # Two versions share record names; the key keeps them apart.
    schemas = tree["components"]["schemas"]
    assert "shop.orders.v1.Order" in schemas
    assert "shop.orders.v2.Order" in schemas
    assert schemas["shop.orders.v1.Tree"]["properties"]["children"]["items"] == _ref(
        "shop.orders.v1.Tree"
    )
    assert all(
        re.fullmatch(r"[a-zA-Z0-9\.\-_]+", key) for key in tree["components"]["schemas"]
    )


class Lookalike(BaseModel):
    """Data shaped like a reference, which the projection never rewrites."""

    links: dict[str, str] = {"$ref": "#/$defs/Lookalike"}
    target: Literal["#/$defs/Lookalike"] = "#/$defs/Lookalike"


@contract("lookalike.v1")
class LookalikeV1(Protocol):
    @action
    def get(self, request: Lookalike) -> Lookalike: ...


@pytest.mark.spec("HTTP-20")
def test_data_shaped_like_a_reference_is_copied_unchanged() -> None:
    described = Contract.of(LookalikeV1).describe()["$defs"]["Lookalike"]
    tree = _check(document([Contract.of(LookalikeV1)]))
    projected = tree["components"]["schemas"]["lookalike.v1.Lookalike"]
    assert projected == described
    assert projected["properties"]["links"]["default"] == {"$ref": "#/$defs/Lookalike"}


def test_text_is_written_once_compact_and_in_utf8() -> None:
    class Accent(BaseModel):
        name: str = "café"

    @contract("accent.v1")
    class AccentV1(Protocol):
        @action
        def get(self, request: Accent) -> None: ...

    text = document([Contract.of(AccentV1)])
    assert "café".encode() in text
    tree = json.loads(text)
    assert text == json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


@pytest.mark.spec("HTTP-20")
def test_equal_hosts_give_equal_bytes_whatever_the_order() -> None:
    forward = document(_ALL)
    assert document(reversed(_ALL)) == forward
    assert document([_ALL[1], _ALL[2], _ALL[0]]) == forward


_IN_A_FRESH_PROCESS = textwrap.dedent(
    """
    import sys
    from tests.test_openapi import _ALL
    from neolith._openapi import document
    sys.stdout.buffer.write(document(_ALL))
    """
)


@pytest.mark.spec("HTTP-20")
def test_equal_hosts_give_equal_bytes_across_processes() -> None:
    # String hashing differs between processes, so any order the projection
    # took from a set would show here. A declaration built from a set has its
    # own order in each process, and so another description: that is not an
    # equal host.
    root = Path(__file__).parent.parent
    outputs = []
    for seed in ("1", "2"):
        env = {"PYTHONPATH": os.pathsep.join(sys.path), "PYTHONHASHSEED": seed}
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _IN_A_FRESH_PROCESS],
            cwd=root,
            env=env,
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr.decode(errors="replace")
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1] == document(_ALL)


def test_one_identity_given_twice_is_refused() -> None:
    with pytest.raises(ValueError, match=r"echo\.v1 is given twice"):
        document([Contract.of(EchoV1), Contract.of(EchoV1)])


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/$defs/Nowhere"},
        {"$ref": "#/components/schemas/Order"},
        {"$ref": "other.json#/$defs/Order"},
        {"$ref": 1},
        {"$id": "https://example.com/x"},
        {"$defs": {}},
        {"$anchor": "a"},
        {"$dynamicRef": "#meta"},
        {"x-unknown": 1},
        {"properties": []},
        {"anyOf": {}},
        {"items": 1},
        [],
    ],
)
def test_a_description_outside_the_admitted_set_is_refused(schema: object) -> None:
    with pytest.raises(ValueError, match=r"^x\.v1: "):
        _copy("x.v1", {"Order": {}}, schema)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (True, True),
        ({"not": False}, {"not": False}),
        (
            {"items": {"$ref": "#/$defs/Order"}},
            {"items": {"$ref": "#/components/schemas/x.v1.Order"}},
        ),
        (
            {"prefixItems": [{"$ref": "#/$defs/Order"}], "enum": [{"$ref": "a"}]},
            {
                "prefixItems": [{"$ref": "#/components/schemas/x.v1.Order"}],
                "enum": [{"$ref": "a"}],
            },
        ),
        (
            {"patternProperties": {"^a$": {"$ref": "#/$defs/Order"}}},
            {"patternProperties": {"^a$": {"$ref": "#/components/schemas/x.v1.Order"}}},
        ),
        ({"properties": {"$ref": {"type": "string"}}}, None),
    ],
)
def test_a_description_inside_the_admitted_set_is_projected(
    schema: object, expected: object
) -> None:
    # A property named "$ref" is a name, not a reference.
    assert _copy("x.v1", {"Order": {}}, schema) == (
        schema if expected is None else expected
    )


@pytest.mark.spec("HTTP-20")
@pytest.mark.parametrize(
    ("status", "body", "admitted"),
    [
        ("500", {"kind": "internal", "message": "x"}, True),
        ("500", {"kind": "internal", "message": "x", "data": {"sku": "a"}}, False),
        ("500", {"kind": "internal", "message": "x", "data": None}, False),
        ("500", {"kind": "timeout", "message": "x"}, False),
        ("500", {"kind": "internal"}, False),
        ("422", {"kind": "out_of_stock", "message": "x", "data": {"sku": "a"}}, True),
        ("422", {"kind": "held", "message": "x", "data": {"order": "o"}}, True),
        ("422", {"kind": "held", "message": "x", "data": {"sku": "a"}}, False),
        ("422", {"kind": "out_of_stock", "message": "x"}, False),
        ("422", {"kind": "internal", "message": "x", "data": {"sku": "a"}}, False),
    ],
)
def test_error_bodies_admit_spec_03s_forms(
    status: str, body: dict[str, Any], admitted: bool
) -> None:
    # A standard kind's body never has data; a declared kind's always does,
    # holding that kind's detail record.
    tree = _check(document(_ALL))
    response = tree["paths"]["/shop.orders/v1/cancel"]["post"]["responses"][status]
    if "$ref" in response:
        response = tree["components"]["responses"][response["$ref"].rpartition("/")[2]]
    schema = {
        **response["content"]["application/json"]["schema"],
        "components": tree["components"],
    }
    assert Draft202012Validator(schema).is_valid(body) is admitted


def test_kind_status_covers_every_standard_kind() -> None:
    assert set(KIND_STATUS) == _STANDARD_KINDS


def _component_schema(tree: dict[str, Any], key: str) -> dict[str, Any]:
    """A schema for one component that resolves its references in the document."""
    return {"$ref": f"#/components/schemas/{key}", "components": tree["components"]}


def _round_trip(record: type[BaseModel], schema: dict[str, Any], value: object) -> None:
    instance = record.model_validate_json(json.dumps(value))
    sent = instance.model_dump_json()
    Draft202012Validator(schema, format_checker=_FORMAT_CHECKER).validate(
        json.loads(sent)
    )
    assert record.model_validate_json(sent) == instance


@pytest.mark.spec("CON-16", "HTTP-20")
@_HYPOTHESIS
@given(st.data())
def test_generated_contracts_round_trip_through_their_components(
    data: st.DataObject,
) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    detail = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])

    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    namespace = {"send": action(errors={"failed": detail})(send)}
    declared = types.new_class(
        _name("C"), (Protocol,), exec_body=lambda ns: ns.update(namespace)
    )
    identity = f"{_name('generated')}.v1"
    try:
        contract(identity)(declared)
    except ContractDefinitionError:
        # Refused: the property is about contracts that are accepted.
        return
    tree = _check(document([Contract.of(declared)]))
    operation = tree["paths"][f"/{identity[:-3]}/v1/send"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_ref == _ref(f"{identity}.{record.__name__}")
    (failed,) = operation["responses"]["422"]["content"]["application/json"]["schema"][
        "oneOf"
    ]
    assert failed["properties"]["data"] == _ref(f"{identity}.{detail.__name__}")
    for kept in (record, detail):
        schema = _component_schema(tree, f"{identity}.{kept.__name__}")
        value = data.draw(admitted_by(schema))
        _round_trip(kept, schema, value)


@pytest.mark.spec("CON-16", "HTTP-20")
@_HYPOTHESIS
@given(st.data())
def test_reference_contract_round_trips_through_its_components(
    data: st.DataObject,
) -> None:
    tree = _check(document(_ALL))
    schema = _component_schema(tree, "shop.orders.v1.Order")
    _round_trip(Order, schema, data.draw(admitted_by(schema)))


# The forms of the header parameters, as the rules state them, written without
# a regular expression.
def _is_timeout(text: str) -> bool:
    return (
        1 <= len(text) <= 10
        and all(c in "0123456789" for c in text)
        and not text.startswith("0")
    )


def _is_key(text: str) -> bool:
    """An RFC 9651 String of 1 to 255 characters after unescaping."""
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return False
    count, i, inner = 0, 0, text[1:-1]
    while i < len(inner):
        c = inner[i]
        if c == "\\":
            if i + 1 >= len(inner) or inner[i + 1] not in '"\\':
                return False
            i += 2
        elif c == '"' or not 0x20 <= ord(c) <= 0x7E:
            return False
        else:
            i += 1
        count += 1
    return 1 <= count <= 255


def _is_principal(text: str) -> bool:
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    return len(text) <= 4096 and set(text) <= alphabet


_FORMS = {
    "Neolith-Timeout": _is_timeout,
    "Idempotency-Key": _is_key,
    "Neolith-Principal": _is_principal,
}

_HEADER_TEXT = st.one_of(
    st.text(alphabet=st.sampled_from('0123456789"\\ab -~\x7f\x1f\xe9'), max_size=12),
    st.integers(0, 10**11).map(str),
    st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E))
    .filter(lambda t: '"' not in t and "\\" not in t)
    .map(lambda t: f'"{t}"'),
    st.integers(250, 260).map(lambda n: '"' + "a" * n + '"'),
    st.integers(250, 260).map(lambda n: '"' + "\\\\" * n + '"'),
    st.integers(4090, 4100).map(lambda n: "A" * n),
    st.just(""),
    st.just("1\n"),
)


@pytest.mark.spec("HTTP-20")
@_HYPOTHESIS
@given(st.sampled_from(sorted(_FORMS)), _HEADER_TEXT)
def test_header_parameter_schemas_state_the_header_forms(name: str, text: str) -> None:
    schema = dict(_HEADERS)[name]
    expected = _FORMS[name](text)
    within = len(text) <= schema.get("maxLength", len(text))
    assert (re.fullmatch(schema["pattern"][1:-1], text) is not None and within) is (
        expected
    )
    # python-jsonschema reads a pattern with re.search, where "$" also matches
    # before a final newline; a header value never holds one.
    if not text.endswith("\n"):
        assert Draft202012Validator(schema).is_valid(text) is expected
