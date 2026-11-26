"""The boundary pass's rules on imports (spec 05, APP-66; ADR 0030,
``neolith.E101`` to ``neolith.E105`` and ``neolith.W105``).

Read over the hosted apps' source (``neolith._check_source``) and their
contract packages (``neolith._check_package``), once the apps are loaded.
A module belongs to the installed app that Django's registry gives for its
name (``Apps.get_containing_app_config``, the innermost). A module is
*public* when it is part of any hosted app's contract package: a contract
module or a package module above one, never a module left out.

For each module of a hosted app ``<a>``, each import (``Source.edges``: the
statements and the literal dynamic imports) of a module ``t``:

- ``neolith.E101`` when ``t`` is a module of another hosted app and is not
  public;
- ``neolith.E102`` when ``t`` is a models module of an undeclared app, a
  contrib app included: ``<name>.models`` or a module under it, or a module
  that a model of that app names as its ``__module__``;
- ``neolith.E103`` when Python, importing ``t``, runs first-party code in no
  hosted app (``t`` itself or a package above it) from which a chain of
  imports through such code only reaches a module that E101 or E102 would
  report for ``<a>``; one shortest chain is named. A chain never starts at
  or goes into a module of a hosted app or one that E102 reports, and a
  module of it that cannot be read or has no source ends it.

Python runs every package above a module before the module itself
(``importlib._bootstrap._find_and_load_unlocked``), so for E103 and E104 an
import of ``t`` is also an import of each package above ``t``, other than
those above the importing module, which ran before it.

For each module ``m`` of a contract package that lies in a hosted app
``<c>``, ``neolith.E104`` when it imports ``t``, or a package above ``t``,
that is a module of ``<c>`` and not public. For each module the contract
package of ``<b>`` leaves out, ``neolith.E105``: it defines a contract or a
record, or is a package above one, and also defines a model or holds the
implementation of a contract ``<b>`` provides.

For each undeclared app ``<c>`` whose package is first-party,
``neolith.W105`` against hosted app ``<a>`` when a module of ``<c>`` imports
a module of ``<a>`` that is not public, or a model of ``<c>`` has a relation
to a model of ``<a>``.
"""

import ast
import os
from collections import deque
from collections.abc import Iterable, Iterator
from typing import Any, Final, NamedTuple

from django.apps import AppConfig
from django.core import checks

from neolith import _check_models, _naming
from neolith._check import Finding, place, shown
from neolith._check_package import ContractPackage, LeftOut, packages
from neolith._check_source import File, Module, Source, split
from neolith._declaration import declaration_of

__all__ = ["findings"]

_INTERNAL_HINT: Final = (
    "import only that app's contract package, and reach the rest through a "
    "contract it provides"
)
_MODELS_HINT: Final = (
    "keep that app's models out of this app's code, and reach its records "
    "through a contract"
)
_CHAIN_HINT: Final = (
    "move what this app needs from that code into the app, or take the import "
    "of the other app out of it"
)
_ALONE_HINT: Final = (
    "a contract package stands alone: move what it needs into the contract "
    "package or into code in no app"
)
_LEFT_OUT_HINT: Final = (
    "move the models and the implementation into modules of their own, outside "
    "the contract package"
)
_UNDECLARED_HINT: Final = (
    "until that app has a declaration and reaches this one through its "
    "contract package, the two cannot run apart"
)
_MODELS: Final = "models"
_INIT: Final = "__init__"


class _Hit(NamedTuple):
    """A module that E101 or E102 reports when an app imports it: ``kind``
    is ``"internal"`` for a module of hosted app ``app`` outside every
    contract package, and ``"models"`` for a models module of undeclared app
    ``app``.
    """

    kind: str
    target: str
    app: str


def findings(source: Source) -> list[Finding]:
    """Every ``neolith.E101`` to ``neolith.E105`` and ``neolith.W105`` of
    the hosted apps that ``source`` reads.
    """
    return _Rules(source).findings()


def _above(name: str) -> list[str]:
    """The packages above ``name``, outermost first."""
    parts = name.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts))]


class _Rules:
    def __init__(self, source: Source) -> None:
        from django.apps import apps as global_apps

        self.source = source
        self.hosted: dict[str, AppConfig] = dict(source.hosted)
        self.packages: dict[str, ContractPackage] = packages(self.hosted)
        self.provides = {
            label: bool(getattr(declaration_of(type(config)), "provides", ()))
            for label, config in self.hosted.items()
        }
        self.public: dict[str, list[str]] = {}
        for label, package in sorted(self.packages.items()):
            for name in [*package.modules, *package.above]:
                self.public.setdefault(name, []).append(label)
        self.undeclared: dict[str, AppConfig] = {
            config.label: config
            for config in global_apps.get_app_configs()
            if config.label not in self.hosted
        }
        self.model_modules: dict[str, str] = {}
        for model in global_apps.get_models(
            include_auto_created=True, include_swapped=True
        ):
            label = model._meta.app_label
            module = _naming.module(model)
            if label in self.undeclared and module is not None:
                self.model_modules.setdefault(module, label)
        # Each module of a hosted app, with the app's label.
        self.files: dict[str, tuple[str, File]] = {}
        for label, listing in source.files().items():
            for file in listing:
                self.files.setdefault(file.module, (label, file))
        self._owners: dict[str, str | None] = {}
        self._nodes: dict[str, list[tuple[str, int]]] = {}

    # What a dotted name is.

    def owner(self, name: str) -> str | None:
        """The label of the installed app that holds ``name``."""
        if name not in self._owners:
            from django.apps import apps as global_apps

            config = global_apps.get_containing_app_config(name)
            self._owners[name] = None if config is None else config.label
        return self._owners[name]

    def models_of(self, name: str) -> str | None:
        """The undeclared app whose models module ``name`` is, if any."""
        label = self.model_modules.get(name)
        if label is not None:
            return label
        owner = self.owner(name)
        config = self.undeclared.get(owner) if owner is not None else None
        if config is None:
            return None
        base = f"{config.name}.{_MODELS}"
        return owner if name == base or name.startswith(f"{base}.") else None

    def hit(self, app: str, name: str) -> _Hit | None:
        """What E101 or E102 reports when hosted app ``app`` imports
        ``name``, if anything.
        """
        owner = self.owner(name)
        if owner == app:
            return None
        if owner in self.hosted:
            return None if name in self.public else _Hit("internal", name, owner)
        models = self.models_of(name)
        return None if models is None else _Hit(_MODELS, name, models)

    def is_node(self, name: str) -> bool:
        """Whether a chain goes through ``name``: first-party code in no
        hosted app.
        """
        return name in self.source.map() and self.owner(name) not in self.hosted

    def runs(self, target: str, importer: str) -> list[str]:
        """The modules Python runs to import ``target`` from ``importer``:
        each package above ``target`` that is not above ``importer``,
        outermost first, then ``target``.
        """
        before = {*_above(importer), importer}
        return [name for name in [*_above(target), target] if name not in before]

    # Reading.

    def edges(self, module: Module) -> list[tuple[str, int]]:
        """The imports of ``module``, each once, in the order of the file."""
        seen: dict[tuple[str, int], None] = {}
        for edge in self.source.edges(module):
            seen.setdefault((edge.target, edge.line), None)
        return sorted(seen, key=lambda edge: (edge[1], edge[0]))

    def node(self, name: str) -> list[tuple[str, int]]:
        """The imports of the first-party module ``name`` in no hosted app;
        none when it is a namespace package, has no source, or cannot be
        read or compiled.
        """
        found = self._nodes.get(name)
        if found is not None:
            return found
        placed = self.source.map()[name]
        found = []
        if placed.path is not None and placed.source:
            parts = split(os.path.basename(placed.path))
            package = (
                name
                if parts is not None and parts[0] == _INIT
                else (name.rpartition(".")[0])
            )
            found = self.edges(self.source.read(File(placed.path, name, package)))
        self._nodes[name] = found
        return found

    def modules(self, label: str) -> Iterator[Module]:
        for module in self.source.modules(label):
            if module.tree is not None:
                yield module

    # The rules.

    def findings(self) -> list[Finding]:
        found: list[Finding] = []
        for label in self.hosted:
            found += self.imports(label)
        found += self.contract_packages()
        found += self.left_out()
        found += self.undeclared_apps()
        return found

    def imports(self, label: str) -> list[Finding]:
        """E101, E102 and E103 for the modules of hosted app ``label``."""
        found: list[Finding] = []
        pending: list[tuple[Module, int, str, list[str]]] = []
        for module in self.modules(label):
            name = module.file.module
            for target, line in self.edges(module):
                hit = self.hit(label, target)
                if hit is not None:
                    found.append(self.direct(label, module, line, hit))
                starts = [
                    each
                    for each in self.runs(target, name)
                    if self.is_node(each) and self.hit(label, each) is None
                ]
                if starts:
                    pending.append((module, line, target, starts))
        chains = _Chains(self, label)
        chains.grow(each for *_, starts in pending for each in starts)
        for module, line, target, starts in pending:
            chain = chains.shortest(starts)
            if chain is not None:
                found.append(self.chained(label, module, line, target, chain))
        return found

    def direct(self, label: str, module: Module, line: int, hit: _Hit) -> Finding:
        name = shown(module.file.module)
        message = checks.Error(
            f"app {shown(label)}: module {name}: imports {self.described(hit)}",
            hint=_INTERNAL_HINT if hit.kind == "internal" else _MODELS_HINT,
            id="neolith.E101" if hit.kind == "internal" else "neolith.E102",
        )
        return Finding(message, label, place(module.file.path), line)

    def described(self, hit: _Hit) -> str:
        """A module that E101 or E102 reports, as a message names it."""
        if hit.kind == _MODELS:
            return (
                f"{shown(hit.target)}, a models module of app {shown(hit.app)}, "
                "which has no declaration"
            )
        text = (
            f"{shown(hit.target)}, a module of app {shown(hit.app)} outside its "
            "contract package"
        )
        if not self.provides[hit.app]:
            text += f", and app {shown(hit.app)} provides no contract"
        return text

    def chained(
        self,
        label: str,
        module: Module,
        line: int,
        target: str,
        chain: tuple[list[str], _Hit],
    ) -> Finding:
        through, hit = chain
        message = checks.Error(
            f"app {shown(label)}: module {shown(module.file.module)}: imports "
            f"{shown(target)}; through first-party code in no hosted app "
            f"({' -> '.join(shown(each) for each in through)}), it reaches "
            f"{self.described(hit)}",
            hint=_CHAIN_HINT,
            id="neolith.E103",
        )
        return Finding(message, label, place(module.file.path), line)

    def contract_packages(self) -> list[Finding]:
        """E104 for each module of a contract package that lies in a hosted
        app.
        """
        found: list[Finding] = []
        for name, labels in sorted(self.public.items()):
            if name not in self.files:
                continue
            app, file = self.files[name]
            if not file.source:
                continue
            module = self.source.read(file)
            of = app if app in labels else labels[0]
            for target, line in self.edges(module):
                inside = [
                    each
                    for each in self.runs(target, name)
                    if self.owner(each) == app and each not in self.public
                ]
                if not inside:
                    continue
                runs = "" if target in inside else f", which runs {shown(inside[0])}"
                message = checks.Error(
                    f"app {shown(app)}: module {shown(name)}, in the contract "
                    f"package of app {shown(of)}: imports {shown(target)}{runs}, "
                    f"a module of app {shown(app)} outside its contract package",
                    hint=_ALONE_HINT,
                    id="neolith.E104",
                )
                found.append(Finding(message, app, place(file.path), line))
        return found

    def left_out(self) -> list[Finding]:
        """E105 for each module a contract package leaves out."""
        found: list[Finding] = []
        for label, package in self.packages.items():
            for name, out in package.left_out.items():
                found.append(self.left(label, name, out, package))
        return found

    def left(
        self, label: str, name: str, out: LeftOut, package: ContractPackage
    ) -> Finding:
        does: list[str] = []
        if out.models:
            does.append(
                f"defines the model{'s' if len(out.models) > 1 else ''} "
                + ", ".join(shown(each) for each in out.models)
            )
        if out.implementations:
            does.append(
                "holds the implementation"
                + ("s " if len(out.implementations) > 1 else " ")
                + ", ".join(shown(each) for each in out.implementations)
            )
        doing = " and ".join(does)
        if out.defines:
            text = (
                f"module {shown(name)} defines "
                f"{', '.join(shown(each) for each in out.defines)} of the "
                f"contract package and also {doing}"
            )
        else:
            below = [each for each in package.modules if each.startswith(f"{name}.")]
            text = (
                f"module {shown(name)}, a package above the contract "
                f"module{'s' if len(below) > 1 else ''} "
                f"{', '.join(shown(each) for each in sorted(below))}, which "
                f"Python runs before {'them' if len(below) > 1 else 'it'}, {doing}"
            )
        message = checks.Error(
            f"app {shown(label)}: {text}, so it is not part of the contract package",
            hint=_LEFT_OUT_HINT,
            id="neolith.E105",
        )
        path, line = self.place_of(name, out)
        return Finding(message, label, place(path), line)

    def place_of(self, name: str, out: LeftOut) -> tuple[str | None, int]:
        """The file of the module ``name`` and the first line in it of a
        model or an implementation it holds, or line 1.
        """
        file = self.files[name][1] if name in self.files else None
        if file is None:
            placed = self.source.map().get(name)
            if placed is None or placed.path is None:
                return None, 1
            file = File(placed.path, name, name, placed.source)
        if not file.source:
            return file.path, 1
        tree = self.source.read(file).tree
        if tree is None:
            return file.path, 1
        models = {each.rpartition(".")[2] for each in out.models}
        held = {each.rpartition(".")[2] for each in out.implementations}
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name in models
        ]
        lines += [node.lineno for node in tree.body if _binds(node) & held]
        return file.path, min(lines, default=1)

    def undeclared_apps(self) -> list[Finding]:
        """W105 for each undeclared app whose package is first-party."""
        found: list[Finding] = []
        placed = self.source.map()
        for label, config in self.undeclared.items():
            if config.name not in placed:
                continue
            for name in sorted(placed):
                if not (name == config.name or name.startswith(f"{config.name}.")):
                    continue
                if self.owner(name) != label or not self.is_node(name):
                    continue
                for target, line in self.node(name):
                    owner = self.owner(target)
                    if owner not in self.hosted or target in self.public:
                        continue
                    assert owner is not None  # noqa: S101 - hosted labels are str
                    message = checks.Warning(
                        f"app {shown(owner)}: module {shown(name)} of app "
                        f"{shown(label)}, which has no declaration: imports "
                        f"{shown(target)}, a module of app {shown(owner)} outside "
                        "its contract package",
                        hint=_UNDECLARED_HINT,
                        id="neolith.W105",
                    )
                    found.append(
                        Finding(message, owner, place(placed[name].path), line)
                    )
            for model in config.get_models():
                found += self.related(label, model)
        return found

    def related(self, label: str, model: type[Any]) -> list[Finding]:
        """W105 for each relation of an undeclared app's model to a model of
        a hosted app.
        """
        options = model._meta
        targets: list[tuple[str, object]] = []
        for field in [*options.local_fields, *options.local_many_to_many]:
            if not field.is_relation:
                continue
            targets.append((field.name, field.remote_field.model))
            through = getattr(field.remote_field, "through", None)
            if field.many_to_many and through is not None:
                # An automatic table is the declaring model's app's own.
                targets.append((field.name, through))
        for field in options.private_fields:
            if (
                field.is_relation
                and field.related_model is not None
                and not getattr(field, "mti_inherited", False)
            ):
                targets.append((field.name, field.related_model))
        found: list[Finding] = []
        for name, target in targets:
            if not _is_model(target):
                continue
            other: str = target._meta.app_label  # type: ignore[attr-defined]
            if other not in self.hosted:
                continue
            message = checks.Warning(
                f"app {shown(other)}: model {shown(options.label)} of app "
                f"{shown(label)}, which has no declaration: field {shown(name)} "
                f"is a relation to {shown(target._meta.label)}, a model of app "  # type: ignore[attr-defined]
                f"{shown(other)}",
                hint=_UNDECLARED_HINT,
                obj=model,
                id="neolith.W105",
            )
            path, line = _check_models._source(model)
            found.append(Finding(message, other, place(path), line))
        return found


def _is_model(value: object) -> bool:
    from django.db import models

    return issubclass(type(value), type) and issubclass(value, models.Model)  # type: ignore[arg-type]


def _binds(node: ast.stmt) -> set[str]:
    """The names a module-level statement binds by definition, assignment
    or import.
    """
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.partition(".")[0] for alias in node.names}
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


class _Chains:
    """The shortest chains, for one hosted app, from first-party code in no
    hosted app to a module that E101 or E102 reports for it.
    """

    def __init__(self, rules: _Rules, app: str) -> None:
        self.rules = rules
        self.app = app
        self._distance: dict[str, int] = {}
        self._hits: dict[str, list[_Hit]] = {}
        self._follows: dict[str, list[str]] = {}
        self._known: set[str] = set()

    def grow(self, starts: Iterable[str]) -> None:
        """Read every node reachable from ``starts`` and, when any is new,
        recompute each node's distance to its nearest hit.
        """
        stack = sorted({each for each in starts if each not in self._known})
        if not stack:
            return
        while stack:
            name = stack.pop()
            if name in self._known:
                continue
            self._known.add(name)
            hits: list[_Hit] = []
            follows: set[str] = set()
            for target, _ in self.rules.node(name):
                hit = self.rules.hit(self.app, target)
                if hit is not None:
                    hits.append(hit)
                follows.update(
                    each
                    for each in self.rules.runs(target, name)
                    if each != name and self.rules.is_node(each)
                )
            self._hits[name] = sorted(set(hits))
            self._follows[name] = sorted(follows)
            stack.extend(each for each in follows if each not in self._known)
        back: dict[str, list[str]] = {}
        for name, after in self._follows.items():
            for each in after:
                back.setdefault(each, []).append(name)
        distance = {name: 0 for name, hits in self._hits.items() if hits}
        queue = deque(sorted(distance))
        while queue:
            name = queue.popleft()
            for before in sorted(back.get(name, [])):
                if before not in distance:
                    distance[before] = distance[name] + 1
                    queue.append(before)
        self._distance = distance

    def shortest(self, starts: list[str]) -> tuple[list[str], _Hit] | None:
        """One shortest chain from one of ``starts`` to a hit: the modules it
        goes through, and the hit at its end; ``None`` when none reaches one.
        Every module the starts reach is read first (``grow``), once.
        """
        self.grow(starts)
        reached = [each for each in starts if each in self._distance]
        if not reached:
            return None
        name = min(reached, key=lambda each: self._distance[each])
        through = [name]
        while self._distance[name]:
            step = self._distance[name] - 1
            name = min(
                each for each in self._follows[name] if self._distance.get(each) == step
            )
            through.append(name)
        return through, self._hits[name][0]
