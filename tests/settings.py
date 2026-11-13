"""Django settings for neolith's own test suite."""

import secrets

# Generated per run: no secret is stored in the repository (CONTRIBUTING K9).
SECRET_KEY = secrets.token_urlsafe(50)

DEBUG = False
INSTALLED_APPS: list[str] = []
# The root middleware comes first, once (ADR 0022 Decision 6).
MIDDLEWARE = ["neolith.middleware.RootMiddleware"]
NEOLITH: dict[str, object] = {}
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
