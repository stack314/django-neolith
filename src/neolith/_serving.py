"""Which app's code serves a view (ADR 0017 Decision 4, spec 02 TRN-57).

One rule, used by the root middleware to bind a request's root and by the
``neolith.W001`` check: the app is that of the code that runs for the view,
not of code that only wraps it. Everything is read with ``vars()`` and
``type()``, never through the author's attribute hooks, and judged by the
installed app that contains the code's module. *Hosted* code is code in an
installed app that has a declaration; *framework* code is Django's own.

- A function ``View.as_view`` made carries its ``view_class``: it is that
  class's code, whatever ``as_view`` copied onto it from ``dispatch``.
- A bound method is its function's code, and a class is its own module's.
- A wrapper's links, the ``__wrapped__`` that ``functools.wraps`` leaves
  and an exact ``functools.partial``'s function, are followed only while
  they lead into hosted code, so a link into the framework, or into code
  the view only inherits, never decides. A wrapper that is not followed
  counts as the code that runs; a partial has no code of its own.
- A view Django's admin wraps carries its ``ModelAdmin``. It serves as the
  app of the most derived class in its MRO defined in a hosted app or in
  the project, in no installed app. When there is none, its classes are
  the framework's or an unhosted app's, such as ``UserAdmin``, and it
  serves the app whose model it shows. Options given
  to ``admin.site.register`` are configuration of the class Django builds,
  so they decide nothing.
- Any other callable is its type's code.
"""

import functools
import types
from typing import Any, Final

from django.apps import apps as global_apps
from django.db.models.options import Options

from neolith._declaration import declaration_of

__all__ = ["admin_code", "label_of"]

# Far more links than any real chain of wrappers; a longer chain, or a loop,
# has no app.
_LINKS: Final = 64


def _config(module: object) -> object:
    if type(module) is not str:
        return None
    return global_apps.get_containing_app_config(module)


def _app(module: object) -> str | None:
    """The label of the installed app that contains ``module``, or ``None``."""
    config = _config(module)
    return None if config is None else config.label  # type: ignore[attr-defined]


def _hosted(module: object) -> bool:
    config = _config(module)
    return config is not None and declaration_of(type(config)) is not None


def _project(module: object) -> bool:
    """Whether ``module`` is project code, in no installed app. A builtin
    class, such as ``object``, names no module of its own and is not.
    """
    return type(module) is str and _config(module) is None


def _class_module(klass: type) -> object:
    return vars(klass).get("__module__")


def _module(code: object) -> object:
    """The module of the code itself, without following any wrapper's link.
    A partial has no code of its own: its type's module is ``functools``.
    """
    kind = type(code)
    if kind is types.MethodType:
        code = code.__func__  # type: ignore[attr-defined]
        kind = type(code)
    if kind is types.FunctionType:
        view_class: Any = vars(code).get("view_class")
        if issubclass(type(view_class), type):
            return _class_module(view_class)
        return code.__module__
    if issubclass(kind, type):
        return _class_module(code)  # type: ignore[arg-type]
    return _class_module(kind)


def _link(code: object) -> object:
    """The one link a wrapper keeps to what it wraps, or ``None``."""
    kind = type(code)
    if kind is types.MethodType:
        code = code.__func__  # type: ignore[attr-defined]
        kind = type(code)
    if kind is types.FunctionType:
        attributes = vars(code)
        if issubclass(type(attributes.get("view_class")), type):
            return None
        return attributes.get("__wrapped__")
    if kind is functools.partial:
        return code.func  # type: ignore[attr-defined]
    return None


def _admin(code: object) -> object:
    kind = type(code)
    if kind is types.MethodType:
        code = code.__func__  # type: ignore[attr-defined]
        kind = type(code)
    return vars(code).get("model_admin") if kind is types.FunctionType else None


def admin_code(admin: object) -> tuple[str | None, str | None]:
    """The app an admin's views serve as, and the class that decided it.

    Returns ``(label, code)``: ``code`` is ``<module>.<qualname>: admin
    class`` for the most derived class defined in a hosted app or in no
    installed app, and ``label`` its app (``None`` for the project); or
    ``code`` is ``None``, and ``label`` is the app whose model the admin
    shows, or ``None`` when that model's app is not installed.
    """
    for klass in type(admin).__mro__:
        module = _class_module(klass)
        if _hosted(module):
            return _app(module), f"{_name(klass)}: admin class"
        if _project(module):
            return None, f"{_name(klass)}: admin class"
    model = vars(admin).get("model")
    meta = vars(model).get("_meta") if issubclass(type(model), type) else None
    label = meta.app_label if type(meta) is Options else None
    installed = type(label) is str and label in global_apps.app_configs
    return (label if installed else None), None


def _name(klass: type) -> str:
    """``<module>.<qualname>`` of a class, read through ``type``'s own slot."""
    qualname = type.__dict__["__qualname__"].__get__(klass)
    return f"{_class_module(klass)}.{qualname}"


def label_of(view: object) -> str | None:
    """The label of the app whose code serves ``view``, or ``None``."""
    for _ in range(_LINKS):
        admin = _admin(view)
        if admin is not None:
            return admin_code(admin)[0]
        link = _link(view)
        if link is not None and _hosted(_first_module(link)):
            view = link
            continue
        return _app(_module(view))
    return None


def _first_module(code: object) -> object:
    """The module a link leads to: through partials, which have no code of
    their own, to the first code that has one.
    """
    for _ in range(_LINKS):
        if type(code) is not functools.partial:
            return _module(code)
        code = code.func
    return None
