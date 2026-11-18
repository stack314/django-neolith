"""The reference provider (ADR 0032 Decision 3).

A pure function of its request, apart from ``deposit``'s totals, which are
keyed by account and kept in memory under a lock. A case keeps itself
repeatable by drawing a fresh account per run.
"""

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from neolith import CallContext, ContractError
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

# What ``stall`` returns once its deadline has passed; no caller receives it.
STALLED = Values(
    boolean=True,
    integer=1,
    number=1.5,
    string="stalled",
    bytes=b"\x00",
    decimal=Decimal("1.50"),
    timestamp=datetime(2027, 1, 1, tzinfo=UTC),
    date=date(2027, 1, 1),
    duration=timedelta(seconds=1),
    list=[1],
    map={"a": 1},
    record=Nested(label="stalled"),
    enum=Colour.RED,
)

_lock = threading.Lock()
# Each account's total, and the total each (account, key) pair produced.
_totals: dict[str, int] = {}
_applied: dict[tuple[str, str], int] = {}


class ReferenceService:
    def echo(self, request: Values) -> Values:
        return request

    def accept(self, request: Values) -> None:
        return None

    def refuse(self, request: Refusal) -> Values:
        if request.kind is RefusalKind.GONE:
            raise ContractError("gone", request.message, Gone(reason=request.message))
        raise ContractError(request.kind.value, request.message)

    def seen(self, request: Empty, ctx: CallContext) -> Seen:
        principal = ctx.principal
        return Seen(
            principal=None
            if principal is None
            else SeenPrincipal(
                issuer=principal.issuer,
                subject=principal.subject,
                claims=json.dumps(
                    dict(principal.claims), sort_keys=True, ensure_ascii=False
                ),
            ),
            caller=ctx.caller,
            deadline=ctx.deadline is not None,
            idempotency_key=ctx.idempotency_key is not None,
        )

    def deposit(self, request: Deposit, ctx: CallContext) -> Balance:
        key = ctx.idempotency_key
        with _lock:
            if key is not None and (request.account, key) in _applied:
                return Balance(total=_applied[request.account, key])
            total = _totals.get(request.account, 0) + request.amount
            _totals[request.account] = total
            if key is not None:
                _applied[request.account, key] = total
        return Balance(total=total)

    def leak(self, request: Empty) -> Values:
        return Empty()  # type: ignore[return-value]

    def boom(self, request: Empty) -> Values:
        raise RuntimeError("the reference provider failed")

    def stall(self, request: Empty, ctx: CallContext) -> Values:
        deadline = ctx.deadline
        if deadline is None:
            raise ContractError(
                "invalid_argument", "stall waits for a deadline; the call has none"
            )
        while (left := deadline - time.monotonic()) >= 0:
            time.sleep(left + 0.001)
        return STALLED
