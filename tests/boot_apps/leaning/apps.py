from neolith import NeolithAppConfig


class LeaningConfig(NeolithAppConfig):
    name = "tests.boot_apps.leaning"
    label = "leaning"
    resources = ("database",)
