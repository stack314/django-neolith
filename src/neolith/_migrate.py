"""The migrate step: each hosted app's migrations on its own database
(spec 05, the schema step; ADR 0022 Decision 11; ADR 0009 Decision 7).

The step first works out its labels from the apps installed in Django's
global registry, and refuses before it touches any database:

- each hosted app with a ``database`` resource is migrated on its own
  alias, in the order of ``INSTALLED_APPS``, which is the order the host
  was given. It is refused when its label has no node in the migration
  graph (``neolith.E008``, Boot's predicate), when a migration of it
  depends on another label (``neolith.E013``), and when its migrations do
  not load (``neolith.E010``) or conflict (``neolith.E011``), since Django's
  ``migrate`` judges conflicts over the whole graph and would stop every
  label;
- on a real ``default``, each app without a declaration that has
  migrations is migrated there, after the hosted apps, with the same
  ``E010`` and ``E011``, and ``E013`` for a migration that depends on a
  hosted app's, whose records live in another database; one without
  migrations is skipped with a line that says so.

Then it runs Django's ``migrate <label> --database=<alias>`` for each, and
stops at the first that fails, naming the labels migrated and not
migrated. It neither boots nor serves.
"""

from collections.abc import Callable, Sequence
from io import TextIOBase
from typing import Final

from django.apps import apps as global_apps
from django.conf import settings
from django.core import checks
from django.core.management import call_command
from django.core.management.base import CommandError

from neolith import _boot, _databases
from neolith._declaration import declaration_of

__all__ = ["migrate"]

_SHOWN: Final = 300
_HOST: Final = "neolith"


def _cause(error: BaseException) -> str:
    try:
        text = str(error)
    except Exception:
        text = "<the message cannot be read>"
    if len(text) > _SHOWN:
        text = f"{text[:_SHOWN]}..."
    return f"{type(error).__qualname__}: {text}"


def _labels() -> tuple[list[str], list[str]]:
    """The hosted apps with a database, and on a real ``default`` the apps
    without a declaration, each in the order of ``INSTALLED_APPS``.
    """
    neolith = getattr(settings, "NEOLITH", {})
    named = neolith.get("DATABASES", {}) if type(neolith) is dict else {}
    real_default = type(named) is dict and _databases.DEFAULT in named
    hosted: list[str] = []
    undeclared: list[str] = []
    for config in global_apps.get_app_configs():
        declaration = declaration_of(type(config))
        if declaration is None:
            if real_default and config.label != _HOST:
                undeclared.append(config.label)
        elif "database" in declaration.resources:
            hosted.append(config.label)
    return hosted, undeclared


def migrate(out: TextIOBase, *, verbosity: int) -> None:
    """Run the migrate step for the host Django holds.

    Raises:
        CommandError: The step refused before it touched a database, or
            migrating a label failed; the message names the labels migrated
            and not migrated.
    """
    hosted, undeclared = _labels()
    found, migrated = _boot.code_messages(hosted, undeclared)
    refusals = sorted(f"({m.id}) {m.msg}" for m in found if m.level >= checks.ERROR)
    if refusals:
        raise CommandError(
            "the migrate step refused before it touched a database:\n"
            + "\n".join(refusals)
        )
    steps = [(label, label) for label in hosted]
    steps += [(label, _databases.DEFAULT) for label in migrated]
    say = _say(out, verbosity)
    for label in undeclared:
        if label not in migrated:
            say(f"Skipping app {label}: it has no migrations")
    done: list[str] = []
    for index, (label, alias) in enumerate(steps):
        say(f"Migrating app {label} on database {alias}")
        try:
            call_command(
                "migrate",
                label,
                database=alias,
                interactive=False,
                verbosity=verbosity,
                stdout=out,
            )
        except Exception as error:
            raise CommandError(
                _failed(label, alias, error, done, [s[0] for s in steps[index:]])
            ) from error
        done.append(label)


def _failed(
    label: str,
    alias: str,
    error: Exception,
    done: Sequence[str],
    left: Sequence[str],
) -> str:
    return (
        f"app {label}: database {alias}: the migrate step failed: {_cause(error)}\n"
        f"migrated: {', '.join(done) or 'none'}\n"
        f"not migrated: {', '.join(left)}"
    )


def _say(out: TextIOBase, verbosity: int) -> Callable[[str], None]:
    """Write one line of the step's own, unless ``verbosity`` is 0."""

    def say(line: str) -> None:
        if verbosity > 0:
            out.write(line + "\n")

    return say
