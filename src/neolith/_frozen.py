"""Frozen values that are built once and never built again in place.

A frozen class refuses ``value.field = x``, but its ``__init__`` writes the
fields with ``object.__setattr__``, so calling ``__init__`` again on a built
value changes that value. A frozen dataclass with ``slots=True`` has a second
such door: the ``__setstate__`` the decorator adds for pickling, which also
writes with ``object.__setattr__``. Both are in ``dataclasses`` itself
(``_field_assign`` and ``_dataclass_setstate``, Python 3.12 to 3.15).

``once`` closes both doors on a class: each refuses a value whose first
field is already bound, or that is being built right now, and runs as
before on one that is neither, so building, copying, pickling and
``copy.replace`` work as they did, since each makes a new value. "Being
built" covers code the build itself runs, such as a ``claims`` mapping's
``__getitem__`` or a state's ``__iter__``: called back from there, either
door would build the value first and the outer build would then write it
again. Going around on purpose, with ``object.__setattr__`` or the wrapped
method's ``__wrapped__``, is not refused, as with any frozen dataclass.

One field is enough: a build that returns has bound every field, and one
that raises leaves a value that only its caller holds, since a build in
progress cannot be entered again. The field is found when the class is
decorated, so a build pays for one lookup, not one per field.
"""

import dataclasses
import functools
import threading
import types
from collections.abc import Callable

__all__ = ["once"]

# The ids of the values being built now. A value is alive while it is built,
# so its id is not reused in that time.
_BUILDING: set[int] = set()
_LOCK = threading.Lock()


def _probe(cls: type) -> Callable[[object], bool]:
    """Whether a value of ``cls`` has its first field bound on itself."""
    if dataclasses.is_dataclass(cls):
        name = next((field.name for field in dataclasses.fields(cls)), None)
    else:
        slots = vars(cls).get("__slots__", ())
        name = slots if isinstance(slots, str) else next(iter(slots), None)
    if name is None:
        raise TypeError(f"{cls.__name__} has no fields for once() to guard")
    slot = vars(cls).get(name)
    if isinstance(slot, types.MemberDescriptorType):
        read = slot.__get__

        def in_slot(value: object) -> bool:
            try:
                read(value)
            except AttributeError:
                return False
            return True

        return in_slot
    if "__slots__" in vars(cls):
        raise TypeError(f"{cls.__name__}.{name} is not a slot once() can read")

    def in_dict(value: object) -> bool:
        return name in vars(value)

    return in_dict


def _guarded(
    cls: type, method: Callable[..., None], built: Callable[[object], bool]
) -> Callable[..., None]:
    @functools.wraps(method)
    def guarded(self: object, /, *args: object, **kwargs: object) -> None:
        key = id(self)
        with _LOCK:
            if key in _BUILDING or built(self):
                raise TypeError(
                    f"this {cls.__name__} is already built, and a frozen value "
                    "is never built again"
                )
            _BUILDING.add(key)
        try:
            method(self, *args, **kwargs)
        finally:
            with _LOCK:
                _BUILDING.discard(key)

    return guarded


def once[C: type](cls: C) -> C:
    """Refuse ``__init__`` and ``__setstate__`` on a value of ``cls`` already built.

    ``cls`` is a frozen dataclass, after ``@dataclass`` has made it, or a
    class whose ``__slots__`` hold its fields. Its value counts as built once
    its first field is bound, which every ``__init__`` of it does.
    """
    built = _probe(cls)
    setattr(cls, "__init__", _guarded(cls, vars(cls)["__init__"], built))  # noqa: B010
    setstate = getattr(cls, "__setstate__", None)
    if setstate is not None:
        setattr(cls, "__setstate__", _guarded(cls, setstate, built))  # noqa: B010
    return cls
