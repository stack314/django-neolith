from neolith import NeolithAppConfig


class BringsConfig(NeolithAppConfig):
    """A declared app whose battery has a migration that depends on a hosted
    app's."""

    name = "tests.run_apps.brings"
    label = "brings"
    batteries = ("tests.run_apps.borrower",)
