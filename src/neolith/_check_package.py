"""The contract package of each hosted app, read from its declaration and
Django's registry once the apps are loaded (ADR 0030 Decision 2).

For each ``Provided`` in a hosted app's declaration, the modules that define
the contract class and the record and enum classes its description names
under ``$defs`` (``Contract._types``, kept by the walk that runs when the
contract is declared). Each is named by the class's own ``__module__``.
Those that the registry places in the app itself
(``Apps.get_containing_app_config``, which gives the innermost installed
app, so a nested app's modules are its own) are the app's contract modules;
one in another app, or in no app, is not part of this app's package. Above
them, every package module up to the top-level package, since Python runs
each before the module itself, with the installed app that holds it: the
app itself, an outer app it is nested in, or none.

A contract module that also defines a model, or holds the implementation of
a contract the app provides, is left out, with each reason: such a module is
not part of the contract package, not even as a package module above
another (``neolith.E105``, which the import rules report). Its models are
those of the registry whose class names it as its own module, other than
the automatic tables of many-to-many relations, which Django builds there
for the model that declares them.

Nothing is imported and no code of an app runs: the classes are the ones the
contracts already hold, models come from the registry, and the
implementation's module is the dotted path of its ``Provided``.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any

from django.apps import AppConfig

from neolith import _naming
from neolith._declaration import declaration_of

__all__ = ["ContractPackage", "LeftOut", "packages"]


@dataclasses.dataclass(frozen=True, slots=True)
class LeftOut:
    """A module of the app that defines a contract or one of its records or
    enums, and is not part of the contract package, with why.

    Attributes:
        defines: The qualified names of the contract classes, records and
            enums it defines, sorted.
        models: The labels of the models it defines, sorted.
        implementations: The dotted paths of the implementations of the
            app's contracts that it holds, sorted.
    """

    defines: tuple[str, ...]
    models: tuple[str, ...]
    implementations: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ContractPackage:
    """The contract package of one hosted app.

    The package is ``modules`` and the keys of ``above``; ``left_out`` is
    not part of it.

    Attributes:
        modules: Each module of the app that defines a contract the app
            provides, or a record or enum of one, with the qualified names
            of those it defines, sorted.
        above: Each package module above one of ``modules``, up to the
            top-level package, other than those modules and those left
            out, with the label of the installed app that holds it, or
            ``None`` when it lies in no app. A namespace package is named
            too; it has no file, so nothing of it runs.
        left_out: Each module that would be one of ``modules``, but also
            defines a model or holds an implementation.
    """

    modules: Mapping[str, tuple[str, ...]]
    above: Mapping[str, str | None]
    left_out: Mapping[str, LeftOut]


def packages(hosted: Mapping[str, AppConfig]) -> dict[str, ContractPackage]:
    """The contract package of each hosted app, by label."""
    from django.apps import apps as global_apps

    models: dict[str, list[str]] = {}
    for model in global_apps.get_models(include_swapped=True):
        module = _naming.module(model)
        if module is not None:
            models.setdefault(module, []).append(model._meta.label)
    return {label: _package(label, config, models) for label, config in hosted.items()}


def _holder(module: str) -> str | None:
    """The label of the installed app that holds ``module``, if any."""
    from django.apps import apps as global_apps

    config = global_apps.get_containing_app_config(module)
    return None if config is None else config.label


def _package(
    label: str, config: AppConfig, models: Mapping[str, list[str]]
) -> ContractPackage:
    declaration = declaration_of(type(config))
    provides = () if declaration is None else declaration.provides
    implementations: dict[str, list[str]] = {}
    for provision in provides:
        path = provision.implementation
        implementations.setdefault(path.rpartition(".")[0], []).append(path)
    defines: dict[str, set[str]] = {}
    for provision in provides:
        held = provision.contract
        classes: list[type[Any]] = [held.declaration, *held._types.values()]
        for cls in classes:
            module = _naming.module(cls)
            if module is not None and _holder(module) == label:
                defines.setdefault(module, set()).add(_naming.qualname(cls, None))
    left_out = {
        module: LeftOut(
            tuple(sorted(names)),
            tuple(sorted(models.get(module, ()))),
            tuple(sorted(implementations.get(module, ()))),
        )
        for module, names in defines.items()
        if module in models or module in implementations
    }
    kept = {
        module: tuple(sorted(names))
        for module, names in defines.items()
        if module not in left_out
    }
    above: dict[str, str | None] = {}
    for module in kept:
        parts = module.split(".")
        for end in range(1, len(parts)):
            package = ".".join(parts[:end])
            if package not in kept and package not in left_out:
                above[package] = _holder(package)
    return ContractPackage(kept, above, left_out)
