"""``checkapps``: the check before deployment (ADR 0030 Decision 7)."""

import sys
from typing import Any

from django.core.management.base import BaseCommand

from neolith import _check


class Command(BaseCommand):
    """Report what the hosted apps' code and models do across an app
    boundary, with no database.
    """

    help = (
        "Report what the hosted apps' code and models do across an app "
        "boundary, after the host's checks that read no database; open no "
        "database connection. Exit 1 when the host refuses, an enforced app has "
        "an error finding or an app listed as report-only has none, and 2 for "
        "a mistake in [tool.neolith] or an error of the check itself. Run it "
        "in a process of its own."
    )
    # Django's own checks are not run first: on Django 6.1 they ask every
    # database alias's features, which can open a connection.
    requires_system_checks: list[str] = []  # noqa: RUF012

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        """Run the check, write its lines, and exit with its status."""
        status = _check.command(
            _check.stream_writer(self.stdout), show_traceback=options["traceback"]
        )
        if status:
            sys.exit(status)
