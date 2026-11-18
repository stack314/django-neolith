"""Caller backends and the shared-secret token (spec 03, ADR 0027).

The token check is a plain function of the ``Authorization`` value, the
identity, the key table and *now*, so every rule is tested here with a
fixed clock; the backends, the search and the settings are tested as plain
functions with built inputs, and set-up in place. Serving a contract path
over HTTP is the provider endpoints' (roadmap step 5, task 6).
"""

import asyncio
import base64
import contextlib
import dataclasses
import hmac
import json
import logging
import pickle
import secrets
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.test import RequestFactory, override_settings
from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter
from hypothesis import given
from hypothesis import strategies as st

import neolith
from neolith import _caller, _checks, _token
from neolith._caller import (
    NO_AUTHORIZATION,
    NO_BACKEND,
    UNRECOGNIZED,
    Built,
    Callers,
    Failed,
    Refused,
    aidentify,
    build,
    identify,
    key_problems,
    shape_problems,
    signing_problems,
)
from neolith._token import Secret, check, decode, mint, secret_problem
from neolith.callers import CallerBackend, CredentialRefused, SharedSecretBackend
from neolith.principals import PrincipalBackend
from tests import host_apps

HERE = "tests.test_caller_backends"
SETTING = 'NEOLITH["CALLER_BACKENDS"]'
SIGNING = 'NEOLITH["SIGNING_SECRETS"]'
PRINCIPALS = 'NEOLITH["PRINCIPAL_BACKENDS"]'
SHARED = "neolith.callers.SharedSecretBackend"

# Spec 03's example: its secret in both spellings, its token and its times.
EXAMPLE_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
EXAMPLE_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
# Spec 03's published example token.
EXAMPLE_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6Im5lb2xpdGgrand0Iiwia2lkIjoib3JkZXJzLTEifQ."  # noqa: S105
    "eyJpc3MiOiJvcmRlcnMiLCJhdWQiOiJpbnZlbnRvcnkudjEiLCJpYXQiOjE3NjcyMjU2MDAs"
    "ImV4cCI6MTc2NzIyNTkwMH0.AhMk-cm_SInC6ek-6CLP8pN7ciQFHuGb-i2MSGu3efU"
)
IAT = 1767225600
EXP = 1767225900

# Fixed secrets that are not the example's, so tests are deterministic (T3).
ORDERS = bytes(range(100, 132))
ORDERS_OLD = bytes(range(140, 172))
BILLING = bytes(range(180, 212))
OTHER = bytes(range(220, 252))
ALL_SECRETS = (ORDERS, ORDERS_OLD, BILLING, OTHER)
IDENTITY = "inventory.v1"
NOW = IAT
B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keys(**apps: dict[str, bytes]) -> dict[str, dict[str, Secret]]:
    return {
        app: {kid: Secret(data, app, kid) for kid, data in held.items()}
        for app, held in apps.items()
    }


KEYS = keys(
    orders={"orders-1": ORDERS, "orders-0": ORDERS_OLD},
    billing={"billing-1": BILLING},
)


def header(**changed: object) -> dict[str, object]:
    return {"alg": "HS256", "typ": "neolith+jwt", "kid": "orders-1", **changed}


def claims(**changed: object) -> dict[str, object]:
    return {"iss": "orders", "aud": IDENTITY, "iat": IAT, "exp": IAT + 60, **changed}


def text(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def signed(
    head: object = None,
    body: object = None,
    secret: bytes = ORDERS,
    *,
    raw_head: str | None = None,
    raw_body: str | None = None,
) -> str:
    """A token over the header and claims, given as values or as raw JSON
    text, signed under ``secret``."""
    first = b64((raw_head if raw_head is not None else text(head or header())).encode())
    second = b64(
        (raw_body if raw_body is not None else text(body or claims())).encode()
    )
    signature = hmac.digest(secret, f"{first}.{second}".encode(), "sha256")
    return f"{first}.{second}.{b64(signature)}"


def bearer(token: str) -> str:
    return f"Bearer {token}"


VALID = ("orders", None)


# Spec 03's example (HTTP-8 to HTTP-15)

ALL_RULES = ("HTTP-8", "HTTP-9", "HTTP-10", "HTTP-11", "HTTP-12", "HTTP-13")


@pytest.mark.spec(*ALL_RULES, "HTTP-14", "HTTP-15")
def test_spec_03_s_example_token_verifies_at_iat_and_fails_at_exp_plus_60() -> None:
    held = keys(orders={"orders-1": bytes.fromhex(EXAMPLE_HEX)})
    for now in (IAT, IAT - 60, EXP + 59):
        assert check(bearer(EXAMPLE_TOKEN), IDENTITY, held, now) == VALID, now
    expired = check(bearer(EXAMPLE_TOKEN), IDENTITY, held, EXP + 60)
    assert expired == (None, _token.EXPIRED)


@pytest.mark.spec("HTTP-13")
def test_the_signer_writes_spec_03_s_example_byte_for_byte() -> None:
    secret = Secret(bytes.fromhex(EXAMPLE_HEX), "orders", "orders-1")
    made = mint(secret, IDENTITY, IAT, lifetime=EXP - IAT)
    assert made.authorization() == bearer(EXAMPLE_TOKEN)


@pytest.mark.spec("HTTP-15")
def test_a_minted_token_lives_60_seconds_and_verifies() -> None:
    made = mint(KEYS["orders"]["orders-1"], IDENTITY, NOW).authorization()
    body = json.loads(decode(made.split(".")[1]) or b"")
    assert body["exp"] - body["iat"] == 60
    assert check(made, IDENTITY, KEYS, NOW) == VALID
    assert check(made, IDENTITY, KEYS, NOW + 119) == VALID
    assert check(made, IDENTITY, KEYS, NOW + 120) == (None, _token.EXPIRED)


# Each rule, in the order of ADR 0027 Decision 2

_SIG = signed().rpartition(".")[2]
_SPACED = signed(raw_body=text(claims()) + " ").rpartition(".")[0]
_ISO = text(claims())

# (id, token, refusal, requirement)
_ROWS: list[tuple[str, str, str, str]] = [
    ("over-4096", signed(body=claims(aud="a" * 4096)), _token.PARTS, "HTTP-8"),
    ("two-parts", signed().rpartition(".")[0], _token.PARTS, "HTTP-8"),
    ("four-parts", signed() + ".e30", _token.PARTS, "HTTP-8"),
    ("empty-signature", signed().rpartition(".")[0] + ".", _token.PARTS, "HTTP-8"),
    ("padded", signed() + "=", _token.PARTS, "HTTP-8"),
    ("plus", signed().replace(".", ".+", 1), _token.PARTS, "HTTP-8"),
    ("unused-bits", signed()[:-1] + "B", _token.PARTS, "HTTP-8"),
    ("space-inside", signed() + " x", _token.PARTS, "HTTP-8"),
    ("repeated-field", signed() + "," + signed(), _token.PARTS, "HTTP-8"),
    ("claims-array", signed(raw_body="[1]"), _token.MEMBERS, "HTTP-8"),
    (
        "claims-twice",
        signed(raw_body='{"iss":"o","iss":"o"}'),
        _token.MEMBERS,
        "HTTP-8",
    ),
    ("claims-not-json", signed(raw_body="not json"), _token.MEMBERS, "HTTP-8"),
    ("extra-header", signed(head=header(x5u="u")), _token.MEMBERS, "HTTP-9"),
    ("double-in-header", signed(head=header(x=1.5)), _token.MEMBERS, "HTTP-9"),
    ("exponent-in-claims", signed(body=claims(x=1e300)), _token.MEMBERS, "HTTP-9"),
    (
        "no-kid",
        signed(head={"alg": "HS256", "typ": "neolith+jwt"}),
        _token.MEMBERS,
        "HTTP-9",
    ),
    ("extra-claim", signed(body=claims(jti="1")), _token.MEMBERS, "HTTP-9"),
    (
        "no-exp",
        signed(body={"iss": "orders", "aud": IDENTITY, "iat": IAT}),
        _token.MEMBERS,
        "HTTP-9",
    ),
    ("iat-string", signed(body=claims(iat=str(IAT))), _token.MEMBERS, "HTTP-9"),
    ("iat-true", signed(body=claims(iat=True)), _token.MEMBERS, "HTTP-9"),
    (
        "iat-fraction",
        signed(raw_body=_ISO.replace(str(IAT), f"{IAT}.0")),
        _token.MEMBERS,
        "HTTP-9",
    ),
    (
        "iat-exponent",
        signed(raw_body=_ISO.replace(str(IAT), "1.7672256e9")),
        _token.MEMBERS,
        "HTTP-9",
    ),
    ("exp-unsafe", signed(body=claims(exp=2**53)), _token.MEMBERS, "HTTP-9"),
    ("kid-int", signed(head=header(kid=1)), _token.MEMBERS, "HTTP-9"),
    ("aud-list", signed(body=claims(aud=[IDENTITY])), _token.MEMBERS, "HTTP-9"),
    ("iss-null", signed(body=claims(iss=None)), _token.MEMBERS, "HTTP-9"),
    (
        "surrogate",
        signed(raw_body=_ISO.replace('"orders"', '"\\ud800"')),
        _token.MEMBERS,
        "HTTP-9",
    ),
    ("alg-case", signed(head=header(alg="hs256")), _token.ALG, "HTTP-10"),
    ("alg-none", signed(head=header(alg="none")), _token.ALG, "HTTP-10"),
    ("alg-hs512", signed(head=header(alg="HS512")), _token.ALG, "HTTP-10"),
    ("unknown-issuer", signed(body=claims(iss="payments")), _token.ISSUER, "HTTP-12"),
    ("unknown-kid", signed(head=header(kid="orders-9")), _token.KID, "HTTP-12"),
    ("empty-kid", signed(head=header(kid="")), _token.KID, "HTTP-12"),
    (
        "other-app-s-kid",
        signed(head=header(kid="billing-1"), secret=BILLING),
        _token.KID,
        "HTTP-12",
    ),
    ("another-secret", signed(secret=OTHER), _token.SIGNATURE, "HTTP-13"),
    ("other-key-s-secret", signed(secret=ORDERS_OLD), _token.SIGNATURE, "HTTP-13"),
    (
        "short-signature",
        signed().rpartition(".")[0] + "." + b64(bytes(31)),
        _token.SIGNATURE,
        "HTTP-13",
    ),
    ("not-as-sent", f"{_SPACED}.{_SIG}", _token.SIGNATURE, "HTTP-13"),
    (
        "another-contract",
        signed(body=claims(aud="inventory.v2")),
        _token.AUDIENCE,
        "HTTP-14",
    ),
    ("no-lifetime", signed(body=claims(exp=IAT)), _token.LIFE, "HTTP-15"),
    ("exp-before-iat", signed(body=claims(exp=IAT - 1)), _token.LIFE, "HTTP-15"),
    ("over-300", signed(body=claims(exp=IAT + 301)), _token.LIFE, "HTTP-15"),
    (
        "future",
        signed(body=claims(iat=NOW + 61, exp=NOW + 120)),
        _token.FUTURE,
        "HTTP-15",
    ),
    (
        "expired",
        signed(body=claims(iat=NOW - 200, exp=NOW - 60)),
        _token.EXPIRED,
        "HTTP-15",
    ),
]
ROWS = [
    pytest.param(token, refusal, id=name, marks=pytest.mark.spec(rule))
    for name, token, refusal, rule in _ROWS
]


@pytest.mark.parametrize(("token", "refusal"), ROWS)
def test_each_rule_refuses_with_its_own_message(token: str, refusal: str) -> None:
    assert check(bearer(token), IDENTITY, KEYS, NOW) == (None, refusal)


_NEAREST: list[tuple[str, str]] = [
    ("typ-upper", signed(head=header(typ="NEOLITH+JWT"))),
    ("typ-media", signed(head=header(typ="application/neolith+jwt"))),
    ("typ-mixed", signed(head=header(typ="Application/Neolith+JWT"))),
    ("second-key", signed(head=header(kid="orders-0"), secret=ORDERS_OLD)),
    ("lifetime-300", signed(body=claims(exp=IAT + 300))),
    ("lifetime-1", signed(body=claims(exp=IAT + 1))),
    ("iat-at-skew", signed(body=claims(iat=NOW + 60, exp=NOW + 61))),
    ("exp-at-skew", signed(body=claims(iat=NOW - 119, exp=NOW - 59))),
    ("json-whitespace", signed(raw_body=" " + _ISO + "\n")),
    ("json-escape", signed(raw_body=_ISO.replace('"orders"', '"\\u006frders"'))),
    ("members-reordered", signed(raw_body=text(dict(reversed(claims().items()))))),
    ("large-audience", signed(body=claims(aud="a" * 2000))),
]


@pytest.mark.spec("HTTP-11", "HTTP-15")
@pytest.mark.parametrize("token", [pytest.param(t, id=n) for n, t in _NEAREST])
def test_the_nearest_admitted_tokens_verify(token: str) -> None:
    """Each rule's nearest legitimate shape is still accepted."""
    identity = json.loads(decode(token.split(".")[1]) or b"")["aud"]
    assert check(bearer(token), identity, KEYS, NOW) == VALID


def _sized(size: int) -> str:
    """A valid token of exactly ``size`` characters, padded with JSON
    whitespace."""
    for before in range(3):
        for after in range(size):
            head = " " * before + text(header())
            token = signed(raw_head=head, raw_body=_ISO + " " * after)
            if len(token) == size:
                return token
            if len(token) > size:
                break
    raise AssertionError(size)


@pytest.mark.spec("HTTP-8")
@pytest.mark.parametrize(
    ("size", "expected"), [(4096, VALID), (4097, (None, _token.PARTS))]
)
def test_the_size_bound_counts_the_token_after_the_scheme(
    size: int, expected: object
) -> None:
    """4096 characters admitted and 4097 refused, the scheme and the spaces
    after it apart."""
    assert check("Bearer    " + _sized(size), IDENTITY, KEYS, NOW) == expected


@pytest.mark.spec("HTTP-7", "HTTP-8")
def test_a_header_nested_as_deep_as_the_size_bound_allows_is_read() -> None:
    """The json module reads any nesting a token of 4096 bytes can hold, so
    the reader's own depth limit decides no token that could be valid."""
    depth = 3000
    head = text(header())[:-1] + ',"x":' + "[" * depth + "]" * depth + "}"
    token = signed(raw_head=head)
    assert len(token) > _token.LIMIT
    assert check(bearer(token), IDENTITY, KEYS, NOW) == (None, _token.PARTS)


# Recognition: the backend abstains on anything else (HTTP-7, HTTP-11)


def _first(part: str) -> str:
    return "Bearer " + part + "." + signed().split(".", 1)[1]


def _head(data: bytes) -> str:
    return _first(b64(data))


_ABSTAINS: list[tuple[str, str | None]] = [
    ("absent", None),
    ("empty", ""),
    ("basic", "Basic b3JkZXJzOnNlY3JldA=="),
    ("scheme-alone", "Bearer"),
    ("scheme-and-space", "Bearer "),
    ("another-scheme", "Token " + signed()),
    ("tab-after-scheme", "Bearer\t" + signed()),
    ("non-ascii-scheme", "Bearér " + signed()),
    ("array-header", _head(b"[]")),
    ("typ-jwt", _head(text(header(typ="JWT")).encode())),
    ("typ-null", _head(text(header(typ=None)).encode())),
    ("no-typ", _head(b'{"alg":"HS256","kid":"orders-1"}')),
    ("typ-twice", _head(b'{"typ":"neolith+jwt","typ":"neolith+jwt"}')),
    ("lone-surrogate", _head(b'{"typ":"neolith+jwt","kid":"\\ud800"}')),
    ("nan", _head(b'{"typ":"neolith+jwt","n":NaN}')),
    ("infinite", _head(b'{"typ":"neolith+jwt","n":1e400}')),
    ("unsafe-integer", _head(b'{"typ":"neolith+jwt","n":9007199254740992}')),
    ("long-integer", _head(b'{"typ":"neolith+jwt","n":' + b"9" * 5000 + b"}")),
    ("not-utf8", _head(b'{"typ":"neolith+jwt","a":"\xff"}')),
    ("non-ascii-typ", _head('{"typ":"NEOLİTH+JWT"}'.encode())),
    ("padded-first", _first(b64(b'{"typ":"neolith+jwt"}') + "=")),
    (
        "standard-alphabet",
        _first(b64(text(header(kid="o~>")).encode()).replace("-", "+")),
    ),
    ("not-base64", _first("[" * 5000)),
    ("deep-json", _head(b"[" * 100000)),
]


@pytest.mark.spec("HTTP-7", "HTTP-11")
@pytest.mark.parametrize("value", [pytest.param(v, id=n) for n, v in _ABSTAINS])
def test_the_backend_abstains_on_credentials_it_does_not_recognize(
    value: str | None,
) -> None:
    assert check(value, IDENTITY, KEYS, NOW) == (None, None)


@pytest.mark.spec("HTTP-7")
@pytest.mark.parametrize(
    "value",
    [
        "bearer " + signed(),
        "BEARER " + signed(),
        "  Bearer " + signed() + " \t",
        "Bearer     " + signed(),
    ],
)
def test_the_scheme_is_any_ascii_case_and_ows_is_not_part_of_the_value(
    value: str,
) -> None:
    assert check(value, IDENTITY, KEYS, NOW) == VALID


_MESSAGES = {
    _token.PARTS,
    _token.MEMBERS,
    _token.ALG,
    _token.ISSUER,
    _token.KID,
    _token.SIGNATURE,
    _token.AUDIENCE,
    _token.LIFE,
    _token.FUTURE,
    _token.EXPIRED,
}


@pytest.mark.spec("HTTP-7")
@given(st.text(max_size=300))
def test_any_text_gives_one_of_three_outcomes_and_never_raises(value: str) -> None:
    for given_value in (value, "Bearer " + value):
        app, message = check(given_value, IDENTITY, KEYS, NOW)
        assert app is None or app in KEYS
        assert message is None or message in _MESSAGES


# One mutation of a drawn token gives the row it breaks


@dataclasses.dataclass(frozen=True)
class Drawn:
    """A valid token's parts and a time when it is valid."""

    head: dict[str, object]
    body: dict[str, object]
    secret: bytes
    identity: str
    now: int

    def token(self, **changed: object) -> str:
        head = {k: changed.get(k, v) for k, v in self.head.items()}
        body = {k: changed.get(k, v) for k, v in self.body.items()}
        return signed(head, body, self.secret)


_IDENTITIES = st.from_regex(
    r"[a-z][a-z0-9_]{0,8}(\.[a-z][a-z0-9_]{0,8}){0,2}\.v[1-9][0-9]{0,2}",
    fullmatch=True,
)
_TYPS = ["neolith+jwt", "application/neolith+jwt", "Neolith+JWT"]


@st.composite
def _drawn(draw: st.DrawFn) -> Drawn:
    app = draw(st.sampled_from(sorted(KEYS)))
    kid = draw(st.sampled_from(sorted(KEYS[app])))
    iat = draw(st.integers(0, 2**40))
    exp = iat + draw(st.integers(1, 300))
    body = {"iss": app, "aud": draw(_IDENTITIES), "iat": iat, "exp": exp}
    order = draw(st.permutations(list(body)))
    return Drawn(
        {"alg": "HS256", "typ": draw(st.sampled_from(_TYPS)), "kid": kid},
        {k: body[k] for k in order},
        KEYS[app][kid]._bytes,
        str(body["aud"]),
        draw(st.integers(iat - 60, exp + 59)),
    )


@pytest.mark.spec(*ALL_RULES, "HTTP-14", "HTTP-15")
@given(_drawn())
def test_every_drawn_token_verifies(drawn: Drawn) -> None:
    found = check(bearer(drawn.token()), drawn.identity, KEYS, drawn.now)
    assert found == (drawn.body["iss"], None)


type _Mutated = tuple[str, int, str]
type _Draw = Callable[[st.SearchStrategy[Any]], Any]
type _Mutation = Callable[[_Draw, Drawn], _Mutated]


def _byte_in_signature(draw: _Draw, d: Drawn) -> _Mutated:
    first, second, signature = d.token().split(".")
    # Not the last character, whose unused bits would break the encoding.
    at = draw(st.integers(0, len(signature) - 2))
    new = draw(st.sampled_from([c for c in B64_ALPHABET if c != signature[at]]))
    changed = signature[:at] + new + signature[at + 1 :]
    return f"{first}.{second}.{changed}", d.now, d.identity


def _byte_in_audience(draw: _Draw, d: Drawn) -> _Mutated:
    first, _, signature = d.token().split(".")
    body = b64(text({**d.body, "aud": d.identity + "x"}).encode())
    return f"{first}.{body}.{signature}", d.now, d.identity


def _member_added(draw: _Draw, d: Drawn) -> _Mutated:
    name = draw(st.sampled_from(["jti", "nbf", "sub", "x5u", "cty"]))
    if draw(st.booleans()):
        return signed({**d.head, name: "x"}, d.body, d.secret), d.now, d.identity
    return signed(d.head, {**d.body, name: "x"}, d.secret), d.now, d.identity


def _member_removed(draw: _Draw, d: Drawn) -> _Mutated:
    name = draw(st.sampled_from(["alg", "kid", "iss", "aud", "iat", "exp"]))
    head = {k: v for k, v in d.head.items() if k != name}
    body = {k: v for k, v in d.body.items() if k != name}
    return signed(head, body, d.secret), d.now, d.identity


def _member_type(draw: _Draw, d: Drawn) -> _Mutated:
    name = draw(st.sampled_from(["alg", "kid", "iss", "aud", "iat", "exp"]))
    value = {**d.head, **d.body}[name]
    others: list[object] = (
        [str(value), [value], None, True, float(value) + 0.5]
        if type(value) is int
        else [1, [value], None, False, {"v": value}]
    )
    return d.token(**{name: draw(st.sampled_from(others))}), d.now, d.identity


def _lifetime(draw: _Draw, d: Drawn) -> _Mutated:
    iat: int = d.body["iat"]  # type: ignore[assignment]
    exp = draw(st.integers(iat - 1000, iat) | st.integers(iat + 301, iat + 10000))
    return d.token(exp=exp), iat, d.identity


def _future(draw: _Draw, d: Drawn) -> _Mutated:
    iat: int = d.body["iat"]  # type: ignore[assignment]
    return d.token(), iat - draw(st.integers(61, 10**6)), d.identity


def _expired(draw: _Draw, d: Drawn) -> _Mutated:
    exp: int = d.body["exp"]  # type: ignore[assignment]
    return d.token(), exp + draw(st.integers(60, 10**6)), d.identity


def _algorithm(draw: _Draw, d: Drawn) -> _Mutated:
    alg = draw(st.sampled_from(["none", "HS384", "HS512", "RS256", "hs256", "HS256 "]))
    return d.token(alg=alg), d.now, d.identity


def _issuer(draw: _Draw, d: Drawn) -> _Mutated:
    names = st.from_regex(r"[a-z]{1,10}", fullmatch=True)
    return d.token(iss=draw(names.filter(lambda a: a not in KEYS))), d.now, d.identity


def _key_id(draw: _Draw, d: Drawn) -> _Mutated:
    held = KEYS[str(d.body["iss"])]
    texts = st.text(st.characters(exclude_categories=["Cs"]), max_size=10)
    return d.token(kid=draw(texts.filter(lambda k: k not in held))), d.now, d.identity


def _audience(draw: _Draw, d: Drawn) -> _Mutated:
    return d.token(), d.now, d.identity + "x"


MUTATIONS: dict[str, tuple[str, _Mutation]] = {
    "byte-in-signature": (_token.SIGNATURE, _byte_in_signature),
    "byte-in-audience": (_token.SIGNATURE, _byte_in_audience),
    "member-added": (_token.MEMBERS, _member_added),
    "member-removed": (_token.MEMBERS, _member_removed),
    "member-type": (_token.MEMBERS, _member_type),
    "lifetime": (_token.LIFE, _lifetime),
    "future": (_token.FUTURE, _future),
    "expired": (_token.EXPIRED, _expired),
    "alg": (_token.ALG, _algorithm),
    "issuer": (_token.ISSUER, _issuer),
    "key-id": (_token.KID, _key_id),
    "audience": (_token.AUDIENCE, _audience),
}


@pytest.mark.spec(*ALL_RULES, "HTTP-14", "HTTP-15", "HTTP-16")
@pytest.mark.parametrize("kind", sorted(MUTATIONS))
@given(drawn=_drawn(), data=st.data())
def test_one_mutation_gives_the_row_it_breaks(
    kind: str, drawn: Drawn, data: st.DataObject
) -> None:
    row, mutate = MUTATIONS[kind]
    token, now, identity = mutate(data.draw, drawn)
    assert check(bearer(token), identity, KEYS, now) == (None, row)


# The secret's one form (ADR 0027 Decision 3)


@given(st.binary(min_size=32, max_size=32))
def test_every_secret_of_32_random_bytes_is_admitted(data: bytes) -> None:
    assert secret_problem(b64(data)) is None


def test_a_generated_secret_is_admitted() -> None:
    assert secret_problem(secrets.token_urlsafe(32)) is None


@given(st.integers(16, 64), st.data())
def test_every_hex_secret_is_refused_without_showing_it(
    size: int, data: st.DataObject
) -> None:
    hexed = data.draw(st.binary(min_size=size, max_size=size)).hex()
    problem = secret_problem(hexed)
    assert problem is not None
    assert hexed not in problem
    if size == 32 and hexed != EXAMPLE_HEX:
        assert problem.startswith("looks like hexadecimal text")


class Text(str):
    """A str subclass, which is not exactly a str."""

    __slots__ = ()


_REFUSED_SECRETS: list[tuple[object, str]] = [
    (EXAMPLE_HEX, "is spec 03's example secret"),
    (EXAMPLE_HEX.upper(), "is spec 03's example secret"),
    (EXAMPLE_B64, "is spec 03's example secret"),
    (b64(ORDERS) + "=", "is padded"),
    (b64(ORDERS)[:-1], "is 42 characters long"),
    (b64(ORDERS + b"\x00"), "is 44 characters long"),
    (b64(ORDERS)[:-2] + "+/", "holds a character outside base64url"),
    (
        b64(ORDERS)[:20] + "\n" + b64(ORDERS)[20:-1],
        "holds a character outside base64url",
    ),
    (b64(ORDERS)[:-1] + "B", "does not end with zero unused bits"),
    (ORDERS, "is a bytes, not exactly a str"),
    (None, "is a NoneType, not exactly a str"),
    (Text(b64(ORDERS)), "is a Text, not exactly a str"),
]


@pytest.mark.parametrize(("value", "start"), _REFUSED_SECRETS)
def test_each_refused_secret_is_named_without_showing_it(
    value: object, start: str
) -> None:
    problem = secret_problem(value)
    assert problem is not None
    assert problem.startswith(start)
    assert "secrets.token_urlsafe(32)" in problem
    if isinstance(value, str):
        assert str(value) not in problem


# The backend's options (ADR 0027 Decision 3; 0023 Decision 10's constructor row)


def _refusal(**options: object) -> str:
    with pytest.raises(ImproperlyConfigured) as raised:
        SharedSecretBackend(**options)
    return str(raised.value)


_A, _B, _O = b64(ORDERS), b64(BILLING), b64(OTHER)
_OPTION_ROWS: list[tuple[dict[str, object], str]] = [
    ({}, "the option 'secrets' is required"),
    ({"secrets": {}, "other": 1}, "the option 'other' is not taken"),
    ({"secrets": []}, "expected a dict of app names, got a list"),
    ({"secrets": {}}, "is empty"),
    ({"secrets": {"Orders": {"k": _A}}}, "a key is not an app's name"),
    ({"secrets": {"orders": []}}, "app orders: expected a dict of key ids"),
    ({"secrets": {"orders": {}}}, "app orders: has no key"),
    ({"secrets": {"orders": {"a": _A, "b": _O, "c": _B}}}, "app orders: has 3 keys"),
    ({"secrets": {"orders": {"a b": _A}}}, "app orders: a key id is not 1 to 64"),
    ({"secrets": {"orders": {"k" * 65: _A}}}, "app orders: a key id is not 1 to 64"),
    (
        {"secrets": {"orders": {"k": EXAMPLE_B64}}},
        "key k: the secret is spec 03's example",
    ),
    (
        {"secrets": {"orders": {"k": EXAMPLE_HEX}}},
        "key k: the secret is spec 03's example",
    ),
    (
        {"secrets": {"orders": {"k": ORDERS.hex()}}},
        "key k: the secret looks like hexadecimal",
    ),
    (
        {"secrets": {"orders": {"a": _A, "b": _A}}},
        "under app orders key a and app orders key b",
    ),
    (
        {"secrets": {"orders": {"a": _A}, "billing": {"b": _A}}},
        "under app orders key a and app billing key b",
    ),
]


@pytest.mark.parametrize(("options", "said"), _OPTION_ROWS)
def test_the_shared_secret_backend_refuses_its_options(
    options: dict[str, object], said: str
) -> None:
    message = _refusal(**options)
    assert said in message
    assert _leaks(message, []) == []


def test_every_problem_of_the_options_is_in_one_message() -> None:
    message = _refusal(
        secrets={"orders": {}, "billing": {"b": EXAMPLE_B64}, "Bad": {}}, other=1
    )
    for said in ("'other'", "app orders: has no key", "billing: key b", "a key is not"):
        assert said in message


@pytest.mark.parametrize(
    "accepted",
    [
        {"orders": {"orders-1": _A}},
        {"orders": {"orders-1": _A, "orders-0": b64(ORDERS_OLD)}},
        {"orders": {"a" * 64: _A}, "billing_2": {"B.-_9": _B}},
    ],
)
def test_the_nearest_admitted_options_build(accepted: dict[str, Any]) -> None:
    backend = SharedSecretBackend(secrets=accepted)
    assert {app: set(held) for app, held in backend._keys.items()} == {
        app: set(held) for app, held in accepted.items()
    }


def test_a_key_id_long_enough_to_be_a_secret_is_never_shown() -> None:
    """A secret pasted as a key id, and the key id pasted as the secret."""
    message = _refusal(secrets={"orders": {_A: "orders-1"}})
    assert "key (a key id of 43 characters, not shown): the secret is 8" in message
    assert _A not in message
    held = Secret(OTHER, "orders", _A)
    assert repr(held) == "<secret orders/(a key id of 43 characters, not shown)>"
    crossed = key_problems(
        {"orders": Secret(ORDERS, "orders", _A)},
        SharedSecretBackend(secrets={"orders": {_A: _O}}),
    )
    assert len(crossed) == 1
    assert _leaks(crossed[0], []) == []
    assert _token.shown_key_id("k" * 31) == "k" * 31


# Signing keys (NEOLITH["SIGNING_SECRETS"], the builder's rows)


LABELS = ["orders", "billing"]
_SIGNING_ROWS: list[tuple[object, str]] = [
    ([], f"{SIGNING}: expected a dict of hosted apps' labels, got list"),
    ({}, f"{SIGNING}: is empty"),
    ({"Orders": {}}, f"{SIGNING}: a key is not a hosted app's label; it is not shown"),
    (
        {"stock": {"KEY_ID": "k", "SECRET": _A}},
        f"{SIGNING}['stock']: stock is not a hosted app",
    ),
    (
        {"orders": []},
        "['orders']: expected a dict with exactly the keys KEY_ID and SECRET, got list",
    ),
    (
        {"orders": {"KEY_ID": "k"}},
        f"{SIGNING}['orders']: expected exactly the keys KEY_ID and SECRET",
    ),
    ({"orders": {"KEY_ID": "k", "SECRET": _A, "X": 1}}, "expected exactly the keys"),
    (
        {"orders": {"KEY_ID": "k k", "SECRET": _A}},
        f"{SIGNING}['orders']: KEY_ID is not 1 to 64",
    ),
    ({"orders": {"KEY_ID": _A + "!", "SECRET": "x"}}, "KEY_ID is not 1 to 64"),
    (
        {"orders": {"KEY_ID": "k", "SECRET": ORDERS.hex()}},
        f"{SIGNING}['orders']: SECRET looks like hexadecimal",
    ),
    (
        {"orders": {"KEY_ID": "k", "SECRET": EXAMPLE_B64}},
        "SECRET is spec 03's example secret",
    ),
    ({"orders": {"KEY_ID": "k", "SECRET": _A.encode()}}, "SECRET is a bytes"),
]


@pytest.mark.parametrize(("value", "said"), _SIGNING_ROWS)
def test_signing_secrets_outside_the_form_are_refused_without_the_secret(
    value: object, said: str
) -> None:
    problems = signing_problems(value, LABELS)
    assert any(said in p for p in problems), problems
    assert _leaks("\n".join(problems), []) == []


def test_the_nearest_admitted_signing_secrets_pass() -> None:
    value = {
        "orders": {"KEY_ID": "orders-1", "SECRET": _A},
        "billing": {"KEY_ID": "b" * 64, "SECRET": _B},
    }
    assert signing_problems(value, LABELS) == []


def _signers(**held: tuple[str, bytes]) -> dict[str, Secret]:
    return {app: Secret(data, app, kid) for app, (kid, data) in held.items()}


_KEY_ROWS: list[tuple[dict[str, tuple[str, bytes]], dict[str, Any] | None, str]] = [
    (
        {"orders": ("orders-1", ORDERS)},
        {"orders": {"orders-1": _O}},
        "app orders key orders-1 has another secret",
    ),
    (
        {"orders": ("orders-1", ORDERS)},
        {"billing": {"billing-1": _A}},
        "its secret is also under app billing key billing-1",
    ),
    (
        {"orders": ("orders-2", ORDERS)},
        {"orders": {"orders-1": _A}},
        "its secret is also under app orders key orders-1",
    ),
    (
        {"orders": ("orders-1", ORDERS), "billing": ("billing-1", ORDERS)},
        None,
        "apps orders and billing sign with one secret",
    ),
]


@pytest.mark.parametrize(("signing", "accepted", "said"), _KEY_ROWS)
def test_one_secret_one_key_across_the_host(
    signing: dict[str, tuple[str, bytes]],
    accepted: dict[str, Any] | None,
    said: str,
) -> None:
    backend = None if accepted is None else SharedSecretBackend(secrets=accepted)
    problems = key_problems(_signers(**signing), backend)
    assert len(problems) == 1, problems
    assert said in problems[0]


def test_one_host_may_sign_as_an_app_and_accept_it_under_the_same_key() -> None:
    backend = SharedSecretBackend(secrets={"orders": {"orders-1": _A}})
    assert key_problems(_signers(orders=("orders-1", ORDERS)), backend) == []


# Building the backends (0023 Decision 10's rows, CallerBackend as the base)


class Counting(CallerBackend):
    """Answers what it was built with, and counts its calls."""

    def __init__(self, *, answer: object = None) -> None:
        self.answer = answer
        self.calls = 0

    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer  # type: ignore[return-value]


class Abstract(CallerBackend):
    pass


class AsyncAuthenticate(CallerBackend):
    async def authenticate(self, request: HttpRequest, contract: str) -> str | None:  # type: ignore[override]
        return None


class SyncAauthenticate(CallerBackend):
    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        return None

    def aauthenticate(self, request: HttpRequest, contract: str) -> str | None:  # type: ignore[override]
        return None


class Raises(CallerBackend):
    def __init__(self, **options: object) -> None:
        raise ValueError("cannot build")

    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        return None


class Unrelated:
    def authenticate(self, request: HttpRequest, contract: str) -> str | None:
        return None


def entry(backend: str, **options: object) -> dict[str, Any]:
    return {"BACKEND": backend, "OPTIONS": options}


_BASE = "neolith.callers.CallerBackend"
_BUILD_ROWS: list[tuple[str, str]] = [
    (f"{HERE}.Missing", "does not import"),
    (
        f"{HERE}.HERE",
        f"is a str, not a class; a caller backend is a subclass of {_BASE}",
    ),
    (_BASE, f"is not a subclass of {_BASE}"),
    (f"{HERE}.Unrelated", f"is not a subclass of {_BASE}"),
    (f"{HERE}.Abstract", "is abstract; it does not define authenticate"),
    (
        f"{HERE}.AsyncAuthenticate",
        "authenticate is a coroutine function; it is a plain one",
    ),
    (f"{HERE}.SyncAauthenticate", "aauthenticate is not a coroutine function"),
    (
        f"{HERE}.Raises",
        "the constructor raised, with the options none: ValueError: cannot build",
    ),
    (
        SHARED,
        "with the options none: ImproperlyConfigured: the option 'secrets' is required",
    ),
]


@pytest.mark.parametrize(("path", "said"), _BUILD_ROWS)
def test_a_backend_set_up_cannot_build_is_refused_naming_its_entry(
    path: str, said: str
) -> None:
    built_now, problems = build([entry(path)])
    assert built_now == ()
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{SETTING}[0] ({path}): ")
    assert said in problems[0]


def test_every_entry_is_built_or_refused_and_none_stops_another() -> None:
    built_now, problems = build(
        [entry(f"{HERE}.Raises"), entry(f"{HERE}.Counting"), entry(f"{HERE}.Abstract")]
    )
    assert [(b.position, b.path) for b in built_now] == [(1, f"{HERE}.Counting")]
    assert [p.split(":")[0] for p in problems] == [
        f"{SETTING}[0] ({HERE}.Raises)",
        f"{SETTING}[2] ({HERE}.Abstract)",
    ]


def test_the_setting_has_the_principal_backends_entry_form() -> None:
    assert shape_problems([]) == [
        f"{SETTING}: is empty; leave the key out for no backend"
    ]
    assert shape_problems([{"BACKEND": "x"}]) == [
        f"{SETTING}[0]: expected exactly the keys BACKEND and OPTIONS, got 'BACKEND'"
    ]
    assert shape_problems([entry("nodots")]) == [
        f"{SETTING}[0]: BACKEND 'nodots' is not a dotted path, such as '{SHARED}'"
    ]
    assert shape_problems([entry(SHARED, secrets={})]) == []


# The search: the first backend that does not abstain decides (HTTP-7, HTTP-16)


def built(*backends: CallerBackend) -> tuple[Built, ...]:
    return tuple(
        Built(i, f"{HERE}.{type(b).__qualname__}", b) for i, b in enumerate(backends)
    )


def shared() -> SharedSecretBackend:
    return SharedSecretBackend(
        secrets={"orders": {"orders-1": _A, "orders-0": b64(ORDERS_OLD)}}
    )


def request(authorization: str | None = None) -> HttpRequest:
    made = RequestFactory().post("/inventory/v1/count")
    if authorization is not None:
        made.META["HTTP_AUTHORIZATION"] = authorization
    return made


def fresh() -> str:
    """An ``Authorization`` value with a token valid now."""
    return mint(KEYS["orders"]["orders-1"], IDENTITY, _now()).authorization()


def _now() -> int:
    return int(time.time())


def both(backends: tuple[Built, ...], given: HttpRequest) -> list[object]:
    """What identify and aidentify each give: the app, or the error."""
    found: list[object] = []
    runs: tuple[Callable[[], str], ...] = (
        lambda: identify(backends, given, IDENTITY),
        lambda: asyncio.run(aidentify(backends, given, IDENTITY)),
    )
    for run in runs:
        try:
            found.append(run())
        except (Refused, Failed) as error:
            found.append((type(error), str(error)))
    return found


@pytest.mark.spec("HTTP-7")
def test_a_valid_token_names_its_issuer() -> None:
    assert both(built(shared()), request(fresh())) == ["orders", "orders"]


@pytest.mark.spec("HTTP-7")
def test_an_abstaining_backend_leaves_the_call_to_the_next() -> None:
    later = Counting(answer="billing")
    assert both(built(shared(), later), request("Basic eA==")) == ["billing"] * 2
    assert later.calls == 2


@pytest.mark.spec("HTTP-16")
@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(bearer(signed(secret=OTHER)), id="signature"),
        pytest.param(bearer(signed(body=claims(iat=1, exp=2))), id="expired"),
        pytest.param(bearer(signed(head=header(alg="none"))), id="alg"),
        pytest.param(bearer(signed() + ".x"), id="parts"),
        pytest.param(bearer(signed(head=header(x=1.5))), id="double-in-header"),
    ],
)
def test_a_recognized_invalid_token_fails_with_no_fallback(authorization: str) -> None:
    later = Counting(answer="billing")
    found = both(built(shared(), later), request(authorization))
    assert later.calls == 0
    assert found[0] == found[1]
    assert type(found[0]) is tuple
    assert found[0][0] is Refused


_NOBODY: list[tuple[tuple[CallerBackend, ...], str | None, str]] = [
    ((), fresh(), NO_BACKEND),
    ((Counting(),), None, NO_AUTHORIZATION),
    ((Counting(),), "", UNRECOGNIZED),
    ((Counting(), Counting()), "Basic eA==", UNRECOGNIZED),
    ((Counting(answer=CredentialRefused("revoked")),), "x", "revoked"),
]


@pytest.mark.parametrize(("backends", "authorization", "message"), _NOBODY)
def test_each_way_to_name_no_app_has_its_message(
    backends: tuple[CallerBackend, ...], authorization: str | None, message: str
) -> None:
    found = both(built(*backends), request(authorization))
    assert found == [(Refused, message)] * 2


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (RuntimeError("down"), "authenticate raised RuntimeError: down"),
        (5, "authenticate returned a int, not an app's name or None"),
        ("Orders", "authenticate returned a str, not an app's name or None"),
        ("", "authenticate returned a str, not an app's name or None"),
        ("orders\n", "authenticate returned a str, not an app's name or None"),
    ],
)
def test_a_backend_that_fails_otherwise_fails_the_call(
    answer: object, said: str
) -> None:
    found = both(built(Counting(answer=answer)), request())
    where = f"{SETTING}[0] ({HERE}.Counting)"
    assert found == [(Failed, f"{where}: {said}"), (Failed, f"{where}: a{said}")]


def test_a_coroutine_returned_by_authenticate_is_closed_and_refused() -> None:
    async def coroutine() -> str:
        return "orders"

    with pytest.raises(Failed, match="returned a coroutine"):
        identify(built(Counting(answer=coroutine())), request(), IDENTITY)


def test_the_default_aauthenticate_runs_authenticate_on_another_thread() -> None:
    seen: list[int] = []

    class Thread(CallerBackend):
        def authenticate(self, request: HttpRequest, contract: str) -> str | None:
            seen.append(threading.get_ident())
            return "orders"

    found = asyncio.run(aidentify(built(Thread()), request(), IDENTITY))
    assert found == "orders"
    assert seen
    assert seen[0] != threading.get_ident()


def test_credential_refused_holds_fixed_text() -> None:
    assert neolith.CredentialRefused is CredentialRefused
    assert CredentialRefused("no").message == "no"
    with pytest.raises(TypeError, match="fixed text"):
        CredentialRefused(b"no")  # type: ignore[arg-type]


# The signer (ADR 0027 Decision 9's tokens)


def test_an_app_with_a_signing_key_mints_a_token_the_backend_accepts() -> None:
    callers = Callers((), (KEYS["orders"]["orders-1"],))
    token = callers.mint("orders", IDENTITY)
    assert token is not None
    assert repr(token) == "<token orders to inventory.v1>"
    named = identify(built(shared()), request(token.authorization()), IDENTITY)
    assert named == "orders"
    assert callers.mint("billing", IDENTITY) is None
    assert _caller.no_signing_key("billing", IDENTITY) == (
        f"app billing has no signing key in {SIGNING}, so it cannot call "
        "inventory.v1 over HTTP"
    )


# No secret or token in any output (ADR 0027 Decision 3)


def _leaks(output: str, tokens: list[str]) -> list[str]:
    """Each secret, token, token part or signing input found in ``output``,
    in any spelling: base64url, hex, or as Python writes the bytes."""
    pieces = [b64(s) for s in ALL_SECRETS] + [s.hex() for s in ALL_SECRETS]
    pieces += [repr(s)[2:-1] for s in ALL_SECRETS]
    for token in tokens:
        parts = token.split(".")
        pieces += [token, *parts, ".".join(parts[:2])]
        pieces += [repr(found)[2:-1] for p in parts if (found := decode(p))]
    return [p for p in pieces if len(p) >= 8 and p in output]


@contextlib.contextmanager
def _captured() -> Iterator[list[str]]:
    """Every log record, at every level, as formatted with its traceback."""
    found: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            found.append(logging.Formatter().format(record))

    root = logging.getLogger()
    handler, level = Handler(logging.DEBUG), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield found
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def _described(error: BaseException, given: HttpRequest | None) -> str:
    """An error as every output shows it: its text, the formatted chain,
    each frame's locals, and Django's technical 500 page as HTML and text."""
    shown = [str(error), repr(error), "".join(traceback.format_exception(error))]
    seen: BaseException | None = error
    while seen is not None:
        tb = seen.__traceback__
        while tb is not None:
            shown += [repr(v) for v in tb.tb_frame.f_locals.values()]
            tb = tb.tb_next
        seen = seen.__cause__ or seen.__context__
    with override_settings(DEBUG=True):
        reporter = ExceptionReporter(given, type(error), error, error.__traceback__)
        shown += [reporter.get_traceback_html(), reporter.get_traceback_text()]
    return "\n".join(shown)


def _failing_digest(key: bytes, message: bytes, digest: str) -> bytes:
    raise ValueError(f"failed on {key!r} over {message!r}")


def _caught(run: Callable[[], object]) -> BaseException:
    """What ``run`` raises, caught in a frame that holds no token, as a
    deployer's view or the host's endpoint would catch it."""
    try:
        run()
    except Exception as error:
        return error
    raise AssertionError("nothing was raised")


@pytest.mark.parametrize("token", [row[1] for row in _ROWS])
def test_no_refusal_shows_the_token(token: str) -> None:
    given = request(bearer(token))
    backends = built(shared())
    with _captured() as logged:
        error = _caught(lambda: identify(backends, given, IDENTITY))
    assert type(error) is Refused
    output = _described(error, given) + "\n".join(logged)
    assert _leaks(output, [token]) == []


def test_a_failure_inside_the_hmac_in_the_backend_shows_no_secret_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = fresh()
    given = request(authorization)
    backends = built(shared())
    monkeypatch.setattr(hmac, "digest", _failing_digest)
    with _captured() as logged:
        error = _caught(lambda: identify(backends, given, IDENTITY))
    assert type(error) is Failed
    cause = error.__cause__
    assert type(cause) is _token.Failure
    assert cause.__context__ is None
    assert cause.__cause__ is None
    output = _described(error, given) + "\n".join(logged)
    assert _leaks(output, [authorization.removeprefix("Bearer ")]) == []


def test_a_failure_inside_the_hmac_in_the_signer_shows_no_secret_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = KEYS["orders"]["orders-1"]
    callers = Callers((), (secret,))
    expected = mint(secret, IDENTITY, NOW).authorization().removeprefix("Bearer ")
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    monkeypatch.setattr(hmac, "digest", _failing_digest)
    error = _caught(lambda: callers.mint("orders", IDENTITY))
    assert type(error) is _token.Failure
    assert error.__context__ is None
    assert error.__cause__ is None
    assert _leaks(_described(error, request()), [expected]) == []


def test_secrets_and_tokens_show_by_name_and_are_not_pickled() -> None:
    secret = KEYS["orders"]["orders-1"]
    token = mint(secret, IDENTITY, NOW)
    callers = Callers(built(shared()), (secret,))
    shown = [repr(secret), repr(token), repr(callers), repr(vars(shared()))]
    assert repr(secret) == "<secret orders/orders-1>"
    text_of = token.authorization().removeprefix("Bearer ")
    assert _leaks("\n".join(shown), [text_of]) == []
    for value in (secret, token):
        with pytest.raises(TypeError, match="not pickled"):
            pickle.dumps(value)


def test_django_s_debug_filter_hides_both_keys_of_secrets() -> None:
    value = {
        "CALLER_BACKENDS": [entry(SHARED, secrets={"orders": {"orders-1": _A}})],
        "SIGNING_SECRETS": {"orders": {"KEY_ID": "orders-1", "SECRET": _A}},
    }
    cleansed = SafeExceptionReporterFilter().cleanse_setting("NEOLITH", value)
    assert _leaks(repr(cleansed), []) == []
    assert cleansed["CALLER_BACKENDS"][0]["BACKEND"] == SHARED


def test_no_settings_refusal_shows_a_secret() -> None:
    """Refusals of the builder, the constructor and the check across keys,
    each with a secret near it."""
    problems = signing_problems(
        {
            "orders": {"KEY_ID": "orders-1", "SECRET": _A[:-1]},
            _B: {"KEY_ID": "k", "SECRET": _B + "="},
            "billing": {"KEY_ID": "k", "SECRET": OTHER.hex()},
        },
        LABELS,
    )
    _, refused = build(
        [entry(SHARED, secrets={"orders": {_O + "!": _O, "k": b64(ORDERS_OLD)[:-1]}})]
    )
    crossed = key_problems(
        _signers(orders=("orders-1", ORDERS), billing=("billing-1", BILLING)),
        SharedSecretBackend(secrets={"orders": {"orders-1": _O}, "billing": {"b": _B}}),
    )
    assert (len(problems), len(refused), len(crossed)) == (3, 1, 2)
    assert _leaks("\n".join([*problems, *refused, *crossed]), []) == []


# Set-up: neolith.E021, neolith.W004, neolith.W005 and neolith.W006


class DeclaresPrincipal(PrincipalBackend):
    request_inputs = ("x-user", "neolith-principal")

    def resolve(self, request: HttpRequest) -> None:
        return None


@pytest.fixture
def registered() -> None:
    _checks.register()


def _in_place(**changed: object) -> Any:  # noqa: ANN401
    from tests.test_host_checks import _in_place as in_place

    return in_place(**changed)


def _set_up(neolith: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Set the host up in place with ``neolith``; return the refusal, if
    any, and the warnings set-up logged."""
    logger = logging.getLogger("neolith.host")
    records: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Handler()
    logger.addHandler(handler)
    refusal = None
    try:
        with _in_place(NEOLITH=neolith):
            try:
                with override_settings(INSTALLED_APPS=host_apps.INSTALLED):
                    pass
            except ImproperlyConfigured as error:
                refusal = str(error)
    finally:
        logger.removeHandler(handler)
    warned = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    return refusal, warned


_ACCEPTING = entry(SHARED, secrets={"stock": {"stock-1": _A}})


def test_set_up_reports_what_it_cannot_build_and_every_key_conflict(
    registered: None,
) -> None:
    refusal, _ = _set_up(
        {
            **host_apps.SERVED,
            "CALLER_BACKENDS": [
                _ACCEPTING,
                entry(SHARED, secrets={"web": {"w": _B}}),
                entry(f"{HERE}.Abstract"),
            ],
            "SIGNING_SECRETS": {"stock": {"KEY_ID": "stock-1", "SECRET": _O}},
        }
    )
    assert refusal is not None
    lines = [line for line in refusal.splitlines() if "(neolith.E021)" in line]
    assert lines == sorted(
        [
            f"(neolith.E021) {SETTING}[2] ({HERE}.Abstract): is abstract; it does "
            "not define authenticate",
            f"(neolith.E021) {SETTING}[1] ({SHARED}): the shared-secret backend is "
            "listed more than once; list it once",
            f"(neolith.E021) {SIGNING}['stock']: app stock key stock-1 has another "
            f"secret in {SETTING}; one app and key id name one secret",
        ]
    ), refusal
    assert _leaks(refusal, []) == []


def test_set_up_refuses_signing_secrets_outside_their_form(registered: None) -> None:
    signing = {"orders": {"KEY_ID": "k", "SECRET": EXAMPLE_B64}}
    refusal, _ = _set_up({**host_apps.SERVED, "SIGNING_SECRETS": signing})
    assert refusal is not None
    assert f"(neolith.E001) {SIGNING}['orders']: orders is not a hosted app" in refusal
    assert f"(neolith.E001) {SIGNING}['orders']: SECRET is spec 03's example" in refusal
    assert EXAMPLE_B64 not in refusal


def test_set_up_builds_the_backends_and_signing_keys(registered: None) -> None:
    from neolith import _host

    neolith = {
        **host_apps.SERVED,
        "CALLER_BACKENDS": [_ACCEPTING],
        "SIGNING_SECRETS": {"stock": {"KEY_ID": "stock-1", "SECRET": _A}},
    }
    with (
        _in_place(NEOLITH=neolith),
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        host = _host._current()
        assert host is not None
        token = host.callers.mint("stock", "stock.v1")
        assert token is not None
        given = request(token.authorization())
        assert identify(host.callers.backends, given, "stock.v1") == "stock"
        found = checks.run_checks(tags=[_checks.TAG], include_deployment_checks=True)
    deploy = [m.msg for m in found if m.id == "neolith.W006"]
    assert deploy == [
        f"{SETTING}[0] ({SHARED}): the shared-secret backend is for development: "
        "every provider that accepts an app holds its secret and can sign as it, "
        "and a token seen on the network can be replayed until it expires"
    ]


def test_a_host_that_provides_contracts_with_no_caller_backend_warns(
    registered: None,
) -> None:
    refusal, warned = _set_up(dict(host_apps.SERVED))
    assert refusal is None
    assert (
        f"(neolith.W004) this host provides contracts and {SETTING} lists no caller "
        "backend, so every call to them over HTTP fails with unauthenticated"
    ) in warned
    _, warned = _set_up({**host_apps.SERVED, "CALLER_BACKENDS": [_ACCEPTING]})
    assert not [w for w in warned if "neolith.W004" in w]


def test_a_principal_backend_that_declares_a_neolith_header_warns(
    registered: None,
) -> None:
    refusal, warned = _set_up(
        {
            **host_apps.SERVED,
            "CALLER_BACKENDS": [_ACCEPTING],
            "PRINCIPAL_BACKENDS": [entry(f"{HERE}.DeclaresPrincipal")],
        }
    )
    assert refusal is None
    assert [w for w in warned if "neolith.W005" in w] == [
        f"(neolith.W005) {PRINCIPALS}[0] ({HERE}.DeclaresPrincipal): request_inputs "
        "names 'neolith-principal'; a principal backend never reads a neolith- "
        "header, which the host reads only from a calling app the contract's "
        "allow-list admits"
    ]


def test_no_deploy_warning_without_the_shared_secret_backend(
    registered: None,
) -> None:
    neolith = {**host_apps.SERVED, "CALLER_BACKENDS": [entry(f"{HERE}.Counting")]}
    with (
        _in_place(NEOLITH=neolith),
        override_settings(INSTALLED_APPS=host_apps.INSTALLED),
    ):
        found = checks.run_checks(tags=[_checks.TAG], include_deployment_checks=True)
    assert not [m for m in found if m.id == "neolith.W006"]


class OwnInit(SharedSecretBackend):
    """A subclass whose constructor never runs the base one."""

    def __init__(self, **options: object) -> None:
        pass


def test_a_subclass_that_holds_no_keys_is_judged_without_reading_them(
    registered: None,
) -> None:
    signing = _signers(orders=("orders-1", ORDERS))
    assert key_problems(signing, OwnInit()) == []
    refusal, _ = _set_up(
        {
            **host_apps.SERVED,
            "CALLER_BACKENDS": [entry(f"{HERE}.OwnInit")],
            "SIGNING_SECRETS": {"stock": {"KEY_ID": "stock-1", "SECRET": _A}},
        }
    )
    assert refusal is None
    error = _caught(lambda: identify(built(OwnInit()), request(fresh()), IDENTITY))
    assert type(error) is Failed
    assert str(error).endswith(
        "authenticate raised Failure: the shared-secret token could not be checked"
    )


def test_a_refused_constructor_holds_no_secret_in_any_frame() -> None:
    """A refusal raised while the options hold an admitted secret."""
    options = {"orders": {"k1": _A, "k2": _O[:-1]}, "billing": {"b": _B}}
    error = _caught(lambda: SharedSecretBackend(secrets=dict(options)))
    assert type(error) is ImproperlyConfigured
    options.clear()
    assert _leaks(_described(error, request()), []) == []
