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

``ContractError`` is the one error type that crosses a contract (ADR 0008).
An action declares its own error kinds with
``@action(errors={"<kind>": DetailRecord})``; the standard kinds need no
declaration.
"""

import copy
import inspect
import re
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, NoReturn, Protocol, final, overload

from pydantic import BaseModel

from neolith._portable import Portable, json_value_problem, shown, text_problem

__all__ = [
    "Action",
    "Contract",
    "ContractDefinitionError",
    "ContractError",
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
# Spec 01, Errors: every error kind, standard or declared, is a segment.
_KIND = re.compile(_SEGMENT)
_STANDARD_KINDS = frozenset(
    {
        "invalid_argument",
        "not_found",
        "unauthenticated",
        "permission_denied",
        "conflict",
        "unavailable",
        "timeout",
        "internal",
    }
)
# Spec 01, CON-23: names kept for standard kinds that a later version of the
# specification may add, taken from gRPC's status codes. A receiver holds a
# kind it does not know without a detail, so these are not standard yet.
_RESERVED_KINDS = frozenset(
    {
        "cancelled",
        "unknown",
        "deadline_exceeded",
        "already_exists",
        "resource_exhausted",
        "failed_precondition",
        "aborted",
        "out_of_range",
        "unimplemented",
        "data_loss",
    }
)
# Spec 01, CON-30: how deep an untyped detail may nest. Far below the depth at
# which copy.deepcopy (about 490 levels of plain dicts at the default
# recursion limit) and pydantic_core.to_json (255) fail.
_UNTYPED_DEPTH = 32

_CONTRACT_ATTR = "__neolith_contract__"
_ACTION_ATTR = "__neolith_action__"
_UNIONS: tuple[object, ...] = (typing.Union, types.UnionType)
_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ContractDefinitionError(TypeError):
    """A contract, an action or an implementation breaks spec 01.

    Raised by ``@contract``, ``@action`` and ``@implements`` when they run, so
    the error surfaces when the module that defines the class is imported.
    """


class _FrozenMap(dict[str, Any]):
    """A ``dict`` that refuses changes through its own methods.

    It stays a ``dict``, so that JSON writers and equality with a plain
    ``dict`` work, and it is hashable, copyable and picklable. As with a frozen
    dataclass, calling ``dict``'s methods on it directly still changes it.
    """

    __slots__ = ()

    def __new__(cls, items: object = ()) -> "_FrozenMap":
        made = super().__new__(cls)
        dict.update(made, items)  # type: ignore[call-overload]  # checked by dict
        return made

    def __init__(self, items: object = ()) -> None:
        """Filled by ``__new__``, so calling this again changes nothing."""

    def _refuse(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError(f"{type(self).__name__[1:]} is read-only")

    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse

    def __hash__(self) -> int:  # type: ignore[override]  # dict sets it to None
        return hash(frozenset(self.items()))

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (dict(self),))

    def __copy__(self) -> "_FrozenMap":
        return self

    def __deepcopy__(self, memo: object) -> "_FrozenMap":
        # Immutable, and so is everything it holds.
        return self


class _FrozenList(list[Any]):
    """A ``list`` that refuses changes through its own methods (see _FrozenMap)."""

    __slots__ = ()

    def __new__(cls, items: object = ()) -> "_FrozenList":
        made = super().__new__(cls)
        list.extend(made, items)  # type: ignore[arg-type]  # checked by list
        return made

    def __init__(self, items: object = ()) -> None:
        """Filled by ``__new__``, so calling this again changes nothing."""

    def _refuse(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError(f"{type(self).__name__[1:]} is read-only")

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _refuse
    append = extend = insert = pop = remove = clear = sort = reverse = _refuse

    def __hash__(self) -> int:  # type: ignore[override]  # list sets it to None
        return hash(tuple(self))

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (list(self),))

    def __copy__(self) -> "_FrozenList":
        return self

    def __deepcopy__(self, memo: object) -> "_FrozenList":
        return self


def _freeze(value: object) -> object:
    """A frozen copy of a checked JSON value, at most _UNTYPED_DEPTH deep."""
    if type(value) is dict:
        return _FrozenMap({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return _FrozenList(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    """A plain copy of a frozen JSON value, as ``ContractError`` admits it."""
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return value


@final
class ContractError(Exception):
    """An error that crosses a contract (spec 01, Errors; ADR 0008).

    Every such error is a ``ContractError``, told apart by ``kind``: a
    standard kind, such as ``"not_found"`` or ``"conflict"``, or a kind the
    action declares. ``message`` is text for people. ``data`` is the detail:
    ``None`` for a standard kind; the kind's detail record for a declared
    kind. A caller holds a kind it does not know, declared by a newer version
    of the contract or made standard by a newer version of the specification,
    with the detail's fields as they arrived, or ``None`` if there are none.
    Those fields are a read-only copy: maps and lists that compare equal to
    ``dict`` and ``list`` and are written as JSON like them, but refuse
    changes. A record is held as given.

    The fields are read-only, and the class cannot be subclassed: a subclass
    would not survive a transport, so callers could tell the modes apart.
    Building one wrongly raises ``TypeError`` or ``ValueError``, never a
    ``ContractError``, so that code handling contract errors does not swallow
    a bug. A copy or a pickle carries the kind, message and detail only, not
    notes or other attributes.
    """

    __slots__ = ("_data", "_kind", "_message")
    _kind: str
    _message: str
    _data: BaseModel | Mapping[str, Any] | None

    def __init__(
        self,
        kind: str,
        message: str,
        data: BaseModel | Mapping[str, Any] | None = None,
    ) -> None:
        try:
            built = self._kind
        except AttributeError:
            pass
        else:
            raise TypeError(f"this ContractError is already built, as {built!r}")
        if type(kind) is not str:
            raise TypeError(f"a contract error's kind is a str, not {shown(kind)}")
        if not _KIND.fullmatch(kind):
            raise ValueError(
                f"{shown(kind)} is not an error kind: a kind starts with a lowercase "
                "ASCII letter, followed by lowercase ASCII letters, digits or "
                "underscores"
            )
        if type(message) is not str:
            raise TypeError(
                f"a contract error's message is a str, not {type(message).__name__}"
            )
        problem = text_problem(message)
        if problem is not None:
            raise ValueError(f"a contract error's message is Unicode text: {problem}")
        if kind in _STANDARD_KINDS:
            if data is not None:
                raise TypeError(
                    f"a {kind!r} error is of a standard kind, which carries no detail"
                )
        elif type(data) is dict:
            problem = json_value_problem(data, _UNTYPED_DEPTH)
            if problem is not None:
                raise TypeError(
                    f"the untyped detail of a {kind!r} error is a dict of JSON "
                    f"values, but {problem}"
                )
            data = typing.cast(_FrozenMap, _freeze(data))
        elif data is not None and not isinstance(data, BaseModel):
            raise TypeError(
                f"a {kind!r} error carries its detail record, or, for a kind the "
                "receiver does not know, an untyped dict of the detail's fields or "
                f"None; not {_name(type(data))}"
            )
        super().__init__(kind, message)
        self._kind = kind
        self._message = message
        self._data = data

    @property
    def kind(self) -> str:
        """The error's kind."""
        return self._kind

    @property
    def message(self) -> str:
        """Text for people. Callers branch on ``kind``, never on this."""
        return self._message

    @property
    def data(self) -> BaseModel | Mapping[str, Any] | None:
        """The detail; ``None`` for a standard kind."""
        return self._data

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError(
            "ContractError cannot be subclassed: every error that crosses a "
            "contract is a ContractError, told apart by its kind"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        # Kind, message and detail only: no notes, no other attributes, and the
        # detail is checked again when the copy is built.
        return (ContractError, (self._kind, self._message, _thaw(self._data)))

    def __str__(self) -> str:
        return f"{self._kind}: {self._message}"

    def __repr__(self) -> str:
        data = "" if self._data is None else f", data={self._data!r}"
        return f"ContractError({self._kind!r}, {self._message!r}{data})"


@dataclass(frozen=True, slots=True)
class Action:
    """One action of a contract: its name, idempotency, types and error kinds.

    ``reply`` is ``None`` when the reply is empty. ``errors`` maps each kind
    the action declares to its detail record, in declaration order; the
    standard kinds are not listed, since every action has them.
    """

    name: str
    idempotent: bool
    request: type[BaseModel]
    reply: type[BaseModel] | None
    errors: Mapping[str, type[BaseModel]]


class Contract:
    """What ``@contract`` records about a declaration class.

    Read it with ``Contract.of(Declaration)``.
    """

    __slots__ = ("_actions", "_description", "declaration", "identity", "major", "name")

    def __init__(
        self,
        identity: str,
        declaration: type[Any],
        actions: Mapping[str, Action],
        description: Mapping[str, Any],
    ) -> None:
        self.identity = identity
        self.name, self.major = _parse_identity(identity)
        self.declaration = declaration
        self._actions = types.MappingProxyType(dict(actions))
        self._description = description

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
        its request and reply, or ``None`` for an empty reply. Each record and
        enum is described once under ``$defs``, by its class name. The
        description is written from the same walk of Pydantic's core schema
        that admits the types, so it states what travels. The result is
        JSON-compatible, and a new copy each time.
        """
        return copy.deepcopy(dict(self._description))

    def _check_implementation(self, impl: type[Any]) -> None:
        where = f"{self.identity}, implementation {impl.__qualname__}"
        for klass in impl.__mro__:
            if klass is object or klass is self.declaration:
                continue
            for attr, value in vars(klass).items():
                function = getattr(value, "__func__", value)
                if inspect.isfunction(function) and _ACTION_ATTR in vars(function):
                    raise ContractDefinitionError(
                        f"{where}: {attr!r} is marked with @action; the contract "
                        "declares each action, its idempotency and its error "
                        "kinds, so implementation methods are not marked"
                    )
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
                # Anything callable from the class counts, so a partialmethod
                # or a singledispatchmethod is a public method too; a class,
                # such as a nested Meta or an exception alias, is not a method.
                member = getattr(impl, attr, None)
                if (callable(member) and not isinstance(member, type)) or isinstance(
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
        portable = Portable(ContractDefinitionError)
        actions: dict[str, Action] = {}
        described: dict[str, Any] = {}
        for attr, value in vars(declaration).items():
            marked = (
                vars(value).get(_ACTION_ATTR) if inspect.isfunction(value) else None
            )
            if marked is None:
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
            portable.where = f"{identity}, action {attr!r}"
            request, reply = _signature(portable.where, value)
            request_type, request_ref = portable.root(request, "request")
            reply_type, reply_ref = (
                (None, None) if reply is None else portable.root(reply, "reply")
            )
            idempotent, declared = marked
            where = f"{identity}, action {attr!r}"
            errors: dict[str, type[BaseModel]] = {}
            error_refs: dict[str, dict[str, str]] = {}
            for kind, detail in _declared_kinds(where, declared):
                portable.where = f"{where}, error {kind!r}"
                errors[kind], error_refs[kind] = portable.root(detail, "detail")
            actions[attr] = Action(
                attr,
                idempotent,
                request_type,
                reply_type,
                _FrozenMap(errors),
            )
            described[attr] = {
                "idempotent": idempotent,
                "request": request_ref,
                "reply": reply_ref,
                "errors": error_refs,
            }
        description = {
            "contract": identity,
            "jsonSchemaDialect": _DIALECT,
            "actions": described,
            "$defs": portable.defs,
        }
        setattr(
            declaration,
            _CONTRACT_ATTR,
            Contract(identity, declaration, actions, description),
        )
        return declaration

    return decorate


@overload
def action[F: Callable[..., Any]](function: F, /) -> F: ...


@overload
def action[F: Callable[..., Any]](
    *,
    idempotent: bool = False,
    errors: dict[str, type[BaseModel]] | None = None,
) -> Callable[[F], F]: ...


def action(
    function: object = None,
    /,
    *,
    idempotent: bool = False,
    errors: dict[str, type[BaseModel]] | None = None,
) -> Any:
    """Mark a method of a contract declaration as an action.

    Use ``@action`` or ``@action(idempotent=True)``. An action is
    non-idempotent unless declared idempotent.

    ``errors`` declares the action's own error kinds, each with its detail
    record, for example ``errors={"out_of_stock": OutOfStock}``. A kind is
    lowercase ASCII letters, digits and underscores, starting with a letter,
    and is not a standard kind. A detail record follows the rules of a request
    record. ``@contract`` checks them, naming the contract, the action and the
    kind.
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
        if _ACTION_ATTR in vars(method):
            raise ContractDefinitionError(
                f"{method.__qualname__} is marked with @action twice; mark it "
                "once, as @action(idempotent=..., errors=...)"
            )
        vars(method)[_ACTION_ATTR] = (idempotent, errors)
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


def _declared_kinds(where: str, errors: object) -> list[tuple[str, object]]:
    """Check the kinds given to ``@action(errors=...)``; return them in order.

    The detail records are checked by the caller, with the portable types.
    """
    if errors is None:
        return []
    if type(errors) is not dict:
        raise ContractDefinitionError(
            f"{where}: errors= takes a dict from each error kind to its detail "
            f"record, not {_name(type(errors))}"
        )
    for kind in errors:
        if type(kind) is not str or not _KIND.fullmatch(kind):
            raise ContractDefinitionError(
                f"{where}: error kind {shown(kind)} must be a str that starts with a "
                "lowercase ASCII letter, followed by lowercase ASCII letters, "
                "digits or underscores"
            )
        if kind in _STANDARD_KINDS:
            raise ContractDefinitionError(
                f"{where}: error kind {kind!r} is a standard kind, which every "
                "action has; declare only kinds of the action's own"
            )
        if kind in _RESERVED_KINDS:
            raise ContractDefinitionError(
                f"{where}: error kind {kind!r} is reserved for a standard kind "
                "that a later version of the specification may add; choose "
                "another name"
            )
    return list(errors.items())


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
    except Exception as error:  # a malformed string annotation, such as "R["
        raise ContractDefinitionError(
            f"{where}: cannot read its annotations ({type(error).__name__}: {error})"
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
