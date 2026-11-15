from neolith import NeolithAppConfig


class ImportbrokenConfig(NeolithAppConfig):
    name = "tests.boot_apps.importbroken"
    label = "importbroken"
    resources = ("database",)
