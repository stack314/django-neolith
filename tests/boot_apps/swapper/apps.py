from neolith import NeolithAppConfig


class SwapperConfig(NeolithAppConfig):
    name = "tests.boot_apps.swapper"
    label = "swapper"
    resources = ("database",)
