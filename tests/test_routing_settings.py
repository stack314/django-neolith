"""Routing settings: ``NEOLITH["ROUTING_POLICIES"]`` and the widened
``NEOLITH["REMOTE_PROVIDERS"]`` (spec 02, Providers and routing; spec 05,
Configure and Resolve; ADR 0031, the admitted set).

The builder's rows are tested through ``neolith._checks`` and the plain
function behind ``neolith.host.build_settings``, ``neolith._configure.configure``,
which records nothing. The Resolve rows are tested through
``neolith._host.resolve`` and the builder. Set-up, and the calls a set-up
routes, run in a process of their own, since a process hosts one set of
apps, and the suite's host provides every contract it requires.
"""

import json
import re
import textwrap
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st

from neolith import _checks, _http_transport
from neolith._configure import configure
from neolith._declaration import Declaration
from neolith._host import resolve
from tests.hosting import in_built_host, in_fresh_process
from tests.test_host import _hosts
from tests.test_remote_providers import (
    _HYPOTHESIS,
    COURIERS,
    CREDENTIAL,
    IN_HOST,
    REMOTE,
    ROUTING,
    SIGNING,
    TRANSPORT,
    WEB,
    deployer,
    entry,
)

A = "https://inventory-a.internal"
B = "https://inventory-b.internal"
NOT_A_POLICY = (
    "is not a routing policy of this host; the one policy to name is "
    "local-first, and leaving {} out means single"
)


class Text(str):
    """A ``str`` subclass, which the rows refuse as a value."""


def problems(neolith: dict[str, Any]) -> list[str]:
    """``NEOLITH``'s problems, as set-up's ``neolith.E001`` finds them."""
    return sorted(_checks.neolith_problems(neolith))


def built(neolith: dict[str, Any], hosted: list[str] | None = None) -> list[str]:
    """The builder's refusal lines for ``hosted`` (the web app alone, whose
    two required contracts are remote, by default), or none when it builds.
    """
    try:
        configure(hosted or [WEB], deployer(SIGNING_SECRETS=SIGNING, **neolith))
    except ImproperlyConfigured as refused:
        return str(refused).splitlines()
    return []


def both(stock: list[object]) -> dict[str, list[object]]:
    """``REMOTE_PROVIDERS`` with ``stock`` for ``stock.v1`` and one entry for
    ``couriers.v3``, the web app's two requirements."""
    return {"stock.v1": stock, "couriers.v3": [entry(A)]}


# The builder's rows: NEOLITH["ROUTING_POLICIES"] (ADR 0031).


@pytest.mark.spec("APP-16", "APP-65")
@pytest.mark.parametrize(
    ("value", "refused"),
    [
        ({"stock.v1": "local-first"}, []),
        ([], [f"{ROUTING}: expected a dict of contract identities, got list"]),
        (
            OrderedDict({"stock.v1": "local-first"}),
            [f"{ROUTING}: expected a dict of contract identities, got OrderedDict"],
        ),
        ({}, [f"{ROUTING}: is empty; leave the key out"]),
        (
            {"Stock.V1": "local-first"},
            [
                f"{ROUTING}: key 'Stock.V1': expected a contract identity, such as "
                "<contract>.v1"
            ],
        ),
        (
            {1: "local-first"},
            [
                f"{ROUTING}: key of type int: expected a contract identity, such "
                "as <contract>.v1"
            ],
        ),
        ({"stock.v1": 1}, [f'{ROUTING}["stock.v1"]: expected a str, got int']),
        ({"stock.v1": None}, [f'{ROUTING}["stock.v1"]: expected a str, got NoneType']),
        (
            {"stock.v1": ["local-first"]},
            [f'{ROUTING}["stock.v1"]: expected a str, got list'],
        ),
        (
            {"stock.v1": Text("local-first")},
            [f'{ROUTING}["stock.v1"]: expected a str, got Text'],
        ),
        (
            {"stock.v1": "single"},
            [
                f'{ROUTING}["stock.v1"]: single is what leaving stock.v1 out '
                "means; leave it out"
            ],
        ),
        (
            {"Stock": "single"},
            [
                f"{ROUTING}: key 'Stock': expected a contract identity, such as "
                "<contract>.v1",
                f"{ROUTING}['Stock']: single is what leaving it out means; leave "
                "it out",
            ],
        ),
        *(
            (
                {"stock.v1": policy},
                [f'{ROUTING}["stock.v1"]: {NOT_A_POLICY.format("stock.v1")}'],
            )
            for policy in [
                "remote-first",
                "weighted",
                "explicit",
                "local_first",
                "Local-First",
                "local-first ",
                " local-first",
                "localfirst",
                "Single",
                "",
            ]
        ),
    ],
)
def test_each_row_of_routing_policies_admits_its_form_and_refuses_the_rest(
    value: object, refused: list[str]
) -> None:
    assert sorted(_checks.routing_problems(value)) == sorted(refused)
    # The same rows through the NEOLITH check, with two entries for the
    # identity so the providers' row adds nothing when local-first is named.
    neolith = {"ROUTING_POLICIES": value, "REMOTE_PROVIDERS": both([entry(A)])}
    if refused:
        assert set(refused) <= set(problems(neolith))
    else:
        neolith["REMOTE_PROVIDERS"] = both([entry(A), entry(B)])
        assert problems(neolith) == []
        assert built(neolith) == []


@pytest.mark.spec("APP-16")
def test_every_routing_refusal_reaches_the_diagnostic() -> None:
    value = {
        "Stock": 3,
        "stock.v1": "single",
        "couriers.v3": "weighted",
        ("other", "v1"): "local-first",
        "other.v1": "local-first",
    }
    assert sorted(_checks.routing_problems(value)) == sorted(
        [
            f"{ROUTING}: key 'Stock': expected a contract identity, such as "
            "<contract>.v1",
            f"{ROUTING}['Stock']: expected a str, got int",
            f'{ROUTING}["stock.v1"]: single is what leaving stock.v1 out means; '
            "leave it out",
            f'{ROUTING}["couriers.v3"]: {NOT_A_POLICY.format("couriers.v3")}',
            f"{ROUTING}: key of type tuple: expected a contract identity, such as "
            "<contract>.v1",
        ]
    )


# The builder's rows: NEOLITH["REMOTE_PROVIDERS"], widened (ADR 0031).

_LISTS = f'{REMOTE}["stock.v1"]: lists {{}} providers; with the single routing '
_LISTS += "policy a contract has one provider; to try them in order, name "
_LISTS += f"local-first for stock.v1 in {ROUTING}"


def _twice(index: int, first: int) -> str:
    return (
        f'{REMOTE}["stock.v1"][{index}]["OPTIONS"]["url"]: is the url of entry '
        f"{first} too; an entry is listed once"
    )


@pytest.mark.spec("APP-16", "APP-63", "APP-64")
@pytest.mark.parametrize(
    ("policies", "stock", "refused"),
    [
        # A step 5 host: one entry, no policy named.
        (None, [entry(A)], []),
        # local-first orders two or more.
        ({"stock.v1": "local-first"}, [entry(A), entry(B)], []),
        (
            {"stock.v1": "local-first"},
            [entry(A), entry(B), entry("http://localhost:8000")],
            [],
        ),
        # Two spellings that reach one server are two entries.
        (
            {"stock.v1": "local-first"},
            [entry("http://localhost:8000"), entry("http://127.0.0.1:8000")],
            [],
        ),
        # Two or more under single, whatever else names a policy.
        (None, [entry(A), entry(B)], [_LISTS.format(2)]),
        ({"couriers.v3": "local-first"}, [entry(A), entry(B)], [_LISTS.format(2)]),
        (
            {"stock.v1": "single"},
            [entry(A), entry(B), entry("http://localhost")],
            [
                _LISTS.format(3),
                f'{ROUTING}["stock.v1"]: single is what leaving stock.v1 out '
                "means; leave it out",
            ],
        ),
        (
            {"stock.v1": Text("local-first")},
            [entry(A), entry(B)],
            [_LISTS.format(2), f'{ROUTING}["stock.v1"]: expected a str, got Text'],
        ),
        (
            ["stock.v1"],
            [entry(A), entry(B)],
            [
                _LISTS.format(2),
                f"{ROUTING}: expected a dict of contract identities, got list",
            ],
        ),
        # A url listed twice, compared as written.
        ({"stock.v1": "local-first"}, [entry(A), entry(A)], [_twice(1, 0)]),
        (
            {"stock.v1": "local-first"},
            [entry(A), entry(B), entry(A), entry(A), entry(B)],
            [_twice(2, 0), _twice(3, 0), _twice(4, 1)],
        ),
        (None, [entry(A), entry(A)], [_LISTS.format(2), _twice(1, 0)]),
        # A url refused by its row, listed twice: both refusals.
        (
            {"stock.v1": "local-first"},
            [entry("http://Localhost"), entry("http://Localhost")],
            [
                f'{REMOTE}["stock.v1"][{i}]["OPTIONS"]["url"]: is not an origin: '
                f"{_http_transport.CASE}"
                for i in (0, 1)
            ]
            + [_twice(1, 0)],
        ),
        # Each entry is judged with its index.
        (
            {"stock.v1": "local-first"},
            [entry(A), {"BACKEND": 1}, entry(b"http://localhost")],
            [
                f'{REMOTE}["stock.v1"][1]: missing key OPTIONS',
                f'{REMOTE}["stock.v1"][1]["BACKEND"]: expected a str, got int',
                f'{REMOTE}["stock.v1"][2]["OPTIONS"]["url"]: expected a str, got bytes',
            ],
        ),
        (
            {"stock.v1": "local-first"},
            [entry(A), "https://inventory-b.internal"],
            [
                f'{REMOTE}["stock.v1"][1]: expected a dict with the keys BACKEND '
                "and OPTIONS, got str"
            ],
        ),
    ],
)
def test_the_providers_row_admits_two_or_more_only_under_local_first(
    policies: object, stock: list[object], refused: list[str]
) -> None:
    neolith: dict[str, Any] = {"REMOTE_PROVIDERS": both(stock)}
    if policies is not None:
        neolith["ROUTING_POLICIES"] = policies
    assert problems(neolith) == sorted(refused)
    lines = built(neolith)
    if refused:
        assert set(refused) <= set(lines)
    else:
        assert lines == []


@pytest.mark.spec("APP-16")
def test_following_the_advice_of_the_providers_row_admits_the_host() -> None:
    neolith: dict[str, Any] = {"REMOTE_PROVIDERS": both([entry(A), entry(B)])}
    assert built(neolith) == [_LISTS.format(2)]
    # "name local-first for stock.v1 in NEOLITH["ROUTING_POLICIES"]"
    neolith["ROUTING_POLICIES"] = {"stock.v1": "local-first"}
    assert built(neolith) == []
    # "leave it out": single with one entry.
    neolith = {
        "ROUTING_POLICIES": {"stock.v1": "single"},
        "REMOTE_PROVIDERS": both([entry(A)]),
    }
    assert built(neolith) == [
        f'{ROUTING}["stock.v1"]: single is what leaving stock.v1 out means; leave '
        "it out"
    ]
    del neolith["ROUTING_POLICIES"]
    assert built(neolith) == []
    # "the one policy to name is local-first"
    neolith = {
        "ROUTING_POLICIES": {"stock.v1": "weighted"},
        "REMOTE_PROVIDERS": both([entry(A), entry(B)]),
    }
    neolith["ROUTING_POLICIES"]["stock.v1"] = "local-first"
    assert built(neolith) == []
    # "an entry is listed once"
    neolith["REMOTE_PROVIDERS"] = both([entry(A), entry(B), entry(A)])
    assert built(neolith) == [_twice(2, 0)]
    del neolith["REMOTE_PROVIDERS"]["stock.v1"][2]
    assert built(neolith) == []


# No message shows a url or a policy value that was refused (ADR 0031, the
# admitted set; ADR 0014 Decision 3).

_URL_CARRIERS = [
    f"http://user:{CREDENTIAL}@inventory.internal",
    f"https://inventory.internal/?token={CREDENTIAL}",
    f"https://{CREDENTIAL}@inventory.internal:8443",
]
_POLICY_CARRIERS = [
    CREDENTIAL,
    f"local-first{CREDENTIAL}",
    f"{CREDENTIAL} local-first",
    f"single {CREDENTIAL}",
    Text(CREDENTIAL),
]


@st.composite
def _carrying(draw: st.DrawFn) -> dict[str, Any]:
    """A ``NEOLITH`` whose refused urls and policy values hold a credential,
    beside any other refusal of the two keys, the Resolve rows' included.
    """
    identities = ["stock.v1", "couriers.v3", "other.v1", "Stock.V1"]
    policies = draw(
        st.dictionaries(
            st.sampled_from(identities),
            st.sampled_from([*_POLICY_CARRIERS, "local-first", "single"]),
            max_size=4,
        )
    )
    urls = st.sampled_from([*_URL_CARRIERS, A, B])
    providers = draw(
        st.dictionaries(
            st.sampled_from(identities),
            st.lists(urls.map(entry), max_size=4),
            max_size=4,
        )
    )
    secret = any(
        type(p) is not str or p not in ("local-first", "single")
        for p in policies.values()
    ) or any(
        e["OPTIONS"]["url"] in _URL_CARRIERS  # type: ignore[index]
        for listed in providers.values()
        for e in listed
    )
    if not secret:
        policies["stock.v1"] = draw(st.sampled_from(_POLICY_CARRIERS))
    neolith: dict[str, Any] = {"ROUTING_POLICIES": policies}
    if providers:
        neolith["REMOTE_PROVIDERS"] = providers
    return neolith


@pytest.mark.spec("APP-16", "APP-64", "APP-65")
@_HYPOTHESIS
@given(_carrying())
def test_no_message_shows_a_refused_url_or_policy_value(
    neolith: dict[str, Any],
) -> None:
    found = problems(neolith)
    assert found
    lines = [
        *found,
        *built(neolith),
        *built(neolith, [COURIERS, WEB]),
        *_checks.routing_problems(neolith["ROUTING_POLICIES"]),
        *_checks.remote_warnings(neolith),
    ]
    assert not any(CREDENTIAL in line for line in lines)


# Resolve (ADR 0031, the Resolve rows; TRN-75, APP-8, APP-64).


def _one_entry(identity: str) -> str:
    return (
        f"{ROUTING}: {identity} has one entry in {REMOTE}, and local-first orders "
        "two or more; remove it, since one provider is routed by single"
    )


_POOL = ["stock.v1", "stock.v2", "billing.invoices.v1", "couriers.v3", "other.v1"]


@st.composite
def _with_routing(
    draw: st.DrawFn,
) -> tuple[list[Declaration], frozenset[str], dict[str, int | None]]:
    declarations = draw(_hosts())
    remote = frozenset(draw(st.lists(st.sampled_from(_POOL), unique=True)))
    routed = draw(
        st.dictionaries(st.sampled_from(_POOL), st.one_of(st.none(), st.integers(0, 3)))
    )
    return declarations, remote, routed


@pytest.mark.spec("TRN-75", "APP-8", "APP-64")
@_HYPOTHESIS
@given(_with_routing())
def test_resolve_with_routing_policies_refuses_exactly_what_the_rules_name(
    drawn: tuple[list[Declaration], frozenset[str], dict[str, int | None]],
) -> None:
    declarations, remote, routed = drawn
    provider: dict[str, str] = {}
    for declaration in declarations:
        for provision in declaration.provides:
            provider.setdefault(provision.contract.identity, declaration.label)
    required = {c.identity for d in declarations for c in d.requires}
    expected = set()
    for identity in remote:
        if identity not in required:
            expected.add(
                f"{REMOTE}: {identity} is required by no hosted app; remove it"
            )
        if identity in provider:
            expected.add(
                f"{REMOTE}: {identity} is provided by app {provider[identity]} in "
                f"this host, {IN_HOST}"
            )
    for identity, count in routed.items():
        if identity not in required:
            expected.add(
                f"{ROUTING}: {identity} is required by no hosted app; remove it"
            )
        if identity in provider:
            expected.add(
                f"{ROUTING}: {identity} is provided by app {provider[identity]} in "
                "this host, which is its one provider; remove it"
            )
        if count == 1:
            expected.add(_one_entry(identity))
    for declaration in declarations:
        for contract in declaration.requires:
            if contract.identity not in provider and contract.identity not in remote:
                expected.add(
                    f"app {declaration.label}: requires: {contract.identity} has no "
                    "provider; no app of this host provides it and "
                    f"{REMOTE} has no entry for it"
                )
    _, refusals = resolve(declarations, remote, routed)
    found = {r for r in refusals if "one identity has one contract class" not in r}
    found = {r for r in found if "one app provides a contract" not in r}
    assert found == expected
    # With no routing policy named, Resolve is step 5's.
    _, alone = resolve(declarations, remote)
    _, none = resolve(declarations, remote, {})
    assert alone == none


@pytest.mark.spec("TRN-75", "APP-8", "APP-12", "APP-64")
def test_the_builder_reports_the_routing_rows_together_and_sorted() -> None:
    neolith: dict[str, Any] = {
        "ROUTING_POLICIES": {
            "stock.v1": "local-first",
            "couriers.v3": "local-first",
            "billing.invoices.v1": "local-first",
        },
        "REMOTE_PROVIDERS": {"stock.v1": [entry(A)], "couriers.v3": [entry(A)]},
    }
    assert built(neolith, [COURIERS, WEB]) == sorted(
        [
            f"{ROUTING}: billing.invoices.v1 is required by no hosted app; remove it",
            f"{ROUTING}: couriers.v3 is provided by app couriers in this host, which "
            "is its one provider; remove it",
            _one_entry("couriers.v3"),
            _one_entry("stock.v1"),
            f"{REMOTE}: couriers.v3 is provided by app couriers in this host, "
            f"{IN_HOST}",
        ]
    )
    # Following the advice: each line's fix admits the host.
    del neolith["ROUTING_POLICIES"]["billing.invoices.v1"]
    del neolith["ROUTING_POLICIES"]["couriers.v3"]
    del neolith["REMOTE_PROVIDERS"]["couriers.v3"]
    assert built(neolith, [COURIERS, WEB]) == [_one_entry("stock.v1")]
    del neolith["ROUTING_POLICIES"]
    assert built(neolith, [COURIERS, WEB]) == []
    neolith["ROUTING_POLICIES"] = {"stock.v1": "local-first"}
    neolith["REMOTE_PROVIDERS"]["stock.v1"].append(entry(B))
    assert built(neolith, [COURIERS, WEB]) == []


@pytest.mark.spec("APP-8", "APP-64")
def test_an_identity_named_with_no_entry_is_refused_by_its_unmet_requirement() -> None:
    neolith: dict[str, Any] = {
        "ROUTING_POLICIES": {"stock.v1": "local-first"},
        "REMOTE_PROVIDERS": {"couriers.v3": [entry(A)]},
    }
    # The requirement's refusal names both places to look; the routing row
    # adds nothing.
    assert built(neolith) == [
        "app web: requires: stock.v1 has no provider; no app of this host "
        f"provides it and {REMOTE} has no entry for it"
    ]


# override_settings, W002 (ADR 0031, the admitted set).


def test_an_override_of_routing_policies_after_set_up_is_refused() -> None:
    assert "ROUTING_POLICIES" not in settings.NEOLITH
    changed = {**settings.NEOLITH, "ROUTING_POLICIES": {"stock.v1": "local-first"}}
    with (
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(NEOLITH=changed),
    ):
        pass  # pragma: no cover
    assert str(raised.value) == f"{ROUTING}: cannot change after set-up"
    # A value outside the form is refused by its row, never shown.
    with (
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(
            NEOLITH={
                **settings.NEOLITH,
                "ROUTING_POLICIES": {"couriers.v3": f"local-first{CREDENTIAL}"},
            }
        ),
    ):
        pass  # pragma: no cover
    assert str(raised.value) == (
        f'{ROUTING}["couriers.v3"]: {NOT_A_POLICY.format("couriers.v3")}'
    )


def test_w002_names_each_entry_of_a_list_by_its_index() -> None:
    neolith = {
        "ROUTING_POLICIES": {"stock.v1": "local-first"},
        "REMOTE_PROVIDERS": {
            "stock.v1": [
                entry(A),
                entry("http://inventory.internal"),
                entry("http://localhost:8000"),
                entry("http://10.0.0.1"),
            ]
        },
    }
    tail = (
        "is plain HTTP to a host that is not loopback; anyone who can watch the "
        "network can read and replay the calling app's token (spec 03)"
    )
    assert _checks.remote_warnings(neolith) == [
        f'{REMOTE}["stock.v1"][1]: http://inventory.internal {tail}',
        f'{REMOTE}["stock.v1"][3]: http://10.0.0.1 {tail}',
    ]


# Set-up, in a process of its own: a step 5 settings module is routed by
# single; a list under local-first is the connector's order (ADR 0031
# Decisions 2 and 7).

_SERVERS = """
import socket
import threading

heads = {}
UNAVAILABLE = b"HTTP/1.1 503 Service Unavailable\\r\\nContent-Length: 0\\r\\n\\r\\n"
REPLY = b'HTTP/1.1 200 OK\\r\\nContent-Length: 13\\r\\n\\r\\n{"on_hand":1}'

def provider(name, reply):
    server = socket.create_server(("127.0.0.1", 0))
    heads[name] = []

    def serve(connection):
        with connection:
            data = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                data += chunk
                while b"\\r\\n\\r\\n" in data:
                    head, _, data = data.partition(b"\\r\\n\\r\\n")
                    length = int(
                        head.lower().split(b"content-length: ")[1].split(b"\\r")[0]
                    )
                    while len(data) < length:
                        data += connection.recv(65536)
                    data = data[length:]
                    heads[name].append(head.split(b"\\r\\n")[0].decode())
                    connection.sendall(reply)

    def accept():
        while True:
            connection, _ = server.accept()
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}"

def listed(*urls):
    return [
        {"BACKEND": "neolith.transports.HTTPTransport", "OPTIONS": {"url": url}}
        for url in urls
    ]
"""


@pytest.mark.spec("APP-63", "TRN-17")
def test_a_step_5_settings_module_is_admitted_and_routed_by_single() -> None:
    out = in_fresh_process(
        _SERVERS
        + textwrap.dedent(
            """
        u = provider("U", UNAVAILABLE)
        settings.NEOLITH["REMOTE_PROVIDERS"] = {"stock.v1": listed(u)}

        import django

        django.setup()

        import neolith
        from neolith._host import _table
        from tests.declaration_contracts import Sku

        host, _ = _table()
        routes = {
            identity: len(found._routes)
            for identity, found in sorted(host.connectors.items())
        }
        kinds = []
        for _ in range(2):
            with neolith.command_scope("web", timeout=10):
                try:
                    neolith.connect("stock.v1").level(Sku(sku="abc"))
                except neolith.ContractError as error:
                    kinds.append(error.kind)
        print(json.dumps([routes, kinds, heads["U"]]))
        """
        ),
        [COURIERS, WEB, "neolith"],
        {"NEOLITH": {"SIGNING_SECRETS": SIGNING}},
        timeout=120,
    )
    routes, kinds, heads = json.loads(out.splitlines()[-1])
    # One provider per identity: the app of the host, else the one remote.
    assert routes == {"couriers.v3": 1, "stock.v1": 1}
    # level is idempotent, and still makes one attempt per call.
    assert kinds == ["unavailable", "unavailable"]
    assert heads == ["POST /stock/v1/level HTTP/1.1"] * 2


@pytest.mark.spec("APP-63")
def test_a_list_under_local_first_is_tried_in_order_and_each_entry_warned() -> None:
    out = in_fresh_process(
        _SERVERS
        + textwrap.dedent(
            """
        import logging

        class Keep(logging.Handler):
            lines = []

            def emit(self, record):
                Keep.lines.append(record.getMessage())

        logging.getLogger("neolith").addHandler(Keep())
        u = provider("U", UNAVAILABLE)
        b = provider("B", REPLY)
        settings.NEOLITH["ROUTING_POLICIES"] = {"stock.v1": "local-first"}
        settings.NEOLITH["REMOTE_PROVIDERS"] = {
            "stock.v1": listed(u, b, "http://inventory.internal"),
        }

        import django

        django.setup()

        import neolith
        from neolith._host import _table
        from tests.declaration_contracts import Sku

        host, _ = _table()
        order = [r.transport.origin for r in host.connectors["stock.v1"]._routes]
        replies = []
        for _ in range(2):
            with neolith.command_scope("web", timeout=10):
                replies.append(neolith.connect("stock.v1").level(Sku(sku="a")).on_hand)
        warned = [line for line in Keep.lines if "W002" in line]
        print(json.dumps([order == [u, b, "http://inventory.internal"], replies,
                          heads, warned]))
        """
        ),
        [COURIERS, WEB, "neolith"],
        {"NEOLITH": {"SIGNING_SECRETS": SIGNING}},
        timeout=120,
    )
    ordered, replies, heads, warned = json.loads(out.splitlines()[-1])
    assert ordered
    # Each call starts at the first entry, and stops at the one that answers.
    assert replies == [1, 1]
    assert heads == {
        "U": ["POST /stock/v1/level HTTP/1.1"] * 2,
        "B": ["POST /stock/v1/level HTTP/1.1"] * 2,
    }
    assert warned == [
        f'(neolith.W002) {REMOTE}["stock.v1"][2]: http://inventory.internal is '
        "plain HTTP to a host that is not loopback; anyone who can watch the "
        "network can read and replay the calling app's token (spec 03)"
    ]


_SET_UP = """
import django
from django.core.exceptions import ImproperlyConfigured

try:
    django.setup()
except ImproperlyConfigured as error:
    print(json.dumps(str(error)))
else:
    print(json.dumps(None))
"""
_E001 = "host: the settings are refused before the host serves:"


@pytest.mark.spec("APP-1", "TRN-75", "APP-64", "APP-65")
@pytest.mark.parametrize(
    ("neolith", "refusal"),
    [
        (
            {
                "ROUTING_POLICIES": {"stock.v1": f"local-first{CREDENTIAL}"},
                "REMOTE_PROVIDERS": {"stock.v1": [entry(A)]},
            },
            [
                _E001,
                f'(neolith.E001) {ROUTING}["stock.v1"]: '
                + NOT_A_POLICY.format("stock.v1"),
            ],
        ),
        (
            {"ROUTING_POLICIES": [], "REMOTE_PROVIDERS": {"stock.v1": [entry(A)]}},
            [
                _E001,
                f"(neolith.E001) {ROUTING}: expected a dict of contract "
                "identities, got list",
            ],
        ),
        (
            {"REMOTE_PROVIDERS": {"stock.v1": [entry(A), entry(B)]}},
            [_E001, f"(neolith.E001) {_LISTS.format(2)}"],
        ),
        (
            {
                "ROUTING_POLICIES": {"stock.v1": "local-first"},
                "REMOTE_PROVIDERS": {"stock.v1": [entry(A), entry(A)]},
            },
            [_E001, f"(neolith.E001) {_twice(1, 0)}"],
        ),
        (
            {
                "ROUTING_POLICIES": {"stock.v1": "local-first"},
                "REMOTE_PROVIDERS": {"stock.v1": [entry(A)]},
            },
            [_one_entry("stock.v1")],
        ),
        (
            {
                "ROUTING_POLICIES": {
                    "stock.v1": "local-first",
                    "other.v1": "local-first",
                },
                "REMOTE_PROVIDERS": {"stock.v1": [entry(A), entry(B)]},
            },
            [f"{ROUTING}: other.v1 is required by no hosted app; remove it"],
        ),
        (
            {
                "ROUTING_POLICIES": {"couriers.v3": "local-first"},
                "REMOTE_PROVIDERS": {"stock.v1": [entry(A)]},
            },
            [
                f"{ROUTING}: couriers.v3 is provided by app couriers in this host, "
                "which is its one provider; remove it"
            ],
        ),
        (
            {"REMOTE_PROVIDERS": both([entry(A)])},  # couriers.v3 is hosted
            [
                f"{REMOTE}: couriers.v3 is provided by app couriers in this host, "
                f"{IN_HOST}"
            ],
        ),
        # Every row at once.
        (
            {
                "ROUTING_POLICIES": {
                    "stock.v1": f"{CREDENTIAL}",
                    "couriers.v3": "local-first",
                    "other.v1": "single",
                },
                "REMOTE_PROVIDERS": {
                    "stock.v1": [
                        entry(f"http://user:{CREDENTIAL}@localhost"),
                        entry(A),
                        entry(A),
                    ],
                    "couriers.v3": [entry(A)],
                },
            },
            sorted(
                [
                    f"{ROUTING}: other.v1 is required by no hosted app; remove it",
                    f"{ROUTING}: couriers.v3 is provided by app couriers in this "
                    "host, which is its one provider; remove it",
                    _one_entry("couriers.v3"),
                    f"{REMOTE}: couriers.v3 is provided by app couriers in this "
                    f"host, {IN_HOST}",
                    f'{ROUTING}["stock.v1"]: {NOT_A_POLICY.format("stock.v1")}',
                    f'{ROUTING}["other.v1"]: single is what leaving other.v1 out '
                    "means; leave it out",
                    _LISTS.format(3),
                    _twice(2, 1),
                    f'{REMOTE}["stock.v1"][0]["OPTIONS"]["url"]: is not an origin: '
                    f"{_http_transport.CREDENTIAL}",
                ]
            ),
        ),
    ],
)
def test_set_up_stops_on_each_routing_row_and_shows_no_refused_value(
    neolith: dict[str, Any], refusal: list[str]
) -> None:
    out = in_fresh_process(
        _SET_UP,
        [COURIERS, WEB, "neolith"],
        {"NEOLITH": {**neolith, "SIGNING_SECRETS": SIGNING}},
        timeout=120,
    )
    assert json.loads(out.splitlines()[-1]).splitlines() == refusal
    assert CREDENTIAL not in out


# The guide's example passes the builder and set-up.

_GUIDE = Path(__file__).resolve().parents[1] / "docs" / "django-host.md"


def _guide_example() -> dict[str, Any]:
    """The guide's ``local-first`` example, its names mapped onto the suite's
    web app, which requires ``stock.v1`` and ``couriers.v3``.
    """
    text = _GUIDE.read_text()
    (block,) = [
        found
        for found in re.findall(r"```python\n(.*?)```", text, re.DOTALL)
        if "ROUTING_POLICIES" in found
    ]
    block = block.replace("inventory.v1", "stock.v1").replace('"orders"', '"web"')
    names: dict[str, Any] = {"ORDERS_KEY": SIGNING["web"]["SECRET"]}
    exec(block, names)  # noqa: S102  # the repository's own guide
    neolith: dict[str, Any] = names["NEOLITH"]
    neolith["SIGNING_SECRETS"]["web"]["KEY_ID"] = SIGNING["web"]["KEY_ID"]
    return neolith


def test_the_guides_example_passes_the_builder_and_set_up() -> None:
    neolith = _guide_example()
    assert neolith["ROUTING_POLICIES"] == {"stock.v1": "local-first"}
    assert len(neolith["REMOTE_PROVIDERS"]["stock.v1"]) == 2
    assert configure([COURIERS, WEB], deployer(**neolith))
    out = in_built_host(
        """
        import django

        django.setup()

        from neolith._host import _table

        host, _ = _table()
        found = host.connectors["stock.v1"]._routes
        print(json.dumps([r.transport.origin for r in found]))
        """,
        [COURIERS, WEB],
        {"NEOLITH": neolith},
        timeout=120,
    )
    assert json.loads(out.splitlines()[-1]) == [
        e["OPTIONS"]["url"] for e in neolith["REMOTE_PROVIDERS"]["stock.v1"]
    ]
    assert TRANSPORT in json.dumps(neolith)
