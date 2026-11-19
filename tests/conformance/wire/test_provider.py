"""Wire part: neolith's provider, driven by the suite's own HTTP caller.

Each case writes its request with ``http.client`` as spec 03 fixes it, to
the package's live server, and asserts on the status, the header fields and
the body's bytes, read as JSON by the suite's own strict reader. The cases
are listed in ``spec/conformance/cases/wire-provider.json``. HTTP only: the
wire is the subject (T2).
"""

from typing import Any

import pytest

from tests.conformance import Live
from tests.conformance.wire import (
    CALLER,
    DECLARED,
    HEALTH,
    OPENAPI,
    PRINCIPAL_HEADER,
    STATUS,
    STRANGER,
    Answer,
    Caller,
    b64url,
    mint,
    read_json,
)


@pytest.fixture
def caller(live: Live) -> Caller:
    return Caller(live.origin, live.settle)


# Every portable form, each required and each optional one set, in the form
# ADR 0019 gives it on the wire.
EVERY = (
    b'{"boolean":true,"integer":9007199254740991,"number":-1.5e300,'
    b'"string":"na\\u00efve \\ud83d\\ude00","bytes":"AP_-","decimal":"-12.340",'
    b'"timestamp":"2027-02-28T23:59:59.999999Z","date":"0001-01-01",'
    b'"duration":"-1.999999s","list":[0,-9007199254740991],"map":{"":0,"k":1},'
    b'"record":{"label":"inner"},"enum":"green",'
    b'"optional_boolean":false,"optional_integer":0,"optional_number":0.5,'
    b'"optional_string":"","optional_bytes":"","optional_decimal":"0.00",'
    b'"optional_timestamp":"9999-12-31T12:00:00-05:00",'
    b'"optional_date":"9999-12-31","optional_duration":"0s",'
    b'"optional_list":[],"optional_map":{},"optional_record":{"label":""},'
    b'"optional_enum":"red"}'
)


def _error(answer: Answer, kind: str, status: int) -> dict[str, Any]:
    """The neolith error body of ``answer``, of ``kind`` and ``status``."""
    assert answer.status == status, answer
    body = answer.error()
    assert body["kind"] == kind, answer
    assert isinstance(body["message"], str), answer
    return body


# A reply.


@pytest.mark.conformance_case("wire_reply_is_200_with_its_json_text")
@pytest.mark.spec("HTTP-41", "TRN-1")
def test_a_reply_is_200_with_its_json_text(caller: Caller) -> None:
    answer = caller.call("echo", EVERY)
    assert answer.status == 200, answer
    assert answer.field("Content-Type") == "application/json"
    assert answer.field("Content-Length") == str(len(answer.body))
    # The same JSON value: the reply may space or write a number otherwise.
    assert read_json(answer.body) == read_json(EVERY)


@pytest.mark.conformance_case("wire_empty_reply_is_204")
@pytest.mark.spec("HTTP-41")
def test_an_empty_reply_is_204_with_no_body(caller: Caller) -> None:
    answer = caller.call("accept", EVERY)
    assert (answer.status, answer.body) == (204, b"")
    assert answer.field("Content-Type") is None
    assert answer.field("Content-Length") in (None, "0")


# What the action ends with.


@pytest.mark.conformance_case("wire_error_has_its_kind_status_and_body")
@pytest.mark.spec("CON-43")
@pytest.mark.parametrize(
    "kind",
    [
        "invalid_argument",
        "unauthenticated",
        "permission_denied",
        "not_found",
        "conflict",
    ],
)
def test_a_standard_kind_has_its_status_and_body(caller: Caller, kind: str) -> None:
    sent = b'{"kind":"%s","message":"said \\u00e9"}' % kind.encode()
    body = _error(caller.call("refuse", sent), kind, STATUS[kind])
    assert body == {"kind": kind, "message": "said é"}


@pytest.mark.conformance_case("wire_error_has_its_kind_status_and_body")
@pytest.mark.spec("CON-43")
def test_a_declared_kind_is_422_with_its_detail(caller: Caller) -> None:
    answer = caller.call("refuse", b'{"kind":"gone","message":"left"}')
    body = _error(answer, "gone", DECLARED)
    assert body == {"kind": "gone", "message": "left", "data": {"reason": "left"}}


@pytest.mark.conformance_case("wire_failure_inside_is_internal")
@pytest.mark.spec("TRN-18")
@pytest.mark.parametrize("action", ["boom", "leak"])
def test_a_failure_inside_the_provider_is_internal(caller: Caller, action: str) -> None:
    body = _error(caller.call(action), "internal", 500)
    assert set(body) == {"kind", "message"}


@pytest.mark.conformance_case("wire_deadline_passed_is_timeout")
@pytest.mark.spec("TRN-4")
def test_a_call_past_its_budget_is_timeout(caller: Caller) -> None:
    answer = caller.call("stall", fields=[("Neolith-Timeout", "200")])
    body = _error(answer, "timeout", 504)
    assert set(body) == {"kind", "message"}


# The calling app and the principal.


@pytest.mark.conformance_case("wire_unauthenticated_carries_the_bearer_challenge")
@pytest.mark.spec(
    "HTTP-9",
    "HTTP-10",
    "HTTP-12",
    "HTTP-13",
    "HTTP-14",
    "HTTP-15",
    "HTTP-16",
    "HTTP-44",
    "HTTP-45",
)
@pytest.mark.parametrize(
    "credential",
    [
        pytest.param(None, id="none"),
        pytest.param("Basic dXNlcjpwYXNz", id="another-scheme"),
        pytest.param({"header": {"alg": "HS384"}}, id="alg"),
        pytest.param({"header": {"typ": "JWT"}}, id="typ"),
        pytest.param({"header": {"kid": "other-1"}}, id="kid"),
        pytest.param({"key": bytes(32)}, id="signature"),
        pytest.param({"audience": "other.v1"}, id="audience"),
        pytest.param({"claims": {"iat": 1, "exp": 301}}, id="expired"),
        pytest.param({"claims": {"exp": 2**40}}, id="exp-past-300-s"),
        pytest.param({"claims": {"sub": "u"}}, id="another-claim"),
    ],
)
def test_a_call_without_a_valid_credential_is_401_with_one_challenge(
    caller: Caller, credential: str | dict[str, Any] | None
) -> None:
    # Minted when the case runs, so that only the row's own rule fails.
    if isinstance(credential, dict):
        credential = "Bearer " + mint(CALLER, **credential)
    fields = [("Content-Type", "application/json"), ("Content-Length", "2")]
    if credential is not None:
        fields.append(("Authorization", credential))
    answer = caller.request("POST", "/reference/v1/seen", fields, b"{}")
    body = _error(answer, "unauthenticated", 401)
    assert set(body) == {"kind", "message"}
    assert answer.values("WWW-Authenticate") == ['Bearer realm="neolith"']


@pytest.mark.conformance_case("wire_valid_credential_names_the_calling_app")
@pytest.mark.spec("HTTP-11", "TRN-36")
@pytest.mark.parametrize(
    "typ", ["neolith+jwt", "application/neolith+jwt", "NEOLITH+JWT"]
)
def test_a_valid_token_names_its_calling_app(caller: Caller, typ: str) -> None:
    token = mint(CALLER, header={"typ": typ})
    answer = caller.request(
        "POST",
        "/reference/v1/seen",
        [
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Authorization", f"bearer {token}"),
        ],
        b"{}",
    )
    assert answer.status == 200, answer
    assert read_json(answer.body) == {
        "principal": None,
        "caller": CALLER,
        "deadline": False,
        "idempotency_key": False,
    }


@pytest.mark.conformance_case("wire_principal_reaches_the_action")
@pytest.mark.spec("TRN-36")
def test_a_listed_caller_s_principal_reaches_the_action(caller: Caller) -> None:
    answer = caller.call("seen", fields=[("Neolith-Principal", PRINCIPAL_HEADER)])
    assert answer.status == 200, answer
    seen = read_json(answer.body)
    principal = seen.pop("principal")
    assert seen == {"caller": CALLER, "deadline": False, "idempotency_key": False}
    # The claims arrive as JSON text, judged by its value, not its spelling.
    claims = read_json(principal.pop("claims").encode())
    assert principal == {"issuer": "https://id.example.com", "subject": "u-81f3"}
    assert claims == {"roles": ["clerk"]}


@pytest.mark.conformance_case("wire_principal_from_an_unlisted_app_is_403")
@pytest.mark.spec("HTTP-46", "TRN-43")
@pytest.mark.parametrize(
    "principal",
    [PRINCIPAL_HEADER, "", "!!", b64url(b'{"issuer":"i"}'), "A" * 5000],
    ids=["valid", "empty", "not-base64url", "not-a-principal", "too-long"],
)
def test_an_unlisted_app_asserting_any_principal_is_403(
    caller: Caller, principal: str
) -> None:
    answer = caller.call(
        "seen", app=STRANGER, fields=[("Neolith-Principal", principal)]
    )
    _error(answer, "permission_denied", 403)


@pytest.mark.conformance_case("wire_principal_from_an_unlisted_app_is_403")
@pytest.mark.spec("HTTP-46", "TRN-43")
def test_an_unlisted_app_with_no_principal_is_served(caller: Caller) -> None:
    answer = caller.call("seen", app=STRANGER)
    assert answer.status == 200, answer
    assert read_json(answer.body)["caller"] == STRANGER


@pytest.mark.conformance_case("wire_malformed_principal_is_invalid_argument")
@pytest.mark.spec("HTTP-46")
@pytest.mark.parametrize(
    "principal",
    [
        "",
        "!!",
        b64url(b'{"issuer":"i"}'),
        b64url(b'{"issuer":"i","subject":""}'),
        b64url(b'{"issuer":"i","subject":"s","extra":1}'),
        b64url(b'{"issuer":"i","subject":"s","subject":"t"}'),
        PRINCIPAL_HEADER + "=",
    ],
    ids=[
        "empty",
        "not-base64url",
        "no-subject",
        "empty-subject",
        "another-member",
        "a-member-twice",
        "padded",
    ],
)
def test_a_listed_app_s_malformed_principal_is_400(
    caller: Caller, principal: str
) -> None:
    answer = caller.call("seen", fields=[("Neolith-Principal", principal)])
    _error(answer, "invalid_argument", 400)


# The call context's headers.


@pytest.mark.conformance_case("wire_context_headers_reach_the_action")
@pytest.mark.spec("HTTP-21", "HTTP-22", "HTTP-23", "TRN-36")
@pytest.mark.parametrize(
    ("timeout", "key"),
    [
        ("5000", '"k"'),
        ("9999999999", '"' + "k" * 255 + '"'),
        ("1000", '"a\\"b"'),
        # 255 characters once unescaped, 257 as written between the quotes.
        ("1000", '"' + "k" * 253 + '\\"\\\\"'),
        # OWS at either end is not part of the value (HTTP-21).
        (" 1000\t", '\t"k" '),
    ],
    ids=["shortest", "longest", "escaped", "longest-escaped", "ows"],
)
def test_a_budget_and_a_key_in_their_forms_reach_the_action(
    caller: Caller, timeout: str, key: str
) -> None:
    answer = caller.call(
        "seen", fields=[("Neolith-Timeout", timeout), ("Idempotency-Key", key)]
    )
    assert answer.status == 200, answer
    seen = read_json(answer.body)
    assert (seen["deadline"], seen["idempotency_key"]) == (True, True)


@pytest.mark.conformance_case("wire_context_header_outside_its_form_is_400")
@pytest.mark.spec("HTTP-21", "HTTP-22", "HTTP-23")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Neolith-Timeout", "0050"),
        ("Neolith-Timeout", "0"),
        ("Neolith-Timeout", "12345678901"),
        ("Neolith-Timeout", "1.5"),
        ("Neolith-Timeout", ""),
        ("Idempotency-Key", "k"),
        ("Idempotency-Key", '""'),
        ("Idempotency-Key", '"' + "k" * 256 + '"'),
        ("Idempotency-Key", '"k";a=1'),
        # 256 characters once unescaped.
        ("Idempotency-Key", '"' + "k" * 254 + '\\"\\\\"'),
        # Whitespace inside, and a character outside ASCII (HTTP-21). One at
        # either end is #156.
        ("Neolith-Timeout", "10 00"),
        ("Idempotency-Key", '"k\xe9"'),
    ],
)
def test_a_context_header_outside_its_form_is_invalid_argument(
    caller: Caller, name: str, value: str
) -> None:
    _error(caller.call("seen", fields=[(name, value)]), "invalid_argument", 400)


@pytest.mark.conformance_case("wire_unnamed_headers_change_nothing")
@pytest.mark.spec("HTTP-28")
def test_headers_spec_03_does_not_name_change_nothing(caller: Caller) -> None:
    answer = caller.call(
        "seen", fields=[("Neolith-Deadline", "1"), ("X-Idempotency-Key", '"k"')]
    )
    assert answer.status == 200, answer
    assert read_json(answer.body) == {
        "principal": None,
        "caller": CALLER,
        "deadline": False,
        "idempotency_key": False,
    }


# The request's own form.


@pytest.mark.conformance_case("wire_method_other_than_post_is_400")
@pytest.mark.spec("HTTP-34")
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_a_method_other_than_post_is_invalid_argument_with_allow(
    caller: Caller, method: str
) -> None:
    answer = caller.call("seen", method=method)
    _error(answer, "invalid_argument", 400)
    assert answer.values("Allow") == ["POST"]


@pytest.mark.conformance_case("wire_query_is_400")
@pytest.mark.spec("HTTP-35")
def test_a_query_is_invalid_argument(caller: Caller) -> None:
    _error(caller.call("seen", query="a=1"), "invalid_argument", 400)


@pytest.mark.conformance_case("wire_content_type_outside_json_is_400")
@pytest.mark.spec("HTTP-36")
@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/json; charset=latin-1", "application/jsonx", ""],
)
def test_a_content_type_outside_json_is_invalid_argument(
    caller: Caller, content_type: str
) -> None:
    answer = caller.request(
        "POST",
        "/reference/v1/seen",
        [
            ("Content-Type", content_type),
            ("Content-Length", "2"),
            ("Authorization", f"Bearer {mint(CALLER)}"),
        ],
        b"{}",
    )
    _error(answer, "invalid_argument", 400)
    assert answer.values("Accept-Post") == ["application/json"]


@pytest.mark.conformance_case("wire_content_type_outside_json_is_400")
@pytest.mark.spec("HTTP-36")
@pytest.mark.parametrize(
    "content_type",
    ["application/json", "Application/JSON", 'application/json ; charset="UTF-8"'],
)
def test_each_spelling_of_json_is_served(caller: Caller, content_type: str) -> None:
    answer = caller.request(
        "POST",
        "/reference/v1/seen",
        [
            ("Content-Type", content_type),
            ("Content-Length", "2"),
            ("Authorization", f"Bearer {mint(CALLER)}"),
        ],
        b"{}",
    )
    assert answer.status == 200, answer


@pytest.mark.conformance_case("wire_content_encoding_is_400")
@pytest.mark.spec("HTTP-37")
@pytest.mark.parametrize("coding", ["gzip", "identity"])
def test_a_content_encoding_is_invalid_argument(caller: Caller, coding: str) -> None:
    answer = caller.call("seen", fields=[("Content-Encoding", coding)])
    _error(answer, "invalid_argument", 400)
    assert answer.values("Accept-Encoding") == ["identity"]


@pytest.mark.conformance_case("wire_content_length_outside_its_form_is_400")
@pytest.mark.spec("HTTP-38")
@pytest.mark.parametrize("length", [None, "x", "-2", "2.0", "1" + "0" * 19])
def test_a_content_length_outside_its_form_is_invalid_argument(
    caller: Caller, length: str | None
) -> None:
    fields = [
        ("Content-Type", "application/json"),
        ("Authorization", f"Bearer {mint(CALLER)}"),
    ]
    if length is not None:
        fields.append(("Content-Length", length))
    answer = caller.request("POST", "/reference/v1/seen", fields, b"{}")
    _error(answer, "invalid_argument", 400)


# The paths the host owns.


@pytest.mark.conformance_case("wire_owned_path_with_no_action_is_an_empty_404")
@pytest.mark.spec("HTTP-30")
@pytest.mark.parametrize(
    "path",
    ["/reference/v1/none", "/reference/v1/", "/.well-known/neolith/other"],
)
def test_an_owned_path_that_names_no_action_is_an_empty_404(
    caller: Caller, path: str
) -> None:
    answer = caller.request(
        "POST",
        path,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Authorization", f"Bearer {mint(CALLER)}"),
        ],
        b"{}",
    )
    assert (answer.status, answer.body) == (404, b""), answer


@pytest.mark.conformance_case("wire_health_is_an_empty_200")
@pytest.mark.spec("HTTP-47", "HTTP-31")
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_health_is_an_empty_200(caller: Caller, method: str) -> None:
    answer = caller.request(method, HEALTH)
    assert (answer.status, answer.body) == (200, b""), answer


@pytest.mark.conformance_case("wire_document_is_served_with_get_and_head")
@pytest.mark.spec("HTTP-2", "HTTP-31")
def test_the_document_is_served_with_get_and_its_fields_with_head(
    caller: Caller,
) -> None:
    got = caller.request("GET", OPENAPI)
    assert got.status == 200, got
    assert got.field("Content-Type") == "application/json"
    assert got.field("Content-Length") == str(len(got.body))
    assert isinstance(read_json(got.body), dict)
    head = caller.request("HEAD", OPENAPI)
    assert (head.status, head.body) == (200, b""), head
    assert head.field("Content-Type") == "application/json"
    # A server may leave Content-Length out of a HEAD answer (RFC 9110, 9.3.2).
    assert head.field("Content-Length") in (None, str(len(got.body)))


@pytest.mark.conformance_case("wire_well_known_path_other_method_is_405")
@pytest.mark.spec("HTTP-32")
@pytest.mark.parametrize("path", [OPENAPI, HEALTH])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_another_method_on_a_well_known_path_is_405(
    caller: Caller, path: str, method: str
) -> None:
    answer = caller.request(method, path, [("Content-Length", "0")], b"")
    assert (answer.status, answer.body) == (405, b""), answer
    assert answer.values("Allow") == ["GET, HEAD"]


@pytest.mark.conformance_case("wire_well_known_path_with_a_query_is_400")
@pytest.mark.spec("HTTP-33")
@pytest.mark.parametrize("path", [OPENAPI, HEALTH])
def test_a_query_on_a_well_known_path_is_400(caller: Caller, path: str) -> None:
    answer = caller.request("GET", f"{path}?v=2")
    assert (answer.status, answer.body) == (400, b""), answer
