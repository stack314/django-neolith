"""Wire part: an idempotent action called again under one key has one effect
(CON-5).

The suite's own caller writes ``Idempotency-Key`` itself, which is the one
way a case can repeat a key: a call opened from an explicit root has no key
of its own, so each of its calls gets a distinct one on either transport
(ADR 0032, fact 13). The case is HTTP only for that reason (T2), and is
listed in ``spec/conformance/cases/wire-provider.json``. It draws a fresh
account, so a second run never sees the first.
"""

import json
import uuid

import pytest

from tests.conformance import Live
from tests.conformance.wire import Answer, Caller, read_json


def _total(answer: Answer) -> int:
    assert answer.status == 200, answer
    assert answer.field("Content-Type") == "application/json"
    reply = read_json(answer.body)
    assert set(reply) == {"total"}, answer
    total = reply["total"]
    assert type(total) is int, answer
    return total


@pytest.mark.conformance_case("wire_deposit_once_per_key")
@pytest.mark.spec("CON-5", "HTTP-23")
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ('"k-1"', '"k-2"'),
        ('"a\\"b"', '"a\\\\b"'),
        # Keys that differ only where a careless reading would join them.
        ('"A"', '"a"'),
        ('"k"', '"k "'),
        ('"' + "k" * 255 + '"', '"' + "k" * 254 + 'K"'),
        ('"' + "k" * 253 + '\\"\\\\"', '"' + "k" * 253 + '\\\\\\""'),
    ],
    ids=["plain", "escaped", "case", "trailing-space", "longest", "longest-escaped"],
)
def test_a_deposit_under_one_key_takes_effect_once(
    live: Live, first: str, second: str
) -> None:
    caller = Caller(live.origin, live.settle)
    body = json.dumps({"account": uuid.uuid4().hex, "amount": 5}).encode()

    def deposit(key: str) -> int:
        return _total(caller.call("deposit", body, fields=[("Idempotency-Key", key)]))

    assert deposit(first) == 5
    # Again under the same key: the same total, and no second effect.
    assert deposit(first) == 5
    # Under another key: a second effect.
    assert deposit(second) == 10
    # Each key keeps its own outcome, and neither adds again.
    assert (deposit(first), deposit(second)) == (5, 10)


@pytest.mark.conformance_case("wire_deposit_once_per_key")
@pytest.mark.spec("CON-5", "HTTP-23")
def test_a_deposit_with_no_key_takes_effect_each_time(live: Live) -> None:
    # The nearest legitimate shape: with no key, nothing ties two calls.
    caller = Caller(live.origin, live.settle)
    body = json.dumps({"account": uuid.uuid4().hex, "amount": 5}).encode()
    totals = [_total(caller.call("deposit", body)) for _ in range(2)]
    assert totals == [5, 10]
