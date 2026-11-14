from neolith import NeolithAppConfig


class LedgerConfig(NeolithAppConfig):
    name = "tests.app_db.ledger"
    label = "ledger"
    resources = ("database",)
