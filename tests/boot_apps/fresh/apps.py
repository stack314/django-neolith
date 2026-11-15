from neolith import NeolithAppConfig


class FreshConfig(NeolithAppConfig):
    name = "tests.boot_apps.fresh"
    label = "fresh"
    resources = ("database",)
