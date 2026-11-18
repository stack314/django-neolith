from neolith import NeolithAppConfig, Provided
from tests.host_apps.reference.contract import Reference


class ReferenceConfig(NeolithAppConfig):
    name = "tests.host_apps.reference"
    label = "reference"
    provides = (
        Provided(
            Reference,
            implementation="tests.host_apps.reference.services.ReferenceService",
            principal_from=("reference_caller",),
        ),
    )
