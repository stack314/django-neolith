"""Frozen values that are built once and never built again in place.

A frozen class refuses ``value.field = x``, but its ``__init__`` writes the
fields with ``object.__setattr__``, so calling ``__init__`` again on a built
value changes that value. A frozen dataclass with ``slots=True`` has a second
such door: the ``__setstate__`` the decorator adds for pickling, which also
writes with ``object.__setattr__``. Both are in ``dataclasses`` itself
(``_field_assign`` and ``_dataclass_setstate``, Python 3.12 to 3.14).

``once`` closes both doors on a class: each refuses a value any of whose
fields is already bound, and runs as before on one that is not, so building,
copying, pickling and ``copy.replace`` work as they did, since each makes a
new value. Going around on purpose, with ``object.__setattr__``, is not
refused, as with any frozen dataclass.
"""

import dataclasses
import functools
import types
from collections.abc import Callable

__all__ = ["once"]


def _bound(value: object, name: str) -> bool:
    """Whether ``name`` is bound on ``value`` itself, not on its class."""
    held = getattr(value, "__dict__", None)
    if held is not None and name in held:
        return True
    slot = getattr(type(value), name, None)
    if isinstance(slot, types.MemberDescriptorType):
        try:
            slot.__get__(value, type(value))
        except AttributeError:
            return False
        return True
    return False


def _fields(cls: type) -> tuple[str, ...]:
    if dataclasses.is_dataclass(cls):
        return tuple(field.name for field in dataclasses.fields(cls))
    slots = vars(cls).get("__slots__", ())
    return (slots,) if isinstance(slots, str) else tuple(slots)


def _guarded(
    cls: type, method: Callable[..., None], names: tuple[str, ...]
) -> Callable[..., None]:
    @functools.wraps(method)
    def guarded(self: object, /, *args: object, **kwargs: object) -> None:
        if any(_bound(self, name) for name in names):
            raise TypeError(
                f"this {cls.__name__} is already built, and a frozen value is "
                "never built again"
            )
        method(self, *args, **kwargs)

    return guarded


def once[C: type](cls: C) -> C:
    """Refuse ``__init__`` and ``__setstate__`` on a value of ``cls`` already built.

    ``cls`` is a frozen dataclass, after ``@dataclass`` has made it, or a
    class whose ``__slots__`` hold its fields. Its value counts as built once
    any of those fields is bound.
    """
    names = _fields(cls)
    if not names:
        raise TypeError(f"{cls.__name__} has no fields for once() to guard")
    setattr(cls, "__init__", _guarded(cls, vars(cls)["__init__"], names))  # noqa: B010
    setstate = getattr(cls, "__setstate__", None)
    if setstate is not None:
        setattr(cls, "__setstate__", _guarded(cls, setstate, names))  # noqa: B010
    return cls
