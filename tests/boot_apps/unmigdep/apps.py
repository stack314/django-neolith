from neolith import NeolithAppConfig


class UnmigdepConfig(NeolithAppConfig):
    name = "tests.boot_apps.unmigdep"
    label = "unmigdep"
    resources = ("database",)
