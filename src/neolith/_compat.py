"""Version-dependent code, and Django internals the host relies on.

ADR 0012: neolith supports Django 5.2 LTS on Python 3.12 up to the newest
Django on the newest Python it supports. When the preferred API exists only
above that floor, the version check and both code paths live here, each with a
comment naming the Python or Django release that lets the shim go. No other
module compares versions.

ADR 0022 Decision 1 also places here the one access to Django's private
settings state that the builder needs.
"""

from typing import Any

from django.conf import LazySettings
from django.db.migrations.loader import MigrationLoader
from django.utils.functional import empty

__all__ = ["all_replaced_applied", "unconfigure"]


def unconfigure(settings: LazySettings) -> None:
    """Put Django's settings back to unconfigured, so the next read of a
    setting imports the settings module again.

    ``_wrapped`` is the attribute Django's own ``override_settings`` assigns
    (``django/test/utils.py``), at 5.2 and 6.1 alike.
    """
    settings._wrapped = empty


def all_replaced_applied(
    loader: MigrationLoader,
    key: tuple[str, str],
    applied: Any,  # noqa: ANN401
) -> bool:
    """Whether ``key`` is a squashed migration whose replaced migrations are
    all applied, as ``MigrationLoader.check_consistent_history`` skips it.

    Django 6.1 has ``MigrationLoader.all_replaced_applied``, which follows
    squashed migrations that replace squashed ones; Django 5.2 checks one
    level inline. This shim goes when the floor has the method.
    """
    method = getattr(loader, "all_replaced_applied", None)
    if method is not None:
        found: bool = method(key, applied)
        return found
    replacement = loader.replacements.get(key)
    return replacement is not None and all(m in applied for m in replacement.replaces)
