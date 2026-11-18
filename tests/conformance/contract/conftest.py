"""``over_both``: one contract case, run over every transport, with the
outcomes compared (ADR 0032's Decision 4).

A case is a callable with no arguments that opens its own scope and makes
its own calls. ``over_both`` runs it once in-process, with the host's own
connector, then once inside the private seam ``over_http(identity,
origin)`` against the package's live server, and after each run waits
until the server holds no open connection. Each run's outcome is the reply
it returned, with its exact type, or the error it raised: its type, and for
a ``ContractError`` its kind, detail and message.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

import pytest

from neolith import ContractError
from neolith._http_transport import over_http
from tests.conformance import Live

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


def same(first: Outcome, second: Outcome) -> bool:
    """Whether two outcomes are the same: replies of one type that compare
    equal, or errors of one type, kind and detail whose messages are equal,
    apart from the kinds whose message is the transport's own.
    """
    if first.raised != second.raised or type(first.value) is not type(second.value):
        return False
    if not first.raised:
        return bool(first.value == second.value)
    one, other = first.value, second.value
    if isinstance(one, ContractError) and isinstance(other, ContractError):
        if one.kind != other.kind or type(one.data) is not type(other.data):
            return False
        if one.data != other.data:
            return False
        return one.kind in _OWN_MESSAGE or one.message == other.message
    return str(one) == str(other)


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
