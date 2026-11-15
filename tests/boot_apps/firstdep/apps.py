from neolith import NeolithAppConfig


class FirstdepConfig(NeolithAppConfig):
    name = "tests.boot_apps.firstdep"
    label = "firstdep"
    resources = ("database",)
