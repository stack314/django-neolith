from neolith import NeolithAppConfig, Provided
from tests.run_apps.contracts import Desk


class DeskConfig(NeolithAppConfig):
    name = "tests.run_apps.desk"
    label = "desk"
    provides = (
        Provided(Desk, implementation="tests.run_apps.desk.services.DeskService"),
    )
    requires = (Desk,)
    resources = ("database",)
