"""Which app's code serves a view (ADR 0017 Decision 4, spec 02 TRN-57).

One rule, used by the root middleware to bind a request's root and by the
``neolith.W001`` check: the app is that of the code that runs for the view,
not of code that only wraps it. Everything is read with ``vars()`` and
``type()``, never through the author's attribute hooks, and judged by the
installed app that contains the code's module.

- A wrapper is followed to what it wraps through the links Python keeps
  without running the author's code: a bound method's function, an exact
  ``functools.partial``'s function, and the ``__wrapped__`` that
  ``functools.wraps`` leaves. A wrapper that keeps no link counts as the
  code that runs.
- A view Django's admin wraps carries its ``ModelAdmin``. Its code is the
  first code outside ``django.contrib.admin`` found in its class's MRO, most
  derived first: a class defined outside the admin, or, in a class the admin
  built (``admin.site.register(Model, **options)``), a function it was given.
  When there is none, the admin runs only its own code, and the view serves
  the app whose model it shows.
- Any other callable is its type's code.
"""

import functools
import types
from typing import Final

from django.apps import apps as global_apps
from django.db.models.options import Options

__all__ = ["admin_code", "label_of"]

_ADMIN: Final = "django.contrib.admin"
# Far more links than any real chain of wrappers; a longer chain, or a loop,
# has no app.
_LINKS: Final = 64


def _app(module: object) -> str | None:
    """The label of the installed app that contains ``module``, or ``None``."""
    if type(module) is not str:
        return None
    config = global_apps.get_containing_app_config(module)
    return None if config is None else config.label


def _in_admin(module: object) -> bool:
    if type(module) is not str:
        return False
    config = global_apps.get_containing_app_config(module)
    return config is not None and config.name == _ADMIN


def _name(code: object) -> str:
    """``<module>.<qualname>`` of a class or function, read from its own
    attributes; the qualname of a class is read through ``type``'s own slot.
    """
    if type(code) is types.FunctionType:
        return f"{code.__module__}.{code.__qualname__}"
    module = vars(code).get("__module__")
    qualname = type.__dict__["__qualname__"].__get__(code)
    return f"{module}.{qualname}"


def _given(klass: type) -> list[types.FunctionType]:
    """The functions a class holds in its own namespace, directly or in a
    tuple or list, in the order it holds them: what
    ``admin.site.register(Model, **options)`` gives the class it builds.
    """
    found: list[types.FunctionType] = []
    for value in list(vars(klass).values()):
        items = list(value) if type(value) in (tuple, list) else [value]
        found.extend(item for item in items if type(item) is types.FunctionType)
    return found


def admin_code(admin: object) -> tuple[str | None, str | None]:
    """The app an admin's views serve as, and the code that decided it.

    Returns ``(label, code)``: ``code`` describes the first code outside the
    admin, as ``<module>.<qualname>`` and whether it is a class or a
    function, and ``label`` is its app (``None`` when that code is in no
    installed app); or ``code`` is ``None`` when the admin runs only its own
    code, and ``label`` is the model's app.
    """
    for klass in type(admin).__mro__:
        if klass is object:
            continue
        module = vars(klass).get("__module__")
        if not _in_admin(module):
            return _app(module), f"{_name(klass)}: admin class"
        for function in _given(klass):
            if not _in_admin(function.__module__):
                return _app(function.__module__), f"{_name(function)}: admin function"
    model = vars(admin).get("model")
    meta = vars(model).get("_meta") if issubclass(type(model), type) else None
    label = meta.app_label if type(meta) is Options else None
    installed = type(label) is str and label in global_apps.app_configs
    return (label if installed else None), None


def label_of(view: object) -> str | None:
    """The label of the app whose code serves ``view``, or ``None``."""
    for _ in range(_LINKS):
        kind = type(view)
        if kind is types.MethodType:
            view = view.__func__  # type: ignore[attr-defined]
        elif kind is functools.partial:
            view = view.func  # type: ignore[attr-defined]
        elif kind is types.FunctionType:
            attributes = vars(view)
            if "model_admin" in attributes:
                return admin_code(attributes["model_admin"])[0]
            if "__wrapped__" not in attributes:
                return _app(view.__module__)
            view = attributes["__wrapped__"]
        else:
            return _app(vars(kind).get("__module__"))
    return None
