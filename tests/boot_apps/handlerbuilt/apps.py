from neolith import NeolithAppConfig


class HandlerbuiltConfig(NeolithAppConfig):
    name = "tests.boot_apps.handlerbuilt"
    label = "handlerbuilt"
    resources = ("database",)
