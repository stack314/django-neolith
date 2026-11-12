"""The test host: two declared apps that one process hosts (ADR 0022 Decision 5).

A process hosts one set of apps, so every test that sets the host up in
Django's global registry installs exactly these two apps and ``neolith``.
"""

INSTALLED = ["tests.host_apps.stock", "tests.host_apps.couriers", "neolith"]
