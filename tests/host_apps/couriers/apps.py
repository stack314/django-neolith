from neolith import NeolithAppConfig, Provided
from tests.declaration_contracts import Couriers


class CouriersConfig(NeolithAppConfig):
    name = "tests.host_apps.couriers"
    label = "couriers"
    provides = (
        Provided(
            Couriers,
            implementation="tests.host_apps.couriers.services.CourierService",
            principal_from=(),
        ),
    )
