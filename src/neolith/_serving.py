"""Which app's code serves a view (ADR 0017 Decision 4, spec 02 TRN-57).

One rule, used by the root middleware to bind a request's root and by the
``neolith.W001`` check: the app is that of the code that runs for the view,
not of code that only wraps it. Everything is read with ``vars()`` and
``type()``, never through the author's attribute hooks, and judged by the
installed app that contains the code's module. *Hosted* code is code in an
installed app that has a declaration; *framework* code is Django's own.

- A function's own code is in the module whose globals it runs in, which
  ``functools.wraps`` never changes, when that module is in an installed
  app other than Django's; so a wrapper an app defines is that app's code,
  whatever ``__module__`` or ``view_class`` ``wraps`` copied onto it.
  Otherwise its ``__module__`` says where it belongs, as for project
  wrappers. A wrapper Django supplies, in one of its contrib apps, such as
  ``login_required``, or in none, such as ``never_cache``, only wraps: it
  follows its ``__wrapped__`` wherever that leads.
- A function whose own code is in no installed app and that carries a
  ``view_class``, as ``View.as_view``'s does, is that class's code,
  whatever ``as_view`` copied onto it from ``dispatch``.
- A bound method and an exact ``functools.partial`` run no code of their
  own: each is its function's code. A class is its own module's.
- The ``__wrapped__`` that ``functools.wraps`` leaves is followed only
  while it leads into hosted code, so a link into the framework, or into
  code the view only inherits, never decides. A wrapper whose link is not
  followed counts as the code that runs.
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


def _unwrap(code: object) -> object:
    """A bound method's function, and an exact partial's function, which
    run no code of their own around it, until neither is left.
    """
    for _ in range(_LINKS):
        kind = type(code)
        if kind is types.MethodType:
            code = code.__func__  # type: ignore[attr-defined]
        elif kind is functools.partial:
            code = code.func  # type: ignore[attr-defined]
        else:
            return code
    return None


def _globals_name(function: types.FunctionType) -> object:
    """The module whose globals the function runs in, which
    ``functools.wraps`` never changes.
    """
    return types.FunctionType.__globals__.__get__(function).get("__name__")


def _django(module: object) -> bool:
    """Whether ``module`` is Django's own, in one of its apps or in none."""
    return type(module) is str and (module == "django" or module.startswith("django."))


def _app_code(function: types.FunctionType) -> object:
    """The module of the installed app the function's own code is in, or
    ``None``: never Django's, whose contrib packages are installed apps but
    whose wrappers, such as ``login_required``, only wrap.
    """
    own = _globals_name(function)
    if _django(own) or _config(own) is None:
        return None
    return own


def _own_module(function: types.FunctionType) -> object:
    """Where a function's own code is: the app module its globals belong
    to, when that is an installed app other than Django's; otherwise
    ``__module__``, which ``functools.wraps`` copies from the code it wraps.
    """
    own = _app_code(function)
    return function.__module__ if own is None else own


def _view_class(function: types.FunctionType) -> Any:  # noqa: ANN401
    """The ``view_class`` a function carries, honoured only when the
    function's own code is not an app's, as ``View.as_view``'s is not:
    ``functools.wraps`` copies it onto a wrapper that an app defines.
    """
    found: Any = vars(function).get("view_class")
    if not issubclass(type(found), type) or _app_code(function) is not None:
        return None
    return found


def _module(code: object) -> object:
    """The module of the code itself, without following any wrapper's link."""
    code = _unwrap(code)
    kind = type(code)
    if kind is types.FunctionType:
        view_class = _view_class(code)  # type: ignore[arg-type]
        if view_class is not None:
            return _class_module(view_class)
        return _own_module(code)  # type: ignore[arg-type]
    if issubclass(kind, type):
        return _class_module(code)  # type: ignore[arg-type]
    return _class_module(kind)


def _link(code: object) -> object:
    """The ``__wrapped__`` a function keeps to what it wraps, or ``None``;
    a function that is its ``view_class``'s code keeps none.
    """
    if type(code) is not types.FunctionType:
        return None
    if _view_class(code) is not None:
        return None
    return vars(code).get("__wrapped__")


def _admin(code: object) -> object:
    if type(code) is not types.FunctionType:
        return None
    return vars(code).get("model_admin")


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
        view = _unwrap(view)
        admin = _admin(view)
        if admin is not None:
            return admin_code(admin)[0]
        link = _link(view)
        if link is not None and (_hosted(_module(link)) or _django_wrapper(view)):
            view = link
            continue
        return _app(_module(view))
    return None


def _django_wrapper(code: object) -> bool:
    """Whether ``code`` is a function Django supplies, which only wraps: it
    follows its ``__wrapped__`` wherever that leads.
    """
    return type(code) is types.FunctionType and _django(_globals_name(code))
