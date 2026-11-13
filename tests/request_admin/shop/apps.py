from neolith import NeolithAppConfig
from tests.declaration_contracts import Couriers


class ShopConfig(NeolithAppConfig):
    name = "tests.request_admin.shop"
    label = "shop"
    requires = (Couriers,)
    resources = ("database",)
