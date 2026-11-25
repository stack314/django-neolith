"""Reading source for the check before deployment (ADR 0030 Decision 3, as
ADR 0035 narrows it).

The files of a hosted app are every Python source file under the
directories its package's ``__path__`` lists, at any depth, and every module
file without source there (``neolith.W104``). A walk skips ``__pycache__``,
a symbolic link to a directory, and a file or directory whose module name
holds a dot: a file's module name is its name less the longest suffix
Python's path finder loads (``importlib.machinery.all_suffixes()``, as
``inspect.getmodulename`` reads it). A name that is not an identifier, such
as a migration's ``0001_initial``, is read, since ``import_module`` reaches
it (ADR 0035).

Each file is read once per run, parsed with ``ast.parse``, compiled with
``compile`` and given its ``symtable``; a file that cannot be read or
compiled is ``neolith.E112``. Every walk over a tree is iterative, so a file
that compiles never ends it with ``RecursionError``.

This module never imports a module of the project and never calls
``importlib.util.find_spec``, which runs a name's parent packages.
"""

import ast
import dataclasses
import importlib.machinery
import os
import stat
import symtable
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

from django.apps import AppConfig
from django.core import checks

from neolith import _naming
from neolith._check import Finding, place, shown

__all__ = [
    "File",
    "Module",
    "Source",
    "app_files",
    "findings",
    "read",
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


@dataclasses.dataclass(frozen=True, slots=True)
class File:
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


def app_files(
    name: str, directories: Sequence[str]
) -> tuple[list[File], list[tuple[str, OSError]]]:
    """The module files of the package ``name`` whose ``__path__`` lists
    ``directories``, at any depth, and each directory that could not be
    listed, with its error.

    Every source file is listed, and, for each module name whose file the
    path finder loads has no source, that file, with ``source`` false.
    """
    files: list[File] = []
    failed: list[tuple[str, OSError]] = []
    stack = [(directory, name) for directory in reversed(directories)]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = _listing(directory)
        except OSError as error:
            failed.append((directory, error))
            continue
        names = frozenset(entry.name for entry in entries)
        below: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in entries:
            if _is_directory(entry):
                if _reached(entry.name):
                    below.append((entry.path, f"{prefix}.{entry.name}"))
                continue
            if _links_a_directory(entry):
                continue
            parts = split(entry.name)
            if parts is None:
                continue
            stem, suffix = parts
            # __init__ names its package, which its relative imports start
            # from; any other module's start from the package it is in.
            module = prefix if stem == _INIT else f"{prefix}.{stem}"
            package = prefix
            if suffix in _SOURCE:
                files.append(File(entry.path, module, package))
            elif stem not in seen:
                seen.add(stem)
                loaded = _loaded(directory, stem, names)
                if loaded is not None and loaded not in _SOURCE:
                    path = os.path.join(directory, stem + loaded)
                    files.append(File(path, module, package, source=False))
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


class Source:
    """What one run of the check reads, each file once: the files of every
    hosted app, as ``owner`` attributes modules to installed apps.
    """

    def __init__(
        self,
        hosted: Mapping[str, AppConfig],
        owner: Callable[[str], str | None],
    ) -> None:
        """Read the hosted apps ``hosted``, by label; ``owner`` gives the
        label of the installed app that holds a dotted name, or ``None``.
        """
        self.hosted = dict(hosted)
        self._owner = owner
        self._modules: dict[str, Module] = {}
        self._files: dict[str, list[File]] | None = None
        self._failed: dict[str, list[tuple[str, OSError]]] = {}

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
    """``neolith.E112`` and ``neolith.W104`` for the hosted apps' files."""
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
    return found
