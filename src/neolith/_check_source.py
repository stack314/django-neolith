"""Reading source for the check before deployment (ADR 0030 Decision 3, as
ADR 0035 narrows it).

The files of a hosted app are, for each dotted name below its package, at
any depth under the directories its ``__path__`` lists, the file Python's
path finder loads for that name, read when it is Python source and
``neolith.W104`` when it is not. A file the path finder never loads for its
name, such as ``x.py`` beside a package ``x``, is not read. A walk skips
``__pycache__``, a symbolic link to a directory, and a file or directory
whose module name holds a dot: a file's module name is its name less the
longest suffix Python's path finder loads
(``importlib.machinery.all_suffixes()``, as ``inspect.getmodulename`` reads
it). A name that is not an identifier, such as a migration's
``0001_initial``, is read, since ``import_module`` reaches it (ADR 0035).

Each file is read once per run (``Source``), parsed with ``ast.parse``,
compiled with ``compile`` and given its ``symtable``; a file that cannot be
read or compiled is ``neolith.E112``. Every walk over a tree is iterative,
so a file that compiles never ends it with ``RecursionError``. From each
file come its import edges (``imports``), the names it binds by top-level
imports alone (``bound``), its literal dynamic imports and the calls of
listed names with a computed argument, ``neolith.W101`` (``calls``).

The first-party directories are those the hosted apps' and the settings
module's top-level packages were imported from, other than the
interpreter's library directories (``first_party``); ``module_map`` places
every dotted name they give as Python's path finder would. This module
never imports a module of the project and never calls
``importlib.util.find_spec``, which runs a name's parent packages.
"""

import ast
import dataclasses
import importlib.machinery
import importlib.util
import os
import site
import stat
import symtable
import sys
import sysconfig
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final, NamedTuple

from django.apps import AppConfig
from django.core import checks

from neolith import _naming
from neolith._check import Finding, place, shown

__all__ = [
    "COMMAND_SCOPE",
    "CONNECT",
    "IMPORT_MODULE",
    "IMPORT_STRING",
    "INCLUDE",
    "LISTED",
    "Call",
    "Edge",
    "File",
    "Module",
    "Placed",
    "Source",
    "app_files",
    "bound",
    "calls",
    "canonical",
    "findings",
    "first_party",
    "imports",
    "is_test_module",
    "library_directories",
    "module_map",
    "nearest",
    "read",
    "resolved",
    "split",
]

_SOURCE: Final = tuple(importlib.machinery.SOURCE_SUFFIXES)
# The suffixes in the order the path finder tries them in one directory:
# extension modules, then source, then bytecode (_get_supported_file_loaders,
# importlib/_bootstrap_external.py).
_LOADED: Final = (
    *importlib.machinery.EXTENSION_SUFFIXES,
    *importlib.machinery.SOURCE_SUFFIXES,
    *importlib.machinery.BYTECODE_SUFFIXES,
)
# Longest first, as inspect.getmodulename tries them.
_SUFFIXES: Final = tuple(
    sorted(set(importlib.machinery.all_suffixes()), key=len, reverse=True)
)
_CACHE: Final = "__pycache__"
_INIT: Final = "__init__"
_UNREADABLE_HINT: Final = (
    "make it a file Python can import; the check judges nothing else in it"
)
_COMPUTED_HINT: Final = (
    "write it as a literal where it is fixed; the check does not judge this "
    "call otherwise"
)
_SOURCELESS_HINT: Final = (
    "keep the module's source in the app, so that the check can read what it "
    "does across the boundary"
)


def split(filename: str) -> tuple[str, str] | None:
    """The module name a file gives and the suffix that gives it, as
    ``inspect.getmodulename`` reads a file name; ``None`` when no suffix
    Python loads ends it, or when the name holds a dot or is empty, which
    no dotted name reaches (ADR 0035).
    """
    for suffix in _SUFFIXES:
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            if not stem or "." in stem:
                return None
            return stem, suffix
    return None


def _reached(name: str) -> bool:
    """Whether a directory named ``name`` is one a dotted name reaches."""
    return bool(name) and "." not in name and name != _CACHE


class File(NamedTuple):
    """One module file of an app: its path, its dotted name, the package
    its relative imports resolve against, and whether it has source.
    """

    path: str
    module: str
    package: str
    source: bool = True


def _listing(directory: str) -> list[os.DirEntry[str]]:
    with os.scandir(directory) as entries:
        return sorted(entries, key=lambda entry: entry.name)


def _is_directory(entry: os.DirEntry[str]) -> bool:
    """A directory, not a symbolic link to one."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _links_a_directory(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_symlink() and os.path.isdir(entry.path)
    except OSError:
        return False


def _loaded(directory: str, stem: str, names: frozenset[str]) -> str | None:
    """The suffix of the file the path finder loads for ``stem`` in
    ``directory``, which lists ``names``: the first suffix, in its order,
    whose file is a regular file (``FileFinder.find_spec``).
    """
    for suffix in _LOADED:
        if stem + suffix in names and os.path.isfile(
            os.path.join(directory, stem + suffix)
        ):
            return suffix
    return None


def _init_file(directory: str) -> str | None:
    """The ``__init__`` file the path finder loads for the package
    ``directory``, or ``None`` when it holds none.
    """
    for suffix in _LOADED:
        path = os.path.join(directory, _INIT + suffix)
        if os.path.isfile(path):
            return path
    return None


# What one location gives a name, as FileFinder.find_spec reads it: a regular
# package, a module, a namespace portion; a regular package reached through
# a symbolic link, which wins the name but is not followed (a blind spot);
# or only a source file the path finder cannot load, such as a broken link.
_PACKAGE: Final = "package"
_MODULE: Final = "module"
_NAMESPACE: Final = "namespace"
_LINKED: Final = "linked"
_UNLOADABLE: Final = "unloadable"


class _Found(NamedTuple):
    kind: str
    file: str | None = None
    holds: str = ""
    # False for a namespace portion reached through a symbolic link: its
    # names are looked up, as Python does, but its files are not read.
    walked: bool = True


def _scan(
    directory: str, inner: frozenset[str] = frozenset(), *, venvs: bool = False
) -> dict[str, _Found]:
    """What each name ``directory`` gives is, as the path finder reads it:
    a regular package before a module of its name and a module before a
    namespace portion (``FileFinder.find_spec``). A symbolic link to a
    directory is never walked: a package there wins its name as a blind
    spot, and a namespace portion there is only probed. Skipped: what no
    dotted name reaches, the first-party directories ``inner``, each of
    which is named from itself, and, when ``venvs``, virtual environments.
    An ``OSError`` listing ``directory`` is raised.
    """
    entries = _listing(directory)
    names = frozenset(entry.name for entry in entries)
    folders: dict[str, os.DirEntry[str]] = {}
    stems: set[str] = set()
    for entry in entries:
        if _is_directory(entry) or _links_a_directory(entry):
            if _reached(entry.name):
                folders[entry.name] = entry
            continue
        parts = split(entry.name)
        if parts is not None and parts[0] != _INIT:
            stems.add(parts[0])
    found: dict[str, _Found] = {}
    for name in sorted(folders.keys() | stems):
        folder = folders.get(name)
        if folder is not None and (
            os.path.realpath(folder.path) in inner
            or (venvs and os.path.lexists(os.path.join(folder.path, "pyvenv.cfg")))
        ):
            folder = None
        linked = folder is not None and not _is_directory(folder)
        if folder is not None:
            init = _init_file(folder.path)
            if init is not None:
                found[name] = _Found(_LINKED if linked else _PACKAGE, init, folder.path)
                continue
        loaded = _loaded(directory, name, names) if name in stems else None
        if loaded is not None:
            found[name] = _Found(_MODULE, os.path.join(directory, name + loaded))
        elif folder is not None:
            found[name] = _Found(_NAMESPACE, None, folder.path, walked=not linked)
        else:
            unloadable = [name + suffix for suffix in _SOURCE if name + suffix in names]
            if unloadable:
                file = os.path.join(directory, unloadable[0])
                found[name] = _Found(_UNLOADABLE, file)
    return found


def _probe(directory: str, name: str) -> _Found | None:
    """What ``name`` is in a directory the check does not walk, such as a
    library directory, by the path finder's own tests on that one name.
    """
    base = os.path.join(directory, name)
    folder = os.path.isdir(base)
    init = _init_file(base) if folder else None
    if init is not None:
        return _Found(_PACKAGE, init, base)
    for suffix in _LOADED:
        if os.path.isfile(base + suffix):
            return _Found(_MODULE, base + suffix)
    return _Found(_NAMESPACE, None, base) if folder else None


# A location to look a name up in, with the first-party directory it is
# named from; ``None`` for a directory the check does not walk.
_Location = tuple[str, str | None]


class _Chosen(NamedTuple):
    kind: str
    file: str | None
    below: list[_Location]
    root: str | None


def _choose(
    locations: Sequence[_Location], scanned: Sequence[dict[str, _Found] | None]
) -> dict[str, _Chosen]:
    """For each name a walked location gives, what Python's path finder
    takes for it across ``locations``, in their order: the first regular
    package or module wins, and namespace portions join only while none
    does (``PathFinder._get_spec``). ``scanned`` holds each walked
    location's scan, and ``None`` for one only probed.
    """
    candidates = sorted({name for scan in scanned if scan for name in scan})
    chosen: dict[str, _Chosen] = {}
    for name in candidates:
        portions: list[_Location] = []
        unloadable: _Chosen | None = None
        result: _Chosen | None = None
        for (directory, root), scan in zip(locations, scanned, strict=True):
            found = scan.get(name) if scan is not None else _probe(directory, name)
            if found is None:
                continue
            if found.kind in (_PACKAGE, _MODULE, _LINKED):
                below = [(found.holds, root)] if found.kind == _PACKAGE else []
                result = _Chosen(found.kind, found.file, below, root)
                break
            if found.kind == _NAMESPACE:
                portions.append((found.holds, root if found.walked else None))
            elif unloadable is None:
                unloadable = _Chosen(found.kind, found.file, [], root)
        if result is None and portions:
            roots = [root for _, root in portions if root is not None]
            result = _Chosen(_NAMESPACE, None, portions, roots[0] if roots else None)
        if result is None:
            result = unloadable
        if result is not None:
            chosen[name] = result
    return chosen


def app_files(
    name: str, directories: Sequence[str]
) -> tuple[list[File], list[tuple[str, OSError]]]:
    """The module files of the package ``name`` whose ``__path__`` lists
    ``directories``, at any depth, and each directory that could not be
    listed, with its error.

    Each dotted name below the package is given the file Python's path
    finder loads for it (``source`` false when that file has no Python
    source), or, when it loads none, the name's source file, which cannot
    be read (``neolith.E112``). A file the path finder never loads for its
    name, such as ``x.py`` beside a package ``x``, is not listed, nor is
    anything under a symbolic link to a directory.
    """
    files: list[File] = []
    failed: list[tuple[str, OSError]] = []
    for directory in directories:
        init = _init_file(directory)
        if init is not None:
            files.append(File(init, name, name, init.endswith(_SOURCE)))
            break
    stack: list[tuple[str, list[_Location]]] = [
        (name, [(directory, directory) for directory in directories])
    ]
    while stack:
        prefix, locations = stack.pop()
        scanned: list[dict[str, _Found] | None] = []
        for directory, root in locations:
            if root is None:
                scanned.append(None)
                continue
            try:
                scanned.append(_scan(directory))
            except OSError as error:
                failed.append((directory, error))
                scanned.append({})
        below: list[tuple[str, list[_Location]]] = []
        for stem, chosen in _choose(locations, scanned).items():
            module = f"{prefix}.{stem}"
            if chosen.file is not None and chosen.kind != _LINKED:
                # A package's relative imports start from itself; a
                # module's from the package it is in.
                package = module if chosen.kind == _PACKAGE else prefix
                source = chosen.file.endswith(_SOURCE)
                files.append(File(chosen.file, module, package, source))
            if chosen.kind in (_PACKAGE, _NAMESPACE):
                below.append((module, chosen.below))
        stack.extend(reversed(below))
    return files, failed


@dataclasses.dataclass(slots=True)
class Module:
    """A file as the check reads it: its tree and symbol table, or the
    reason it cannot be read or compiled, with the line it names.
    """

    file: File
    tree: ast.Module | None = None
    table: symtable.SymbolTable | None = None
    problem: str | None = None
    line: int = 1


def _bytes(path: str) -> bytes:
    """The bytes of the regular file at ``path``; anything else, such as a
    pipe, is refused without waiting on it.
    """
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path} is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _cause(error: BaseException) -> str:
    """An error as a finding names it: a ``SyntaxError`` by its message
    alone, since its text repeats the path and line the finding prints.
    """
    if isinstance(error, SyntaxError) and type(error.msg) is str:
        return f"{_naming.type_name(error)}: {error.msg}"
    return _naming.cause(error)


def read(file: File) -> Module:
    """Read, parse and compile ``file``, and build its symbol table.

    Any ``OSError`` while reading, and any exception that ``ast.parse``,
    ``compile`` or ``symtable`` raises, is kept as the module's problem.
    Warnings the compiler gives, such as ``SyntaxWarning``, are not errors
    here, as they are not when Python imports the file.
    """
    module = Module(file)
    try:
        data = _bytes(file.path)
    except OSError as error:
        module.problem = f"cannot be read: {_cause(error)}"
        return module
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(data, filename=file.path)
            compile(tree, file.path, "exec", dont_inherit=True)
            table = symtable.symtable(data, file.path, "exec")  # type: ignore[arg-type]
    except Exception as error:
        module.problem = f"cannot be compiled: {_cause(error)}"
        line = getattr(error, "lineno", None)
        module.line = line if type(line) is int and line > 0 else 1
        return module
    module.tree = tree
    module.table = table
    return module


class Edge(NamedTuple):
    """An import of the module ``target`` at ``line``; ``dynamic`` when a
    literal of a call names it rather than a statement.
    """

    target: str
    line: int
    dynamic: bool = False


def _iterate(node: ast.AST) -> Iterator[ast.AST]:
    """Every node under ``node``, ``node`` included, without recursion."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _relative(name: str, level: int, package: str) -> str | None:
    """A ``from`` statement's module, resolved as Python resolves it
    (``importlib.util.resolve_name``); ``None`` when Python refuses it, such
    as a relative name past the top-level package.
    """
    if not level:
        return name
    try:
        return importlib.util.resolve_name("." * level + name, package)
    except (ImportError, ValueError):
        return None


def nearest(name: str, known: Callable[[str], bool]) -> str:
    """``name``, or its nearest package that ``known`` holds, however many
    levels are missing; ``name`` itself when no part of it is known, which
    is a module the check cannot place.
    """
    candidate = name
    while True:
        if known(candidate):
            return candidate
        candidate, dot, _ = candidate.rpartition(".")
        if not dot:
            return name


def imports(tree: ast.Module, package: str, known: Callable[[str], bool]) -> list[Edge]:
    """Every ``import`` and ``from ... import`` statement in the file,
    wherever it stands, resolved as ADR 0030 Decision 3 says.

    ``import a.b`` and ``import a.b as x`` are imports of ``a.b``; ``from m
    import n`` of ``m.n`` when ``known`` holds it and of ``m`` otherwise; a
    star import of ``m``. A name with no file is its nearest known package.
    A relative name Python would refuse gives no edge.
    """
    edges: list[Edge] = []
    for node in _iterate(tree):
        if isinstance(node, ast.Import):
            edges += [
                Edge(nearest(alias.name, known), node.lineno) for alias in node.names
            ]
        elif isinstance(node, ast.ImportFrom):
            base = _relative(node.module or "", node.level, package)
            if not base:
                continue
            for alias in node.names:
                below = f"{base}.{alias.name}"
                if alias.name != "*" and known(below):
                    edges.append(Edge(below, node.lineno))
                else:
                    edges.append(Edge(nearest(base, known), node.lineno))
    return edges


# The listed names (ADR 0030 Decision 3): each object, named by the module
# that defines it, with every module of Django and of neolith whose __all__
# lists it, bound to the same object. tests/test_check_listed.py pins the
# set with a walk of both, on each supported Django version.
_TRANSACTION: Final = (
    "atomic",
    "clean_savepoints",
    "commit",
    "get_autocommit",
    "get_connection",
    "get_rollback",
    "mark_for_rollback_on_error",
    "non_atomic_requests",
    "on_commit",
    "rollback",
    "savepoint",
    "savepoint_commit",
    # Django 6.1 adds it; on 5.2 a name of it reaches nothing.
    "savepoint_create",
    "savepoint_rollback",
    "set_autocommit",
    "set_rollback",
)
LISTED: Final[Mapping[str, frozenset[str]]] = {
    "neolith._host.connect": frozenset({"neolith.connect"}),
    "neolith._host.command_scope": frozenset({"neolith.command_scope"}),
    "neolith.testing.boot": frozenset(),
    "django.contrib.auth.get_user_model": frozenset(),
    "importlib.import_module": frozenset(),
    "django.utils.module_loading.import_string": frozenset(),
    "django.urls.conf.include": frozenset(
        {"django.urls.include", "django.conf.urls.include"}
    ),
    "django.db.connection": frozenset(),
    "django.db.connections": frozenset(),
    "django.db.utils.DEFAULT_DB_ALIAS": frozenset({"django.db.DEFAULT_DB_ALIAS"}),
    **{f"django.db.transaction.{name}": frozenset() for name in _TRANSACTION},
}
_CANONICAL: Final[Mapping[str, str]] = {
    name: canonical
    for canonical, others in LISTED.items()
    for name in (canonical, *others)
}
CONNECT: Final = "neolith._host.connect"
COMMAND_SCOPE: Final = "neolith._host.command_scope"
IMPORT_MODULE: Final = "importlib.import_module"
IMPORT_STRING: Final = "django.utils.module_loading.import_string"
INCLUDE: Final = "django.urls.conf.include"
_IMPORT: Final = "__import__"


def canonical(name: str) -> str:
    """The listed object ``name`` stands for, by its defining module; any
    other name as it is.
    """
    return _CANONICAL.get(name, name)


def _blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    """The blocks of a compound statement that stay in its scope: those of
    ``if``, ``try``, ``with``, ``for``, ``while`` and ``match``; never a
    function's or a class's body.
    """
    if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        return [statement.body, statement.orelse]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return [statement.body]
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return [
            statement.body,
            *(handler.body for handler in statement.handlers),
            statement.orelse,
            statement.finalbody,
        ]
    if isinstance(statement, ast.Match):
        return [case.body for case in statement.cases]
    return []


def _top_level(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    """Each import statement in the module's own scope, at any depth of its
    compound statements.
    """
    stack = list(reversed(tree.body))
    while stack:
        statement = stack.pop()
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            yield statement
        for block in reversed(_blocks(statement)):
            stack.extend(reversed(block))


def _stars(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
        for node in _iterate(tree)
    )


def _tables(table: symtable.SymbolTable) -> Iterator[symtable.SymbolTable]:
    stack = [table]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.get_children())


def _bound_otherwise(tree: ast.Module, table: symtable.SymbolTable) -> set[str]:
    """Every name something other than a top-level import can bind: a
    table of any scope marks it assigned, a parameter, declared global or
    nonlocal, or imported in a scope other than the module's; or it is a
    comprehension's target, which Python 3.12 folds into its scope.
    """
    names: set[str] = set()
    for current in _tables(table):
        module = current is table
        for symbol in current.get_symbols():
            if (
                symbol.is_assigned()
                or symbol.is_parameter()
                or symbol.is_declared_global()
                or symbol.is_nonlocal()
                or (symbol.is_imported() and not module)
            ):
                names.add(symbol.get_name())
    for node in _iterate(tree):
        if isinstance(node, ast.comprehension):
            names.update(
                target.id
                for target in _iterate(node.target)
                if isinstance(target, ast.Name)
            )
    return names


def _binds(
    node: ast.Import | ast.ImportFrom, package: str
) -> Iterator[tuple[str, str | None]]:
    """The names an import statement binds, each with the dotted name it
    binds it to, or ``None`` when Python would refuse the statement.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname is None:
                head = alias.name.partition(".")[0]
                yield head, head
            else:
                yield alias.asname, alias.name
        return
    base = _relative(node.module or "", node.level, package)
    for alias in node.names:
        if alias.name != "*":
            yield alias.asname or alias.name, f"{base}.{alias.name}" if base else None


def bound(
    tree: ast.Module, table: symtable.SymbolTable, package: str
) -> dict[str, str]:
    """The names the file binds by top-level imports alone, each with the
    dotted name it stands for (ADR 0030 Decision 3, *How a name is
    resolved*).

    A name is followed when every top-level import of it gives one dotted
    name, or names that stand for one listed object, and nothing else in
    the file can bind it. No name is followed in a file with a star import.
    """
    if _stars(tree):
        return {}
    found: dict[str, set[str | None]] = {}
    for node in _top_level(tree):
        for name, dotted in _binds(node, package):
            found.setdefault(name, set()).add(dotted)
    otherwise = _bound_otherwise(tree, table)
    followed: dict[str, str] = {}
    for name, targets in found.items():
        if name in otherwise or None in targets:
            continue
        names = {each for each in targets if each is not None}
        objects = {canonical(each) for each in names}
        if len(names) == 1:
            followed[name] = names.pop()
        elif len(objects) == 1 and next(iter(objects)) in LISTED:
            followed[name] = objects.pop()
    return followed


def resolved(node: ast.expr, followed: Mapping[str, str]) -> str | None:
    """The dotted name an expression stands for: a followed name, or an
    attribute chain on one, which extends its dotted name.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in followed:
        return None
    return ".".join([followed[node.id], *reversed(parts)])


def _builtin_import(tree: ast.Module, table: symtable.SymbolTable) -> bool:
    """Whether ``__import__`` in the file is the builtin: nothing in it
    binds that name, an import at module level included, and it has no
    star import.
    """
    if _stars(tree) or _IMPORT in _bound_otherwise(tree, table):
        return False
    try:
        return not table.lookup(_IMPORT).is_imported()
    except KeyError:
        return True


class _Computed:
    """An argument the check cannot read: a starred or ``**`` argument in
    front of it.
    """


_COMPUTED: Final = _Computed()


def _argument(
    call: ast.Call, index: int | None, keyword: str | None
) -> ast.expr | _Computed | None:
    """The argument of ``call`` at positional ``index`` or named
    ``keyword``; ``None`` when it is not given.
    """
    if index is not None:
        for position, argument in enumerate(call.args):
            if isinstance(argument, ast.Starred):
                return _COMPUTED
            if position == index:
                return argument
    for given in call.keywords:
        if given.arg is None:
            return _COMPUTED
        if given.arg == keyword:
            return given.value
    return None


def _computed(argument: ast.expr | _Computed | None) -> bool:
    return argument is not None and not isinstance(argument, ast.Constant)


def _text(argument: ast.expr | _Computed | None) -> str | None:
    if isinstance(argument, ast.Constant) and type(argument.value) is str:
        return argument.value
    return None


class Call(NamedTuple):
    """A call of a listed name the check cannot read: ``name`` is called
    with a computed ``what`` at ``line`` (``neolith.W101``).
    """

    name: str
    what: str
    line: int


# Each listed callee W101 names, by the object it resolves to, with the
# place of the argument that names what it reaches.
_NAMED: Final[Mapping[str, tuple[str, int, str]]] = {
    CONNECT: ("connect", 0, ""),
    COMMAND_SCOPE: ("command_scope", 0, "app"),
    IMPORT_STRING: ("import_string", 0, "dotted_path"),
}
_BY_ATTRIBUTE: Final = {
    "get_model": (("app_label", 0), ("model_name", 1)),
    "get_app_config": (("app_label", 0),),
}


def calls(
    tree: ast.Module,
    table: symtable.SymbolTable,
    package: str,
    known: Callable[[str], bool],
    followed: Mapping[str, str] | None = None,
) -> tuple[list[Edge], list[Call]]:
    """The literal dynamic imports in the file, as edges, and each call of
    a listed name with a computed argument (ADR 0030 Decision 3, *Literal
    dynamic imports*, and ``neolith.W101``).
    """
    if followed is None:
        followed = bound(tree, table, package)
    builtin = _builtin_import(tree, table)
    edges: list[Edge] = []
    unread: list[Call] = []
    for node in _iterate(tree):
        if not isinstance(node, ast.Call):
            continue
        line = node.lineno
        callee = resolved(node.func, followed)
        target = canonical(callee) if callee is not None else None
        if target == IMPORT_MODULE:
            name = _argument(node, 0, "name")
            base = _argument(node, 1, "package")
            if _computed(name):
                unread.append(Call("import_module", "argument", line))
            if _computed(base) and not (
                isinstance(base, ast.Name) and base.id == "__package__"
            ):
                unread.append(Call("import_module", "package", line))
            text = _text(name)
            if text is None:
                continue
            if text.startswith("."):
                if isinstance(base, ast.Name) and base.id == "__package__":
                    start: str | None = package
                else:
                    start = _text(base)
                try:
                    text = importlib.util.resolve_name(text, start)
                except (ImportError, ValueError, TypeError, AttributeError):
                    continue
            if text:
                edges.append(Edge(nearest(text, known), line, dynamic=True))
        elif builtin and isinstance(node.func, ast.Name) and node.func.id == _IMPORT:
            found, readable = _dunder_import(node, package, known)
            edges += found
            if not readable:
                unread.append(Call(_IMPORT, "argument", line))
        elif target is not None and target in _NAMED:
            short, index, keyword = _NAMED[target]
            argument = _argument(node, index, keyword or None)
            if _computed(argument):
                unread.append(Call(short, "argument", line))
            text = _text(argument)
            if target == IMPORT_STRING and text and "." in text:
                edges.append(
                    Edge(nearest(text.rpartition(".")[0], known), line, dynamic=True)
                )
        elif target == INCLUDE:
            argument = _argument(node, 0, "arg")
            if isinstance(argument, ast.Tuple) and len(argument.elts) == 2:
                argument = argument.elts[0]
            text = _text(argument)
            if text:
                edges.append(Edge(nearest(text, known), line, dynamic=True))
        elif _unread_by_attribute(node):
            unread.append(Call(node.func.attr, "argument", line))  # type: ignore[attr-defined]
    return edges, unread


def _from_names(argument: ast.expr | _Computed | None) -> list[str] | None:
    """The names of a literal ``fromlist``: a list or tuple of strings, or
    ``None``; ``None`` for anything else.
    """
    if argument is None or (
        isinstance(argument, ast.Constant) and argument.value is None
    ):
        return []
    if isinstance(argument, (ast.List, ast.Tuple)) and all(
        isinstance(item, ast.Constant) and type(item.value) is str
        for item in argument.elts
    ):
        return [item.value for item in argument.elts]  # type: ignore[attr-defined]
    return None


def _globals_call(argument: ast.expr | _Computed | None) -> bool:
    return (
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Name)
        and argument.func.id == "globals"
        and not argument.args
        and not argument.keywords
    )


def _dunder_import(
    call: ast.Call, package: str, known: Callable[[str], bool]
) -> tuple[list[Edge], bool]:
    """The edges of a call of the builtin ``__import__(name, globals,
    locals, fromlist, level)``, read as the ``import`` or ``from ... import``
    statement it performs, and whether the check could read it: a computed
    name, ``fromlist`` or ``level``, or a relative level with ``globals``
    other than ``globals()``, cannot be read.
    """
    line = call.lineno
    name = _argument(call, 0, "name")
    scope = _argument(call, 1, "globals")
    level = _argument(call, 4, "level")
    names = _from_names(_argument(call, 3, "fromlist"))
    if _computed(name) or names is None or _computed(level):
        return [], False
    text = _text(name)
    depth = level.value if isinstance(level, ast.Constant) else 0
    if text is None or type(depth) is not int or depth < 0:
        return [], True
    if depth:
        if not _globals_call(scope):
            return [], scope is None or (
                isinstance(scope, ast.Constant) and scope.value is None
            )
        base = _relative(text, depth, package)
    else:
        base = text or None
    if not base:
        return [], True
    if not names:
        return [Edge(nearest(base, known), line, dynamic=True)], True
    edges = []
    for each in names:
        below = f"{base}.{each}"
        target = below if each != "*" and known(below) else nearest(base, known)
        edges.append(Edge(target, line, dynamic=True))
    return edges, True


def _unread_by_attribute(call: ast.Call) -> bool:
    """A call of an attribute named ``get_model`` or ``get_app_config``,
    whatever its receiver, with a computed label or model name.
    """
    function = call.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr in _BY_ATTRIBUTE
        and any(
            _computed(_argument(call, index, keyword))
            for keyword, index in _BY_ATTRIBUTE[function.attr]
        )
    )


def library_directories() -> list[str]:
    """The interpreter's library directories: those ``sysconfig`` gives for
    ``stdlib``, ``platstdlib``, ``purelib`` and ``platlib``, and those
    ``site`` reports, the user site included.
    """
    paths = sysconfig.get_paths()
    found = [
        paths[key]
        for key in ("stdlib", "platstdlib", "purelib", "platlib")
        if key in paths
    ]
    getsitepackages = getattr(site, "getsitepackages", None)
    if callable(getsitepackages):
        found += getsitepackages()
    found.append(site.getusersitepackages())
    return found


def _under(path: str, directories: Sequence[str]) -> bool:
    """Whether ``path`` is one of ``directories`` or lies under one, each
    compared by its real path.
    """
    real = os.path.realpath(path)
    for directory in directories:
        other = os.path.realpath(directory)
        try:
            if os.path.commonpath([real, other]) == other:
                return True
        except ValueError:
            continue
    return False


def _imported_from(module: object) -> list[str]:
    """The directories a top-level module or package was imported from,
    read from its spec: the parent of each of a package's directories, or
    of a module's file.
    """
    spec = getattr(module, "__spec__", None)
    locations = getattr(spec, "submodule_search_locations", None)
    if locations is not None:
        try:
            return [os.path.dirname(entry) for entry in locations if type(entry) is str]
        except TypeError:
            return []
    origin = getattr(spec, "origin", None)
    if type(origin) is str and os.path.isabs(origin):
        return [os.path.dirname(origin)]
    return []


def first_party(
    tops: Sequence[str],
    modules: Mapping[str, object],
    path: Sequence[str],
    libraries: Sequence[str],
) -> list[str]:
    """The first-party directories: each directory one of the top-level
    modules ``tops``, read from ``modules`` (``sys.modules``), was imported
    from, unless it lies under one of ``libraries``, in the order of
    ``path`` (``sys.path``) and then in the order found.
    """
    found: list[str] = []
    for top in tops:
        module = modules.get(top)
        for directory in _imported_from(module):
            if directory not in found and not _under(directory, libraries):
                found.append(directory)
    order = [os.path.realpath(entry or os.curdir) for entry in path]

    def place_of(directory: str) -> int:
        real = os.path.realpath(directory)
        return order.index(real) if real in order else len(order)

    return sorted(found, key=place_of)


class Placed(NamedTuple):
    """Where a first-party dotted name is: the file Python's path finder
    loads for it and whether it has source, or, for a namespace package,
    its portions; and the first-party directory it is named from.
    """

    path: str | None
    root: str
    source: bool = True
    portions: tuple[str, ...] = ()

    def parts(self) -> list[str]:
        """The file's path below its first-party directory, by part."""
        if self.path is None:
            return []
        return os.path.relpath(self.path, self.root).split(os.sep)


def _taken_elsewhere(name: str) -> bool:
    """Whether Python imports the top-level ``name`` before the path finder
    looks: a built-in or frozen module (``sys.meta_path``'s first finders).
    """
    return (
        name in sys.builtin_module_names
        or importlib.machinery.FrozenImporter.find_spec(name) is not None
    )


def _ordered(directories: Sequence[str], path: Sequence[str]) -> list[_Location]:
    """Every location of ``path`` (``sys.path``) in its order, each named
    from itself when it is one of the first-party ``directories``, then the
    first-party directories ``path`` does not hold.
    """
    first = {os.path.realpath(directory): directory for directory in directories}
    locations: list[_Location] = []
    seen: set[str] = set()
    for entry in path:
        given = entry or os.getcwd()
        real = os.path.realpath(given)
        if real in seen:
            continue
        seen.add(real)
        directory = first.get(real)
        locations.append((given, None) if directory is None else (directory, directory))
    locations += [
        (directory, directory) for real, directory in first.items() if real not in seen
    ]
    return locations


def module_map(
    directories: Sequence[str], path: Sequence[str] | None = None
) -> dict[str, Placed]:
    """Every dotted name the first-party ``directories`` give, mapped to
    where it is, without running code (ADR 0030 Decision 3, *How a dotted
    name is placed*).

    Python's path finder decides across locations: the first that holds a
    regular package or a module wins, and namespace portions join only
    while none does (``PathFinder._get_spec``). The locations are those of
    ``path`` (``sys.path``) in its order, the first-party directories
    walked and every other one probed name by name, or, when ``path`` is
    not given, the first-party directories in their order. A name another
    location wins, or a built-in or frozen module takes, is not
    first-party; nor is a package reached through a symbolic link, which
    is a blind spot. Where one directory lies inside another, each file is
    named from the innermost.
    """
    inner = frozenset(os.path.realpath(directory) for directory in directories)
    top: list[_Location] = (
        [(directory, directory) for directory in directories]
        if path is None
        else _ordered(directories, path)
    )
    placed: dict[str, Placed] = {}
    queue: list[tuple[str, list[_Location]]] = [("", top)]
    while queue:
        prefix, locations = queue.pop()
        scanned: list[dict[str, _Found] | None] = []
        for directory, root in locations:
            if root is None:
                scanned.append(None)
                continue
            try:
                scanned.append(_scan(directory, inner, venvs=True))
            except OSError:
                scanned.append({})
        for name, chosen in _choose(locations, scanned).items():
            if (
                chosen.root is None
                or chosen.kind in (_LINKED, _UNLOADABLE)
                or (not prefix and _taken_elsewhere(name))
            ):
                continue
            dotted = f"{prefix}{name}"
            source = chosen.file is None or chosen.file.endswith(_SOURCE)
            portions = (
                tuple(holds for holds, _ in chosen.below)
                if chosen.kind == _NAMESPACE
                else ()
            )
            placed[dotted] = Placed(chosen.file, chosen.root, source, portions)
            if chosen.below:
                queue.append((f"{dotted}.", chosen.below))
    return placed


def is_test_module(parts: Sequence[str]) -> bool:
    """Whether a file, by its path's parts below its first-party directory,
    is a test module: ``conftest.py``, ``tests.py``, ``test_*.py``,
    ``*_test.py``, or any file under a directory named ``tests``.
    """
    if not parts:
        return False
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or name in ("conftest.py", "tests.py")
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


class Source:
    """What one run of the check reads, each file once: the files of every
    hosted app, as ``owner`` attributes modules to installed apps, and the
    map of first-party dotted names to files.
    """

    def __init__(
        self,
        hosted: Mapping[str, AppConfig],
        owner: Callable[[str], str | None],
        *,
        settings_module: str | None = None,
    ) -> None:
        """Read the hosted apps ``hosted``, by label; ``owner`` gives the
        label of the installed app that holds a dotted name, or ``None``.
        The first-party directories are those the hosted apps and
        ``settings_module`` were imported from.
        """
        self.hosted = dict(hosted)
        self._owner = owner
        self._settings = settings_module
        self._modules: dict[str, Module] = {}
        self._files: dict[str, list[File]] | None = None
        self._failed: dict[str, list[tuple[str, OSError]]] = {}
        self._directories: list[str] | None = None
        self._map: dict[str, Placed] | None = None
        self._loaded: frozenset[str] = frozenset()
        self._bound: dict[str, dict[str, str]] = {}
        self._calls: dict[str, tuple[list[Edge], list[Call]]] = {}

    def files(self) -> dict[str, list[File]]:
        """The files of each hosted app, by label. A nested app's files are
        its own, and a file of an installed app that is not hosted, nested
        in a hosted one, is no hosted app's.
        """
        if self._files is not None:
            return self._files
        found: dict[str, list[File]] = {label: [] for label in self.hosted}
        listed: set[str] = set()
        for label, config in self.hosted.items():
            listing, failed = app_files(config.name, _directories(config))
            self._failed[label] = failed
            for file in listing:
                owner = self._owner(file.module)
                if file.path in listed or owner not in found:
                    continue
                listed.add(file.path)
                found[owner].append(file)
        self._files = found
        return found

    def failed(self) -> dict[str, list[tuple[str, OSError]]]:
        """Each hosted app's directories that could not be listed."""
        self.files()
        return self._failed

    def read(self, file: File) -> Module:
        """``file`` as read once in this run."""
        module = self._modules.get(file.path)
        if module is None:
            module = self._modules[file.path] = read(file)
        return module

    def modules(self, label: str) -> Iterator[Module]:
        """The read source files of the hosted app ``label``."""
        for file in self.files()[label]:
            if file.source:
                yield self.read(file)

    def first_party(self) -> list[str]:
        """The first-party directories of this host."""
        if self._directories is None:
            tops = [config.name.partition(".")[0] for config in self.hosted.values()]
            if self._settings:
                tops.append(self._settings.partition(".")[0])
            self._directories = first_party(
                list(dict.fromkeys(tops)),
                dict(sys.modules),
                list(sys.path),
                library_directories(),
            )
        return self._directories

    def map(self) -> dict[str, Placed]:
        """The first-party dotted names, each with where it is."""
        if self._map is None:
            self._map = module_map(self.first_party(), list(sys.path))
            self._loaded = frozenset(sys.modules)
        return self._map

    def known(self, name: str) -> bool:
        """Whether ``name`` is a module the check knows of: a first-party
        name the map holds, or a module loaded in this process, such as a
        library module in a namespace package that first-party code shares.
        """
        return name in self.map() or name in self._loaded

    def bound(self, module: Module) -> dict[str, str]:
        """The names ``module`` binds by top-level imports alone."""
        if module.tree is None or module.table is None:
            return {}
        found = self._bound.get(module.file.path)
        if found is None:
            found = self._bound[module.file.path] = bound(
                module.tree, module.table, module.file.package
            )
        return found

    def edges(self, module: Module) -> list[Edge]:
        """Every import of ``module``: its statements and its literal
        dynamic imports.
        """
        if module.tree is None:
            return []
        return [
            *imports(module.tree, module.file.package, self.known),
            *self._read_calls(module)[0],
        ]

    def unread(self, module: Module) -> list[Call]:
        """The calls of listed names in ``module`` with a computed
        argument.
        """
        return self._read_calls(module)[1]

    def _read_calls(self, module: Module) -> tuple[list[Edge], list[Call]]:
        if module.tree is None or module.table is None:
            return [], []
        found = self._calls.get(module.file.path)
        if found is None:
            found = self._calls[module.file.path] = calls(
                module.tree,
                module.table,
                module.file.package,
                self.known,
                self.bound(module),
            )
        return found


def _directories(config: AppConfig) -> list[str]:
    """The directories of an app's package, as its ``__path__`` lists
    them, or the directory Django gives the app when it has none.
    """
    path = getattr(config.module, "__path__", None)
    try:
        listed = [entry for entry in path if type(entry) is str]  # type: ignore[union-attr]
    except TypeError:
        listed = []
    return listed or [config.path]


def findings(source: Source) -> list[Finding]:
    """``neolith.E112``, ``neolith.W104`` and ``neolith.W101`` for the hosted
    apps' files.
    """
    found: list[Finding] = []
    message: checks.CheckMessage
    for label, failed in source.failed().items():
        for directory, error in failed:
            message = checks.Error(
                f"app {shown(label)}: directory {place(directory)}: "
                f"cannot be read: {shown(_cause(error))}",
                hint=_UNREADABLE_HINT,
                id="neolith.E112",
            )
            found.append(Finding(message, label, place(directory), 1))
    for label, files in source.files().items():
        for file in files:
            if not file.source:
                message = checks.Warning(
                    f"app {shown(label)}: module {shown(file.module)}: has no "
                    "Python source, so the check cannot read it",
                    hint=_SOURCELESS_HINT,
                    id="neolith.W104",
                )
                found.append(Finding(message, label, place(file.path), 1))
                continue
            module = source.read(file)
            if module.problem is not None:
                message = checks.Error(
                    f"app {shown(label)}: module {shown(file.module)}: "
                    f"{shown(module.problem)}",
                    hint=_UNREADABLE_HINT,
                    id="neolith.E112",
                )
                found.append(Finding(message, label, place(file.path), module.line))
                continue
            for call in source.unread(module):
                message = checks.Warning(
                    f"app {shown(label)}: module {shown(file.module)}: "
                    f"{call.name}() is called with a computed {call.what}, which "
                    "the check cannot read",
                    hint=_COMPUTED_HINT,
                    id="neolith.W101",
                )
                found.append(Finding(message, label, place(file.path), call.line))
    return found
