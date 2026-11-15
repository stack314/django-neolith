"""The test host: three declared apps that one process hosts (ADR 0022 Decision 5).

A process hosts one set of apps, so every test that sets the host up in
Django's global registry installs exactly these three apps and ``neolith``,
in the order ``tests/settings.py`` builds, with ``contenttypes`` on the
real ``default``.
"""

HOSTED = [
    "tests.host_apps.stock",
    "tests.host_apps.couriers",
    "tests.host_apps.web",
]
INSTALLED = [*HOSTED, "django.contrib.contenttypes", "neolith"]
# The test host's NEOLITH: a real default, in memory, with contenttypes on it.
NEOLITH = {
    "DATABASES": {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    },
    "UNDECLARED_APPS": ["django.contrib.contenttypes"],
}
