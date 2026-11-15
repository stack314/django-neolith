from django.apps import AppConfig


class BootAdminConfig(AppConfig):
    name = "tests.boot_admin"
    label = "boot_admin"

    def ready(self) -> None:
        import tests.boot_project_admin  # noqa: F401
