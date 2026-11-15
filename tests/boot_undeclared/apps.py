from django.apps import AppConfig


class BootUndeclaredConfig(AppConfig):
    """An app with no declaration whose migrations conflict."""

    name = "tests.boot_undeclared"
    label = "boot_undeclared"
