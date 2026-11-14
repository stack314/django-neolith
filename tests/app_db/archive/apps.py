from neolith import NeolithAppConfig


class ArchiveConfig(NeolithAppConfig):
    name = "tests.app_db.archive"
    label = "archive"
    resources = ("database",)
