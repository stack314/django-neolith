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
# The root middleware first, once (ADR 0022 Decision 6), and the principal
# middleware after the two the Django-user backend reads (ADR 0023).
MIDDLEWARE = list(host_apps.MIDDLEWARE)
ROOT_URLCONF = "tests.request_urls"
NEOLITH = copy.deepcopy(host_apps.SERVED)
# Each hosted app signs its calls over HTTP with a key generated per run, and
# the shared-secret backend accepts each app under that same key (ADR 0027),
# so a call this host sends to itself over HTTP is signed and accepted under
# one key. The keys follow host_apps.HOSTED: an app added there signs too.
# Each hosted app's label is the last part of its name, Django's default.
_keys = {
    name.rpartition(".")[2]: secrets.token_urlsafe(32) for name in host_apps.HOSTED
}
NEOLITH["CALLER_BACKENDS"] = [
    {
        "BACKEND": "neolith.callers.SharedSecretBackend",
        "OPTIONS": {
            "secrets": {app: {f"{app}-1": secret} for app, secret in _keys.items()}
        },
    }
]
NEOLITH["SIGNING_SECRETS"] = {
    app: {"KEY_ID": f"{app}-1", "SECRET": secret} for app, secret in _keys.items()
}
# The conformance suite's live server listens on this address (Django's
# test set-up adds "testserver" for the test client).
ALLOWED_HOSTS = ["127.0.0.1"]
USE_TZ = True

globals().update(
    build_settings(
        list(host_apps.HOSTED),
        globals(),
    )
)
