from neolith import NeolithAppConfig
from tests.declaration_contracts import Couriers


class DepotConfig(NeolithAppConfig):
    name = "tests.request_admin.depot"
    label = "depot"
    requires = (Couriers,)
    resources = ("database",)
