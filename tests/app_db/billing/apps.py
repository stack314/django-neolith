from neolith import NeolithAppConfig


class BillingConfig(NeolithAppConfig):
    name = "tests.app_db.billing"
    label = "billing"
    resources = ("database",)
