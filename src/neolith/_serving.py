"""Which app's code serves a view (ADR 0017 Decision 4, spec 02 TRN-57).

One recursive rule, used by the root middleware to bind a request's root and
by the ``neolith.W001`` check: the app is that of the code that runs for the
view, not of code that only wraps it. Every question of where a link leads
is answered by the rule itself, so wrappers compose: a wrapper that is the
code it wraps can be put anywhere in a stack without changing the answer.
Everything is read with ``vars()`` and ``type()``, never through the
author's attribute hooks. *Hosted* code is code in an installed app that
has a declaration; Django's own code is never an app's code, though its
contrib packages are installed apps.

- A bound method and an exact ``functools.partial`` run no code of their
  own: each is the code of its function.
- A function's own code is in the module whose globals it runs in, which
  ``functools.wraps`` never changes. A copied attribute is trusted only on
  the function that set it: ``__module__`` never is, ``view_class`` only on
  the function ``View.as_view`` builds, and ``model_admin`` only on the
  function Django's admin builds for a ``ModelAdmin`` view.
- A function whose code is an app's (in an installed app other than
  Django's own) serves as what its ``__wrapped__`` serves as, when that is
  a hosted app; otherwise as its own app. So an app's wrapper follows a
  link into hosted code, past any wrapper that is the code it wraps, and is
  its own app's code over anything else.
- A function whose code is no app's (Django's, or the project's) only
  wraps. The function ``as_view`` builds serves as its ``view_class``'s
  app; the admin's view function as ``admin_code`` decides; any other
  serves as what its ``__wrapped__`` serves as, or, with none, as the app
  its module is in (a view of a contrib app, such as ``flatpage``), or as
  no app.
- A class is its own module's code, and any other callable its type's.
- A view Django's admin wraps carries its ``ModelAdmin``. It serves as the
  app of the most derived class in its MRO defined in a hosted app or in
  the project, in no installed app. When there is none, its classes are
  the framework's or an unhosted app's, such as ``UserAdmin``, and it
  serves the app whose model it shows. Options given to
  ``admin.site.register`` are configuration of the class Django builds, so
  they decide nothing.
"""

import functools
import types
from typing import Any, Final

from django.apps import apps as global_apps
from django.db.models.options import Options
from django.views.generic.base import View

from neolith import _naming
from neolith._declaration import declaration_of

__all__ = ["admin_code", "label_of"]

# A function's code, read through the slot of the function type itself, so
# no hook of the author's runs.
# safe read: FunctionType's own slots.
_CODE: Final[Any] = vars(types.FunctionType)["__code__"]
_GLOBALS: Final[Any] = vars(types.FunctionType)["__globals__"]  # safe read: ditto
# The code of the function View.as_view builds: the only Django function
# whose view_class is its own.
_AS_VIEW: Final = _CODE.__get__(View.as_view())
# Where Django's admin builds the function that sets model_admin
# (ModelAdmin.get_urls), named without importing the admin.
_ADMIN_VIEW: Final = (
    "django.contrib.admin.options",
    "ModelAdmin.get_urls.<locals>.wrap.<locals>.wrapper",
)

# Far more links than any real stack of wrappers; a deeper one, or a loop,
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


def _hosted_label(label: str | None) -> bool:
    config = global_apps.app_configs.get(label) if label is not None else None
    return config is not None and declaration_of(type(config)) is not None


def _project(module: object) -> bool:
    """Whether ``module`` is project code, in no installed app. A builtin
    class, such as ``object``, names no module of its own and is not.
    """
    return type(module) is str and _config(module) is None


def _class_module(klass: type) -> object:
    return _naming.entry(_naming.namespace(klass), "__module__")


def _globals_name(function: types.FunctionType) -> object:
    """The module whose globals the function runs in: where its own code
    is, which ``functools.wraps`` never changes.
    """
    return _GLOBALS.__get__(function).get("__name__")


def _django(module: object) -> bool:
    """Whether ``module`` is Django's own, in one of its apps or in none."""
    return type(module) is str and (module == "django" or module.startswith("django."))


def _app_code(function: types.FunctionType) -> bool:
    """Whether the function's own code is an app's: in an installed app
    other than Django's own.
    """
    own = _globals_name(function)
    return not _django(own) and _config(own) is not None


def _sets(function: types.FunctionType, where: tuple[str, str]) -> bool:
    """Whether the function is the one Django builds at ``where`` (its
    module and the qualified name of its code).
    """
    return (_globals_name(function), _CODE.__get__(function).co_qualname) == where


def admin_code(admin: object) -> tuple[str | None, str | None]:
    """The app an admin's views serve as, and the class that decided it.

    Returns ``(label, code)``: ``code`` is ``<module>.<qualname>: admin
    class`` for the most derived class defined in a hosted app or in no
    installed app, and ``label`` its app (``None`` for the project); or
    ``code`` is ``None``, and ``label`` is the app whose model the admin
    shows, or ``None`` when that model's app is not installed.
    """
    for klass in _naming.mro(type(admin)):
        module = _class_module(klass)
        if _hosted(module):
            return _app(module), f"{_name(klass)}: admin class"
        if _project(module):
            return None, f"{_name(klass)}: admin class"
    model = _naming.entry(_naming.instance_dict(admin) or {}, "model")
    meta = (
        _naming.entry(_naming.namespace(model), "_meta")
        if issubclass(type(model), type)
        else None
    )
    label = meta.app_label if type(meta) is Options else None
    installed = type(label) is str and label in global_apps.app_configs
    return (label if installed else None), None


def _name(klass: type) -> str:
    """``<module>.<qualname>`` of a class, read through ``type``'s own slots."""
    return _naming.full_name(klass, None)


def label_of(view: object, _depth: int = 0, _ran: bool = False) -> str | None:
    """The label of the app whose code serves ``view``, or ``None``.

    With ``_ran``, as an app's wrapper asks of its link, an admin view that
    serves as its model's app, which runs none of that app's code, serves
    as no app.
    """
    if _depth > _LINKS:
        return None
    deeper = _depth + 1
    kind = type(view)
    if kind is types.MethodType:
        return label_of(view.__func__, deeper, _ran)  # type: ignore[attr-defined]
    if kind is functools.partial:
        return label_of(view.func, deeper, _ran)  # type: ignore[attr-defined]
    if issubclass(kind, type):
        return _app(_class_module(view))  # type: ignore[arg-type]
    if kind is not types.FunctionType:
        return _app(_class_module(kind))
    function: types.FunctionType = view  # type: ignore[assignment]
    attributes = vars(function)  # safe read: an exact function's own dict
    link = attributes.get("__wrapped__")
    if _app_code(function):
        # An app's code: what it wraps, when that runs a hosted app's code.
        if link is not None:
            inner = label_of(link, deeper, True)
            if _hosted_label(inner):
                return inner
        return _app(_globals_name(function))
    # Code that is no app's only wraps.
    if _CODE.__get__(function) is _AS_VIEW:
        view_class: Any = attributes.get("view_class")
        if issubclass(type(view_class), type):
            return _app(_class_module(view_class))
    if _sets(function, _ADMIN_VIEW) and "model_admin" in attributes:
        label, code = admin_code(attributes["model_admin"])
        return None if _ran and code is None else label
    if link is not None:
        return label_of(link, deeper, _ran)
    return _app(_globals_name(function))
