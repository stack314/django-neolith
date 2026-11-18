from neolith import NeolithAppConfig


class HalfConfig(NeolithAppConfig):
    """An app whose first migration applies and whose second fails."""

    name = "tests.run_apps.half"
    label = "half"
    resources = ("database",)
