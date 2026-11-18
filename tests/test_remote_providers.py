"""Remote providers: ``NEOLITH["REMOTE_PROVIDERS"]`` (spec 02, Providers and
routing; spec 05, Resolve; ADR 0014 as amended for step 5).

The builder's rows are tested through ``neolith._checks.remote_problems``
and the plain function behind ``neolith.host.build_settings``,
``neolith._configure.configure``, which records nothing. The Resolve rows
are tested through ``neolith._host.resolve`` and the builder. Set-up, Boot
and a call to a provider that cannot be reached run in a process of their
own, since a process hosts one set of apps, and the suite's host provides
every contract it requires.
"""

import ipaddress
import json
import re
import secrets
import socket
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from neolith import _checks, _http, _http_transport
from neolith._configure import configure
from neolith._declaration import Declaration, declaration_of
from neolith._host import resolve
from tests.host_apps.web.apps import WebConfig
from tests.hosting import NEOLITH_KEYS, in_fresh_process
from tests.test_host import _hosts

REMOTE = 'NEOLITH["REMOTE_PROVIDERS"]'
ROUTING = 'NEOLITH["ROUTING_POLICIES"]'
TRANSPORT = "neolith.transports.HTTPTransport"
ROOT = "neolith.middleware.RootMiddleware"
PRINCIPAL = "neolith.middleware.PrincipalMiddleware"
WEB = "tests.host_apps.web"
COURIERS = "tests.host_apps.couriers"
# A secret, for the fresh hosts' signing keys.
SECRET = secrets.token_urlsafe(32)
SIGNING = {"web": {"KEY_ID": "web-1", "SECRET": SECRET}}
# The tail of the refusal of a remote provider for a contract an app of the
# host provides (ADR 0031, the Resolve rows).
IN_HOST = (
    "which answers every call to it; an in-process call never ends with "
    "unavailable, so a remote provider listed beside it would never be called; "
    "remove the entry"
)
# Text that no fixed message holds, standing for a credential.
CREDENTIAL = "Zq7hunter2"

_HYPOTHESIS = hypothesis_settings(
    derandomize=True,
    database=None,
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def entry(url: object = "https://inventory.internal:8443") -> dict[str, object]:
    return {"BACKEND": TRANSPORT, "OPTIONS": {"url": url}}


def remote(**urls: object) -> dict[str, list[dict[str, object]]]:
    """``REMOTE_PROVIDERS`` with one entry per identity, keyed as written
    with ``_`` for ``.``: ``stock_v1`` is ``stock.v1``."""
    return {name.replace("_", "."): [entry(url)] for name, url in urls.items()}


def deployer(**neolith: object) -> dict[str, Any]:
    return {"MIDDLEWARE": [ROOT, PRINCIPAL], "NEOLITH": dict(neolith)}


def built(value: object) -> str | None:
    """The builder's refusal for the web app, alone, whose two required
    contracts are both remote, or ``None`` when it builds.
    """
    try:
        configure([WEB], deployer(REMOTE_PROVIDERS=value))
    except ImproperlyConfigured as refused:
        return str(refused)
    return None


# The builder's rows (ADR 0014, the admitted set).

_URL = f'{REMOTE}["stock.v1"][0]["OPTIONS"]["url"]'


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("value", "problems"),
    [
        (remote(stock_v1="https://x.internal:8443", couriers_v3="https://x.y"), []),
        (remote(stock_v1="http://localhost:8000", couriers_v3="http://[::1]:9"), []),
        ([], [f"{REMOTE}: expected a dict of contract identities, got list"]),
        (
            OrderedDict(remote(stock_v1="http://localhost")),
            [f"{REMOTE}: expected a dict of contract identities, got OrderedDict"],
        ),
        ({}, [f"{REMOTE}: is empty; leave the key out"]),
        (
            {"Stock.V1": [entry()]},
            [
                f"{REMOTE}: key 'Stock.V1': expected a contract identity, such as "
                "<contract>.v1"
            ],
        ),
        (
            {"stock.v1": [entry()], 1: [entry()]},
            [
                f"{REMOTE}: key of type int: expected a contract identity, such as "
                "<contract>.v1"
            ],
        ),
        (
            {"stock.v1": (entry(),)},
            [f'{REMOTE}["stock.v1"]: expected a list of providers, got tuple'],
        ),
        ({"stock.v1": []}, [f'{REMOTE}["stock.v1"]: is empty; leave stock.v1 out']),
        (
            {"stock.v1": [entry(), entry("https://other.internal")]},
            [
                f'{REMOTE}["stock.v1"]: lists 2 providers; with the single routing '
                "policy a contract has one provider; to try them in order, name "
                f"local-first for stock.v1 in {ROUTING}"
            ],
        ),
        (
            {"stock.v1": ["https://inventory.internal"]},
            [
                f'{REMOTE}["stock.v1"][0]: expected a dict with the keys BACKEND '
                "and OPTIONS, got str"
            ],
        ),
        (
            {"stock.v1": [OrderedDict(entry())]},
            [
                f'{REMOTE}["stock.v1"][0]: expected a dict with the keys BACKEND '
                "and OPTIONS, got OrderedDict"
            ],
        ),
        (
            {"stock.v1": [{"OPTIONS": {"url": "http://localhost"}}]},
            [f'{REMOTE}["stock.v1"][0]: missing key BACKEND'],
        ),
        (
            {"stock.v1": [{**entry(), "TIMEOUT": 5}]},
            [
                f"{REMOTE}[\"stock.v1\"][0]: unknown key 'TIMEOUT'; the keys are "
                "BACKEND, OPTIONS"
            ],
        ),
        (
            {"stock.v1": [{**entry(), "BACKEND": None}]},
            [f'{REMOTE}["stock.v1"][0]["BACKEND"]: expected a str, got NoneType'],
        ),
        (
            {"stock.v1": [{**entry(), "BACKEND": "neolith.transports.HttpTransport"}]},
            [
                f'{REMOTE}["stock.v1"][0]["BACKEND"]: '
                "'neolith.transports.HttpTransport' is not a transport of this "
                f"host; the one remote transport in 0.1 is {TRANSPORT}"
            ],
        ),
        (
            {"stock.v1": [{**entry(), "OPTIONS": [("url", "http://localhost")]}]},
            [f'{REMOTE}["stock.v1"][0]["OPTIONS"]: expected a dict, got list'],
        ),
        (
            {"stock.v1": [{**entry(), "OPTIONS": {}}]},
            [f'{REMOTE}["stock.v1"][0]["OPTIONS"]: missing key url'],
        ),
        (
            {
                "stock.v1": [
                    {**entry(), "OPTIONS": {"url": "http://localhost", "timeout": 5}}
                ]
            },
            [
                f'{REMOTE}["stock.v1"][0]["OPTIONS"]: unknown key \'timeout\'; the '
                "HTTP transport's options are url"
            ],
        ),
        (remote(stock_v1=b"http://localhost"), [f"{_URL}: expected a str, got bytes"]),
        (
            remote(stock_v1="https://inventory.internal/"),
            [f"{_URL}: is not an origin: {_http_transport.PATH}"],
        ),
    ],
)
def test_each_row_of_the_builder_admits_its_form_and_refuses_the_rest(
    value: object, problems: list[str]
) -> None:
    assert _checks.remote_problems(value) == problems
    # The same rows through the NEOLITH check and the builder.
    found = _checks.neolith_problems({"REMOTE_PROVIDERS": value})
    assert found == problems
    refusal = built(value)
    if not problems:
        assert refusal is None
    else:
        assert refusal is not None
        lines = refusal.splitlines()
        assert all(problem in lines for problem in problems)


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    "backend",
    [
        "neolith.transports.HttpTransport",
        "neolith._http_transport.HTTPTransport",
        "transports.HTTPTransport",
        "neolith.transports.HTTPTransport ",
        "neolith.transports.HTTPTransportX",
        "neolith.transports",
    ],
)
def test_backend_is_compared_as_written_and_never_imported(backend: str) -> None:
    value = {"stock.v1": [{**entry(), "BACKEND": backend}]}
    assert _checks.remote_problems(value) == [
        f'{REMOTE}["stock.v1"][0]["BACKEND"]: {backend!r} is not a transport of '
        f"this host; the one remote transport in 0.1 is {TRANSPORT}"
    ]


@pytest.mark.spec("APP-16")
def test_every_refusal_reaches_the_diagnostic_whatever_was_refused_first() -> None:
    value = {
        "Stock": [entry("http://Localhost"), {"BACKEND": 1}],
        "couriers.v3": [{"BACKEND": TRANSPORT, "OPTIONS": {"url": 8, "x": 1}}],
    }
    assert sorted(_checks.remote_problems(value)) == sorted(
        [
            f"{REMOTE}: key 'Stock': expected a contract identity, such as "
            "<contract>.v1",
            f"{REMOTE}['Stock']: lists 2 providers; with the single routing policy "
            "a contract has one provider; to try them in order, name local-first "
            f"for it in {ROUTING}",
            f'{REMOTE}[\'Stock\'][0]["OPTIONS"]["url"]: is not an origin: '
            f"{_http_transport.CASE}",
            f"{REMOTE}['Stock'][1]: missing key OPTIONS",
            f"{REMOTE}['Stock'][1][\"BACKEND\"]: expected a str, got int",
            f'{REMOTE}["couriers.v3"][0]["OPTIONS"]: unknown key \'x\'; the HTTP '
            "transport's options are url",
            f'{REMOTE}["couriers.v3"][0]["OPTIONS"]["url"]: expected a str, got int',
        ]
    )


@pytest.mark.spec("APP-16")
def test_the_example_of_the_record_is_admitted_by_the_builder() -> None:
    inventory = "https://inventory.internal:8443"
    value = remote(stock_v1=inventory, couriers_v3=inventory)
    assert configure([WEB], deployer(REMOTE_PROVIDERS=value, SIGNING_SECRETS=SIGNING))


# A refused url is never shown, in whole or in part, on any refusal path
# (ADR 0014 Decision 3). A refused key or BACKEND is shown, bounded, as the
# record's table writes it.

_CARRIERS = [
    f"http://user:{CREDENTIAL}@inventory.internal",
    f"https://{CREDENTIAL}@inventory.internal:8443",
    f"https://inventory.internal/?token={CREDENTIAL}",
    f"https://inventory.internal/{CREDENTIAL}/",
    f"https://inventory.internal#{CREDENTIAL}",
    f"HTTPS://inventory.internal?key={CREDENTIAL}",
    f"http://a_b.internal/?t={CREDENTIAL}",
    f"http://[::1%{CREDENTIAL}]",
    f"http://inventory.internal:{CREDENTIAL}",
]


@st.composite
def _carrying(draw: st.DrawFn) -> object:
    """A ``REMOTE_PROVIDERS`` value whose refused url holds a credential,
    beside any other refusal of the key: of the identity, the entries, the
    entry's keys, ``BACKEND`` or the options.
    """
    url = draw(st.sampled_from(_CARRIERS))
    item: dict[str, object] = entry(url)
    others = draw(
        st.sets(st.sampled_from(["identity", "entry", "backend", "options", "list"]))
    )
    if "entry" in others:
        item["TIMEOUT"] = 5
    if "backend" in others:
        item["BACKEND"] = "neolith.transports.HttpTransport"
    if "options" in others:
        item["OPTIONS"] = {"url": url, "timeout": 5}
    identity = "Stock.V1" if "identity" in others else "stock.v1"
    providers = [item, entry()] if "list" in others else [item]
    return {identity: providers, "couriers.v3": [entry(url)]}


@pytest.mark.spec("APP-16")
@_HYPOTHESIS
@given(_carrying())
def test_no_refusal_shows_a_refused_url_whatever_else_is_refused(
    value: object,
) -> None:
    problems = _checks.remote_problems(value)
    assert problems
    messages = [
        *problems,
        *_checks.neolith_problems({"REMOTE_PROVIDERS": value}),
        built(value) or "",
    ]
    assert not any(CREDENTIAL in message for message in messages)


@pytest.mark.parametrize("key", [1, None, ("stock", "v1"), "x" * 81 + "y", "Stock.V1"])
def test_a_refused_key_is_shown_bounded_and_nothing_else_runs(key: object) -> None:
    class Loud:
        def __repr__(self) -> str:
            raise AssertionError("an author's repr ran")

        def __hash__(self) -> int:
            return 1

    for refused in (key, Loud()):
        (problem, *_) = _checks.remote_problems({refused: [entry()]})
        assert len(problem) < 200
        if type(refused) is not str:
            assert f"{REMOTE}: key of type " in problem


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize("url", _CARRIERS)
def test_a_refused_url_is_never_shown_and_names_its_reason(url: str) -> None:
    reason = _http_transport.origin_problem(url)
    assert reason is not None
    assert _checks.remote_problems(remote(stock_v1=url)) == [
        f"{_URL}: is not an origin: {reason}"
    ]


# The url's one spelling: admitted exactly when it is its origin's
# serialisation (ADR 0014 Decision 3), judged against the URL Standard's
# host and port rules, written here from the standard.


def _ipv4_number(text: str) -> int | None:
    """The URL Standard's IPv4 number parser, or ``None`` on failure."""
    if not text:
        return None
    radix, digits = 10, "0123456789"
    if text[:2] in ("0x", "0X"):
        text, radix, digits = text[2:], 16, "0123456789abcdefABCDEF"
    elif len(text) > 1 and text[0] == "0":
        text, radix, digits = text[1:], 8, "01234567"
    if not text:
        return 0
    if any(c not in digits for c in text):
        return None
    return int(text, radix)


def _ends_in_a_number(host: str) -> bool:
    parts = host.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if last and all(c in "0123456789" for c in last):
        return True
    return _ipv4_number(last) is not None


def _ipv4(host: str) -> str | None:
    """The URL Standard's IPv4 parser and serialiser, or ``None``."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        return None
    numbers = [_ipv4_number(part) for part in parts]
    if any(n is None for n in numbers):
        return None
    found = [n for n in numbers if n is not None]
    if any(n > 255 for n in found[:-1]) or found[-1] >= 256 ** (5 - len(found)):
        return None
    value = found[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(found[:-1]))
    return ".".join(str((value >> shift) & 255) for shift in (24, 16, 8, 0))


def _ipv6(pieces: list[int]) -> str:
    """The URL Standard's IPv6 serialiser: the first longest run of two or
    more zero pieces compressed, each piece in lower-case hex."""
    start, best, at = None, 1, 0
    while at < 8:
        if pieces[at] == 0:
            end = at
            while end < 8 and pieces[end] == 0:
                end += 1
            if end - at > best:
                start, best = at, end - at
            at = end
        else:
            at += 1
    if start is None:
        return ":".join(format(p, "x") for p in pieces)
    head = ":".join(format(p, "x") for p in pieces[:start])
    tail = ":".join(format(p, "x") for p in pieces[start + best :])
    return f"{head}::{tail}"


_LAYOUT = re.compile(
    r"(?P<scheme>[^:/?#]*)://(?P<host>\[[^\]]*\]|[^:/?#@\[]*)(?::(?P<port>[^/?#]*))?"
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _expected(url: str) -> bool:
    """Whether ``url`` is its origin's ASCII serialisation within the
    grammar's restrictions: RFC 1123 labels, no trailing dot, no zone, no
    IPv4-mapped address, a port from 1.

    A label is taken as written and never decoded, ``xn--`` labels included:
    ADR 0014 admits an A-label as an ordinary RFC 1123 label, so a name that
    is not valid IDNA fails at the first call, like any misspelt name.
    """
    found = _LAYOUT.fullmatch(url)
    if found is None:  # a serialisation has exactly this layout
        return False
    scheme, host, port = found["scheme"], found["host"], found["port"]
    if scheme not in ("http", "https"):
        return False
    if host.startswith("["):
        inner = host[1:-1]
        try:
            address = ipaddress.IPv6Address(inner) if "%" not in inner else None
        except ValueError:
            address = None
        if address is None or address.ipv4_mapped is not None:
            return False
        number = int(address)
        serialised = (
            "[" + _ipv6([(number >> (112 - 16 * i)) & 0xFFFF for i in range(8)]) + "]"
        )
    elif not host.isascii() or not host:
        return False
    elif _ends_in_a_number(host):
        address4 = _ipv4(host)
        if address4 is None:
            return False
        serialised = address4
    else:
        serialised = host.lower()
        labels = host.split(".")
        if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
            return False
    if port is not None:
        if not port or any(c not in "0123456789" for c in port):
            return False
        number = int(port)
        if number > 65535 or number == 0:
            return False
        if number != {"http": 80, "https": 443}[scheme]:
            serialised += f":{number}"
    return url == f"{scheme}://{serialised}"


_PIECES = st.sampled_from([0, 0, 0, 1, 0xA, 0xFFFF, 0x7F00, 0xDB8, 0x2001])


@st.composite
def _host(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["name", "name", "ipv4", "ipv6", "odd"]))
    if kind == "name":
        labels = draw(
            st.lists(
                st.text(alphabet="ab09-_AÉ", min_size=0, max_size=5)
                | st.sampled_from(
                    [
                        *("a", "example", "x1", "0x7f", "1", "0x", "a" * 63, "a" * 64),
                        *("xn--zz", "xn--a", "xn--bcher-kva", "xn--zca", "XN--zca"),
                    ]
                ),
                min_size=1,
                max_size=4,
            )
        )
        return ".".join(labels) + draw(st.sampled_from(["", "", "", "."]))
    if kind == "ipv4":
        count = draw(st.sampled_from([4, 4, 4, 1, 2, 3, 5]))
        parts = []
        for _ in range(count):
            n = draw(st.integers(0, 300))
            form = draw(st.sampled_from(["{}", "{}", "{}", "0x{:x}", "0{:o}", "00{}"]))
            parts.append(form.format(n))
        return ".".join(parts) + draw(st.sampled_from(["", "", "."]))
    if kind == "ipv6":
        address = ipaddress.IPv6Address(
            sum(draw(_PIECES) << (112 - 16 * i) for i in range(8))
        )
        if draw(st.booleans()):
            address = ipaddress.IPv6Address(
                "::ffff:" + str(ipaddress.IPv4Address(draw(st.integers(0, 2**32 - 1))))
            )
        text = draw(
            st.sampled_from(
                [
                    str(address),
                    str(address),
                    address.exploded,
                    str(address).upper(),
                    _ipv6(
                        [(int(address) >> (112 - 16 * i)) & 0xFFFF for i in range(8)]
                    ),
                    str(address) + "%eth0",
                ]
            )
        )
        return f"[{text}]"
    return draw(st.sampled_from(["", " localhost", "local host", "[::1", "a@b", "x:y"]))


_ODD: dict[str, st.SearchStrategy[str]] = {
    "scheme": st.sampled_from(["HTTP", "Https", "ftp", "", "http ", "httpx"]),
    "separator": st.sampled_from([":/", ":///", "//", ":\\\\", "\t://"]),
    "user": st.sampled_from(["u@", "u:p@", "@", ":@"]),
    "port": st.sampled_from(
        [":", ":0", ":00", ":80", ":443", ":08000", ":65536", ":99999", ":8a", "::1"]
    ),
    "tail": st.sampled_from(["/", "/x", "?t=1", "#f", " ", "\n", "."]),
}


@st.composite
def _canonical_host(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["name", "name", "ipv4", "ipv6"]))
    if kind == "name":
        label = st.from_regex(
            r"(xn--)?[a-z0-9]([a-z0-9-]{0,6}[a-z0-9])?", fullmatch=True
        )
        labels = draw(st.lists(label, min_size=1, max_size=4))
        if _ends_in_a_number(".".join(labels)):
            labels.append("example")
        return ".".join(labels)
    if kind == "ipv4":
        return str(ipaddress.IPv4Address(draw(st.integers(0, 2**32 - 1))))
    address = ipaddress.IPv6Address(
        sum(draw(_PIECES) << (112 - 16 * i) for i in range(8))
    )
    return f"[{address}]" if address.ipv4_mapped is None else "[::1]"


@st.composite
def _urls(draw: st.DrawFn) -> str:
    """A url in its one spelling, with up to two of its parts written
    another way, so that admitted and refused urls are both drawn often.
    """
    parts = {
        "scheme": draw(st.sampled_from(["http", "https"])),
        "separator": "://",
        "user": "",
        "host": draw(_canonical_host()),
        "port": draw(st.sampled_from(["", f":{draw(st.integers(1, 65535))}"])),
        "tail": "",
    }
    for part in draw(st.lists(st.sampled_from([*_ODD, "host"]), max_size=2)):
        parts[part] = draw(_host() if part == "host" else _ODD[part])
    return "".join(parts.values())


@pytest.mark.spec("APP-16")
@_HYPOTHESIS
@given(_urls())
def test_a_url_is_admitted_exactly_when_it_is_its_origins_serialisation(
    url: str,
) -> None:
    refusal = built(remote(stock_v1=url, couriers_v3="http://localhost"))
    assert (refusal is None) == _expected(url), refusal
    if refusal is None:
        # Every admitted url is one the client reads, as written.
        assert _http.Origin.parse(url).url == url
    else:
        assert url not in refusal or url == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://0x7f.1",
        "http://1.0x2",
        "http://127.1",
        "http://a_b.example",
        "http://example.com.",
        "http://[::ffff:127.0.0.1]",
        "http://[::ffff:7f00:1]",
        "http://münchen.example",
    ],
)
def test_the_records_refused_examples_are_refused_by_the_builder(url: str) -> None:
    assert not _expected(url)
    refusal = built(remote(stock_v1=url, couriers_v3="http://localhost"))
    assert refusal is not None
    assert f"{_URL}: is not an origin:" in refusal


# Resolve (TRN-75, APP-8).


@st.composite
def _with_remote(draw: st.DrawFn) -> tuple[list[Declaration], frozenset[str]]:
    declarations = draw(_hosts())
    pool = ["stock.v1", "stock.v2", "billing.invoices.v1", "couriers.v3", "other.v1"]
    return declarations, frozenset(draw(st.lists(st.sampled_from(pool), unique=True)))


@pytest.mark.spec("TRN-75", "APP-8")
@_HYPOTHESIS
@given(_with_remote())
def test_resolve_with_remote_providers_refuses_exactly_what_the_rules_name(
    drawn: tuple[list[Declaration], frozenset[str]],
) -> None:
    declarations, listed = drawn
    provider: dict[str, str] = {}
    for declaration in declarations:
        for provision in declaration.provides:
            provider.setdefault(provision.contract.identity, declaration.label)
    required = {c.identity for d in declarations for c in d.requires}
    expected = set()
    for identity in listed:
        if identity not in required:
            expected.add(
                f"{REMOTE}: {identity} is required by no hosted app; remove it"
            )
        if identity in provider:
            expected.add(
                f"{REMOTE}: {identity} is provided by app {provider[identity]} in "
                f"this host, {IN_HOST}"
            )
    for declaration in declarations:
        for contract in declaration.requires:
            if contract.identity not in provider and contract.identity not in listed:
                expected.add(
                    f"app {declaration.label}: requires: {contract.identity} has no "
                    "provider; no app of this host provides it and "
                    f"{REMOTE} has no entry for it"
                )
    _, refusals = resolve(declarations, listed)
    found = {r for r in refusals if "one identity has one contract class" not in r}
    found = {r for r in found if "one app provides a contract" not in r}
    assert found == expected
    # Without remote providers, Resolve is as it was.
    _, alone = resolve(declarations)
    _, none = resolve(declarations, frozenset())
    assert alone == none


@pytest.mark.spec("TRN-75", "APP-8", "APP-12")
def test_the_builder_reports_the_resolve_rows_together_and_sorted() -> None:
    value = remote(stock_v1="http://localhost", couriers_v3="http://localhost")
    value["billing.invoices.v1"] = [entry()]
    with pytest.raises(ImproperlyConfigured) as raised:
        configure([COURIERS, WEB], deployer(REMOTE_PROVIDERS=value))
    assert str(raised.value).splitlines() == [
        f"{REMOTE}: billing.invoices.v1 is required by no hosted app; remove it",
        f"{REMOTE}: couriers.v3 is provided by app couriers in this host, {IN_HOST}",
    ]
    # Following the advice: each line's fix admits the host.
    del value["billing.invoices.v1"], value["couriers.v3"]
    assert configure([COURIERS, WEB], deployer(REMOTE_PROVIDERS=value))
    # A requirement with neither provider names both places to look.
    with pytest.raises(ImproperlyConfigured) as raised:
        configure([WEB], deployer(REMOTE_PROVIDERS=value))
    assert str(raised.value) == (
        "app web: requires: couriers.v3 has no provider; no app of this host "
        f"provides it and {REMOTE} has no entry for it"
    )


@pytest.mark.spec("APP-12", "APP-16")
def test_a_refused_remote_entry_hides_no_other_problem_of_neolith() -> None:
    value = remote(stock_v1="http://Localhost", couriers_v3="http://localhost")
    with pytest.raises(ImproperlyConfigured) as raised:
        configure([WEB], deployer(REMOTE_PROVIDERS=value, REQUEST_DEADLINE=-1, X=1))
    assert str(raised.value).splitlines() == sorted(
        [
            'NEOLITH["REQUEST_DEADLINE"]: -1 is outside 0.001 to 9999999.999 seconds',
            f"NEOLITH: unknown key 'X'; {NEOLITH_KEYS}",
            f"{_URL}: is not an origin: {_http_transport.CASE}",
        ]
    )


_UNMET = [
    f"app web: requires: {identity} has no provider; no app of this host "
    f"provides it and {REMOTE} has no entry for it"
    for identity in ("couriers.v3", "stock.v1")
]


@pytest.mark.spec("APP-12", "APP-8")
@pytest.mark.parametrize(
    ("neolith", "problem"),
    [
        (
            {"REMOTE_PROVIDER": remote(stock_v1="http://localhost")},
            f"NEOLITH: unknown key 'REMOTE_PROVIDER'; {NEOLITH_KEYS}",
        ),
        (
            OrderedDict(
                REMOTE_PROVIDERS=remote(
                    stock_v1="http://localhost", couriers_v3="http://localhost"
                )
            ),
            "NEOLITH: expected a dict, got OrderedDict",
        ),
        ([], "NEOLITH: expected a dict, got list"),
        (
            {"REMOTE_PROVIDERS": []},
            f"{REMOTE}: expected a dict of contract identities, got list",
        ),
    ],
)
def test_resolve_reports_the_problem_of_neolith_its_refusal_may_come_from(
    neolith: object, problem: str
) -> None:
    given = {"MIDDLEWARE": [ROOT, PRINCIPAL], "NEOLITH": neolith}
    with pytest.raises(ImproperlyConfigured) as raised:
        configure([WEB], given)
    assert str(raised.value).splitlines() == sorted([*_UNMET, problem])


@pytest.mark.spec("APP-12")
def test_resolve_reports_settings_that_are_not_a_dict_and_nothing_false() -> None:
    value = remote(stock_v1="http://localhost", couriers_v3="http://localhost")
    given = OrderedDict(deployer(REMOTE_PROVIDERS=value))
    with pytest.raises(ImproperlyConfigured) as raised:
        configure([WEB], given)
    assert str(raised.value).splitlines() == sorted(
        [*_UNMET, "settings: expected a dict of setting names, got OrderedDict"]
    )


# W002 and the signing key (ADR 0014 Decision 3, ADR 0027 Decision 9).


def _warned(url: str) -> list[str]:
    return _checks.remote_warnings({"REMOTE_PROVIDERS": remote(stock_v1=url)})


@pytest.mark.parametrize(
    "url",
    [
        "http://foo.localhost",
        "http://foo.localhost:8000",
        "http://localhost.example",
        "http://inventory.internal",
        "http://10.0.0.1:8000",
        "http://128.0.0.1",
        "http://[2001:db8::1]",
        "http://[::2]",
    ],
)
def test_plain_http_to_a_host_that_is_not_loopback_is_warned(url: str) -> None:
    assert _warned(url) == [
        f'{REMOTE}["stock.v1"][0]: {url} is plain HTTP to a host that is not '
        "loopback; anyone who can watch the network can read and replay the "
        "calling app's token (spec 03)"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.2:9",
        "http://127.255.255.254:65535",
        "http://[::1]",
        "http://[::1]:8000",
        "https://inventory.internal",
        "https://foo.localhost",
        "https://10.0.0.1",
    ],
)
def test_loopback_and_https_are_not_warned(url: str) -> None:
    assert _warned(url) == []


def test_a_hosted_app_that_requires_a_remote_contract_needs_a_signing_key() -> None:
    web = declaration_of(WebConfig)
    assert web is not None
    neolith = {"REMOTE_PROVIDERS": remote(stock_v1="http://localhost")}
    assert _checks.unsigned_problems(neolith, [web]) == [
        'app web has no signing key in NEOLITH["SIGNING_SECRETS"], so it cannot '
        "call stock.v1 over HTTP"
    ]
    assert (
        _checks.unsigned_problems({**neolith, "SIGNING_SECRETS": SIGNING}, [web]) == []
    )
    assert _checks.unsigned_problems({}, [web]) == []
    # Judged on what can be read, whatever else NEOLITH holds.
    broken = {**neolith, "X": 1, "REQUEST_DEADLINE": -1}
    assert _checks.unsigned_problems(broken, [web]) == [
        'app web has no signing key in NEOLITH["SIGNING_SECRETS"], so it cannot '
        "call stock.v1 over HTTP"
    ]
    # A SIGNING_SECRETS that cannot be read names no app: E001 reports it.
    assert _checks.unsigned_problems({**neolith, "SIGNING_SECRETS": []}, [web]) == []
    malformed = {"web": {"KEY_ID": "?"}}
    assert (
        _checks.unsigned_problems({**neolith, "SIGNING_SECRETS": malformed}, [web])
        == []
    )


def test_w002_is_judged_on_each_entry_it_can_read() -> None:
    value = {
        "stock.v1": [entry("http://inventory.internal")],
        "couriers.v3": [],
        "other.v1": [entry("http://Inventory.internal")],
        "more.v1": [
            entry("http://Inventory.internal"),
            {"BACKEND": TRANSPORT},
            entry("https://more.internal"),
            entry("http://more.internal"),
            entry("http://more.internal"),
        ],
        "Stock.V1": [entry("http://inventory.internal")],
        "last.v1": (entry("http://inventory.internal"),),
    }
    tail = (
        "is plain HTTP to a host that is not loopback; anyone who can watch the "
        "network can read and replay the calling app's token (spec 03)"
    )
    # Each entry in the admitted form, by its index, whatever the list and the
    # rest of NEOLITH hold; a url listed again is refused, so only its first
    # entry warns.
    assert _checks.remote_warnings({"REMOTE_PROVIDERS": value, "X": 1}) == [
        f'{REMOTE}["more.v1"][3]: http://more.internal {tail}',
        f'{REMOTE}["stock.v1"][0]: http://inventory.internal {tail}',
    ]
    twice = {"stock.v1": [entry("http://Inventory.internal")] * 2}
    twice["stock.v1"].append(entry("http://inventory.internal"))
    assert _checks.remote_warnings({"REMOTE_PROVIDERS": twice}) == [
        f'{REMOTE}["stock.v1"][2]: http://inventory.internal {tail}',
    ]
    assert _checks.remote_urls({"REMOTE_PROVIDERS": twice}) == {}
    assert _checks.remote_warnings([]) == []
    assert _checks.remote_warnings({"REMOTE_PROVIDERS": []}) == []


# REMOTE_PROVIDERS cannot change after set-up (ADR 0014, Under override_settings).


def test_an_override_of_remote_providers_after_set_up_is_refused() -> None:
    value = remote(couriers_v3="http://localhost")
    with (
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(NEOLITH={**settings.NEOLITH, "REMOTE_PROVIDERS": value}),
    ):
        pass  # pragma: no cover
    assert str(raised.value) == f"{REMOTE}: cannot change after set-up"
    # A value outside the form is refused by its row, never shown.
    url = f"http://user:{CREDENTIAL}@localhost"
    with (
        pytest.raises(ImproperlyConfigured) as raised,
        override_settings(
            NEOLITH={**settings.NEOLITH, "REMOTE_PROVIDERS": remote(couriers_v3=url)}
        ),
    ):
        pass  # pragma: no cover
    assert CREDENTIAL not in str(raised.value)
    assert _http_transport.CREDENTIAL in str(raised.value)


# A host with remote providers, in a process of its own.


@pytest.fixture
def unreachable() -> Iterator[str]:
    """An origin on this machine that refuses every connection: a socket
    bound to a port and never listening, held while the test runs."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    try:
        yield f"http://127.0.0.1:{held.getsockname()[1]}"
    finally:
        held.close()


@pytest.mark.spec("APP-62", "TRN-66")
def test_a_host_whose_remote_provider_is_down_sets_up_boots_serves_and_fails_the_call(
    unreachable: str,
) -> None:
    neolith = {
        "REMOTE_PROVIDERS": remote(stock_v1=unreachable),
        "SIGNING_SECRETS": SIGNING,
    }
    out = in_fresh_process(
        """
        import socket

        reached = []
        _connect = socket.socket.connect
        _lookup = socket.getaddrinfo

        def connect(self, address):
            reached.append(("connect", address))
            return _connect(self, address)

        def getaddrinfo(*args, **kwargs):
            reached.append(("getaddrinfo", args[0]))
            return _lookup(*args, **kwargs)

        socket.socket.connect = connect
        socket.getaddrinfo = getaddrinfo

        import django

        django.setup()

        from django.test import Client

        import neolith
        from neolith._boot import boot
        from neolith._host import _table
        from tests.declaration_contracts import Sku

        boot()
        status = Client().get("/anything").status_code
        before = list(reached)
        host, _ = _table()
        transports = {
            identity: type(found._transport).__name__
            for identity, found in host.connectors.items()
        }
        served = list(host.endpoints.prefixes)
        with neolith.command_scope("web", timeout=10):
            try:
                neolith.connect("stock.v1").level(Sku(sku="abc"))
            except neolith.ContractError as error:
                kind = error.kind
        print(json.dumps([status, before, transports, served, kind, len(reached) > 0]))
        """,
        [COURIERS, WEB, "neolith"],
        {"NEOLITH": neolith, "ROOT_URLCONF": "tests.empty_urls"},
        timeout=120,
    )
    status, before, transports, served, kind, called = json.loads(out.splitlines()[-1])
    # Set-up, Boot and a request served, and nothing reached for the network.
    assert status == 404
    assert before == []
    # Under single: the app of the host where one provides, else the remote one.
    assert transports == {"couriers.v3": "InProcess", "stock.v1": "HTTPTransport"}
    # The host serves only what its apps provide, never a remote contract.
    assert served == ["/couriers/v3/"]
    assert kind == "unavailable"
    assert called


@pytest.mark.spec("TRN-66")
def test_remote_identities_on_one_origin_share_its_connections() -> None:
    url = "http://localhost:1"
    out = in_fresh_process(
        """
        import django

        django.setup()

        from neolith._host import _table

        host, _ = _table()
        stock = host.connectors["stock.v1"]._transport
        couriers = host.connectors["couriers.v3"]._transport
        kind = type(host.connectors).__name__
        print(json.dumps([stock.origin, stock._pool is couriers._pool, kind]))
        """,
        [WEB, "neolith"],
        {
            "NEOLITH": {
                "REMOTE_PROVIDERS": remote(stock_v1=url, couriers_v3=url),
                "SIGNING_SECRETS": SIGNING,
            }
        },
        timeout=120,
    )
    # The table holds them read-only, as it holds the in-process ones.
    assert json.loads(out.splitlines()[-1]) == [url, True, "mappingproxy"]


@pytest.mark.spec("APP-1", "TRN-75")
def test_set_up_stops_on_each_remote_row_with_every_refusal_and_no_credential() -> None:
    url = f"http://user:{CREDENTIAL}@localhost"
    out = in_fresh_process(
        """
        import logging

        import django
        from django.core.exceptions import ImproperlyConfigured

        class Keep(logging.Handler):
            lines = []

            def emit(self, record):
                Keep.lines.append(record.getMessage())

        logging.getLogger("neolith").addHandler(Keep())
        try:
            django.setup()
        except ImproperlyConfigured as error:
            print(json.dumps([str(error), Keep.lines]))
        """,
        [COURIERS, WEB, "neolith"],
        {
            "NEOLITH": {
                "REMOTE_PROVIDERS": {
                    **remote(
                        stock_v1="http://localhost", couriers_v3="http://localhost"
                    ),
                    "other.v1": [entry()],
                },
                "SIGNING_SECRETS": SIGNING,
            }
        },
        timeout=120,
    )
    refusal, logged = json.loads(out.splitlines()[-1])
    assert refusal.splitlines() == [
        f"{REMOTE}: couriers.v3 is provided by app couriers in this host, {IN_HOST}",
        f"{REMOTE}: other.v1 is required by no hosted app; remove it",
    ]
    assert any(refusal in line for line in logged)
    out = in_fresh_process(
        """
        import logging

        import django
        from django.core.exceptions import ImproperlyConfigured

        class Keep(logging.Handler):
            lines = []

            def emit(self, record):
                Keep.lines.append(record.getMessage())

        logging.getLogger("neolith").addHandler(Keep())
        try:
            django.setup()
        except ImproperlyConfigured as error:
            print(json.dumps([str(error), Keep.lines]))
        """,
        [WEB, "neolith"],
        {
            "NEOLITH": {
                "REMOTE_PROVIDERS": remote(
                    stock_v1=url, couriers_v3="http://localhost:9"
                )
            }
        },
        timeout=120,
    )
    refusal, logged = json.loads(out.splitlines()[-1])
    # Every refusal together: the url's, and the missing signing keys'.
    unsigned = 'app web has no signing key in NEOLITH["SIGNING_SECRETS"], so it'
    assert refusal.splitlines() == [
        "host: the settings are refused before the host serves:",
        f"(neolith.E001) {_URL}: is not an origin: {_http_transport.CREDENTIAL}",
        f"(neolith.E021) {unsigned} cannot call couriers.v3 over HTTP",
        f"(neolith.E021) {unsigned} cannot call stock.v1 over HTTP",
    ]
    assert any(refusal in line for line in logged)
    assert CREDENTIAL not in out


@pytest.mark.spec("APP-1", "APP-12")
def test_set_up_reports_a_misspelt_key_with_the_requirements_it_leaves_unmet() -> None:
    out = in_fresh_process(
        """
        import django
        from django.core.exceptions import ImproperlyConfigured

        try:
            django.setup()
        except ImproperlyConfigured as error:
            print(json.dumps(str(error)))
        """,
        [WEB, "neolith"],
        {
            "NEOLITH": {
                "REMOTE_PROVIDER": remote(
                    stock_v1="http://localhost", couriers_v3="http://localhost"
                ),
                "SIGNING_SECRETS": SIGNING,
            }
        },
        timeout=120,
    )
    assert json.loads(out.splitlines()[-1]).splitlines() == sorted(
        [*_UNMET, f"NEOLITH: unknown key 'REMOTE_PROVIDER'; {NEOLITH_KEYS}"]
    )


@pytest.mark.spec("APP-1")
@pytest.mark.parametrize("unknown", [False, True])
def test_set_up_stops_when_an_app_that_calls_a_remote_provider_cannot_sign(
    unknown: bool,
) -> None:
    value = remote(stock_v1="http://localhost", couriers_v3="http://localhost")
    if unknown:
        value["other.v1"] = [entry()]
    out = in_fresh_process(
        """
        import django
        from django.core.exceptions import ImproperlyConfigured

        try:
            django.setup()
        except ImproperlyConfigured as error:
            print(json.dumps(str(error)))
        """,
        [WEB, "neolith"],
        {"NEOLITH": {"REMOTE_PROVIDERS": value}},
        timeout=120,
    )
    unsigned = [
        f'app web has no signing key in NEOLITH["SIGNING_SECRETS"], so it cannot '
        f"call {identity} over HTTP"
        for identity in ("couriers.v3", "stock.v1")
    ]
    refusal = json.loads(out.splitlines()[-1]).splitlines()
    if unknown:
        # Resolve refuses, and the missing keys come with its refusal.
        assert refusal == sorted(
            [*unsigned, f"{REMOTE}: other.v1 is required by no hosted app; remove it"]
        )
    else:
        assert refusal == [
            "host: the settings are refused before the host serves:",
            *(f"(neolith.E021) {line}" for line in unsigned),
        ]


def test_set_up_logs_w002_and_an_override_leaves_the_set_up_transport() -> None:
    out = in_fresh_process(
        """
        import logging

        import django
        from django.conf import settings
        from django.core import checks
        from django.core.exceptions import ImproperlyConfigured
        from django.test import override_settings

        class Keep(logging.Handler):
            lines = []

            def emit(self, record):
                Keep.lines.append(record.getMessage())

        logging.getLogger("neolith").addHandler(Keep())
        django.setup()
        found = [
            (m.id, m.msg) for m in checks.run_checks(tags=["neolith"])
            if m.id == "neolith.W002"
        ]
        from neolith._host import _table

        before = _table()[0].connectors["stock.v1"]
        changed = {**settings.NEOLITH["REMOTE_PROVIDERS"]}
        changed["stock.v1"] = [
            {"BACKEND": "neolith.transports.HTTPTransport",
             "OPTIONS": {"url": "http://localhost:2"}}
        ]
        try:
            with override_settings(
                NEOLITH={**settings.NEOLITH, "REMOTE_PROVIDERS": changed}
            ):
                pass
        except ImproperlyConfigured as error:
            refused = str(error)
        after = _table()[0].connectors["stock.v1"]
        print(json.dumps([
            found, Keep.lines, refused, after is before, after._transport.origin
        ]))
        """,
        [WEB, "neolith"],
        {
            "NEOLITH": {
                "REMOTE_PROVIDERS": remote(
                    stock_v1="http://inventory.internal",
                    couriers_v3="http://localhost",
                ),
                "SIGNING_SECRETS": SIGNING,
            }
        },
        timeout=120,
    )
    found, logged, refused, same, origin = json.loads(out.splitlines()[-1])
    warning = (
        f'{REMOTE}["stock.v1"][0]: http://inventory.internal is plain HTTP to a '
        "host that is not loopback; anyone who can watch the network can read "
        "and replay the calling app's token (spec 03)"
    )
    assert found == [["neolith.W002", warning]]
    # Set-up logs it once; manage.py check prints it again.
    assert [line for line in logged if "W002" in line] == [f"(neolith.W002) {warning}"]
    assert refused == f"{REMOTE}: cannot change after set-up"
    assert same
    assert origin == "http://inventory.internal"


def test_a_set_up_that_django_drops_closes_its_connections() -> None:
    out = in_fresh_process(
        """
        import gc
        import socket
        import sys
        import threading

        server = socket.create_server(("127.0.0.1", 0))
        port = server.getsockname()[1]
        reply = b'HTTP/1.1 200 OK\\r\\nContent-Length: 13\\r\\n\\r\\n{"on_hand":1}'

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
                        connection.sendall(reply)

        def accept():
            while True:
                connection, _ = server.accept()
                threading.Thread(target=serve, args=(connection,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        url = f"http://127.0.0.1:{port}"
        settings.NEOLITH["REMOTE_PROVIDERS"] = {
            identity: [
                {"BACKEND": "neolith.transports.HTTPTransport", "OPTIONS": {"url": url}}
            ]
            for identity in ("stock.v1", "couriers.v3")
        }
        unraisable = []
        sys.unraisablehook = lambda found: unraisable.append(repr(found.exc_value))

        import django
        from django.test import override_settings

        django.setup()
        import neolith
        from tests.declaration_contracts import Sku

        def call():
            with neolith.command_scope("web", timeout=10):
                return neolith.connect("stock.v1").level(Sku(sku="a")).on_hand

        replies = [call()]
        for _ in range(3):
            with override_settings(INSTALLED_APPS=list(settings.INSTALLED_APPS)):
                replies.append(call())
        replies.append(call())
        gc.collect()
        print(json.dumps([replies, unraisable]))
        """,
        [WEB, "neolith"],
        {"NEOLITH": {"SIGNING_SECRETS": SIGNING}},
        timeout=120,
    )
    replies, unraisable = json.loads(out.splitlines()[-1])
    assert replies == [1, 1, 1, 1, 1]
    assert unraisable == []


@pytest.mark.parametrize(
    "url",
    [
        "http://xn--zz",
        "http://xn--a-ecp.example",
        "http://xn--mnchen-3ya.example",
        "https://xn--zca",
    ],
)
def test_an_a_label_is_an_ordinary_label_taken_as_written(url: str) -> None:
    assert _expected(url)
    assert built(remote(stock_v1=url, couriers_v3="http://localhost")) is None


# A key that is not a contract identity is named only by its own refusal,
# bounded: Resolve and W002 never read it.

_LONG_KEY = "x" * 300


def test_a_key_that_is_no_identity_is_read_by_nothing_but_its_refusal() -> None:
    neolith = {"REMOTE_PROVIDERS": {_LONG_KEY: [entry("http://inventory.internal")]}}
    assert _checks.remote_identities(neolith) == frozenset()
    assert _checks.remote_urls(neolith) == {}
    assert _checks.remote_warnings(neolith) == []
    (problem,) = _checks.remote_problems(neolith["REMOTE_PROVIDERS"])
    assert _LONG_KEY not in problem
    assert problem.startswith(f"{REMOTE}: key 'xxx")


@pytest.mark.spec("APP-1")
def test_set_up_names_a_long_key_that_is_no_identity_once_and_bounded() -> None:
    out = in_fresh_process(
        """
        import logging

        import django
        from django.core.exceptions import ImproperlyConfigured

        class Keep(logging.Handler):
            lines = []

            def emit(self, record):
                Keep.lines.append(record.getMessage())

        logging.getLogger("neolith").addHandler(Keep())
        try:
            django.setup()
        except ImproperlyConfigured as error:
            print(json.dumps([str(error), Keep.lines]))
        """,
        [COURIERS, "neolith"],
        {
            "NEOLITH": {
                "REMOTE_PROVIDERS": {_LONG_KEY: [entry("http://inventory.internal")]}
            }
        },
        timeout=120,
    )
    refusal, logged = json.loads(out.splitlines()[-1])
    assert refusal.splitlines() == [
        "host: the settings are refused before the host serves:",
        f"(neolith.E001) {REMOTE}: key {'x' * 80!r}...: expected a contract "
        "identity, such as <contract>.v1",
    ]
    assert _LONG_KEY not in out
    assert not any("W002" in line or "required by no" in line for line in logged)


def test_the_check_reports_e021_and_w002_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Read as manage.py check reads it, on the suite's host, whose web and
    # stock apps require stock.v1 and hold no signing key.
    remote_only = {"REMOTE_PROVIDERS": remote(stock_v1="http://inventory.internal")}
    monkeypatch.setattr(settings, "NEOLITH", remote_only)
    found = sorted((m.id, m.msg) for m in _checks.remote_check())
    unsigned = 'has no signing key in NEOLITH["SIGNING_SECRETS"], so it cannot call'
    assert found == [
        ("neolith.E021", f"app stock {unsigned} stock.v1 over HTTP"),
        ("neolith.E021", f"app web {unsigned} stock.v1 over HTTP"),
        ("neolith.W002", _warned("http://inventory.internal")[0]),
    ]
