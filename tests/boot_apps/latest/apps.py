from neolith import NeolithAppConfig


class LatestConfig(NeolithAppConfig):
    name = "tests.boot_apps.latest"
    label = "latest"
    resources = ("database",)
