from neolith import NeolithAppConfig


class SquashedConfig(NeolithAppConfig):
    name = "tests.boot_apps.squashed"
    label = "squashed"
    resources = ("database",)
