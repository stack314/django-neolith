from neolith import NeolithAppConfig


class NodbConfig(NeolithAppConfig):
    """An app with no database whose migrations conflict: Django's migrate
    judges conflicts over the whole graph."""

    name = "tests.run_apps.nodb"
    label = "nodb"
