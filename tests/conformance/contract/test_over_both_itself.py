"""Contract part: ``over_both`` and the live server's wait, tested themselves.

Every parity case agrees over both transports, so these tests drive the
paths the cases never take: two outcomes that differ in each judgment the
outcome rule makes (type, value, kind, detail, message, and the kinds whose
message is the transport's own), a case whose legs differ, and a connection
whose request line the server has not read.
"""

import socket
import time

import pytest
from pydantic import BaseModel

import tests.conformance as conformance
from neolith import ContractError
from tests.conformance import Live
from tests.conformance.contract.conftest import Outcome, OverBoth, same


class Short(BaseModel):
    n: int


class Other(BaseModel):
    n: int


def _replied(value: object) -> Outcome:
    return Outcome(raised=False, value=value)


def _raised(error: Exception) -> Outcome:
    return Outcome(raised=True, value=error)


def _error(kind: str, message: str = "m", data: object = None) -> Outcome:
    return _raised(ContractError(kind, message, data))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_replied(1), _replied(1)),
        (_replied(Short(n=1)), _replied(Short(n=1))),
        (_replied(None), _replied(None)),
        (_raised(ValueError("a")), _raised(ValueError("a"))),
        (_error("not_found", "a"), _error("not_found", "a")),
        (_error("short", "a", {"n": 1}), _error("short", "a", {"n": 1})),
        (_error("short", "a", Short(n=1)), _error("short", "a", Short(n=1))),
        *(
            (_error(kind, "a"), _error(kind, "b"))
            for kind in ("internal", "unavailable", "timeout")
        ),
    ],
)
def test_outcomes_that_agree_are_the_same(first: Outcome, second: Outcome) -> None:
    assert same(first, second)
    assert same(second, first)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(_replied(1), _replied(2), id="value"),
        pytest.param(_replied(1), _replied(1.0), id="reply type, equal value"),
        pytest.param(_replied(1), _replied(True), id="int and bool"),
        pytest.param(_replied(Short(n=1)), _replied(Other(n=1)), id="record type"),
        pytest.param(_replied(None), _raised(ValueError()), id="reply and error"),
        pytest.param(
            _raised(ValueError("a")), _raised(TypeError("a")), id="error type"
        ),
        pytest.param(
            _raised(ValueError("a")), _raised(ValueError("b")), id="error message"
        ),
        pytest.param(_error("not_found"), _error("permission_denied"), id="kind"),
        pytest.param(_error("not_found", "a"), _error("not_found", "b"), id="message"),
        pytest.param(
            _error("short", "a", {"n": 1}), _error("short", "a", {"n": 2}), id="detail"
        ),
        pytest.param(
            _error("short", "a", {"n": 1}), _error("short", "a"), id="no detail"
        ),
        pytest.param(
            _error("short", "a", Short(n=1)),
            _error("short", "a", {"n": 1}),
            id="detail type",
        ),
        pytest.param(
            _error("internal", "a"), _error("not_found", "a"), id="own-message kind"
        ),
    ],
)
def test_outcomes_that_differ_are_not_the_same(first: Outcome, second: Outcome) -> None:
    assert not same(first, second)
    assert not same(second, first)


def test_over_both_fails_with_both_outcomes_when_the_legs_differ(
    over_both: OverBoth,
) -> None:
    legs = iter([1, 2])
    with pytest.raises(
        pytest.fail.Exception, match=r"in-process: replied int 1\n  http: replied int 2"
    ):
        over_both(lambda: next(legs), identity="stock.v1")
    assert over_both.transport_outcomes() == {
        "in-process": "replied int 1",
        "http": "replied int 2",
    }


def test_the_first_run_where_the_legs_differ_is_the_one_recorded(
    over_both: OverBoth,
) -> None:
    legs = iter([1, 2, 3, 4])
    for _ in range(2):
        with pytest.raises(pytest.fail.Exception):
            over_both(lambda: next(legs), identity="stock.v1")
    assert over_both.transport_outcomes()["http"] == "replied int 2"


def test_runs_that_agree_record_same_for_every_transport(over_both: OverBoth) -> None:
    assert over_both(lambda: 1, identity="stock.v1").reply == 1
    assert over_both.runs == 1
    assert over_both.transport_outcomes() == {"in-process": "same", "http": "same"}


def test_settle_waits_for_a_connection_whose_request_line_is_unread(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = int(live.origin.rsplit(":", 1)[1])
    monkeypatch.setattr(conformance, "BOUND", 0.5)
    with socket.create_connection(("127.0.0.1", port), timeout=10):
        until = time.monotonic() + 10
        while live.connections.open_count() == 0 and time.monotonic() < until:
            time.sleep(0.01)
        assert live.connections.in_flight() == []
        with pytest.raises(pytest.fail.Exception, match="still holds a connection"):
            live.settle()
    monkeypatch.setattr(conformance, "BOUND", 10.0)
    live.settle()
    assert live.connections.open_count() == 0
