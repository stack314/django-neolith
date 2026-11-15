"""Django settings for neolith's own test suite: the test host.

The host builds ``INSTALLED_APPS``, ``DATABASES`` and ``DATABASE_ROUTERS``
from the apps' declarations and ``NEOLITH`` (ADR 0022 Decision 1). The test
host has a real ``default`` with ``contenttypes`` on it, so tests can install
Django's contrib apps beside the hosted ones; it lives in memory, which a
test host may use since the session boots with ``neolith.testing.boot()``.
"""

import copy
import secrets

from neolith.host import build_settings
from tests import host_apps

# Generated per run: no secret is stored in the repository (CONTRIBUTING K9).
SECRET_KEY = secrets.token_urlsafe(50)

DEBUG = False
# The root middleware comes first, once (ADR 0022 Decision 6).
MIDDLEWARE = ["neolith.middleware.RootMiddleware"]
ROOT_URLCONF = "tests.request_urls"
NEOLITH = copy.deepcopy(host_apps.NEOLITH)
USE_TZ = True

globals().update(
    build_settings(
        list(host_apps.HOSTED),
        globals(),
    )
)
