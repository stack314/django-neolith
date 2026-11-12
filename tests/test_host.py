"""The host's set-up: Resolve, Validate and connect() (spec 05, ADR 0021, 0022).

Resolve and Validate are checked on built registries, with ``build``, so a
test needs no host of its own. Set-up in Django's global registry runs on
the test host (``tests.host_apps``) in this process, since a process hosts
one set of apps, and on any other set in a fresh process.
"""

import copy
import importlib
import itertools
import pickle
import re
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, cast

import pytest
from django.apps import AppConfig
from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.test.utils import isolate_apps, override_settings
from hypothesis import given
from hypothesis import strategies as st

import neolith
from neolith import (
    Contract,
    ContractError,
    UsageError,
    _context,
    action,
    connect,
    contract,
)
from neolith._context import Scope
from neolith._contracts import _IDENTITY
from neolith._declaration import (
    Declaration,
    NeolithAppConfig,
    Provision,
    declaration_of,
)
from neolith._host import _RECORDED, HostConfig, _current, build, resolve
from tests import declaration_contracts, host_apps
from tests.declaration_contracts import (
    Couriers,
    Invoices,
    Level,
    Sku,
    Stock,
    StockTwin,
    StockV2,
)
from tests.host_apps.couriers import services as courier_services
from tests.host_apps.web.apps import WebConfig
from tests.hosting import in_fresh_process
from tests.test_contract_round_trip import _HYPOTHESIS

_COUNTER = itertools.count()

_STOCK = """
from tests.declaration_contracts import Level, Sku


class StockService:
    def level(self, request: Sku) -> Level:
        return Level(on_hand=1)
"""

_INVOICES = """
from tests.declaration_contracts import Sku


class InvoiceService:
    def void(self, request: Sku) -> None:
        return None
"""

_COURIERS = """
from tests.declaration_contracts import Level, Sku


class CourierService:
    def quote(self, request: Sku) -> Level:
        return Level(on_hand=2)
"""


def _apps_module(
    *, provides: Sequence[tuple[str, str]] = (), requires: Sequence[str] = ()
) -> str:
    """An ``apps`` module: ``provides`` pairs a contract class's name in
    ``tests.declaration_contracts`` with its implementation path, in which
    ``{package}`` stands for the app's package."""
    lines = [
        "from neolith import NeolithAppConfig, Provided",
        "from tests import declaration_contracts as c",
        "",
        "",
        "class Config(NeolithAppConfig):",
        '    name = "{package}"',
        '    label = "{package}"',
    ]
    if provides:
        lines.append("    provides = (")
        lines += [
            f'        Provided(c.{name}, implementation="{path}", principal_from=()),'
            for name, path in provides
        ]
        lines.append("    )")
    if requires:
        lines.append(f"    requires = ({''.join(f'c.{name}, ' for name in requires)})")
    return "\n".join(lines) + "\n"


WriteApp = Callable[..., str]


@pytest.fixture
def write_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WriteApp]:
    """Write an app package from module sources; return its name.

    ``{package}`` in a source stands for the package's name.
    """
    made: list[str] = []

    def write(apps: str, **modules: str) -> str:
        package = f"hosted_{next(_COUNTER)}"
        root = tmp_path / package
        root.mkdir()
        for module, source in {"__init__": "", "apps": apps, **modules}.items():
            text = textwrap.dedent(source).replace("{package}", package)
            (root / f"{module}.py").write_text(text, encoding="utf-8")
        made.append(package)
        importlib.invalidate_caches()
        return package

    monkeypatch.syspath_prepend(str(tmp_path))
    yield write
    for loaded in list(sys.modules):
        if loaded.split(".")[0] in made:
            del sys.modules[loaded]


def _stock_app(write_app: WriteApp, *, requires: Sequence[str] = ()) -> str:
    return write_app(
        _apps_module(
            provides=[("Stock", "{package}.services.StockService")],
            requires=requires,
        ),
        services=_STOCK,
    )


def _invoices_app(write_app: WriteApp, service: str = _INVOICES) -> str:
    return write_app(
        _apps_module(provides=[("Invoices", "{package}.services.InvoiceService")]),
        services=service,
    )


def _refusals(*entries: str) -> list[str]:
    """Resolve and Validate the apps in a registry of their own."""
    _, refusals = build(Apps(list(entries)))
    return sorted(refusals)


# Resolve


@pytest.mark.spec("APP-8")
def test_a_host_whose_requirements_are_provided_builds_its_table(
    write_app: WriteApp,
) -> None:
    stock = _stock_app(write_app, requires=["Invoices", "Stock"])
    invoices = _invoices_app(write_app)
    host, refusals = build(Apps([stock, invoices]))
    assert refusals == []
    assert host.known == {"stock.v1", "billing.invoices.v1"}
    assert set(host.connectors) == {"stock.v1", "billing.invoices.v1"}


@pytest.mark.spec("APP-1", "APP-8")
def test_a_requirement_without_a_provider_is_refused(write_app: WriteApp) -> None:
    stock = _stock_app(write_app, requires=["Invoices", "Couriers"])
    assert _refusals(stock) == [
        f"app {stock}: requires: billing.invoices.v1 has no provider; no app of "
        "this host provides it, so install the app that does",
        f"app {stock}: requires: couriers.v3 has no provider; no app of this "
        "host provides it, so install the app that does",
    ]
    # Following the advice: installing the providers admits the host.
    couriers = write_app(
        _apps_module(provides=[("Couriers", "{package}.services.CourierService")]),
        services=_COURIERS,
    )
    assert _refusals(stock, _invoices_app(write_app), couriers) == []


@pytest.mark.spec("APP-9")
def test_two_apps_that_provide_one_contract_are_refused(write_app: WriteApp) -> None:
    first, second, third = (_stock_app(write_app) for _ in range(3))
    assert _refusals(first, second, third) == [
        f"app {second}: provides: stock.v1 is provided by app {first} too; in a "
        "host, one app provides a contract",
        f"app {third}: provides: stock.v1 is provided by app {first} too; in a "
        "host, one app provides a contract",
    ]
    # One provider, required by the others, is admitted.
    other = write_app(_apps_module(requires=["Stock"]))
    assert _refusals(first, other) == []


@pytest.mark.spec("APP-10")
@pytest.mark.parametrize("field", ["provides", "requires"])
def test_one_identity_with_two_contract_classes_in_a_host_is_refused(
    write_app: WriteApp, field: str
) -> None:
    stock = _stock_app(write_app)
    twin = write_app(
        _apps_module(provides=[("StockTwin", "{package}.services.StockService")])
        if field == "provides"
        else _apps_module(requires=["StockTwin"]),
        services=_STOCK,
    )
    twin_line = (
        f"app {twin}: {field}: stock.v1 is tests.declaration_contracts.StockTwin "
        "here and tests.declaration_contracts.Stock in app "
        f"{stock}; in a host, one identity has one contract class"
    )
    refusals = _refusals(stock, twin)
    assert twin_line in refusals
    # Another version of the same contract is another identity.
    v2 = write_app(_apps_module(requires=["StockV2"]))
    assert _refusals(stock, v2) == [
        f"app {v2}: requires: stock.v2 has no provider; no app of this host "
        "provides it, so install the app that does"
    ]


_POOL: tuple[type, ...] = (Stock, StockTwin, StockV2, Invoices, Couriers)


@st.composite
def _hosts(draw: st.DrawFn) -> list[Declaration]:
    """Declarations as PR 1 admits them: within one app, each identity names
    one class and is listed once per field."""
    declarations = []
    for index in range(draw(st.integers(1, 4))):
        chosen: dict[str, type] = {}
        for cls in draw(st.lists(st.sampled_from(_POOL), max_size=5)):
            chosen.setdefault(Contract.of(cls).identity, cls)
        identities = sorted(chosen)
        provides = (
            draw(st.lists(st.sampled_from(identities), unique=True))
            if identities
            else []
        )
        requires = (
            draw(st.lists(st.sampled_from(identities), unique=True))
            if identities
            else []
        )
        config = cast("type[NeolithAppConfig]", type(f"Config{index}", (), {}))
        declarations.append(
            Declaration(
                config=config,
                name=f"app{index}",
                label=f"app{index}",
                provides=tuple(
                    Provision(Contract.of(chosen[i]), "a.B", ()) for i in provides
                ),
                requires=tuple(Contract.of(chosen[i]) for i in requires),
                resources=(),
                batteries=(),
            )
        )
    return declarations


_LINE = re.compile(r"app (\w+): (provides|requires): ([\w.]+) (?:is|has) ")


def _kind(line: str) -> str:
    for phrase, kind in (
        ("one identity has one contract class", "class"),
        ("one app provides a contract", "provider"),
        ("has no provider", "unmet"),
    ):
        if phrase in line:
            return kind
    raise AssertionError(line)


@pytest.mark.spec("APP-8", "APP-9", "APP-10")
@_HYPOTHESIS
@given(_hosts())
def test_resolve_refuses_exactly_what_the_rules_name(
    declarations: list[Declaration],
) -> None:
    expected = set()
    first_class: dict[str, tuple[type, str]] = {}
    provider: dict[str, str] = {}
    for declaration in declarations:
        app = declaration.label
        for field, named in [("provides", p.contract) for p in declaration.provides] + [
            ("requires", c) for c in declaration.requires
        ]:
            seen = first_class.setdefault(named.identity, (named.declaration, app))
            if seen[0] is not named.declaration:
                expected.add((app, field, named.identity, "class"))
        for provision in declaration.provides:
            if provider.setdefault(provision.contract.identity, app) != app:
                expected.add((app, "provides", provision.contract.identity, "provider"))
    for declaration in declarations:
        for named in declaration.requires:
            if named.identity not in provider:
                expected.add((declaration.label, "requires", named.identity, "unmet"))

    contracts, refusals = resolve(declarations)
    found = set()
    for line in refusals:
        match = _LINE.match(line)
        assert match is not None, line
        found.add((*match.groups(), _kind(line)))
    assert found == expected
    assert len(refusals) == len(found)
    assert set(contracts) == {
        c.identity
        for d in declarations
        for c in [*(p.contract for p in d.provides), *d.requires]
    }


# Validate


_ACTION_MISSING = """
class StockService:
    pass
"""

_WRONG_TYPES = """
from tests.declaration_contracts import Level, Sku


class StockService:
    def level(self, request: Level) -> Sku:
        return Sku(sku="")
"""


# A class that must never be built: set-up refuses it unbuilt.
_DANGER = """
class StockService:
    def __init__(self) -> None:
        raise OSError("built")
"""


def _validate_case(write_app: WriteApp, case: str) -> tuple[str, str]:
    """An app with one Validate refusal; return the app and the line's tail."""
    impl = "{package}.services.StockService"
    services = _STOCK
    if case == "module missing":
        impl = "{package}.absent.StockService"
        tail = (
            "does not import: ModuleNotFoundError: No module named '{package}.absent'"
        )
    elif case == "module replaced":
        services = (
            "import sys\n\n\nclass _Slots:\n    __slots__ = ()\n\n\n"
            "sys.modules[__name__] = _Slots()\n"
        )
        tail = (
            " does not import: {package}.services is not a module whose names can "
            "be read (TypeError: "
        )
    elif case == "module raises":
        services = "raise RuntimeError('broken at import')\n"
        tail = "does not import: RuntimeError: broken at import"
    elif case == "attribute missing":
        impl = "{package}.services.Absent"
        tail = "does not import: {package}.services defines no Absent"
    elif case == "not a class":
        services = "StockService = 3\n"
        tail = "is an int, not a class"
    elif case == "re-exported":
        impl = "{package}.reexport.StockService"
        tail = (
            "names {package}.services.StockService by another path; a class is "
            'named by its own path, implementation="{package}.services.StockService"'
        )
    elif case == "nested":
        services = (
            "class Outer:\n    class StockService:\n        pass\n\n\n"
            "StockService = Outer.StockService\n"
        )
        tail = (
            ": the class is {package}.services.Outer.StockService, which is not "
            "defined at the top level of its module; define it at the top level "
            "of a module of app {package}"
        )
    elif case == "outside the package":
        impl = "tests.declaration_contracts.Level"
        tail = (
            " is outside the app's package, {package}; an implementation belongs "
            'to the app that provides it, so its path starts with "{package}."'
        )
    elif case == "class of no app":
        # Reached through the app's package, defined in a package that is no
        # app, and never built.
        impl = "{package}.reexport.StockService"
        tail = (
            ": the class is in {other}.services, which belongs to no app; an "
            "implementation belongs to the app that provides it, {package}"
        )
    elif case == "module unreadable":
        services = _DANGER + "\n\nStockService.__module__ = None\n"
        tail = ": the class's module cannot be read"
    elif case == "in the app's package":
        impl = "{package}.StockService"
        tail = (
            ": the class is in {package}, which is read with the declaration; put "
            "the implementation in another module of the app, such as "
            "{package}.services"
        )
    elif case == "in the config's module":
        impl = "{package}.apps.StockService"
        tail = (
            ": the class is in {package}.apps, which is read with the declaration; "
            "put the implementation in another module of the app, such as "
            "{package}.services"
        )
    elif case == "action missing":
        services = _ACTION_MISSING
        tail = ": stock.v1, implementation StockService: action 'level' is missing"
    elif case == "construction raises":
        services = (
            _STOCK
            + "\n    def __init__(self) -> None:\n        raise OSError('no disk')\n"
        )
        tail = ": building it with no arguments raised OSError: no disk"
    else:
        raise AssertionError(case)
    apps = _apps_module(provides=[("Stock", impl)])
    extra: dict[str, str] = {}
    if case == "re-exported":
        extra["reexport"] = "from {package}.services import StockService\n"
    if case == "class of no app":
        other = write_app("", services=_DANGER)
        extra["reexport"] = f"from {other}.services import StockService\n"
        tail = tail.replace("{other}", other)
    if case == "in the app's package":
        extra["__init__"] = _STOCK
    if case == "in the config's module":
        apps += _STOCK
    package = write_app(apps, services=services, **extra)
    return package, tail.replace("{package}", package)


_VALIDATE_CASES = [
    "module missing",
    "module raises",
    "module replaced",
    "attribute missing",
    "not a class",
    "re-exported",
    "nested",
    "outside the package",
    "class of no app",
    "module unreadable",
    "in the app's package",
    "in the config's module",
    "action missing",
    "construction raises",
]


@pytest.mark.spec("APP-1", "APP-11")
@pytest.mark.parametrize("case", _VALIDATE_CASES)
def test_an_implementation_outside_the_admitted_form_is_refused(
    write_app: WriteApp, case: str
) -> None:
    package, tail = _validate_case(write_app, case)
    refusals = _refusals(package)
    head = f"app {package}: provides: stock.v1"
    # The tail ends with the cause where only its class is fixed: the message
    # of a builtin error can change between Python versions.
    assert any(line.startswith(head) and tail in line for line in refusals), refusals
    # A class outside the app is refused unbuilt.
    assert not any("raised OSError: built" in line for line in refusals)


@pytest.mark.spec("APP-11", "APP-12")
def test_an_implementation_is_checked_in_full_whatever_it_fails_first(
    write_app: WriteApp,
) -> None:
    # Named through a re-export, missing one action, taking the wrong types
    # in another, and failing to build: every one is listed.
    package = write_app(
        _apps_module(provides=[("Couriers", "{package}.reexport.CourierService")]),
        services="""
from tests.declaration_contracts import Sku


class CourierService:
    def quote(self, request: Sku) -> Sku:
        return request

    def __init__(self) -> None:
        raise OSError("no disk")
""",
        reexport="from {package}.services import CourierService\n",
    )
    where = f"app {package}: provides: couriers.v3"
    assert _refusals(package) == sorted(
        [
            f"app {package}: provides: couriers.v3, implementation CourierService: "
            "action 'quote' "
            "returns Sku, but couriers.v3 declares Level",
            f"{where}: implementation '{package}.reexport.CourierService' names "
            f"{package}.services.CourierService by another path; a class is named "
            f'by its own path, implementation="{package}.services.CourierService"',
            f"{where}: implementation '{package}.reexport.CourierService': building "
            "it with no arguments raised OSError: no disk",
        ]
    )


def test_set_up_lists_every_contract_problem_and_implements_the_first() -> None:
    class Twice:
        def other(self) -> None:
            pass

    contract = Contract.of(declaration_contracts.Stock)
    problems = contract._implementation_problems(Twice)
    assert problems == [
        "stock.v1, implementation test_set_up_lists_every_contract_problem_"
        "and_implements_the_first.<locals>.Twice: action 'level' is missing",
        "stock.v1, implementation test_set_up_lists_every_contract_problem_"
        "and_implements_the_first.<locals>.Twice: public method 'other' is "
        "not an action of stock.v1; make it private (prefix it with '_') or move "
        "it out of the class",
    ]
    with pytest.raises(neolith.ContractDefinitionError) as raised:
        neolith.implements(declaration_contracts.Stock)(Twice)
    assert str(raised.value) == problems[0]


@pytest.mark.spec("APP-11")
def test_a_fixed_implementation_is_admitted_for_each_refusal(
    write_app: WriteApp,
) -> None:
    # The advice of each message leads to this form: a top-level class in a
    # module of the app, named by its own path, which builds.
    assert _refusals(_stock_app(write_app)) == []


@pytest.mark.spec("APP-11", "APP-13")
def test_a_call_from_an_implementation_init_is_refused_as_not_set_up(
    write_app: WriteApp,
) -> None:
    package = _invoices_app(
        write_app,
        _INVOICES
        + "\n    def __init__(self) -> None:\n"
        + "        from neolith import connect\n"
        + "        from tests.declaration_contracts import Sku\n"
        + '        connect("couriers.v3").quote(Sku(sku="x"))\n',
    )
    assert _refusals(package) == [
        f"app {package}: provides: billing.invoices.v1: implementation "
        f"'{package}.services.InvoiceService': building it with no arguments "
        "raised UsageError: connector to couriers.v3: the host is not set up "
        "yet. Calls begin once Django ha..."
    ]


# Every refusal of Resolve and Validate in one diagnostic.


@pytest.mark.spec("APP-12")
@pytest.mark.parametrize("case", ["unmet", "provider", "class", *_VALIDATE_CASES])
def test_every_refusal_reaches_the_diagnostic(write_app: WriteApp, case: str) -> None:
    # Each refusal beside one of another kind, in apps listed both ways round.
    if case == "unmet":
        apps = [_stock_app(write_app, requires=["Couriers"])]
        line = "requires: couriers.v3 has no provider"
    elif case == "provider":
        apps = [_stock_app(write_app), _stock_app(write_app)]
        line = "provides: stock.v1 is provided by app"
    elif case == "class":
        apps = [_stock_app(write_app), write_app(_apps_module(requires=["StockTwin"]))]
        line = "requires: stock.v1 is tests.declaration_contracts.StockTwin here"
    else:
        package, line = _validate_case(write_app, case)
        apps = [package]
    behind = _invoices_app(write_app, "class InvoiceService:\n    pass\n")
    behind_line = "action 'void' is missing"
    for entries in ([*apps, behind], [behind, *apps]):
        refusals = _refusals(*entries)
        assert any(line in found for found in refusals), (case, refusals)
        assert any(behind_line in found for found in refusals), (case, refusals)


# Set-up in Django's global registry, on the test host.


def _web() -> AbstractContextManager[Scope]:
    """A root for the web app, which requires what its declaration says."""
    declaration = declaration_of(WebConfig)
    assert declaration is not None
    return _context.root(
        "web", requires=tuple(c.identity for c in declaration.requires)
    )


def test_neolith_is_an_installed_app_whose_config_django_picks() -> None:
    assert type(AppConfig.create("neolith")) is HostConfig
    assert HostConfig.name == HostConfig.label == "neolith"


@pytest.mark.spec("APP-8", "APP-11")
def test_after_set_up_a_call_reaches_the_provider_as_its_app() -> None:
    stock = connect("stock.v1")
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        with _web():
            reply = stock.level(Sku(sku="abcd"))
        assert reply == Level(on_hand=4)
        # The stock app's own call to couriers is made as the stock app.
        assert courier_services.CALLS[-1][0] == "stock"


@pytest.mark.spec("APP-13", "CON-29")
def test_a_call_before_set_up_is_a_usage_error_and_not_sent() -> None:
    before = len(courier_services.CALLS)
    couriers = connect("couriers.v3")
    with pytest.raises(UsageError) as raised:
        couriers.quote  # noqa: B018
    assert str(raised.value).startswith(
        "connector to couriers.v3: the host is not set up yet."
    )
    assert isinstance(raised.value, RuntimeError)
    assert ContractError not in type(raised.value).__mro__
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        quote = couriers.quote
    # An action read during set-up is refused once the host is gone again.
    with _web(), pytest.raises(UsageError, match="not set up"):
        quote(Sku(sku="x"))
    assert len(courier_services.CALLS) == before


def test_set_up_twice_with_the_same_apps_builds_a_fresh_table() -> None:
    couriers = connect("couriers.v3")
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        first = _current()
        first_instance = courier_services.BUILT[-1]
        with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
            second = _current()
            assert first is not None
            assert second is not None
            assert second is not first
            assert (
                second.connectors["couriers.v3"] is not first.connectors["couriers.v3"]
            )
            second_instance = courier_services.BUILT[-1]
            assert second_instance is not first_instance
            with _web():
                couriers.quote(Sku(sku="a"))
                couriers.quote(Sku(sku="b"))
            assert [served for _, served in courier_services.CALLS[-2:]] == [
                second_instance,
                second_instance,
            ]
        # Leaving the override restores the registry, and its set-up.
        assert _current() is first
        with _web():
            couriers.quote(Sku(sku="c"))
        assert courier_services.CALLS[-1] == ("web", first_instance)
    assert _current() is None


def test_an_override_that_leaves_neolith_out_holds_no_host_until_it_ends() -> None:
    couriers = connect("couriers.v3")
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        host = _current()
        with override_settings(INSTALLED_APPS=["django.contrib.humanize"]):
            assert _current() is None
            with (
                _web(),
                pytest.raises(UsageError, match="calls resume when that ends"),
            ):
                couriers.quote(Sku(sku="x"))
        assert _current() is host


def test_an_undeclared_app_may_come_and_go_in_a_later_set_up() -> None:
    with (
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
        override_settings(
            INSTALLED_APPS=["django.contrib.humanize", *host_apps.INSTALLED]
        ),
    ):
        assert _current() is not None


def test_isolate_apps_with_the_hosted_apps_and_neolith_sets_nothing_up() -> None:
    with isolate_apps(*host_apps.INSTALLED) as isolated:
        assert isolated is not None
        assert "_neolith_host" not in vars(isolated.get_app_config("neolith"))
        assert _current() is None
    # Nor inside a host: the global registry's set-up is untouched.
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        host = _current()
        with isolate_apps(*host_apps.INSTALLED):
            assert _current() is host
    # Any registry but Django's global one is not a host.
    assert "_neolith_host" not in vars(
        Apps(host_apps.INSTALLED).get_app_config("neolith")
    )


def test_concurrent_first_calls_after_set_up_each_get_their_reply() -> None:
    stock = connect("stock.v1")
    threads = 8
    barrier = threading.Barrier(threads)
    replies: dict[int, object] = {}

    def call(index: int) -> None:
        barrier.wait()
        with _web():
            replies[index] = stock.level(Sku(sku="x" * index))

    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        workers = [threading.Thread(target=call, args=(i,)) for i in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    assert replies == {i: Level(on_hand=i) for i in range(threads)}


# connect()


@pytest.mark.spec("APP-15", "CON-29")
def test_connect_after_set_up_refuses_an_identity_the_host_does_not_know() -> None:
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):

        @_HYPOTHESIS
        @given(st.from_regex(_IDENTITY, fullmatch=True))
        def check(identity: str) -> None:
            if identity in {"stock.v1", "couriers.v3"}:
                assert repr(connect(identity)) == f"<connector to {identity}>"
                return
            with pytest.raises(UsageError) as raised:
                connect(identity)
            assert str(raised.value) == (
                f"connect({identity!r}): no app of this host provides or requires "
                f"{identity}; it knows the contracts its apps provide or require, "
                "which are couriers.v3, stock.v1"
            )
            assert ContractError not in type(raised.value).__mro__

        check()


class _Text(str):
    __slots__ = ()


def test_connect_refuses_a_value_that_is_not_an_identity_at_any_time() -> None:
    @_HYPOTHESIS
    @given(st.text(max_size=40).filter(lambda text: not _IDENTITY.fullmatch(text)))
    def check(text: str) -> None:
        with pytest.raises(ValueError, match="is not a contract identity"):
            connect(text)

    check()
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        check()
    # The whole text is the identity: nothing before or after it.
    for text in ("stock.v1junk", "stock.v1\n", " stock.v1", "stock.v01", "stock"):
        with pytest.raises(ValueError, match="is not a contract identity"):
            connect(text)
    for value in (b"stock.v1", _Text("stock.v1"), None, Stock):
        with pytest.raises(TypeError, match="takes a contract identity as a str"):
            connect(value)  # type: ignore[arg-type]


def test_a_connector_holds_its_identity_and_nothing_else() -> None:
    stock = connect("stock.v1")
    assert type(stock).__slots__ == ("_identity",)
    assert copy.copy(stock) is stock
    assert copy.deepcopy(stock) is stock
    with pytest.raises(TypeError, match=r"call connect\('stock.v1'\) where"):
        pickle.dumps(stock)
    with pytest.raises(AttributeError, match="cannot be changed"):
        stock.level = None
    with pytest.raises(AttributeError):
        stock._contract  # noqa: B018
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        # A mistake in the calling code keeps step 3's builtin classes.
        with pytest.raises(AttributeError, match="has no action 'restock'"):
            stock.restock  # noqa: B018
        with _web(), pytest.raises(TypeError, match="takes one request"):
            stock.level()
        assert stock.level.__name__ == "level"


@pytest.mark.spec("APP-14", "APP-15")
def test_a_connector_is_a_neolith_connector_made_only_by_connect() -> None:
    stock = connect("stock.v1")
    assert type(stock) is neolith.Connector
    assert isinstance(stock, neolith.Connector)
    # Its class cannot build one: that would skip connect()'s checks and the
    # identity set-up records (APP-14, APP-15).
    for args in ((), ("stock.v1",), ("nothing.v9",)):
        with pytest.raises(TypeError, match=r"made by neolith\.connect\(\)"):
            neolith.Connector(*args)
    with pytest.raises(TypeError, match=r"made by neolith\.connect\(\)"):
        neolith.Connector(identity="stock.v1")
    with pytest.raises(TypeError):

        class Wider(neolith.Connector):  # type: ignore[misc]
            pass


# Set-up in a fresh process: other sets of apps, and set-up's own failures.


_SET_UP_FAILS = """
import logging

import django

logged = []


class Keep(logging.Handler):
    def emit(self, record):
        logged.append((record.name, record.levelname, record.getMessage()))


logging.getLogger("neolith").addHandler(Keep())
try:
    django.setup()
except Exception as error:
    print(type(error).__name__)
    print(error)
    print("logged:", [entry for entry in logged if "cannot be set up" in entry[2]][:1])
    try:
        django.setup()
    except RuntimeError as again:
        print("again:", again)
"""


@pytest.mark.spec("APP-1", "APP-8")
def test_a_host_with_an_unsatisfied_requirement_fails_to_start(
    write_app: WriteApp,
) -> None:
    stock = _stock_app(write_app, requires=["Invoices"])
    printed = in_fresh_process(_SET_UP_FAILS, [stock, "neolith"])
    line = (
        f"app {stock}: requires: billing.invoices.v1 has no provider; no app of "
        "this host provides it, so install the app that does"
    )
    assert printed == (
        f"DeclarationError\n{line}\n"
        f"logged: [('neolith.host', 'ERROR', 'the host cannot be set up:\\n{line}')]\n"
        "again: populate() isn't reentrant\n"
    )


@pytest.mark.spec("APP-1", "APP-11")
def test_a_host_whose_implementation_misses_an_action_fails_to_start(
    write_app: WriteApp,
) -> None:
    package = write_app(
        _apps_module(provides=[("Stock", "{package}.services.StockService")]),
        services=_ACTION_MISSING,
    )
    printed = in_fresh_process(_SET_UP_FAILS, [package, "neolith"])
    assert printed.splitlines()[:2] == [
        "DeclarationError",
        f"app {package}: provides: stock.v1, implementation StockService: "
        "action 'level' is missing",
    ]


@pytest.mark.spec("APP-1", "APP-12", "APP-14")
def test_connect_before_set_up_to_an_unknown_identity_stops_the_host(
    write_app: WriteApp, tmp_path: Path
) -> None:
    # At import of a models module and of an implementation module, in an
    # app's ready(), and in an implementation's __init__: each place is named,
    # beside the other refusals of the host.
    package = write_app(
        _apps_module(provides=[("Invoices", "{package}.services.InvoiceService")])
        + "\n    def ready(self):\n        from neolith import connect\n"
        '        connect("nope.v3")\n',
        models='from neolith import connect\n\nconnect("nope.v1")\n',
        services='from neolith import connect\n\nconnect("nope.v2")\n'
        + _INVOICES
        + '\n    def __init__(self):\n        connect("nope.v4")\n'
        "        connect('stock.v1')\n",
    )
    unmet = _stock_app(write_app, requires=["Couriers"])
    printed = in_fresh_process(_SET_UP_FAILS, [package, unmet, "neolith"])
    root = tmp_path / package
    knows = (
        "it knows the contracts its apps provide or require, which are "
        "billing.invoices.v1, couriers.v3, stock.v1"
    )
    lines = printed.splitlines()
    assert lines[0] == "DeclarationError"
    assert lines[1:6] == sorted(
        [
            f"connect: 'nope.v1' at {root / 'models.py'}:3 is not a contract this "
            f"host knows; {knows}",
            f"connect: 'nope.v2' at {root / 'services.py'}:3 is not a contract this "
            f"host knows; {knows}",
            f"connect: 'nope.v3' at {root / 'apps.py'}:14 is not a contract this "
            f"host knows; {knows}",
            f"connect: 'nope.v4' at {root / 'services.py'}:13 is not a contract "
            f"this host knows; {knows}",
            f"app {unmet}: requires: couriers.v3 has no provider; no app of this "
            "host provides it, so install the app that does",
        ]
    )


@pytest.mark.spec("APP-14")
def test_connect_racing_set_up_is_either_recorded_and_checked_or_refused() -> None:
    printed = in_fresh_process(
        """
        import threading

        import django

        from neolith import UsageError, connect

        start = threading.Barrier(9)
        outcomes = []

        def ask():
            start.wait()
            for _ in range(200):
                try:
                    connect("nope.v1")
                except UsageError:
                    outcomes.append("refused")
                    return
            outcomes.append("recorded")

        workers = [threading.Thread(target=ask) for _ in range(8)]
        for worker in workers:
            worker.start()
        start.wait()
        try:
            django.setup()
            passed = True
        except Exception:
            passed = False
        for worker in workers:
            worker.join()
        # A connect() recorded before the table existed is checked; one made
        # after is refused at once. None is admitted unseen.
        print(passed, "recorded" in outcomes if passed else True)
        """,
        host_apps.INSTALLED,
    )
    assert printed == "False True\n" or printed == "True False\n"


@pytest.mark.spec("APP-1")
def test_a_later_set_up_with_other_hosted_apps_is_refused(
    write_app: WriteApp, tmp_path: Path
) -> None:
    invoices = _invoices_app(write_app)
    other = tmp_path / "other_config.py"
    other.write_text(
        _apps_module(
            provides=[("Stock", "tests.host_apps.stock.services.StockService")]
        )
        .replace("{package}", "tests.host_apps.stock")
        .replace('label = "tests.host_apps.stock"', 'label = "stock"')
        .replace("class Config", "class Other")
        + "    requires = (c.Couriers,)\n",
        encoding="utf-8",
    )
    printed = in_fresh_process(
        f"""
        import django
        from django.core.exceptions import ImproperlyConfigured
        from django.test.utils import override_settings

        from neolith import connect
        from neolith import _context
        from tests.declaration_contracts import Sku

        django.setup()
        stock = connect("stock.v1")
        installed = list(settings.INSTALLED_APPS)
        for changed in (
            [{invoices!r}, *installed],
            [name for name in installed if name != "tests.host_apps.web"],
            ["other_config.Other", *installed[1:]],
        ):
            try:
                with override_settings(INSTALLED_APPS=changed):
                    print("admitted", changed)
            except ImproperlyConfigured as error:
                print(type(error).__name__, error)
            with _context.root("web", requires=("stock.v1",)):
                print(stock.level(Sku(sku="ab")).on_hand)
        with override_settings(INSTALLED_APPS=installed):
            print("the same apps are admitted")
        """,
        host_apps.INSTALLED,
    )
    message = "ImproperlyConfigured host: the hosted apps changed after set-up: "
    assert printed.splitlines() == [
        f"{message}{invoices} was added; a process hosts one set of apps",
        "2",
        f"{message}web was removed; a process hosts one set of apps",
        "2",
        f"{message}stock has another config class; a process hosts one set of apps",
        "2",
        "the same apps are admitted",
    ]


def test_a_message_is_bounded_whatever_was_written(write_app: WriteApp) -> None:
    long = "x" * 5000
    package = write_app(
        _apps_module(provides=[("Stock", f"{{package}}.{long}.StockService")])
    )
    (line,) = _refusals(package)
    assert len(line) < 800
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        with pytest.raises(UsageError) as raised:
            connect(f"{long}.v1")
        assert len(str(raised.value)) < 800


# Every problem of the contract check, and nothing that escapes it.


@contract("ledger.v1")
class Ledger(Protocol):
    @action(idempotent=True)
    def first(self, request: Sku) -> Level: ...

    @action
    def second(self, request: Sku) -> None: ...


_KINDS = [
    "missing",
    "not a function",
    "invalid signature",
    "unexpected signature",
    "wrong request",
    "wrong reply",
]


def _member(kind: str, reply: object) -> object:
    """An implementation of a Ledger action with one problem of ``kind``."""
    if kind == "not a function":
        return staticmethod(len)
    wrong = Sku if reply is Level else Level

    def taking_level(self: object, request: Level) -> object: ...

    def replying_wrong(self: object, request: Sku) -> object: ...

    def plain(self: object, request: Sku) -> object: ...

    taking_level.__annotations__["return"] = reply
    replying_wrong.__annotations__["return"] = wrong
    plain.__annotations__["return"] = reply
    if kind == "wrong request":
        return taking_level
    if kind == "wrong reply":
        return replying_wrong
    plain.__signature__ = (  # type: ignore[attr-defined]
        "not a signature" if kind == "invalid signature" else 5
    )
    return plain


@pytest.mark.spec("APP-11", "APP-12")
@pytest.mark.parametrize(("one", "two"), list(itertools.product(_KINDS, _KINDS)))
def test_a_problem_with_one_action_hides_none_of_the_next(one: str, two: str) -> None:
    body = {
        name: _member(kind, reply)
        for name, kind, reply in (("first", one, Level), ("second", two, None))
        if kind != "missing"
    }
    impl = type("Impl", (), body)
    problems = Contract.of(Ledger)._implementation_problems(impl)
    # One line per action, each naming its action, none twice.
    assert len(problems) == 2, problems
    assert "action 'first'" in problems[0]
    assert "action 'second'" in problems[1]


def test_implements_raises_the_first_problem_as_before() -> None:
    # The first action is missing and the second cannot be read: @implements
    # names the first, as it did before set-up listed them all.
    impl = type("Impl", (), {"second": _member("unexpected signature", None)})
    with pytest.raises(neolith.ContractDefinitionError) as raised:
        neolith.implements(Ledger)(impl)
    assert (
        str(raised.value) == "ledger.v1, implementation Impl: action 'first' is missing"
    )
    # An unreadable signature that comes first is a ContractDefinitionError.
    impl = type("Impl", (), {"first": _member("invalid signature", Level)})
    with pytest.raises(
        neolith.ContractDefinitionError, match="cannot read its signature"
    ):
        neolith.implements(Ledger)(impl)


@pytest.mark.spec("APP-12")
def test_an_unreadable_signature_is_listed_beside_another_apps_refusal(
    write_app: WriteApp,
) -> None:
    unreadable = write_app(
        _apps_module(provides=[("Stock", "{package}.services.StockService")]),
        services=_STOCK + '\n\nStockService.level.__signature__ = "not a signature"\n',
    )
    other = _invoices_app(write_app, _INVOICES.replace("request: Sku", "request: int"))
    refusals = _refusals(unreadable, other)
    assert len(refusals) == 2, refusals
    assert refusals[0].startswith(f"app {unreadable}: provides: stock.v1, ")
    # Which error inspect raises for it depends on the Python version.
    assert re.search(
        r"action 'level': cannot read its signature \((ValueError|TypeError): ",
        refusals[0],
    ), refusals[0]
    assert refusals[1].startswith(f"app {other}: provides: billing.invoices.v1, ")


@pytest.mark.spec("APP-10")
def test_a_second_class_is_refused_naming_the_first_app_and_class(
    write_app: WriteApp,
) -> None:
    stock = _stock_app(write_app)
    needs_twin = write_app(_apps_module(requires=["StockTwin"]))
    gives_twin = write_app(
        _apps_module(provides=[("StockTwin", "{package}.services.StockService")]),
        services=_STOCK,
    )
    first = (
        "is tests.declaration_contracts.StockTwin here and "
        f"tests.declaration_contracts.Stock in app {stock}; in a host, one "
        "identity has one contract class"
    )
    assert _refusals(stock, needs_twin, gives_twin) == sorted(
        [
            f"app {gives_twin}: provides: stock.v1 is provided by app {stock} too; in "
            "a host, one app provides a contract",
            f"app {gives_twin}: provides: stock.v1 {first}",
            f"app {needs_twin}: requires: stock.v1 {first}",
        ]
    )


@pytest.mark.spec("APP-15")
def test_connect_in_an_override_without_neolith_checks_the_restored_table() -> None:
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        recorded = dict(_RECORDED)
        for installed in ([], ["django.contrib.humanize"]):
            with override_settings(INSTALLED_APPS=installed):
                with pytest.raises(UsageError, match="no app of this host provides"):
                    connect("nope.v1")
                assert repr(connect("stock.v1")) == "<connector to stock.v1>"
        # Nothing was recorded, so a later set-up of the same apps passes.
        assert dict(_RECORDED) == recorded
        with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
            assert _current() is not None


@pytest.mark.spec("APP-14")
def test_set_up_empties_the_record_it_checked() -> None:
    printed = in_fresh_process(
        """
        import django

        from neolith import _host, connect

        connect("stock.v1")
        print(sorted(_host._RECORDED))
        django.setup()
        print(sorted(_host._RECORDED))
        """,
        host_apps.INSTALLED,
    )
    assert printed == "['stock.v1']\n[]\n"


# Round 2: nothing outside the app imported or built, one table definition,
# records tied to the set-up that checked them.


@pytest.mark.spec("APP-11", "APP-12")
def test_a_sibling_package_or_a_nested_app_is_refused_unimported(
    write_app: WriteApp, tmp_path: Path
) -> None:
    package = write_app(
        _apps_module(provides=[("Stock", "{package}x.services.StockService")])
    )
    # A package whose name starts with the app's, and an app nested in it.
    for root in (tmp_path / f"{package}x",):
        root.mkdir()
        (root / "__init__.py").write_text("", encoding="utf-8")
        (root / "services.py").write_text(_DANGER, encoding="utf-8")
    importlib.invalidate_caches()
    behind = _invoices_app(write_app, "class InvoiceService:\n    pass\n")
    sibling_refusals = _refusals(package, behind)
    assert any(
        f"is outside the app's package, {package};" in line for line in sibling_refusals
    )
    assert any("action 'void' is missing" in line for line in sibling_refusals)
    assert f"{package}x.services" not in sys.modules

    # The nested app: the declaring app is the outer package, its billing
    # package another installed app.
    outer = write_app(
        _apps_module(provides=[("Stock", "{package}.billing.services.StockService")])
    )
    (tmp_path / outer / "billing").mkdir()
    (tmp_path / outer / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / outer / "billing" / "services.py").write_text(_DANGER, encoding="utf-8")
    importlib.invalidate_caches()
    refusals = _refusals(outer, f"{outer}.billing", behind)
    assert (
        f"app {outer}: provides: stock.v1: implementation "
        f"'{outer}.billing.services.StockService': {outer}.billing.services belongs "
        f"to app billing; an implementation belongs to the app that provides it, "
        f"{outer}"
    ) in refusals
    assert any("action 'void' is missing" in line for line in refusals)
    assert f"{outer}.billing.services" not in sys.modules


@pytest.mark.spec("APP-11", "APP-12")
def test_a_class_of_another_installed_app_is_refused_unbuilt(
    write_app: WriteApp,
) -> None:
    other = write_app(_apps_module(), services=_DANGER)
    package = write_app(
        _apps_module(provides=[("Stock", "{package}.reexport.StockService")]),
        reexport=f"from {other}.services import StockService\n",
    )
    behind = _invoices_app(write_app, "class InvoiceService:\n    pass\n")
    refusals = _refusals(package, other, behind)
    assert any(
        f"the class is in {other}.services, which belongs to app {other};" in line
        for line in refusals
    )
    assert any("action 'void' is missing" in line for line in refusals)
    assert not any("raised OSError: built" in line for line in refusals)


class _ReprRaises:
    def __repr__(self) -> str:
        raise RuntimeError("repr refused")


def test_a_message_that_cannot_be_formatted_hides_no_problem() -> None:
    def second(self: object, request: Sku) -> None: ...

    second.__annotations__["request"] = _ReprRaises()
    impl = type("Impl", (), {"second": second})
    problems = Contract.of(Ledger)._implementation_problems(impl)
    assert problems == [
        "ledger.v1, implementation Impl: action 'first' is missing",
        "ledger.v1, implementation Impl: action 'second' takes <_ReprRaises>, but "
        "ledger.v1 declares Sku",
    ]
    # @implements names the first problem, as before set-up listed them all.
    with pytest.raises(neolith.ContractDefinitionError) as raised:
        neolith.implements(Ledger)(impl)
    assert str(raised.value) == problems[0]


def test_calls_and_connect_read_one_table_under_set_available_apps() -> None:
    couriers = connect("couriers.v3")
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        global_apps.set_available_apps(["tests.host_apps.couriers"])
        try:
            with pytest.raises(UsageError, match="no app of this host provides"):
                connect("nope.v1")
            with (
                _web(),
                pytest.raises(UsageError, match="calls resume when that ends"),
            ):
                couriers.quote(Sku(sku="x"))
        finally:
            global_apps.unset_available_apps()
        with _web():
            assert couriers.quote(Sku(sku="ab")) == Level(on_hand=2)


_UNDECLARED_CONNECTS = """
from neolith import connect

connect("nope.v1")
"""


@pytest.mark.spec("APP-12", "APP-14", "APP-15")
@pytest.mark.parametrize("place", ["__init__", "models"])
@pytest.mark.parametrize("before_neolith", [True, False])
def test_connect_during_a_later_set_up_is_refused_in_either_phase(
    write_app: WriteApp, tmp_path: Path, place: str, before_neolith: bool
) -> None:
    # An undeclared app, loaded by a later set-up, asks for an unknown
    # identity in either phase of loading: set-up reports it with its place.
    package = write_app("", **{place: _UNDECLARED_CONNECTS})
    installed = list(host_apps.INSTALLED)
    added = [package, *installed] if before_neolith else [*installed, package]
    printed = in_fresh_process(
        f"""
        import django
        from django.test.utils import override_settings

        django.setup()
        try:
            with override_settings(INSTALLED_APPS={added!r}):
                print("admitted")
        except Exception as error:
            print(type(error).__name__)
            print(error)
        """,
        installed,
    )
    where = tmp_path / package / f"{place}.py"
    knows = (
        "it knows the contracts its apps provide or require, which are "
        "couriers.v3, stock.v1"
    )
    if place == "__init__" and before_neolith:
        # Imported before neolith's app is created in this load: checked at
        # once against the table this set-up would restore (APP-15).
        assert printed.splitlines() == [
            "UsageError",
            f"connect('nope.v1'): no app of this host provides or requires "
            f"nope.v1; {knows}",
        ]
    else:
        # Imported once neolith's app is created: recorded, and this set-up
        # reports it with its place (APP-14).
        assert printed.splitlines() == [
            "DeclarationError",
            f"connect: 'nope.v1' at {where}:4 is not a contract this host knows; "
            f"{knows}",
        ]


@pytest.mark.spec("APP-14")
def test_a_refused_later_set_up_leaves_no_record_its_table_knew(
    write_app: WriteApp,
) -> None:
    # Added apps whose modules connect() at import to a contract the refused
    # set-up's table knew: one refused as a changed set, one by Resolve.
    connects = 'from neolith import connect\n\nconnect("billing.invoices.v1")\n'
    changed = _invoices_app(write_app, connects + _INVOICES)
    unmet = write_app(
        _apps_module(
            provides=[("Invoices", "{package}.services.InvoiceService")],
            requires=["StockV2"],
        ),
        services=connects + _INVOICES,
    )
    printed = in_fresh_process(
        f"""
        import django
        from django.test.utils import override_settings

        django.setup()
        installed = list(settings.INSTALLED_APPS)
        for added in ({changed!r}, {unmet!r}):
            try:
                with override_settings(INSTALLED_APPS=[added, *installed]):
                    print("admitted")
            except Exception as error:
                print(type(error).__name__)
            with override_settings(INSTALLED_APPS=installed):
                print("the first apps are admitted")
        """,
        host_apps.INSTALLED,
    )
    assert printed.splitlines() == [
        "ImproperlyConfigured",
        "the first apps are admitted",
        "DeclarationError",
        "the first apps are admitted",
    ]


@pytest.mark.spec("APP-15")
@pytest.mark.parametrize("place", ["__init__", "models"])
def test_connect_while_an_override_without_neolith_loads_is_refused_at_once(
    write_app: WriteApp, place: str
) -> None:
    # A module first imported while an override that leaves neolith out is
    # loading asks for an unknown identity: Validate ended in the set-up the
    # override will restore, so it is refused at the connect() line.
    latecomer = write_app(
        "", **{place: 'from neolith import connect\n\nconnect("stokc.v1")\n'}
    )
    with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
        recorded = dict(_RECORDED)
        with (
            pytest.raises(UsageError, match=r"connect\('stokc.v1'\): no app of this"),
            override_settings(INSTALLED_APPS=[latecomer]),
        ):
            pytest.fail("the override was admitted")
        assert dict(_RECORDED) == recorded
        # The restored set-up runs cleanly, and so does a later one.
        assert _current() is not None
        with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
            assert _current() is not None
