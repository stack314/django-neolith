from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "tests.app_db.legacy"
    label = "legacy"
    default_auto_field = "django.db.models.AutoField"
