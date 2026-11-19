"""``over_both``: one contract case, run over every transport, with the
outcomes compared (ADR 0032's Decision 4).

A case is a callable with no arguments that opens its own scope and makes
its own calls. ``over_both`` runs it once in-process, with the host's own
connector, then once inside the private seam ``over_http(identity,
origin)`` against the package's live server, and after each run waits
until the server holds no open connection. Each run's outcome is the reply
it returned, with its exact type, or the error it raised: its type, and for
a ``ContractError`` its kind, detail and message. Two outcomes are the same
when their portable forms and the types at every depth are: ``==`` is not the
test, since it ignores data a portable form keeps (a decimal's scale) and
equates what is not the same value (an enum member and its text).
"""

import datetime
import enum
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import HealthCheck, settings
from pydantic import BaseModel

import neolith
from neolith import ContractError
from neolith._http_transport import over_http
from tests.conformance import Live
from tests.host_apps.reference.contract import Balance, Deposit

# The settings of a Hypothesis case run inside ``over_both``: those of
# ``tests/test_contract_round_trip.py``, so CI sees the same examples every
# time (CONTRIBUTING T3), with the examples counted so the part fits the CI
# budget (K10): each example is two calls, one over a local socket.
# ``over_both`` is function-scoped but holds no state from one example to the
# next, so the health check against such a fixture is suppressed, as
# Hypothesis documents for a fixture like it.
HYPOTHESIS: Final = settings(
    derandomize=True,
    database=None,
    max_examples=40,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
    ],
)
# The transports a case runs over, in order; a later transport adds its
# entry here and its seam in ``OverBoth``.
TRANSPORTS: Final = ("in-process", "http")
# Kinds whose message names a trace or what the transport saw, so it may
# differ between transports: an in-process provider never yields
# unavailable or timeout (ADR 0008 Decision 1), and internal's message
# carries the trace identifier (Decision 3).
_OWN_MESSAGE: Final = frozenset({"internal", "unavailable", "timeout"})


@dataclass(frozen=True)
class Outcome:
    """What one run of a case gave its caller: a reply, or an error."""

    raised: bool
    value: object

    @property
    def reply(self) -> object:
        """The reply; a run that raised has none."""
        assert not self.raised, f"the case raised: {self}"
        return self.value

    @property
    def error(self) -> Exception:
        """The error; a run that replied has none."""
        assert self.raised, f"the case replied: {self}"
        assert isinstance(self.value, Exception)
        return self.value

    @property
    def kind(self) -> str | None:
        """The kind of a ``ContractError``, else ``None``."""
        return self.value.kind if isinstance(self.value, ContractError) else None

    def __str__(self) -> str:
        if not self.raised:
            return f"replied {type(self.value).__name__} {self.value!r}"
        if isinstance(self.value, ContractError):
            return (
                f"raised ContractError kind={self.value.kind!r} "
                f"message={self.value.message!r} detail={self.value.data!r}"
            )
        return f"raised {type(self.value).__name__} {str(self.value)!r}"


def outcome_of(call: Callable[[], object]) -> Outcome:
    """Run ``call`` once; a signal to stop is never an outcome."""
    try:
        return Outcome(raised=False, value=call())
    except Exception as error:
        return Outcome(raised=True, value=error)


def form(value: object) -> object:
    """``value`` as its portable form, with the exact type at every depth.

    Two values have one ``form`` only when each depth has the same type and
    the same data on the wire (spec 01): a decimal's text with its trailing
    zeros, a timestamp's instant (its offset is not data), a number's exact
    value, a zero without its sign, a record's fields in turn, a map without
    regard to order. A value of a type this does not list is judged by ``==``
    beside its type.
    """
    match value:
        case BaseModel():
            return type(value), tuple((name, form(held)) for name, held in value)
        case enum.Enum():
            return type(value), form(value.value)
        case Decimal():
            # Written without an exponent or, for a zero, a sign (CON-37).
            return type(value), format(abs(value) if value.is_zero() else value, "f")
        case float():
            # ``-0`` and ``0`` are one value (spec 01, CON-35).
            return type(value), repr(value + 0.0)
        case datetime.datetime() if value.tzinfo is not None:
            return type(value), value.astimezone(datetime.UTC)
        case list() | tuple():
            return type(value), tuple(form(item) for item in value)
        case Mapping():
            return type(value), {form(key): form(item) for key, item in value.items()}
        case _:
            return type(value), value


def same(first: Outcome, second: Outcome) -> bool:
    """Whether two outcomes are the same: replies of one type and one portable
    form, or errors of one type, kind and detail form whose messages are
    equal, apart from the kinds whose message is the transport's own.
    """
    if first.raised != second.raised or type(first.value) is not type(second.value):
        return False
    if not first.raised:
        return bool(form(first.value) == form(second.value))
    one, other = first.value, second.value
    if isinstance(one, ContractError) and isinstance(other, ContractError):
        if one.kind != other.kind or form(one.data) != form(other.data):
            return False
        return one.kind in _OWN_MESSAGE or one.message == other.message
    return str(one) == str(other)


def balance_of(account: str) -> int:
    """The total of ``account`` on the reference provider, read in-process by a
    deposit of zero, so a case can say a call was never sent: its account
    still holds nothing.
    """
    with neolith.command_scope("reference_caller"):
        total = neolith.connect("reference.v1").deposit(
            Deposit(account=account, amount=0)
        )
    assert isinstance(total, Balance)
    return total.total


class OverBoth:
    """Runs a case over every transport and compares the outcomes; the
    ``over_both`` fixture is one, bound to the package's live server.
    """

    def __init__(self, live: Live) -> None:
        self._live = live
        self.runs = 0
        self._diverged: dict[str, str] | None = None

    def __call__(
        self, call: Callable[[], object], *, identity: str = "reference.v1"
    ) -> Outcome:
        """Run ``call`` in-process, then over HTTP for ``identity``; fail
        with both outcomes, each labelled with its transport, when they
        differ, and return the outcome when they agree.

        Raises:
            TypeError: ``call`` is not callable, or ``identity`` is not a
                ``str``.
            UsageError: ``over_http`` refuses ``identity``.
        """
        given: object = call
        if not callable(given) or type(identity) is not str:
            raise TypeError(
                "over_both takes a callable with no arguments, and identity as a str"
            )
        outcomes: dict[str, Outcome] = {}
        outcomes["in-process"] = outcome_of(call)
        self._live.settle()
        with over_http(identity, self._live.origin):
            outcomes["http"] = outcome_of(call)
        self._live.settle()
        self.runs += 1
        first = outcomes[TRANSPORTS[0]]
        if all(same(first, outcomes[t]) for t in TRANSPORTS[1:]):
            return first
        if self._diverged is None:
            self._diverged = {t: str(outcomes[t]) for t in TRANSPORTS}
        pytest.fail(
            "the transports gave different outcomes:\n"
            + "\n".join(f"  {t}: {outcomes[t]}" for t in TRANSPORTS),
            pytrace=False,
        )

    def transport_outcomes(self) -> dict[str, str]:
        """For each transport, ``"same"`` when every run agreed, or its
        outcome in the first run where the transports differed.
        """
        if self._diverged is None:
            return dict.fromkeys(TRANSPORTS, "same")
        return dict(self._diverged)


@pytest.fixture
def over_both(live: Live, request: pytest.FixtureRequest) -> Iterator[OverBoth]:
    """Run a case over every transport; record each leg's outcome on the
    test as ``transport_outcomes``.
    """
    both = OverBoth(live)
    yield both
    if both.runs:
        request.node.user_properties.append(
            ("transport_outcomes", both.transport_outcomes())
        )
