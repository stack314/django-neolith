"""Contract part: one call over the in-process and the HTTP transport gives
the caller the same outcome (CNF-1).

Every case calls the reference contract, ``reference.v1``, from the
reference apps of the test host (ADR 0032 Decision 3), on a host whose apps
sign their calls over HTTP and accept them under one key
(``tests/settings.py``). Each cites CNF-1, and HTTP-1: the HTTP transport,
with the in-process one, is what it runs over. A case that writes state draws
a fresh account per run, so the second run never sees the first.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import neolith
from neolith import ContractError, Principal, UsageError, _context
from tests.conformance.contract.conftest import OverBoth
from tests.host_apps.reference.contract import (
    Balance,
    Colour,
    Deposit,
    Empty,
    Gone,
    Nested,
    Refusal,
    RefusalKind,
    Seen,
    SeenPrincipal,
    Values,
)

CALLER = "reference_caller"
STRANGER = "reference_stranger"
ALICE = Principal("https://id.example.com", "alice", {"roles": ["clerk"], "level": 3})

# Every portable form, each required one set and each optional one set.
EVERY = Values(
    boolean=True,
    integer=2**53 - 1,
    number=-1.5e300,
    string="naïve \U0001f600 ￿",
    bytes=b"\x00\xff\xfe",
    decimal=Decimal("-12.340"),
    timestamp=datetime(2027, 2, 28, 23, 59, 59, 999999, tzinfo=UTC),
    date=date(1, 1, 1),
    duration=timedelta(seconds=-2, microseconds=1),
    list=[0, -(2**53 - 1)],
    map={"": 0, "k": 1},
    record=Nested(label="inner"),
    enum=Colour.GREEN,
    optional_boolean=False,
    optional_integer=0,
    optional_number=0.0,
    optional_string="",
    optional_bytes=b"",
    optional_decimal=Decimal("0.00"),
    optional_timestamp=datetime(9999, 12, 31, 12, tzinfo=timezone(timedelta(hours=-5))),
    optional_date=date(9999, 12, 31),
    optional_duration=timedelta(0),
    optional_list=[],
    optional_map={},
    optional_record=Nested(label=""),
    optional_enum=Colour.RED,
)
# Every optional form left out.
FEWEST = Values(
    boolean=False,
    integer=0,
    number=0.0,
    string="",
    bytes=b"",
    decimal=Decimal("0"),
    timestamp=datetime(2027, 1, 1, tzinfo=UTC),
    date=date(2027, 1, 1),
    duration=timedelta(0),
    list=[],
    map={},
    record=Nested(label=""),
    enum=Colour.RED,
)

# Every case cites these; the test of over_both itself does not.
PARITY = pytest.mark.spec("CNF-1", "HTTP-1")


def _reference() -> neolith.Connector:
    return neolith.connect("reference.v1")


@PARITY
@pytest.mark.spec("TRN-1", "TRN-16", "HTTP-41")
@pytest.mark.parametrize("values", [EVERY, FEWEST], ids=["every", "fewest"])
def test_a_reply_is_the_same_over_both(over_both: OverBoth, values: Values) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            return _reference().echo(values)

    reply = over_both(call).reply
    assert type(reply) is Values
    assert reply == values


@PARITY
@pytest.mark.spec("TRN-1", "TRN-16", "HTTP-41")
def test_an_empty_reply_is_the_same_over_both(over_both: OverBoth) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            return _reference().accept(EVERY)

    assert over_both(call).reply is None


@PARITY
@pytest.mark.spec("TRN-31", "HTTP-42")
def test_the_provider_sees_the_same_context_over_both(over_both: OverBoth) -> None:
    # Over HTTP the calling app is the one the token names, authenticated
    # under the key both sides hold; in-process it is the scope's app.
    def call() -> object:
        with neolith.command_scope(CALLER, timeout=10):
            return _reference().seen(Empty())

    assert over_both(call).reply == Seen(
        principal=None, caller=CALLER, deadline=True, idempotency_key=True
    )


@PARITY
@pytest.mark.spec("TRN-2", "TRN-36", "HTTP-46")
def test_the_principal_reaches_the_provider_alike(over_both: OverBoth) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER, principal=ALICE):
            return _reference().seen(Empty())

    seen = over_both(call).reply
    assert isinstance(seen, Seen)
    assert seen.principal == SeenPrincipal(
        issuer=ALICE.issuer,
        subject=ALICE.subject,
        claims=json.dumps({"level": 3, "roles": ["clerk"]}, sort_keys=True),
    )


@PARITY
@pytest.mark.spec("TRN-43", "HTTP-46")
def test_a_principal_from_an_app_off_the_allow_list_is_refused_alike(
    over_both: OverBoth,
) -> None:
    def call() -> object:
        with neolith.command_scope(STRANGER, principal=ALICE):
            return _reference().seen(Empty())

    outcome = over_both(call)
    assert outcome.kind == "permission_denied"
    assert outcome.error.args == (
        "permission_denied",
        "app reference_stranger may not assert a principal on a call to "
        "reference.v1: the contract's allow-list does not name it",
    )


@PARITY
@pytest.mark.spec("TRN-43")
def test_a_call_with_no_principal_from_an_app_off_the_allow_list_is_served_alike(
    over_both: OverBoth,
) -> None:
    def call() -> object:
        with neolith.command_scope(STRANGER):
            return _reference().seen(Empty())

    seen = over_both(call).reply
    assert isinstance(seen, Seen)
    assert (seen.principal, seen.caller) == (None, STRANGER)


@PARITY
@pytest.mark.spec("HTTP-18", "TRN-42")
def test_a_call_from_no_app_is_refused_alike_and_never_sent(
    over_both: OverBoth,
) -> None:
    account = []

    def call() -> object:
        account.append(uuid.uuid4().hex)
        with _context.root(None, requires=("reference.v1",)):
            return _reference().deposit(Deposit(account=account[-1], amount=1))

    outcome = over_both(call)
    assert outcome.kind == "unauthenticated"
    assert outcome.error.args[1] == (
        "a call to reference.v1 has no calling app; a call is made from a scope "
        "that belongs to an app"
    )
    assert _balances(account) == [0, 0]


@PARITY
@pytest.mark.spec("TRN-4", "TRN-28", "TRN-48")
def test_a_call_whose_deadline_has_passed_is_a_timeout_over_both(
    over_both: OverBoth,
) -> None:
    # A timeout of zero gives a root whose deadline has passed (TRN-48), so
    # the deadline passes first, before anything is sent, in both runs.
    account = []

    def call() -> object:
        account.append(uuid.uuid4().hex)
        with neolith.command_scope(CALLER, timeout=0):
            return _reference().deposit(Deposit(account=account[-1], amount=1))

    assert over_both(call).kind == "timeout"
    assert _balances(account) == [0, 0]


@PARITY
@pytest.mark.spec("TRN-4")
def test_a_call_whose_deadline_passes_at_the_provider_is_a_timeout_over_both(
    over_both: OverBoth,
) -> None:
    # stall returns only once its deadline has passed.
    def call() -> object:
        with neolith.command_scope(CALLER, timeout=0.2):
            return _reference().stall(Empty())

    assert over_both(call).kind == "timeout"


@PARITY
def test_a_call_with_no_deadline_reaches_the_provider_with_none_over_both(
    over_both: OverBoth,
) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            return _reference().stall(Empty())

    outcome = over_both(call)
    assert outcome.error.args == (
        "invalid_argument",
        "stall waits for a deadline; the call has none",
    )


@PARITY
@pytest.mark.spec("TRN-14", "TRN-15")
def test_a_request_the_caller_refuses_is_refused_alike_and_never_sent(
    over_both: OverBoth,
) -> None:
    account = []

    def call() -> object:
        account.append(uuid.uuid4().hex)
        with neolith.command_scope(CALLER):
            return _reference().deposit(
                Deposit.model_construct(account=account[-1], amount="1")
            )

    outcome = over_both(call)
    assert outcome.raised
    assert not isinstance(outcome.error, ContractError)
    assert _balances(account) == [0, 0]


@PARITY
@pytest.mark.spec("TRN-39")
def test_a_call_to_a_version_the_app_does_not_require_is_refused_alike(
    over_both: OverBoth,
) -> None:
    # The provider's own app does not require its contract.
    def call() -> object:
        with neolith.command_scope("reference"):
            return _reference().seen(Empty())

    assert isinstance(over_both(call).error, UsageError)


@PARITY
@pytest.mark.spec("CON-43", "CON-26", "CON-27")
@pytest.mark.parametrize("kind", list(RefusalKind))
def test_an_error_the_action_raises_is_the_same_over_both(
    over_both: OverBoth, kind: RefusalKind
) -> None:
    message = f"refused with {kind.value}: é￿"

    def call() -> object:
        with neolith.command_scope(CALLER):
            return _reference().refuse(Refusal(kind=kind, message=message))

    error = over_both(call).error
    assert isinstance(error, ContractError)
    assert (error.kind, error.message) == (kind.value, message)
    if kind is RefusalKind.GONE:
        assert type(error.data) is Gone
        assert error.data == Gone(reason=message)
    else:
        assert error.data is None


@PARITY
@pytest.mark.spec("TRN-18")
def test_a_reply_of_another_type_is_internal_over_both(over_both: OverBoth) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            return _reference().leak(Empty())

    assert over_both(call).kind == "internal"


@PARITY
@pytest.mark.spec("CON-44", "CON-46")
def test_a_provider_that_fails_is_internal_over_both(over_both: OverBoth) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            return _reference().boom(Empty())

    error = over_both(call).error
    assert isinstance(error, ContractError)
    assert error.kind == "internal"
    assert "the reference provider failed" not in error.message


@PARITY
def test_a_deposit_is_the_same_over_both(over_both: OverBoth) -> None:
    account = []

    def call() -> object:
        account.append(uuid.uuid4().hex)
        with neolith.command_scope(CALLER):
            first = _reference().deposit(Deposit(account=account[-1], amount=5))
            second = _reference().deposit(Deposit(account=account[-1], amount=2))
        return first, second

    assert over_both(call).reply == (Balance(total=5), Balance(total=7))
    assert _balances(account) == [7, 7]


def test_over_both_refuses_what_it_cannot_run(over_both: OverBoth) -> None:
    with pytest.raises(TypeError):
        over_both("not a call")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        over_both(lambda: None, identity=b"reference.v1")  # type: ignore[arg-type]
    with pytest.raises(UsageError, match=r"does not know desk\.v1"):
        over_both(lambda: None, identity="desk.v1")
    assert over_both.runs == 0


def _balances(accounts: list[str]) -> list[int]:
    """Each account's total, read by a deposit of zero in-process."""
    totals = []
    for account in accounts:
        with neolith.command_scope(CALLER):
            balance = _reference().deposit(Deposit(account=account, amount=0))
        assert isinstance(balance, Balance)
        totals.append(balance.total)
    return totals
