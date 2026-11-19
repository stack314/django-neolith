"""The boundary pass's rules on models, read from Django's registry once the
apps are loaded (spec 05, APP-67; ADR 0030, ``neolith.E106`` and
``neolith.E107``).

For each model of a hosted app ``<a>``:

- ``neolith.E106``: each relation the model declares to a model of another
  installed app: a foreign key, a one-to-one relation, a many-to-many
  relation (to its target, and through an explicit model of another app), a
  generic relation, and a multi-table inheritance link. The foreign key to
  ``ContentType`` that a ``GenericForeignKey`` rests on is a foreign key
  like any other, and its message names the ``GenericForeignKey``. A
  relation is read from the model's own fields, so a field inherited from
  an abstract model counts and one of a concrete parent is judged there.
  Lazy references are resolved by Django; one that never resolved is
  Django's own error and is not judged here.
- ``neolith.E107``: the model is a proxy of another app's model, or the
  table of the model, or of a many-to-many table it creates, equals the
  table of another app's model, compared without regard to ASCII case.

Another app is any installed app other than ``<a>``: another hosted app, an
app without a declaration, a contrib app such as ``auth``, and the app that
``AUTH_USER_MODEL`` names (ADR 0004). A model's place is its class's first
line; a many-to-many table's is the model that creates it.
"""

import inspect
import sys
from collections.abc import Mapping
from typing import Any, Final

from django.apps import AppConfig
from django.core import checks

from neolith._check import Finding, place, shown

__all__ = ["findings"]

_RELATION_HINT: Final = (
    "keep the other record's identifier in a plain field, and reach that "
    "app through a contract it provides"
)
_PROXY_HINT: Final = (
    "define a model of the app's own, or reach those records through a "
    "contract that app provides"
)
_TABLE_HINT: Final = "give the model a table name of its own"
_ASCII_LOWER: Final = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def findings(hosted: Mapping[str, AppConfig]) -> list[Finding]:
    """Every ``neolith.E106`` and ``neolith.E107`` of the hosted apps."""
    from django.apps import apps as global_apps

    tables: dict[str, list[type[Any]]] = {}
    for model in global_apps.get_models(include_auto_created=True):
        if not model._meta.proxy:
            tables.setdefault(_folded(model._meta.db_table), []).append(model)
    found: list[Finding] = []
    for label, config in hosted.items():
        for model in config.get_models(include_auto_created=True):
            if not model._meta.auto_created:
                found += _relations(label, model)
            found += _stored(label, model, tables)
    return found


def _folded(table: str) -> str:
    return table.translate(_ASCII_LOWER)


def _relations(label: str, model: type[Any]) -> list[Finding]:
    options = model._meta
    generic = {
        getattr(field, "ct_field", None): field.name
        for field in options.private_fields
        if field.is_relation and field.related_model is None
    }
    found: list[Finding] = []
    for field in [*options.local_fields, *options.local_many_to_many]:
        if not field.is_relation:
            continue
        note = ""
        if field.name in generic:
            note = (
                f", which the GenericForeignKey {shown(generic[field.name])} rests on"
            )
        found += _across(
            label, model, field.name, _kind(field), field.remote_field.model, note
        )
        if field.many_to_many:
            through = field.remote_field.through
            if _is_model(through) and not through._meta.auto_created:
                found += _across(
                    label,
                    model,
                    field.name,
                    "a many-to-many relation through",
                    through,
                )
    for field in options.private_fields:
        if field.is_relation and field.related_model is not None:
            found += _across(
                label, model, field.name, "a generic relation to", field.related_model
            )
    return found


def _kind(field: Any) -> str:  # noqa: ANN401
    if field.many_to_many:
        return "a many-to-many relation to"
    if field.one_to_one:
        if field.remote_field.parent_link:
            return "a multi-table inheritance link to"
        return "a one-to-one relation to"
    return "a foreign key to"


def _is_model(value: object) -> bool:
    """Whether ``value`` is a model class; a reference that never resolved
    is still a ``str``.
    """
    from django.db import models

    return issubclass(type(value), type) and issubclass(value, models.Model)  # type: ignore[arg-type]


def _across(
    label: str,
    model: type[Any],
    name: str,
    kind: str,
    target: object,
    note: str = "",
) -> list[Finding]:
    if not _is_model(target):
        return []
    other: str = target._meta.app_label  # type: ignore[attr-defined]
    if other == label:
        return []
    message = checks.Error(
        f"app {shown(label)}: model {shown(model._meta.label)}: field "
        f"{shown(name)} is {kind} {shown(target._meta.label)}, a model of app "  # type: ignore[attr-defined]
        f"{shown(other)}{note}",
        hint=_RELATION_HINT,
        obj=model,
        id="neolith.E106",
    )
    return [_at(message, label, model)]


def _stored(
    label: str, model: type[Any], tables: Mapping[str, list[type[Any]]]
) -> list[Finding]:
    options = model._meta
    if options.proxy:
        concrete = options.concrete_model
        if concrete is None or concrete._meta.app_label == label:
            return []
        message = checks.Error(
            f"app {shown(label)}: model {shown(options.label)} is a proxy of "
            f"{shown(concrete._meta.label)}, a model of app "
            f"{shown(concrete._meta.app_label)}, so its records are that app's",
            hint=_PROXY_HINT,
            obj=model,
            id="neolith.E107",
        )
        return [_at(message, label, model)]
    found: list[Finding] = []
    for other in tables.get(_folded(options.db_table), []):
        if other._meta.app_label == label:
            continue
        message = checks.Error(
            f"app {shown(label)}: model {shown(options.label)} has the table "
            f"{shown(repr(options.db_table))}, the table of "
            f"{shown(other._meta.label)}, a model of app "
            f"{shown(other._meta.app_label)}, compared without regard to case",
            hint=_TABLE_HINT,
            obj=model,
            id="neolith.E107",
        )
        found.append(_at(message, label, model))
    return found


def _at(message: checks.CheckMessage, label: str, model: type[Any]) -> Finding:
    """The finding at the first line of the model's class, or of the model
    that created it (an automatic many-to-many table).
    """
    owner = model._meta.auto_created or model
    path, line = _source(owner)
    return Finding(message, label, place(path), line)


def _source(model: type[Any]) -> tuple[str | None, int]:
    """The file and first line of a model's class, as ``inspect`` reads it;
    the module's file and line 1 when the class has no source there.
    """
    try:
        path = inspect.getsourcefile(model)
        _, line = inspect.getsourcelines(model)
    except (OSError, TypeError):
        module = sys.modules.get(model.__module__)
        file = getattr(module, "__file__", None)
        return (file if type(file) is str else None), 1
    return path, max(line, 1)
