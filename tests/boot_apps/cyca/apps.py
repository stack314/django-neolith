from neolith import NeolithAppConfig


class CycaConfig(NeolithAppConfig):
    name = "tests.boot_apps.cyca"
    label = "cyca"
    resources = ("database",)
