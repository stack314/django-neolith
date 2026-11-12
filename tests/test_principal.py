"""The principal value, its sent form and the check of a bare assertion.

A principal is checked through its sent form (spec 03): the caller writes it
with JCS and reads it with the provider's own check, so a caller refuses
exactly what a provider would (ADR 0017 Decision 1). How a call fails over its
principal is tested in ``test_call_context.py``.
"""

import copy
import json
import pickle
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neolith import Principal
from neolith._context import b64url, principal_form, read_principal

# Deterministic, as every test is (CONTRIBUTING T3): the same examples on each
# run, and no time limit that a busy machine could miss.
_SETTINGS = settings(derandomize=True, database=None, max_examples=200, deadline=None)

# The value.


def test_a_principal_holds_its_issuer_subject_and_claims() -> None:
    claims: dict[str, Any] = {
        "name": "Ada",
        "age": 36,
        "admin": True,
        "roles": ["clerk", "buyer"],
    }
    principal = Principal("https://id.example.com", "u-81f3", claims)
    assert principal.issuer == "https://id.example.com"
    assert principal.subject == "u-81f3"
    assert dict(principal.claims) == {**claims, "roles": ("clerk", "buyer")}
    assert Principal("i", "s").claims == {}


def test_a_principal_keeps_a_copy_of_its_claims() -> None:
    roles = ["clerk"]
    claims: dict[str, Any] = {"roles": roles}
    principal = Principal("i", "s", claims)
    roles.append("admin")
    claims["more"] = 1
    assert dict(principal.claims) == {"roles": ("clerk",)}


def test_claims_are_read_once_so_a_change_cannot_pass_the_check() -> None:
    class Changing(Mapping[str, Any]):
        """A mapping whose values turn into something refused once read."""

        def __init__(self) -> None:
            self.reads = 0

        def __getitem__(self, name: str) -> Any:  # noqa: ANN401
            self.reads += 1
            return "fine" if self.reads == 1 else 1.5

        def __iter__(self) -> Iterator[str]:
            return iter(["claim"])

        def __len__(self) -> int:
            return 1

    changing = Changing()
    assert dict(Principal("i", "s", changing).claims) == {"claim": "fine"}
    assert changing.reads == 1


@pytest.mark.parametrize(
    ("issuer", "subject", "claims"),
    [
        (1, "s", None),
        ("i", None, None),
        (b"i", "s", None),
        ("i", "s", [("a", "b")]),
        ("i", "s", "claims"),
        ("i", "s", {1: "a"}),
        ("i", "s", {"a": 1.5}),
        ("i", "s", {"a": None}),
        ("i", "s", {"a": {"b": "c"}}),
        ("i", "s", {"a": [1]}),
        ("i", "s", {"a": ["b", ["c"]]}),
        ("i", "s", {"a": {"b"}}),
        ("i", "s", {"a": b"b"}),
    ],
    ids=repr,
)
def test_a_part_of_another_type_is_refused(
    issuer: Any,  # noqa: ANN401
    subject: Any,  # noqa: ANN401
    claims: Any,  # noqa: ANN401
) -> None:
    with pytest.raises(TypeError, match=r"principal's|claim's"):
        Principal(issuer, subject, claims)


def test_a_subtype_of_a_parts_type_is_refused() -> None:
    class Text(str):
        __slots__ = ()

    class Names(list[str]):
        pass

    for args in (
        (Text("i"), "s"),
        ("i", Text("s")),
        ("i", "s", {Text("a"): "b"}),
        ("i", "s", {"a": Text("b")}),
        ("i", "s", {"a": Names(["b"])}),
        ("i", "s", {"a": [Text("b")]}),
    ):
        with pytest.raises(TypeError, match=r"principal's|claim's"):
            Principal(*args)


def test_a_principal_is_frozen_and_cannot_be_subclassed() -> None:
    principal = Principal("i", "s", {"a": "b"})
    with pytest.raises(AttributeError, match="frozen"):
        principal.issuer = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError, match="frozen"):
        principal._subject = "other"
    with pytest.raises(AttributeError, match="frozen"):
        del principal._claims
    with pytest.raises(TypeError):
        principal.claims["a"] = "c"  # type: ignore[index]
    with pytest.raises(TypeError, match="cannot be subclassed"):
        type("Mine", (Principal,), {})
    assert not hasattr(principal, "__dict__")


def test_principals_are_equal_when_their_members_are() -> None:
    one = Principal("i", "s", {"a": "b", "roles": ["x", "y"], "n": 1})
    assert one == Principal("i", "s", {"n": 1, "roles": ("x", "y"), "a": "b"})
    assert hash(one) == hash(
        Principal("i", "s", {"n": 1, "roles": ["x", "y"], "a": "b"})
    )
    assert Principal("i", "s") == Principal("i", "s", {})
    for other in (
        Principal("j", "s", dict(one.claims)),
        Principal("i", "t", dict(one.claims)),
        Principal("i", "s", {"a": "b", "roles": ["y", "x"], "n": 1}),
        Principal("i", "s", {"a": "b", "roles": ["x", "y"], "n": True}),
        Principal("i", "s", {"a": "b", "roles": ["x", "y"]}),
        Principal("i", "s", {"a": "b", "roles": "x", "n": 1}),
        ("i", "s"),
    ):
        assert one != other
    # A claim's value is compared with its type, as JSON tells 1 from true.
    assert Principal("i", "s", {"n": 1}) != Principal("i", "s", {"n": True})
    # Text is compared code point by code point, with no normalization.
    assert Principal("i", "é") != Principal("i", "é")


def test_a_principal_copies_and_pickles_as_an_equal_one() -> None:
    principal = Principal("i", "s", {"roles": ["x"], "n": 1, "ok": False})
    for made in (
        copy.copy(principal),
        copy.deepcopy(principal),
        pickle.loads(pickle.dumps(principal)),  # noqa: S301
    ):
        assert made == principal
        assert type(made) is Principal
        assert type(made.claims["ok"]) is bool
    assert repr(principal) == (
        "Principal('i', 's', {'roles': ('x',), 'n': 1, 'ok': False})"
    )
    assert repr(Principal.__new__(Principal)) == "<unbuilt Principal>"


# The sent form.


def test_the_sent_form_is_the_example_of_spec_03() -> None:
    principal = Principal("https://id.example.com", "u-81f3", {"roles": ["clerk"]})
    form = principal_form(principal)
    assert form == (
        b'{"claims":{"roles":["clerk"]},"issuer":"https://id.example.com",'
        b'"subject":"u-81f3"}'
    )
    assert b64url(form) == (
        "eyJjbGFpbXMiOnsicm9sZXMiOlsiY2xlcmsiXX0sImlzc3VlciI6Imh0dHBzOi8vaWQu"
        "ZXhhbXBsZS5jb20iLCJzdWJqZWN0IjoidS04MWYzIn0"
    )
    assert read_principal(form) == principal


def test_empty_claims_are_left_out_of_the_sent_form() -> None:
    assert principal_form(Principal("i", "s", {})) == b'{"issuer":"i","subject":"s"}'


# The longest subject whose principal still fits: the form is 27 bytes and the
# subject, and 3072 bytes encode as exactly 4096.
_FITS = 3072 - len(b'{"issuer":"i","subject":""}')

_VALID: dict[str, Principal] = {
    "plain": Principal("i", "s"),
    "U+FFFF": Principal("i", "s￿", {"￿": ["￿"]}),
    "astral": Principal("\U0001f600", "s", {"a": "\U0010ffff"}),
    "largest integers": Principal("i", "s", {"a": 2**53 - 1, "b": -(2**53 - 1)}),
    "booleans": Principal("i", "s", {"a": True, "b": False, "c": 0}),
    "empty name and values": Principal("i", "s", {"": "", "a": []}),
    "controls": Principal("i\x00\x1f", 's"\\\n', {"\t": "\x7f"}),
    "encoding of exactly 4096": Principal("i", "s" * _FITS),
}

_INVALID: dict[str, Principal] = {
    "empty issuer": Principal("", "s"),
    "empty subject": Principal("i", ""),
    "surrogate in issuer": Principal("i\ud800", "s"),
    "surrogate in subject": Principal("i", "\udfffs"),
    "surrogate in a claim's name": Principal("i", "s", {"\ud83d": "a"}),
    "surrogate in a claim": Principal("i", "s", {"a": "b\ud800"}),
    "surrogate in a list": Principal("i", "s", {"a": ["b", "\udc00"]}),
    "integer above the range": Principal("i", "s", {"a": 2**53}),
    "integer below the range": Principal("i", "s", {"a": -(2**53)}),
    "integer no double holds": Principal("i", "s", {"a": 2**53 + 1}),
    "integer of 5000 digits": Principal("i", "s", {"a": 10**5000}),
    "encoding of 4097": Principal("i", "s" * (_FITS + 1)),
    "encoding far over": Principal("i", "s", {"a": ["x" * 100] * 100}),
}


@pytest.mark.spec("CON-51")
@pytest.mark.parametrize("case", list(_VALID), ids=list(_VALID))
def test_a_valid_principal_reads_back_from_its_sent_form(case: str) -> None:
    principal = _VALID[case]
    form = principal_form(principal)
    assert len(b64url(form)) <= 4096
    read = read_principal(form)
    assert read == principal
    assert read is not principal


@pytest.mark.spec("CON-51")
@pytest.mark.parametrize("case", list(_INVALID), ids=list(_INVALID))
def test_an_invalid_principal_has_no_sent_form(case: str) -> None:
    with pytest.raises(ValueError, match=r"principal|claim") as raised:
        principal_form(_INVALID[case])
    # The refusal names the rule; it is logged, so it shows none of the value.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True or raised.value.__context__ is None


def test_the_encoding_boundary_is_exact() -> None:
    assert len(b64url(principal_form(Principal("i", "s" * _FITS)))) == 4096
    with pytest.raises(ValueError, match="longer than 4096 bytes"):
        principal_form(Principal("i", "s" * (_FITS + 1)))


# The provider's check of a bare assertion (spec 03, The principal).


@pytest.mark.parametrize(
    "form",
    [
        b'{"issuer":"i","subject":"s"}',
        b' {\n"subject" : "s",\t"issuer":"i"\r} ',
        b'{"issuer":"i","subject":"s","claims":{}}',
        b'{"issuer":"\\u0069","subject":"s"}',
        b'{"claims":{"a":"b","n":-9007199254740991,"t":true,"l":["x"],"e":[]},'
        b'"issuer":"i","subject":"s"}',
        '{"issuer":"i￿","subject":"\U0001f600"}'.encode(),
        b'{"issuer":"i","subject":"\\ud83d\\ude00"}',
    ],
)
def test_a_principal_object_is_read_in_any_valid_serialization(form: bytes) -> None:
    read = read_principal(form)
    assert type(read) is Principal
    assert read.issuer.startswith("i")
    # An absent claims member is the same as an empty one.
    assert read_principal(b'{"issuer":"i","subject":"s","claims":{}}') == Principal(
        "i", "s"
    )


_MARKED = "marked-value"

_NOT_PRINCIPALS: dict[str, bytes] = {
    "not UTF-8": b'{"issuer":"\xff","subject":"s"}',
    "a byte order mark": b'\xef\xbb\xbf{"issuer":"i","subject":"s"}',
    "not JSON": b'{"issuer":"i","subject":"s"',
    "two values": b'{"issuer":"i","subject":"s"}{}',
    "an array": b'["i","s"]',
    "a string": b'"i"',
    "null": b"null",
    "no subject": b'{"issuer":"i"}',
    "no issuer": b'{"subject":"s"}',
    "another member": b'{"issuer":"i","subject":"s","sub":"s"}',
    "issuer twice": b'{"issuer":"i","issuer":"i","subject":"s"}',
    "a claim twice": b'{"issuer":"i","subject":"s","claims":{"a":1,"a":1}}',
    "empty issuer": b'{"issuer":"","subject":"s"}',
    "empty subject": b'{"issuer":"i","subject":""}',
    "issuer not text": b'{"issuer":1,"subject":"s"}',
    "subject null": b'{"issuer":"i","subject":null}',
    "claims null": b'{"issuer":"i","subject":"s","claims":null}',
    "claims an array": b'{"issuer":"i","subject":"s","claims":[]}',
    "a claim with a fraction": b'{"issuer":"i","subject":"s","claims":{"a":1.0}}',
    "a claim with an exponent": b'{"issuer":"i","subject":"s","claims":{"a":1e3}}',
    "a claim of NaN": b'{"issuer":"i","subject":"s","claims":{"a":NaN}}',
    "a claim of Infinity": b'{"issuer":"i","subject":"s","claims":{"a":-Infinity}}',
    "a claim above the range": (
        b'{"issuer":"i","subject":"s","claims":{"a":9007199254740992}}'
    ),
    "a claim of 5000 digits": (
        b'{"issuer":"i","subject":"s","claims":{"a":' + b"9" * 5000 + b"}}"
    ),
    "a claim of null": b'{"issuer":"i","subject":"s","claims":{"a":null}}',
    "a claim of an object": b'{"issuer":"i","subject":"s","claims":{"a":{}}}',
    "a list of integers": b'{"issuer":"i","subject":"s","claims":{"a":[1]}}',
    "a list in a list": b'{"issuer":"i","subject":"s","claims":{"a":[["b"]]}}',
    "a lone surrogate": b'{"issuer":"i","subject":"\\ud800"}',
    "a lone surrogate in a name": (
        b'{"issuer":"i","subject":"s","claims":{"\\udc00":1}}'
    ),
    "a lone surrogate in a list": (
        b'{"issuer":"i","subject":"s","claims":{"a":["\\ud83d"]}}'
    ),
    "nested past the reader": (
        b'{"issuer":"i","subject":"s","claims":{"a":' + b"[" * 100_000 + b"}}"
    ),
}


@pytest.mark.parametrize("case", list(_NOT_PRINCIPALS), ids=list(_NOT_PRINCIPALS))
def test_what_is_not_a_principal_object_is_refused(case: str) -> None:
    with pytest.raises(ValueError, match=r".") as raised:
        read_principal(_NOT_PRINCIPALS[case])
    assert type(raised.value) is ValueError


def test_a_refusal_shows_none_of_the_principal() -> None:
    for form in (
        f'{{"issuer":"{_MARKED}","subject":""}}',
        f'{{"issuer":"i","subject":"s","claims":{{"{_MARKED}":null}}}}',
        f'{{"issuer":"i","subject":"s","{_MARKED}":1}}',
        f'{{"issuer":"i","subject":"s","claims":{{"a":"{_MARKED}","b":1.5}}}}',
        f'{{"issuer":"i","subject":"{_MARKED}\\ud800"}}',
        f'{{"issuer":"i","subject":"{_MARKED}"',
    ):
        with pytest.raises(ValueError, match=r".") as raised:
            read_principal(form.encode())
        assert _MARKED not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None or raised.value.__suppress_context__
    for principal in (
        Principal(_MARKED, ""),
        Principal("i", "s", {_MARKED: 2**60}),
        Principal("i", _MARKED + "\ud800"),
        Principal("i", "s", {"a": [_MARKED] * 1000}),
    ):
        with pytest.raises(ValueError, match=r".") as raised:
            principal_form(principal)
        assert _MARKED not in str(raised.value)


# Invariant: a caller refuses exactly what a provider would, and what it sends
# reads back as the principal it holds.

_TEXT = st.text(max_size=6)
_CLAIM = (
    _TEXT
    | st.booleans()
    | st.integers(-(2**53) - 2, 2**53 + 2)
    | st.integers()
    | st.lists(_TEXT, max_size=3)
)
_PRINCIPALS = st.builds(
    Principal, _TEXT, _TEXT, st.none() | st.dictionaries(_TEXT, _CLAIM, max_size=4)
)


def _lone_surrogate(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _valid(principal: Principal) -> bool:
    """Spec 03's rules for a principal object, stated apart from the code."""
    texts = [principal.issuer, principal.subject]
    for name, value in principal.claims.items():
        texts.append(name)
        if type(value) is str:
            texts.append(value)
        elif type(value) is tuple:
            texts.extend(value)
        elif type(value) is int and abs(value) > 2**53 - 1:
            return False
    return (
        principal.issuer != ""
        and principal.subject != ""
        and not any(_lone_surrogate(text) for text in texts)
    )


@pytest.mark.spec("CON-51")
@_SETTINGS
@given(_PRINCIPALS)
def test_a_principal_has_a_sent_form_exactly_when_it_is_valid(
    principal: Principal,
) -> None:
    if not _valid(principal):
        with pytest.raises(ValueError, match=r"."):
            principal_form(principal)
        return
    form = principal_form(principal)
    assert read_principal(form) == principal
    # The form is JCS: one spelling, whatever order the claims were given in.
    again = Principal(
        principal.issuer,
        principal.subject,
        dict(reversed(list(principal.claims.items()))),
    )
    assert principal_form(again) == form
    written = json.loads(form)
    assert ("claims" in written) == bool(principal.claims)
    assert list(written) == sorted(written)
