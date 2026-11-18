from neolith import NeolithAppConfig


class LeanerConfig(NeolithAppConfig):
    """A declared app with no database whose migration depends on a hosted
    app's: Django would record that app's migration where this one runs."""

    name = "tests.run_apps.leaner"
    label = "leaner"
