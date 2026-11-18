from neolith import NeolithAppConfig
from tests.host_apps.reference.contract import Reference


class ReferenceStrangerConfig(NeolithAppConfig):
    """Calls the reference contract, and is not on its allow-list."""

    name = "tests.host_apps.reference_stranger"
    label = "reference_stranger"
    requires = (Reference,)
