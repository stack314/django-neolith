"""Wire part: the projection a provider of the reference contract serves
(CNF-2).

The suite's own caller fetches the OpenAPI document from the live server
and compares the reference contract's part of it with the reference
document, ``spec/conformance/reference.v1.openapi.json``: the reference
contract's paths, every component they reach, and the top-level members
spec 03's projection list fixes. Members are compared as JSON values, so
their order and spacing are free. Arrays compare in order, apart from
``required``, ``enum``, ``oneOf``, ``anyOf`` and ``allOf``, which compare
without their order: the same entries, each as often. A second case checks
the order spec 03 gives the paths (HTTP-20). The cases are listed in
``spec/conformance/cases/wire-provider.json``.

The comparison is the suite's oracle, so the tests after the case check it
on its own: each rule it applies, and the nearest change it must catch.
"""

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from tests.conformance import Live
from tests.conformance.wire import OPENAPI, Caller, NotJSON, read_json

REFERENCE: Final = (
    Path(__file__).resolve().parents[3]
    / "spec"
    / "conformance"
    / "reference.v1.openapi.json"
)
PREFIX: Final = "/reference/v1/"
# Arrays spec 03 does not order.
_SETS: Final = frozenset({"required", "enum", "oneOf", "anyOf", "allOf"})
# Members whose value is data, never a schema or a keyword.
_DATA: Final = frozenset({"const", "default", "example", "examples", "enum"})
# Members whose value is an object of names (properties, paths, components),
# each mapping to a schema or an OpenAPI object.
_NAMES: Final = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "dependentSchemas",
        "paths",
        "components",
        "schemas",
        "responses",
        "parameters",
        "securitySchemes",
        "requestBodies",
        "headers",
        "content",
    }
)
# The top-level members spec 03's projection list fixes, apart from paths
# and components, which hold every contract version a host provides.
_TOP: Final = ("openapi", "info", "jsonSchemaDialect", "security")

Canonical = tuple[Any, ...]


def canonical(value: object, mode: str = "keywords") -> Canonical:
    """``value`` in a form that compares equal exactly when the JSON values
    are equal under CNF-2's rule. ``mode`` says what ``value`` is: an object
    of ``keywords``, an object of ``names``, or ``data``.
    """
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, int | Decimal):
        # Equal numbers are one value, whatever their spelling: 1 and 1.0.
        return ("number", Decimal(value))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        item = "data" if mode == "data" else "keywords"
        return ("array", tuple(canonical(entry, item) for entry in value))
    assert isinstance(value, dict), value
    members = []
    for name, member in value.items():
        if mode == "data":
            members.append((name, canonical(member, "data")))
        elif mode == "names":
            members.append((name, canonical(member, "keywords")))
        elif name in _SETS and isinstance(member, list):
            item = "data" if name in _DATA else "keywords"
            entries = sorted(canonical(entry, item) for entry in member)
            members.append((name, ("set", tuple(entries))))
        elif name in _DATA:
            members.append((name, canonical(member, "data")))
        elif name in _NAMES:
            members.append((name, canonical(member, "names")))
        else:
            members.append((name, canonical(member, "keywords")))
    return ("object", tuple(sorted(members)))


def _refs(node: object) -> set[str]:
    """Every ``$ref`` value in ``node``, at any depth."""
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        for value in node.values():
            found |= _refs(value)
        return found
    if isinstance(node, list):
        found = set()
        for value in node:
            found |= _refs(value)
        return found
    return set()


def reference_part(tree: dict[str, Any]) -> dict[str, Any]:
    """The reference contract's part of an OpenAPI document: its paths,
    every component they reach, the security schemes the document requires,
    and the top-level members the projection fixes. A member it needs and
    cannot find fails the comparison, never the reader.
    """
    paths = {
        p: item for p, item in tree.get("paths", {}).items() if p.startswith(PREFIX)
    }
    components = tree.get("components", {})
    reached: dict[str, dict[str, Any]] = {}
    pending = _refs(paths)
    while pending:
        ref = pending.pop()
        assert ref.startswith("#/components/"), f"a reference outside components: {ref}"
        section, _, name = ref.removeprefix("#/components/").partition("/")
        name = name.replace("~1", "/").replace("~0", "~")
        if name in reached.setdefault(section, {}):
            continue
        assert name in components.get(section, {}), f"{ref} names nothing"
        reached[section][name] = components[section][name]
        pending |= _refs(components[section][name])
    schemes = {name for entry in tree.get("security", []) for name in entry}
    reached["securitySchemes"] = {
        name: components.get("securitySchemes", {}).get(name) for name in schemes
    }
    part = {member: tree.get(member) for member in _TOP}
    part["servers"] = tree.get("servers")
    part["paths"] = paths
    part["components"] = reached
    return part


def holds(served: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    """Where the reference part of ``served`` differs from that of
    ``reference``, by top-level member, path and component; empty when it
    holds it unchanged.
    """
    mine, theirs = reference_part(served), reference_part(reference)
    differs = []
    for member in (*_TOP, "servers"):
        if canonical(mine[member]) != canonical(theirs[member]):
            differs.append(member)
    for name in sorted(set(mine["paths"]) | set(theirs["paths"])):
        if canonical(mine["paths"].get(name)) != canonical(theirs["paths"].get(name)):
            differs.append(f"paths {name}")
    sections = set(mine["components"]) | set(theirs["components"])
    for section in sorted(sections):
        ours = mine["components"].get(section, {})
        expected = theirs["components"].get(section, {})
        for name in sorted(set(ours) | set(expected)):
            if canonical(ours.get(name)) != canonical(expected.get(name)):
                differs.append(f"components {section} {name}")
    return differs


def _reference() -> dict[str, Any]:
    tree = read_json(REFERENCE.read_bytes())
    assert isinstance(tree, dict)
    return tree


def _served(live: Live) -> dict[str, Any]:
    answer = Caller(live.origin, live.settle).request("GET", OPENAPI)
    assert answer.status == 200, answer
    assert answer.field("Content-Type") == "application/json"
    served = read_json(answer.body)
    assert isinstance(served, dict)
    return served


@pytest.mark.conformance_case("wire_projection_holds_the_reference_document")
@pytest.mark.spec("CNF-2", "HTTP-2")
def test_the_served_document_holds_the_reference_document(live: Live) -> None:
    served = _served(live)
    assert holds(served, _reference()) == []
    # This suite's test host provides other contract versions too, so the
    # case runs where the comparison has to leave them out.
    assert set(served["paths"]) > set(_reference()["paths"])


def _identity(path: str) -> str:
    """The identity a path's contract version has: ``<name>.v<N>``."""
    _, name, version, _ = path.split("/", 3)
    return f"{name}.{version}"


@pytest.mark.conformance_case("wire_projection_orders_its_paths")
@pytest.mark.spec("HTTP-20")
def test_the_served_paths_are_in_the_projection_s_order(live: Live) -> None:
    paths = list(_served(live)["paths"])
    identities = [_identity(path) for path in paths]
    # Contract versions ordered by identity, code point by code point, each
    # version's paths together.
    assert identities == sorted(identities)
    # The reference contract's actions in their declaration order, which is
    # the reference document's.
    ours = [path for path in paths if path.startswith(PREFIX)]
    assert ours == list(_reference()["paths"])


# The comparison itself.


def test_the_reference_document_is_its_own_part() -> None:
    reference = _reference()
    part = reference_part(reference)
    assert part.pop("servers") is None
    assert canonical(part) == canonical(reference)


def _changed(change: str) -> dict[str, Any]:
    tree = copy.deepcopy(_reference())
    schemas = tree["components"]["schemas"]
    values = schemas["reference.v1.Values"]
    echo = tree["paths"]["/reference/v1/echo"]["post"]
    if change == "members reordered":
        values.update(dict(reversed(list(values.items()))))
        tree = dict(reversed(list(tree.items())))
    elif change == "required reordered":
        values["required"].reverse()
    elif change == "enum reordered":
        schemas["reference.v1.Colour"]["enum"].reverse()
    elif change == "another contract":
        tree["paths"]["/other/v1/go"] = echo
        schemas["other.v1.Thing"] = {"type": "object"}
    elif change == "an integer written as a fraction":
        values["properties"]["integer"]["minimum"] = Decimal("-9007199254740991.0")
    elif change == "parameters reordered":
        echo["parameters"].reverse()
    elif change == "true as 1":
        echo["x-neolith-idempotent"] = 1
    elif change == "a reached component changed":
        schemas["reference.v1.Nested"]["properties"]["label"]["type"] = "integer"
    elif change == "a component left out":
        del schemas["reference.v1.Nested"]
    elif change == "a path left out":
        del tree["paths"]["/reference/v1/stall"]
    elif change == "a path added":
        tree["paths"]["/reference/v1/extra"] = echo
    elif change == "servers added":
        tree["servers"] = [{"url": "/"}]
    elif change == "info changed":
        tree["info"]["version"] = "0.2"
    elif change == "required with one name more":
        values["required"].append("optional_boolean")
    elif change == "the scheme changed":
        tree["components"]["securitySchemes"]["bearer"]["scheme"] = "basic"
    elif change == "a default changed":
        values["properties"]["optional_list"]["default"] = [1, 2]
    else:  # pragma: no cover
        raise AssertionError(change)
    return tree


@pytest.mark.parametrize(
    "change",
    [
        "members reordered",
        "required reordered",
        "enum reordered",
        "another contract",
        "an integer written as a fraction",
    ],
)
def test_what_the_comparison_leaves_free_still_holds(change: str) -> None:
    assert holds(_changed(change), _reference()) == []


@pytest.mark.parametrize(
    ("change", "where"),
    [
        ("parameters reordered", ["paths /reference/v1/echo"]),
        ("true as 1", ["paths /reference/v1/echo"]),
        ("a reached component changed", ["components schemas reference.v1.Nested"]),
        ("a component left out", None),
        ("a path left out", ["paths /reference/v1/stall"]),
        ("a path added", ["paths /reference/v1/extra"]),
        ("servers added", ["servers"]),
        ("info changed", ["info"]),
        ("required with one name more", ["components schemas reference.v1.Values"]),
        ("the scheme changed", ["components securitySchemes bearer"]),
        ("a default changed", ["components schemas reference.v1.Values"]),
    ],
)
def test_every_other_change_to_the_part_is_found(
    change: str, where: list[str] | None
) -> None:
    tree = _changed(change)
    if where is None:
        # A reference that names nothing fails the comparison itself.
        with pytest.raises(AssertionError, match="names nothing"):
            holds(tree, _reference())
    else:
        assert holds(tree, _reference()) == where


@pytest.mark.parametrize("name", sorted(_SETS))
def test_each_array_spec_03_does_not_order_compares_without_order(
    name: str,
) -> None:
    entries = [{"type": "string"}, {"type": "null"}, "a"]
    assert canonical({name: entries}) == canonical({name: entries[::-1]})
    assert canonical({name: entries}) != canonical({name: entries[:2]})
    # Any other array keeps its order.
    assert canonical({"prefixItems": entries}) != canonical(
        {"prefixItems": entries[::-1]}
    )


def test_an_unordered_array_keeps_how_often_each_entry_occurs() -> None:
    # oneOf with an entry twice matches nothing either entry matches, so
    # the count is part of what the array says.
    once = {"required": ["a", "b"]}
    twice = {"required": ["a", "b", "b"]}
    assert canonical(once) != canonical(twice)
    assert canonical({"required": ["b", "a"]}) == canonical(once)


def test_a_name_that_is_a_keyword_is_still_a_name() -> None:
    # A record whose fields are called required and enum: their schemas are
    # schemas, and their arrays keep their order where a schema's do.
    def record(order: list[str]) -> dict[str, Any]:
        return {"properties": {"required": {"prefixItems": order}, "enum": {}}}

    assert canonical(record(["a", "b"])) != canonical(record(["b", "a"]))
    assert canonical(record(["a", "b"])) == canonical(record(["a", "b"]))

    # A field called default, const or enum keeps its schema too: its
    # anyOf compares without order, as a schema's does.
    for name in ("default", "const", "enum", "example", "examples"):

        def field(entries: list[object], name: str = name) -> dict[str, Any]:
            return {"properties": {name: {"anyOf": entries}}}

        one, other = {"type": "string"}, {"type": "null"}
        assert canonical(field([one, other])) == canonical(field([other, one]))


def test_data_is_compared_as_written() -> None:
    def default(value: object) -> dict[str, Any]:
        return {"default": value}

    # Inside data, a member called required is data, and its array ordered.
    assert canonical(default({"required": [1, 2]})) != canonical(
        default({"required": [2, 1]})
    )
    assert canonical(default(True)) != canonical(default(1))
    assert canonical(default("1")) != canonical(default(1))
    assert canonical(default(None)) != canonical(default(False))


@pytest.mark.parametrize(
    "text",
    [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1}\xff'],
    ids=["a member twice", "NaN", "Infinity", "not UTF-8"],
)
def test_the_reader_refuses_text_that_is_not_i_json(text: bytes) -> None:
    with pytest.raises(NotJSON):
        read_json(text)
