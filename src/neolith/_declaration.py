"""The app declaration (spec 05, ADR 0021).

An app declares itself in the class attributes of one ``NeolithAppConfig``
subclass. The class is checked when it is defined, so a declaration in an
unexpected form fails when its ``apps`` module is imported. A class that
passes holds one frozen snapshot, which is all the host reads.

Every value is judged by its type alone. Nothing is looked up on a value the
author wrote, so a lazy object in a class body is never evaluated.
"""

import enum
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Final, final

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.core.exceptions import ImproperlyConfigured

from neolith import _naming
from neolith._contracts import _SEGMENT, Contract, ContractDefinitionError
from neolith._frozen import once

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
@once
class Provided:
    """One entry of ``provides``: a contract, its implementation, its allow-list.

    ``Provided(<contract class>, implementation="<module>.<Class>",
    principal_from=("<calling app>", ...))``. ``implementation`` is the dotted
    path of the implementation class, so that reading a declaration imports no
    implementation. ``principal_from`` lists the calling apps that may assert
    a principal on calls to this contract; an empty tuple means none may.

    The implementation is a class defined at the top level of a module of the
    providing app, other than its package and the module of its config class,
    and ``implementation`` is that module's path, a dot and the class's name.
    The host imports it once Django has loaded models, checks it against the
    contract, and builds one instance with no arguments, which serves every
    call from every thread until the next set-up. Its ``__init__`` runs in
    every process that loads apps, so it reads no database and makes no
    contract call.

    Nothing is checked here: the class body runs before the config class
    exists. ``NeolithAppConfig`` checks every entry with the rest of the
    declaration, and its error names the app. An argument left out, which the
    signature shows as ``<missing>``, is refused there too.

    Args:
        contract: The contract's class, which carries ``@contract``.
        implementation: The dotted path of the implementation class.
        principal_from: The names of the calling apps that may assert a
            principal on calls to this contract, as a ``tuple``.

    Raises:
        TypeError: When a subclass is defined; ``Provided`` cannot be
            subclassed. Calling ``__init__`` on an entry already built raises
            it too.
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


@once
@dataclass(frozen=True, slots=True)
class Provision:
    """A checked entry of ``provides``."""

    contract: Contract
    implementation: str
    principal_from: tuple[str, ...]


@once
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
    found = _naming.entry(_naming.namespace(config), _SNAPSHOT_ATTR)
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
    ``path`` and ``default`` describe one app, its directory and whether
    Django picks this class from its ``apps`` module, so they are written in
    the body of the class that sets ``name``.

    A declaration outside this form raises ``DeclarationError`` when the class
    is defined, listing every refusal.

    The four fields are not set on this class. A declaring class sets those
    it has an entry for, and its snapshot holds an empty tuple for each one
    left out.

    Attributes:
        default: Django's option: whether Django picks this config for its
            app on its own. ``False`` on this class and on a base class;
            ``True`` on a declaring class unless its own body sets it.
        provides: The contracts the app provides, one ``Provided`` each.
        requires: The contract classes the app calls. The host finds each
            one's provider: the app of the host that provides it, or the
            remote provider the deployer names for it in
            ``NEOLITH["REMOTE_PROVIDERS"]``.
        resources: The kinds of resource the app needs; ``"database"`` is
            the one kind admitted.
        batteries: The Django apps the app relies on, each by its dotted
            name, such as ``"django.contrib.humanize"``.

    Raises:
        DeclarationError: When a subclass is defined whose declaration is
            outside this form.
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
        # Every key of the class's own body is exactly a str, or it was
        # refused above.
        if "default" not in _naming.namespace(cls):
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

        Raises:
            DeclarationError: If the config holds no checked declaration, if
                its ``name`` or ``label`` differs from the snapshot, or if it
                is in Django's global registry and neolith's own app is not
                installed.
        """
        config = type(self)
        snapshot = declaration_of(config)
        if snapshot is None:
            # Nothing is left to read: the two checks below compare with a
            # snapshot, or are about a declared app, and this config is none.
            raise DeclarationError(
                f"app {_class_name(config)}: it holds no checked declaration, "
                "so it cannot be an installed app; an app's config class sets "
                "name and label in its own body"
            )
        refusals: list[str] = []
        # As getattr_static finds them, the instance first, but each class is
        # read through type's own descriptors, never through its metaclass.
        held = [_naming.namespace(klass) for klass in _naming.mro(config)]
        own = _naming.instance_dict(self)
        if own is None:
            # Fail closed: its own name and label cannot be read without
            # running what the class holds as __dict__.
            refusals.append(
                f"app {snapshot.label}: the app config's own attributes cannot "
                "be read, as its class holds __dict__; remove it"
            )
        else:
            held.insert(0, own)
        for attribute, declared in (("name", snapshot.name), ("label", snapshot.label)):
            found = next(
                (
                    v
                    for h in held
                    if (v := _naming.entry(h, attribute, _MISSING)) is not _MISSING
                ),
                _MISSING,
            )
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
    # Classes are read through type's own descriptors and compared by
    # identity, and a name is looked up only in a body whose keys are each
    # exactly a str, so that no code of the classes, or of their keys, runs.
    ours = _naming.mro(NeolithAppConfig)
    extras = [k for k in _naming.mro(cls) if not _naming.one_of(k, ours)]
    refusals: list[str] = []
    checked: set[int] = set()
    for parent in extras[1:]:
        if _naming.holds(_naming.namespace(parent), _SNAPSHOT_ATTR):
            refusals.append(
                f"it extends {_class_name(parent)}, which holds a declaration; "
                "a declaration cannot be extended or relabelled"
            )
            # The parent's own classes were checked when it was defined.
            checked.update(map(id, _naming.mro(parent)))
    extras = [k for k in extras if id(k) not in checked]
    bodies: dict[int, dict[str, Any]] = {}
    for klass in extras:
        body: dict[str, Any] = {}
        for key, value in _naming.namespace(klass).items():
            if type(key) is str:
                body[key] = value
        if len(body) != len(_naming.namespace(klass)):
            refusals.append(
                f"{_class_name(klass)} has an attribute whose name is not exactly a str"
            )
        bodies[id(klass)] = body

    own = bodies[id(cls)]
    declares = "name" in own
    # A part of the declaration written in a base class or a mixin is refused
    # for where it is, and its value is checked all the same, so that moving
    # it shows no new refusal.
    written: dict[str, Any] = {}
    for klass in extras:
        body = bodies[id(klass)]
        whole = klass is cls and declares
        for attribute in body:
            if attribute.startswith("_"):
                # Not a refusal: a name that starts with "_" is admitted.
                continue
            if (
                attribute == "default"
                and klass is not cls
                and issubclass(klass, NeolithAppConfig)
                and body[attribute] is False
            ):
                # Not a refusal: the value neolith gave this base class when
                # it was defined.
                continue
            # One name gets one line for where it is. The branches exclude
            # each other, and only the last has a value with a form to check.
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
                written.setdefault(attribute, body[attribute])
    written.update(own)

    # A class that writes neither name nor label is a base class with no
    # app's name to check. The fields are checked whenever they are written.
    named = "name" in written or "label" in written
    name = _check_name(written["name"], refusals) if "name" in written else None
    label = _check_label(written, refusals) if named else None
    provides, provided = _check_provides(written, refusals)
    requires = _check_requires(written, provided, refusals)
    resources = _check_resources(written, refusals)
    batteries = _check_batteries(written, name, refusals)
    # Not a skipped check: every check has run, and only a declaring class
    # with no refusal gets a snapshot.
    if refusals or not declares or name is None or label is None:
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


# name, label and implementation are single values. Each has one form, so
# each gets one line, and its check has nothing behind it to skip.


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


_BUILTIN_CONTAINERS: Final = (list, set, frozenset, dict)


def _read(value: object) -> tuple[str, tuple[Any, ...] | None]:
    """Say how a value holds its entries, and return every entry readable.

    Whether an entry is read depends only on whether reading is safe, judged
    by the value's type, never on what the author may have meant:

    - a ``tuple`` is the admitted form;
    - an exact ``str``, a class or a ``Provided`` is itself the one entry of
      a tuple that lost its comma;
    - an exact ``list``, ``set``, ``frozenset`` or ``dict`` is iterated by
      the interpreter alone, and gives the values the author wrote (the keys,
      for a ``dict``);
    - anything else gives ``None``. Iterating a subclass, an iterator or a
      lazy object runs the author's code, the items of ``bytes`` or a
      ``range`` are not values the author wrote, and a value such as ``None``
      or an ``int`` holds none.
    """
    kind = type(value)
    if kind is tuple:
        return "tuple", value  # type: ignore[return-value]  # judged by type()
    if kind is str or issubclass(kind, type) or kind is Provided:
        return "entry", (value,)
    if _naming.one_of(kind, _BUILTIN_CONTAINERS):
        return "other", tuple(value)  # type: ignore[arg-type]  # as above
    return "other", None


def _entries(field: str, own: dict[str, Any], refusals: list[str]) -> tuple[Any, ...]:
    """Return a field's entries, as far as ``_read`` can read them.

    A refused container does not hide its entries.
    """
    if field not in own:
        # Nothing was written: a field with no entry is left out.
        return ()
    value = own[field]
    form, entries = _read(value)
    if form == "entry":
        refusals.append(
            f"{field}: got {_show(value)}; the trailing comma of a one-element "
            f"tuple is missing, write {_EXAMPLES[field]}"
        )
    elif form == "other":
        refusals.append(
            f"{field}: got {_show(value)}; write a tuple, {_EXAMPLES[field]}"
        )
    elif not entries:
        refusals.append(
            f"{field}: an empty tuple; remove the line, a field with no entry "
            "is left out"
        )
    # None only where _read cannot read the value.
    return () if entries is None else entries


def _contract_of(value: object, where: str, refusals: list[str]) -> Contract | None:
    """Return the contract a value names, or ``None`` with its one refusal.

    Each branch ends the read of this value because no contract can be read
    from it: it is absent, it is not a class, or the class carries no mark.
    """
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


def _again(entry: object, unread: list[type]) -> bool:
    """True if a class that names no contract was already listed.

    The class is compared by identity. When its own refusal is fixed it is
    still the same class, so its repeat is reported beside it.
    """
    if not issubclass(type(entry), type):
        # Another value is replaced, not kept, when its refusal is fixed.
        return False
    if any(entry is earlier for earlier in unread):
        return True
    unread.append(entry)  # type: ignore[arg-type]  # judged by type()
    return False


def _check_provides(
    own: dict[str, Any], refusals: list[str]
) -> tuple[list[Provision], dict[str, Contract]]:
    """Return the checked entries, and every contract named, by identity."""
    provisions: list[Provision] = []
    seen: dict[str, Contract] = {}
    unread: list[type] = []
    for position, entry in enumerate(_entries("provides", own, refusals)):
        where = f"provides: entry {position}"
        if type(entry) is not Provided:
            refusals.append(
                f"{where}: {_show(entry)} is not a Provided; write "
                f"{_EXAMPLES['provides']}"
            )
            # Only a Provided has the three arguments the rest reads.
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
        elif _again(held, unread):
            refusals.append(f"{where}: {_show(held)} is listed twice")
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
    # A single value with one form: one line, and nothing behind it.
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
        # Nothing was written, so there is no entry to read.
        refusals.append(f"{where} is missing; {advice}")
        return None
    form, found = _read(value)
    if form == "entry":
        refusals.append(
            f"{where} is {_show(value)}, not a tuple; the trailing comma of a "
            'one-element tuple is missing, write principal_from=("<app>",)'
        )
    elif form == "other":
        refusals.append(f"{where} is {_show(value)}, not a tuple; {advice}")
    if found is None:
        # _read cannot read the value, so it has no entries to check.
        return None
    admitted = form == "tuple"
    entries = found
    for position, entry in enumerate(entries):
        # One entry gets one line: "*" is not an app's name either, and the
        # line that names the wildcard says more.
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
    for repeat in _repeats(entries):
        refusals.append(f"{where}: {_show(repeat)} is listed twice")
        admitted = False
    return entries if admitted else None


def _repeats(entries: tuple[Any, ...]) -> list[str]:
    """The exact ``str`` entries that occur again, whatever else is refused."""
    seen: set[str] = set()
    again: list[str] = []
    for entry in entries:
        if type(entry) is not str:
            # Comparing another value could run the author's __eq__ or
            # __hash__, and such an entry is replaced when its refusal is
            # fixed, so no repeat of it can be known.
            continue
        if entry in seen and entry not in again:
            again.append(entry)
        seen.add(entry)
    return again


def _check_requires(
    own: dict[str, Any], provided: dict[str, Contract], refusals: list[str]
) -> list[Contract]:
    required: list[Contract] = []
    seen: dict[str, Contract] = {}
    unread: list[type] = []
    for position, entry in enumerate(_entries("requires", own, refusals)):
        found = _contract_of(entry, f"requires: entry {position}", refusals)
        if found is None:
            if _again(entry, unread):
                refusals.append(
                    f"requires: entry {position}: {_show(entry)} is listed twice"
                )
            # The checks below compare identities, and this entry has none.
            continue
        where = f"requires: {found.identity}"
        if found.identity in seen:
            refusals.append(
                f"{where}: {_twice(seen[found.identity], found)}; a contract "
                "is required once"
            )
        else:
            seen[found.identity] = found
        other = provided.get(found.identity, found)
        if other is not found:
            refusals.append(
                f"{where}: {_class_name(found.declaration)} here and "
                f"{_class_name(other.declaration)} in provides; one identity "
                "has one contract class"
            )
        required.append(found)
    return required


def _check_resources(own: dict[str, Any], refusals: list[str]) -> tuple[str, ...]:
    kinds = ", ".join(f'"{kind}"' for kind in RESOURCE_KINDS)
    entries = _entries("resources", own, refusals)
    for position, entry in enumerate(entries):
        if type(entry) is not str or entry not in RESOURCE_KINDS:
            refusals.append(
                f"resources: entry {position}, {_show(entry)}, is not a "
                f"resource kind; the kinds admitted are {kinds}"
            )
    for repeat in _repeats(entries):
        refusals.append(
            f"resources: {_show(repeat)} is listed twice; a kind is listed once"
        )
    return entries


def _check_batteries(
    own: dict[str, Any], name: str | None, refusals: list[str]
) -> tuple[str, ...]:
    entries = _entries("batteries", own, refusals)
    for position, entry in enumerate(entries):
        if type(entry) is not str or not _is_dotted_path(entry):
            refusals.append(
                f"batteries: entry {position}, {_show(entry)}, is not a Django "
                'app name in dotted form, such as "django.contrib.humanize"'
            )
        # The branches exclude each other: an entry equal to a checked name
        # is a dotted path. A name that was refused is None here, and no
        # entry in dotted form equals a name that is not in dotted form.
        elif entry == name:
            refusals.append(
                f"batteries: entry {position}, {entry!r}, is the app itself; "
                "an app does not list itself"
            )
    for repeat in _repeats(entries):
        refusals.append(f"batteries: {_show(repeat)} is listed twice")
    return entries


def _is_dotted_path(value: str) -> bool:
    return all(part.isidentifier() for part in value.split("."))


def _listed(names: frozenset[str]) -> str:
    return ", ".join(sorted(names))


def _clip(text: str) -> str:
    return text if len(text) <= _SHOWN else f"{text[:_SHOWN]}..."


def _class_name(klass: type) -> str:
    return _naming.full_name(klass, 2 * _SHOWN + 3)


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
    builtin = _naming.module(kind) == "builtins"
    name = _naming.qualname(kind) if builtin else _class_name(kind)
    return f"{'an' if name[:1] in 'aeiou' else 'a'} {name}"
