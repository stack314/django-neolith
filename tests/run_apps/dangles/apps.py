from neolith import NeolithAppConfig


class DanglesConfig(NeolithAppConfig):
    """An app with no database whose migration depends on one that does not
    exist: Django's migrate loads the whole graph."""

    name = "tests.run_apps.dangles"
    label = "dangles"
