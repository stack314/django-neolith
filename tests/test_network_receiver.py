"""The network receiver, and the provider's side both transports share.

A receiver that reads text from a network runs the receive check of ADR 0007
Decision 3 before the declared type's validator reads the text: the text is
I-JSON (spec 01, CON-42), and it passes the description check (CON-16,
CON-39), with the rules that check adds (CON-40). In-process the receiver
reads only text a sender's check passed, so it runs the validator alone.

From the deadline on, the in-process transport and the HTTP endpoint share
one provider side (ADR 0011 Decision 3): it takes the call's context as it
arrived, with the deadline counted at arrival, the key or none, and the
principal already read, and a receiver kind that turns the network check on.
"""

import json
import logging
import sys
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import AwareDatetime, BaseModel, Field

from neolith import CallContext, Contract, ContractDefinitionError, action, contract
from neolith._connector import Failure, InProcess, Provider
from neolith._context import Arrived, Principal, Sent
from neolith._crossing import Receiver, Refusal, codec
from tests.strategies import admitted_by
from tests.test_call_context import Clock
from tests.test_contract_round_trip import _HYPOTHESIS, _TYPES, Reference, _records
from tests.test_send_side_check import (
    _FORMATS,
    _JSON,
    _contract_for,
    _every_field,
    _near,
    _part,
    _paths,
    _Reference,
    _with,
)

# Deterministic, as every test is (CONTRIBUTING T3).
_SETTINGS = settings(derandomize=True, database=None, max_examples=300, deadline=None)


class Line(BaseModel):
    sku: str = ""
    qty: int = 0


class Order(BaseModel):
    lines: list[Line] = []
    notes: dict[str, Line] = {}
    ratio: Annotated[float, Field(le=2**60)] = 0.0
    count: int = 0
    day: date | None = None
    when: AwareDatetime | None = None


class Taken(BaseModel):
    lines: int = 0


@contract("orders.v1")
class Orders(Protocol):
    @action
    def place(self, request: Order) -> Taken: ...


ORDERS = Contract.of(Orders)
PLACE = ORDERS.actions["place"]
ORDER_SCHEMA = {"$defs": ORDERS.describe()["$defs"], "$ref": "#/$defs/Order"}


class Placing:
    """An implementation that keeps each request and context it is handed."""

    def __init__(self, then: Callable[[], object] | None = None) -> None:
        self.seen: list[Order] = []
        self.contexts: list[CallContext] = []
        self._then = then

    def place(self, request: Order, ctx: CallContext) -> Taken:
        self.seen.append(request)
        self.contexts.append(ctx)
        if self._then is not None:
            self._then()
        return Taken(lines=len(request.lines))


def _provider(implementation: object) -> Provider:
    return Provider(
        Orders, implementation, app="orders", requires=(), principal_from=("shop",)
    )


def _arrived(**changes: Any) -> Arrived:  # noqa: ANN401
    fields: dict[str, Any] = {
        "principal": None,
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "tracestate": "",
        "deadline": None,
        "idempotency_key": "a-key",
        "caller": "shop",
    }
    return Arrived(**{**fields, **changes})


def _receive(text: bytes) -> str | None:
    """The refusal the network receiver gives a request, or None."""
    try:
        codec(ORDERS, Order).receive(text, "the request")
    except Refusal as refusal:
        return str(refusal)
    return None


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    from neolith import _context

    made = Clock()
    monkeypatch.setattr(_context, "now", made)
    return made


# I-JSON (CON-42): text rules the description check cannot see, because they
# hide in members a record ignores, or the reader resolves them before any
# check runs.

_LARGEST = int(sys.float_info.max)

# A member written into a record, which a record ignores unless the text
# itself is refused, with the rule each one breaks.
_REFUSED_MEMBERS: dict[str, tuple[str, str]] = {
    "NaN": ('"later":NaN', "NaN or Infinity"),
    "Infinity": ('"later":Infinity', "NaN or Infinity"),
    "-Infinity": ('"later":[1,-Infinity]', "NaN or Infinity"),
    "1e400": ('"later":1e400', "outside the range of a double"),
    "-1e400": ('"later":{"deep":[-1e400]}', "outside the range of a double"),
    "an integer past the largest double": (
        f'"later":{_LARGEST + 1}',
        "outside the range of a double",
    ),
    "a long integer": ('"later":-1' + "0" * 400, "outside the range of a double"),
    "a lone high surrogate": ('"later":"\\ud800"', "lone surrogate in a string"),
    "a lone low surrogate": ('"later":["a\\udfffb"]', "lone surrogate in a string"),
    "reversed surrogates": ('"later":"\\udc00\\ud800"', "lone surrogate in a string"),
    "a lone surrogate in a name": (
        '"\\ud83d":1',
        "lone surrogate in a member name",
    ),
    "a duplicate inside": ('"later":{"a":1,"a":1}', "member name twice"),
}

# The nearest member the text rules admit, for each refused one.
_ADMITTED_MEMBERS: dict[str, str] = {
    "the largest double": '"later":1.7976931348623157e308',
    "the largest double, as an integer": f'"later":{_LARGEST}',
    "a tiny number": '"later":1e-400',
    "a negative zero": '"later":-0',
    "a surrogate pair": '"later":"\\ud83d\\ude00"',
    "an escaped name": '"\\u00e9":["\\uffff"]',
    "the same name in two objects": '"later":[{"a":1},{"a":1}]',
}


def _write(
    value: object, at: tuple[Any, ...], extra: str, here: tuple[Any, ...] = ()
) -> str:
    """JSON text of ``value``, with the member ``extra`` written last into
    the object at path ``at``."""
    if type(value) is dict:
        members = [
            f"{json.dumps(key)}:{_write(part, at, extra, (*here, key))}"
            for key, part in value.items()
        ]
        if here == at:
            members.append(extra)
        return "{" + ",".join(members) + "}"
    if type(value) is list:
        return (
            "["
            + ",".join(
                _write(part, at, extra, (*here, index))
                for index, part in enumerate(value)
            )
            + "]"
        )
    return json.dumps(value)


def _records_in(order: dict[str, Any]) -> list[tuple[Any, ...]]:
    """The path of each record object in a request: the order and its lines."""
    paths: list[tuple[Any, ...]] = [()]
    paths += [("lines", index) for index in range(len(order["lines"]))]
    paths += [("notes", key) for key in order["notes"]]
    return paths


@pytest.mark.spec("CON-42")
@_SETTINGS
@given(data=st.data())
def test_the_network_receiver_refuses_text_that_is_not_i_json_at_any_depth(
    data: st.DataObject,
) -> None:
    order = data.draw(admitted_by(_every_field(ORDER_SCHEMA)))
    at = data.draw(st.sampled_from(_records_in(order)))
    assert _receive(json.dumps(order).encode()) is None
    # A member the record already has, written again with the same value.
    first, part = next(iter(cast("dict[str, Any]", _part(order, at)).items()))
    twice = f"{json.dumps(first)}:{json.dumps(part)}"
    said = _receive(_write(order, at, twice).encode())
    assert said == "the request holds a member name twice in one object"
    # A member no field names, holding what the text rules refuse.
    name = data.draw(st.sampled_from(sorted(_REFUSED_MEMBERS)))
    member, rule = _REFUSED_MEMBERS[name]
    said = _receive(_write(order, at, member).encode())
    assert said is not None
    assert rule in said
    # And the nearest member they admit.
    near = _ADMITTED_MEMBERS[data.draw(st.sampled_from(sorted(_ADMITTED_MEMBERS)))]
    assert _receive(_write(order, at, near).encode()) is None


@pytest.mark.spec("CON-42")
@pytest.mark.parametrize(
    ("text", "said"),
    [
        (b'{"notes":{"a":{},"a":{}}}', "holds a member name twice in one object"),
        (b'{"lines":[{"sku":"\\ud800"}]}', "holds a lone surrogate in a string"),
        (b'{"ratio":NaN}', "holds NaN or Infinity, which are not JSON numbers"),
        (b'{"ratio":-1e999}', "holds a number outside the range of a double"),
        (b"\xef\xbb\xbf{}", "is not JSON text"),
        (b'{"lines":[]} []', "is not JSON text"),
        (b'{"sku":"\xed\xa0\x80"}', "is not UTF-8 text"),
        (
            b"[" * 100_000 + b"]" * 100_000,
            "is nested too deeply to be read as JSON text",
        ),
    ],
    ids=[
        "duplicate-in-a-map",
        "surrogate-in-a-field",
        "nan-in-a-field",
        "past-a-double-in-a-field",
        "byte-order-mark",
        "trailing-text",
        "encoded-surrogate",
        "deep",
    ],
)
def test_each_text_rule_names_itself(text: bytes, said: str) -> None:
    assert _receive(text) == f"the request {said}"


# The description check (CON-39), with integer text compared exactly.


class Bounds(BaseModel):
    at_most: Annotated[float, Field(le=2**60)] = 0.0
    below: Annotated[float, Field(lt=2**60)] = 0.0
    at_least: Annotated[float, Field(ge=-(2**60))] = 0.0
    above: Annotated[float, Field(gt=-(2**60))] = 0.0
    whole: int = 0


class Bounded(BaseModel):
    top: Bounds = Field(default_factory=Bounds)
    many: list[Bounds] = []
    keyed: dict[str, Bounds] = {}
    maybe: Bounds | None = None
    bare: list[Annotated[float, Field(le=2**60)]] = []


@contract("bounded.v1")
class BoundedContract(Protocol):
    @action
    def put(self, request: Bounded) -> None: ...


BOUNDED = Contract.of(BoundedContract)
BOUNDED_SCHEMA = {"$defs": BOUNDED.describe()["$defs"], "$ref": "#/$defs/Bounded"}

_SAFE = 2**53 - 1
# What each field admits of a number, compared exactly.
_ADMITS: dict[str, Callable[[float | int], bool]] = {
    "at_most": lambda x: x <= 2**60,
    "below": lambda x: x < 2**60,
    "at_least": lambda x: x >= -(2**60),
    "above": lambda x: x > -(2**60),
    "whole": lambda x: -_SAFE <= x <= _SAFE,
}
_PLACES: dict[str, Callable[[str, int], str]] = {
    "a record": lambda field, x: f'{{"top":{{"{field}":{x}}}}}',
    "a list": lambda field, x: f'{{"many":[{{}},{{"{field}":{x}}}]}}',
    "a map": lambda field, x: f'{{"keyed":{{"k":{{"{field}":{x}}}}}}}',
    "an optional": lambda field, x: f'{{"maybe":{{"{field}":{x}}}}}',
}
_EDGES = [
    edge + step
    for edge in (2**60, -(2**60), 2**53, -(2**53))
    for step in (-129, -128, -65, -64, -63, -1, 0, 1, 63, 64, 65, 128, 129)
]


@pytest.mark.spec("CON-39")
@_SETTINGS
@given(
    x=st.sampled_from(_EDGES) | st.integers(-(2**61), 2**61),
    field=st.sampled_from(sorted(_ADMITS)),
    place=st.sampled_from(sorted(_PLACES)),
)
def test_integer_text_is_held_to_each_bound_exactly(
    x: int, field: str, place: str
) -> None:
    """Integer text is admitted where its exact value is in range and, in a
    number's place, so is the double the validator reads it as. Whatever a
    JSON Schema validator, which compares integers exactly, refuses, the
    receiver refuses too.
    """
    text = _PLACES[place](field, x)
    admits = _ADMITS[field]
    expected = admits(x) and (field == "whole" or admits(float(x)))
    try:
        codec(BOUNDED, Bounded).receive(text.encode(), "the request")
    except Refusal as refusal:
        refused: str | None = str(refusal)
    else:
        refused = None
    assert (refused is None) == expected, (text, refused)
    if not _Reference(BOUNDED_SCHEMA, format_checker=_FORMATS).is_valid(
        json.loads(text)
    ):
        assert refused is not None


@pytest.mark.spec("CON-39")
@pytest.mark.parametrize("x", [2**60 + 1, 2**60 + 64, 2**60 + 127])
def test_integer_text_that_rounds_onto_a_maximum_is_refused(x: int) -> None:
    # Each of these reads as the double 2**60, which the bound admits.
    assert float(x) == 2**60
    for text in (f'{{"top":{{"at_most":{x}}}}}', f'{{"bare":[0,{x}]}}'):
        with pytest.raises(Refusal) as refused:
            codec(BOUNDED, Bounded).receive(text.encode(), "the request")
        assert str(refused.value).endswith(
            "Input should be less than or equal to 1152921504606846976"
        )
    # The bound itself is admitted, and so is the same value with a fraction,
    # which is read as the double it rounds to.
    assert (
        codec(BOUNDED, Bounded)
        .receive(
            f'{{"top":{{"at_most":{2**60}}},"bare":[{x}.0]}}'.encode(), "the request"
        )
        .top.at_most
        == 2**60
    )


@pytest.mark.spec("CON-39", "CON-16")
@pytest.mark.parametrize(
    ("text", "place"),
    [
        (b'{"top":{"at_most":"1"}}', "Bounded.top.at_most"),
        (b'{"many":[{},{"whole":1.5}]}', "Bounded.many[1].whole"),
        (
            b'{"keyed":{"s3cr3t":{},"t0p":{"above":true}}}',
            "Bounded.keyed[entry 1].above",
        ),
        (b'{"keyed":{"s3cr3t":[]}}', "Bounded.keyed[entry 0]"),
        (b'{"maybe":{"whole":"s3cr3t"}}', "Bounded.maybe.whole"),
        (b'{"bare":[1,null]}', "Bounded.bare[1]"),
        (b"[]", "Bounded"),
    ],
)
def test_a_refusal_names_the_place_and_never_a_value(text: bytes, place: str) -> None:
    with pytest.raises(Refusal) as refused:
        codec(BOUNDED, Bounded).receive(text, "the request")
    said = str(refused.value)
    assert said.startswith(
        f"the request does not fit the contract's description of Bounded, at {place}: "
    )
    assert "s3cr3t" not in said
    assert "t0p" not in said


# The rules the description cannot state (CON-40).


@pytest.mark.spec("CON-40")
@pytest.mark.parametrize(
    ("refused", "admitted"),
    [
        (b'{"count":1.0}', b'{"count":1}'),
        (b'{"count":1e0}', b'{"count":1}'),
        (b'{"lines":[{"qty":-0.0}]}', b'{"lines":[{"qty":0}]}'),
        (b'{"day":"2027-02-29"}', b'{"day":"2028-02-29"}'),
        (b'{"when":"2027-04-31T00:00:00Z"}', b'{"when":"2027-04-30T00:00:00Z"}'),
        (
            b'{"when":"0001-01-01T00:00:00+00:01"}',
            b'{"when":"0001-01-01T00:01:00+00:01"}',
        ),
        (
            b'{"when":"9999-12-31T23:59:59-00:01"}',
            b'{"when":"9999-12-31T23:58:59-00:01"}',
        ),
    ],
    ids=[
        "integer-with-a-fraction",
        "integer-with-an-exponent",
        "integer-written-as-negative-zero",
        "day-that-does-not-exist",
        "timestamp-on-a-day-that-does-not-exist",
        "instant-before-year-1",
        "instant-after-year-9999",
    ],
)
def test_the_network_receiver_refuses_what_the_description_cannot_state(
    refused: bytes, admitted: bytes
) -> None:
    said = _receive(refused)
    assert said is not None
    assert said.startswith("the request does not fit the contract's description")
    assert _receive(admitted) is None


def test_in_process_the_receiver_reads_with_the_validator_alone() -> None:
    # The sender's check passed the text, so the in-process receiver runs no
    # check of its own (ADR 0007 Decision 3); the validator alone would read
    # each of these.
    for text in (b'{"count":1.0}', b'{"later":NaN}', b'{"count":1,"count":2}'):
        assert _receive(text) is not None
        codec(ORDERS, Order).read(text)


# Agreement with a JSON Schema validator, on every keyword the description
# writes (CON-16, CON-39, CON-40).


def _receives(found: Contract, record: type[BaseModel], value: object) -> bool:
    try:
        codec(found, record).receive(json.dumps(value).encode(), "the value")
    except Refusal:
        return False
    return True


def _agrees(
    found: Contract,
    record: type[BaseModel],
    schema: dict[str, Any],
    data: st.DataObject,
) -> None:
    """For a value drawn from the description, and each value made from it by
    replacing one part, at any depth, with a value next to that part or with
    any JSON value, the receiver admits what the reference validator admits.
    """
    reference = _Reference(schema, format_checker=_FORMATS)
    valid = data.draw(admitted_by(_every_field(schema)))
    assert _receives(found, record, valid)
    paths = _paths(valid)
    chosen = data.draw(
        st.lists(st.sampled_from(paths), min_size=1, max_size=8, unique=True)
    )
    for path in chosen:
        for near in [*_near(_part(valid, path)), data.draw(_JSON)]:
            value = _with(valid, path, near)
            assert _receives(found, record, value) == reference.is_valid(value), (
                path,
                near,
            )


@pytest.mark.spec("CON-16", "CON-39", "CON-40")
@settings(parent=_HYPOTHESIS, max_examples=40)
@given(data=st.data())
def test_the_network_receiver_agrees_with_a_json_schema_validator(
    data: st.DataObject,
) -> None:
    found, schema = _contract_for(Reference)
    _agrees(found, Reference, schema, data)


@pytest.mark.spec("CON-16", "CON-39", "CON-40")
@settings(parent=_HYPOTHESIS, max_examples=120)
@given(data=st.data())
def test_the_network_receiver_agrees_with_json_schema_on_generated_contracts(
    data: st.DataObject,
) -> None:
    record = cast("type[BaseModel]", data.draw(_records(_TYPES))[0])
    try:
        found, schema = _contract_for(record)
    except ContractDefinitionError:
        return
    _agrees(found, record, schema, data)


# The provider's side, shared by both transports (ADR 0011 Decision 3).

_ALICE = Principal("https://id.example.com", "alice", {"roles": ["clerk"]})


@pytest.mark.spec("CON-42", "CON-39", "CON-40")
@pytest.mark.parametrize(
    "text",
    [
        b'{"count":1,"count":1}',
        b'{"later":NaN}',
        b'{"lines":[{"sku":"\\udfff"}]}',
        b'{"ratio":1152921504606846977}',
        b'{"count":1.0}',
        b'{"notes":{"s3cr3t":{"qty":"s3cr3t"}}}',
        b"not json",
    ],
)
def test_a_request_the_network_receiver_refuses_is_invalid_argument_and_never_runs(
    text: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    placing = Placing()
    with caplog.at_level(logging.DEBUG, "neolith"), pytest.raises(Failure) as raised:
        _provider(placing).serve(PLACE, text, _arrived(), Receiver.NETWORK)
    assert raised.value.kind == "invalid_argument"
    assert raised.value.message == _receive(text)
    assert raised.value.detail is None
    assert "s3cr3t" not in raised.value.message
    assert placing.seen == []
    # The caller's input, not a failure of this host: nothing is logged.
    assert caplog.records == []


def test_in_process_the_same_text_is_read_by_the_validator_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    placing = Placing()
    provider = _provider(placing)
    # Text the validator reads: the action runs.
    assert (
        provider.serve(PLACE, b'{"count":1.0}', _arrived(), Receiver.IN_PROCESS)
        == b'{"lines":0}'
    )
    assert placing.seen == [Order(count=1)]
    # Text it does not read is a failure of the host, as it was.
    with caplog.at_level(logging.ERROR, "neolith"), pytest.raises(Failure) as raised:
        provider.serve(PLACE, b"not json", _arrived(), Receiver.IN_PROCESS)
    assert raised.value.kind == "internal"
    assert len(caplog.records) == 1


@pytest.mark.spec("TRN-36")
@pytest.mark.parametrize("receiver", list(Receiver))
def test_the_action_runs_in_the_context_as_it_arrived(
    receiver: Receiver, clock: Clock
) -> None:
    placing = Placing()
    arrived = _arrived(
        principal=_ALICE,
        tracestate="vendor=1",
        deadline=clock.time + 5,
        idempotency_key=None,
    )
    reply = _provider(placing).serve(PLACE, b'{"lines":[{}]}', arrived, receiver)
    assert reply == b'{"lines":1}'
    (ctx,) = placing.contexts
    assert ctx.principal == _ALICE
    assert ctx.trace_id == arrived.trace_id
    assert ctx.tracestate == "vendor=1"
    assert ctx.deadline == arrived.deadline
    # A call that arrived without a key has none.
    assert ctx.idempotency_key is None
    assert ctx.caller == "shop"


@pytest.mark.spec("TRN-36")
def test_an_in_process_call_sent_without_a_key_arrives_without_one() -> None:
    placing = Placing()
    transport = InProcess(Orders, placing, app="orders", principal_from=("shop",))
    sent = Sent(None, "0af7651916cd43dd8448eb211c80319c", "", None, None, "shop")
    assert transport.call(PLACE, b"{}", sent) == b'{"lines":0}'
    assert placing.contexts[0].idempotency_key is None


@pytest.mark.spec("TRN-35")
def test_over_a_network_the_deadline_is_checked_before_the_request(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    placing = Placing()
    provider = _provider(placing)
    with caplog.at_level(logging.DEBUG, "neolith"), pytest.raises(Failure) as raised:
        provider.serve(
            PLACE, b"not json", _arrived(deadline=clock.time), Receiver.NETWORK
        )
    assert (raised.value.kind, raised.value.message) == (
        "timeout",
        "the deadline passed before the action was run",
    )
    assert caplog.records == []
    # In time, the same request fails the receive check instead.
    with pytest.raises(Failure) as raised:
        provider.serve(
            PLACE, b"not json", _arrived(deadline=clock.time + 1), Receiver.NETWORK
        )
    assert raised.value.kind == "invalid_argument"
    assert placing.seen == []


def test_over_a_network_an_outcome_ready_after_the_deadline_is_discarded(
    clock: Clock,
) -> None:
    def late() -> None:
        clock.time += 10

    placing = Placing(late)
    with pytest.raises(Failure) as raised:
        _provider(placing).serve(
            PLACE, b"{}", _arrived(deadline=clock.time + 5), Receiver.NETWORK
        )
    assert (raised.value.kind, raised.value.message) == (
        "timeout",
        "the deadline passed before the call ended; the action might have run",
    )
    assert len(placing.seen) == 1


@pytest.mark.parametrize("receiver", list(Receiver))
def test_both_receivers_give_the_same_reply_to_text_a_sender_wrote(
    receiver: Receiver,
) -> None:
    sent = codec(ORDERS, Order).send(
        Order(lines=[Line(sku="a", qty=2)], notes={"k": Line()}, ratio=2.0**60),
        "the request",
    )
    assert _provider(Placing()).serve(PLACE, sent, _arrived(), receiver) == (
        b'{"lines":1}'
    )


def test_the_steps_before_the_deadline_are_the_transports() -> None:
    provider = _provider(Placing())
    assert provider.refusal("shop", True) is None
    assert provider.refusal("till", False) is None
    unlisted = provider.refusal("till", True)
    assert unlisted is not None
    assert unlisted.kind == "permission_denied"
    nobody = provider.refusal(None, False)
    assert nobody is not None
    assert nobody.kind == "unauthenticated"
    # The shared side takes only a context as it arrived, and a receiver.
    with pytest.raises(TypeError, match="as it arrived"):
        provider.serve(PLACE, b"{}", cast("Arrived", object()), Receiver.NETWORK)
    with pytest.raises(TypeError, match="Receiver"):
        provider.serve(PLACE, b"{}", _arrived(), cast("Receiver", "network"))
    with pytest.raises(TypeError, match="has no action"):
        provider.serve(BOUNDED.actions["put"], b"{}", _arrived(), Receiver.NETWORK)


def test_a_bound_that_is_not_whole_is_compared_as_written() -> None:
    # A non-whole bound has no integer form to show; integer text against it
    # is still compared exactly.
    class Part(BaseModel):
        share: Annotated[float, Field(le=0.5)] = 0.0

    found, _ = _contract_for(Part)
    with pytest.raises(Refusal, match=r"less than or equal to 0\.5$"):
        codec(found, Part).receive(b'{"share":1}', "the request")
    assert codec(found, Part).receive(b'{"share":0}', "the request").share == 0
