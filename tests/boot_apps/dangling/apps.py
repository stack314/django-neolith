from neolith import NeolithAppConfig


class DanglingConfig(NeolithAppConfig):
    name = "tests.boot_apps.dangling"
    label = "dangling"
    resources = ("database",)
