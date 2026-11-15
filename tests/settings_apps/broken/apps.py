from django.conf import settings

from neolith import NeolithAppConfig

DEBUG = settings.DEBUG


class BrokenConfig(NeolithAppConfig):
    name = "tests.settings_apps.broken"
    label = "broken"
    # A list where a tuple is admitted: refused when the class is defined.
    resources = ["database"]  # type: ignore[assignment]  # noqa: RUF012
