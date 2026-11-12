"""Contract packages for the declaration tests: interfaces and types only.

An ``apps`` module imports its contracts from here, as an app imports them
from a contract package. Nothing here reads a Django setting.
"""

from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Sku(BaseModel):
    sku: str


class Level(BaseModel):
    on_hand: int


@contract("stock.v1")
class Stock(Protocol):
    @action(idempotent=True)
    def level(self, request: Sku) -> Level: ...


@contract("stock.v1")
class StockTwin(Protocol):
    """A second class with the identity of ``Stock``."""

    @action(idempotent=True)
    def level(self, request: Sku) -> Level: ...


@contract("stock.v2")
class StockV2(Protocol):
    @action(idempotent=True)
    def level(self, request: Sku) -> Level: ...


@contract("billing.invoices.v1")
class Invoices(Protocol):
    @action
    def void(self, request: Sku) -> None: ...


@contract("couriers.v3")
class Couriers(Protocol):
    @action(idempotent=True)
    def quote(self, request: Sku) -> Level: ...


class Unmarked(Protocol):
    """A protocol class that carries no ``@contract`` mark."""

    def level(self, request: Sku) -> Level: ...
