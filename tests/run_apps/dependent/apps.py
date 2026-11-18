from django.apps import AppConfig


class DependentConfig(AppConfig):
    """An app with no declaration, on a real default, whose migration
    depends on contenttypes' and then fails, after Django has applied
    contenttypes' migrations as its dependencies."""

    name = "tests.run_apps.dependent"
    label = "dependent"
