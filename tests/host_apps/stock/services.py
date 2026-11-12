"""The stock app's implementation: each level is a courier's quote for it."""

from neolith import connect
from tests.declaration_contracts import Level, Sku

couriers = connect("couriers.v3")


class StockService:
    def level(self, request: Sku) -> Level:
        quoted = couriers.quote(request)
        assert isinstance(quoted, Level)
        return Level(on_hand=quoted.on_hand)
