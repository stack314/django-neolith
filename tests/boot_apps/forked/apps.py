from neolith import NeolithAppConfig


class ForkedConfig(NeolithAppConfig):
    name = "tests.boot_apps.forked"
    label = "forked"
    resources = ("database",)
