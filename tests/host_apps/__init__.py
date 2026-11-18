"""The test host: three declared apps that one process hosts (ADR 0022 Decision 5).

A process hosts one set of apps, so every test that sets the host up in
Django's global registry installs exactly these three apps and ``neolith``,
in the order ``tests/settings.py`` builds, with Django's ``contenttypes``,
``auth`` and ``sessions`` on the real ``default``.
"""

HOSTED = [
    "tests.host_apps.stock",
    "tests.host_apps.couriers",
    "tests.host_apps.web",
]
UNDECLARED = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
]
INSTALLED = [*HOSTED, *UNDECLARED, "neolith"]
# The issuer of the test host's Django users (ADR 0023 Decision 8).
ISSUER = "https://users.test.example"
# A host of these apps: a real default, in memory, with Django's
# contenttypes, auth and sessions on it, and no principal backend.
NEOLITH = {
    "DATABASES": {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    },
    "UNDECLARED_APPS": UNDECLARED,
}
# The test host's NEOLITH (tests/settings.py) without its keys: the Django-user
# backend alone. tests/settings.py adds the caller backend and signing keys.
SERVED = {
    **NEOLITH,
    "PRINCIPAL_BACKENDS": [
        {
            "BACKEND": "neolith.principals.DjangoUserBackend",
            "OPTIONS": {"issuer": ISSUER},
        }
    ],
}
# The test host's MIDDLEWARE: the root middleware first, and the principal
# middleware after the two the Django-user backend reads (ADR 0023).
MIDDLEWARE = [
    "neolith.middleware.RootMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "neolith.middleware.PrincipalMiddleware",
]
