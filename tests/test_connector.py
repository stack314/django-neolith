"""The connector and the in-process transport (spec 02).

A connector mirrors its contract's actions and sends each call that passes the
caller's request check to its one provider, once. The in-process transport runs
the action in the caller's thread and carries the request and the reply as JSON
text. Until the provider's host delivers errors (#10 task 5), every failure on
the provider's side reaches the caller as ``internal``.
"""

import contextvars
import json
import logging
import threading
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from pydantic import BaseModel

from neolith import (
    Contract,
    ContractDefinitionError,
    ContractError,
    action,
    contract,
    implements,
)
from neolith._connector import Connector, InProcess
from neolith._contracts import Action
from tests.test_contract_round_trip import _HYPOTHESIS, _TYPES, _name, _records
from tests.test_crossing import _shared


class Part(BaseModel):
    sku: str
    quantity: int = 1


class Reserve(BaseModel):
    order: int
    parts: list[Part] = []


class Reserved(BaseModel):
    ok: bool
    parts: list[Part] = []


class Release(BaseModel):
    order: int


class OutOfStock(BaseModel):
    sku: str


@contract("connector.v1")
class Inventory(Protocol):
    @action(idempotent=True, errors={"out_of_stock": OutOfStock})
    def reserve(self, request: Reserve) -> Reserved: ...

    @action
    def release(self, request: Release) -> None: ...


class Recording:
    """An implementation that keeps what it received and returned."""

    def __init__(self, reply: Callable[[Reserve], object] | None = None) -> None:
        self.received: list[BaseModel] = []
        self.returned: list[object] = []
        self.threads: list[int] = []
        self._reply = reply

    def reserve(self, request: Reserve) -> Reserved:
        self.received.append(request)
        self.threads.append(threading.get_ident())
        made = (
            self._reply(request)
            if self._reply is not None
            else Reserved(ok=True, parts=request.parts)
        )
        self.returned.append(made)
        return cast("Reserved", made)

    def release(self, request: Release) -> None:
        self.received.append(request)
        self.threads.append(threading.get_ident())


def _connect(implementation: object) -> Any:  # noqa: ANN401
    return Connector(Inventory, InProcess(Inventory, implementation))


def _reserve() -> Reserve:
    return Reserve(order=7, parts=[Part(sku="a"), Part(sku="b", quantity=2)])


# The connector mirrors the contract's actions.


@pytest.mark.spec("TRN-15", "TRN-1", "TRN-5")
def test_a_call_returns_the_reply_as_a_new_value() -> None:
    provider = Recording()
    request = _reserve()
    reply = _connect(provider).reserve(request)
    assert type(reply) is Reserved
    assert reply == Reserved(ok=True, parts=request.parts)
    # Each side holds its own objects: the request the action got, the reply
    # it returned and the reply the caller got share nothing.
    (received,) = provider.received
    (returned,) = provider.returned
    assert received == request
    assert _shared(request, received) == set()
    assert _shared(returned, reply) == set()
    assert _shared(request, reply) == set()


@pytest.mark.spec("TRN-15")
def test_a_request_can_be_passed_by_keyword() -> None:
    assert _connect(Recording()).reserve(request=_reserve()).ok is True


@pytest.mark.spec("TRN-15")
def test_an_action_with_an_empty_reply_returns_none() -> None:
    provider = Recording()
    assert _connect(provider).release(Release(order=1)) is None
    assert provider.received == [Release(order=1)]


@pytest.mark.spec("TRN-15", "TRN-1")
def test_changes_after_the_call_do_not_reach_the_other_side() -> None:
    provider = Recording()
    request = _reserve()
    reply = _connect(provider).reserve(request)
    request.parts[0].sku = "changed"
    reply.parts.append(Part(sku="c"))
    assert provider.received[0].parts[0].sku == "a"  # type: ignore[attr-defined]
    assert len(provider.returned[0].parts) == 2  # type: ignore[attr-defined]


@pytest.mark.spec("TRN-15")
def test_the_connector_lists_its_actions() -> None:
    connector = _connect(Recording())
    assert {"reserve", "release"} <= set(dir(connector))
    assert repr(connector) == "<Connector connector.v1>"


@pytest.mark.spec("TRN-15", "CON-29")
@pytest.mark.parametrize("name", ["ship", "Reserve", "reserve_", "_contract_", "__x__"])
def test_an_action_the_contract_does_not_have_is_a_usage_error(name: str) -> None:
    connector = _connect(Recording())
    with pytest.raises(AttributeError, match=rf"has no action '{name}'") as raised:
        getattr(connector, name)
    if not name.startswith("_"):
        assert "its actions are reserve, release" in str(raised.value)


@pytest.mark.spec("TRN-15", "CON-29")
@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((), {}),
        ((_reserve(), _reserve()), {}),
        ((_reserve(),), {"request": _reserve()}),
        ((), {"req": _reserve()}),
        ((), {"request": _reserve(), "timeout": 1}),
    ],
    ids=["nothing", "two requests", "both ways", "other keyword", "extra keyword"],
)
def test_a_call_with_anything_but_one_request_is_a_usage_error(
    args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    provider = Recording()
    with pytest.raises(TypeError, match=r"connector\.v1, action 'reserve' takes one"):
        _connect(provider).reserve(*args, **kwargs)
    assert provider.received == []


@pytest.mark.spec("TRN-14", "CON-29")
@pytest.mark.parametrize(
    "request_",
    [Release(order=1), {"order": 1}, Reserve.model_construct(order="x")],
    ids=["other record", "dict", "constructed"],
)
def test_a_request_that_fails_the_callers_check_is_never_sent(request_: object) -> None:
    provider = Recording()
    with pytest.raises((TypeError, ValueError)) as raised:
        _connect(provider).reserve(request_)
    assert not isinstance(raised.value, ContractError)
    assert provider.received == []


# The in-process transport runs the action in the caller's thread.


_SEEN: contextvars.ContextVar[str] = contextvars.ContextVar("seen", default="unset")


@pytest.mark.spec("TRN-6")
def test_the_action_runs_in_the_callers_thread_and_context() -> None:
    seen: list[str] = []

    def reply(request: Reserve) -> Reserved:
        seen.append(_SEEN.get())
        _SEEN.set("set by the action")
        return Reserved(ok=True)

    provider = Recording(reply)
    connector = _connect(provider)

    def call() -> tuple[int, str]:
        _SEEN.set("set by the caller")
        connector.reserve(_reserve())
        return threading.get_ident(), _SEEN.get()

    with ThreadPoolExecutor(max_workers=1) as pool:
        caller, after = pool.submit(contextvars.copy_context().run, call).result()
    assert provider.threads == [caller]
    assert seen == ["set by the caller"]
    # Same thread and same context: nothing runs elsewhere, as a direct call.
    assert after == "set by the action"


@pytest.mark.spec("TRN-6", "TRN-15")
def test_concurrent_callers_each_get_their_own_reply() -> None:
    connector = _connect(Recording(lambda r: Reserved(ok=r.order % 2 == 0)))

    def call(order: int) -> bool:
        return cast("bool", connector.reserve(Reserve(order=order)).ok)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(200)))
    assert results == [order % 2 == 0 for order in range(200)]


# Single routing: one provider, one attempt, never a retry or fallback.


class Counting:
    """A transport that counts its calls and answers with a fixed outcome."""

    def __init__(self, outcome: Callable[[Action, bytes], bytes | None]) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self._outcome = outcome

    def call(self, action: Action, request: bytes) -> bytes | None:
        self.calls.append((action.name, request))
        return self._outcome(action, request)


def _unavailable(action: Action, request: bytes) -> bytes | None:
    raise ContractError("unavailable", "no provider could be reached")


def _timeout(action: Action, request: bytes) -> bytes | None:
    raise ContractError("timeout", "the deadline passed")


@pytest.mark.spec("TRN-16", "TRN-8", "CON-4")
@pytest.mark.parametrize("name", ["reserve", "release"], ids=["idempotent", "not"])
@pytest.mark.parametrize(
    "outcome", [_unavailable, _timeout], ids=["unavailable", "timeout"]
)
def test_a_failed_call_is_never_retried(
    name: str, outcome: Callable[[Action, bytes], bytes | None]
) -> None:
    transport = Counting(outcome)
    connector: Any = Connector(Inventory, transport)
    request = _reserve() if name == "reserve" else Release(order=1)
    with pytest.raises(ContractError) as raised:
        getattr(connector, name)(request)
    assert len(transport.calls) == 1
    # A transport's error reaches the caller as the transport raised it.
    assert raised.value.kind == outcome.__name__.lstrip("_")


@pytest.mark.spec("TRN-16", "TRN-13")
def test_the_transport_gets_the_text_that_passed_the_request_check() -> None:
    transport = Counting(lambda a, r: b'{"ok": true}')
    Connector(Inventory, transport).reserve(_reserve())
    ((name, text),) = transport.calls
    assert name == "reserve"
    assert json.loads(text) == _reserve().model_dump(mode="json")


# Failures on the provider's side reach the caller as internal, with nothing
# of the original.


class Secret(Exception):
    """An exception whose text must not cross."""


def _assert_internal(raised: pytest.ExceptionInfo[ContractError]) -> None:
    error = raised.value
    assert error.kind == "internal"
    assert error.data is None
    assert error.message == "the provider failed, or its reply could not be read"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert getattr(error, "__notes__", []) == []


_WRONG_REPLIES: dict[str, Callable[[Reserve], object]] = {
    "subtype": lambda r: type("Sub", (Reserved,), {})(ok=True),
    "other record": lambda r: Release(order=1),
    "dict": lambda r: {"ok": True},
    "None": lambda r: None,
    "constructed with text in a bool": lambda r: Reserved.model_construct(ok="yes"),
    "constructed with a number for text, nested": lambda r: Reserved.model_construct(
        ok=True, parts=[Part.model_construct(sku=1)]
    ),
}


@pytest.mark.spec("TRN-17", "TRN-9", "TRN-11", "TRN-3")
@pytest.mark.parametrize("wrong", list(_WRONG_REPLIES), ids=list(_WRONG_REPLIES))
def test_a_reply_the_provider_cannot_send_reaches_the_caller_as_internal(
    wrong: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="neolith"),
        pytest.raises(ContractError) as raised,
    ):
        _connect(Recording(_WRONG_REPLIES[wrong])).reserve(_reserve())
    _assert_internal(raised)
    assert "connector.v1, action 'reserve': the provider failed" in caplog.text


@implements(Inventory)
class ReleasingWithAReply:
    def reserve(self, request: Reserve) -> Reserved:
        return Reserved(ok=True)

    def release(self, request: Release) -> None:
        return Reserved(ok=True)  # type: ignore[return-value]


@pytest.mark.spec("TRN-17", "TRN-3")
def test_a_reply_for_an_empty_reply_reaches_the_caller_as_internal() -> None:
    with pytest.raises(ContractError) as raised:
        _connect(ReleasingWithAReply()).release(Release(order=1))
    _assert_internal(raised)


@pytest.mark.spec("TRN-17", "TRN-12", "TRN-3")
@pytest.mark.parametrize(
    "reply",
    [b'{"ok": "maybe"}', b"{", b'{"parts": []}', "not bytes", None],
    ids=["wrong type", "not JSON", "missing field", "str", "none"],
)
def test_a_reply_the_caller_cannot_read_reaches_it_as_internal(
    reply: object, caplog: pytest.LogCaptureFixture
) -> None:
    transport = Counting(lambda a, r: cast("bytes", reply))
    with (
        caplog.at_level(logging.ERROR, logger="neolith"),
        pytest.raises(ContractError) as raised,
    ):
        Connector(Inventory, transport).reserve(_reserve())
    _assert_internal(raised)
    assert "the reply could not be read" in caplog.text


@pytest.mark.spec("TRN-17", "TRN-3")
def test_a_reply_text_for_an_empty_reply_reaches_the_caller_as_internal() -> None:
    transport = Counting(lambda a, r: b"null")
    with pytest.raises(ContractError) as raised:
        Connector(Inventory, transport).release(Release(order=1))
    _assert_internal(raised)


def _raises(error: BaseException) -> Callable[[Reserve], object]:
    def reply(request: Reserve) -> object:
        raise error

    return reply


@pytest.mark.spec("TRN-3")
@pytest.mark.parametrize(
    "error",
    [
        Secret("password=hunter2"),
        ValueError("bad"),
        ContractError("not_found", "no such order"),
        ContractError("out_of_stock", "none left", OutOfStock(sku="a")),
        ContractError("unavailable", "an implementation may not raise this"),
    ],
    ids=["own exception", "builtin", "standard kind", "declared kind", "unavailable"],
)
def test_an_exception_from_the_action_reaches_the_caller_as_internal(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    # Until the provider's host delivers errors (#10 task 5), every failure of
    # the action fails closed, as internal.
    with (
        caplog.at_level(logging.ERROR, logger="neolith"),
        pytest.raises(ContractError) as raised,
    ):
        _connect(Recording(_raises(error))).reserve(_reserve())
    _assert_internal(raised)
    assert "hunter2" not in raised.value.message
    assert caplog.records[-1].exc_info is not None
    assert caplog.records[-1].exc_info[1] is error


class Stop(BaseException):
    """A signal to stop, not a failure of the action."""


@pytest.mark.spec("TRN-3")
@pytest.mark.parametrize("error", [Stop(), KeyboardInterrupt(), SystemExit(3)])
def test_an_exception_that_is_not_an_exception_propagates(error: BaseException) -> None:
    with pytest.raises(type(error)) as raised:
        _connect(Recording(_raises(error))).reserve(_reserve())
    assert raised.value is error


@pytest.mark.spec("TRN-3")
def test_an_error_the_action_received_from_its_own_call_is_internal() -> None:
    inner: Any = Connector(Inventory, Counting(_unavailable))

    def reply(request: Reserve) -> Reserved:
        return cast("Reserved", inner.reserve(request))

    with pytest.raises(ContractError) as raised:
        _connect(Recording(reply)).reserve(_reserve())
    _assert_internal(raised)


@pytest.mark.spec("TRN-3")
def test_an_exception_a_transport_raises_reaches_the_caller_as_internal() -> None:
    def broken(action: Action, request: bytes) -> bytes | None:
        raise Secret("socket details")

    with pytest.raises(ContractError) as raised:
        Connector(Inventory, Counting(broken)).reserve(_reserve())
    _assert_internal(raised)


# The provider is checked against its contract when it is built.


class Missing:
    def reserve(self, request: Reserve) -> Reserved:
        return Reserved(ok=True)


@pytest.mark.spec("CON-17")
def test_an_implementation_that_does_not_meet_its_contract_is_refused() -> None:
    with pytest.raises(ContractDefinitionError, match="action 'release' is missing"):
        InProcess(Inventory, Missing())


@pytest.mark.spec("TRN-15")
def test_an_attribute_set_on_the_instance_does_not_replace_an_action() -> None:
    provider = Recording()
    provider.reserve = lambda request: Reserved(ok=False)  # type: ignore[method-assign]
    assert _connect(provider).reserve(_reserve()).ok is True


@pytest.mark.spec("TRN-15")
def test_an_action_of_another_contract_is_refused_by_the_transport() -> None:
    @contract("other.v1")
    class Other(Protocol):
        @action
        def reserve(self, request: Reserve) -> Reserved: ...

    transport = InProcess(Inventory, Recording())
    with pytest.raises(TypeError, match="has no action 'reserve' of this declaration"):
        transport.call(Contract.of(Other).actions["reserve"], b"{}")


# Invariant: whatever the description admits crosses there and back unchanged.


def _echo(record: type[BaseModel]) -> tuple[Any, list[BaseModel]]:
    def send(self: object, request: record) -> record: ...  # type: ignore[valid-type]

    declared = types.new_class(
        _name("E"), (Protocol,), exec_body=lambda ns: ns.update(send=action(send))
    )
    contract(f"{_name('echo')}.v1")(declared)
    received: list[BaseModel] = []

    def echo(self: object, request: BaseModel) -> BaseModel:
        received.append(request)
        return request

    echo.__annotations__ = {"request": record, "return": record}
    implementation = type(_name("Echo"), (), {"send": echo})()
    return Connector(declared, InProcess(declared, implementation)), received


@pytest.mark.spec("TRN-1", "TRN-5", "TRN-15")
@_HYPOTHESIS
@given(st.data())
def test_generated_values_cross_to_the_provider_and_back_unchanged(
    data: st.DataObject,
) -> None:
    from tests.test_contract_round_trip import Reference

    record = cast(
        "type[BaseModel]",
        data.draw(st.just(Reference) | _records(_TYPES).map(lambda r: r[0])),
    )
    try:
        connector, received = _echo(record)
    except ContractDefinitionError:
        return
    description = Contract.of(connector._contract.declaration).describe()
    schema = {"$defs": description["$defs"], "$ref": f"#/$defs/{record.__name__}"}
    sent = record.model_validate_json(json.dumps(data.draw(from_schema(schema))))
    reply = connector.send(sent)
    assert type(reply) is record
    assert reply == sent
    assert _shared(sent, received[0]) == set()
    assert _shared(received[0], reply) == set()
