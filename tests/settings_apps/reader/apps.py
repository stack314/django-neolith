import django.conf

from neolith import NeolithAppConfig

# A read of a setting at import, which a declaration must not make.
DEBUG = django.conf.settings.DEBUG


class ReaderConfig(NeolithAppConfig):
    name = "tests.settings_apps.reader"
    label = "reader"
