"""The host's checks of its settings and its request entry (ADR 0022).

Each rule is a plain function over the value it judges, which returns every
problem it finds, so one function serves every place that checks the same
input: set-up, through the system checks registered under the tag
``neolith``, and the root middleware's constructor. The admitted set of each
input is stated in its function; anything else is a problem, never a pass.

The ids are the ones this step fixes from ADR 0022's working numbers:
``neolith.E001`` for ``NEOLITH``, ``neolith.E006`` for ``MIDDLEWARE`` and
``neolith.W001`` for an admin class in no installed app.
"""

import math
import sys
from typing import Any, Final

from django.apps import apps as global_apps
from django.conf import settings
from django.core import checks

__all__ = [
    "DEFAULT_REQUEST_DEADLINE",
    "ROOT_MIDDLEWARE",
    "TAG",
    "admin_check",
    "middleware_check",
    "middleware_problems",
    "neolith_check",
    "neolith_problems",
    "register",
    "request_deadline",
]

TAG: Final = "neolith"
ROOT_MIDDLEWARE: Final = "neolith.middleware.RootMiddleware"
# The request deadline when NEOLITH has no REQUEST_DEADLINE (ADR 0017
# Decision 4), in seconds.
DEFAULT_REQUEST_DEADLINE: Final = 30.0
# The bounds of a configured request deadline, in seconds: 1 ms to
# 9999999999 ms, the budget cap (ADR 0022 Decision 3).
_SHORTEST: Final = 0.001
_LONGEST: Final = 9999999.999
_DEADLINE: Final = 'NEOLITH["REQUEST_DEADLINE"]'
# The keys of NEOLITH that exist so far. Later steps add theirs (ADR 0022
# Decision 3).
_KEYS: Final = ("REQUEST_DEADLINE",)
_MISSING: Final = object()
_SHOWN: Final = 80


def _type(value: object) -> str:
    return type(value).__qualname__


def _shown_key(key: object) -> str:
    """A key for a message: an exact ``str`` by its content, within a bound,
    and anything else by its type alone, so no author code runs.
    """
    if type(key) is str:
        text: str = key
        return repr(text) if len(text) <= _SHOWN else f"{text[:_SHOWN]!r}..."
    return f"of type {_type(key)}"


def _shown_number(value: int | float) -> str:
    """A number for a message. ``str`` of a very large ``int`` raises, so
    such a one is shown by its size.
    """
    if type(value) is int and value.bit_length() > 128:
        return f"an int of {value.bit_length()} bits"
    return repr(value)


def middleware_problems(value: object) -> list[str]:
    """Judge ``MIDDLEWARE``: a ``list`` of ``str`` whose first entry is the
    root middleware, which is listed once (ADR 0022, the builder's table).
    """
    if type(value) is not list:
        return [f"MIDDLEWARE: expected a list, got {_type(value)}"]
    entries: list[object] = value
    problems = [
        f"MIDDLEWARE: entry {index}: expected a str, got {_type(entry)}"
        for index, entry in enumerate(entries)
        if type(entry) is not str
    ]
    named = [type(entry) is str and entry == ROOT_MIDDLEWARE for entry in entries]
    if not named or not named[0]:
        problems.append(f"MIDDLEWARE: {ROOT_MIDDLEWARE} must be the first entry")
    if sum(named) > 1:
        problems.append(f"MIDDLEWARE: {ROOT_MIDDLEWARE} is listed {sum(named)} times")
    return problems


def _deadline_problems(value: object) -> list[str]:
    if value is None:
        return []
    if type(value) not in (int, float):
        return [
            f"{_DEADLINE}: expected a number of seconds or None, got {_type(value)}"
        ]
    number: int | float = value  # type: ignore[assignment]  # judged by type()
    if type(number) is float and not math.isfinite(number):
        return [f"{_DEADLINE}: {number!r} is not finite"]
    if number < _SHORTEST or number > _LONGEST:
        return [
            f"{_DEADLINE}: {_shown_number(number)} is outside {_SHORTEST} to "
            f"{_LONGEST} seconds"
        ]
    return []


def neolith_problems(value: object = _MISSING) -> list[str]:
    """Judge ``NEOLITH``: present, a ``dict`` whose keys are in the key
    table, each with its one form (ADR 0022 Decision 3). Leave ``value`` out
    when the setting is absent.
    """
    if value is _MISSING:
        return ["settings: NEOLITH is required; it is {} when nothing is set"]
    if type(value) is not dict:
        return [f"NEOLITH: expected a dict, got {_type(value)}"]
    problems: list[str] = []
    # Items are read, never looked up, so no key's own hooks run.
    entries: dict[object, object] = value
    for key, item in entries.items():
        if type(key) is not str or key not in _KEYS:
            problems.append(
                f"NEOLITH: unknown key {_shown_key(key)}; the keys are "
                + ", ".join(sorted(_KEYS))
            )
        elif key == "REQUEST_DEADLINE":
            problems.extend(_deadline_problems(item))
    return problems


def request_deadline(value: dict[str, Any]) -> float | None:
    """The request deadline, in seconds, of a ``NEOLITH`` that passed
    ``neolith_problems``: 30 when the key is absent, ``None`` for none.
    """
    for key, item in value.items():
        if key == "REQUEST_DEADLINE":
            return None if item is None else float(item)
    return DEFAULT_REQUEST_DEADLINE


def _hosting() -> bool:
    """Whether neolith's app is in Django's global registry: its checks
    judge a host, and a process that installs no host has none.
    """
    return global_apps.is_installed(TAG)


def middleware_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E006``: the root middleware's place in ``MIDDLEWARE``."""
    if not _hosting():
        return []
    return [
        checks.Error(problem, id="neolith.E006")
        for problem in middleware_problems(getattr(settings, "MIDDLEWARE", _MISSING))
    ]


def neolith_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.E001``: the ``NEOLITH`` setting."""
    if not _hosting():
        return []
    return [
        checks.Error(problem, id="neolith.E001")
        for problem in neolith_problems(getattr(settings, "NEOLITH", _MISSING))
    ]


def admin_check(**kwargs: object) -> list[checks.CheckMessage]:
    """``neolith.W001``: each admin class registered on an admin site whose
    module is in no installed app. Its views run in a request root of no
    app, so a contract call from them is refused (ADR 0017 Decision 4).

    Only admin sites that exist are read, and the admin is not imported
    when nothing has imported it.
    """
    if not _hosting():
        return []
    sites = sys.modules.get("django.contrib.admin.sites")
    if sites is None:
        return []
    found: set[str] = set()
    for site in list(vars(sites).get("all_sites", ())):
        registry = vars(site).get("_registry")
        if type(registry) is not dict:
            continue
        for admin in list(registry.values()):
            klass = type(admin)
            module = vars(klass).get("__module__")
            if type(module) is not str:
                continue
            if global_apps.get_containing_app_config(module) is None:
                found.add(f"{module}.{klass.__qualname__}")
    return [
        checks.Warning(
            f"{name}: admin class in no installed app; its views have no calling "
            "app, and a contract call from them is refused",
            id="neolith.W001",
        )
        for name in sorted(found)
    ]


def register() -> None:
    """Register the host's checks under the tag ``neolith``; registering
    again changes nothing.
    """
    for check in (neolith_check, middleware_check, admin_check):
        checks.register(check, TAG)
