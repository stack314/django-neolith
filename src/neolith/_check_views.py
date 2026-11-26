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
project's under any wrapper ``label_of`` follows (a bound method, an exact
``partial``, a function whose ``__wrapped__`` leads to it), and a wrapper of
the project's around one of those, are. The finding is placed at the
innermost such code: a function's first line in the file its code was
compiled from, or a class's first line in the file of the module it names
(as ``label_of`` judges it), each at line 1 when that file does not hold
it. Such a file is read as every family of the run reads it, once
(``Source.placed``), so it is parsed at most once in a run.

Not reported, as blind spots:

- an admin's view that ``neolith.W001`` reports: one Django builds for a
  ``ModelAdmin``, a method bound to an admin whose class W001 names, and a
  function defined in such a class;
- a project view behind a wrapper ``label_of`` does not follow: a function
  that sets no ``__wrapped__``, or a callable object of a library's type,
  such as ``sync_to_async(view)``, ``functools.lru_cache(view)`` or a
  library's ``partial`` subclass;
- a walk deeper than ``label_of`` follows;
- code of the project's in a module that has no file, such as a namespace
  package: it is passed over, and the finding, if any, placed at the next
  code out;
- an entry of a URLconf that is neither Django's ``URLPattern`` nor its
  ``URLResolver``, and one whose route, view or patterns cannot be read
  (Django raises at the first request that reaches it);
- an include of a URLconf inside itself: the walk reads it once, under its
  shortest route, though Django serves it at every depth;
- an include nested more than ``_DEPTH`` deep, and every entry after the
  first ``_ENTRIES`` read, so that a URLconf built lazily without end does
  not keep the walk going.

Nothing is reported when ``ROOT_URLCONF`` is not set or does not import,
which the host's own check reports (ADR 0030 Decision 7, phase 4), nor when
its patterns cannot be read, which Django raises at the first request.
"""

import ast
import functools
import itertools
import os
import sys
import types
from collections.abc import Callable, Iterator
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from django.apps import apps as global_apps
from django.core import checks

from neolith import _naming, _serving
from neolith._check import Finding, place, shown
from neolith._check_source import Source, class_statements

if TYPE_CHECKING:
    from django.urls import URLResolver

__all__ = ["Code", "codes", "findings", "patterns"]
# The end of a list of patterns, as the walk reads it.
_END: Final = object()
# The deepest include the walk follows, and the most entries it reads.
_DEPTH: Final = 64
_ENTRIES: Final = 100_000
_ADMIN_CLASS: Final = ": admin class"
_HINT: Final = (
    "move the view into the app whose work it does, so that its requests run "
    "in that app's root"
)


# Each class statement of a file by its qualified name: its first and last
# lines, from its first decorator.
type _Classes = dict[str, list[tuple[int, int]]]


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
    admins = _reported_admins()
    classes: dict[str, _Classes | None] = {}
    found: list[Finding] = []
    for route, view in patterns(name):
        if _serving.label_of(view) is not None:
            continue
        walked = codes(view, admins)
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
        tree = functools.partial(_tree, source, str(code.module))
        found.append(
            Finding(message, None, place(path), _line(code.thing, path, classes, tree))
        )
    return found


def patterns(urlconf: str) -> Iterator[tuple[str, object]]:
    """Each URL pattern reachable from the URLconf ``urlconf``, as its whole
    route and its view, in the order Django tries them. An include of a
    URLconf inside itself is not followed again; an entry that cannot be
    read is passed over; the walk ends at the bounds the module states.
    """
    from django.urls import URLPattern, URLResolver, get_resolver

    try:
        root = get_resolver(urlconf)
        first = _entries(root)
        start = (id(root.urlconf_module),)
    except Exception:  # Django raises it when it serves
        return
    # Each entry: the patterns left to read, the route so far, and the
    # URLconfs it is included from. The walk keeps its own stack, so no
    # depth of includes ends it with RecursionError.
    stack: list[tuple[Iterator[object], str, tuple[int, ...]]] = [(first, "", start)]
    for _ in range(_ENTRIES):
        found: tuple[str, object] | None = None
        while stack:
            entries, prefix, within = stack[-1]
            entry = next(entries, _END)
            if entry is not _END:
                break
            stack.pop()
        else:
            return
        try:
            if isinstance(entry, URLPattern):
                found = f"{prefix}{entry.pattern}", entry.callback
            elif isinstance(entry, URLResolver) and len(stack) <= _DEPTH:
                module = entry.urlconf_module
                if id(module) not in within:
                    stack.append(
                        (
                            _entries(entry),
                            f"{prefix}{entry.pattern}",
                            (*within, id(module)),
                        )
                    )
        except Exception:  # Django raises it when it serves
            found = None
        if found is not None:
            yield found


def _entries(resolver: "URLResolver") -> Iterator[object]:
    """The patterns of ``resolver``, read once and never more than the walk
    reads in all.
    """
    return iter(list(itertools.islice(resolver.url_patterns, _ENTRIES)))


def codes(view: object, admins: frozenset[str] | None = None) -> list[Code] | None:
    """The code a request to ``view`` runs, outermost first, read along the
    links ``label_of`` follows and ending where it ends; ``None`` for an
    admin's view that ``neolith.W001`` reports and for a walk deeper than
    ``label_of`` follows. ``admins`` holds the classes W001 names, as
    ``_reported_admins`` gives them; ``None`` reads them now.
    """
    if admins is None:
        admins = _reported_admins()
    walked: list[Code] = []
    # As label_of: one link per step, and the step past its bound has no
    # app.
    for _ in range(_serving._LINKS + 1):
        kind = type(view)
        if kind is types.MethodType:
            if admins:
                owner = _serving.admin_code(view.__self__)[1]  # type: ignore[attr-defined]
                if owner is not None and owner.removesuffix(_ADMIN_CLASS) in admins:
                    return None
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
        module = _serving._globals_name(function)
        name = f"{module}.{_serving._CODE.__get__(function).co_qualname}"
        if any(name.startswith(f"{admin}.") for admin in admins):
            return None
        walked.append(Code(module, function))
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


def _reported_admins() -> frozenset[str]:
    """``<module>.<qualname>`` of each admin class that ``neolith.W001``
    names: of an admin registered on an admin site whose code is in no
    installed app, read as ``_checks.admin_check`` reads it.
    """
    sites = sys.modules.get("django.contrib.admin.sites")
    if sites is None:
        return frozenset()
    found: set[str] = set()
    held = _naming.module_dict(sites) or {}
    for site in list(_naming.entry(held, "all_sites", ())):  # type: ignore[call-overload]
        registry = _naming.entry(_naming.instance_dict(site) or {}, "_registry")
        if type(registry) is not dict:
            continue
        for admin in list(registry.values()):
            label, code = _serving.admin_code(admin)
            if label is None and code is not None:
                found.add(code.removesuffix(_ADMIN_CLASS))
    return frozenset(found)


def _named(code: Code) -> str:
    """``<module>.<qualname>`` of a function or a class. A function is named
    by its code, which ``functools.wraps`` never changes, not by the
    ``__qualname__`` it copies onto a wrapper.
    """
    if issubclass(type(code.thing), type):
        return _naming.full_name(code.thing, None)  # type: ignore[arg-type]
    return f"{code.module}.{_serving._CODE.__get__(code.thing).co_qualname}"


def _tree(source: Source, module: str) -> ast.Module | None:
    """The tree of the first-party module ``module`` as the run read it;
    ``None`` when it has no source or cannot be read or compiled.
    """
    read = source.placed(module)
    return None if read is None else read.tree


def _line(
    thing: object,
    path: str,
    classes: dict[str, _Classes | None],
    tree: Callable[[], ast.Module | None],
) -> int:
    """The first line of a function or a class in the file ``path``; 1 when
    that file does not hold it. ``classes`` keeps each file's class
    statements, taken once for a run from ``tree``, the file's tree as the
    run read it.
    """
    if type(thing) is not types.FunctionType:
        return _class_line(thing, path, classes, tree)  # type: ignore[arg-type]
    found = _serving._CODE.__get__(thing)
    return max(found.co_firstlineno, 1) if _same(found.co_filename, path) else 1


def _class_line(
    klass: type,
    path: str,
    classes: dict[str, _Classes | None],
    tree: Callable[[], ast.Module | None],
) -> int:
    """The first line of the class ``klass`` in the file ``path``, its first
    decorator's if it has one: of the one definition there with its
    qualified name that agrees with the class, by its ``__firstlineno__``
    (Python 3.13 and later) and by the lines of the functions defined in its
    body; 1 when no definition or more than one agrees, when such a
    function was compiled from another file, or when ``tree`` gives none:
    the file cannot be read or compiled.
    """
    held = _naming.namespace(klass)
    qualname = _naming.qualname(klass, None)
    lines: list[int] = []
    for value in list(held.values()):
        if _naming.one_of(type(value), (staticmethod, classmethod)):
            value = value.__func__
        if type(value) is not types.FunctionType:
            continue
        code = _serving._CODE.__get__(value)
        if not code.co_qualname.startswith(f"{qualname}."):
            continue
        if not _same(code.co_filename, path):
            return 1
        lines.append(code.co_firstlineno)
    if path not in classes:
        classes[path] = _held(tree())
    held_classes = classes[path]
    if held_classes is None:
        return 1
    first = _naming.entry(held, "__firstlineno__")
    found = [
        (start, end)
        for start, end in held_classes.get(qualname, ())
        if (type(first) is not int or start == first)
        and all(start <= line <= end for line in lines)
    ]
    return found[0][0] if len(found) == 1 else 1


def _held(tree: ast.Module | None) -> _Classes | None:
    """The class statements of ``tree``; ``None`` when there is no tree."""
    if tree is None:
        return None
    held: _Classes = {}
    for qualname, start, end in class_statements(tree):
        held.setdefault(qualname, []).append((start, end))
    return held


def _same(file: str, path: str) -> bool:
    """Whether ``file`` names the file at ``path``."""
    try:
        return os.path.samefile(file, path)
    except (OSError, ValueError):
        return False
