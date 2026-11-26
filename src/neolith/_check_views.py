"""The boundary pass's rule on views: ``neolith.W102``, the project's own
code in no app that serves requests (spec 05, APP-69; ADR 0030, as its
Decision 9 narrows ADR 0017 Decision 4).

Every URL pattern reachable from ``ROOT_URLCONF`` through Django's URL
resolver is read, and its view judged by the function that binds request
roots, ``neolith._serving.label_of``: a view that serves as an app is never
reported. One that serves as no app is reported when the code its request
runs includes the project's own code: a module that the first-party map
holds (``neolith._check_source.Source.map``) and that no installed app
contains. That code is read along the links ``label_of`` follows, and only
those: a bound method is its function, an exact ``functools.partial`` its
``func``, a function what its ``__wrapped__`` serves as, the function
``View.as_view`` builds its ``view_class``, and a class or any other
callable its own or its type's code. So Django's generic views,
``static.serve`` and a library's views are not reported, while a view of the
project's, under any wrapper, and a wrapper of the project's around one of
those, are. The finding is placed at the innermost such code: a function's
first line, or a class's.

Not reported: an admin view Django builds for a ``ModelAdmin``, which
``neolith.W001`` reports when its class is the project's; a walk deeper than
``label_of`` follows; an entry of a URLconf that is neither Django's
``URLPattern`` nor its ``URLResolver``; and an include of a URLconf inside
itself, which Django never finishes resolving either. Nothing is reported
when ``ROOT_URLCONF`` is not set or does not import, which the host's own
check reports (ADR 0030 Decision 7, phase 4).
"""

import functools
import inspect
import os
import types
from collections.abc import Iterator
from importlib import import_module
from typing import Any, Final, NamedTuple

from django.apps import apps as global_apps
from django.core import checks

from neolith import _naming, _serving
from neolith._check import Finding, place, shown
from neolith._check_source import Source

__all__ = ["Code", "codes", "findings", "patterns"]
# The end of a list of patterns, as the walk reads it.
_END: Final = object()
_HINT: Final = (
    "move the view into the app whose work it does, so that its requests run "
    "in that app's root"
)


class Code(NamedTuple):
    """One piece of code a view's request runs: the module it is in and the
    function or class itself.
    """

    module: object
    thing: object


def findings(source: Source) -> list[Finding]:
    """``neolith.W102`` for every URL pattern reachable from
    ``ROOT_URLCONF`` whose view serves as no app and runs the project's own
    code.
    """
    from django.conf import settings

    name = getattr(settings, "ROOT_URLCONF", None)
    if type(name) is not str:
        return []
    try:
        import_module(name)
    except Exception:  # reported by the host's own check, phase 4
        return []
    first_party = source.map()
    found: list[Finding] = []
    for route, view in patterns(name):
        if _serving.label_of(view) is not None:
            continue
        walked = codes(view)
        if walked is None:
            continue
        own = [
            code
            for code in walked
            if type(code.module) is str
            and code.module in first_party
            and first_party[code.module].path is not None
            and global_apps.get_containing_app_config(code.module) is None
        ]
        if not own:
            continue
        code = own[-1]
        path: str = first_party[code.module].path  # type: ignore[assignment,index]
        message = checks.Warning(
            f"URL pattern {shown(repr(route))}: view {shown(_named(code))} is the "
            "project's code in no installed app; a request it serves has no "
            "calling app, and a contract call from it is refused",
            hint=_HINT,
            id="neolith.W102",
        )
        found.append(Finding(message, None, place(path), _line(code.thing, path)))
    return found


def patterns(urlconf: str) -> Iterator[tuple[str, object]]:
    """Each URL pattern reachable from the URLconf ``urlconf``, as its whole
    route and its view, in the order Django tries them. An include of a
    URLconf inside itself is not followed again.
    """
    from django.urls import URLPattern, URLResolver, get_resolver

    root = get_resolver(urlconf)
    # Each entry: the patterns left to read, the route so far, and the
    # URLconfs it is included from. The walk keeps its own stack, so no
    # depth of includes ends it with RecursionError.
    stack: list[tuple[Iterator[object], str, tuple[int, ...]]] = [
        (iter(list(root.url_patterns)), "", (id(root.urlconf_module),))
    ]
    while stack:
        entries, prefix, within = stack[-1]
        entry = next(entries, _END)
        if entry is _END:
            stack.pop()
            continue
        if isinstance(entry, URLPattern):
            yield f"{prefix}{entry.pattern}", entry.callback
        elif isinstance(entry, URLResolver):
            module = entry.urlconf_module
            if id(module) in within:
                continue
            stack.append(
                (
                    iter(list(entry.url_patterns)),
                    f"{prefix}{entry.pattern}",
                    (*within, id(module)),
                )
            )


def codes(view: object) -> list[Code] | None:
    """The code a request to ``view`` runs, outermost first, read along the
    links ``label_of`` follows and ending where it ends; ``None`` for an
    admin view Django builds for a ``ModelAdmin``, and for a walk deeper
    than ``label_of`` follows.
    """
    walked: list[Code] = []
    # As label_of: one link per step, and the step past its bound has no
    # app.
    for _ in range(_serving._LINKS + 1):
        kind = type(view)
        if kind is types.MethodType:
            view = view.__func__  # type: ignore[attr-defined]
            continue
        if kind is functools.partial:
            view = view.func  # type: ignore[attr-defined]
            continue
        if issubclass(kind, type):
            walked.append(Code(_serving._class_module(view), view))  # type: ignore[arg-type]
            return walked
        if kind is not types.FunctionType:
            walked.append(Code(_serving._class_module(kind), kind))
            return walked
        function: types.FunctionType = view  # type: ignore[assignment]
        attributes = vars(function)  # safe read: an exact function's own dict
        walked.append(Code(_serving._globals_name(function), function))
        if _serving._app_code(function):
            return walked
        if _serving._CODE.__get__(function) is _serving._AS_VIEW:
            view_class: Any = attributes.get("view_class")
            if issubclass(type(view_class), type):
                walked.append(Code(_serving._class_module(view_class), view_class))
                return walked
        if (
            _serving._sets(function, _serving._ADMIN_VIEW)
            and "model_admin" in attributes
        ):
            return None
        link = attributes.get("__wrapped__")
        if link is None:
            return walked
        view = link
    return None


def _named(code: Code) -> str:
    """``<module>.<qualname>`` of a function or a class. A function is named
    by its code, which ``functools.wraps`` never changes, not by the
    ``__qualname__`` it copies onto a wrapper.
    """
    if issubclass(type(code.thing), type):
        return _naming.full_name(code.thing, None)  # type: ignore[arg-type]
    return f"{code.module}.{_serving._CODE.__get__(code.thing).co_qualname}"


def _line(thing: object, path: str) -> int:
    """The first line of a function or a class in the file ``path``; 1 when
    it is not read from that file.
    """
    if type(thing) is types.FunctionType:
        found = _serving._CODE.__get__(thing)
        file, line = found.co_filename, found.co_firstlineno
    else:
        try:
            file = inspect.getsourcefile(thing)  # type: ignore[arg-type]
            line = inspect.getsourcelines(thing)[1]  # type: ignore[arg-type]
        except (OSError, TypeError, SyntaxError, ValueError):
            # inspect reads the file again, which may have changed since.
            return 1
    try:
        same = file is not None and os.path.samefile(file, path)
    except (OSError, ValueError):
        same = False
    return max(line, 1) if same else 1
