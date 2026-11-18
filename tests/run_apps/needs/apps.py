from neolith import NeolithAppConfig


class NeedsConfig(NeolithAppConfig):
    name = "tests.run_apps.needs"
    label = "needs"
    batteries = ("django.contrib.contenttypes",)
