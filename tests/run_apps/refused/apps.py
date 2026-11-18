from neolith import NeolithAppConfig


class RefusedConfig(NeolithAppConfig):
    """A declaration refused when its class is defined: a resource twice."""

    name = "tests.run_apps.refused"
    label = "refused"
    resources = ("database", "database")
