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

import contextlib
import copy
import functools
import inspect
import math
import re
import types
import typing
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, NoReturn, Protocol, final, overload

from pydantic import BaseModel

from neolith._context import CallContext
from neolith._crossing import Codec
from neolith._portable import (
    Portable,
    json_value_problem,
    shown,
    text_problem,
)

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
# Spec 01, CON-30: how many values an untyped detail may hold in all, each
# occurrence of a shared value counted, so that a few shared lists cannot
# stand for millions of values.
_UNTYPED_SIZE = 10_000

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


def _private_copy(value: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Copy the maps and lists of an untyped detail; say if a bound is broken.

    Each map and list is read once, with ``dict.copy`` or ``list.copy``, and
    everything after that reads the copy, which nothing else can reach. So the
    check that follows sees exactly what is kept, even if another thread
    changes the caller's value meanwhile. Frozen maps and lists, as held by
    another error, are copied the same way. The walk stops at CON-30's depth
    and size, so a cycle or a value shared many times cannot run away.
    """
    containers = (dict, _FrozenMap, list, _FrozenList)
    top = dict.copy(value)
    pending: list[tuple[dict[Any, Any] | list[Any], int]] = [(top, 1)]
    size = 0
    while pending:
        container, depth = pending.pop()
        if depth > _UNTYPED_DEPTH:
            return top, (
                f"it nests maps and lists more than {_UNTYPED_DEPTH} levels deep"
            )
        slots: Iterable[tuple[Any, Any]] = (
            container.items() if type(container) is dict else enumerate(container)
        )
        for slot, item in list(slots):
            size += 1
            if size > _UNTYPED_SIZE:
                return top, f"it holds more than {_UNTYPED_SIZE} values"
            kind = type(item)
            if kind not in containers:
                continue
            copied = dict.copy(item) if issubclass(kind, dict) else list.copy(item)
            container[slot] = copied
            pending.append((copied, depth + 1))
    return top, None


def _untyped_copy(value: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """A private copy of an untyped detail, and why it breaks CON-30, if it does."""
    copied, problem = _private_copy(value)
    if problem is None:
        problem = json_value_problem(copied, _UNTYPED_DEPTH)
    return copied, problem


def untyped_problem(value: object) -> str | None:
    """Say why a value read from JSON is not an untyped detail (CON-30), or None."""
    if type(value) is not dict:
        return "it is not a map"
    return _untyped_copy(typing.cast(dict[str, Any], value))[1]


def mark_received(error: "ContractError") -> None:
    """Mark an error as one a connector raised, so a provider's host that sees
    it end an action knows the action did not raise it itself.
    """
    error._received = True


def is_received(error: "ContractError") -> bool:
    """Whether a connector raised this error (``mark_received``)."""
    return getattr(error, "_received", False) is True


def _rebuilt(
    kind: str,
    message: str,
    data: BaseModel | dict[str, Any] | None,
    received: bool,
) -> "ContractError":
    """A ``ContractError`` rebuilt from a copy or a pickle."""
    error = ContractError(kind, message, data)
    if received is True:
        mark_received(error)
    return error


def _freeze(value: object) -> object:
    """A frozen copy of a private, checked JSON value (at most 32 levels deep)."""
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
    action declares. A caller holds a kind it does not know, declared by a
    newer version of the contract or made standard by a newer version of the
    specification, with the detail's fields as they arrived, or ``None`` if
    there are none. Those fields are a read-only copy: maps and lists that
    compare equal to ``dict`` and ``list`` and are written as JSON like them,
    but refuse changes. A record is held as given.

    The fields are read-only, and the class cannot be subclassed: a subclass
    would not survive a transport, so callers could tell the modes apart.
    Building one wrongly never raises a ``ContractError``, so that code
    handling contract errors does not swallow a bug. A copy or a pickle
    carries the kind, message and detail, and whether a connector raised the
    error, but not notes or other attributes.

    Args:
        kind: A standard kind, or a kind the action declares: a lowercase
            ASCII letter, followed by lowercase ASCII letters, digits or
            underscores.
        message: Text for people.
        data: The detail: ``None`` for a standard kind; the kind's detail
            record for a declared kind; for a kind the receiver does not
            know, a ``dict`` of the detail's fields, or ``None``.

    Raises:
        TypeError: If ``kind`` or ``message`` is not a ``str``, if a standard
            kind is given a detail, or if ``data`` is not a record, a
            ``dict`` of JSON values within the limits of CON-30, or ``None``.
            Building an error a second time, or defining a subclass, raises
            it too.
        ValueError: If ``kind`` does not have the form of a kind, or
            ``message`` holds a lone surrogate.
    """

    __slots__ = ("_data", "_kind", "_message", "_received")
    _kind: str
    _message: str
    _data: BaseModel | dict[str, Any] | None
    _received: bool

    def __init__(
        self,
        kind: str,
        message: str,
        data: BaseModel | dict[str, Any] | None = None,
    ) -> None:
        if type(self) is not ContractError:
            raise TypeError(
                "ContractError cannot be subclassed: every error that crosses a "
                "contract is a ContractError, told apart by its kind"
            )
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
        elif type(data) is dict or type(data) is _FrozenMap:
            # Copy first, then check the copy, which nothing else can change.
            copied, problem = _untyped_copy(data)
            if problem is not None:
                raise TypeError(
                    f"the untyped detail of a {kind!r} error is a dict of JSON "
                    f"values, but {problem}"
                )
            data = typing.cast(_FrozenMap, _freeze(copied))
        elif issubclass(type(data), BaseModel):
            # Held as given. Whether its value travels within CON-30 (CON-31)
            # depends on the value, so the provider's host checks it as it
            # sends the error (step 3); a valid record never fails here.
            pass
        elif data is not None:
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
    def data(self) -> BaseModel | dict[str, Any] | None:
        """The detail; ``None`` for a standard kind."""
        return self._data

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError(
            "ContractError cannot be subclassed: every error that crosses a "
            "contract is a ContractError, told apart by its kind"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        # Kind, message and detail only, and whether a connector raised it: no
        # notes, no other attributes, and the detail is checked again when the
        # copy is built. A copy of a received error is still a received one.
        return (
            _rebuilt,
            (self._kind, self._message, _thaw(self._data), is_received(self)),
        )

    def __str__(self) -> str:
        try:
            return f"{self._kind}: {self._message}"
        except AttributeError:  # made with __new__ alone, never built
            return "<unbuilt ContractError>"

    def __repr__(self) -> str:
        try:
            kind, message, data = self._kind, self._message, self._data
        except AttributeError:
            return "<unbuilt ContractError>"
        # A record detail is held as given, so its repr may be long or raise.
        held = "" if data is None else f", data={shown(data, 1000)}"
        return f"ContractError({kind!r}, {shown(message, 1000)}{held})"


@dataclass(frozen=True, slots=True)
class Action:
    """One action of a contract: its name, idempotency, types and error kinds.

    Attributes:
        name: The action's name, which is the name of its method.
        idempotent: Whether the action is declared idempotent.
        request: The request record.
        reply: The reply record; ``None`` when the reply is empty.
        errors: Maps each kind the action declares to its detail record, in
            declaration order; the standard kinds are not listed, since every
            action has them.
    """

    name: str
    idempotent: bool
    request: type[BaseModel]
    reply: type[BaseModel] | None
    errors: Mapping[str, type[BaseModel]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A frozen dict, however the caller passed it, so Action stays
        # hashable, copyable and picklable.
        if type(self.errors) is not _FrozenMap:
            object.__setattr__(self, "errors", _FrozenMap(self.errors))


class Contract:
    """What ``@contract`` records about a declaration class.

    Read it with ``Contract.of(Declaration)``.

    Attributes:
        identity: The contract's identity, ``<name>.v<N>``, such as
            ``"inventory.v1"``.
        name: The name in the identity, such as ``"inventory"``.
        major: The major version in the identity, such as ``1``.
        declaration: The class marked with ``@contract``.
    """

    __slots__ = (
        "_actions",
        "_codecs",
        "_description",
        "declaration",
        "identity",
        "major",
        "name",
    )
    identity: str
    name: str
    major: int
    declaration: type[Any]

    def __init__(
        self,
        identity: str,
        declaration: type[Any],
        actions: Mapping[str, Action],
        description: Mapping[str, Any],
        codecs: Mapping[type[BaseModel], object],
    ) -> None:
        self.identity = identity
        self.name, self.major = _parse_identity(identity)
        self.declaration = declaration
        self._actions = types.MappingProxyType(dict(actions))
        self._description = description
        # The serializer and validator of each request, reply and detail
        # record, built from the core schema the description was written
        # from (spec 02, Values across a call).
        self._codecs = types.MappingProxyType(dict(codecs))

    @property
    def actions(self) -> Mapping[str, Action]:
        """The actions, by name, in declaration order."""
        return self._actions

    @classmethod
    def of(cls, declaration: object) -> "Contract":
        """Return the contract recorded on a class marked with ``@contract``.

        Raises:
            ContractDefinitionError: If ``declaration`` is not a class marked
                with ``@contract``.
        """
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
        """Raise ``ContractDefinitionError`` for the first problem found."""
        problems = self._implementation_problems(impl)
        if problems:
            raise ContractDefinitionError(problems[0])

    def _implementation_problems(self, impl: type[Any]) -> list[str]:
        """Every problem of an implementation class, in a fixed order.

        Only the implementation's own classes are read, never a contract
        declaration it inherits from, and only statically: no attribute is
        looked up through its descriptor or its __getattr__, so a lazy object
        on the class is never evaluated. Each step adds to one list, and a
        step that raises adds one problem for itself: the problems found
        before it stay, and the steps after it run.
        """
        where = f"{self.identity}, implementation {_qualname(impl)}"
        problems: list[str] = []
        own: list[type] = []
        try:
            own = [
                klass
                for klass in impl.__mro__
                if klass is not object and _CONTRACT_ATTR not in vars(klass)
            ]
        except Exception as error:  # an __mro__ or __dict__ it cannot read
            problems.append(f"{where}: its classes cannot be read ({_cause(error)})")
        for klass in own:
            try:
                self._marks(where, klass, problems)
            except Exception as error:
                problems.append(
                    f"{where}: {_qualname(klass)} cannot be read ({_cause(error)})"
                )
        for name, item in self._actions.items():
            try:
                self._action_problems(where, impl, name, item, problems)
            except Exception as error:
                problems.append(
                    f"{where}: action {name!r} cannot be read ({_cause(error)})"
                )
        for klass in own:
            try:
                self._public_methods(where, klass, problems)
            except Exception as error:
                problems.append(
                    f"{where}: {_qualname(klass)} cannot be read ({_cause(error)})"
                )
        return problems

    def _marks(self, where: str, klass: type, problems: list[str]) -> None:
        for attr, value in vars(klass).items():
            if _carries_mark(value):
                problems.append(
                    f"{where}: {attr!r} is marked with @action; the contract "
                    "declares each action, its idempotency and its error "
                    "kinds, so implementation methods are not marked"
                )

    def _action_problems(
        self,
        where: str,
        impl: type[Any],
        name: str,
        item: "Action",
        problems: list[str],
    ) -> None:
        """Add the problems of one action; may raise, keeping what it added."""
        member = inspect.getattr_static(impl, name, None)
        # A declaration's own method, inherited from this contract or another
        # one, is a stub, not an implementation.
        if member is None or _carries_mark(member):
            problems.append(f"{where}: action {name!r} is missing")
            # Nothing of this action is left to read.
            return
        if type(member) is not types.FunctionType:
            problems.append(
                f"{where}: action {name!r} must be an instance method taking "
                "(self, request) or (self, request, ctx: CallContext), not "
                f"a {type(member).__name__}"
            )
            # Only a function has the signature the checks below read.
            return
        try:
            request, reply, _ = _signature(f"{where}, action {name!r}", member)
        except ContractDefinitionError as error:
            problems.append(str(error))
            # The checks below compare the types this read did not give.
            return
        if request is not item.request:
            problems.append(
                f"{where}: action {name!r} takes {_name(request)}, but "
                f"{self.identity} declares {_name(item.request)}"
            )
        if item.reply is None and reply is not None:
            problems.append(
                f"{where}: action {name!r} returns {_name(reply)}, but "
                f"{self.identity} declares an empty reply; annotate it -> None"
            )
        elif reply is not item.reply:
            problems.append(
                f"{where}: action {name!r} returns {_name(reply)}, but "
                f"{self.identity} declares {_name(item.reply)}"
            )

    def _public_methods(self, where: str, klass: type, problems: list[str]) -> None:
        for attr, value in vars(klass).items():
            if attr.startswith("_") or attr in self._actions:
                continue
            if _is_method(value):
                problems.append(
                    f"{where}: public method {attr!r} is not an action of "
                    f"{self.identity}; make it private (prefix it with '_') or "
                    "move it out of the class"
                )

    def __repr__(self) -> str:
        return f"<Contract {self.identity}>"


def contract[T](identity: str, /) -> Callable[[type[T]], type[T]]:
    """Mark a ``typing.Protocol`` class as the declaration of a contract.

    The class holds only methods marked with ``@action``; names starting with
    ``_`` are ignored.

    Args:
        identity: ``<name>.v<N>``, for example ``"inventory.v1"``.

    Returns:
        A decorator that takes the class and returns it unchanged.

    Raises:
        ContractDefinitionError: If ``identity`` is not ``<name>.v<N>``, or,
            when the class is decorated, if the class, an action or a type it
            names breaks spec 01.
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
        codecs: dict[type[BaseModel], Codec[Any]] = {}

        def capture(record: type[BaseModel]) -> None:
            if record not in codecs:
                try:
                    codecs[record] = Codec(
                        record, *portable.built[record], portable.defs
                    )
                except TypeError as error:
                    raise portable.fail(str(error)) from error

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
            request, reply, _ = _signature(portable.where, value, declared=True)
            request_type, request_ref = portable.root(request, "request")
            capture(request_type)
            reply_type, reply_ref = (
                (None, None) if reply is None else portable.root(reply, "reply")
            )
            if reply_type is not None:
                capture(reply_type)
            idempotent, declared = marked
            where = f"{identity}, action {attr!r}"
            errors: dict[str, type[BaseModel]] = {}
            error_refs: dict[str, dict[str, str]] = {}
            for kind, detail in _declared_kinds(where, declared):
                portable.where = f"{where}, error {kind!r}"
                errors[kind], error_refs[kind] = portable.root(detail, "detail")
                capture(errors[kind])
                if _nests_deeper(portable.defs, error_refs[kind], _UNTYPED_DEPTH):
                    raise portable.fail(
                        f"detail record {errors[kind].__name__} can nest maps and "
                        f"lists more than {_UNTYPED_DEPTH} levels deep, so a caller "
                        "that does not know the kind could not hold it untyped; "
                        "a recursive record always can"
                    )
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
            Contract(identity, declaration, actions, description, codecs),
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

    Use ``@action`` or ``@action(idempotent=True)``.

    Args:
        function: The method, when the decorator is used bare, as ``@action``.
        idempotent: Whether the action is idempotent. An action is
            non-idempotent unless declared idempotent.
        errors: The action's own error kinds, a ``dict`` from each kind to
            its detail record, for example
            ``errors={"out_of_stock": OutOfStock}``. A kind is lowercase ASCII
            letters, digits and underscores, starting with a letter; it is
            neither a standard kind nor a name reserved for a later standard
            kind (CON-23). A detail record follows the rules of a request
            record, and its values nest at most 32 levels deep in records,
            maps and lists, the detail record being the first (CON-24).
            ``@contract`` checks them, naming the contract, the action and
            the kind.

    Returns:
        The method, marked, when used bare; otherwise a decorator that marks
        the method and returns it.

    Raises:
        ContractDefinitionError: If ``idempotent`` is not a ``bool``, if what
            is marked is not a function, or if it is marked twice.
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
    (``None`` for an empty reply). It returns the reply, so it is neither an
    ``async def`` nor a generator function. An action that reads the call
    context takes it after the request, as ``(self, request, ctx:
    CallContext)``. Any other public method is an error; helpers start with
    ``_``.

    Args:
        declaration: The class marked with ``@contract``.

    Returns:
        A decorator that takes the class and returns it unchanged.

    Raises:
        ContractDefinitionError: If ``declaration`` is not a contract, or,
            when the class is decorated, if the class does not match the
            contract.
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


# Descriptors whose class-level value is a callable: a public method, even
# though the object itself is not callable.
_METHOD_DESCRIPTORS = (
    staticmethod,
    classmethod,
    functools.partialmethod,
    functools.singledispatchmethod,
)


def _is_method(value: object) -> bool:
    """True if a class attribute is a method, judged by its type alone.

    A class, such as a nested Meta or an exception alias, is not a method.
    Nothing is looked up on the value: ``callable`` reads only its type, and
    ``isinstance`` would read a lazy object's ``__class__``, which evaluates
    it.
    """
    kind = type(value)
    if issubclass(kind, type):
        return False
    return callable(value) or issubclass(kind, _METHOD_DESCRIPTORS)


def _carries_mark(value: object) -> bool:
    """True if ``value`` is, or wraps, a function marked with ``@action``.

    Follows staticmethod, classmethod, property, partialmethod,
    singledispatchmethod and cached_property by their known attributes, and
    any ``__wrapped__`` chain (functools.wraps, functools.cache) found
    statically. A descriptor of another kind that hides a function is not
    followed.
    """
    pending: list[object] = [value]
    seen: set[int] = set()
    while pending and len(seen) < 64:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        kind = type(item)
        if kind is types.FunctionType:
            if _ACTION_ATTR in vars(item):
                return True
        elif issubclass(kind, staticmethod | classmethod):
            pending.append(typing.cast(Any, item).__func__)
        elif issubclass(kind, property):
            held = typing.cast(property, item)
            pending.extend(f for f in (held.fget, held.fset, held.fdel) if f)
        elif issubclass(
            kind,
            functools.partialmethod
            | functools.singledispatchmethod
            | functools.cached_property,
        ):
            pending.append(typing.cast(Any, item).func)
        wrapped = inspect.getattr_static(item, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
    return False


def _nests_deeper(
    defs: Mapping[str, Any], schema: Mapping[str, Any], limit: int
) -> bool:
    """True if a value the description admits can nest deeper than ``limit``.

    Reads the description the contract publishes: each object (a record or a
    map) and each array is one level. The height of each record is worked out
    once, so the cost grows with the description, not with how often a record
    is reused. A record that refers back to itself admits values of any depth.
    """
    heights: dict[str, float] = {}
    open_records: set[str] = set()

    def record(name: str) -> float:
        if name in heights:
            return heights[name]
        if name in open_records:
            return math.inf
        open_records.add(name)
        found = height(defs[name])
        open_records.discard(name)
        heights[name] = found
        return found

    def height(node: Mapping[str, Any]) -> float:
        ref = node.get("$ref")
        if isinstance(ref, str):
            return record(ref.rsplit("/", 1)[-1])
        children: list[Any] = [
            *node.get("properties", {}).values(),
            *node.get("anyOf", []),
            *(node[key] for key in ("items", "additionalProperties") if key in node),
        ]
        below = max(
            (height(child) for child in children if isinstance(child, Mapping)),
            default=0,
        )
        return below + (1 if node.get("type") in ("object", "array") else 0)

    return height(schema) > limit


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


# Names a connector call takes for its options (ADR 0017 Decision 3), so a
# declaration's request may not take them (spec 01, CON-50).
_RESERVED_PARAMETERS = frozenset({"context", "timeout"})


def _probe() -> Iterator[None]:
    yield


async def _async_probe() -> AsyncIterator[None]:
    yield


_CONTEXT_MANAGER_CODES = frozenset(
    {
        contextlib.contextmanager(_probe).__code__,
        contextlib.asynccontextmanager(_async_probe).__code__,
    }
)


def _signature(
    where: str, function: Callable[..., Any], *, declared: bool = False
) -> tuple[object, object, bool]:
    """Return the request and reply annotations, and whether the function
    takes the call context; the reply is ``None`` when it is empty.

    ``declared`` marks an action's declaration, which takes the request alone
    and may not name it as a connector call names its options. An
    implementation may take the call context after the request, as a
    parameter annotated ``CallContext`` (ADR 0017 Decision 2).
    """
    if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
        raise ContractDefinitionError(
            f"{where}: actions are synchronous in this version"
        )
    # A generator function's call returns a generator and runs none of its
    # body, so it never returns the reply. That includes one that
    # types.coroutine made awaitable. Only the function's own code is read: a
    # wrapper's body decides what its call returns.
    if inspect.isgeneratorfunction(function):
        raise ContractDefinitionError(
            f"{where}: it is a generator function, so a call returns a "
            "generator, not the reply; return the reply instead of yielding it"
        )
    # contextlib's two decorators return a plain function whose call always
    # returns a context manager. Every function they return shares one code
    # object, so that code identifies them.
    if function.__code__ in _CONTEXT_MANAGER_CODES:
        raise ContractDefinitionError(
            f"{where}: it is wrapped by contextlib's contextmanager, so a call "
            "returns a context manager, not the reply; return the reply instead"
        )
    try:
        signature = inspect.signature(function)
    except Exception as error:  # a __signature__ or __wrapped__ it cannot read
        raise ContractDefinitionError(
            f"{where}: cannot read its signature ({_cause(error)})"
        ) from error
    parameters = list(signature.parameters.values())
    if len(parameters) not in ((2,) if declared else (2, 3)) or any(
        p.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
        or p.default is not inspect.Parameter.empty
        for p in parameters
    ):
        raise ContractDefinitionError(
            f"{where}: an action takes exactly (self, request)"
            + ("" if declared else " or (self, request, ctx: CallContext)")
            + f", not {signature}"
        )
    if declared and parameters[1].name in _RESERVED_PARAMETERS:
        raise ContractDefinitionError(
            f"{where}: its request is named {parameters[1].name!r}, which a "
            "connector call keeps for its own option; name it otherwise, such "
            "as 'request'"
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
            f"{where}: cannot read its annotations ({_cause(error)})"
        ) from error
    request = hints.get(parameters[1].name)
    if request is None:
        raise ContractDefinitionError(f"{where}: annotate its request type")
    takes_context = len(parameters) == 3
    if takes_context and hints.get(parameters[2].name) is not CallContext:
        raise ContractDefinitionError(
            f"{where}: its third parameter, {parameters[2].name!r}, is not "
            "annotated CallContext; an action takes (self, request) or "
            "(self, request, ctx: CallContext)"
        )
    if "return" not in hints:
        raise ContractDefinitionError(
            f"{where}: annotate its reply type, or -> None for an empty reply"
        )
    reply = hints["return"]
    return request, (None if reply is type(None) else reply), takes_context


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
        return _qualname(annotation)
    try:
        return repr(annotation)
    except Exception:  # a repr that raises still names the value's type
        return f"<{type(annotation).__name__}>"


def _qualname(cls: type) -> str:
    """A class's own qualified name, read statically, never raising."""
    try:
        found = type.__dict__["__qualname__"].__get__(cls)
    except Exception:
        found = None
    return found if type(found) is str else "<a class>"


def _cause(error: BaseException) -> str:
    """An error's class and message, never raising."""
    try:
        text = str(error)
    except Exception:
        text = "<the message cannot be read>"
    return f"{type(error).__name__}: {text}"
