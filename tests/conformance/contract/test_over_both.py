"""Contract part: one call over the in-process and the HTTP transport gives
the caller the same outcome.

The cases call the test host's own contracts, ``stock.v1`` and
``couriers.v3``, on a host whose apps sign their calls over HTTP and accept
them under one key (``tests/settings.py``). They stand in for the cases over
the reference contract that step 6 writes in their place. Every case cites
HTTP-1: the HTTP transport, with the in-process one, is what it runs over.
"""

import pytest

import neolith
from neolith import Principal, UsageError, _context
from tests.conformance.contract.conftest import OverBoth
from tests.declaration_contracts import Level, Sku
from tests.host_apps.couriers import services as couriers

ALICE = Principal("https://id.example.com", "alice", {"roles": ["clerk"]})


@pytest.mark.spec("HTTP-1", "TRN-1", "TRN-16", "HTTP-41")
def test_a_reply_is_the_same_over_both(over_both: OverBoth) -> None:
    def call() -> object:
        with neolith.command_scope("web"):
            return neolith.connect("stock.v1").level(Sku(sku="abcde"))

    assert over_both(call, identity="stock.v1").reply == Level(on_hand=5)


@pytest.mark.spec("HTTP-1", "TRN-31", "HTTP-42")
def test_the_provider_sees_the_same_calling_app_over_both(over_both: OverBoth) -> None:
    # Over HTTP the calling app is the one the token names, authenticated
    # under the key both sides hold; in-process it is the scope's app.
    def call() -> object:
        with neolith.command_scope("web"):
            level = neolith.connect("couriers.v3").quote(Sku(sku="abc"))
        return level, couriers.SEEN[-1]

    outcome = over_both(call, identity="couriers.v3")
    assert outcome.reply == (Level(on_hand=3), ("web", None))


@pytest.mark.spec("HTTP-1", "TRN-2", "TRN-36", "HTTP-46")
def test_the_principal_reaches_the_provider_and_its_calls_alike(
    over_both: OverBoth,
) -> None:
    # stock.v1 admits a principal from web; its action calls couriers.v3,
    # which admits one from stock, in-process in both runs.
    def call() -> object:
        with neolith.command_scope("web", principal=ALICE):
            level = neolith.connect("stock.v1").level(Sku(sku="ab"))
        return level, couriers.SEEN[-1]

    outcome = over_both(call, identity="stock.v1")
    assert outcome.reply == (Level(on_hand=2), ("stock", ALICE))


@pytest.mark.spec("HTTP-1", "TRN-43", "HTTP-46")
def test_a_principal_from_an_app_off_the_allow_list_is_refused_alike(
    over_both: OverBoth,
) -> None:
    # couriers.v3 admits a principal from stock only.
    seen = len(couriers.SEEN)

    def call() -> object:
        with neolith.command_scope("web", principal=ALICE):
            return neolith.connect("couriers.v3").quote(Sku(sku="abc"))

    outcome = over_both(call, identity="couriers.v3")
    assert outcome.kind == "permission_denied"
    assert outcome.error.args == (
        "permission_denied",
        "app web may not assert a principal on a call to couriers.v3: the "
        "contract's allow-list does not name it",
    )
    assert len(couriers.SEEN) == seen


@pytest.mark.spec("HTTP-1", "HTTP-18", "TRN-42")
def test_a_call_from_no_app_is_refused_alike_and_never_sent(
    over_both: OverBoth,
) -> None:
    seen = len(couriers.SEEN)

    def call() -> object:
        with _context.root(None, requires=("couriers.v3",)):
            return neolith.connect("couriers.v3").quote(Sku(sku="abc"))

    outcome = over_both(call, identity="couriers.v3")
    assert outcome.kind == "unauthenticated"
    assert outcome.error.args[1] == (
        "a call to couriers.v3 has no calling app; a call is made from a scope "
        "that belongs to an app"
    )
    assert len(couriers.SEEN) == seen


@pytest.mark.spec("HTTP-1", "TRN-4", "TRN-28", "TRN-48")
def test_a_call_whose_deadline_has_passed_is_a_timeout_over_both(
    over_both: OverBoth,
) -> None:
    # A timeout of zero gives a root whose deadline has passed (TRN-48), so
    # the deadline passes first, before anything is sent, in both runs.
    seen = len(couriers.SEEN)

    def call() -> object:
        with neolith.command_scope("web", timeout=0):
            return neolith.connect("couriers.v3").quote(Sku(sku="abc"))

    assert over_both(call, identity="couriers.v3").kind == "timeout"
    assert len(couriers.SEEN) == seen


@pytest.mark.spec("HTTP-1", "TRN-14", "TRN-15")
def test_a_request_the_caller_refuses_is_refused_alike_and_never_sent(
    over_both: OverBoth,
) -> None:
    seen = len(couriers.SEEN)

    def call() -> object:
        with neolith.command_scope("web"):
            return neolith.connect("couriers.v3").quote(Sku.model_construct(sku=5))

    outcome = over_both(call, identity="couriers.v3")
    assert outcome.raised
    assert not isinstance(outcome.error, neolith.ContractError)
    assert len(couriers.SEEN) == seen


@pytest.mark.spec("HTTP-1", "TRN-39")
def test_a_call_to_a_version_the_app_does_not_require_is_refused_alike(
    over_both: OverBoth,
) -> None:
    # couriers requires nothing.
    def call() -> object:
        with neolith.command_scope("couriers"):
            return neolith.connect("stock.v1").level(Sku(sku="abc"))

    assert isinstance(over_both(call, identity="stock.v1").error, UsageError)


def test_over_both_refuses_what_it_cannot_run(over_both: OverBoth) -> None:
    with pytest.raises(TypeError):
        over_both("not a call")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        over_both(lambda: None, identity=b"stock.v1")  # type: ignore[arg-type]
    with pytest.raises(UsageError, match=r"does not know desk\.v1"):
        over_both(lambda: None, identity="desk.v1")
    assert over_both.runs == 0
