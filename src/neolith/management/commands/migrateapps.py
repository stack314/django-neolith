"""``migrateapps``: the migrate step (ADR 0022 Decision 11)."""

import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from neolith import _migrate


class Command(BaseCommand):
    """Apply each hosted app's migrations to the app's own database."""

    help = (
        "Apply each hosted app's migrations to the app's own database, and on a "
        "real default database the migrations of the apps without a declaration. "
        "With --check, read every database and schema as Boot does, write "
        "nothing, and exit 1 when Boot or the migrate step would refuse."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Add ``--check``."""
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Report what Boot and the migrate step would refuse, write "
                "nothing, and exit 1 if either would."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        """Run the migrate step, or its check."""
        if options["check"]:
            found = _migrate.check()
            for line in found:
                self.stderr.write(line)
            if found:
                sys.exit(1)
            return
        _migrate.migrate(self.stdout, verbosity=options["verbosity"])
