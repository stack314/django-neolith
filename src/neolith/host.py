"""The app host's settings builder (ADR 0022 Decision 1).

A Django settings module ends with::

    globals().update(build_settings(["<pkg>.<app>", ...], globals()))

and every tool that reads ``DJANGO_SETTINGS_MODULE`` works unchanged.

State the module owns (CONTRIBUTING K6): the first refusal, which every
later call in the process raises again.
"""

import threading
from typing import Any, Final

from django.conf import settings

from neolith import _compat, _configure

__all__ = ["build_settings"]

_LOCK: Final = threading.Lock()
_refused: BaseException | None = None


def build_settings(apps: list[str], deployer: dict[str, Any]) -> dict[str, Any]:
    """Build the whole settings mapping of a host from its apps and the
    deployer's settings.

    The host builds ``INSTALLED_APPS``, ``DATABASES`` and
    ``DATABASE_ROUTERS`` from the apps' declarations and ``NEOLITH``, and
    refuses them in ``deployer``. It checks ``NEOLITH``, ``MIDDLEWARE`` and
    ``SILENCED_SYSTEM_CHECKS``, and passes every other upper-case name to
    Django unchanged. Names that are not upper case are ignored. Each app is
    imported to read its declaration, before any setting exists, so an app
    whose import reads a setting is refused.

    A refusal is final for the process: every later call raises it again,
    whatever its arguments, and settings that Django built during the call,
    or from the calling module before it, are put back to unconfigured, so
    that no tool goes on with half-built settings.

    Args:
        apps: The hosted apps, each the import path of an app package, in
            the order ``INSTALLED_APPS`` lists them.
        deployer: The deployer's settings, such as a settings module's
            ``globals()``; it holds ``NEOLITH``, which is ``{}`` when nothing
            is set.

    Returns:
        The deployer's upper-case settings with ``INSTALLED_APPS``,
        ``DATABASES`` and ``DATABASE_ROUTERS`` added.

    Raises:
        DeclarationError: A hosted app's declaration is refused, or Resolve
            refuses the hosted apps.
        ImproperlyConfigured: Any other input is outside what the host
            admits, or an earlier call in this process was refused.
    """
    global _refused
    from django.core.exceptions import ImproperlyConfigured

    with _LOCK:
        if _refused is not None:
            raise _refused
        before = settings.configured
        calling = deployer.get("__name__") if type(deployer) is dict else None
        early = (
            before
            and type(calling) is str
            and getattr(settings, "SETTINGS_MODULE", None) == calling
        )
        try:
            if early:
                raise ImproperlyConfigured(
                    "settings: a setting was read before build_settings was called"
                )
            return _configure.configure(apps, deployer)
        except Exception as error:
            _refused = error
            if early or (not before and settings.configured):
                _compat.unconfigure(settings)
            raise
