"""The reference contract, ``reference.v1``, and its committed document.

``spec/conformance/reference.v1.openapi.json`` is the reference contract's
neutral definition (ADR 0032 Decision 3). These tests hold it equal to the
projection of the contract the test host provides, and check the reference
provider's own behaviour where a call over either transport cannot show it:
``deposit`` under a repeated key, and ``stall`` waiting for its deadline.
Calls over both transports are the contract part's
(``tests/conformance/contract/test_over_both.py``).
"""

import json
import time
import uuid
from typing import Any, cast

import pytest
from django.test import Client

from neolith import CallContext, ContractError, _endpoint
from tests.host_apps.reference import document
from tests.host_apps.reference.contract import Balance, Deposit, Empty
from tests.host_apps.reference.services import STALLED, ReferenceService

# The members spec 03's projection list fixes at the top level, apart from
# paths and components, which hold every contract version the host provides.
_TOP: tuple[str, ...] = ("openapi", "info", "jsonSchemaDialect", "security")


def _refs(node: object) -> set[str]:
    """Every ``$ref`` value in ``node``, at any depth."""
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        for value in node.values():
            found |= _refs(value)
        return found
    if isinstance(node, list):
        return set().union(*map(_refs, node)) if node else set()
    return set()


def reference_part(tree: dict[str, Any], prefix: str) -> dict[str, Any]:
    """The part of an OpenAPI document that describes the contract version
    whose paths start with ``prefix``: those paths, every component they
    reach, the security schemes the document requires, and the top-level
    members the projection fixes.
    """
    paths = {
        path: item for path, item in tree["paths"].items() if path.startswith(prefix)
    }
    components = tree["components"]
    reached: dict[str, dict[str, Any]] = {}
    pending = _refs(paths)
    while pending:
        ref = pending.pop()
        _, _, section, name = ref.split("/", 3)
        if name in reached.setdefault(section, {}):
            continue
        reached[section][name] = components[section][name]
        pending |= _refs(components[section][name])
    schemes = {name for entry in tree["security"] for name in entry}
    reached["securitySchemes"] = {
        name: components["securitySchemes"][name] for name in schemes
    }
    part = {member: tree[member] for member in _TOP}
    part["paths"] = paths
    part["components"] = reached
    return part


def _text(tree: object) -> str:
    return json.dumps(tree, sort_keys=True, ensure_ascii=False)


def test_the_committed_document_is_the_projection_of_the_reference_contract() -> None:
    assert document.PATH.read_text(encoding="utf-8") == document.text(), (
        "spec/conformance/reference.v1.openapi.json is out of date; write it "
        "again with: python -m tests.host_apps.reference.document"
    )


def test_the_served_document_holds_the_committed_one() -> None:
    served = Client().get(_endpoint.OPENAPI)
    assert served.status_code == 200
    tree = cast("dict[str, Any]", json.loads(served.content))
    committed = json.loads(document.PATH.read_text(encoding="utf-8"))
    # Compared as JSON text with sorted members, so true is never 1.
    assert _text(reference_part(tree, "/reference/v1/")) == _text(committed)
    # The served document describes the host's other contracts too.
    assert set(tree["paths"]) > set(committed["paths"])


def test_the_reference_part_leaves_out_every_other_contract() -> None:
    tree = json.loads(Client().get(_endpoint.OPENAPI).content)
    part = reference_part(tree, "/reference/v1/")
    assert set(part["components"]["schemas"]) == {
        name
        for name in tree["components"]["schemas"]
        if name.startswith("reference.v1.")
    }
    assert _text(part) == _text(reference_part(part, "/reference/v1/"))


def _context(key: str | None, deadline: float | None = None) -> CallContext:
    return CallContext(
        principal=None,
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        tracestate="",
        deadline=deadline,
        idempotency_key=key,
        caller="reference_caller",
        scope_id="test",
    )


def test_a_deposit_takes_effect_once_per_key() -> None:
    service, account = ReferenceService(), uuid.uuid4().hex
    first = service.deposit(Deposit(account=account, amount=5), _context("k1"))
    again = service.deposit(Deposit(account=account, amount=5), _context("k1"))
    other = service.deposit(Deposit(account=account, amount=5), _context("k2"))
    assert (first, again, other) == (
        Balance(total=5),
        Balance(total=5),
        Balance(total=10),
    )
    # A key counts per account, and a call with no key always takes effect.
    elsewhere = service.deposit(
        Deposit(account=account + "x", amount=1), _context("k1")
    )
    assert elsewhere == Balance(total=1)
    assert service.deposit(Deposit(account=account, amount=1), _context(None)) == (
        Balance(total=11)
    )
    assert service.deposit(Deposit(account=account, amount=1), _context(None)) == (
        Balance(total=12)
    )


def test_stall_returns_only_once_its_deadline_has_passed() -> None:
    deadline = time.monotonic() + 0.05
    assert ReferenceService().stall(Empty(), _context("k", deadline)) == STALLED
    assert time.monotonic() >= deadline


def test_stall_refuses_a_call_with_no_deadline_at_once() -> None:
    with pytest.raises(ContractError) as raised:
        ReferenceService().stall(Empty(), _context("k"))
    assert raised.value.kind == "invalid_argument"
