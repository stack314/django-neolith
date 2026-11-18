from neolith import NeolithAppConfig


class FailingConfig(NeolithAppConfig):
    name = "tests.run_apps.failing"
    label = "failing"
    resources = ("database",)
