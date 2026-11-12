"""The app declaration (spec 05, ADR 0021).

A declaration is the class attributes of one ``NeolithAppConfig`` subclass.
These tests draw declarations from the admitted grammar, change one element
at a time to reach each refusal, and load declared apps into Django's global
registry and into isolated ones.
"""

import collections
import copy
import importlib
import itertools
import json
import os
import pickle
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from django.apps import AppConfig
from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test.utils import isolate_apps, modify_settings, override_settings
from django.utils.functional import SimpleLazyObject
from hypothesis import given
from hypothesis import strategies as st

import neolith
from neolith import Contract, DeclarationError, NeolithAppConfig, Provided
from neolith._declaration import (
    _DJANGO_OPTIONS,
    _EVENT_FIELDS,
    _FIELDS,
    ADMITTED_NAMES,
    RESOURCE_KINDS,
    SHARED_NAMES,
    Declaration,
    Provision,
    declaration_of,
)
from tests import host_apps
from tests.declaration_contracts import (
    Couriers,
    Invoices,
    Stock,
    StockTwin,
    StockV2,
    Unmarked,
)
from tests.hosting import in_fresh_process
from tests.test_contract_round_trip import _HYPOTHESIS

Namespace = dict[str, Any]

_APP = f"app {__name__}.StockConfig: "
_IMPLEMENTATION = "shop.stock.services.StockService"


def _valid(**changes: object) -> Namespace:
    return {"name": "shop.stock", "label": "stock", **changes}


def _provided(
    declaration: object = Stock,
    implementation: object = _IMPLEMENTATION,
    principal_from: object = (),
) -> Provided:
    return Provided(
        declaration, implementation=implementation, principal_from=principal_from
    )


def _declare(
    namespace: Namespace,
    *bases: type,
    name: str = "StockConfig",
) -> type[NeolithAppConfig]:
    if not any(issubclass(base, NeolithAppConfig) for base in bases):
        bases = (*bases, NeolithAppConfig)
    return cast(
        "type[NeolithAppConfig]",
        type(name, bases, {"__module__": __name__, **namespace}),
    )


def _snapshot(config: type[NeolithAppConfig]) -> Declaration:
    found = declaration_of(config)
    assert found is not None
    return found


def _refusals(
    namespace: Namespace, *bases: type, name: str = "StockConfig"
) -> list[tuple[str, str]]:
    """Define a class that is refused; return each refusal as (field, text)."""
    with pytest.raises(DeclarationError) as raised:
        _declare(namespace, *bases, name=name)
    lines = str(raised.value).split("\n")
    assert lines == sorted(lines)
    prefix = f"app {__name__}.{name}: "
    assert all(line.startswith(prefix) for line in lines), lines
    return [
        cast("tuple[str, str]", tuple(line.removeprefix(prefix).split(": ", 1)))
        for line in lines
    ]


def _refusal(namespace: Namespace, *bases: type) -> tuple[str, str]:
    refusals = _refusals(namespace, *bases)
    assert len(refusals) == 1, refusals
    return refusals[0]


# The admitted grammar.

_CONTRACTS = (Stock, StockV2, Invoices, Couriers)
_SEGMENTS = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True)
_IDENTIFIERS = st.one_of(
    st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,7}", fullmatch=True),
    st.sampled_from(["café", "données", "名前"]),
)
_DRAWN_FIELDS = ("provides", "requires", "resources", "batteries")


def _dotted(min_size: int) -> st.SearchStrategy[str]:
    return st.lists(_IDENTIFIERS, min_size=min_size, max_size=4).map(".".join)


@st.composite
def _declarations(draw: st.DrawFn) -> Namespace:
    """A declaration of ADR 0021's grammar, as the namespace of a class body."""
    name = draw(_dotted(1))
    namespace: Namespace = {"name": name, "label": draw(_SEGMENTS)}
    provided = draw(st.lists(st.sampled_from(_CONTRACTS), unique=True, max_size=4))
    if provided:
        namespace["provides"] = tuple(
            _provided(
                declaration,
                draw(_dotted(2)),
                tuple(draw(st.lists(_SEGMENTS, unique=True, max_size=3))),
            )
            for declaration in provided
        )
    required = draw(st.lists(st.sampled_from(_CONTRACTS), unique=True, max_size=4))
    if required:
        namespace["requires"] = tuple(required)
    resources = draw(st.lists(st.sampled_from(RESOURCE_KINDS), unique=True))
    if resources:
        namespace["resources"] = tuple(resources)
    batteries = draw(
        st.lists(_dotted(1).filter(lambda b: b != name), unique=True, max_size=3)
    )
    if batteries:
        namespace["batteries"] = tuple(batteries)
    return namespace


@pytest.mark.spec("APP-3", "APP-5", "APP-6")
@_HYPOTHESIS
@given(_declarations())
def test_a_declaration_of_the_grammar_is_admitted_and_read_back(
    namespace: Namespace,
) -> None:
    config = _declare(namespace)
    snapshot = _snapshot(config)

    assert snapshot.config is config
    assert (snapshot.name, snapshot.label) == (namespace["name"], namespace["label"])
    written = namespace.get("provides", ())
    assert snapshot.provides == tuple(
        Provision(
            Contract.of(entry._contract), entry._implementation, entry._principal_from
        )
        for entry in written
    )
    assert snapshot.requires == tuple(
        Contract.of(entry) for entry in namespace.get("requires", ())
    )
    assert snapshot.resources == namespace.get("resources", ())
    assert snapshot.batteries == namespace.get("batteries", ())
    assert config.default is True


def test_the_generator_draws_every_field_the_check_admits() -> None:
    # A field the check learns to admit has to be drawn above as well.
    assert _DRAWN_FIELDS == _FIELDS
    assert {*_FIELDS, *_DJANGO_OPTIONS, "ready"} == ADMITTED_NAMES
    assert RESOURCE_KINDS == ("database",)
    assert SHARED_NAMES < ADMITTED_NAMES


def _entries(namespace: Namespace, field: str) -> tuple[Any, ...]:
    return cast("tuple[Any, ...]", namespace.get(field, ()))


# Each mutation changes one element of an admitted declaration, and names the
# one field its refusals are about.
_MUTATIONS: dict[str, tuple[str, Callable[[Namespace], object]]] = {
    "a public name": ("helper", lambda ns: ns.update(helper=1)),
    "no label": ("label", lambda ns: ns.pop("label")),
    "an upper-case label": ("label", lambda ns: ns.update(label=ns["label"].upper())),
    "a name that is no path": ("name", lambda ns: ns.update(name=ns["name"] + ".")),
    "publishes": ("publishes", lambda ns: ns.update(publishes=())),
    "subscribes": ("subscribes", lambda ns: ns.update(subscribes=())),
    "provides without a comma": ("provides", lambda ns: ns.update(provides=Stock)),
    "requires without a comma": ("requires", lambda ns: ns.update(requires=Stock)),
    "batteries without a comma": ("batteries", lambda ns: ns.update(batteries="x")),
    "resources as a list": (
        "resources",
        lambda ns: ns.update(resources=["database"]),
    ),
    "an empty requires": ("requires", lambda ns: ns.update(requires=())),
    "a bare contract in provides": (
        "provides",
        lambda ns: ns.update(provides=(*_entries(ns, "provides"), Stock)),
    ),
    "a Provided with no contract": (
        "provides",
        lambda ns: ns.update(
            provides=(
                *_entries(ns, "provides"),
                Provided(implementation=_IMPLEMENTATION, principal_from=()),
            )
        ),
    ),
    "an unmarked class in requires": (
        "requires",
        lambda ns: ns.update(requires=(*_entries(ns, "requires"), Unmarked)),
    ),
    "an implementation of one identifier": (
        "provides",
        lambda ns: ns.update(provides=(_provided(implementation="Service"),)),
    ),
    "no allow-list": (
        "provides",
        lambda ns: ns.update(
            provides=(Provided(Stock, implementation=_IMPLEMENTATION),)
        ),
    ),
    "a wildcard": (
        "provides",
        lambda ns: ns.update(provides=(_provided(principal_from=("*",)),)),
    ),
    "a caller twice": (
        "provides",
        lambda ns: ns.update(provides=(_provided(principal_from=("web", "web")),)),
    ),
    "one identity twice in provides": (
        "provides",
        lambda ns: ns.update(provides=(_provided(Stock), _provided(StockTwin))),
    ),
    "one identity twice in requires": (
        "requires",
        lambda ns: ns.update(requires=(Stock, Couriers, Stock)),
    ),
    "a resource kind no host supplies": (
        "resources",
        lambda ns: ns.update(resources=("cache",)),
    ),
    "a resource twice": (
        "resources",
        lambda ns: ns.update(resources=("database", "database")),
    ),
    "a battery that is no path": (
        "batteries",
        lambda ns: ns.update(batteries=(*_entries(ns, "batteries"), "a..b")),
    ),
    "a battery twice": (
        "batteries",
        lambda ns: ns.update(batteries=("django.contrib.humanize",) * 2),
    ),
    "the app as its own battery": (
        "batteries",
        lambda ns: ns.update(batteries=(*_entries(ns, "batteries"), ns["name"])),
    ),
}


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("mutation", sorted(_MUTATIONS))
@_HYPOTHESIS
@given(namespace=_declarations())
def test_one_element_outside_the_grammar_is_refused_naming_the_app_and_the_field(
    mutation: str, namespace: Namespace
) -> None:
    field, change = _MUTATIONS[mutation]
    change(namespace)
    assert {found for found, _ in _refusals(namespace)} == {field}


# The closed namespace.


def test_the_closed_namespace_accounts_for_every_public_name_of_a_django_config() -> (
    None
):
    # A Django release that adds an option, a method or instance state to
    # AppConfig fails here first, on that version of the matrix.
    instance = AppConfig("neolith", neolith)
    public = {
        name for name in {*dir(AppConfig), *vars(instance)} if not name.startswith("_")
    }
    options = {"name", "label", "verbose_name", "path", "default_auto_field"}
    state = {"module", "apps", "models", "models_module"}
    methods = {"create", "get_model", "get_models", "import_models"}
    assert public == options | state | methods | {"ready"}
    # Django reads ``default`` with getattr and never defines it.
    assert "default" not in dir(AppConfig)
    assert {*_FIELDS, *options, "default", "ready"} == ADMITTED_NAMES


@pytest.mark.parametrize("name", sorted(ADMITTED_NAMES - {"name", "label", *_FIELDS}))
def test_a_declaring_class_may_set_each_django_option_and_ready(name: str) -> None:
    config = _declare(_valid(**{name: lambda self: None}))
    assert name in vars(config)
    assert declaration_of(config) is not None


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    "name",
    [
        "helper",
        "reload",
        "Meta",
        "import_models",
        "create",
        "get_model",
        "get_models",
        "module",
        "apps",
        "models",
        "models_module",
        "require",
        "Label",
        "neolith_name",
    ],
)
def test_any_other_public_name_is_refused(name: str) -> None:
    field, text = _refusal(_valid(**{name: lambda self: None}))
    assert field == name
    assert all(admitted in text for admitted in ADMITTED_NAMES)
    assert 'starts with "_"' in text
    # Following the advice gives an admitted class.
    assert declaration_of(_declare(_valid(**{f"_{name}": lambda self: None})))


@pytest.mark.spec("APP-6")
def test_a_public_name_in_a_mixin_is_refused_naming_the_mixin() -> None:
    reports = type("Reports", (), {"__module__": __name__, "report": lambda self: 1})
    field, text = _refusal(_valid(), reports)
    assert field == "report"
    assert text.startswith(f"{__name__}.Reports holds it; ")


def test_a_mixin_may_hold_private_names_and_the_shared_names() -> None:
    class Quiet:
        verbose_name = "Stock"
        default_auto_field = "django.db.models.BigAutoField"
        _cache: tuple[str, ...] = ()

        def ready(self) -> None:
            pass

        def _helper(self) -> None:
            pass

    config = _declare(_valid(), Quiet)
    assert _snapshot(config).label == "stock"
    assert config.verbose_name == "Stock"


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("name", sorted(ADMITTED_NAMES - SHARED_NAMES))
def test_a_mixin_holds_no_part_of_the_declaration(name: str) -> None:
    mixin = type("Mixin", (), {"__module__": __name__, name: ("database",)})
    refusals = _refusals(_valid(), mixin)
    assert (name, True) in [
        (field, f"{__name__}.Mixin holds it" in text) for field, text in refusals
    ]


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("name", sorted(ADMITTED_NAMES - SHARED_NAMES - {"name"}))
def test_a_base_class_holds_no_part_of_the_declaration(name: str) -> None:
    # Without ``name`` in its body a class is a base class.
    refusals = _refusals({name: ("database",)}, name="Base")
    held = [text for field, text in refusals if "holds it; " in text]
    assert {field for field, _ in refusals} == {name}
    assert len(held) == 1
    assert held[0].startswith(f"{__name__}.Base holds it; ")
    assert "the class whose own body sets name" in held[0]


def test_a_base_class_is_not_a_default_and_its_declaring_subclass_is() -> None:
    def ready(self: NeolithAppConfig) -> None:
        pass

    base = _declare(
        {"verbose_name": "Shop", "default_auto_field": "x.Y", "ready": ready},
        name="Base",
    )
    assert vars(base)["default"] is False
    assert declaration_of(base) is None

    config = _declare(_valid(), base)
    assert vars(config)["default"] is True
    assert _snapshot(config).config is config
    assert NeolithAppConfig.default is False
    assert declaration_of(NeolithAppConfig) is None


@pytest.mark.spec("APP-6")
def test_a_base_class_whose_default_was_rebound_is_refused_in_a_subclass() -> None:
    # Only the False that neolith gave the base class is passed over.
    base = _declare({}, name="Base")
    setattr(base, "default", True)  # noqa: B010
    field, text = _refusal(_valid(), base)
    assert field == "default"
    assert text.startswith(f"{__name__}.Base holds it; ")


def test_a_declaring_class_keeps_the_default_it_sets() -> None:
    assert _declare(_valid(default=False)).default is False


@pytest.mark.spec("APP-6", "APP-7")
@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({}, []),
        ({"label": "other"}, ["label"]),
        ({"name": "shop.other", "label": "other"}, []),
        ({"helper": 1}, ["helper"]),
        (
            {"name": "shop.child", "label": "Child", "requires": [Stock], "helper": 1},
            ["helper", "label", "requires"],
        ),
    ],
    ids=["empty", "relabelled", "redeclared", "with a public name", "several"],
)
def test_a_subclass_of_a_declaring_class_is_refused_with_its_other_refusals(
    body: Namespace, fields: list[str]
) -> None:
    # The parent's own body was checked when it was defined, and is not
    # reported again.
    parent = _declare(_valid(requires=(Couriers,)), name="ParentConfig")
    with pytest.raises(DeclarationError) as raised:
        _declare(body, parent)
    prefix = f"app {__name__}.StockConfig: "
    lines = [line.removeprefix(prefix) for line in str(raised.value).split("\n")]
    extends = (
        f"it extends {__name__}.ParentConfig, which holds a declaration; a "
        "declaration cannot be extended or relabelled"
    )
    assert extends in lines
    lines.remove(extends)
    assert [line.split(": ")[0] for line in lines] == fields


# The app's name.


@pytest.mark.spec("APP-3")
@pytest.mark.parametrize("label", ["a", "stock", "order_desk", "a1", "a_", "x9_y"])
def test_a_label_that_is_a_segment_is_admitted(label: str) -> None:
    assert _snapshot(_declare(_valid(label=label))).label == label


class _Text(str):
    __slots__ = ()


@pytest.mark.spec("APP-3", "APP-6")
@pytest.mark.parametrize(
    "label",
    [
        "",
        "Stock",
        "sTock",
        "1stock",
        "_stock",
        "order-desk",
        "shop.stock",
        "stock ",
        "stock\n",
        "st\N{LATIN SMALL LETTER O WITH DIAERESIS}ck",
        "\N{FULLWIDTH LATIN SMALL LETTER S}tock",
        "*",
        _Text("stock"),
        b"stock",
        5,
        None,
        ("stock",),
    ],
    ids=repr,
)
def test_a_label_outside_the_grammar_is_refused(label: object) -> None:
    field, text = _refusal(_valid(label=label))
    assert field == "label"
    assert "lowercase ASCII letter" in text
    assert 'label = "<app>"' in text


@pytest.mark.spec("APP-3", "APP-6")
def test_a_declaring_class_writes_its_own_label() -> None:
    # Also when Django's default, the last component of name, would do.
    field, text = _refusal({"name": "shop.stock"})
    assert (field, text.split(";")[0]) == ("label", "missing")
    assert _snapshot(_declare({"name": "shop.stock", "label": "stock"})).label


@pytest.mark.parametrize("name", ["stock", "shop.stock", "_shop.Stock2", "données.x"])
def test_a_name_that_is_a_dotted_path_is_admitted(name: str) -> None:
    assert _snapshot(_declare(_valid(name=name))).name == name


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    "name",
    [
        *("", ".", "shop.", ".stock", "shop..stock", "shop.1x", "shop stock"),
        *("shop-x", _Text("shop.stock"), b"shop.stock", 5, None, ("shop.stock",)),
    ],
    ids=repr,
)
def test_a_name_that_is_not_a_dotted_path_is_refused(name: object) -> None:
    field, text = _refusal(_valid(name=name))
    assert field == "name"
    assert 'name = "<package>.<app>"' in text


# The form of a field.

_EXAMPLE_ENTRY: dict[str, object] = {
    "provides": _provided(),
    "requires": Stock,
    "resources": "database",
    "batteries": "django.contrib.humanize",
}


@pytest.mark.parametrize("field", _FIELDS)
def test_each_field_is_admitted_as_a_tuple_and_may_be_left_out(field: str) -> None:
    assert _snapshot(_declare(_valid())).provides == ()
    config = _declare(_valid(**{field: (_EXAMPLE_ENTRY[field],)}))
    assert len(getattr(_snapshot(config), field)) == 1


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("value", ["django.contrib.humanize", Stock, Unmarked, int])
def test_a_field_that_is_a_str_or_a_class_misses_its_trailing_comma(
    field: str, value: object
) -> None:
    # The value is also checked as the one entry of the tuple, whatever its
    # kind. Only an entry its field admits adds no second line.
    refusals = _refusals(_valid(**{field: value}))
    assert {found for found, _ in refusals} == {field}
    assert sum("trailing comma" in text for _, text in refusals) == 1
    admitted = (field, value) in [
        ("batteries", "django.contrib.humanize"),
        ("requires", Stock),
    ]
    assert len(refusals) == (1 if admitted else 2)


class _Pair(tuple[Any, ...]):
    __slots__ = ()


_Named = collections.namedtuple("_Named", ["entry"])


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize(
    "wrap",
    [
        list,
        set,
        frozenset,
        lambda entries: dict.fromkeys(entries, True),
        iter,
        _Pair,
        lambda entries: _Named(*entries),
        lambda entries: None,
        lambda entries: 5,
        lambda entries: b"database",
    ],
    ids=[
        "list",
        "set",
        "frozenset",
        "dict",
        "iterator",
        "tuple subclass",
        "namedtuple",
        "None",
        "int",
        "bytes",
    ],
)
def test_a_field_that_is_not_exactly_a_tuple_is_refused(
    field: str, wrap: Callable[[tuple[object, ...]], object]
) -> None:
    entries = (_EXAMPLE_ENTRY[field],) if field != "provides" else ("x",)
    value = wrap(entries)
    refusals = _refusals(_valid(**{field: value}))
    assert {found for found, _ in refusals} == {field}
    assert sum("write a tuple, (" in text for _, text in refusals) == 1
    # The items of a builtin container are checked as entries, and "x" is
    # not a Provided.
    read = type(value) in (list, set, frozenset, dict) and field == "provides"
    assert len(refusals) == (2 if read else 1)


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("field", _FIELDS)
def test_an_empty_field_is_refused_and_removing_the_line_is_admitted(
    field: str,
) -> None:
    found, text = _refusal(_valid(**{field: ()}))
    assert (found, "remove the line" in text) == (field, True)
    assert getattr(_snapshot(_declare(_valid())), field) == ()


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize("field", _EVENT_FIELDS)
@pytest.mark.parametrize("value", [(), ("stock.changed.v1",), None])
def test_an_events_field_is_refused_even_when_empty(field: str, value: object) -> None:
    found, text = _refusal(_valid(**{field: value}))
    assert (found, text) == (
        field,
        "events arrive with spec 04; remove the attribute",
    )


# provides.


def test_provided_checks_nothing_when_it_is_called() -> None:
    # The class body runs before the config class exists, so an entry could
    # not name its app. The class's check reports it instead.
    for entry in (
        Provided(),
        Provided(5),
        Provided(Stock),
        Provided(Stock, implementation=5, principal_from="*"),
    ):
        assert _refusals(_valid(provides=(entry,)))


def test_python_refuses_an_argument_provided_does_not_take() -> None:
    with pytest.raises(TypeError):
        Provided(Stock, _IMPLEMENTATION)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Provided(Stock, implementation=_IMPLEMENTATION, callers=())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Provided(contract=Stock)  # type: ignore[call-arg]


def test_a_provided_is_frozen_copies_and_pickles() -> None:
    entry = _provided(Stock, _IMPLEMENTATION, ("web", "desk"))
    with pytest.raises(AttributeError):
        entry._principal_from = ("*",)
    with pytest.raises(AttributeError):
        setattr(entry, "principal_from", ("*",))  # noqa: B010
    with pytest.raises(AttributeError):
        del entry._contract
    with pytest.raises(TypeError):
        type("Loose", (Provided,), {})
    assert repr(entry) == (
        f"Provided(the class {Stock.__module__}.Stock, "
        f"implementation={_IMPLEMENTATION!r}, principal_from=a tuple)"
    )
    for clone in (
        copy.copy(entry),
        copy.deepcopy(entry),
        pickle.loads(pickle.dumps(entry)),  # noqa: S301
        pickle.loads(pickle.dumps(Provided())),  # noqa: S301
    ):
        assert type(clone) is Provided
    twin = pickle.loads(pickle.dumps(entry))  # noqa: S301
    snapshot = _snapshot(_declare(_valid(provides=(twin,))))
    assert snapshot.provides == (
        Provision(Contract.of(Stock), _IMPLEMENTATION, ("web", "desk")),
    )
    # A marker for a missing argument survives a copy as the same marker.
    assert _refusals(_valid(provides=(copy.deepcopy(Provided()),))) == _refusals(
        _valid(provides=(Provided(),))
    )


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    "entry",
    [Stock, "stock.v1", (Stock, _IMPLEMENTATION, ()), None, Contract.of(Stock)],
    ids=["class", "identity", "tuple", "None", "Contract"],
)
def test_a_provides_entry_that_is_not_a_provided_is_refused(entry: object) -> None:
    field, text = _refusal(_valid(provides=(_provided(StockV2), entry)))
    assert field == "provides"
    assert text.startswith("entry 1: ")
    assert "is not a Provided" in text


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    ("field", "wrap"),
    [
        ("provides", lambda value: (_provided(value),)),
        ("requires", lambda value: (value,)),
    ],
    ids=["provides", "requires"],
)
@pytest.mark.parametrize(
    "value",
    [Unmarked, int, "stock.v1", Contract.of(Stock), None, 5],
    ids=["unmarked", "int", "identity", "Contract", "None", "5"],
)
def test_a_contract_is_named_by_a_class_that_carries_the_mark(
    field: str, wrap: Callable[[object], tuple[object, ...]], value: object
) -> None:
    found, text = _refusal(_valid(**{field: wrap(value)}))
    assert found == field
    assert text.startswith("entry 0: ")
    assert "@contract" in text


@pytest.mark.spec("APP-6")
def test_a_provided_without_its_contract_is_refused() -> None:
    field, text = _refusal(
        _valid(provides=(Provided(implementation=_IMPLEMENTATION, principal_from=()),))
    )
    assert (field, text.split(";")[0]) == (
        "provides",
        "entry 0: the contract is missing",
    )


@pytest.mark.parametrize(
    "path", ["a.B", "shop.stock.services.StockService", "_a._b", "données.Service"]
)
def test_an_implementation_is_a_dotted_path_of_two_or_more_identifiers(
    path: str,
) -> None:
    snapshot = _snapshot(_declare(_valid(provides=(_provided(implementation=path),))))
    assert snapshot.provides[0].implementation == path


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    "path",
    [
        *("", "Service", "a..B", "a.", ".B", "a.1b", "a.b-c", "a:B", "a.B "),
        *(_Text("a.B"), b"a.B", 5, None, int, ("a.B",)),
    ],
    ids=repr,
)
def test_an_implementation_in_another_form_is_refused(path: object) -> None:
    field, text = _refusal(_valid(provides=(_provided(implementation=path),)))
    assert field == "provides"
    assert text.startswith("stock.v1: implementation ")
    assert 'implementation="<module>.<Class>"' in text


@pytest.mark.spec("APP-6")
def test_a_provided_without_its_implementation_is_refused() -> None:
    field, text = _refusal(_valid(provides=(Provided(Stock, principal_from=()),)))
    assert (field, text.split(";")[0]) == (
        "provides",
        "stock.v1: implementation is missing",
    )


@pytest.mark.spec("APP-5")
@pytest.mark.parametrize(
    "callers", [(), ("web",), ("web", "order_desk", "stock"), ("a1",)]
)
def test_an_allow_list_is_a_tuple_of_app_names_and_may_be_empty(
    callers: tuple[str, ...],
) -> None:
    # The providing app's own name, "stock", is an entry like any other.
    snapshot = _snapshot(
        _declare(_valid(provides=(_provided(principal_from=callers),)))
    )
    assert snapshot.provides[0].principal_from == callers


@pytest.mark.spec("APP-5", "APP-6")
def test_a_provided_without_its_allow_list_is_refused_and_an_empty_one_admitted() -> (
    None
):
    field, text = _refusal(
        _valid(provides=(Provided(Stock, implementation=_IMPLEMENTATION),))
    )
    assert (field, text.split(";")[0]) == (
        "provides",
        "stock.v1: principal_from is missing",
    )
    assert "principal_from=()" in text
    assert _snapshot(_declare(_valid(provides=(_provided(),)))).provides


@pytest.mark.spec("APP-5", "APP-6")
@pytest.mark.parametrize(
    "callers",
    ["web", ["web"], {"web"}, frozenset(), None, _Pair(("web",)), 5, Stock],
    ids=repr,
)
def test_an_allow_list_that_is_not_exactly_a_tuple_is_refused(callers: object) -> None:
    refusals = _refusals(_valid(provides=(_provided(principal_from=callers),)))
    assert {field for field, _ in refusals} == {"provides"}
    lines = [text for _, text in refusals if "not a tuple" in text]
    assert len(lines) == 1
    assert lines[0].startswith("stock.v1: principal_from is ")
    # A str or a class is the one entry of a tuple that lost its comma.
    assert ("trailing comma" in lines[0]) == (callers in ("web", Stock))
    # Only the class is an entry that is refused as well.
    assert len(refusals) == (2 if callers is Stock else 1)


@pytest.mark.spec("APP-6")
def test_a_one_element_tuple_without_its_comma_is_named_as_that() -> None:
    # (Provided(...)) and ("orders") are the entry itself, not a tuple.
    field, text = _refusal(_valid(provides=_provided()))
    assert field == "provides"
    assert "got a neolith._declaration.Provided; the trailing comma" in text
    assert _snapshot(_declare(_valid(provides=(_provided(),)))).provides

    field, text = _refusal(_valid(provides=(_provided(principal_from="orders"),)))
    assert field == "provides"
    assert text.endswith(
        'comma of a one-element tuple is missing, write principal_from=("<app>",)'
    )
    written = _provided(principal_from=("orders",))
    assert _snapshot(_declare(_valid(provides=(written,)))).provides[0].principal_from

    # In another field a Provided is read the same way, as its one entry.
    for other in ("requires", "resources", "batteries"):
        lines = [text for _, text in _refusals(_valid(**{other: _provided()}))]
        assert len(lines) == 2
        assert sum("trailing comma" in text for text in lines) == 1


@pytest.mark.spec("APP-5", "APP-6")
@pytest.mark.parametrize(
    ("callers", "said"),
    [
        (("*",), "there is no wildcard"),
        (("web", "*"), "there is no wildcard"),
        (("Web",), "is not an app's name"),
        (("shop.web",), "is not an app's name"),
        (("web*",), "is not an app's name"),
        (("",), "is not an app's name"),
        ((_Text("web"),), "is not an app's name"),
        ((5,), "is not an app's name"),
        ((None,), "is not an app's name"),
        ((("web",),), "is not an app's name"),
        (("web", "desk", "web"), "'web' is listed twice"),
    ],
    ids=repr,
)
def test_an_allow_list_entry_is_one_app_name_once(
    callers: tuple[object, ...], said: str
) -> None:
    field, text = _refusal(_valid(provides=(_provided(principal_from=callers),)))
    assert field == "provides"
    assert text.startswith("stock.v1: principal_from: ")
    assert said in text


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    ("field", "entries", "said"),
    [
        ("provides", (Stock, Stock), "Stock is listed twice"),
        ("provides", (Stock, StockTwin), "Stock and as "),
        ("provides", (StockTwin, StockV2, Stock), "StockTwin and as "),
        ("requires", (Stock, Stock), "Stock is listed twice"),
        ("requires", (Stock, StockTwin), "Stock and as "),
        ("requires", (StockTwin, StockV2, Stock), "StockTwin and as "),
    ],
)
def test_one_identity_is_listed_once_in_a_field(
    field: str, entries: tuple[type, ...], said: str
) -> None:
    written = (
        tuple(_provided(entry) for entry in entries) if field == "provides" else entries
    )
    found, text = _refusal(_valid(**{field: written}))
    assert found == field
    assert text.startswith("stock.v1: ")
    assert said in text


@pytest.mark.spec("APP-6")
def test_one_identity_has_one_class_across_provides_and_requires() -> None:
    field, text = _refusal(_valid(provides=(_provided(Stock),), requires=(StockTwin,)))
    assert field == "requires"
    assert "StockTwin here and" in text
    assert "Stock in provides" in text


def test_an_app_may_require_a_contract_it_provides_and_other_versions() -> None:
    snapshot = _snapshot(
        _declare(_valid(provides=(_provided(Stock),), requires=(Stock, StockV2)))
    )
    assert [entry.identity for entry in snapshot.requires] == ["stock.v1", "stock.v2"]
    assert snapshot.provides[0].contract is snapshot.requires[0]


# resources and batteries.


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    "entries",
    [
        ("cache",),
        ("queue",),
        ("object store",),
        ("Database",),
        ("database", "cache"),
        (_Text("database"),),
        (5,),
        (None,),
        (("database",),),
    ],
    ids=repr,
)
def test_a_resource_is_a_kind_a_host_supplies(entries: tuple[object, ...]) -> None:
    field, text = _refusal(_valid(resources=entries))
    assert field == "resources"
    assert 'the kinds admitted are "database"' in text


@pytest.mark.spec("APP-6")
def test_a_resource_kind_is_listed_once() -> None:
    assert _refusal(_valid(resources=("database", "database"))) == (
        "resources",
        "'database' is listed twice; a kind is listed once",
    )
    assert _snapshot(_declare(_valid(resources=("database",)))).resources == (
        "database",
    )


@pytest.mark.parametrize(
    "entries",
    [("humanize",), ("django.contrib.humanize", "django.contrib.sites"), ("shop",)],
)
def test_a_battery_is_a_dotted_app_name(entries: tuple[str, ...]) -> None:
    assert _snapshot(_declare(_valid(batteries=entries))).batteries == entries


@pytest.mark.spec("APP-6")
@pytest.mark.parametrize(
    ("entries", "said"),
    [
        (("",), "is not a Django app name"),
        (("django..humanize",), "is not a Django app name"),
        (("django.contrib.humanize.",), "is not a Django app name"),
        (("django-humanize",), "is not a Django app name"),
        (("django.contrib.admin.apps:SimpleAdminConfig",), "is not a Django app name"),
        ((_Text("humanize"),), "is not a Django app name"),
        ((5,), "is not a Django app name"),
        ((NeolithAppConfig,), "is not a Django app name"),
        (("humanize", "sites", "humanize"), "'humanize' is listed twice"),
        (("shop.stock",), "is the app itself"),
    ],
    ids=repr,
)
def test_a_battery_in_another_form_is_refused(
    entries: tuple[object, ...], said: str
) -> None:
    field, text = _refusal(_valid(batteries=entries))
    assert field == "batteries"
    assert said in text


# One error for one class.

_REACHES: dict[str, tuple[Namespace, dict[str, object], list[str]]] = {
    "name, then label": (
        _valid(name="shop.", label="Stock"),
        {},
        ["name: 'shop.' is not", "label: 'Stock' is not"],
    ),
    "name, then a battery twice": (
        _valid(name=5, batteries=("sites", "sites")),
        {},
        ["name: an int is not", "batteries: 'sites' is listed twice"],
    ),
    "provides without a comma, then its allow-list without one": (
        _valid(provides=_provided(principal_from="orders")),
        {},
        ["provides: got a neolith", "provides: stock.v1: principal_from is 'orders'"],
    ),
    "an allow-list without a comma, then the wildcard": (
        _valid(provides=(_provided(principal_from="*"),)),
        {},
        ["principal_from is '*', not a tuple", "there is no wildcard"],
    ),
    "an allow-list as a list, then its entries": (
        _valid(provides=(_provided(principal_from=["Web", "Web", "*"]),)),
        {},
        [
            "principal_from is a list",
            "entry 0, 'Web', is not",
            "entry 1, 'Web', is not",
            'entry 2 is "*"',
            "'Web' is listed twice",
        ],
    ),
    "an allow-list entry refused, then repeated": (
        _valid(provides=(_provided(principal_from=("*", "ok", "*")),)),
        {},
        ['entry 0 is "*"', 'entry 2 is "*"', "'*' is listed twice"],
    ),
    "requires as a list, then an identity twice": (
        _valid(requires=[Stock, Stock]),
        {},
        ["requires: got a list", "requires: stock.v1: "],
    ),
    "requires without a comma, then an unmarked class": (
        _valid(requires=Unmarked),
        {},
        ["requires: got the class", "requires: entry 0: "],
    ),
    "an identity twice in requires, then another class than provides": (
        _valid(provides=(_provided(Stock),), requires=(Stock, StockTwin)),
        {},
        [".Stock and as ", "StockTwin here and"],
    ),
    "batteries without a comma, then the app itself": (
        _valid(name="shop.c", batteries="shop.c"),
        {},
        ["batteries: got 'shop.c'", "batteries: entry 0, 'shop.c', is the app itself"],
    ),
    "the app itself, then twice": (
        _valid(batteries=("shop.stock", "shop.stock")),
        {},
        [
            "entry 0, 'shop.stock', is the app",
            "entry 1, 'shop.stock', is the app",
            "'shop.stock' is listed twice",
        ],
    ),
    "a battery that is no path, then twice": (
        _valid(batteries=("a..b", "a..b")),
        {},
        ["entry 0, 'a..b', is not", "entry 1, 'a..b', is not", "'a..b' is listed"],
    ),
    "resources as a list, then a kind no host supplies": (
        _valid(resources=["cache"]),
        {},
        ["resources: got a list", "resources: entry 0, 'cache', is not"],
    ),
    "a kind no host supplies, then twice": (
        _valid(resources=("cache", "cache")),
        {},
        ["entry 0, 'cache', is not", "entry 1, 'cache', is not", "'cache' is listed"],
    ),
    "provides as a list, then every argument of its entry": (
        _valid(provides=[Provided(Unmarked)]),
        {},
        [
            "provides: got a list",
            "provides: entry 0: Unmarked is not a contract",
            "provides: entry 0: implementation is missing",
            "provides: entry 0: principal_from is missing",
        ],
    ),
    "a contract, then its implementation, then its allow-list": (
        _valid(provides=(_provided(5, "Service", ("*",)),)),
        {},
        [
            "entry 0: an int is not a contract class",
            "entry 0: implementation 'Service'",
            "entry 0: principal_from: entry 0 is",
        ],
    ),
    "an identity twice in provides, then its implementation": (
        _valid(provides=(_provided(), _provided(implementation="Service"))),
        {},
        ["Stock is listed twice", "stock.v1: implementation 'Service'"],
    ),
    "a field in a mixin, then its form and its entries": (
        _valid(),
        {"requires": [Stock, Stock]},
        [
            "requires: tests.test_declaration.Mixin holds it",
            "requires: got a list",
            "requires: stock.v1: ",
        ],
    ),
    "a label in a mixin, then its form": (
        {"name": "shop.stock"},
        {"label": "Other"},
        ["label: tests.test_declaration.Mixin holds it", "label: 'Other' is not"],
    ),
    "a name in a mixin, then its form": (
        {"label": "stock"},
        {"name": "shop."},
        [
            "label: tests.test_declaration.StockConfig holds it",
            "name: tests.test_declaration.Mixin holds it",
            "name: 'shop.' is not",
        ],
    ),
    "a base class with parts of a declaration, then their forms": (
        {"label": "Stock", "resources": ("cache",), "batteries": "a..b"},
        {},
        [
            "label: tests.test_declaration.StockConfig holds it",
            "label: 'Stock' is not",
            "resources: tests.test_declaration.StockConfig holds it",
            "resources: entry 0, 'cache', is not",
            "batteries: tests.test_declaration.StockConfig holds it",
            "batteries: got 'a..b'",
            "batteries: entry 0, 'a..b', is not",
        ],
    ),
}


@pytest.mark.spec("APP-7")
@pytest.mark.parametrize("case", sorted(_REACHES))
def test_a_refusal_of_an_entry_or_a_placement_hides_none_behind_it(case: str) -> None:
    namespace, mixed_in, expected = _REACHES[case]
    bases = (
        (type("Mixin", (), {"__module__": __name__, **mixed_in}),) if mixed_in else ()
    )
    lines = [f"{field}: {text}" for field, text in _refusals(namespace, *bases)]
    for fragment in expected:
        assert sum(fragment in line for line in lines) == 1, (fragment, lines)
    assert len(lines) == len(expected), lines


# The matrix: where a value is written, by the kind of value. Each kind holds
# one entry that its field refuses, as the second mistake behind the first.

_BAD_ENTRY: dict[str, tuple[object, str]] = {
    "provides": ("stock.v1", "'stock.v1' is not a Provided"),
    "requires": ("shop.Stock", "'shop.Stock' is not a contract class"),
    "resources": ("cache", "'cache', is not a resource kind"),
    "batteries": ("a..b", "'a..b', is not a Django app name"),
    "principal_from": ("Web", "'Web', is not an app's name"),
}
_BAD_CLASS: dict[str, str] = {
    "provides": "Unmarked is not a Provided",
    "requires": "Unmarked is not a contract",
    "resources": "Unmarked, is not a resource kind",
    "batteries": "Unmarked, is not a Django app name",
    "principal_from": "Unmarked, is not an app's name",
}
_BAD_PROVIDED: dict[str, list[str]] = {
    "provides": [
        "entry 0: Unmarked is not a contract",
        "entry 0: implementation is missing",
        "entry 0: principal_from is missing",
    ],
    "requires": ["a neolith._declaration.Provided is not a contract class"],
    "resources": ["a neolith._declaration.Provided, is not a resource kind"],
    "batteries": ["a neolith._declaration.Provided, is not a Django app name"],
    "principal_from": ["a neolith._declaration.Provided, is not an app's name"],
}
# How each kind is refused as a container, and whether its entries are read.
_READ_KINDS: dict[str, Callable[[object], object]] = {
    "list": lambda entry: [entry],
    "set": lambda entry: {entry},
    "frozenset": lambda entry: frozenset({entry}),
    "dict": lambda entry: {entry: True},
}
_STOP_KINDS: dict[str, Callable[[object], object]] = {
    "iterator": lambda entry: iter([entry]),
    "generator": lambda entry: (item for item in [entry]),
    "tuple subclass": lambda entry: _Pair((entry,)),
    "namedtuple": lambda entry: _Named(entry),
    "list subclass": lambda entry: _Listed([entry]),
    "bytes": lambda entry: b"cache",
    "range": lambda entry: range(1),
    "None": lambda entry: None,
    "int": lambda entry: 5,
    "lazy": lambda entry: SimpleLazyObject(lambda: pytest.fail("evaluated")),
}
_KINDS = ["str", "class", "Provided", *_READ_KINDS, "empty tuple", *_STOP_KINDS]


class _Listed(list[Any]):
    __slots__ = ()

    def __iter__(self) -> Iterator[Any]:
        pytest.fail("the author's code ran")


def _written(place: str, value: object) -> Namespace:
    # A second field holds a mistake of its own, in every cell.
    if place == "principal_from":
        return _valid(label="Stock", provides=(_provided(principal_from=value),))
    return _valid(label="Stock", **{place: value})


@pytest.mark.spec("APP-7")
@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("place", [*_FIELDS, "principal_from"])
def test_every_refusal_reaches_the_diagnostic(place: str, kind: str) -> None:
    # A check is skipped only where the value cannot be read safely, judged
    # by its type: the stop kinds. Everywhere else the entry behind the
    # refused container is refused beside it.
    entry, said = _BAD_ENTRY[place]
    comma, container = "trailing comma", "write a tuple"
    if place == "principal_from":
        container = "not a tuple; list the calling apps"
    behind: list[str]
    if kind == "str":
        value, first, behind = entry, comma, [said]
    elif kind == "class":
        value, first, behind = Unmarked, comma, [_BAD_CLASS[place]]
    elif kind == "Provided":
        value, first, behind = Provided(Unmarked), comma, _BAD_PROVIDED[place]
    elif kind in _READ_KINDS:
        value, first, behind = _READ_KINDS[kind](entry), container, [said]
    elif kind == "empty tuple":
        # An empty allow-list is admitted, and says that no app may assert.
        value, behind = (), []
        first = "" if place == "principal_from" else "an empty tuple"
    else:
        value, first, behind = _STOP_KINDS[kind](entry), container, []

    refusals = _refusals(_written(place, value))
    field = "provides" if place == "principal_from" else place
    lines = [text for found, text in refusals if found == field]
    assert [found for found, _ in refusals if found != field] == ["label"]
    expected = [*([first] if first else []), *behind]
    for fragment in expected:
        assert sum(fragment in line for line in lines) == 1, (fragment, lines)
    assert len(lines) == len(expected), lines


@pytest.mark.spec("APP-7")
@pytest.mark.parametrize("field", ["provides", "requires"])
def test_a_class_listed_twice_is_reported_before_it_names_a_contract(
    field: str,
) -> None:
    # Marked with @contract it is still the same class, listed twice.
    def write(first: type, second: type) -> Namespace:
        if field == "requires":
            return _valid(requires=(first, second))
        return _valid(provides=(_provided(first), _provided(second)))

    lines = [text for _, text in _refusals(write(Unmarked, Unmarked))]
    assert sum("is not a contract" in text for text in lines) == 2
    assert sum("Unmarked is listed twice" in text for text in lines) == 1
    assert len(lines) == 3
    assert len(_refusals(write(Unmarked, int))) == 2
    assert len(_refusals(write(Stock, Stock))) == 1


@pytest.mark.spec("APP-7")
def test_every_refusal_of_one_class_is_reported_in_one_error() -> None:
    class Reports:
        export = True

    refusals = _refusals(
        {
            "name": "shop.stock",
            "label": "Stock",
            "provides": (
                Provided(Stock, implementation="Service", principal_from=("*",)),
                Provided(Unmarked),
                "stock.v2",
            ),
            "requires": [Stock],
            "resources": ("database", "cache", "database"),
            "batteries": "django.contrib.humanize",
            "subscribes": (),
            "helper": 1,
        },
        Reports,
    )
    assert [field for field, _ in refusals] == [
        "batteries",
        "export",
        "helper",
        "label",
        "provides",
        "provides",
        "provides",
        "provides",
        "provides",
        "provides",
        "requires",
        "resources",
        "resources",
        "subscribes",
    ]


def test_a_declaration_error_is_an_improperly_configured_and_pickles() -> None:
    with pytest.raises(ImproperlyConfigured) as raised:
        _declare(_valid(label="Stock", helper=1))
    assert type(raised.value) is DeclarationError
    clone = pickle.loads(pickle.dumps(raised.value))  # noqa: S301
    assert (type(clone), str(clone)) == (DeclarationError, str(raised.value))


def test_a_refused_class_does_not_exist() -> None:
    before = set(NeolithAppConfig.__subclasses__())
    _refusals(_valid(label="Stock"))
    remaining = [
        declaration_of(config)
        for config in set(NeolithAppConfig.__subclasses__()) - before
    ]
    assert remaining in ([], [None])


# Values are judged by their type alone.


def test_a_lazy_object_in_a_class_body_is_never_evaluated() -> None:
    calls: list[int] = []

    def setup() -> object:
        calls.append(1)
        raise AssertionError("evaluated")

    def lazy() -> object:
        return SimpleLazyObject(setup)

    admitted = _declare(
        _valid(verbose_name=lazy(), path=lazy(), _storage=lazy(), ready=lazy())
    )
    assert _snapshot(admitted).label == "stock"
    for namespace in (
        _valid(name=lazy()),
        _valid(label=lazy()),
        _valid(storage=lazy()),
        *(_valid(**{field: lazy()}) for field in (*_FIELDS, *_EVENT_FIELDS)),
        *(_valid(**{field: (lazy(),)}) for field in _FIELDS),
        _valid(provides=(Provided(lazy(), implementation=lazy()),)),
        _valid(provides=(_provided(principal_from=lazy()),)),
        _valid(provides=(_provided(principal_from=("web", lazy())),)),
    ):
        assert _refusals(namespace)
    assert repr(Provided(lazy(), implementation=lazy(), principal_from=lazy()))
    assert calls == []


@pytest.mark.parametrize(
    "namespace",
    [
        _valid(label="X" * 100_000),
        _valid(name="x" * 100_000 + "."),
        _valid(batteries=("-" * 100_000,)),
        _valid(resources=(10**5000,)),
        _valid(provides=(_provided(implementation="x" * 100_000),)),
        _valid(provides=(_provided(principal_from=("X" * 100_000,)),)),
    ],
    ids=["label", "name", "battery", "resource", "implementation", "caller"],
)
def test_a_message_is_bounded_whatever_was_written(namespace: Namespace) -> None:
    _field, text = _refusal(namespace)
    assert len(text) < 500


# The snapshot.


def test_the_host_reads_a_snapshot_that_rebinding_a_field_does_not_change() -> None:
    config = _declare(
        _valid(provides=(_provided(principal_from=("web",)),), requires=(Couriers,))
    )
    before = _snapshot(config)

    setattr(config, "provides", (_provided(principal_from=("web", "anyone")),))  # noqa: B010
    setattr(config, "requires", ())  # noqa: B010
    setattr(config, "resources", ["database"])  # noqa: B010
    setattr(config, "batteries", ("django.contrib.admin",))  # noqa: B010

    after = _snapshot(config)
    assert after is before
    assert after.provides[0].principal_from == ("web",)
    assert [entry.identity for entry in after.requires] == ["couriers.v3"]
    assert (after.resources, after.batteries) == ((), ())
    with pytest.raises(AttributeError):
        after.label = "other"  # type: ignore[misc]
    assert hash(after) == hash(before)


def test_only_a_class_own_snapshot_counts() -> None:
    config = _declare(_valid())
    for other in (None, 5, "shop.stock", config("shop", neolith), AppConfig, object):
        assert declaration_of(other) is None
    assert declaration_of(config) is _snapshot(config)


def test_a_snapshot_copied_onto_another_class_does_not_count() -> None:
    snapshot = _snapshot(_declare(_valid()))
    other = _declare({}, name="Base")
    setattr(other, "__neolith_declaration__", snapshot)  # noqa: B010
    assert vars(other)["__neolith_declaration__"] is snapshot
    assert declaration_of(other) is None


def test_a_snapshot_held_only_by_a_base_class_does_not_count() -> None:
    base = _declare({}, name="Base")
    config = _declare(_valid(), base)
    snapshot = _snapshot(config)
    # The snapshot names this class, and the class reaches it by inheritance.
    delattr(config, "__neolith_declaration__")
    setattr(base, "__neolith_declaration__", snapshot)  # noqa: B010
    assert getattr(config, "__neolith_declaration__").config is config  # noqa: B009
    assert declaration_of(config) is None


# Loading a declared app into a registry.

_COUNTER = itertools.count()

_APPS = """
from neolith import NeolithAppConfig, Provided
from tests.declaration_contracts import Couriers, Stock


class StockConfig(NeolithAppConfig):
    name = "{package}"
    label = "{package}"
    provides = (
        Provided(
            Stock,
            implementation="{package}.services.StockService",
            principal_from=("web",),
        ),
    )
    requires = (Couriers,)
    resources = ("database",)
"""

_SERVICES = """
from tests.declaration_contracts import Level, Sku


class StockService:
    def level(self, request: Sku) -> Level:
        return Level(on_hand=0)
"""

# The app that provides the contract _APPS requires, for a host to set up.
_COURIERS = "tests.host_apps.couriers"

MakeApp = Callable[..., str]


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MakeApp]:
    """Write an app package with the given ``apps`` module; return its name."""
    made: list[str] = []

    def make(apps: str = _APPS, **modules: str) -> str:
        package = f"declared_{next(_COUNTER)}"
        root = tmp_path / package
        root.mkdir()
        written = {"__init__": "", "apps": apps, "services": _SERVICES, **modules}
        for module, source in written.items():
            text = textwrap.dedent(source).replace("{package}", package)
            (root / f"{module}.py").write_text(text, encoding="utf-8")
        made.append(package)
        # The path finder caches a directory's listing by its modification
        # time, which need not change between two packages.
        importlib.invalidate_caches()
        return package

    monkeypatch.syspath_prepend(str(tmp_path))
    yield make
    for loaded in list(sys.modules):
        if loaded.split(".")[0] in made:
            del sys.modules[loaded]


def _installed() -> list[str]:
    return [config.name for config in global_apps.get_app_configs()]


def _config_class(package: str, module: str = "apps") -> type[NeolithAppConfig]:
    __import__(f"{package}.{module}")
    found = getattr(sys.modules[f"{package}.{module}"], "StockConfig")  # noqa: B009
    return cast("type[NeolithAppConfig]", found)


def test_django_picks_the_declaring_class_from_an_apps_module(
    make_app: MakeApp,
) -> None:
    # The module also holds NeolithAppConfig, by import, and a base class.
    # Django would otherwise count three candidates and use a plain AppConfig.
    package = make_app(
        _APPS.replace(
            "class StockConfig(NeolithAppConfig):",
            'class Base(NeolithAppConfig):\n    verbose_name = "Shop"\n\n\n'
            "class StockConfig(Base):",
        )
    )
    config = AppConfig.create(package)
    assert type(config) is _config_class(package)
    assert (config.name, config.label, config.verbose_name) == (
        package,
        package,
        "Shop",
    )


def test_django_refuses_two_declaring_classes_in_one_apps_module(
    make_app: MakeApp,
) -> None:
    package = make_app(
        _APPS + '\n\nclass Second(NeolithAppConfig):\n    name = "json"\n'
        '    label = "second"\n'
    )
    with pytest.raises(RuntimeError, match="more than one default AppConfig"):
        AppConfig.create(package)


_FRESH_PROCESS = """
import sys

from django.apps import AppConfig
from django.conf import settings

config = AppConfig.create(sys.argv[1])

from neolith._declaration import declaration_of

snapshot = declaration_of(type(config))
assert not settings.configured
assert not any(name.startswith(sys.argv[1] + ".services") for name in sys.modules)
print(type(config).__name__, snapshot.provides[0].contract.identity)
"""


def test_a_declaration_is_read_in_a_fresh_process_before_settings_exist(
    make_app: MakeApp,
) -> None:
    # The apps module imports its contract packages and no implementation.
    package = make_app(services="raise AssertionError('implementation imported')\n")
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _FRESH_PROCESS, package],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert (ran.stdout.strip(), ran.stderr[-600:]) == ("StockConfig stock.v1", "")


@pytest.mark.parametrize("entry", ["{package}", "{package}.apps.StockConfig"])
@pytest.mark.parametrize("neolith_first", [False, True])
def test_a_declared_app_loads_in_the_global_registry_beside_neolith(
    make_app: MakeApp, entry: str, neolith_first: bool
) -> None:
    # A process hosts one set of apps, so each set loads in its own.
    package = make_app()
    entries = [entry.replace("{package}", package), _COURIERS, "neolith"]
    printed = in_fresh_process(
        f"""
        import django
        from django.apps import apps
        from django.test.utils import override_settings

        from neolith._declaration import declaration_of
        from {package}.apps import StockConfig

        django.setup()
        config = apps.get_app_config({package!r})
        assert type(config) is StockConfig
        assert declaration_of(StockConfig).label == config.label == {package!r}
        assert apps.is_installed("neolith")
        # A second override builds new instances and checks them again.
        with override_settings(INSTALLED_APPS=settings.INSTALLED_APPS):
            assert apps.get_app_config({package!r}) is not config
        assert apps.get_app_config({package!r}) is config
        print("loaded")
        """,
        entries[::-1] if neolith_first else entries,
    )
    assert printed == "loaded\n"


def test_a_config_class_kept_outside_the_apps_module_is_an_entry(
    make_app: MakeApp,
) -> None:
    package = make_app(apps="", config=_APPS)
    printed = in_fresh_process(
        f"""
        import django
        from django.apps import apps

        from {package}.config import StockConfig

        django.setup()
        print(type(apps.get_app_config({package!r})) is StockConfig)
        """,
        [f"{package}.config.StockConfig", _COURIERS, "neolith"],
    )
    assert printed == "True\n"


@pytest.mark.spec("APP-1")
def test_a_declared_app_in_the_global_registry_needs_neolith_installed(
    make_app: MakeApp,
) -> None:
    package = make_app()
    before = _installed()
    for entries in ([package], [f"{package}.apps.StockConfig"], [package, "json"]):
        with (
            pytest.raises(DeclarationError) as raised,
            override_settings(INSTALLED_APPS=entries),
        ):
            pytest.fail("the override was admitted")
        assert str(raised.value) == (
            f"app {package}: INSTALLED_APPS: 'neolith' is not installed; "
            'add "neolith" to INSTALLED_APPS'
        )
        assert _installed() == before
    # Following the advice loads the app, beside the provider of the
    # contract it requires, in a process of its own.
    printed = in_fresh_process(
        f"""
        import django
        from django.apps import apps

        django.setup()
        print(apps.is_installed({package!r}))
        """,
        [package, _COURIERS, "neolith"],
    )
    assert printed == "True\n"


def test_the_settings_fixture_and_modify_settings_take_the_same_path(
    make_app: MakeApp, settings: object
) -> None:
    package = make_app()
    with pytest.raises(DeclarationError, match="'neolith' is not installed"):
        setattr(settings, "INSTALLED_APPS", [package])  # noqa: B010
    # This process sets up the test host only (tests.host_apps).
    setattr(settings, "INSTALLED_APPS", host_apps.INSTALLED)  # noqa: B010
    assert global_apps.is_installed("tests.host_apps.stock")
    setattr(settings, "INSTALLED_APPS", [])  # noqa: B010

    with pytest.raises(DeclarationError, match="'neolith' is not installed"):
        modify_settings(
            INSTALLED_APPS={"append": package, "remove": "neolith"}
        ).enable()
    with modify_settings(INSTALLED_APPS={"append": host_apps.INSTALLED}):
        assert global_apps.is_installed("tests.host_apps.stock")


def test_isolate_apps_takes_a_declared_app_without_neolith(make_app: MakeApp) -> None:
    package = make_app()
    with isolate_apps(package) as isolated:
        assert isolated is not None
        assert not isolated.is_installed("neolith")
        config = isolated.get_app_config(package)
        assert type(config) is _config_class(package)

        class Note(models.Model):  # noqa: DJ008
            class Meta:
                app_label = package

        assert isolated.get_model(package, "Note") is Note
    assert "note" not in global_apps.all_models[package]
    assert not global_apps.is_installed(package)

    with isolate_apps(package, "neolith") as isolated:
        assert isolated is not None
        assert isolated.is_installed("neolith")
    with isolate_apps(f"{package}.apps.StockConfig") as isolated:
        assert isolated is not None
        assert isolated.get_app_config(package).name == package


def test_the_models_module_of_a_declared_app_is_imported(make_app: MakeApp) -> None:
    package = make_app(
        models="from django.db import models\n\n\nclass Note(models.Model):\n    pass\n"
    )
    printed = in_fresh_process(
        f"""
        import sys

        import django
        from django.apps import apps

        django.setup()
        config = apps.get_app_config({package!r})
        assert config.models_module is sys.modules[{package!r} + ".models"]
        print(config.get_model("Note")._meta.app_label)
        """,
        [package, _COURIERS, "neolith"],
    )
    assert printed == f"{package}\n"


@pytest.mark.spec("APP-1")
@pytest.mark.parametrize(
    ("attribute", "value", "shown"),
    [
        ("label", "relabelled", "'relabelled'"),
        ("label", _Text("{package}"), "a tests.test_declaration._Text"),
        ("name", "json", "'json'"),
    ],
    ids=["label", "label of another type", "name"],
)
@pytest.mark.parametrize("entry", ["{package}", "{package}.apps.StockConfig"])
def test_a_name_or_label_rebound_after_the_class_is_defined_is_refused(
    make_app: MakeApp, attribute: str, value: str, shown: str, entry: str
) -> None:
    package = make_app()
    entry = entry.replace("{package}", package)
    config = _config_class(package)
    declared = getattr(config, attribute)
    setattr(config, attribute, type(value)(value.replace("{package}", package)))
    expected = (
        f"app {package}: {attribute}: the app config has {shown}, and the "
        f"declaration was checked with {declared!r}; {attribute} is not changed "
        "after the class is defined"
    )
    before = _installed()

    # In any registry: an isolated one, with and without neolith, and Django's.
    for attempt in (
        lambda: Apps([entry]),
        lambda: isolate_apps(entry, "neolith").enable(),
        lambda: override_settings(INSTALLED_APPS=[entry, "neolith"]).enable(),
    ):
        with pytest.raises(DeclarationError) as raised:
            attempt()
        assert str(raised.value) == expected
    assert _installed() == before
    assert _snapshot(config).label == package

    # Written back, the same class loads.
    setattr(config, attribute, declared)
    assert Apps([entry]).get_app_config(package).name == package


@pytest.mark.spec("APP-1")
def test_an_instance_built_with_another_name_or_label_is_refused(
    make_app: MakeApp,
) -> None:
    package = make_app()
    config = _config_class(package)

    renamed = config("json", json)
    with pytest.raises(DeclarationError, match=f"app {package}: name: .* 'json'"):
        Apps([renamed])

    relabelled = config(package, sys.modules[package])
    relabelled.label = "other"
    with pytest.raises(DeclarationError) as raised:
        Apps([relabelled])
    assert f"app {package}: label: the app config has 'other'" in str(raised.value)

    both = config("json", json)
    both.label = "other"
    with pytest.raises(DeclarationError) as raised:
        Apps([both])
    assert [line.split(": ")[1] for line in str(raised.value).split("\n")] == [
        "label",
        "name",
    ]

    # In the global registry, a missing neolith is reported beside them.
    with (
        pytest.raises(DeclarationError) as raised,
        override_settings(INSTALLED_APPS=[both]),
    ):
        pytest.fail("the override was admitted")
    assert [line.split(": ")[1] for line in str(raised.value).split("\n")] == [
        "INSTALLED_APPS",
        "label",
        "name",
    ]

    assert Apps([config(package, sys.modules[package])]).is_installed(package)


@pytest.mark.spec("APP-1")
def test_a_config_whose_check_never_ran_cannot_be_an_installed_app() -> None:
    base = _declare({"verbose_name": "Shop"}, name="Base")

    class Skips:
        def __init_subclass__(cls, **kwargs: object) -> None:
            pass

    unchecked = _declare(_valid(provides=["anything"]), Skips, name="Unchecked")
    assert declaration_of(unchecked) is None
    assert unchecked.default is False

    for config in (base, unchecked, NeolithAppConfig):
        with pytest.raises(DeclarationError) as raised:
            Apps([config("json", json)])
        assert str(raised.value).startswith(
            f"app {config.__module__}.{config.__qualname__}: it holds no checked "
            "declaration"
        )
    # Django itself refuses a base class named as an entry: it has no name.
    with pytest.raises(ImproperlyConfigured, match="must supply a name attribute"):
        AppConfig.create("neolith.NeolithAppConfig")
