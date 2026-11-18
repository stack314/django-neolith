from neolith import NeolithAppConfig
from tests.host_apps.reference.contract import Reference


class ReferenceCallerConfig(NeolithAppConfig):
    """Calls the reference contract, and may assert a principal on it."""

    name = "tests.host_apps.reference_caller"
    label = "reference_caller"
    requires = (Reference,)
