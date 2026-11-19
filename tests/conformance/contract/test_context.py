"""Contract part: the call context crosses alike (CNF-1, spec 02, spec 06
item 7).

The cases call ``reference.v1`` from the reference apps of the test host, over
the in-process and the HTTP transport. A call finds the scope it derives its
context from, or is refused as a mistake in the calling code, before anything
is sent. A principal is judged once, at the caller, and the provider receives
the one its sent form holds. Each leg's own outcome is judged, not only the
one a case returns. Each cites CNF-1, and HTTP-1: the HTTP transport, with the
in-process one, is what it runs over.
"""

import contextvars
import dataclasses
import json
import logging
import secrets
import threading
import uuid
from collections.abc import Callable

import pytest

import neolith
from neolith import ContractError, Principal, _connector, _context
from neolith._contracts import is_received
from tests.conformance.contract.conftest import OverBoth, balance_of
from tests.host_apps.reference.contract import Deposit, Empty, Seen, SeenPrincipal

CALLER = "reference_caller"
REQUIRES = ("reference.v1",)

PARITY = pytest.mark.spec("CNF-1", "HTTP-1")


def _in_a_thread(call: Callable[[], object]) -> object:
    """Run ``call`` in a thread and hand back its outcome or raise its error."""
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as error:  # the case reads it
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def _closed_scope() -> _context.Scope:
    with _context.root(CALLER, requires=REQUIRES) as scope:
        return scope


def _closed_ambient() -> contextvars.Context:
    """A copy of the context made inside a root, taken out after it closed."""
    with _context.root(CALLER, requires=REQUIRES):
        return contextvars.copy_context()


# Every way a connector call finds no open scope to derive its context from,
# and the text that says which. Each takes the request the call carries.
_NO_OPEN_SCOPE: dict[str, tuple[Callable[[Deposit], object], str]] = {
    "no scope": (
        lambda request: contextvars.Context().run(_deposit, request),
        "needs a call context",
    ),
    "no scope, in a thread": (
        lambda request: _in_a_thread(
            lambda: contextvars.Context().run(_deposit, request)
        ),
        "needs a call context",
    ),
    "context= naming a closed scope": (
        lambda request: _deposit(request, context=_closed_scope().context),
        "names no open scope",
    ),
    "context= naming a scope this host did not issue": (
        lambda request: _deposit(
            request,
            context=dataclasses.replace(
                _closed_scope().context, scope_id=secrets.token_hex(16)
            ),
        ),
        "names no open scope",
    ),
    "a closed ambient scope copied into a context": (
        lambda request: _closed_ambient().run(_deposit, request),
        "has ended",
    ),
    "a closed ambient scope copied into a thread": (
        lambda request: _in_a_thread(lambda: _closed_ambient().run(_deposit, request)),
        "has ended",
    ),
}


def _deposit(request: Deposit, **options: object) -> object:
    return neolith.connect("reference.v1").deposit(request, **options)


@PARITY
@pytest.mark.spec("TRN-23", "TRN-25", "TRN-26", "CON-29")
@pytest.mark.conformance_case("call_with_no_open_scope")
@pytest.mark.parametrize("situation", list(_NO_OPEN_SCOPE))
def test_a_call_with_no_open_scope_is_a_usage_error_and_is_never_sent(
    over_both: OverBoth, situation: str
) -> None:
    call, message = _NO_OPEN_SCOPE[situation]
    accounts: list[str] = []
    errors: list[BaseException] = []

    def case() -> object:
        accounts.append(uuid.uuid4().hex)
        try:
            return call(Deposit(account=accounts[-1], amount=1))
        except Exception as error:
            errors.append(error)
            raise

    over_both(case)
    assert len(errors) == 2
    for error in errors:
        # A mistake in the calling code, not an outcome of the call (CON-29).
        assert type(error) is neolith.UsageError
        assert message in str(error)
    assert [balance_of(account) for account in accounts] == [0, 0]


# 3072 bytes of JSON is the 4096 bytes of base64url a sent principal may have.
_FITS = 3072 - len(b'{"issuer":"i","subject":""}')

_NOT_VALID = {
    "empty subject": Principal("i", ""),
    "empty issuer": Principal("", "s"),
    "lone surrogate": Principal("i", "s\ud800"),
    "lone surrogate in a list": Principal("i", "s", {"a": ["\udfff"]}),
    "integer beyond the range": Principal("i", "s", {"a": 2**53}),
    "sent form of 4097": Principal("i", "s" * (_FITS + 1)),
}


@PARITY
@pytest.mark.spec("CON-51", "CON-46")
@pytest.mark.conformance_case("principal_not_valid")
@pytest.mark.parametrize("which", list(_NOT_VALID))
def test_a_call_whose_principal_is_not_valid_fails_internal_at_the_caller(
    over_both: OverBoth, which: str, caplog: pytest.LogCaptureFixture
) -> None:
    accounts: list[str] = []
    seen: list[tuple[str, ContractError]] = []

    def case() -> object:
        accounts.append(uuid.uuid4().hex)
        with _context.root(CALLER, requires=REQUIRES, principal=_NOT_VALID[which]) as s:
            try:
                return _deposit(Deposit(account=accounts[-1], amount=1))
            except ContractError as error:
                seen.append((s.context.trace_id, error))
                raise

    with caplog.at_level(logging.ERROR, "neolith"):
        over_both(case)
    assert len(seen) == 2
    for trace, error in seen:
        # Fixed text and the trace identifier, and nothing of the cause.
        assert type(error) is ContractError
        assert (error.kind, error.message, error.data) == (
            "internal",
            _connector.internal_message(trace),
            None,
        )
        assert error.__cause__ is None
        assert error.__context__ is None
        assert is_received(error)
    # Never sent, in either mode.
    assert [balance_of(account) for account in accounts] == [0, 0]
    # Each leg's caller recorded the cause under its trace identifier, once.
    records = [record for record in caplog.records if record.name == "neolith"]
    assert len(records) == 2
    for record in records:
        assert "principal is not valid" in record.getMessage()
        assert record.exc_info is not None
        assert type(record.exc_info[1]) is ValueError
    for trace, _ in seen:
        assert sum(trace in record.getMessage() for record in records) == 1


_VALID = {
    "exactly 4096": Principal("i", "s" * _FITS),
    "U+FFFF": Principal("i￿", "￿", {"￿": ["￿"]}),
    "every kind of claim": Principal(
        "i", "s", {"a": 2**53 - 1, "b": True, "c": [], "d": ""}
    ),
}


@PARITY
@pytest.mark.spec("CON-51", "TRN-30", "TRN-36", "OVR-6")
@pytest.mark.conformance_case("principal_read_from_its_sent_form")
@pytest.mark.parametrize("which", list(_VALID))
def test_a_valid_principal_reaches_the_provider_read_from_its_sent_form(
    over_both: OverBoth, which: str
) -> None:
    principal = _VALID[which]
    # The sent form reads back as the principal that was sent.
    assert _context.read_principal(_context.principal_form(principal)) == principal
    replies: list[object] = []

    def case() -> object:
        with neolith.command_scope(CALLER, principal=principal):
            replies.append(neolith.connect("reference.v1").seen(Empty()))
        return replies[-1]

    over_both(case)
    assert len(replies) == 2
    for reply in replies:
        assert isinstance(reply, Seen)
        assert reply.principal == SeenPrincipal(
            issuer=principal.issuer,
            subject=principal.subject,
            claims=json.dumps(
                dict(principal.claims), sort_keys=True, ensure_ascii=False
            ),
        )
