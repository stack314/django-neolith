"""Routing part: a host of its own, configured through settings with one or
more remote providers per identity (ADR 0032 Decisions 2 and 8).

One process, ``child.main``, binds a set of scripted providers per case
(``tests/scripted_providers.py``), configures its one calling app in ADR
0031 Decision 2's form, makes each case's calls, and prints one record per
case: the outcome of each call, and the requests each provider recorded,
with every secret, principal and key as a position. Each test here asserts
on one case's record. The cases, their outcomes and the IDs they cite are
the rows of ADR 0031's Decision 9 table, listed in
``spec/conformance/cases/routing.json``. HTTP only: routing among remote
providers is the subject (T2).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from tests.conformance.routing.child import CASES

# What the process is run with: nothing else, so no secret is in argv.
COMMAND: Final = (
    sys.executable,
    "-W",
    "error",
    "-c",
    "from tests.conformance.routing import child; child.main()",
)
# The bound on every subprocess of the suite (ADR 0032 Decision 11).
BOUND: Final = 120
CASE_FILE: Final = Path(__file__).parents[3] / "spec/conformance/cases/routing.json"
POSITION: Final = re.compile(r"(token|principal|key|tracestate) [1-9][0-9]*")
HIDDEN: Final = ("authorization", "neolith-principal", "idempotency-key", "tracestate")
UNAVAILABLE_503: Final = re.compile(
    r"the provider at http://127\.0\.0\.1:[0-9]+ answered 503 without a neolith "
    r"error body"
)
REPLY: Final = {"reply": {"n": 7}, "type": "Answer"}


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    """Every case's record, from one run of the routing part's host."""
    env = {"PYTHONPATH": os.pathsep.join(sys.path)}
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    ran = subprocess.run(  # noqa: S603
        COMMAND,
        capture_output=True,
        text=True,
        check=False,
        timeout=BOUND,
        env=env,
    )
    # A token and a principal's sent form are base64url of JSON, which
    # starts eyJ; neither may reach the output, nor may a credential. Checked
    # before a failure prints the output.
    for said in (ran.stdout, ran.stderr):
        assert "eyJ" not in said
        assert "Bearer" not in said
    assert ran.returncode == 0, ran.stderr[-3000:]
    *cases, last = (json.loads(line) for line in ran.stdout.splitlines())
    records = {case.pop("name"): case for case in cases}
    return {"cases": records, **last}


def case(run: dict[str, Any], name: str) -> dict[str, Any]:
    """The record of case ``name``, whose providers met no error."""
    found: dict[str, Any] = run["cases"][name]
    assert found["errors"] == [], found["errors"]
    return found


def seen(record: dict[str, Any]) -> list[str]:
    """Which providers received a request, in the order received, and for
    which action.
    """
    return [
        f"{r['provider']} {r['line'].split()[1].rpartition('/')[2]}"
        for r in record["requests"]
    ]


def field(request: dict[str, Any], name: str) -> str | None:
    value: str | None = request["fields"].get(name)
    return value


def span(request: dict[str, Any]) -> str:
    traceparent = field(request, "traceparent")
    assert traceparent is not None
    return traceparent.split("-")[2]


def trace(request: dict[str, Any]) -> str:
    traceparent = field(request, "traceparent")
    assert traceparent is not None
    return traceparent.split("-")[1]


def one_outcome(record: dict[str, Any]) -> dict[str, Any]:
    (outcome,) = record["outcomes"]
    found: dict[str, Any] = outcome
    return found


def same_context(first: dict[str, Any], second: dict[str, Any]) -> None:
    """TRN-71: the same principal, trace, ``tracestate`` and key; a span
    and a token for each attempt.
    """
    for name in ("neolith-principal", "idempotency-key", "tracestate"):
        assert field(first, name) is not None, name
        assert field(first, name) == field(second, name), name
    assert trace(first) == trace(second)
    assert span(first) != span(second)
    # A token minted for each attempt; two minted in one second are the same
    # bytes, so only their presence is compared.
    assert field(first, "authorization") is not None
    assert field(second, "authorization") is not None


# The run as a whole.


def test_the_run_holds_exactly_the_cases_the_case_file_lists(
    run: dict[str, Any],
) -> None:
    listed = [entry["name"] for entry in json.loads(CASE_FILE.read_text())]
    assert listed == [c.name for c in CASES]
    assert list(run["cases"]) == listed


def test_secrets_principals_and_keys_are_recorded_as_positions_only(
    run: dict[str, Any],
) -> None:
    for record in run["cases"].values():
        for request in record["requests"]:
            for name in HIDDEN:
                value = field(request, name)
                assert value is None or POSITION.fullmatch(value), (name, value)
    # Nothing printed or logged holds a value a position stands for, or the
    # signing secret, and the host did log.
    assert run["leaks"] == []
    assert run["logged"] > 0


def test_the_process_is_given_no_secret_in_argv(run: dict[str, Any]) -> None:
    # What the child received: Python's own "-c" and nothing else. It
    # generates its signing secret itself.
    assert run["argv"] == ["-c"]


# Under single (ADR 0031 Decision 1).


@pytest.mark.conformance_case("routing_single_one_attempt")
@pytest.mark.spec("TRN-17", "APP-63")
def test_single_makes_one_attempt(run: dict[str, Any]) -> None:
    record = case(run, "routing_single_one_attempt")
    outcome = one_outcome(record)
    assert outcome["kind"] == "unavailable"
    assert UNAVAILABLE_503.fullmatch(outcome["message"])
    assert seen(record) == ["U get"]


@pytest.mark.conformance_case("routing_single_refused")
@pytest.mark.spec("TRN-17", "APP-63")
def test_single_refused_connection_makes_no_other_attempt(run: dict[str, Any]) -> None:
    record = case(run, "routing_single_refused")
    assert one_outcome(record)["kind"] == "unavailable"
    assert seen(record) == []


# Moves on unavailable, for an idempotent action (ADR 0031 Decision 4).


@pytest.mark.conformance_case("routing_moves_on_503")
@pytest.mark.spec("TRN-8", "TRN-68", "TRN-71")
def test_moves_on_503(run: dict[str, Any]) -> None:
    record = case(run, "routing_moves_on_503")
    assert one_outcome(record) == REPLY
    assert seen(record) == ["U get", "B get"]
    first, second = record["requests"]
    same_context(first, second)
    # The clock stood still until U moved it 5 ms, so the budget read for B
    # is exactly 5 less than for U.
    at_u, at_b = field(first, "neolith-timeout"), field(second, "neolith-timeout")
    assert at_u is not None
    assert at_b is not None
    assert int(at_b) == int(at_u) - 5


@pytest.mark.conformance_case("routing_moves_on_refused_connection")
@pytest.mark.spec("TRN-8", "TRN-68")
def test_moves_on_refused_connection(run: dict[str, Any]) -> None:
    record = case(run, "routing_moves_on_refused_connection")
    assert one_outcome(record) == REPLY
    assert seen(record) == ["B get"]


@pytest.mark.conformance_case("routing_moves_on_unavailable_body")
@pytest.mark.spec("TRN-8")
def test_moves_on_an_unavailable_error_body(run: dict[str, Any]) -> None:
    record = case(run, "routing_moves_on_unavailable_body")
    assert one_outcome(record) == REPLY
    assert seen(record) == ["N get", "B get"]


@pytest.mark.conformance_case("routing_moves_on_reset_after_read")
@pytest.mark.spec("TRN-8", "TRN-71")
def test_moves_on_reset_after_read(run: dict[str, Any]) -> None:
    record = case(run, "routing_moves_on_reset_after_read")
    assert one_outcome(record) == REPLY
    assert seen(record) == ["X get", "B get"]
    first, second = record["requests"]
    assert field(first, "idempotency-key") is not None
    assert field(first, "idempotency-key") == field(second, "idempotency-key")


# A non-idempotent call stays (CON-4).


@pytest.mark.conformance_case("routing_put_stays_after_503")
@pytest.mark.spec("CON-4", "TRN-8")
def test_put_stays_after_503(run: dict[str, Any]) -> None:
    record = case(run, "routing_put_stays_after_503")
    outcome = one_outcome(record)
    assert outcome["kind"] == "unavailable"
    assert UNAVAILABLE_503.fullmatch(outcome["message"])
    assert seen(record) == ["U put"]


@pytest.mark.conformance_case("routing_put_stays_after_refused_connection")
@pytest.mark.spec("CON-4", "TRN-8")
def test_put_stays_after_refused_connection(run: dict[str, Any]) -> None:
    record = case(run, "routing_put_stays_after_refused_connection")
    assert one_outcome(record)["kind"] == "unavailable"
    assert seen(record) == []


@pytest.mark.conformance_case("routing_put_stays_after_reset_after_read")
@pytest.mark.spec("CON-4", "TRN-8")
def test_put_stays_after_reset_after_read(run: dict[str, Any]) -> None:
    record = case(run, "routing_put_stays_after_reset_after_read")
    assert one_outcome(record)["kind"] == "unavailable"
    assert seen(record) == ["X put"]


# Any other kind stays, and is passed on as the provider gave it.


def _passed_on(run: dict[str, Any], name: str, kind: str) -> None:
    record = case(run, name)
    outcome = one_outcome(record)
    assert outcome["kind"] == kind
    assert outcome["message"] == f"the scripted provider answers {kind}"
    assert seen(record) == ["E get"]


@pytest.mark.conformance_case("routing_stays_on_timeout")
@pytest.mark.spec("TRN-8", "TRN-72")
def test_stays_on_timeout(run: dict[str, Any]) -> None:
    _passed_on(run, "routing_stays_on_timeout", "timeout")


@pytest.mark.conformance_case("routing_stays_on_unauthenticated")
@pytest.mark.spec("TRN-8", "TRN-72")
def test_stays_on_unauthenticated(run: dict[str, Any]) -> None:
    _passed_on(run, "routing_stays_on_unauthenticated", "unauthenticated")


@pytest.mark.conformance_case("routing_stays_on_permission_denied")
@pytest.mark.spec("TRN-8", "TRN-72")
def test_stays_on_permission_denied(run: dict[str, Any]) -> None:
    _passed_on(run, "routing_stays_on_permission_denied", "permission_denied")


@pytest.mark.conformance_case("routing_stays_on_conflict")
@pytest.mark.spec("TRN-8", "TRN-72")
def test_stays_on_conflict(run: dict[str, Any]) -> None:
    _passed_on(run, "routing_stays_on_conflict", "conflict")


@pytest.mark.conformance_case("routing_stays_on_internal")
@pytest.mark.spec("TRN-8", "TRN-72")
def test_stays_on_internal(run: dict[str, Any]) -> None:
    _passed_on(run, "routing_stays_on_internal", "internal")


@pytest.mark.conformance_case("routing_stays_on_declared_kind")
@pytest.mark.spec("TRN-8", "TRN-72")
def test_stays_on_a_declared_kind(run: dict[str, Any]) -> None:
    _passed_on(run, "routing_stays_on_declared_kind", "gone")
    outcome = one_outcome(case(run, "routing_stays_on_declared_kind"))
    assert outcome["detail"] == {"reason": "the scripted provider says gone"}


@pytest.mark.conformance_case("routing_invalid_reply_stays")
@pytest.mark.spec("TRN-8", "TRN-18")
def test_an_invalid_reply_stays_as_internal(run: dict[str, Any]) -> None:
    record = case(run, "routing_invalid_reply_stays")
    assert one_outcome(record)["kind"] == "internal"
    assert seen(record) == ["V get"]


@pytest.mark.conformance_case("routing_silent_provider_times_out")
@pytest.mark.spec("TRN-8", "TRN-38")
def test_a_silent_provider_times_out(run: dict[str, Any]) -> None:
    record = case(run, "routing_silent_provider_times_out")
    assert one_outcome(record)["kind"] == "timeout"
    assert seen(record) == ["S get"]


# The deadline, and the order (ADR 0031 Decisions 5 and 7).


@pytest.mark.conformance_case("routing_no_time_left_ends_unavailable")
@pytest.mark.spec("TRN-70", "TRN-72")
def test_no_time_left_ends_with_unavailable(run: dict[str, Any]) -> None:
    record = case(run, "routing_no_time_left_ends_unavailable")
    outcome = one_outcome(record)
    assert outcome["kind"] == "unavailable"
    assert UNAVAILABLE_503.fullmatch(outcome["message"])
    assert seen(record) == ["U get"]


@pytest.mark.conformance_case("routing_last_entry_unavailable")
@pytest.mark.spec("TRN-69", "TRN-72")
def test_the_last_entry_s_unavailable_ends_the_call(run: dict[str, Any]) -> None:
    record = case(run, "routing_last_entry_unavailable")
    outcome = one_outcome(record)
    assert outcome["kind"] == "unavailable"
    # The outcome is the last attempt's: it names D, the second provider.
    assert outcome["message"].startswith(f"the provider at {record['providers']['D']} ")
    assert seen(record) == ["U get", "D get"]


@pytest.mark.conformance_case("routing_moves_with_no_deadline")
@pytest.mark.spec("TRN-8")
def test_moves_with_no_deadline(run: dict[str, Any]) -> None:
    record = case(run, "routing_moves_with_no_deadline")
    assert one_outcome(record) == REPLY
    assert seen(record) == ["U get", "B get"]
    assert [field(r, "neolith-timeout") for r in record["requests"]] == [None, None]


@pytest.mark.conformance_case("routing_each_call_starts_first")
@pytest.mark.spec("TRN-68")
def test_each_call_starts_from_the_first_provider(run: dict[str, Any]) -> None:
    record = case(run, "routing_each_call_starts_first")
    assert record["outcomes"] == [REPLY, REPLY]
    assert seen(record) == ["U get", "B get", "U get", "B get"]
    first, second, third, fourth = record["requests"]
    # Each call's two attempts carry its key; the two calls' keys differ.
    assert field(first, "idempotency-key") == field(second, "idempotency-key")
    assert field(third, "idempotency-key") == field(fourth, "idempotency-key")
    assert field(first, "idempotency-key") != field(third, "idempotency-key")
