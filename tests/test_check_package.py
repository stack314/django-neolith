"""The contract package of each hosted app (ADR 0030 Decision 2).

A contract keeps the record and enum classes that the walk run when it is
declared reaches, beside its description, under the names the description
gives them under ``$defs``; a property test checks that against a graph of
records generated with its own answer. The package itself is derived from
the declarations of a host built in a child process, from a project written
under ``tmp_path`` (``tests/checking.py``), where every module it names is
also looked up in the interpreter's own table of loaded modules.
"""

import copy
import dataclasses
import enum
import itertools
import json
import pickle
import types
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeAliasType, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, create_model

from neolith import Contract, action, contract
from tests.checking import Project


class Colour(StrEnum):
    RED = "red"


class Count(BaseModel):
    n: int
    colour: Colour


class TooMany(BaseModel):
    limit: int


@contract("package.v1")
class Counter(Protocol):
    @action(idempotent=True, errors={"too_many": TooMany})
    def count(self, request: Count) -> Count: ...


# The I-JSON range of an integer, as every description states it.
_INTEGER = {
    "type": "integer",
    "minimum": -9007199254740991,
    "maximum": 9007199254740991,
}


def _pickled(held: Contract) -> object:
    return pickle.loads(pickle.dumps(held))  # noqa: S301


def test_copy_deepcopy_pickle_hash_and_describe_of_a_contract_are_unchanged() -> None:
    held = Contract.of(Counter)
    # The one slot added for the check before deployment.
    assert sorted(Contract.__slots__) == [
        "_actions",
        "_codecs",
        "_description",
        "_types",
        "declaration",
        "identity",
        "major",
        "name",
    ]
    copied = copy.copy(held)
    assert type(copied) is Contract
    assert copied is not held
    for slot in Contract.__slots__:
        assert getattr(copied, slot) is getattr(held, slot), slot
    # A contract holds read-only maps, which neither copy nor pickle.
    clones: list[Callable[[Contract], object]] = [copy.deepcopy, _pickled]
    for clone in clones:
        with pytest.raises(TypeError) as raised:
            clone(held)
        # Python 3.14 adds notes on where, which pytest's match reads too.
        assert str(raised.value) == "cannot pickle 'mappingproxy' object"
    assert hash(held) == object.__hash__(held)
    assert held != copied
    assert repr(held) == "<Contract package.v1>"
    described = held.describe()
    assert described == {
        "contract": "package.v1",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "actions": {
            "count": {
                "idempotent": True,
                "request": {"$ref": "#/$defs/Count"},
                "reply": {"$ref": "#/$defs/Count"},
                "errors": {"too_many": {"$ref": "#/$defs/TooMany"}},
            }
        },
        "$defs": {
            "Count": {
                "type": "object",
                "properties": {
                    "n": _INTEGER,
                    "colour": {"$ref": "#/$defs/Colour"},
                },
                "required": ["n", "colour"],
            },
            "Colour": {"type": "string", "enum": ["red"]},
            "TooMany": {
                "type": "object",
                "properties": {"limit": _INTEGER},
                "required": ["limit"],
            },
        },
    }
    assert json.loads(json.dumps(described)) == described
    assert held.describe() is not described


def test_a_contract_keeps_the_classes_it_names_read_only() -> None:
    held = Contract.of(Counter)
    assert dict(held._types) == {"Count": Count, "Colour": Colour, "TooMany": TooMany}
    with pytest.raises(TypeError):
        held._types["Other"] = Count  # type: ignore[index]


_counter = itertools.count()


def _name(prefix: str) -> str:
    return f"{prefix}{next(_counter)}"


# How a record's field reaches another record or an enum.
_WRAPS: dict[str, Any] = {
    "plain": lambda t: t,
    "list": lambda t: list[t],  # type: ignore[valid-type]
    "map": lambda t: dict[str, t],  # type: ignore[valid-type]
    "optional": lambda t: t | None,
    "alias": lambda t: TypeAliasType(_name("Alias"), t),
    "alias_list": lambda t: TypeAliasType(_name("Bag"), list[t]),  # type: ignore[valid-type]
}


@dataclasses.dataclass(frozen=True)
class _Graph:
    records: int
    enums: int
    # For each record, the records (later ones only) and enums it reaches.
    edges: dict[int, list[tuple[tuple[str, int], str]]]
    reply: int | None
    detail: int | None


@st.composite
def _graphs(draw: st.DrawFn) -> _Graph:
    records = draw(st.integers(1, 5))
    enums = draw(st.integers(0, 2))
    edges: dict[int, list[tuple[tuple[str, int], str]]] = {}
    for i in range(records):
        targets = [("R", j) for j in range(i + 1, records)]
        targets += [("E", k) for k in range(enums)]
        edges[i] = (
            draw(
                st.lists(
                    st.tuples(
                        st.sampled_from(targets), st.sampled_from(sorted(_WRAPS))
                    ),
                    max_size=3,
                )
            )
            if targets
            else []
        )
    reply = draw(st.none() | st.integers(0, records - 1))
    detail = draw(st.none() | st.integers(0, records - 1))
    return _Graph(records, enums, edges, reply, detail)


def _declared(graph: _Graph) -> tuple[Contract, set[type[Any]]]:
    """A contract over the graph's records, and the classes it reaches."""
    enums = [
        cast("type[StrEnum]", StrEnum(_name("E"), {"A": "a"}))
        for _ in range(graph.enums)
    ]
    records: dict[int, type[BaseModel]] = {}
    for i in reversed(range(graph.records)):
        fields: dict[str, Any] = {}
        for n, ((kind, j), wrap) in enumerate(graph.edges[i]):
            target = records[j] if kind == "R" else enums[j]
            fields[f"f{n}"] = (_WRAPS[wrap](target), ...)
        records[i] = create_model(_name("R"), **fields)
    roots = [0, *(i for i in (graph.reply, graph.detail) if i is not None)]
    reached: set[type[Any]] = set()
    pending: deque[tuple[str, int]] = deque(("R", i) for i in roots)
    while pending:
        kind, i = pending.popleft()
        cls: type[Any] = records[i] if kind == "R" else enums[i]
        if cls in reached:
            continue
        reached.add(cls)
        if kind == "R":
            pending.extend(target for target, _ in graph.edges[i])

    def send(self: object, request: Any) -> Any: ...  # noqa: ANN401

    send.__annotations__ = {
        "request": records[0],
        "return": None if graph.reply is None else records[graph.reply],
    }
    errors = {} if graph.detail is None else {"failed": records[graph.detail]}
    declared = types.new_class(
        _name("C"),
        (Protocol,),
        exec_body=lambda ns: ns.update(send=action(errors=errors)(send)),
    )
    contract(f"{_name('generated')}.v1")(declared)
    return Contract.of(declared), reached


@settings(max_examples=60, deadline=None)
@given(_graphs())
def test_a_contract_keeps_every_class_its_description_names(graph: _Graph) -> None:
    held, reached = _declared(graph)
    named = held._types
    assert list(named) == list(held.describe()["$defs"])
    assert all(cls.__name__ == name for name, cls in named.items())
    assert len(named) == len(reached)
    assert set(named.values()) == reached
    assert all(
        issubclass(cls, BaseModel) or issubclass(cls, enum.Enum)
        for cls in named.values()
    )


# The project: one hosted app for each layout Decision 2 names.

_RECORDS = """\
from pydantic import BaseModel


class Ping(BaseModel):
    n: int
"""

_ONE = """\
from typing import Protocol

from neolith import action, contract
from one.records import Ping


@contract("one.v1")
class One(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""


def _service(module: str, record: str, action_name: str = "ping") -> str:
    return f"""\
from {module} import {record}


class Service:
    def {action_name}(self, request: {record}) -> {record}:
        return request
"""


_SEVERAL_CONTRACT = """\
from typing import Protocol

from neolith import action, contract
from several.api.records import Answer, Ask
from several.errors import Failure


@contract("several.v1")
class Several(Protocol):
    @action(errors={"failed": Failure})
    def ask(self, request: Ask) -> Answer: ...
"""

_SEVERAL_RECORDS = """\
from pydantic import BaseModel

from several.kinds import Colour


class Ask(BaseModel):
    colour: Colour


class Answer(BaseModel):
    text: str
"""

_SEVERAL_KINDS = """\
from enum import StrEnum


class Colour(StrEnum):
    RED = "red"
"""

_SEVERAL_ERRORS = """\
from pydantic import BaseModel


class Failure(BaseModel):
    why: str
"""

_SEVERAL_SERVICE = """\
from several.api.records import Answer, Ask


class Service:
    def ask(self, request: Ask) -> Answer:
        return Answer(text=request.colour)
"""

# Records nested in lists, maps, optional fields, records and a type alias,
# each in a module of its own.
_DEEP_CONTRACT = """\
from typing import Protocol

from pydantic import BaseModel

from deep.items import Item
from deep.other import Other
from deep.pieces import Piece
from deep.things import Thing
from neolith import action, contract

type Bag = list[Piece]


class Request(BaseModel):
    items: list[Item]
    things: dict[str, Thing]
    other: Other | None
    bag: Bag


@contract("deep.v1")
class Deep(Protocol):
    @action
    def go(self, request: Request) -> None: ...
"""


def _record(name: str, field: str = "n: int", imports: str = "") -> str:
    return f"""\
from pydantic import BaseModel
{imports}

class {name}(BaseModel):
    {field}
"""


_DEEP_SERVICE = """\
from deep.contracts import Request


class Service:
    def go(self, request: Request) -> None:
        return None
"""

_OUT = """\
from typing import Protocol

from neolith import action, contract
from shared.records import Shared


@contract("out.v1")
class Out(Protocol):
    @action
    def ping(self, request: Shared) -> Shared: ...
"""

# A module that defines a model while apps are being loaded cannot be
# imported before (Django's ModelBase asks the registry for the model's app),
# so the record module that a declaration imports builds its model in a
# function, which the models module calls: the model's module is still the
# record module.
_MIXED_RECORDS = """\
from pydantic import BaseModel


class Note(BaseModel):
    text: str


def thing():
    from django.db import models

    class Thing(models.Model):
        name = models.CharField(max_length=10)
        tags = models.ManyToManyField("self")

        class Meta:
            app_label = "mixed"

    return Thing
"""

_MIXED = """\
from typing import Protocol

from mixed.records import Note
from neolith import action, contract


@contract("mixed.v1")
class Mixed(Protocol):
    @action
    def note(self, request: Note) -> Note: ...
"""

_JOINED = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Ping(BaseModel):
    n: int


@contract("joined.v1")
class Joined(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""


_LAYERED = """\
from typing import Protocol

from layered.api.records import Ping
from neolith import action, contract


@contract("layered.v1")
class Layered(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


class Service:
    def ping(self, request: Ping) -> Ping:
        return request
"""


def _plain(identity: str, records: str) -> str:
    return f"""\
from typing import Protocol

from neolith import action, contract
from {records} import Ping


@contract({identity!r})
class Plain(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""


def _provider(
    project: Project,
    app: str,
    contracts: str,
    name: str,
    implementation: str,
    models: str = "",
) -> None:
    """Write hosted app ``app``, which provides contract ``name`` of the
    module ``contracts``.
    """
    project.app(
        app,
        models=models,
        imports=f"from neolith import Provided\nfrom {contracts} import {name}\n",
        config=f"""\
        provides = (
            Provided({name}, implementation={implementation!r}, principal_from=()),
        )
        """,
    )


# A contract in a package's __init__.py, with its records in a module below
# it: the package is a module of the contract package, not one above it.
_KEPT_ABOVE = """\
from typing import Protocol

from neolith import action, contract
from pk.api.records import Ping


@contract("pk.v1")
class Pk(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...
"""

# A record nested in a plain class is named by its qualified name.
_NESTED = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Outer:
    class Inner(BaseModel):
        n: int


@contract("nest.v1")
class Nest(Protocol):
    @action
    def ping(self, request: Outer.Inner) -> Outer.Inner: ...
"""

# Two contracts and their two implementations in one module, provided in
# the reverse order of their names.
_TWO = """\
from typing import Protocol

from pydantic import BaseModel

from neolith import action, contract


class Ping(BaseModel):
    n: int


@contract("two.a.v1")
class A(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


@contract("two.b.v1")
class B(Protocol):
    @action
    def ping(self, request: Ping) -> Ping: ...


class AImpl:
    def ping(self, request: Ping) -> Ping:
        return request


class ZImpl:
    def ping(self, request: Ping) -> Ping:
        return request
"""

# A record module whose two models are registered in the reverse order of
# their names, the second swapped out for the first.
_SWAPPED_RECORDS = """\
from pydantic import BaseModel


class Ping(BaseModel):
    n: int


def build():
    from django.db import models

    class Zed(models.Model):
        class Meta:
            app_label = "sw"

    class Abe(models.Model):
        class Meta:
            app_label = "sw"
            swappable = "SW_ABE"

    return Zed, Abe
"""


# Run in the child: build the host, derive every contract package, and
# look each module it names up in the interpreter's table of loaded modules.
_SCRIPT = """\
import dataclasses
import json
import sys

import django

django.setup()

from django.apps import apps

from neolith import _check_package
from neolith._declaration import declaration_of

hosted = {
    config.label: config
    for config in apps.get_app_configs()
    if declaration_of(type(config)) is not None
}
found = _check_package.packages(hosted)
named = set()
for package in found.values():
    named |= {*package.modules, *package.above, *package.left_out}
loaded = {
    name: [name in sys.modules, hasattr(sys.modules.get(name), "__path__")]
    for name in sorted(named)
}
print(json.dumps({
    "packages": {label: dataclasses.asdict(p) for label, p in found.items()},
    "loaded": loaded,
}))
"""


def _project(root: Path) -> Project:
    project = Project(root)
    project.write("one/records.py", _RECORDS)
    project.write("one/contracts.py", _ONE)
    project.write("one/service.py", _service("one.records", "Ping"))
    _provider(project, "one", "one.contracts", "One", "one.service.Service")

    project.write("several/api/__init__.py")
    project.write("several/api/contract.py", _SEVERAL_CONTRACT)
    project.write("several/api/records.py", _SEVERAL_RECORDS)
    project.write("several/kinds.py", _SEVERAL_KINDS)
    project.write("several/errors.py", _SEVERAL_ERRORS)
    project.write("several/service.py", _SEVERAL_SERVICE)
    _provider(
        project, "several", "several.api.contract", "Several", "several.service.Service"
    )

    project.write("deep/contracts.py", _DEEP_CONTRACT)
    project.write(
        "deep/items.py", _record("Item", "part: Part", "from deep.parts import Part\n")
    )
    project.write("deep/parts.py", _record("Part"))
    project.write("deep/things.py", _record("Thing"))
    project.write("deep/other.py", _record("Other"))
    project.write("deep/pieces.py", _record("Piece"))
    project.write("deep/service.py", _DEEP_SERVICE)
    _provider(project, "deep", "deep.contracts", "Deep", "deep.service.Service")

    project.write("shared/__init__.py")
    project.write("shared/records.py", _record("Shared"))
    project.write("out/contracts.py", _OUT)
    project.write("out/service.py", _service("shared.records", "Shared"))
    _provider(project, "out", "out.contracts", "Out", "out.service.Service")

    project.write("mixed/contracts.py", _MIXED)
    project.write("mixed/records.py", _MIXED_RECORDS)
    project.write("mixed/service.py", _service("mixed.records", "Note", "note"))
    _provider(
        project,
        "mixed",
        "mixed.contracts",
        "Mixed",
        "mixed.service.Service",
        models="from mixed.records import thing\n\nThing = thing()\n",
    )

    project.write("joined/contracts.py", _JOINED)
    _provider(
        project, "joined", "joined.contracts", "Joined", "joined.contracts.Service"
    )

    # A left-out module that is also a package above a contract module.
    project.write("layered/api/__init__.py", _LAYERED)
    project.write("layered/api/records.py", _RECORDS)
    _provider(project, "layered", "layered.api", "Layered", "layered.api.Service")

    # A record of another hosted app's contract package.
    project.write("cross/contracts.py", _plain("cross.v1", "one.records"))
    project.write("cross/service.py", _service("one.records", "Ping"))
    _provider(project, "cross", "cross.contracts", "Plain", "cross.service.Service")

    # A hosted app nested in another, whose contract uses a record of the
    # nested app.
    project.write("outer/contracts.py", _plain("outer.v1", "outer.inner.api.records"))
    project.write("outer/service.py", _service("outer.inner.api.records", "Ping"))
    _provider(project, "outer", "outer.contracts", "Plain", "outer.service.Service")
    project.write("outer/inner/api/__init__.py")
    project.write("outer/inner/api/records.py", _RECORDS)
    project.write(
        "outer/inner/api/contracts.py", _plain("inner.v1", "outer.inner.api.records")
    )
    project.write("outer/inner/service.py", _service("outer.inner.api.records", "Ping"))
    _provider(
        project,
        "outer.inner",
        "outer.inner.api.contracts",
        "Plain",
        "outer.inner.service.Service",
    )

    # A hosted app in a regular package of no app, and one in a namespace
    # package.
    project.write("proj/__init__.py")
    project.write("proj/shop/contracts.py", _plain("shop.v1", "proj.shop.records"))
    project.write("proj/shop/records.py", _RECORDS)
    project.write("proj/shop/service.py", _service("proj.shop.records", "Ping"))
    _provider(
        project,
        "proj.shop",
        "proj.shop.contracts",
        "Plain",
        "proj.shop.service.Service",
    )
    project.write(
        "space/desk/contracts.py", _plain("desk.v1", "space.desk.contracts_records")
    )
    project.write("space/desk/contracts_records.py", _RECORDS)
    project.write(
        "space/desk/service.py", _service("space.desk.contracts_records", "Ping")
    )
    _provider(
        project,
        "space.desk",
        "space.desk.contracts",
        "Plain",
        "space.desk.service.Service",
    )

    # An implementation in a package module above a contract module.
    project.write("imp/api/__init__.py", _service("imp.api.records", "Ping"))
    project.write("imp/api/records.py", _RECORDS)
    project.write("imp/api/contracts.py", _plain("imp.v1", "imp.api.records"))
    _provider(project, "imp", "imp.api.contracts", "Plain", "imp.api.Service")

    project.write("pk/api/__init__.py", _KEPT_ABOVE)
    project.write("pk/api/records.py", _RECORDS)
    project.write("pk/service.py", _service("pk.api.records", "Ping"))
    _provider(project, "pk", "pk.api", "Pk", "pk.service.Service")

    project.write("nest/contracts.py", _NESTED)
    project.write(
        "nest/service.py",
        "from nest.contracts import Outer\n\n\nclass Service:\n"
        "    def ping(self, request: Outer.Inner) -> Outer.Inner:\n"
        "        return request\n",
    )
    _provider(project, "nest", "nest.contracts", "Nest", "nest.service.Service")

    project.write("two/contracts.py", _TWO)
    project.app(
        "two",
        imports="from neolith import Provided\nfrom two.contracts import A, B\n",
        config="""\
        provides = (
            Provided(B, implementation="two.contracts.ZImpl", principal_from=()),
            Provided(A, implementation="two.contracts.AImpl", principal_from=()),
        )
        """,
    )

    project.write("sw/contracts.py", _plain("sw.v1", "sw.records"))
    project.write("sw/records.py", _SWAPPED_RECORDS)
    project.write("sw/service.py", _service("sw.records", "Ping"))
    _provider(
        project,
        "sw",
        "sw.contracts",
        "Plain",
        "sw.service.Service",
        models="from sw.records import build\n\nZed, Abe = build()\n",
    )

    hosted = [
        "one",
        "several",
        "deep",
        "out",
        "mixed",
        "joined",
        "layered",
        "cross",
        "outer",
        "outer.inner",
        "proj.shop",
        "space.desk",
        "imp",
        "pk",
        "nest",
        "two",
        "sw",
    ]
    project.settings(
        hosted,
        neolith={
            "DATABASES": project.databases(
                *(name.rpartition(".")[2] for name in hosted)
            )
        },
        below='SILENCED_SYSTEM_CHECKS = ["neolith.W004"]\nSW_ABE = "sw.Zed"\n',
    )
    return project


def _package(
    modules: dict[str, list[str]],
    above: dict[str, str | None],
    left_out: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, Any]:
    return {"modules": modules, "above": above, "left_out": left_out or {}}


def test_the_contract_package_of_each_layout(tmp_path: Path) -> None:
    project = _project(tmp_path)
    ran = project.run("-c", _SCRIPT, env={"DJANGO_SETTINGS_MODULE": "settings"})
    assert ran.status == 0, ran.stderr
    result = json.loads(ran.stdout)
    packages = result["packages"]
    assert packages == {
        # The contract and its records in two modules of the app.
        "one": _package(
            {"one.contracts": ["One"], "one.records": ["Ping"]},
            {"one": "one"},
        ),
        # In several modules, at several depths, with an enum and an error
        # detail record.
        "several": _package(
            {
                "several.api.contract": ["Several"],
                "several.api.records": ["Answer", "Ask"],
                "several.errors": ["Failure"],
                "several.kinds": ["Colour"],
            },
            {"several.api": "several", "several": "several"},
        ),
        # Nested in lists, maps, optional fields, records and a type alias.
        "deep": _package(
            {
                "deep.contracts": ["Deep", "Request"],
                "deep.items": ["Item"],
                "deep.other": ["Other"],
                "deep.parts": ["Part"],
                "deep.pieces": ["Piece"],
                "deep.things": ["Thing"],
            },
            {"deep": "deep"},
        ),
        # A record in a package outside every app is code in no app.
        "out": _package({"out.contracts": ["Out"]}, {"out": "out"}),
        # A record module that also defines a model is left out.
        "mixed": _package(
            {"mixed.contracts": ["Mixed"]},
            {"mixed": "mixed"},
            {
                "mixed.records": {
                    "defines": ["Note"],
                    "models": ["mixed.Thing"],
                    "implementations": [],
                }
            },
        ),
        # So is the module of the implementation, here the contract's own.
        "joined": _package(
            {},
            {},
            {
                "joined.contracts": {
                    "defines": ["Joined", "Ping"],
                    "models": [],
                    "implementations": ["joined.contracts.Service"],
                }
            },
        ),
        # A left-out module is not part of the package, even above a module
        # of it.
        "layered": _package(
            {"layered.api.records": ["Ping"]},
            {"layered": "layered"},
            {
                "layered.api": {
                    "defines": ["Layered"],
                    "models": [],
                    "implementations": ["layered.api.Service"],
                }
            },
        ),
        # A record of another app's contract package is that app's.
        "cross": _package({"cross.contracts": ["Plain"]}, {"cross": "cross"}),
        # A record of a nested app is that app's.
        "outer": _package({"outer.contracts": ["Plain"]}, {"outer": "outer"}),
        # The package modules above: in the app, in an outer hosted app,
        # in a regular package of no app and in a namespace package.
        "inner": _package(
            {
                "outer.inner.api.contracts": ["Plain"],
                "outer.inner.api.records": ["Ping"],
            },
            {"outer.inner.api": "inner", "outer.inner": "inner", "outer": "outer"},
        ),
        "shop": _package(
            {"proj.shop.contracts": ["Plain"], "proj.shop.records": ["Ping"]},
            {"proj.shop": "shop", "proj": None},
        ),
        "desk": _package(
            {
                "space.desk.contracts": ["Plain"],
                "space.desk.contracts_records": ["Ping"],
            },
            {"space.desk": "desk", "space": None},
        ),
        # A package module above a contract module that holds the
        # implementation is left out, as a module that defines one is.
        "imp": _package(
            {"imp.api.contracts": ["Plain"], "imp.api.records": ["Ping"]},
            {"imp": "imp"},
            {
                "imp.api": {
                    "defines": [],
                    "models": [],
                    "implementations": ["imp.api.Service"],
                }
            },
        ),
        # A module of the package that is also above another is not named
        # among those above.
        "pk": _package(
            {"pk.api": ["Pk"], "pk.api.records": ["Ping"]},
            {"pk": "pk"},
        ),
        # A nested record is named by its qualified name.
        "nest": _package({"nest.contracts": ["Nest", "Outer.Inner"]}, {"nest": "nest"}),
        # Several implementations in one module, sorted.
        "two": _package(
            {},
            {},
            {
                "two.contracts": {
                    "defines": ["A", "B", "Ping"],
                    "models": [],
                    "implementations": [
                        "two.contracts.AImpl",
                        "two.contracts.ZImpl",
                    ],
                }
            },
        ),
        # Several models in one module, sorted, a swapped one included.
        "sw": _package(
            {"sw.contracts": ["Plain"]},
            {"sw": "sw"},
            {
                "sw.records": {
                    "defines": ["Ping"],
                    "models": ["sw.Abe", "sw.Zed"],
                    "implementations": [],
                }
            },
        ),
    }
    # Every module named is one the interpreter loaded, and every package
    # module above is a package.
    loaded = result["loaded"]
    assert all(found for found, _ in loaded.values()), loaded
    for package in packages.values():
        for name in package["above"]:
            assert loaded[name] == [True, True], name
        for name in package["modules"]:
            assert loaded[name][0], name
