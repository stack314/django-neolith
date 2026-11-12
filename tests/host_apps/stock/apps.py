from neolith import NeolithAppConfig, Provided
from tests.declaration_contracts import Couriers, Stock


class StockConfig(NeolithAppConfig):
    name = "tests.host_apps.stock"
    label = "stock"
    provides = (
        Provided(
            Stock,
            implementation="tests.host_apps.stock.services.StockService",
            principal_from=("web",),
        ),
    )
    # It calls its own contract too, which it lists like any other.
    requires = (Couriers, Stock)
