"""How values cross a call: as JSON text, through the declared type (spec 02).

These tests drive the crossing itself, below any transport: the caller's
request check (ADR 0017 Decision 9, step 2) and reading a value on the other
side. The transport and connector that use it, and the description check
between the dump and the read-back, arrive with #10 tasks 3 and 4.
"""

import json
import re
import types
from collections.abc import Generator, Iterator
from typing import Annotated, Any, Protocol, cast

import pydantic.plugin._loader
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    create_model,
)
from pydantic_core import SchemaSerializer

from neolith import Contract, ContractDefinitionError, action, contract
from neolith._crossing import check_request, codec
from tests.test_contract_round_trip import (
    _FORMAT_CHECKER,
    _HYPOTHESIS,
    _TYPES,
    Reference,
    _name,
    _records,
)


class Part(BaseModel):
    sku: str
    quantity: int = 1


class Reserve(BaseModel):
    order: int
    parts: list[Part] = []
    tags: dict[str, list[str]] = {}
    note: Part | None = None


class Reserved(BaseModel):
    ok: bool
    parts: list[Part] = []


class OutOfStock(BaseModel):
    sku: str


@contract("crossing.v1")
class Inventory(Protocol):
    @action(errors={"out_of_stock": OutOfStock})
    def reserve(self, request: Reserve) -> Reserved: ...


INVENTORY = Contract.of(Inventory)
RESERVE = INVENTORY.actions["reserve"]


def _request() -> Reserve:
    return Reserve(
        order=7,
        parts=[Part(sku="a"), Part(sku="b", quantity=2)],
        tags={"x": ["y"]},
        note=Part(sku="n"),
    )


def _containers(value: object) -> Iterator[object]:
    """Every record, list and map reachable from a value: what could be shared."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, BaseModel):
            yield item
            pending.extend(vars(item).values())
        elif isinstance(item, list | dict):
            yield item
            pending.extend(item.values() if isinstance(item, dict) else item)


def _shared(sent: object, received: object) -> set[int]:
    return {id(c) for c in _containers(sent)} & {id(c) for c in _containers(received)}


# TRN-12: the receiver reads the text alone, into new values.


@pytest.mark.spec("TRN-12")
def test_a_request_crosses_as_an_equal_value_that_shares_nothing() -> None:
    request = _request()
    received = codec(INVENTORY, Reserve).read(
        check_request(INVENTORY, RESERVE, request)
    )
    assert type(received) is Reserve
    assert received == request
    assert received is not request
    assert _shared(request, received) == set()


@pytest.mark.spec("TRN-12")
def test_changes_on_either_side_after_the_call_do_not_reach_the_other() -> None:
    request = _request()
    text = check_request(INVENTORY, RESERVE, request)
    received = codec(INVENTORY, Reserve).read(text)
    request.parts[0].sku = "changed"
    request.tags["x"].append("z")
    received.parts.append(Part(sku="c"))
    assert received.parts[0].sku == "a"
    assert received.tags == {"x": ["y"]}
    assert len(request.parts) == 2
    # The text that passed the check is bytes, so nothing changes it later.
    assert codec(INVENTORY, Reserve).read(text) == _request()


@pytest.mark.spec("TRN-10", "TRN-12")
def test_replies_and_detail_records_cross_the_same_way() -> None:
    reply = Reserved(ok=True, parts=[Part(sku="a")])
    replies = codec(INVENTORY, Reserved)
    received = replies.read(replies.write(reply, "the reply"))
    assert received == reply
    assert _shared(reply, received) == set()
    detail = OutOfStock(sku="a")
    details = codec(INVENTORY, OutOfStock)
    assert details.read(details.write(detail, "the detail")) == detail


@pytest.mark.spec("TRN-12", "CON-6", "CON-7")
@pytest.mark.parametrize("record", [Reserve, Reserved])
def test_unknown_fields_are_ignored_when_reading(record: type[BaseModel]) -> None:
    text = b'{"order": 1, "ok": true, "added_later": {"a": [1]}}'
    received = codec(INVENTORY, record).read(text)
    assert type(received) is record
    assert "added_later" not in vars(received)
    assert received.__pydantic_extra__ is None


@pytest.mark.spec("TRN-12")
def test_defaults_read_on_the_receiving_side_are_not_shared() -> None:
    # A field the text leaves out takes its default, which Pydantic copies
    # when it is mutable, so two received values never share one.
    reader = codec(INVENTORY, Reserve)
    first, second = reader.read(b'{"order": 1}'), reader.read(b'{"order": 1}')
    assert first.parts is not second.parts
    assert first.tags is not second.tags


# TRN-10: the declared type's rules, never the value's own methods.


class Overriding(BaseModel):
    """A record that overrides every way a value has to write or read itself."""

    n: int | None = None
    parts: list[Part] = []

    def model_dump_json(self, **kwargs: object) -> str:
        return "{}"

    def model_dump(self, **kwargs: object) -> dict[str, Any]:
        return {}

    def __iter__(self) -> Generator[tuple[str, Any]]:
        yield from ()

    def __getstate__(self) -> dict[Any, Any]:
        return {}

    @classmethod
    def model_validate_json(cls, *args: object, **kwargs: object) -> "Overriding":
        return cls.model_construct(n=-1)

    @classmethod
    def model_validate(cls, *args: object, **kwargs: object) -> "Overriding":
        return cls.model_construct(n=-1)


@contract("overriding.v1")
class OverridingContract(Protocol):
    @action
    def send(self, request: Overriding) -> Overriding: ...


@pytest.mark.spec("TRN-10", "TRN-12", "TRN-13")
def test_a_records_own_methods_do_not_change_what_crosses() -> None:
    declared = Contract.of(OverridingContract)
    request = Overriding(n=3, parts=[Part(sku="a")])
    text = check_request(declared, declared.actions["send"], request)
    assert json.loads(text) == {"n": 3, "parts": [{"sku": "a", "quantity": 1}]}
    received = codec(declared, Overriding).read(text)
    assert vars(received) == {"n": 3, "parts": [Part(sku="a")]}


class Late(BaseModel):
    n: int
    part: Part


@contract("late.v1")
class LateContract(Protocol):
    @action
    def send(self, request: Late) -> None: ...


class Spoof:
    """Validates as nothing a record would, wherever it is used."""

    def validate_json(self, *args: object, **kwargs: object) -> object:
        return Part.model_construct(sku=1)

    validate_python = validate_json


@pytest.mark.spec("TRN-10", "TRN-12")
@pytest.mark.parametrize("record", [Late, Part], ids=["request", "nested"])
@pytest.mark.parametrize(
    ("attribute", "replacement"),
    [
        ("__pydantic_serializer__", SchemaSerializer({"type": "any"})),
        ("__pydantic_serializer__", Reserved.__pydantic_serializer__),
        ("__pydantic_validator__", Reserved.__pydantic_validator__),
        ("__pydantic_validator__", Spoof()),
    ],
)
def test_nothing_set_on_a_record_class_after_declaration_changes_what_crosses(
    monkeypatch: pytest.MonkeyPatch,
    record: type[BaseModel],
    attribute: str,
    replacement: object,
) -> None:
    declared = Contract.of(LateContract)
    value = Late(n=1, part=Part(sku="a"))
    monkeypatch.setattr(record, attribute, replacement)
    text = check_request(declared, declared.actions["send"], value)
    assert json.loads(text) == {"n": 1, "part": {"sku": "a", "quantity": 1}}
    assert codec(declared, Late).read(text) == value


class WrapsAValidator:
    """Exposes a validator as Pydantic's plugin wrapper does."""

    def __init__(self, validator: object) -> None:
        self.__pydantic_schema_validator__ = validator


def _declare(record: type[BaseModel]) -> Contract:
    def send(self: object, request: record) -> None: ...  # type: ignore[valid-type]

    declaration = types.new_class(
        _name("D"), (Protocol,), exec_body=lambda ns: ns.update(send=action(send))
    )
    contract(f"{_name('declared')}.v1")(declaration)
    return Contract.of(declaration)


def _holding(inner: type[BaseModel]) -> type[BaseModel]:
    """A record with one field of another record."""
    return create_model(_name("Holder"), inner=(inner, ...))


@pytest.mark.spec("TRN-10", "TRN-12", "CON-17")
@pytest.mark.parametrize("nested", [False, True], ids=["request", "nested"])
@pytest.mark.parametrize(
    ("attribute", "replacement", "what"),
    [
        ("__pydantic_serializer__", Reserved.__pydantic_serializer__, "serializer"),
        ("__pydantic_serializer__", object(), "serializer"),
        ("__pydantic_validator__", Reserved.__pydantic_validator__, "validator"),
        (
            "__pydantic_validator__",
            WrapsAValidator(Reserved.__pydantic_validator__),
            "validator",
        ),
    ],
)
def test_a_record_that_pydantic_core_would_reuse_otherwise_is_refused(
    nested: bool, attribute: str, replacement: object, what: str
) -> None:
    # pydantic-core reuses, for each record a schema names, what is stored on
    # its class, so a record whose stored serializer or validator was not
    # built from its own core schema is refused when it is declared.
    class Odd(BaseModel):
        n: int

    setattr(Odd, attribute, replacement)
    record = _holding(Odd) if nested else Odd
    with pytest.raises(
        ContractDefinitionError,
        match=f"record Odd has a {what} that Pydantic did not build from its core",
    ):
        _declare(record)


@pytest.mark.spec("TRN-10", "TRN-12")
@pytest.mark.parametrize("nested", [False, True], ids=["request", "nested"])
@pytest.mark.parametrize(
    "replacement", [object(), Spoof(), WrapsAValidator(object())], ids=repr
)
def test_a_stored_validator_pydantic_core_never_reuses_is_ignored(
    nested: bool, replacement: object
) -> None:
    # The nearest legitimate shape: nothing there that pydantic-core would
    # take, so neolith's own validator, built from the schema, reads.
    class Odd(BaseModel):
        n: int

    Odd.__pydantic_validator__ = replacement  # type: ignore[assignment]
    record = _holding(Odd) if nested else Odd
    declared = _declare(record)
    value = (
        record.model_construct(inner=Odd.model_construct(n=1))
        if nested
        else (Odd.model_construct(n=1))
    )
    text = check_request(declared, declared.actions["send"], value)
    received = codec(declared, record).read(text)
    assert json.loads(text) == ({"inner": {"n": 1}} if nested else {"n": 1})
    assert type(received) is record


@pytest.mark.spec("TRN-10", "TRN-12")
def test_a_pydantic_plugin_does_not_see_or_change_what_crosses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a Pydantic plugin installed, every record's validator is Pydantic's
    # wrapper, which runs the plugin's handlers when it reads JSON. neolith
    # reads with its own validator, so none of them runs.
    seen: list[object] = []

    class Handler:
        def on_enter(self, *args: object, **kwargs: object) -> None:
            seen.append(args)

        def on_success(self, result: object) -> None:
            seen.append(result)
            if isinstance(result, BaseModel):
                object.__setattr__(result, "n", "changed")

    class Plugin:
        def new_schema_validator(
            self, *args: object, **kwargs: object
        ) -> tuple[Any, ...]:
            return None, Handler(), Handler()

    monkeypatch.setattr(pydantic.plugin._loader, "_plugins", {"probe": Plugin()})

    class Watched(BaseModel):
        n: int

    class Holds(BaseModel):
        watched: Watched

    assert type(Watched.__pydantic_validator__).__name__ == "PluggableSchemaValidator"
    seen.clear()
    for record, value in (
        (Watched, Watched(n=1)),
        (Holds, Holds(watched=Watched(n=1))),
    ):
        seen.clear()
        declared = _declare(record)
        text = check_request(declared, declared.actions["send"], value)
        assert codec(declared, record).read(text) == value
        assert seen == []


# TRN-10: a record class keeps what Pydantic reads from its values.


class Issue36(BaseModel):
    x: int
    _cache: int = PrivateAttr(0)

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        if name == "model_post_init":

            def changed(context: object, /) -> None:
                object.__setattr__(self, "x", "not an int")

            return changed
        return super().__getattribute__(name)


class ReadsDict(BaseModel):
    n: int

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        return super().__getattribute__(name)


class MissingAttribute(BaseModel):
    n: int

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return 1


class Constructs(BaseModel):
    n: int

    def __new__(cls, *args: object, **kwargs: object) -> "Constructs":
        return super().__new__(cls)


class Mixin:
    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return 1


class MixinAfter(BaseModel, Mixin):
    n: int


class Base(BaseModel):
    n: int

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        return super().__getattribute__(name)


class FromBase(Base):
    pass


def _slot_shadow(name: str) -> type[BaseModel]:
    return create_model(
        _name("Shadow"), __base__=type(_name("Base"), (BaseModel,), {name: property()})
    )


@pytest.mark.spec("TRN-10", "TRN-12", "CON-17")
@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (Issue36, "defines __getattribute__"),
        (ReadsDict, "defines __getattribute__"),
        (MissingAttribute, "defines __getattr__"),
        (Constructs, "defines __new__"),
        (MixinAfter, "defines __getattr__ in Mixin"),
        (FromBase, "defines __getattribute__ in Base"),
        *(
            (_slot_shadow(name), f"defines {name}")
            for name in (
                "__dict__",
                "__class__",
                "__pydantic_extra__",
                "__pydantic_fields_set__",
                "__pydantic_private__",
            )
        ),
    ],
)
@pytest.mark.parametrize("nested", [False, True], ids=["request", "nested"])
def test_a_record_class_that_replaces_what_pydantic_reads_is_refused(
    record: type[BaseModel], refusal: str, nested: bool
) -> None:
    with pytest.raises(ContractDefinitionError, match=re.escape(refusal)):
        _declare(_holding(record) if nested else record)


class Ordinary(BaseModel):
    """Defines methods Pydantic does not read from a value to write or read it."""

    n: int

    def __repr__(self) -> str:
        return "Ordinary"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ordinary) and other.n == self.n

    __hash__ = None  # type: ignore[assignment]

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)

    def total(self) -> int:
        return self.n


@pytest.mark.spec("TRN-10", "TRN-12")
def test_a_record_class_with_methods_pydantic_does_not_read_crosses() -> None:
    declared = _declare(Ordinary)
    text = check_request(declared, declared.actions["send"], Ordinary(n=2))
    assert codec(declared, Ordinary).read(text) == Ordinary(n=2)


# TRN-9: exactly the declared type.


class SubReserve(Reserve):
    added: str = "s"


class LookAlike:
    """Has every field of Reserve, and nothing else of it."""

    def __init__(self) -> None:
        self.order = 1
        self.parts: list[Part] = []
        self.tags: dict[str, list[str]] = {}
        self.note = None


class Liar:
    """Claims to be a Reserve through ``__class__``."""

    order = 1

    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return Reserve


@pytest.mark.spec("TRN-9", "TRN-13", "TRN-14", "CON-29")
@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (SubReserve(order=1), "SubReserve"),
        (LookAlike(), "LookAlike"),
        (
            types.SimpleNamespace(order=1, parts=[], tags={}, note=None),
            "SimpleNamespace",
        ),
        (Liar(), "Liar"),
        ({"order": 1}, "dict"),
        (b'{"order": 1}', "bytes"),
        (None, "None"),
        (Reserve, "the class Reserve"),
        (Reserved(ok=True), "Reserved"),
    ],
)
def test_a_request_not_exactly_of_the_request_type_is_a_type_error(
    value: object, shown: str
) -> None:
    with pytest.raises(TypeError) as raised:
        check_request(INVENTORY, RESERVE, value)
    message = str(raised.value)
    assert message.startswith("crossing.v1, action 'reserve': the request is ")
    assert shown in message
    assert "exactly a Reserve" in message


@pytest.mark.spec("TRN-9")
def test_the_reply_and_detail_get_the_same_exact_type_check() -> None:
    class SubReserved(Reserved):
        pass

    with pytest.raises(
        TypeError, match=r"the reply is a \S*SubReserved, not exactly a Reserved"
    ):
        codec(INVENTORY, Reserved).write(SubReserved(ok=True), "the reply")
    with pytest.raises(TypeError, match="the detail is a dict"):
        codec(INVENTORY, OutOfStock).write({"sku": "a"}, "the detail")


@pytest.mark.spec("TRN-9", "TRN-13")
def test_the_nearest_legitimate_shapes_pass_the_type_check() -> None:
    # Exactly the declared type, however it was built: validated, built with
    # model_construct from correct data, or copied.
    built = Reserve.model_construct(order=1, parts=[], tags={}, note=None)
    for value in (Reserve(order=1), built, Reserve(order=1).model_copy(deep=True)):
        assert json.loads(check_request(INVENTORY, RESERVE, value))["order"] == 1


@pytest.mark.spec("TRN-9", "TRN-13")
def test_a_nested_look_alike_crosses_only_as_text_that_reads_back() -> None:
    # The type check covers the request itself. A record inside it is written
    # from its fields, and crosses only if that text reads back.
    class LooksLikePart:
        def __init__(self, **fields: object) -> None:
            vars(self).update(fields)

    request = Reserve(order=1)
    request.parts.append(cast("Part", LooksLikePart(sku="a", quantity=2)))
    received = codec(INVENTORY, Reserve).read(
        check_request(INVENTORY, RESERVE, request)
    )
    assert received.parts == [Part(sku="a", quantity=2)]
    assert type(received.parts[0]) is Part

    request = Reserve(order=1)
    request.parts.append(cast("Part", LooksLikePart(quantity=2)))
    with pytest.raises(ValueError, match="does not read back"):
        check_request(INVENTORY, RESERVE, request)


# TRN-11: a value the type does not hold is refused, not written.


def _assigned(**fields: object) -> Reserve:
    request = Reserve(order=1)
    for name, value in fields.items():
        setattr(request, name, value)
    return request


@pytest.mark.spec("TRN-11", "TRN-13", "TRN-14", "CON-29")
@pytest.mark.parametrize(
    "request_value",
    [
        pytest.param(_assigned(order="1"), id="assigned-text-for-an-integer"),
        pytest.param(_assigned(order=1.5), id="assigned-float-for-an-integer"),
        pytest.param(_assigned(parts={"a": 1}), id="assigned-map-for-a-list"),
        pytest.param(_assigned(note={"sku": "a"}), id="assigned-map-for-a-record"),
        pytest.param(
            _assigned(tags={"x": [1]}), id="assigned-integer-in-a-nested-list"
        ),
        pytest.param(
            Reserve.model_construct(order=object(), parts=[], tags={}, note=None),
            id="constructed-object",
        ),
    ],
)
def test_a_request_holding_what_its_type_does_not_is_a_value_error(
    request_value: Reserve,
) -> None:
    with pytest.raises(ValueError, match="cannot be written as JSON") as raised:
        check_request(INVENTORY, RESERVE, request_value)
    assert str(raised.value).startswith("crossing.v1, action 'reserve': the request ")
    assert raised.value.__cause__ is not None


@pytest.mark.spec("TRN-11")
def test_the_nearest_legitimate_assignments_are_written() -> None:
    # The same fields, assigned values their type holds.
    request = _assigned(order=2, parts=[Part(sku="a")], note=None, tags={"x": []})
    assert json.loads(check_request(INVENTORY, RESERVE, request)) == {
        "order": 2,
        "parts": [{"sku": "a", "quantity": 1}],
        "tags": {"x": []},
        "note": None,
    }


class Measure(BaseModel):
    value: float = 0.0


class Measures(BaseModel):
    single: float = 0.0
    maybe: float | None = None
    series: list[float | None] = []
    named: dict[str, float] = {}
    nested: Measure = Measure()
    many: list[Measure] = []


@contract("measures.v1")
class MeasuresContract(Protocol):
    @action
    def send(self, request: Measures) -> Measures: ...


MEASURES = Contract.of(MeasuresContract)


def _measured(**fields: object) -> Measures:
    """Built through the record's own validator, which takes nan and inf."""
    return Measures.model_validate(fields)


def _assigned_measure(name: str, value: object) -> Measures:
    measures = Measures()
    setattr(measures, name, value)
    return measures


_NAN, _INF = float("nan"), float("inf")


@pytest.mark.spec("TRN-11", "TRN-13", "TRN-14")
@pytest.mark.parametrize(
    ("value", "shown"),
    [
        pytest.param(_measured(maybe=_NAN), "nan", id="nan-in-an-optional"),
        pytest.param(_measured(series=[1.0, _INF]), "inf", id="inf-in-a-list"),
        pytest.param(_measured(single=_NAN), "nan", id="nan"),
        pytest.param(_measured(named={"a": -_INF}), "-inf", id="in-a-map"),
        pytest.param(_measured(nested={"value": _NAN}), "nan", id="in-a-record"),
        pytest.param(
            _measured(many=[{"value": 1.0}, {"value": _INF}]),
            "inf",
            id="in-a-record-in-a-list",
        ),
        pytest.param(_assigned_measure("maybe", _NAN), "nan", id="assigned"),
        pytest.param(_assigned_measure("single", 1e308 * 10), "inf", id="overflowed"),
    ],
)
def test_a_number_that_is_not_finite_is_refused_not_written_as_null(
    value: Measures, shown: str
) -> None:
    # pydantic-core writes nan and inf as null, which an optional field reads
    # back as None, so the value is refused before it is sent.
    with pytest.raises(ValueError, match=f"it holds {shown}, and a number must be"):
        check_request(MEASURES, MEASURES.actions["send"], value)
    with pytest.raises(ValueError, match="the reply cannot be written"):
        codec(MEASURES, Measures).write(value, "the reply")


@pytest.mark.spec("TRN-11", "TRN-13")
def test_finite_numbers_and_absent_ones_are_written() -> None:
    # The nearest legitimate shapes: the largest finite doubles, and None.
    value = _measured(
        single=1.7976931348623157e308,
        maybe=None,
        series=[None, -1.7976931348623157e308],
        named={"a": 5e-324},
        nested={"value": 1.5},
    )
    text = check_request(MEASURES, MEASURES.actions["send"], value)
    assert codec(MEASURES, Measures).read(text) == value


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)
    n: int = 0


class FrozenWithList(BaseModel):
    model_config = ConfigDict(frozen=True)
    ns: list[int] = []


@pytest.mark.spec("TRN-12", "CON-17")
@pytest.mark.parametrize("nested", [False, True], ids=["field", "nested-field"])
def test_a_record_default_every_read_would_share_is_refused(nested: bool) -> None:
    # pydantic-core copies a default only when it cannot be hashed, so a
    # frozen record default would be the same object in every value read
    # without the field.
    record = create_model(_name("Sharing"), frozen=(Frozen, Frozen(n=1)))
    with pytest.raises(
        ContractDefinitionError, match="has a hashable record as its default"
    ):
        _declare(_holding(record) if nested else record)


@pytest.mark.spec("TRN-12")
@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        pytest.param(Frozen, Field(default_factory=lambda: Frozen(n=1)), id="factory"),
        pytest.param(Part, Part(sku="a"), id="unfrozen"),
        pytest.param(FrozenWithList, FrozenWithList(ns=[1]), id="frozen-unhashable"),
    ],
)
def test_a_record_default_each_read_gets_its_own_of_is_admitted(
    annotation: type[BaseModel], default: object
) -> None:
    record = create_model(_name("Own"), value=(annotation, default))
    declared = _declare(record)
    reader = codec(declared, record)
    first, second = reader.read(b"{}"), reader.read(b"{}")
    assert first.value == second.value  # type: ignore[attr-defined]
    assert first.value is not second.value  # type: ignore[attr-defined]


class Tree(BaseModel):
    name: str = "t"
    children: list["Tree"] = []
    parent: "Tree | None" = None


@contract("trees.v1")
class Trees(Protocol):
    @action
    def plant(self, request: Tree) -> None: ...


def _deep(levels: int) -> Tree:
    tree = Tree()
    for _ in range(levels):
        tree = Tree(children=[tree])
    return tree


def _cyclic(through: str) -> Tree:
    tree = Tree()
    if through == "list":
        tree.children.append(tree)
    else:
        tree.parent = Tree(parent=tree)
    return tree


@pytest.mark.spec("TRN-11", "TRN-13", "TRN-14")
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_cyclic("list"), id="contains-itself-through-a-list"),
        pytest.param(_cyclic("optional"), id="contains-itself-through-a-field"),
        pytest.param(_deep(300), id="deeper-than-the-serializer-goes"),
    ],
)
def test_a_value_json_cannot_hold_is_a_value_error(value: Tree) -> None:
    declared = Contract.of(Trees)
    with pytest.raises(ValueError, match="cannot be written as JSON"):
        check_request(declared, declared.actions["plant"], value)


@pytest.mark.spec("TRN-13", "TRN-14")
def test_a_value_written_deeper_than_the_reader_goes_is_a_value_error() -> None:
    # Each level of the tree is two levels of JSON, an object and a list. The
    # serializer writes this one, and the validator's JSON reader refuses it,
    # so it is never sent.
    declared = Contract.of(Trees)
    with pytest.raises(ValueError, match="does not read back") as raised:
        check_request(declared, declared.actions["plant"], _deep(150))
    assert isinstance(raised.value.__cause__, ValidationError)


@pytest.mark.spec("TRN-11", "TRN-13")
def test_a_deep_tree_and_a_shared_subtree_are_written() -> None:
    # The nearest legitimate shapes: deep, but within what the serializer
    # writes and the reader reads, and one subtree reached twice without a
    # cycle.
    declared = Contract.of(Trees)
    deep = _deep(60)
    received = codec(declared, Tree).read(
        check_request(declared, declared.actions["plant"], deep)
    )
    assert received == deep
    leaf = Tree(name="leaf")
    shared = Tree(children=[leaf, leaf])
    received = codec(declared, Tree).read(
        check_request(declared, declared.actions["plant"], shared)
    )
    assert received == shared
    assert received.children[0] is not received.children[1]


@pytest.mark.spec("TRN-11")
def test_a_reply_holding_what_its_type_does_not_is_refused_too() -> None:
    reply = Reserved(ok=True)
    reply.ok = cast("bool", "yes")
    with pytest.raises(ValueError, match="the reply cannot be written as JSON"):
        codec(INVENTORY, Reserved).write(reply, "the reply")


# TRN-13: the text is read back with the request type.


@pytest.mark.spec("TRN-13", "TRN-14", "CON-29")
@pytest.mark.parametrize(
    "request_value",
    [
        pytest.param(
            Reserve.model_construct(parts=[], tags={}, note=None),
            id="constructed-without-a-required-field",
        ),
        pytest.param(
            Reserve.model_construct(order=1, parts=[Part.model_construct()], tags={}),
            id="nested-record-without-a-required-field",
        ),
    ],
)
def test_a_request_that_does_not_read_back_is_a_value_error(
    request_value: Reserve,
) -> None:
    with pytest.raises(ValueError, match="does not read back") as raised:
        check_request(INVENTORY, RESERVE, request_value)
    assert isinstance(raised.value.__cause__, ValidationError)


def _refuse_large(value: object) -> object:
    if isinstance(value, int) and value > 10:
        raise RuntimeError("an author's validator raised")
    return value


class Checked(BaseModel):
    n: Annotated[int, BeforeValidator(_refuse_large)]


def _interrupt(value: object) -> object:
    if value == 13:
        raise KeyboardInterrupt
    return value


class Interrupting(BaseModel):
    n: Annotated[int, BeforeValidator(_interrupt)]


@contract("checked.v1")
class CheckedContract(Protocol):
    @action
    def send(self, request: Checked) -> None: ...

    @action
    def stop(self, request: Interrupting) -> None: ...


@pytest.mark.spec("TRN-13", "TRN-14")
def test_any_exception_from_reading_back_is_a_value_error_that_keeps_its_cause() -> (
    None
):
    declared = Contract.of(CheckedContract)
    request = Checked.model_construct(n=11)
    with pytest.raises(ValueError, match="does not read back") as raised:
        check_request(declared, declared.actions["send"], request)
    assert type(raised.value.__cause__) is RuntimeError
    # The nearest legitimate value reads back.
    assert (
        check_request(declared, declared.actions["send"], Checked(n=10)) == b'{"n":10}'
    )


def test_an_exception_that_is_not_an_exception_propagates_unchanged() -> None:
    declared = Contract.of(CheckedContract)
    request = Interrupting.model_construct(n=13)
    with pytest.raises(KeyboardInterrupt):
        check_request(declared, declared.actions["stop"], request)


@pytest.mark.spec("TRN-13")
def test_the_request_checks_run_in_order() -> None:
    # A value failing every check reports the first: its type, then the
    # writing, then the reading back.
    class Wrong(Reserve):
        pass

    wrong = Wrong.model_construct(order="x")
    with pytest.raises(TypeError, match="exactly a Reserve"):
        check_request(INVENTORY, RESERVE, wrong)
    unwritable = Reserve.model_construct(order="x", tags={})
    with pytest.raises(ValueError, match="cannot be written as JSON"):
        check_request(INVENTORY, RESERVE, unwritable)
    unreadable = Reserve.model_construct(tags={})
    with pytest.raises(ValueError, match="does not read back"):
        check_request(INVENTORY, RESERVE, unreadable)


# Invariants over generated contracts and values.


def _declare_request(record: type[BaseModel]) -> tuple[Contract, dict[str, Any]]:
    def send(self: object, request: record) -> record: ...  # type: ignore[valid-type]

    declared = types.new_class(
        _name("X"), (Protocol,), exec_body=lambda ns: ns.update(send=action(send))
    )
    contract(f"{_name('crossed')}.v1")(declared)
    found = Contract.of(declared)
    description = found.describe()
    return found, {"$defs": description["$defs"], "$ref": f"#/$defs/{record.__name__}"}


def _crosses(
    found: Contract, record: type[BaseModel], schema: dict[str, Any], value: object
) -> None:
    sent = codec(found, record).read(json.dumps(value).encode())
    text = check_request(found, found.actions["send"], sent)
    Draft202012Validator(schema, format_checker=_FORMAT_CHECKER).validate(
        json.loads(text)
    )
    received = codec(found, record).read(text)
    assert received == sent
    assert type(received) is record
    assert _shared(sent, received) == set()
    # The reply crosses back the same way.
    back = codec(found, record).read(codec(found, record).write(received, "the reply"))
    assert back == sent
    assert _shared(received, back) == set()


@pytest.mark.spec("TRN-10", "TRN-12", "TRN-13")
@_HYPOTHESIS
@given(st.data())
def test_generated_values_cross_equal_and_unshared(data: st.DataObject) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    try:
        found, schema = _declare_request(record)
    except ContractDefinitionError:
        return
    _crosses(found, record, schema, data.draw(from_schema(schema)))


@pytest.mark.spec("TRN-10", "TRN-12", "TRN-13")
@_HYPOTHESIS
@given(st.data())
def test_every_portable_form_crosses_equal_and_unshared(data: st.DataObject) -> None:
    found, schema = _declare_request(Reference)
    _crosses(found, Reference, schema, data.draw(from_schema(schema)))
