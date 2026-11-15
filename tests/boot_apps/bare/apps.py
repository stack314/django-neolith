from neolith import NeolithAppConfig


class BareConfig(NeolithAppConfig):
    name = "tests.boot_apps.bare"
    label = "bare"
    resources = ("database",)
