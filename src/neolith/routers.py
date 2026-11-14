"""The router that keeps each app's data on its own database (ADR 0009).

List ``"neolith.routers.AppRouter"`` as the one entry of
``DATABASE_ROUTERS``. Every hosted app that declares the ``database``
resource has an alias of its own, named by its label; every other label,
an app with no declaration, one of Django's contrib apps or a label of no
installed app such as the database cache's ``django_cache``, lives on
``default``. That alias is the label's *home*.

The router answers every question itself, from the labels the question
names and the declarations installed in Django's global registry. It reads
no hint, so an instance's ``_state.db`` never moves a query, and it never
answers ``None``:

- ``db_for_read`` and ``db_for_write``: the home of the model's label;
- ``allow_migrate``: true only on the home of the label;
- ``allow_relation``: true only when both labels have one home.

Two apps on two aliases cannot share a transaction: ``transaction.atomic``
and ``on_commit`` act on one alias, so app code names its own,
``transaction.atomic(using="<label>")``. An operation that names no alias,
such as a bare ``transaction.atomic()`` or ``django.db.connection``, goes to
``default``, never to an app's alias.
"""

from typing import Final, final

from django.apps import apps as global_apps
from django.db import DEFAULT_DB_ALIAS

from neolith._declaration import declaration_of

__all__ = ["AppRouter", "home"]

DATABASE: Final = "database"


def home(label: str) -> str:
    """The alias of a label: the label itself for a hosted app that declares
    the ``database`` resource, ``default`` for every other label.

    Read from the app config installed under the label in Django's global
    registry, so the answer follows the apps installed now.
    """
    config = global_apps.app_configs.get(label)
    declaration = declaration_of(type(config))
    if declaration is not None and DATABASE in declaration.resources:
        return label
    return DEFAULT_DB_ALIAS


def _label(model: object) -> str:
    found: str = model._meta.app_label  # type: ignore[attr-defined]
    return found


@final
class AppRouter:
    """Django's database router for a neolith host: every model to the home
    of its label (``neolith.routers.home``).
    """

    def db_for_read(self, model: object, **hints: object) -> str:
        """The home of the model's label; ``hints`` are not read."""
        return home(_label(model))

    def db_for_write(self, model: object, **hints: object) -> str:
        """The home of the model's label; ``hints`` are not read."""
        return home(_label(model))

    def allow_relation(self, obj1: object, obj2: object, **hints: object) -> bool:
        """Whether both labels have one home; ``hints`` are not read."""
        return home(_label(obj1)) == home(_label(obj2))

    def allow_migrate(self, db: str, app_label: str, **hints: object) -> bool:
        """Whether ``db`` is the home of ``app_label``; ``hints`` are not
        read.
        """
        return db == home(app_label)
