"""Contracts, actions and the checks that run when they are defined (spec 01).

A contract is declared as a ``typing.Protocol`` subclass marked with
``@contract("<name>.v<N>")``. Each method marked with ``@action`` is an action
taking one request and returning one reply. A Protocol lets a type checker
accept the ``...`` bodies, keeps the class from being instantiated, and lets
it check implementations structurally. The decorator returns the class
unchanged and records a ``Contract`` on it, read back with ``Contract.of``.

``@implements(Contract)`` checks an implementation class against the contract.
Every check runs when the decorator runs, so a broken contract or
implementation fails when its module is imported (CONTRIBUTING K2), with a
``ContractDefinitionError`` naming the contract, the action and the field.
"""

import enum
import inspect
import re
import types
import typing
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol, overload

from pydantic import AwareDatetime, BaseModel, RootModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

__all__ = [
    "Action",
    "Contract",
    "ContractDefinitionError",
    "action",
    "contract",
    "implements",
]

# Spec 01, the grammar under "Identity and versioning". ASCII only: "[a-z]"
# and "[0-9]" do not match other scripts' letters or digits, as "\w" and "\d"
# would.
_SEGMENT = r"[a-z][a-z0-9_]*"
_IDENTITY = re.compile(
    rf"(?P<name>{_SEGMENT}(?:\.{_SEGMENT})*)\.v(?P<major>[1-9][0-9]*)"
)
_ACTION_NAME = re.compile(_SEGMENT)

_CONTRACT_ATTR = "__neolith_contract__"
_ACTION_ATTR = "__neolith_action__"

# The portable scalars. A timestamp is AwareDatetime: RFC 3339 requires an
# offset, which a plain datetime may lack.
_SCALARS: tuple[object, ...] = (
    bool,
    int,
    float,
    str,
    bytes,
    Decimal,
    AwareDatetime,
    date,
    timedelta,
)
_UNIONS: tuple[object, ...] = (typing.Union, types.UnionType)
_DIALECT = "https://json-schema.org/draft/2020-12/schema"
# Every type is described as it travels: the shape a caller sends and a
# provider returns.
_MODE: JsonSchemaMode = "serialization"


class ContractDefinitionError(TypeError):
    """A contract, an action or an implementation breaks spec 01.

    Raised by ``@contract``, ``@action`` and ``@implements`` when they run, so
    the error surfaces when the module that defines the class is imported.
    """


@dataclass(frozen=True, slots=True)
class Action:
    """One action of a contract: its name, idempotency and types.

    ``reply`` is ``None`` when the reply is empty.
    """

    name: str
    idempotent: bool
    request: type[BaseModel]
    reply: type[BaseModel] | None


class Contract:
    """What ``@contract`` records about a declaration class.

    Read it with ``Contract.of(Declaration)``.
    """

    __slots__ = ("_actions", "declaration", "identity", "major", "name")

    def __init__(
        self,
        identity: str,
        declaration: type[Any],
        actions: Mapping[str, Action],
    ) -> None:
        self.identity = identity
        self.name, self.major = _parse_identity(identity)
        self.declaration = declaration
        self._actions = types.MappingProxyType(dict(actions))

    @property
    def actions(self) -> Mapping[str, Action]:
        """The actions, by name, in declaration order."""
        return self._actions

    @classmethod
    def of(cls, declaration: object) -> "Contract":
        """Return the contract recorded on a class marked with ``@contract``."""
        found = (
            vars(declaration).get(_CONTRACT_ATTR)
            if inspect.isclass(declaration)
            else None
        )
        if not isinstance(found, Contract):
            raise ContractDefinitionError(
                f"{_name(declaration)} is not a contract; mark the class with "
                '@contract("<name>.v<N>")'
            )
        return found

    def describe(self) -> dict[str, Any]:
        """Return the neutral description of the contract (spec 01, Publication).

        Each action gives its idempotency and a reference to the JSON Schema of
        its request and reply, or ``None`` for an empty reply. Each record is
        described once under ``$defs``, by name. The result is JSON-compatible.
        """
        models: dict[type[BaseModel], None] = {}
        for item in self._actions.values():
            models[item.request] = None
            if item.reply is not None:
                models[item.reply] = None
        refs, schema = models_json_schema(
            [(model, _MODE) for model in models], ref_template="#/$defs/{model}"
        )
        return {
            "contract": self.identity,
            "jsonSchemaDialect": _DIALECT,
            "actions": {
                name: {
                    "idempotent": item.idempotent,
                    "request": refs[(item.request, _MODE)],
                    "reply": None if item.reply is None else refs[(item.reply, _MODE)],
                }
                for name, item in self._actions.items()
            },
            "$defs": schema.get("$defs", {}),
        }

    def _check_implementation(self, impl: type[Any]) -> None:
        where = f"{self.identity}, implementation {impl.__qualname__}"
        for name, item in self._actions.items():
            member = inspect.getattr_static(impl, name, None)
            if member is None or member is vars(self.declaration).get(name):
                raise ContractDefinitionError(f"{where}: action {name!r} is missing")
            if not inspect.isfunction(member):
                raise ContractDefinitionError(
                    f"{where}: action {name!r} must be an instance method taking "
                    f"(self, request), not a {type(member).__name__}"
                )
            request, reply = _signature(f"{where}, action {name!r}", member)
            if request is not item.request:
                raise ContractDefinitionError(
                    f"{where}: action {name!r} takes {_name(request)}, but "
                    f"{self.identity} declares {_name(item.request)}"
                )
            if item.reply is None and reply is not None:
                raise ContractDefinitionError(
                    f"{where}: action {name!r} returns {_name(reply)}, but "
                    f"{self.identity} declares an empty reply; annotate it -> None"
                )
            if reply is not item.reply:
                raise ContractDefinitionError(
                    f"{where}: action {name!r} returns {_name(reply)}, but "
                    f"{self.identity} declares {_name(item.reply)}"
                )
        for klass in impl.__mro__:
            if klass is object or klass is self.declaration:
                continue
            for attr, value in vars(klass).items():
                if attr.startswith("_") or attr in self._actions:
                    continue
                if inspect.isfunction(value) or isinstance(
                    value, staticmethod | classmethod
                ):
                    raise ContractDefinitionError(
                        f"{where}: public method {attr!r} is not an action of "
                        f"{self.identity}; make it private (prefix it with '_') or "
                        "move it out of the class"
                    )

    def __repr__(self) -> str:
        return f"<Contract {self.identity}>"


def contract[T](identity: str, /) -> Callable[[type[T]], type[T]]:
    """Mark a ``typing.Protocol`` class as the declaration of a contract.

    ``identity`` is ``<name>.v<N>``, for example ``"inventory.v1"``. The class
    holds only methods marked with ``@action``; names starting with ``_`` are
    ignored. The class is returned unchanged.
    """
    _parse_identity(identity)

    def decorate(declaration: type[T]) -> type[T]:
        if not inspect.isclass(declaration):
            raise ContractDefinitionError(
                f"{identity}: @contract decorates a class, not {_name(declaration)}"
            )
        if _CONTRACT_ATTR in vars(declaration):
            raise ContractDefinitionError(
                f"{identity}: {declaration.__qualname__} is already the contract "
                f"{Contract.of(declaration).identity}"
            )
        if declaration.__bases__ != (Protocol,):
            raise ContractDefinitionError(
                f"{identity}: declare the contract as "
                f"class {declaration.__name__}(Protocol), with no other base"
            )
        checker = _TypeChecker()
        actions: dict[str, Action] = {}
        for attr, value in vars(declaration).items():
            idempotent = (
                vars(value).get(_ACTION_ATTR) if inspect.isfunction(value) else None
            )
            if idempotent is None:
                if attr.startswith("_"):
                    continue
                raise ContractDefinitionError(
                    f"{identity}: {declaration.__qualname__}.{attr} is not an action; "
                    "a contract declares only methods marked with @action"
                )
            if not _ACTION_NAME.fullmatch(attr):
                raise ContractDefinitionError(
                    f"{identity}: action name {attr!r} must start with a lowercase "
                    "ASCII letter, followed by lowercase ASCII letters, digits or "
                    "underscores"
                )
            where = f"{identity}, action {attr!r}"
            request, reply = _signature(where, value)
            checker.where = where
            actions[attr] = Action(
                attr,
                idempotent,
                checker.message(request, "request"),
                None if reply is None else checker.message(reply, "reply"),
            )
        setattr(declaration, _CONTRACT_ATTR, Contract(identity, declaration, actions))
        return declaration

    return decorate


@overload
def action[F: Callable[..., Any]](function: F, /) -> F: ...


@overload
def action[F: Callable[..., Any]](*, idempotent: bool = False) -> Callable[[F], F]: ...


def action(function: object = None, /, *, idempotent: bool = False) -> Any:
    """Mark a method of a contract declaration as an action.

    Use ``@action`` or ``@action(idempotent=True)``. An action is
    non-idempotent unless declared idempotent.
    """
    if type(idempotent) is not bool:
        raise ContractDefinitionError(
            f"@action(idempotent=...) takes True or False, not {idempotent!r}"
        )

    def mark[F](method: F) -> F:
        if not inspect.isfunction(method):
            raise ContractDefinitionError(
                f"@action marks a function defined in a contract, not {_name(method)}"
            )
        vars(method)[_ACTION_ATTR] = idempotent
        return method

    return mark if function is None else mark(function)


def implements[T](declaration: type[Any], /) -> Callable[[type[T]], type[T]]:
    """Check a class against the contract it implements, and return it unchanged.

    The class has one instance method per action, taking ``(self, request)``
    annotated with the action's request type and returning its reply type
    (``None`` for an empty reply). Any other public method is an error; helpers
    start with ``_``.
    """
    spec = Contract.of(declaration)

    def decorate(impl: type[T]) -> type[T]:
        if not inspect.isclass(impl):
            raise ContractDefinitionError(
                f"{spec.identity}: @implements decorates a class, not {_name(impl)}"
            )
        spec._check_implementation(impl)
        return impl

    return decorate


def _parse_identity(identity: object) -> tuple[str, int]:
    match = _IDENTITY.fullmatch(identity) if isinstance(identity, str) else None
    if match is None:
        raise ContractDefinitionError(
            f"{identity!r} is not a contract identity: write <name>.v<N>, where "
            "<name> is dot-separated segments of lowercase ASCII letters, digits "
            "and underscores, each starting with a letter, and <N> is a positive "
            "integer, for example 'inventory.v1'"
        )
    return match["name"], int(match["major"])


def _signature(where: str, function: Callable[..., Any]) -> tuple[object, object]:
    """Return the request and reply annotations; ``None`` for an empty reply."""
    if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
        raise ContractDefinitionError(
            f"{where}: actions are synchronous in this version"
        )
    parameters = list(inspect.signature(function).parameters.values())
    if len(parameters) != 2 or any(
        p.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
        or p.default is not inspect.Parameter.empty
        for p in parameters
    ):
        raise ContractDefinitionError(
            f"{where}: an action takes exactly (self, request), "
            f"not {inspect.signature(function)}"
        )
    try:
        hints = typing.get_type_hints(function)
    except NameError as error:
        raise ContractDefinitionError(
            f"{where}: cannot resolve the annotation {error.name!r}; define the "
            "request and reply types before the class that uses them"
        ) from error
    request = hints.get(parameters[1].name)
    if request is None:
        raise ContractDefinitionError(f"{where}: annotate its request type")
    if "return" not in hints:
        raise ContractDefinitionError(
            f"{where}: annotate its reply type, or -> None for an empty reply"
        )
    reply = hints["return"]
    return request, (None if reply is type(None) else reply)


class _TypeChecker:
    """Check request and reply types against the portable forms (spec 01).

    One checker serves one contract, so that the names of records and enums
    are checked across all its actions.
    """

    def __init__(self) -> None:
        self.where = ""
        self.named: dict[str, type[Any]] = {}

    def message(
        self, annotation: object, role: Literal["request", "reply"]
    ) -> type[BaseModel]:
        if (
            inspect.isclass(annotation)
            and issubclass(annotation, BaseModel)
            and not issubclass(annotation, RootModel)
        ):
            self.record(annotation, annotation.__name__)
            return annotation
        empty = "" if role == "request" else ", or None for an empty reply"
        raise ContractDefinitionError(
            f"{self.where}: the {role} type must be a record (a pydantic "
            f"model with named fields){empty}, not {_name(annotation)}"
        )

    def register(self, named: type[Any]) -> bool:
        """Record the name of a record or enum; False if it was seen already."""
        known = self.named.get(named.__name__)
        if known is named:
            return False
        if known is not None:
            raise ContractDefinitionError(
                f"{self.where}: two different records or enums are named "
                f"{named.__name__!r}: {known.__module__}.{known.__qualname__} and "
                f"{named.__module__}.{named.__qualname__}"
            )
        self.named[named.__name__] = named
        return True

    def record(self, model: type[BaseModel], path: str) -> None:
        if not self.register(model):
            return
        extra = model.model_config.get("extra")
        if extra not in (None, "ignore"):
            raise ContractDefinitionError(
                f"{self.where}: record {model.__name__} at {path} sets "
                f"extra={extra!r}; a record must ignore unknown fields so that "
                "adding a field stays compatible"
            )
        for field_name, field in model.model_fields.items():
            if (field.alias, field.validation_alias, field.serialization_alias) != (
                None,
                None,
                None,
            ):
                raise ContractDefinitionError(
                    f"{self.where}: field {path}.{field_name} has an alias; a field "
                    "travels under its own name in this version, so that the "
                    "description and the wire agree"
                )
            self.check(field.annotation, f"{path}.{field_name}")
        overrides = [key for key in model.model_config if key.startswith("ser_json_")]
        if "json_schema_extra" in model.model_config:
            overrides.append("json_schema_extra")
        if (
            model.__get_pydantic_json_schema__.__func__  # type: ignore[attr-defined]
            is not BaseModel.__get_pydantic_json_schema__.__func__  # type: ignore[attr-defined]
        ):
            overrides.append("__get_pydantic_json_schema__")
        changes: list[tuple[str | None, str]] = [
            (None, f"{key} set") for key in overrides
        ]
        changes += _wire_or_description_changes(_model_schema(model), None)
        for changed, kind in changes:
            at = f"field {path}.{changed}" if changed else f"record {path}"
            raise ContractDefinitionError(
                f"{self.where}: {at} has {kind}; a record travels as its declared "
                "fields, each in its portable form, and is described from them, so "
                "custom serialization and JSON Schema overrides are not supported"
            )

    def check(self, annotation: object, path: str) -> None:
        annotation = _unwrap(annotation)
        if any(annotation is scalar for scalar in _SCALARS):
            return
        if annotation is datetime:
            raise ContractDefinitionError(
                f"{self.where}: field {path} is a timestamp, which carries an "
                "offset from UTC (RFC 3339); annotate it as pydantic.AwareDatetime"
            )
        origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
        if inspect.isclass(annotation) and not arguments:
            if issubclass(annotation, BaseModel) and not issubclass(
                annotation, RootModel
            ):
                self.record(annotation, path)
                return
            if issubclass(annotation, enum.Enum) and all(
                isinstance(member.value, str) for member in annotation
            ):
                self.register(annotation)
                return
        elif origin is list and len(arguments) == 1:
            self.check(arguments[0], f"{path}[*]")
            return
        elif origin is dict and len(arguments) == 2 and _unwrap(arguments[0]) is str:
            self.check(arguments[1], f"{path}{{*}}")
            return
        elif origin in _UNIONS and len(arguments) == 2 and type(None) in arguments:
            self.check(next(a for a in arguments if a is not type(None)), path)
            return
        elif origin is Literal and all(type(value) is str for value in arguments):
            return
        raise ContractDefinitionError(
            f"{self.where}: field {path} has type {_name(annotation)}, which is not "
            "a portable form"
        )


def _model_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Find the model's own node in its Pydantic core schema."""
    pending: list[object] = [model.__pydantic_core_schema__]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("type") == "model" and node.get("cls") is model:
                return node
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    # Pydantic builds a model node for every model; this guards its internals.
    raise ContractDefinitionError(
        f"cannot read the core schema of {model.__qualname__}"
    )


def _wire_or_description_changes(
    node: object, field: str | None, *, root: bool = True
) -> Iterator[tuple[str | None, str]]:
    """Yield what makes a record travel or be described other than as declared.

    Walks the record's own core schema, which is what Pydantic serialises and
    describes from. On the wire: serializers on the record or a field
    (``model_serializer``, ``field_serializer``, ``PlainSerializer`` and
    ``WrapSerializer``, at any depth), computed fields, and fields excluded
    from serialization. In the description: ``WithJsonSchema`` at any depth and
    ``json_schema_extra`` on a field. Nested records are checked on their own.
    """
    if isinstance(node, dict):
        if node.get("type") == "model" and not root:
            return
        if "serialization" in node:
            yield field, "a custom serializer"
        if node.get("computed_fields"):
            yield field, "computed fields"
        if node.get("serialization_exclude") or node.get("serialization_exclude_if"):
            yield field, "an exclusion from serialization"
        metadata = node.get("metadata")
        if isinstance(metadata, dict) and (
            metadata.get("pydantic_js_annotation_functions")
            or metadata.get("pydantic_js_extra")
        ):
            yield field, "a JSON Schema override"
        if node.get("type") == "model-fields":
            for name, sub in node.get("fields", {}).items():
                yield from _wire_or_description_changes(sub, name, root=False)
            return
        for value in node.values():
            yield from _wire_or_description_changes(value, field, root=False)
    elif isinstance(node, list):
        for value in node:
            yield from _wire_or_description_changes(value, field, root=False)


def _unwrap(annotation: object) -> object:
    """Strip ``Annotated`` metadata and resolve ``type`` aliases."""
    while True:
        if typing.get_origin(annotation) is Annotated:
            annotation = typing.get_args(annotation)[0]
        elif isinstance(annotation, typing.TypeAliasType):
            annotation = annotation.__value__
        else:
            return annotation


def _name(annotation: object) -> str:
    """Name a type for an error message, without module paths."""
    if annotation is None or annotation is type(None):
        return "None"
    origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in _UNIONS:
        return " | ".join(_name(a) for a in arguments)
    if origin is Annotated:
        return _name(arguments[0])
    if origin is not None and arguments:
        return f"{_name(origin)}[{', '.join(_name(a) for a in arguments)}]"
    if inspect.isclass(annotation):
        return annotation.__qualname__
    return repr(annotation)
