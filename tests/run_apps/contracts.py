"""The contract ``desk`` provides and calls: interfaces and types only."""

from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Tally(BaseModel):
    notes: int


@contract("desk.v1")
class Desk(Protocol):
    @action(idempotent=True)
    def count(self, request: Tally) -> Tally: ...
