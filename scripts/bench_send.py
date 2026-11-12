"""Measure what an in-process call costs against a direct call (ADR 0007, K10).

ADR 0007 crosses every value as JSON text, in-process too, so each call pays
for the caller's request check, the provider's read of the request, the reply's
send check and the caller's read of the reply. This script times one call
through a connector and the in-process transport against calling the
implementation's method directly, for requests of a few sizes, and prints the
median of several runs.

A call made from a scope that has an idempotency key, as every call an action
makes has, also derives its own key from the request's canonical form (ADR
0017 Decision 3). The last column times a call from such a scope; the one
before it, a call from a root with no key.

Not run in CI. Run it from the repository root, with the package importable:

    python scripts/bench_send.py [--runs N]

The numbers depend on the machine and the Pydantic version, so a pull request
that quotes them names both.
"""

import argparse
import functools
import platform
import statistics
import sys
import time
from collections.abc import Callable
from typing import Any, Protocol

import pydantic
from pydantic import BaseModel

from neolith import Contract, _context, action, contract
from neolith._connector import Connector, InProcess
from neolith._crossing import check_request


class Line(BaseModel):
    sku: str
    quantity: int
    note: str


class Order(BaseModel):
    order: int
    lines: list[Line]


class Placed(BaseModel):
    order: int
    count: int


@contract("bench.v1")
class Orders(Protocol):
    @action
    def place(self, request: Order) -> Placed: ...


class OrdersImpl:
    def place(self, request: Order) -> Placed:
        return Placed(order=request.order, count=len(request.lines))


def _order(lines: int) -> Order:
    return Order(
        order=1,
        lines=[
            Line(sku=f"SKU-{i:08d}", quantity=i % 7, note="x" * 40)
            for i in range(lines)
        ],
    )


def _median_ms(call: Callable[[], object], runs: int) -> float:
    call()  # warm up
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        call()
        times.append(time.perf_counter() - start)
    return statistics.median(times) * 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=11, help="runs per size")
    args = parser.parse_args(argv)
    implementation = OrdersImpl()
    connector: Any = Connector(Orders, InProcess(Orders, implementation))

    place = Contract.of(Orders).actions["place"]
    print(
        f"Python {platform.python_version()}, Pydantic {pydantic.VERSION}, "
        f"{platform.machine()}, median of {args.runs} runs"
    )
    print(
        f"{'request':>10} {'direct':>10} {'check':>10} {'connector':>10} {'keyed':>10}"
    )
    for lines in (10, 1_000, 15_000):
        request = _order(lines)
        size = len(check_request(Contract.of(Orders), place, request))
        direct = _median_ms(functools.partial(implementation.place, request), args.runs)
        checked = _median_ms(
            functools.partial(check_request, Contract.of(Orders), place, request),
            args.runs,
        )
        with _context.root("bench"):
            through = _median_ms(functools.partial(connector.place, request), args.runs)
        with _context.opened(
            principal=None,
            trace_id="1" * 32,
            tracestate="",
            deadline=None,
            idempotency_key="bench",
            caller=None,
            app="bench",
        ):
            keyed = _median_ms(functools.partial(connector.place, request), args.runs)
        print(
            f"{size / 1024:>8.0f}KB {direct:>8.3f}ms {checked:>8.3f}ms "
            f"{through:>8.3f}ms {keyed:>8.3f}ms"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
