"""Contract part: a deadline holds alike (CNF-1, spec 02).

The cases call ``reference.v1`` from the reference apps of the test host, over
the in-process and the HTTP transport. A deadline reaches the provider when
the scope has one and not when it has none, a call from a scope whose
deadline has passed is never sent, and a deadline that passes is a
``timeout`` over both and leaves the host fit for the next scope. Each leg's
own outcome is judged, not only the one a case returns. Each cites CNF-1, and
HTTP-1: the HTTP transport, with the in-process one, is what it runs over.
"""

import uuid
from collections.abc import Callable

import pytest

import neolith
from neolith import ContractError
from tests.conformance.contract.conftest import OverBoth, balance_of
from tests.host_apps.reference.contract import Deposit, Empty, Seen

CALLER = "reference_caller"

PARITY = pytest.mark.spec("CNF-1", "HTTP-1")


@PARITY
@pytest.mark.spec("TRN-27", "TRN-36", "TRN-48", "TRN-29")
@pytest.mark.conformance_case("deadline_reaches_the_provider")
@pytest.mark.parametrize(
    "timeout",
    [None, 10, 0.5, 10**9, float(10**300)],
    ids=["none", "ten", "half a second", "above the cap", "far above the cap"],
)
def test_the_provider_sees_a_deadline_only_when_the_scope_has_one(
    over_both: OverBoth, timeout: float | None
) -> None:
    replies: list[object] = []

    def case() -> object:
        with neolith.command_scope(CALLER, timeout=timeout):
            replies.append(neolith.connect("reference.v1").seen(Empty()))
        return replies[-1]

    over_both(case)
    assert len(replies) == 2
    for reply in replies:
        assert isinstance(reply, Seen)
        assert reply.deadline is (timeout is not None)


@PARITY
@pytest.mark.spec("TRN-4", "TRN-28", "TRN-48")
@pytest.mark.conformance_case("deadline_passed_before_the_call")
@pytest.mark.parametrize("timeout", [0, -1, -0.5, -1e9], ids=repr)
def test_a_timeout_of_zero_or_less_is_a_timeout_and_the_call_is_never_sent(
    over_both: OverBoth, timeout: float
) -> None:
    accounts: list[str] = []
    errors: list[ContractError] = []

    def case() -> object:
        accounts.append(uuid.uuid4().hex)
        with neolith.command_scope(CALLER, timeout=timeout):
            try:
                return neolith.connect("reference.v1").deposit(
                    Deposit(account=accounts[-1], amount=1)
                )
            except ContractError as error:
                errors.append(error)
                raise

    over_both(case)
    assert [error.kind for error in errors] == ["timeout", "timeout"]
    assert [balance_of(account) for account in accounts] == [0, 0]


@PARITY
@pytest.mark.spec("TRN-4", "TRN-28", "TRN-38")
@pytest.mark.conformance_case("deadline_passed_in_the_scope")
def test_once_a_deadline_has_passed_every_later_call_of_the_scope_is_a_timeout(
    over_both: OverBoth,
) -> None:
    accounts: list[str] = []
    kinds: list[tuple[str, str]] = []

    def case() -> object:
        accounts.append(uuid.uuid4().hex)
        with neolith.command_scope(CALLER, timeout=0.2):
            connector = neolith.connect("reference.v1")
            outcomes = []
            # stall returns only once its deadline has passed.
            calls: tuple[Callable[[], object], ...] = (
                lambda: connector.stall(Empty()),
                lambda: connector.deposit(Deposit(account=accounts[-1], amount=1)),
            )
            for call in calls:
                try:
                    call()
                except ContractError as error:
                    outcomes.append(error.kind)
            kinds.append((outcomes[0], outcomes[1]))
            return tuple(outcomes)

    assert over_both(case).reply == ("timeout", "timeout")
    assert kinds == [("timeout", "timeout")] * 2
    # The second call was not sent: its account holds nothing.
    assert [balance_of(account) for account in accounts] == [0, 0]


@PARITY
@pytest.mark.spec("TRN-4", "TRN-38", "TRN-48")
@pytest.mark.conformance_case("deadline_passed_leaves_the_next_scope_fit")
def test_a_timeout_leaves_the_next_scope_with_a_deadline_of_its_own(
    over_both: OverBoth,
) -> None:
    accounts: list[str] = []

    def case() -> object:
        accounts.append(uuid.uuid4().hex)
        with neolith.command_scope(CALLER, timeout=0.1):
            with pytest.raises(ContractError) as raised:
                neolith.connect("reference.v1").stall(Empty())
            assert raised.value.kind == "timeout"
        # A new root counts its own timeout from when it opens (TRN-48).
        with neolith.command_scope(CALLER, timeout=10):
            return neolith.connect("reference.v1").deposit(
                Deposit(account=accounts[-1], amount=3)
            )

    assert over_both(case).reply.total == 3  # type: ignore[attr-defined]
    assert [balance_of(account) for account in accounts] == [3, 3]
