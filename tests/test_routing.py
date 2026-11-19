"""The connector's walk under ``local-first`` (spec 02, ADR 0031).

A connector built with two or more transports tries them in order, and moves
a call to the next only when an attempt ends with ``unavailable``, the action
is idempotent and at least 1 ms of the budget is left. The transports here
are fakes that record what each attempt was given and answer with a fixed
outcome; time is a clock the tests hold still and move (T3). The share of the
connect phase as the HTTP client applies it is tested in
``test_connect_share.py``.
"""

import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from neolith import Contract, ContractError, Principal, _context, action, contract
from neolith._connector import (
    _LATE,
    _NOT_RUN,
    _NOT_SENT,
    SHARE_FLOOR,
    Connector,
    Failure,
    InProcess,
)
from neolith._context import Sent
from neolith._contracts import Action, is_received
from neolith._crossing import Receiver


class Ask(BaseModel):
    n: int = 0


class Answer(BaseModel):
    n: int


class Busy(BaseModel):
    n: int


@contract("routing.v1")
class Desk(Protocol):
    @action(idempotent=True, errors={"busy": Busy})
    def get(self, request: Ask) -> Answer: ...

    @action
    def put(self, request: Ask) -> Answer: ...


@contract("elsewhere.v1")
class Elsewhere(Protocol):
    @action(idempotent=True)
    def get(self, request: Ask) -> Answer: ...


DESK = Contract.of(Desk)
REQUIRES = ("routing.v1",)
ALICE = Principal("https://id.example.com", "alice", {"roles": ("clerk",)})
TRACESTATE = "vendor=routingsecretstate"


class Clock:
    """This host's clock, held still until a test moves it."""

    def __init__(self) -> None:
        self.at = 1_000_000.0

    def __call__(self) -> float:
        return self.at


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    held = Clock()
    monkeypatch.setattr(_context, "now", held)
    return held


Outcome = Callable[[Action], bytes | None]


def reply(action: Action) -> bytes | None:
    return b'{"n":7}'


def failing(kind: str, message: str = "m", detail: bytes | None = None) -> Outcome:
    def outcome(action: Action) -> bytes | None:
        raise Failure(kind, message, detail)

    outcome.__name__ = kind
    return outcome


UNAVAILABLE = failing("unavailable", "the provider could not be reached")


class Fake:
    """A remote transport that records each attempt and answers ``outcome``.

    ``takes`` moves the clock forward by that many seconds before it answers,
    as a provider that takes time would.
    """

    receiver = Receiver.NETWORK

    def __init__(
        self,
        outcome: Outcome,
        origin: str = "http://a.test",
        *,
        clock: Clock | None = None,
        takes: float = 0.0,
        contract: Contract = DESK,
    ) -> None:
        self.contract = contract
        self.origin = origin
        self.outcome = outcome
        self.clock = clock
        self.takes = takes
        self.attempts: list[tuple[Sent, float | None]] = []

    def call(
        self,
        action: Action,
        request: bytes,
        context: Sent,
        connect_by: float | None = None,
    ) -> bytes | None:
        self.attempts.append((context, connect_by))
        if self.clock is not None:
            self.clock.at += self.takes
        return self.outcome(action)


class Alone:
    """A transport written for ``single``: its ``call`` takes no connect_by."""

    receiver = Receiver.NETWORK

    def __init__(self, outcome: Outcome) -> None:
        self.contract = DESK
        self.outcome = outcome
        self.calls = 0

    def call(self, action: Action, request: bytes, context: Sent) -> bytes | None:
        self.calls += 1
        return self.outcome(action)


def fakes(*outcomes: Outcome, clock: Clock | None = None) -> list[Fake]:
    return [
        Fake(outcome, f"http://{chr(ord('a') + i)}.test", clock=clock)
        for i, outcome in enumerate(outcomes)
    ]


@contextlib.contextmanager
def scope(
    key: str | None = None,
    **given: Any,  # noqa: ANN401
) -> Iterator[_context.Scope]:
    fields = _context.root_fields("shop", requires=REQUIRES, **given)
    fields.update(tracestate=TRACESTATE, idempotency_key=key)
    with _context.opened(**fields) as opened:
        yield opened


def run(
    connector: Any,  # noqa: ANN401
    name: str = "get",
    timeout: float | None = None,
) -> object:
    """The call's reply, or the ``ContractError`` it ended with."""
    with scope(principal=ALICE):
        try:
            return getattr(connector, name)(Ask(n=1), timeout=timeout)
        except ContractError as error:
            return error


def attempts(transports: list[Fake]) -> list[int]:
    return [len(t.attempts) for t in transports]


# Which outcomes move a call (TRN-8, CON-4), from the first entry to the second.

_STAYS = [
    failing("timeout", _NOT_SENT),
    failing("timeout", _LATE),
    failing("unauthenticated"),
    failing("permission_denied"),
    failing("invalid_argument"),
    failing("not_found"),
    failing("conflict"),
    failing("internal"),
    failing("busy", "m", b'{"n":1}'),
    failing("a_later_kind"),
]


@pytest.mark.spec("TRN-8", "TRN-68", "TRN-72")
@pytest.mark.parametrize("first", _STAYS, ids=lambda o: o.__name__)
def test_no_kind_but_unavailable_moves_a_call(first: Outcome) -> None:
    transports = fakes(first, reply)
    outcome = run(Connector(Desk, *transports))
    assert type(outcome) is ContractError
    assert outcome.kind == first.__name__
    assert attempts(transports) == [1, 0]


@pytest.mark.spec("TRN-8", "TRN-68")
def test_an_idempotent_call_moves_on_unavailable() -> None:
    transports = fakes(UNAVAILABLE, reply)
    assert run(Connector(Desk, *transports)) == Answer(n=7)
    assert attempts(transports) == [1, 1]


@pytest.mark.spec("TRN-8", "TRN-72")
@pytest.mark.parametrize(
    "first",
    [
        lambda action: b"not json",
        lambda action: b'{"n":"seven"}',
        failing("internal", "m", b'{"n":1}'),
    ],
    ids=["text", "record", "detail"],
)
def test_an_outcome_that_cannot_be_read_is_internal_and_stays(first: Outcome) -> None:
    transports = fakes(first, reply)
    outcome = run(Connector(Desk, *transports))
    assert type(outcome) is ContractError
    assert outcome.kind == "internal"
    assert attempts(transports) == [1, 0]


@pytest.mark.spec("TRN-8", "TRN-72")
def test_a_transport_that_fails_is_internal_and_stays() -> None:
    def broken(action: Action) -> bytes | None:
        raise RuntimeError("a bug in the transport")

    transports = fakes(broken, reply)
    outcome = run(Connector(Desk, *transports))
    assert type(outcome) is ContractError
    assert outcome.kind == "internal"
    assert attempts(transports) == [1, 0]


@pytest.mark.spec("CON-4", "TRN-8", "TRN-72")
def test_a_non_idempotent_call_stays_and_ends_unavailable() -> None:
    transports = fakes(UNAVAILABLE, reply)
    outcome = run(Connector(Desk, *transports), "put")
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == (
        "unavailable",
        "the provider could not be reached",
    )
    assert is_received(outcome)
    assert attempts(transports) == [1, 0]


@pytest.mark.spec("TRN-8", "TRN-68", "TRN-69", "TRN-72")
@given(
    outcomes=st.lists(
        st.sampled_from(["reply", "unavailable", "timeout", "internal", "busy"]),
        min_size=1,
        max_size=5,
    ),
    name=st.sampled_from(["get", "put"]),
)
def test_the_walk_matches_the_rule_for_every_list(
    outcomes: list[str], name: str
) -> None:
    """For any order of answers, the attempts are a prefix of the order: one
    more after each ``unavailable`` of an idempotent call, while an entry is
    left, and the call ends with the last attempt's outcome."""
    made = {
        "reply": reply,
        "unavailable": UNAVAILABLE,
        "timeout": failing("timeout", _NOT_SENT),
        "internal": failing("internal"),
        "busy": failing("busy", "m", b'{"n":1}'),
    }
    transports = fakes(*(made[o] for o in outcomes))
    outcome = run(Connector(Desk, *transports), name)
    tried = 1
    while (
        name == "get" and outcomes[tried - 1] == "unavailable" and tried < len(outcomes)
    ):
        tried += 1
    assert attempts(transports) == [1] * tried + [0] * (len(outcomes) - tried)
    last = outcomes[tried - 1]
    if last == "reply":
        assert outcome == Answer(n=7)
    else:
        assert type(outcome) is ContractError
        assert outcome.kind == last
        if last == "timeout" and tried > 1:
            assert outcome.message == _LATE


# Each entry once, and the last attempt's outcome (TRN-69, TRN-72)


@pytest.mark.spec("TRN-69", "TRN-72")
def test_each_entry_is_tried_once_and_the_call_ends_with_the_last() -> None:
    transports = fakes(
        failing("unavailable", "first"),
        failing("unavailable", "second"),
        failing("unavailable", "third"),
    )
    outcome = run(Connector(Desk, *transports))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == ("unavailable", "third")
    assert attempts(transports) == [1, 1, 1]


@pytest.mark.spec("TRN-68")
def test_each_call_starts_at_the_first_entry() -> None:
    transports = fakes(UNAVAILABLE, reply)
    connector = Connector(Desk, *transports)
    assert run(connector) == Answer(n=7)
    assert run(connector) == Answer(n=7)
    assert attempts(transports) == [2, 2]


@pytest.mark.spec("TRN-68")
def test_a_reply_from_the_first_entry_ends_the_call() -> None:
    transports = fakes(reply, reply)
    assert run(Connector(Desk, *transports)) == Answer(n=7)
    assert attempts(transports) == [1, 0]


# The same context on every attempt (TRN-71)


@pytest.mark.spec("TRN-71")
def test_every_attempt_carries_the_same_sent_context() -> None:
    transports = fakes(UNAVAILABLE, UNAVAILABLE, reply)
    with scope(principal=ALICE) as opened:
        assert Connector(Desk, *transports).get(Ask(n=1)) == Answer(n=7)
        trace = opened.held.trace_id
    (first, _), (second, _), (third, _) = (t.attempts[0] for t in transports)
    assert first is second is third
    assert first.principal is not None
    assert first.trace_id == trace
    assert first.tracestate == TRACESTATE
    assert first.idempotency_key


@pytest.mark.spec("TRN-71")
def test_a_moved_call_counts_once_in_its_parent() -> None:
    """The key is derived once per call, so the call after a moved one gets
    the key it would get after a call of one attempt."""
    keys: list[list[str | None]] = []
    for transports in (fakes(UNAVAILABLE, reply), fakes(reply, reply)):
        with scope(key="parent-key"):
            connector: Any = Connector(Desk, *transports)
            connector.get(Ask(n=1))
            connector.get(Ask(n=1))
        keys.append(
            [sent.idempotency_key for t in transports for sent, _ in t.attempts]
        )
    moved, straight = keys
    # By transport: the first entry's two calls, then the second's two.
    assert moved == straight + straight
    assert straight[0] != straight[1]


# Under 1 ms left, no next attempt (TRN-70, TRN-72); the deadline (TRN-38)


@pytest.mark.spec("TRN-70", "TRN-72")
def test_under_1_ms_left_ends_with_the_last_unavailable(clock: Clock) -> None:
    first = Fake(UNAVAILABLE, clock=clock, takes=0.9995)
    second = Fake(reply, "http://b.test")
    outcome = run(Connector(Desk, first, second), timeout=1)
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == (
        "unavailable",
        "the provider could not be reached",
    )
    assert len(second.attempts) == 0


@pytest.mark.spec("TRN-70")
def test_exactly_1_ms_left_moves(clock: Clock) -> None:
    first = Fake(UNAVAILABLE, clock=clock, takes=0.999)
    second = Fake(reply, "http://b.test")
    assert run(Connector(Desk, first, second), timeout=1) == Answer(n=7)
    assert len(second.attempts) == 1


@pytest.mark.spec("TRN-38", "TRN-70")
def test_a_deadline_passed_after_an_unavailable_is_a_timeout(clock: Clock) -> None:
    first = Fake(UNAVAILABLE, clock=clock, takes=1)
    second = Fake(reply, "http://b.test")
    outcome = run(Connector(Desk, first, second), timeout=1)
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == ("timeout", _LATE)
    assert len(second.attempts) == 0


@pytest.mark.spec("TRN-8")
def test_a_call_with_no_deadline_moves() -> None:
    transports = fakes(UNAVAILABLE, reply)
    assert run(Connector(Desk, *transports)) == Answer(n=7)
    assert [t.attempts[0][0].deadline for t in transports] == [None, None]


# A timeout after a move says the action might have run (TRN-74)


@pytest.mark.spec("TRN-74")
@pytest.mark.parametrize(
    "message",
    [_NOT_SENT, _NOT_RUN, _LATE, "the provider's own words"],
    ids=["not-sent", "not-run", "late", "other"],
)
def test_a_timeout_after_a_move_says_the_action_might_have_run(message: str) -> None:
    transports = fakes(UNAVAILABLE, failing("timeout", message))
    outcome = run(Connector(Desk, *transports))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == ("timeout", _LATE)
    assert outcome.data is None
    assert is_received(outcome)


@pytest.mark.spec("TRN-74", "TRN-38")
def test_the_connectors_own_check_after_a_move_says_the_action_might_have_run(
    clock: Clock,
) -> None:
    first = Fake(UNAVAILABLE)
    second = Fake(reply, "http://b.test", clock=clock, takes=2)
    outcome = run(Connector(Desk, first, second), timeout=1)
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == ("timeout", _LATE)


@pytest.mark.spec("TRN-38", "TRN-74")
@pytest.mark.parametrize("message", [_NOT_SENT, _NOT_RUN])
def test_a_timeout_before_any_move_is_passed_on_as_it_is(message: str) -> None:
    transports = fakes(failing("timeout", message), reply)
    outcome = run(Connector(Desk, *transports))
    assert type(outcome) is ContractError
    assert (outcome.kind, outcome.message) == ("timeout", message)


@pytest.mark.spec("TRN-74")
def test_a_reply_after_a_move_is_the_reply() -> None:
    transports = fakes(UNAVAILABLE, reply)
    assert run(Connector(Desk, *transports)) == Answer(n=7)


# The share of the connect phase (TRN-73)


@pytest.mark.spec("TRN-73")
@pytest.mark.parametrize(
    ("entries", "timeout", "shares"),
    [
        # 8 s left and two entries: 4 s for the first, none for the last.
        (2, 8.0, [4.0, None]),
        # 30 s and three: 10 s, then 30 s over two, then none.
        (3, 30.0, [10.0, 15.0, None]),
        # 3 s and two: the floor, 2 s, is less than all the time left.
        (2, 3.0, [2.0, None]),
        # 1.5 s and two: the floor is all the time left, so no share.
        (2, 1.5, [None, None]),
        # Exactly 2 s: the share is all of it, and the deadline bounds.
        (2, 2.0, [None, None]),
    ],
)
def test_each_attempt_that_can_move_gets_its_share(
    clock: Clock, entries: int, timeout: float, shares: list[float | None]
) -> None:
    assert SHARE_FLOOR == 2.0
    transports = fakes(*[UNAVAILABLE] * entries, clock=clock)
    started = clock.at
    run(Connector(Desk, *transports), timeout=timeout)
    given = [t.attempts[0][1] for t in transports]
    assert given == [
        None if share is None else pytest.approx(started + share) for share in shares
    ]


@pytest.mark.spec("TRN-73")
def test_the_share_is_of_the_time_left_when_the_attempt_starts(clock: Clock) -> None:
    first = Fake(UNAVAILABLE, clock=clock, takes=6)
    second = Fake(UNAVAILABLE, "http://b.test")
    third = Fake(UNAVAILABLE, "http://c.test")
    started = clock.at
    run(Connector(Desk, first, second, third), timeout=30)
    # 24 s left over two entries.
    assert second.attempts[0][1] == pytest.approx(started + 6 + 12)
    assert first.attempts[0][1] == pytest.approx(started + 10)


@pytest.mark.spec("TRN-73")
@pytest.mark.parametrize(
    ("name", "timeout"),
    [("put", 8.0), ("get", None)],
    ids=["not-idempotent", "no-deadline"],
)
def test_no_share_where_the_call_cannot_move_or_has_no_deadline(
    clock: Clock, name: str, timeout: float | None
) -> None:
    transports = fakes(UNAVAILABLE, UNAVAILABLE, clock=clock)
    run(Connector(Desk, *transports), name, timeout=timeout)
    assert transports[0].attempts[0][1] is None


# single is unchanged (TRN-17)


@pytest.mark.spec("TRN-17")
@pytest.mark.parametrize("name", ["get", "put"])
def test_one_transport_is_one_attempt_and_no_share(
    clock: Clock, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    alone = Alone(UNAVAILABLE)
    with caplog.at_level(logging.INFO, logger="neolith"):
        outcome = run(Connector(Desk, alone), name, timeout=8)
    assert type(outcome) is ContractError
    assert outcome.kind == "unavailable"
    assert alone.calls == 1
    assert caplog.records == []


# The connector's order


def test_a_routed_transport_must_take_connect_by() -> None:
    with pytest.raises(TypeError, match="transport 1 takes no connect_by="):
        Connector(Desk, Fake(reply), Alone(reply))


class Keywords:
    """A remote transport whose ``call`` takes any keyword."""

    receiver = Receiver.NETWORK
    contract = DESK
    origin = "http://k.test"

    def call(
        self,
        action: Action,
        request: bytes,
        context: Sent,
        **options: object,
    ) -> bytes | None:
        return reply(action)


class Positional:
    """A remote transport whose ``connect_by`` cannot be passed by name."""

    receiver = Receiver.NETWORK
    contract = DESK

    def call(
        self,
        action: Action,
        request: bytes,
        context: Sent,
        connect_by: float | None = None,
        /,
    ) -> bytes | None:
        return reply(action)


def test_a_transport_that_takes_any_keyword_is_routed() -> None:
    assert run(Connector(Desk, Fake(UNAVAILABLE), Keywords()), timeout=8) == Answer(n=7)


def test_a_transport_whose_connect_by_is_positional_only_is_refused() -> None:
    with pytest.raises(TypeError, match="transport 1 takes no connect_by="):
        Connector(Desk, Fake(reply), Positional())


def test_a_transport_is_listed_once() -> None:
    fake = Fake(reply)
    with pytest.raises(ValueError, match="transport 1 is listed before"):
        Connector(Desk, fake, fake)


def test_every_transport_carries_the_contract() -> None:
    other = Fake(reply, contract=Contract.of(Elsewhere))
    with pytest.raises(TypeError, match=r"transport 1 carries calls of elsewhere\.v1"):
        Connector(Desk, Fake(reply), other)


def test_every_transport_has_a_receiver() -> None:
    odd = Fake(reply)
    odd.receiver = "network"  # type: ignore[assignment]
    with pytest.raises(TypeError, match="a transport's receiver is a Receiver"):
        Connector(Desk, Fake(reply), odd)


class Implementation:
    def get(self, request: Ask) -> Answer:
        return Answer(n=request.n + 1)

    def put(self, request: Ask) -> Answer:
        return Answer(n=request.n)


def test_the_in_process_transport_takes_connect_by_and_ignores_it() -> None:
    later = Fake(reply, "http://b.test")
    connector = Connector(
        Desk,
        InProcess(Desk, Implementation(), app="desk", principal_from=("shop",)),
        later,
    )
    assert run(connector, timeout=8) == Answer(n=2)
    assert later.attempts == []


def test_each_entry_reads_with_its_own_receiver() -> None:
    """An in-process provider's text is read with the validator alone, and a
    remote one's after the network receive check, in one order."""
    duplicated = Fake(lambda action: b'{"n":1,"n":2}', "http://b.test")
    connector = Connector(Desk, Fake(UNAVAILABLE), duplicated)
    outcome = run(connector)
    assert type(outcome) is ContractError
    assert outcome.kind == "internal"


# The log (ADR 0031 Decision 8)


def _lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [(r.levelno, r.getMessage()) for r in caplog.records]


@pytest.mark.spec("TRN-8")
def test_a_move_is_logged_once_at_info(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    transports = fakes(UNAVAILABLE, reply)
    with (
        caplog.at_level(logging.DEBUG, logger="neolith"),
        scope(principal=ALICE) as opened,
    ):
        Connector(Desk, *transports).get(Ask(n=1), timeout=8)
        trace = opened.held.trace_id
    assert _lines(caplog) == [
        (
            logging.INFO,
            f"routing.v1, action 'get': provider 1 of 2 at http://a.test was "
            "unavailable, and get is idempotent; trying provider 2 at "
            f"http://b.test with 8000 ms left (trace {trace})",
        )
    ]


@pytest.mark.spec("TRN-8")
def test_a_move_with_no_deadline_says_so(caplog: pytest.LogCaptureFixture) -> None:
    transports = fakes(UNAVAILABLE, reply)
    with caplog.at_level(logging.INFO, logger="neolith"):
        run(Connector(Desk, *transports))
    ((_, line),) = _lines(caplog)
    assert "trying provider 2 at http://b.test with no deadline (trace " in line


@pytest.mark.spec("CON-4")
def test_a_call_not_moved_for_its_action_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transports = fakes(UNAVAILABLE, reply)
    with caplog.at_level(logging.INFO, logger="neolith"):
        run(Connector(Desk, *transports), "put")
    ((level, line),) = _lines(caplog)
    assert level == logging.INFO
    assert line.startswith(
        "routing.v1, action 'put': provider 1 of 2 at http://a.test was "
        "unavailable, and put is not idempotent, so the call is not moved to "
        "provider 2 (trace "
    )


@pytest.mark.spec("TRN-70")
def test_a_call_not_moved_for_its_deadline_is_logged(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    first = Fake(UNAVAILABLE, clock=clock, takes=0.9995)
    with caplog.at_level(logging.INFO, logger="neolith"):
        run(Connector(Desk, first, Fake(reply, "http://b.test")), timeout=1)
    ((level, line),) = _lines(caplog)
    assert level == logging.INFO
    assert line.startswith(
        "routing.v1, action 'get': provider 1 of 2 at http://a.test was "
        "unavailable, and the call's deadline left under 1 ms to try provider 2 "
        "at http://b.test (trace "
    )


@pytest.mark.spec("TRN-69")
def test_nothing_is_logged_when_the_first_answers_or_the_last_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="neolith"):
        run(Connector(Desk, *fakes(reply, reply)))
        run(Connector(Desk, *fakes(UNAVAILABLE, UNAVAILABLE)))
    assert [line for _, line in _lines(caplog) if "trying" not in line] == []
    assert len(_lines(caplog)) == 1


def test_an_in_process_provider_is_named_in_this_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Unavailable:
        """An in-process transport stand-in that ends with unavailable."""

        receiver = Receiver.IN_PROCESS
        contract = DESK

        def call(
            self,
            action: Action,
            request: bytes,
            context: Sent,
            connect_by: float | None = None,
        ) -> bytes | None:
            raise Failure("unavailable", "m")

    with caplog.at_level(logging.INFO, logger="neolith"):
        run(Connector(Desk, Unavailable(), Fake(reply, "http://b.test")))
    ((_, line),) = _lines(caplog)
    assert "provider 1 of 2 in this host was unavailable" in line


@pytest.mark.spec("TRN-71")
def test_no_secret_reaches_the_routing_log(caplog: pytest.LogCaptureFixture) -> None:
    transports = fakes(UNAVAILABLE, failing("unavailable", "secret message"))
    with caplog.at_level(logging.DEBUG, logger="neolith"):
        run(Connector(Desk, *transports), timeout=8)
        run(Connector(Desk, *transports), "put")
    sent = transports[0].attempts[0][0]
    text = caplog.text
    assert sent.idempotency_key
    assert sent.idempotency_key not in text
    assert sent.principal is not None
    assert sent.principal.decode() not in text
    assert _context.b64url(sent.principal) not in text
    assert TRACESTATE not in text
    assert "routingsecretstate" not in text
    assert "alice" not in text
    assert "secret message" not in text
    assert "Bearer" not in text
