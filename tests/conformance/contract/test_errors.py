"""Contract part: an error crosses alike (CNF-1, spec 01).

The cases call ``reference.v1``'s ``refuse``, ``leak`` and ``boom`` from the
reference apps of the test host, over the in-process and the HTTP transport.
Each leg's error is judged on its own, not only the one a case returns: its
kind, its message, its detail record by type and by data, and, for a failure
that stands for another, that it carries nothing of it. Each cites CNF-1, and
HTTP-1: the HTTP transport, with the in-process one, is what it runs over.
"""

import logging
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

import neolith
from neolith import ContractError, _connector
from tests.conformance.contract.conftest import HYPOTHESIS, OverBoth, form
from tests.host_apps.reference.contract import Empty, Gone, Refusal, RefusalKind, Values

CALLER = "reference_caller"

PARITY = pytest.mark.spec("CNF-1", "HTTP-1")


_VALUES = Values.model_validate_json(
    '{"boolean":true,"integer":1,"number":1.5,"string":"s","bytes":"AA==",'
    '"decimal":"1.50","timestamp":"2027-01-01T00:00:00Z","date":"2027-01-01",'
    '"duration":"1s","list":[1],"map":{"a":1},"record":{"label":"l"},'
    '"enum":"green"}'
)


def _refuse(
    over_both: OverBoth, kind: RefusalKind, message: str
) -> list[BaseException]:
    """Ask ``refuse`` for an error; each leg's error."""
    errors: list[BaseException] = []

    def call() -> object:
        with neolith.command_scope(CALLER):
            try:
                return neolith.connect("reference.v1").refuse(
                    Refusal(kind=kind, message=message)
                )
            except ContractError as error:
                errors.append(error)
                raise

    over_both(call)
    return errors


def _delivered(errors: list[BaseException], kind: RefusalKind, message: str) -> None:
    assert len(errors) == 2
    for error in errors:
        assert type(error) is ContractError
        assert (error.kind, error.message) == (kind.value, message)
        if kind is RefusalKind.GONE:
            # The detail is the kind's record, with the request's message.
            assert form(error.data) == form(Gone(reason=message))
        else:
            assert error.data is None


_MESSAGES = {
    "empty": "",
    "one character": "x",
    "quotes and a backslash": 'a "quoted" \\ message',
    "controls": "a\x00b\x1f\n\r\t",
    "line separators": chr(0x2028) + chr(0x2029),
    "U+FFFF": "￿",
    "astral": "\U0001f600\U0010ffff",
    "composed and decomposed": "é é",
    "long": "m" * 100_000,
}


@PARITY
@pytest.mark.spec("CON-43", "CON-25", "CON-26", "CON-27")
@pytest.mark.conformance_case("error_message_at_its_edges")
@pytest.mark.parametrize("kind", list(RefusalKind), ids=lambda kind: kind.value)
@pytest.mark.parametrize("message", list(_MESSAGES))
def test_an_error_message_arrives_as_it_was_raised(
    over_both: OverBoth, kind: RefusalKind, message: str
) -> None:
    _delivered(_refuse(over_both, kind, _MESSAGES[message]), kind, _MESSAGES[message])


@PARITY
@pytest.mark.spec("CON-43", "CON-25", "CON-26", "CON-27")
@pytest.mark.conformance_case("error_generated")
@HYPOTHESIS
@given(kind=st.sampled_from(list(RefusalKind)), message=st.text(max_size=300))
def test_any_error_of_the_action_arrives_as_it_was_raised(
    over_both: OverBoth, kind: RefusalKind, message: str
) -> None:
    # st.text draws no lone surrogate, which no message may hold (CON-25).
    _delivered(_refuse(over_both, kind, message), kind, message)


@PARITY
@pytest.mark.spec("CON-44", "CON-46", "TRN-18")
@pytest.mark.conformance_case("failure_is_internal_and_names_nothing")
@pytest.mark.parametrize("action", ["boom", "leak"])
def test_a_failure_arrives_as_internal_with_nothing_of_it_over_each_leg(
    over_both: OverBoth, action: str, caplog: pytest.LogCaptureFixture
) -> None:
    errors: list[ContractError] = []

    def call() -> object:
        with neolith.command_scope(CALLER, timeout=10):
            try:
                return getattr(neolith.connect("reference.v1"), action)(Empty())
            except ContractError as error:
                errors.append(error)
                raise

    with caplog.at_level(logging.ERROR, "neolith"):
        over_both(call)
    assert len(errors) == 2
    for error in errors:
        assert type(error) is ContractError
        assert error.kind == "internal"
        assert error.data is None
        # Fixed text and a trace identifier, and nothing of the failure: not
        # its text, its type, nor the type of the reply that was refused.
        trace = error.message.rsplit(" ", 1)[-1]
        assert error.message == _connector.internal_message(trace)
        assert len(trace) == 32
        assert error.__cause__ is None
        assert error.__context__ is None
    # Each leg's host recorded the failure under that leg's trace identifier.
    logged = " ".join(record.getMessage() for record in caplog.records)
    for error in errors:
        assert error.message.rsplit(" ", 1)[-1] in logged


@PARITY
@pytest.mark.spec("CON-43", "TRN-3")
@pytest.mark.conformance_case("error_then_reply")
def test_a_scope_that_met_an_error_goes_on_calling_alike(over_both: OverBoth) -> None:
    def call() -> object:
        with neolith.command_scope(CALLER):
            connector = neolith.connect("reference.v1")
            with pytest.raises(ContractError) as raised:
                connector.refuse(Refusal(kind=RefusalKind.CONFLICT, message="no"))
            return raised.value.kind, connector.echo(_VALUES)

    outcome = over_both(call)
    kind, reply = cast("tuple[str, object]", outcome.reply)
    assert kind == "conflict"
    assert form(reply) == form(_VALUES)
