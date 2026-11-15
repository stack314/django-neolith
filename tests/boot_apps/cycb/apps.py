from neolith import NeolithAppConfig


class CycbConfig(NeolithAppConfig):
    name = "tests.boot_apps.cycb"
    label = "cycb"
    resources = ("database",)
