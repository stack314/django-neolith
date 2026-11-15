from neolith import NeolithAppConfig


class PusherConfig(NeolithAppConfig):
    name = "tests.boot_apps.pusher"
    label = "pusher"
    resources = ("database",)
