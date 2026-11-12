from neolith import NeolithAppConfig
from tests.declaration_contracts import Couriers, Stock


class WebConfig(NeolithAppConfig):
    name = "tests.host_apps.web"
    label = "web"
    requires = (Stock, Couriers)
