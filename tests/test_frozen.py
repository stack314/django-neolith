"""Every frozen value of the package stays the value it was built as.

A frozen class refuses ``value.field = x``, yet its ``__init__`` writes with
``object.__setattr__``, and so does the ``__setstate__`` of a frozen dataclass
with slots. Called again on a built value, either would change it under
whatever holds it: a call context, a cache key, the host's table. The
invariant: for every frozen type the package defines, a built value is the
same after ``__init__``, ``__setstate__``, ``copy.replace``, a copy and a
pickle are tried on it, and the copies are built as before.

The frozen types are found, not listed: each class of a ``neolith`` module,
subpackages included, that is a frozen dataclass or refuses ``__setattr__``
itself, besides the read-only types that refuse changes otherwise. A new one
fails the first test until it has an example below.
"""

import contextlib
import copy
import dataclasses
import importlib
import pickle
import pkgutil
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import BaseModel

import neolith
from neolith import Action, CallContext, ContractError, Principal, Provided
from neolith._context import Sent
from neolith._contracts import _FrozenList, _FrozenMap
from neolith._declaration import Declaration, Provision
from neolith._forms import _Form
from neolith._frozen import once
from neolith._host import Connector, Host
from neolith._server import _Request


class Ask(BaseModel):
    n: int = 0


class Told(BaseModel):
    n: int = 0


@dataclasses.dataclass(frozen=True)
class Case:
    """A built value, the arguments of another valid one, and a state to set."""

    make: Callable[[], object]
    again: tuple[tuple[Any, ...], dict[str, Any]]
    # What __setstate__ would write, for a type that has it.
    state: object = None
    # Whether the class can be called to build the other value.
    callable: bool = True


_ALICE = Principal("https://id.example", "alice", {"role": "admin"})
_CASES: dict[type, Case] = {
    Principal: Case(
        lambda: Principal("https://id.example", "alice", {"role": "admin"}),
        (("https://id.example", "mallory", {"role": "root"}), {}),
    ),
    CallContext: Case(
        lambda: CallContext(_ALICE, "a" * 32, "b" * 16, "", 5.0, "k", "shop", "s1"),
        ((None, "f" * 32, "e" * 16, "x=1", None, None, None, "s2"), {}),
        state=[None, "f" * 32, "e" * 16, "x=1", None, None, None, "s2"],
    ),
    Sent: Case(
        lambda: Sent(None, "a" * 32, "", 1000, "k", "shop"),
        ((b"p", "f" * 32, "x=1", None, "other", "admin"), {}),
        state=[b"p", "f" * 32, "x=1", None, "other", "admin"],
    ),
    Action: Case(
        lambda: Action("ask", True, Ask, Told),
        (("tell", False, Told, None), {}),
        state=["tell", False, Told, None, _FrozenMap()],
    ),
    Provided: Case(
        lambda: Provided(Ask, implementation="shop.impl.Shop", principal_from=()),
        ((Told,), {"implementation": "evil.Evil", "principal_from": ("x",)}),
    ),
    Provision: Case(
        lambda: Provision("shop.v1", "shop.impl.Shop", ()),  # type: ignore[arg-type]
        (("evil.v1", "evil.Evil", ("x",)), {}),
        state=["evil.v1", "evil.Evil", ("x",)],
    ),
    Declaration: Case(
        lambda: Declaration(object, "shop", "shop", (), (), (), ()),  # type: ignore[arg-type]
        ((int, "evil", "evil", (), (), ("x",), ()), {}),
        state=[int, "evil", "evil", (), (), ("x",), ()],
    ),
    Host: Case(
        lambda: Host(frozenset({"shop.v1"}), {}, {"shop": ()}),
        ((frozenset(), {}, {}, None, frozenset({"w"})), {}),
        state=[frozenset(), {}, {}, None, frozenset({"w"})],
    ),
    _Form: Case(
        lambda: _Form("Binary", bytes, bytes, str, {}),
        (("Evil", str, str, repr, {"x": 1}, True), {}),
    ),
    Connector: Case(
        lambda: Connector._made("shop.v1"),
        (("evil.v1",), {}),
        callable=False,
    ),
    ContractError: Case(
        lambda: ContractError("not_found", "no such order"),
        (("conflict", "other"), {}),
        state={"_kind": "conflict", "_message": "other"},
    ),
    _FrozenMap: Case(lambda: _FrozenMap({"a": 1}), (({"b": 2},), {})),
    _FrozenList: Case(lambda: _FrozenList([1]), (([2],), {})),
    _Request: Case(
        lambda: _Request("GET / HTTP/1.1", 1.0),
        (("POST /x HTTP/1.1", 2.0), {}),
        state=["POST /x HTTP/1.1", 2.0],
    ),
}


# Importing it raises, by design: the command line points Django at it while
# settings do not exist yet.
_RAISES = frozenset({"neolith._unset"})


def _found() -> Iterator[type]:
    for module in pkgutil.walk_packages(neolith.__path__, "neolith."):
        if module.name in _RAISES:
            continue
        for value in vars(importlib.import_module(module.name)).values():
            if not isinstance(value, type) or value.__module__ != module.name:
                continue
            params = vars(value).get("__dataclass_params__")
            if (params is not None and params.frozen) or "__setattr__" in vars(value):
                yield value


def _state(value: object) -> tuple[dict[str, Any], object]:
    """What the value holds: each bound slot and attribute, and its items."""
    held: dict[str, Any] = dict(getattr(value, "__dict__", {}))
    for klass in type(value).__mro__:
        slots = vars(klass).get("__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            with contextlib.suppress(AttributeError):
                held[name] = object.__getattribute__(value, name)
    items = (
        dict(value)
        if isinstance(value, dict)
        else list(value)
        if isinstance(value, list)
        else None
    )
    return held, items


def _same(value: object, before: tuple[dict[str, Any], object]) -> None:
    held, items = _state(value)
    assert held.keys() == before[0].keys()
    for name, kept in before[0].items():
        assert held[name] is kept, name
    assert items == before[1]


def test_every_frozen_type_has_a_case() -> None:
    missing = sorted(
        f"{klass.__module__}.{klass.__qualname__}"
        for klass in _found()
        if klass not in _CASES
    )
    assert not missing, (
        f"add a case for {missing}, and build each with neolith._frozen.once"
    )


@pytest.mark.parametrize("klass", list(_CASES), ids=lambda klass: klass.__name__)
def test_a_built_value_is_never_built_again(klass: type) -> None:
    case = _CASES[klass]
    args, kwargs = case.again
    if case.callable:
        # The other arguments are valid: only the value being built refuses.
        klass(*args, **kwargs)
    value = case.make()
    before = _state(value)
    # Refused, or changing nothing, as _FrozenMap's __init__ documents.
    with contextlib.suppress(TypeError):
        value.__init__(*args, **kwargs)  # type: ignore[misc]
    _same(value, before)
    if hasattr(value, "__setstate__"):
        assert case.state is not None, "give the case a state to set"
        with pytest.raises(TypeError):
            value.__setstate__(case.state)
        _same(value, before)


_RESTORED = [
    klass for klass in _CASES if hasattr(vars(klass).get("__setstate__"), "__wrapped__")
]


@pytest.mark.parametrize("klass", _RESTORED, ids=lambda klass: klass.__name__)
def test_a_value_is_not_restored_again_from_its_own_state(klass: type) -> None:
    # The state is read while the value is restored: a state that restores the
    # value from there would have it seen as one value, then written as another.
    held = _CASES[klass].state
    assert isinstance(held, list)
    state: list[object] = held
    unbuilt: Any = object.__new__(klass)
    refused: list[str] = []

    class State:
        def __iter__(self) -> Iterator[object]:
            try:
                unbuilt.__setstate__(state)
            except TypeError as error:
                refused.append(str(error))
            yield from state

    unbuilt.__setstate__(State())
    assert refused
    assert "already built" in refused[0]
    assert _state(unbuilt)[0].keys() == _state(_CASES[klass].make())[0].keys()


@once
class _Hooked:
    """A value that runs a hook before it writes its field, as Principal does."""

    __slots__ = ("name",)
    name: str

    def __init__(self, name: str, hook: Callable[[], object] = lambda: None) -> None:
        hook()
        object.__setattr__(self, "name", name)


def test_once_refuses_a_build_started_from_inside_the_build() -> None:
    unbuilt = _Hooked.__new__(_Hooked)
    with pytest.raises(TypeError, match="already built"):
        _Hooked.__init__(unbuilt, "first", lambda: unbuilt.__init__("second"))  # type: ignore[misc]
    # The refusal ended that build, so the value can be built once more.
    unbuilt.__init__("third")  # type: ignore[misc]
    assert unbuilt.name == "third"
    with pytest.raises(TypeError, match="already built"):
        unbuilt.__init__("fourth")  # type: ignore[misc]
    # Another value of the class can still be built from inside a build.
    inner = _Hooked.__new__(_Hooked)
    outer = _Hooked("outer", lambda: inner.__init__("inner"))  # type: ignore[misc]
    assert (outer.name, inner.name) == ("outer", "inner")


@pytest.mark.parametrize("klass", list(_CASES), ids=lambda klass: klass.__name__)
def test_copies_of_a_frozen_value_are_built_and_leave_it_as_it_was(
    klass: type,
) -> None:
    value = _CASES[klass].make()
    before = _state(value)
    made: list[object] = []
    routes: tuple[Callable[[object], object], ...] = (
        copy.copy,
        copy.deepcopy,
        lambda value: pickle.loads(pickle.dumps(value)),  # noqa: S301
    )
    for route in routes:
        try:
            made.append(route(value))
        except TypeError:
            # A connector is neither copied nor pickled, by design.
            assert klass is Connector
    if dataclasses.is_dataclass(value) and hasattr(copy, "replace"):
        first = dataclasses.fields(value)[0].name
        made.append(copy.replace(value, **{first: None}))
    for other in made:
        _same(value, before)
        assert type(other) is klass
        assert _state(other)[0].keys() == before[0].keys()
