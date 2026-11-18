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
  migrations is skipped with a line that says so;
- every other installed app gets ``E010`` and ``E011`` too, since Django's
  ``migrate`` loads the whole graph and judges conflicts over it.

Then it runs Django's ``migrate <label> --database=<alias>`` for each, and
stops at the first that fails, naming the labels migrated, the label that
failed with the migrations its database records as applied, which Django
keeps for each migration that completed, and the labels not migrated. It
neither boots nor serves.
"""

from collections.abc import Callable, Sequence
from io import TextIOBase
from typing import Final

from django.apps import apps as global_apps
from django.conf import settings
from django.core import checks
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

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
            raise CommandError(_failed(steps, index, error, done)) from error
        done.append(label)


def _failed(
    steps: Sequence[tuple[str, str]],
    index: int,
    error: Exception,
    done: Sequence[str],
) -> str:
    """The diagnostic of a step that failed at ``steps[index]``, read from
    the state of the databases it touched: Django's ``migrate`` also applies
    the migrations of other labels that a label's depend on, so a label
    after the one that failed may be migrated, in part or whole, on a
    database the step touched. A database it never touched is not opened,
    since opening a SQLite path creates its file.
    """
    label, alias = steps[index]
    touched = {where for _, where in steps[: index + 1]}
    lines = [
        f"app {label}: database {alias}: the migrate step failed: {_cause(error)}",
    ]
    migrated = list(done)
    partly: list[str] = []
    untouched: list[str] = []
    for other, where in steps[index + 1 :]:
        if where not in touched:
            untouched.append(other)
            continue
        state = _state(other, where)
        if type(state) is str:
            partly.append(f"{other}, {state}")
        elif not state[0]:
            migrated.append(other)
        elif state[1]:
            partly.append(f"{other}, with {', '.join(state[1])} applied")
        else:
            untouched.append(other)
    lines.append(f"migrated: {', '.join(migrated) or 'none'}")
    failed = _state(label, alias)
    if type(failed) is str:
        lines.append(f"failed: {label}, {failed}")
    elif failed[1]:
        lines.append(f"failed: {label}, with {', '.join(failed[1])} applied")
    else:
        lines.append(f"failed: {label}, with none of its migrations applied")
    lines += [f"partly migrated: {entry}" for entry in partly]
    if untouched:
        lines.append(f"not migrated: {', '.join(untouched)}")
    return "\n".join(lines)


def _state(label: str, alias: str) -> tuple[list[str], list[str]] | str:
    """The migrations of ``label`` still to apply on ``alias``, and those
    applied, as Django's planner reads them; or why they cannot be read.
    """
    try:
        executor = MigrationExecutor(connections[alias])
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes(label))
        rows = executor.loader.applied_migrations or {}
    except Exception as error:
        return f"whose migrations cannot be read: {_cause(error)}"
    pending = [m.name for m, back in plan if m.app_label == label and not back]
    applied = sorted(name for owner, name in rows if owner == label)
    return pending, applied


def _say(out: TextIOBase, verbosity: int) -> Callable[[str], None]:
    """Write one line of the step's own, unless ``verbosity`` is 0."""

    def say(line: str) -> None:
        if verbosity > 0:
            out.write(line + "\n")

    return say
