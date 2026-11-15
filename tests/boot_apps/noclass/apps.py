from neolith import NeolithAppConfig


class NoclassConfig(NeolithAppConfig):
    name = "tests.boot_apps.noclass"
    label = "noclass"
    resources = ("database",)
