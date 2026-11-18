"""Name and judge an object in a check without letting its own code replace
the refusal.

A check that refuses something names it: a value by its class, a class by its
name, an error by its class and message. Each of those reads can run code the
author wrote. A metaclass answers ``__qualname__``, ``__name__`` and
``__module__`` (and ``__getattribute__`` runs for every attribute of the
class); ``repr`` and ``str`` run the object's own methods; and a class or
function name, or what ``repr`` returns, may be a ``str`` subclass whose
``__format__`` or ``__len__`` runs code again. Any of these may raise, and an
exception raised while a refusal is written would escape in its place.

So every helper here reads a class's names through ``type``'s own
descriptors, which return what the class was built with; copies text to an
exact ``str`` before it is measured or formatted; bounds it; and never
raises. Each falls back to a fixed description when there is nothing it can
read.

A check that judges a value's class runs code the author wrote too:
``isinstance`` reads the value's own ``__class__``; ``in`` on a tuple of
classes runs the class's metaclass ``__eq__``, and on a set its
``__hash__``; ``issubclass`` against an ABC, such as ``BaseModel``, hashes
the class. ``is_a`` keeps what ``isinstance`` raises from replacing the
refusal; ``one_of`` and ``derives`` compare classes by identity alone.
"""

import types
import typing
from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "cause",
    "derives",
    "entry",
    "find",
    "full_name",
    "is_a",
    "module",
    "mro",
    "name",
    "namespace",
    "one_of",
    "qualname",
    "shown",
    "text",
    "type_name",
]

LIMIT: Final = 200
_QUALNAME: Final = type.__dict__["__qualname__"]
_NAME: Final = type.__dict__["__name__"]
_MODULE: Final = type.__dict__["__module__"]
_FUNCTION_QUALNAME: Final = types.FunctionType.__dict__["__qualname__"]
_MRO: Final = type.__dict__["__mro__"]
_NAMESPACE: Final = type.__dict__["__dict__"]


def _exact(found: object, limit: int | None) -> str | None:
    """``found`` as an exact ``str`` within ``limit``, or None if not text.

    ``str.__str__`` copies a ``str`` subclass to an exact ``str`` without
    calling anything the subclass defines.
    """
    if not issubclass(type(found), str):
        return None
    copied = str.__str__(found)
    if limit is None or len(copied) <= limit:
        return copied
    return f"{copied[: max(limit - 3, 0)]}..."


def _or(found: str | None, otherwise: str) -> str:
    return otherwise if found is None else found


def qualname(named: object, limit: int | None = LIMIT) -> str:
    """The qualified name of a class or function, as it was built.

    Args:
        named: A class or a function. Anything else is ``"<unnamed>"``.
        limit: The longest name kept whole; ``None`` keeps any length.

    Returns:
        The name, or ``"<unnamed>"`` if it is not text.
    """
    kind = type(named)
    if issubclass(kind, type):
        found = _QUALNAME.__get__(named)
    elif kind is types.FunctionType:
        found = _FUNCTION_QUALNAME.__get__(named)
    else:
        found = None
    return _or(_exact(found, limit), "<unnamed>")


def name(cls: type, limit: int | None = LIMIT) -> str:
    """A class's own ``__name__``, as it was built, never asking its metaclass.

    Args:
        cls: The class.
        limit: The longest name kept whole; ``None`` keeps any length.

    Returns:
        The name, or ``"<unnamed>"`` if ``cls`` is not a class.
    """
    found = _NAME.__get__(cls) if issubclass(type(cls), type) else None
    return _or(_exact(found, limit), "<unnamed>")


def module(cls: type) -> str | None:
    """The module a class names as its own, or None if that is not text.

    For a class defined in Python this is the ``__module__`` entry of its own
    namespace, which an author can set to anything.
    """
    if not issubclass(type(cls), type):
        return None
    try:
        found = _MODULE.__get__(cls)
    except Exception:  # a namespace without __module__
        return None
    return _exact(found, None)


def full_name(cls: type, limit: int | None = LIMIT) -> str:
    """``<module>.<qualname>`` of a class, as it was built.

    Args:
        cls: The class.
        limit: The longest result kept whole; ``None`` keeps any length.

    Returns:
        The dotted name; a module that is not text reads ``<unknown module>``.
    """
    dotted = f"{module(cls) or '<unknown module>'}.{qualname(cls, None)}"
    return _or(_exact(dotted, limit), dotted)


def is_a(value: object, kind: type) -> bool:
    """``isinstance(value, kind)``, or False if the value's own ``__class__``
    raises when ``isinstance`` reads it.

    ``isinstance`` reads ``__class__`` only after ``type(value)`` does not
    match, so a proxy such as Django's ``SimpleLazyObject`` is still judged by
    what it wraps.
    """
    try:
        return isinstance(value, kind)
    except Exception:  # the value's own __class__ may raise anything
        return False


def one_of(kind: object, kinds: Iterable[object]) -> bool:
    """Whether ``kind`` is one of ``kinds``, compared by identity.

    ``kind in kinds`` would run the metaclass ``__eq__`` (a tuple) or
    ``__hash__`` (a set) of ``kind``.
    """
    return any(kind is each for each in kinds)


def mro(cls: type) -> tuple[type, ...]:
    """A class's method resolution order, read without asking its metaclass."""
    return _MRO.__get__(cls)  # type: ignore[no-any-return]


def namespace(cls: object) -> Mapping[Any, Any]:
    """A class's own namespace, read without asking its metaclass. Its keys
    may be anything the author put there: look a name up with ``entry``.
    """
    return _NAMESPACE.__get__(cls)  # type: ignore[no-any-return]


def entry(held: Mapping[Any, object], name: str) -> object:
    """The entry ``name`` of a namespace or dict, or None, compared only with
    keys that are exactly ``str``, so that no key's own ``__eq__`` runs.
    """
    for key, value in held.items():
        if type(key) is str and key == name:
            return value
    return None


def find(cls: object, name: str) -> object:
    """``name`` as the class ``cls`` holds it, its own or inherited, or None.

    Like ``inspect.getattr_static`` on a class, but each class is read
    through ``type``'s own descriptors: ``getattr_static`` reads each
    class's ``__dict__`` through its metaclass.
    """
    for klass in mro(typing.cast(type, cls)):
        found = entry(namespace(klass), name)
        if found is not None:
            return found
    return None


def derives(kind: type, base: type) -> bool:
    """Whether ``base`` is in the method resolution order of the class
    ``kind``, compared by identity.

    ``issubclass(kind, base)`` against an ABC hashes ``kind``, which runs its
    metaclass ``__hash__``; a virtual subclass registered with the ABC is not
    counted.
    """
    return any(each is base for each in mro(kind))


def type_name(value: object, limit: int | None = LIMIT) -> str:
    """The qualified name of a value's class, as it was built."""
    return qualname(type(value), limit)


def shown(value: object, limit: int = LIMIT) -> str:
    """``repr`` of a value, bounded, or its class's name if ``repr`` fails.

    ``repr`` of an int with more than 4300 digits raises ValueError
    (``sys.set_int_max_str_digits``), and an author's ``__repr__`` may raise
    anything, or return a ``str`` subclass.
    """
    try:
        found: object = repr(value)
    except Exception:  # an author's __repr__ may raise anything
        found = None
    return _or(_exact(found, limit), f"<{type_name(value)} that cannot be shown>")


def _str(value: object) -> object:
    try:
        return str(value)
    except Exception:  # an author's __str__ may raise anything
        return None


def text(value: object, limit: int | None = LIMIT) -> str:
    """``str`` of a value, bounded, or a placeholder if ``str`` fails."""
    return _or(_exact(_str(value), limit), "<the text cannot be read>")


def cause(error: BaseException, limit: int | None = LIMIT) -> str:
    """An exception's class and message, ``<class>: <message>``, bounded."""
    message = _or(_exact(_str(error), limit), "<the message cannot be read>")
    return f"{type_name(error)}: {message}"
