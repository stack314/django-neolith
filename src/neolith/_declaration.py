"""The app declaration (spec 05, ADR 0021).

An app declares itself in the class attributes of one ``NeolithAppConfig``
subclass. The class is checked when it is defined, so a declaration in an
unexpected form fails when its ``apps`` module is imported. A class that
passes holds one frozen snapshot, which is all the host reads.

Every value is judged by its type alone. Nothing is looked up on a value the
author wrote, so a lazy object in a class body is never evaluated.
"""

import enum
import inspect
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Final, final

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.core.exceptions import ImproperlyConfigured

from neolith._contracts import _SEGMENT, Contract, ContractDefinitionError

_FIELDS: Final = ("provides", "requires", "resources", "batteries")
_EVENT_FIELDS: Final = ("publishes", "subscribes")
_DJANGO_OPTIONS: Final = (
    "name",
    "label",
    "verbose_name",
    "path",
    "default",
    "default_auto_field",
)

# Every public name a config class may define: the four fields, the options
# Django's documentation gives for a config class, and ready(). A test compares
# this with AppConfig on every supported Django version.
ADMITTED_NAMES: Final = frozenset({*_FIELDS, *_DJANGO_OPTIONS, "ready"})

# The public names a base class or a mixin may define. Everything else in
# ADMITTED_NAMES is written in the body of the declaring class.
SHARED_NAMES: Final = frozenset({"default_auto_field", "verbose_name", "ready"})

RESOURCE_KINDS: Final = ("database",)

_APP_NAME = re.compile(_SEGMENT)
_APP_NAME_FORM = (
    "a lowercase ASCII letter, then lowercase ASCII letters, digits and underscores"
)
_SNAPSHOT_ATTR = "__neolith_declaration__"
_OWN_APP = "neolith"
_SHOWN = 80

_EXAMPLES: Final = {
    "provides": (
        '(Provided(<contract class>, implementation="<module>.<Class>", '
        "principal_from=()),)"
    ),
    "requires": "(<contract class>,)",
    "resources": '("database",)',
    "batteries": '("<django app name>",)',
}


class DeclarationError(ImproperlyConfigured):
    """An app's declaration is outside the admitted form (spec 05, ADR 0021).

    The message holds one line per refusal, sorted. Each line names the app
    and the field.
    """


class _Missing(enum.Enum):
    """The marker for a ``Provided`` argument that was left out."""

    MISSING = enum.auto()

    def __repr__(self) -> str:
        return "<missing>"


_MISSING: Final = _Missing.MISSING


@final
class Provided:
    """One entry of ``provides``: a contract, its implementation, its allow-list.

    ``Provided(<contract class>, implementation="<module>.<Class>",
    principal_from=("<calling app>", ...))``. ``implementation`` is the dotted
    path of the implementation class, so that reading a declaration imports no
    implementation. ``principal_from`` lists the calling apps that may assert
    a principal on calls to this contract; an empty tuple means none may.

    Nothing is checked here: the class body runs before the config class
    exists. ``NeolithAppConfig`` checks every entry with the rest of the
    declaration, and its error names the app.
    """

    __slots__ = ("_contract", "_implementation", "_principal_from")

    _contract: object
    _implementation: object
    _principal_from: object

    def __init__(
        self,
        contract: object = _MISSING,
        /,
        *,
        implementation: object = _MISSING,
        principal_from: object = _MISSING,
    ) -> None:
        object.__setattr__(self, "_contract", contract)
        object.__setattr__(self, "_implementation", implementation)
        object.__setattr__(self, "_principal_from", principal_from)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("Provided cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Provided cannot be changed")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a Provided cannot be changed")

    def __reduce__(self) -> tuple[Any, ...]:
        return (
            _rebuild_provided,
            (self._contract, self._implementation, self._principal_from),
        )

    def __repr__(self) -> str:
        return (
            f"Provided({_show(self._contract)}, "
            f"implementation={_show(self._implementation)}, "
            f"principal_from={_show(self._principal_from)})"
        )


def _rebuild_provided(
    contract: object, implementation: object, principal_from: object
) -> Provided:
    return Provided(
        contract, implementation=implementation, principal_from=principal_from
    )


@dataclass(frozen=True, slots=True)
class Provision:
    """A checked entry of ``provides``."""

    contract: Contract
    implementation: str
    principal_from: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Declaration:
    """The snapshot of a declaring class, taken when its check passed.

    A field the class leaves out is an empty tuple here.
    """

    config: "type[NeolithAppConfig]"
    name: str
    label: str
    provides: tuple[Provision, ...]
    requires: tuple[Contract, ...]
    resources: tuple[str, ...]
    batteries: tuple[str, ...]


def declaration_of(config: object) -> Declaration | None:
    """Return the snapshot of a declaring class, or ``None`` for any other value.

    Only the class's own snapshot counts, never one a base class holds.
    """
    if not issubclass(type(config), type):
        return None
    found = vars(config).get(_SNAPSHOT_ATTR)
    if type(found) is not Declaration or found.config is not config:
        return None
    return found


class NeolithAppConfig(AppConfig):
    """The config class of a neolith app, which holds the app's declaration.

    A subclass that sets ``name`` in its own body is a declaring class. It
    also sets ``label``, the app's name, and any of the four fields::

        class <App>Config(NeolithAppConfig):
            name = "<package>.<app>"
            label = "<app>"
            provides = (
                Provided(
                    <Contract>,
                    implementation="<package>.<app>.<module>.<Class>",
                    principal_from=("<calling app>",),
                ),
            )
            requires = (<OtherContract>,)
            resources = ("database",)
            batteries = ("django.contrib.humanize",)

    Each field is exactly a ``tuple``, and a field with no entry is left out.
    The class may also set Django's options ``verbose_name``, ``path``,
    ``default`` and ``default_auto_field``, and define ``ready``. Any other
    public name is refused; a helper's name starts with ``_``. A declaring
    class cannot be subclassed.

    A subclass that does not set ``name`` is a base class. It, and any mixin,
    may hold ``default_auto_field``, ``verbose_name`` and ``ready`` only.

    A declaration outside this form raises ``DeclarationError`` when the class
    is defined, listing every refusal.
    """

    default = False

    provides: ClassVar[tuple[Provided, ...]]
    requires: ClassVar[tuple[type, ...]]
    resources: ClassVar[tuple[str, ...]]
    batteries: ClassVar[tuple[str, ...]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        refusals, snapshot = _check(cls)
        if refusals:
            app = _class_name(cls)
            raise DeclarationError(
                "\n".join(sorted(f"app {app}: {refusal}" for refusal in refusals))
            )
        if snapshot is None:
            cls.default = False
            return
        if "default" not in vars(cls):
            cls.default = True
        setattr(cls, _SNAPSHOT_ATTR, snapshot)

    def import_models(self) -> None:
        """Check this instance against the snapshot, then load the models.

        Django's registry calls this for every config while it loads. Django
        reads ``name`` and ``label`` from the live class for each instance, so
        an instance that differs from the snapshot is refused, in any
        registry. A config that holds no checked declaration, such as a base
        class or a class whose check never ran, is refused as well. In
        Django's global registry a declared app also needs neolith's own app
        installed.
        """
        config = type(self)
        snapshot = declaration_of(config)
        if snapshot is None:
            raise DeclarationError(
                f"app {_class_name(config)}: it holds no checked declaration, "
                "so it cannot be an installed app; an app's config class sets "
                "name and label in its own body"
            )
        refusals: list[str] = []
        for attribute, declared in (("name", snapshot.name), ("label", snapshot.label)):
            found = inspect.getattr_static(self, attribute, _MISSING)
            if type(found) is not str or found != declared:
                refusals.append(
                    f"app {snapshot.label}: {attribute}: the app config has "
                    f"{_show(found)}, and the declaration was checked with "
                    f"{declared!r}; {attribute} is not changed after the class "
                    "is defined"
                )
        if self.apps is global_apps and not global_apps.is_installed(_OWN_APP):
            refusals.append(
                f"app {snapshot.label}: INSTALLED_APPS: {_OWN_APP!r} is not "
                f'installed; add "{_OWN_APP}" to INSTALLED_APPS'
            )
        if refusals:
            raise DeclarationError("\n".join(sorted(refusals)))
        super().import_models()


def _check(cls: type[NeolithAppConfig]) -> tuple[list[str], Declaration | None]:
    """Check a new subclass; return its refusals and, if it declares, its snapshot."""
    extras = [k for k in cls.__mro__ if k not in NeolithAppConfig.__mro__]
    for parent in extras[1:]:
        if _SNAPSHOT_ATTR in vars(parent):
            return [
                f"it extends {_class_name(parent)}, which holds a declaration; "
                "a declaration cannot be extended or relabelled"
            ], None

    own = dict(vars(cls))
    declares = "name" in own
    refusals: list[str] = []
    for klass in extras:
        body = own if klass is cls else dict(vars(klass))
        whole = klass is cls and declares
        for attribute in body:
            if attribute.startswith("_"):
                continue
            if (
                attribute == "default"
                and klass is not cls
                and issubclass(klass, NeolithAppConfig)
                and body[attribute] is False
            ):
                # The value neolith gave this base class when it was defined.
                continue
            holder = "" if klass is cls else f"{_class_name(klass)} holds it; "
            if attribute in _EVENT_FIELDS:
                refusals.append(
                    f"{attribute}: {holder}events arrive with spec 04; remove "
                    "the attribute"
                )
            elif attribute not in ADMITTED_NAMES:
                refusals.append(
                    f"{attribute}: {holder}not a name a config class may "
                    f"define, which are {_listed(ADMITTED_NAMES)}; a helper's "
                    'name starts with "_"'
                )
            elif not whole and attribute not in SHARED_NAMES:
                refusals.append(
                    f"{attribute}: {_class_name(klass)} holds it; a declaration "
                    "is written in one class body, the class whose own body "
                    "sets name, and a base class or a mixin holds only "
                    f"{_listed(SHARED_NAMES)}"
                )
    if not declares:
        return refusals, None

    name = _check_name(own["name"], refusals)
    label = _check_label(own, refusals)
    provides, provided = _check_provides(own, refusals)
    requires = _check_requires(own, provided, refusals)
    resources = _check_resources(own, refusals)
    batteries = _check_batteries(own, name, refusals)
    if refusals or name is None or label is None:
        return refusals, None
    return refusals, Declaration(
        config=cls,
        name=name,
        label=label,
        provides=tuple(provides),
        requires=tuple(requires),
        resources=resources,
        batteries=batteries,
    )


def _check_name(value: object, refusals: list[str]) -> str | None:
    if type(value) is str and _is_dotted_path(value):
        return value
    refusals.append(
        f"name: {_show(value)} is not a dotted Python path; write "
        'name = "<package>.<app>"'
    )
    return None


def _check_label(own: dict[str, Any], refusals: list[str]) -> str | None:
    if "label" not in own:
        refusals.append(
            f'label: missing; write label = "<app>", the app\'s name: {_APP_NAME_FORM}'
        )
        return None
    value = own["label"]
    if type(value) is str and _APP_NAME.fullmatch(value):
        return value
    refusals.append(
        f"label: {_show(value)} is not an app's name; write "
        f'label = "<app>": {_APP_NAME_FORM}'
    )
    return None


def _entries(field: str, own: dict[str, Any], refusals: list[str]) -> tuple[Any, ...]:
    """Return a field's entries, or ``()`` when it is absent or refused."""
    if field not in own:
        return ()
    value = own[field]
    kind = type(value)
    if kind is str or issubclass(kind, type):
        refusals.append(
            f"{field}: got {_show(value)}; the trailing comma of a one-element "
            f"tuple is missing, write {_EXAMPLES[field]}"
        )
        return ()
    if kind is not tuple:
        refusals.append(
            f"{field}: got {_show(value)}; write a tuple, {_EXAMPLES[field]}"
        )
        return ()
    if not value:
        refusals.append(
            f"{field}: an empty tuple; remove the line, a field with no entry "
            "is left out"
        )
        return ()
    return tuple(value)


def _contract_of(value: object, where: str, refusals: list[str]) -> Contract | None:
    if value is _MISSING:
        refusals.append(
            f"{where}: the contract is missing; name the contract's class, "
            "which carries @contract"
        )
        return None
    if not issubclass(type(value), type):
        refusals.append(
            f"{where}: {_show(value)} is not a contract class; name the "
            "contract's class, which carries @contract"
        )
        return None
    try:
        return Contract.of(value)
    except ContractDefinitionError as error:
        refusals.append(f"{where}: {_clip(str(error))}")
        return None


def _twice(first: Contract, second: Contract) -> str:
    if first is second:
        return f"{_class_name(first.declaration)} is listed twice"
    return (
        f"listed as {_class_name(first.declaration)} and as "
        f"{_class_name(second.declaration)}"
    )


def _check_provides(
    own: dict[str, Any], refusals: list[str]
) -> tuple[list[Provision], dict[str, Contract]]:
    """Return the checked entries, and every contract named, by identity."""
    provisions: list[Provision] = []
    seen: dict[str, Contract] = {}
    for position, entry in enumerate(_entries("provides", own, refusals)):
        where = f"provides: entry {position}"
        if type(entry) is not Provided:
            refusals.append(
                f"{where}: {_show(entry)} is not a Provided; write "
                f"{_EXAMPLES['provides']}"
            )
            continue
        # Each argument is read once, and the value read is the value kept.
        held = entry._contract
        implementation = entry._implementation
        principal_from = entry._principal_from
        found = _contract_of(held, where, refusals)
        if found is not None:
            where = f"provides: {found.identity}"
            if found.identity in seen:
                refusals.append(
                    f"{where}: {_twice(seen[found.identity], found)}; a "
                    "contract is provided once"
                )
            else:
                seen[found.identity] = found
        path = _check_implementation(implementation, where, refusals)
        callers = _check_principal_from(principal_from, where, refusals)
        if found is not None and path is not None and callers is not None:
            provisions.append(Provision(found, path, callers))
    return provisions, seen


def _check_implementation(value: object, where: str, refusals: list[str]) -> str | None:
    advice = (
        'name the implementation class by its dotted path, implementation="'
        '<module>.<Class>"'
    )
    if value is _MISSING:
        refusals.append(f"{where}: implementation is missing; {advice}")
        return None
    if type(value) is str and _is_dotted_path(value) and "." in value:
        return value
    refusals.append(
        f"{where}: implementation {_show(value)} is not a dotted path of two "
        f"or more identifiers; {advice}"
    )
    return None


def _check_principal_from(
    value: object, where: str, refusals: list[str]
) -> tuple[str, ...] | None:
    where = f"{where}: principal_from"
    advice = (
        "list the calling apps that may assert a principal, "
        'principal_from=("<app>",), or write principal_from=() for none'
    )
    if value is _MISSING:
        refusals.append(f"{where} is missing; {advice}")
        return None
    if type(value) is not tuple:
        refusals.append(f"{where} is {_show(value)}, not a tuple; {advice}")
        return None
    admitted = True
    seen: set[str] = set()
    for position, entry in enumerate(value):
        if type(entry) is str and entry == "*":
            refusals.append(
                f'{where}: entry {position} is "*"; there is no wildcard, '
                "name each calling app"
            )
            admitted = False
        elif type(entry) is not str or not _APP_NAME.fullmatch(entry):
            refusals.append(
                f"{where}: entry {position}, {_show(entry)}, is not an app's "
                f"name: {_APP_NAME_FORM}"
            )
            admitted = False
        elif entry in seen:
            refusals.append(f"{where}: {entry!r} is listed twice")
            admitted = False
        else:
            seen.add(entry)
    return tuple(value) if admitted else None


def _check_requires(
    own: dict[str, Any], provided: dict[str, Contract], refusals: list[str]
) -> list[Contract]:
    required: list[Contract] = []
    seen: dict[str, Contract] = {}
    for position, entry in enumerate(_entries("requires", own, refusals)):
        found = _contract_of(entry, f"requires: entry {position}", refusals)
        if found is None:
            continue
        where = f"requires: {found.identity}"
        if found.identity in seen:
            refusals.append(
                f"{where}: {_twice(seen[found.identity], found)}; a contract "
                "is required once"
            )
            continue
        seen[found.identity] = found
        other = provided.get(found.identity)
        if other is not None and other is not found:
            refusals.append(
                f"{where}: {_class_name(found.declaration)} here and "
                f"{_class_name(other.declaration)} in provides; one identity "
                "has one contract class"
            )
            continue
        required.append(found)
    return required


def _check_resources(own: dict[str, Any], refusals: list[str]) -> tuple[str, ...]:
    kinds = ", ".join(f'"{kind}"' for kind in RESOURCE_KINDS)
    admitted: list[str] = []
    for position, entry in enumerate(_entries("resources", own, refusals)):
        if type(entry) is not str or entry not in RESOURCE_KINDS:
            refusals.append(
                f"resources: entry {position}, {_show(entry)}, is not a "
                f"resource kind; the kinds admitted are {kinds}"
            )
        elif entry in admitted:
            refusals.append(
                f"resources: {entry!r} is listed twice; a kind is listed once"
            )
        else:
            admitted.append(entry)
    return tuple(admitted)


def _check_batteries(
    own: dict[str, Any], name: str | None, refusals: list[str]
) -> tuple[str, ...]:
    admitted: list[str] = []
    for position, entry in enumerate(_entries("batteries", own, refusals)):
        if type(entry) is not str or not _is_dotted_path(entry):
            refusals.append(
                f"batteries: entry {position}, {_show(entry)}, is not a Django "
                'app name in dotted form, such as "django.contrib.humanize"'
            )
        elif entry == name:
            refusals.append(
                f"batteries: {entry!r} is the app itself; an app does not list itself"
            )
        elif entry in admitted:
            refusals.append(f"batteries: {entry!r} is listed twice")
        else:
            admitted.append(entry)
    return tuple(admitted)


def _is_dotted_path(value: str) -> bool:
    return all(part.isidentifier() for part in value.split("."))


def _listed(names: frozenset[str]) -> str:
    return ", ".join(sorted(names))


def _clip(text: str) -> str:
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


def _class_name(klass: type) -> str:
    name = f"{klass.__module__}.{klass.__qualname__}"
    return name if len(name) <= 2 * _SHOWN else f"{name[: 2 * _SHOWN]}..."


def _show(value: object) -> str:
    """Describe a value for a message, by its type alone and within a bound.

    Only an exact ``str`` is shown by its content. ``repr`` of anything else
    could run the author's code, or have no bound.
    """
    kind = type(value)
    if kind is str:
        text: str = value  # type: ignore[assignment]  # judged by type(), not isinstance
        return repr(text) if len(text) <= _SHOWN else f"{text[:_SHOWN]!r}..."
    if kind is _Missing:
        return "<missing>"
    if issubclass(kind, type):
        shown: type = value  # type: ignore[assignment]  # as above
        return f"the class {_class_name(shown)}"
    if kind is tuple and not value:
        return "()"
    if kind.__module__ == "builtins":
        return f"a {kind.__qualname__}"
    return f"a {_class_name(kind)}"
