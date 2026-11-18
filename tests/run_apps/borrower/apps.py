from django.apps import AppConfig


class BorrowerConfig(AppConfig):
    """An app with no declaration, on a real default, whose migration
    depends on a hosted app's: its records would live in another database."""

    name = "tests.run_apps.borrower"
    label = "borrower"
