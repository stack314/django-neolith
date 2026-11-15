from neolith import NeolithAppConfig


class EmptyConfig(NeolithAppConfig):
    name = "tests.boot_apps.empty"
    label = "empty"
    resources = ("database",)
