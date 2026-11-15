import django.conf
from django.core.exceptions import ImproperlyConfigured

from neolith import NeolithAppConfig

# A read of a setting at import, inside the app's own try, with a fallback.
# No module-level name holds Django's lazy settings, which Django's own
# AppConfig.create would evaluate when it looks for the config class.
try:
    DEBUG = django.conf.settings.DEBUG
except ImproperlyConfigured:
    DEBUG = False


class GuardedConfig(NeolithAppConfig):
    name = "tests.settings_apps.guarded"
    label = "guarded"
