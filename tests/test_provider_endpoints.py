"""Contract endpoints: the host answers every path it owns (spec 03; ADR 0011
as amended for step 5; ADR 0027).

Each case runs on Django's own WSGI and ASGI handlers, not the test client,
with the request's header fields as the server hands them to Django: under
WSGI as wsgiref and Django's server build the environ (repeated fields
joined with ``,``, the first ``Content-Type`` and ``Content-Length`` kept,
and names with ``_`` dropped), under ASGI as a list of fields, which Django
joins itself. A few cases run on a live ``neolith run`` server, whose
request reading is the real one. The host's table is the test host's, with
a contract version and caller backends of these tests published on it.
"""

import contextlib
import json
import logging
import sys
import time
from collections.abc import Callable

import pytest
from django.test import override_settings

from neolith import Contract, _caller, _context, _endpoint, _openapi, _token
from neolith.callers import CallerBackend
from tests.endpoint_site import (
    CHALLENGE,
    CLEAR,
    ENTRY,
    MODES,
    POST,
    PRINCIPAL,
    TRACE,
    TRACEPARENT,
    Abstains,
    Clock,
    Fails,
    Ledger,
    Refuses,
    Returns,
    Site,
    Watches,
    asgi,
    built,
    call,
    capture,
    publish,
    refused,
    set_clock,
    shared,
    token,
    wsgi,
)


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Site:
    return publish(monkeypatch)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return set_clock(monkeypatch)


@pytest.fixture
def calls(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    return capture(caplog)


# A reply, and every outcome the provider side delivers


@pytest.mark.spec("HTTP-41")
@pytest.mark.parametrize("mode", MODES)
def test_a_reply_is_200_with_the_sender_s_text_and_an_empty_one_204(
    site: Site, mode: str
) -> None:
    served = call(mode)
    assert served.status == 200
    assert served.header("Content-Type") == "application/json"
    assert served.body == b'{"amount":5}'
    empty = call(mode, path=CLEAR)
    assert empty.status == 204
    assert empty.body == b""
    assert empty.header("Content-Type") is None


@pytest.mark.spec("HTTP-41", "TRN-36")
@pytest.mark.parametrize("mode", MODES)
def test_the_action_gets_the_context_the_headers_carry(
    site: Site, mode: str, clock: Clock
) -> None:
    served = call(
        mode,
        fields=[
            ("Neolith-Principal", PRINCIPAL),
            ("Neolith-Timeout", "2500"),
            ("Idempotency-Key", '"k-\\"1\\\\"'),
            ("traceparent", TRACEPARENT),
            ("tracestate", "a=1, b=2"),
        ],
    )
    assert served.status == 200
    (seen,) = site.books.seen
    assert seen.caller == "orders"
    assert seen.principal is not None
    assert (seen.principal.issuer, seen.principal.subject) == (
        "https://id.example.com",
        "u-81f3",
    )
    assert seen.trace_id == TRACE
    assert seen.tracestate == "a=1,b=2"
    assert seen.idempotency_key == 'k-"1\\'
    assert seen.deadline == pytest.approx(1002.5)


@pytest.mark.parametrize("mode", MODES)
def test_a_call_with_no_optional_header_has_no_principal_key_or_deadline(
    site: Site, mode: str
) -> None:
    assert call(mode).status == 200
    (seen,) = site.books.seen
    assert seen.principal is None
    assert seen.idempotency_key is None
    assert seen.deadline is None
    assert len(seen.trace_id) == 32


@pytest.mark.parametrize("mode", MODES)
def test_each_kind_the_action_ends_with_keeps_its_status_and_body(
    site: Site, mode: str, calls: pytest.LogCaptureFixture
) -> None:
    declared = call(mode, body=b'{"amount":-3}')
    assert declared.status == 422
    assert declared.error() == {
        "kind": "overdrawn",
        "message": "the account is short",
        "data": {"short": 3},
    }
    standard = call(mode, body=b'{"amount":404}')
    refused(standard, 404, "not_found", "no such account")
    levels = [r.levelno for r in calls.records if r.name == "neolith.calls"]
    assert levels == [logging.INFO, logging.WARNING]


# ADR 0011's table, rows 1 to 13, in order, and ADR 0027 Decision 8's table


@pytest.mark.spec("HTTP-44", "HTTP-45", "TRN-42")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("backends", "authorization", "message"),
    [
        pytest.param([], "orders", _caller.NO_BACKEND, id="no-backend"),
        pytest.param([shared()], None, _caller.NO_AUTHORIZATION, id="no-authorization"),
        pytest.param(
            [shared()], "Basic b3JkZXJzOnB3", _caller.UNRECOGNIZED, id="unrecognized"
        ),
        pytest.param(
            [built(Abstains()), shared()],
            "Bearer x.y.z",
            _caller.UNRECOGNIZED,
            id="all-abstain",
        ),
        pytest.param(
            [built(Refuses("the badge is revoked"))],
            "orders",
            "the badge is revoked",
            id="a-backend-refuses",
        ),
    ],
)
def test_row_1_a_call_no_backend_names_is_unauthenticated_with_the_challenge(
    site: Site,
    mode: str,
    backends: list[_caller.Built],
    authorization: str | None,
    message: str,
) -> None:
    site.backends[:] = backends
    served = call(mode, authorization=authorization)
    refused(served, 401, "unauthenticated", message)
    assert served.headers["www-authenticate"] == [CHALLENGE]
    assert site.books.seen == []


@pytest.mark.spec("HTTP-16", "HTTP-44", "HTTP-45")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("authorization", "message"),
    [
        pytest.param(
            lambda: token(identity="other.v1"), _token.AUDIENCE, id="audience"
        ),
        pytest.param(
            lambda: token(issued=int(time.time()) - 600), _token.EXPIRED, id="expired"
        ),
        pytest.param(lambda: token(key_id="orders-9"), _token.KID, id="key-id"),
    ],
)
def test_row_1_each_rule_of_the_token_gives_its_message(
    site: Site, mode: str, authorization: Callable[[], str], message: str
) -> None:
    served = call(mode, authorization=authorization())
    refused(served, 401, "unauthenticated", message)
    assert served.headers["www-authenticate"] == [CHALLENGE]


@pytest.mark.spec("HTTP-44")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "backend",
    [Fails(), Returns(7), Returns("Not An App")],
    ids=["raises", "returns-an-int", "returns-no-app-name"],
)
def test_row_1_a_backend_that_fails_is_internal_and_its_cause_logged(
    site: Site,
    mode: str,
    backend: CallerBackend,
    calls: pytest.LogCaptureFixture,
) -> None:
    site.backends[:] = [built(backend)]
    served = call(mode)
    assert served.status == 500
    error = served.error()
    assert error["kind"] == "internal"
    assert error["message"].startswith("the call failed inside a host")
    assert "www-authenticate" not in served.headers
    (cause,) = [r for r in calls.records if r.name == "django.neolith"]
    assert cause.levelno == logging.ERROR
    assert not hasattr(cause, "request")
    trace = error["message"].rsplit(" ", 1)[1]
    assert trace in cause.getMessage()


@pytest.mark.spec("HTTP-46", "TRN-43")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "principal",
    [PRINCIPAL, "", "not base64url!", "e30", "W1tbW1tbXV1dXV1d"],
    ids=["valid", "empty", "not-base64url", "not-a-principal", "nested"],
)
def test_row_2_an_unlisted_app_asserting_any_principal_is_permission_denied(
    site: Site, mode: str, principal: str
) -> None:
    served = call(
        mode,
        authorization=token("stranger"),
        fields=[("Neolith-Principal", principal)],
    )
    refused(
        served,
        403,
        "permission_denied",
        "app stranger may not assert a principal on a call to ledger.v1: the "
        "contract's allow-list does not name it",
    )
    assert site.books.seen == []


@pytest.mark.spec("TRN-43")
@pytest.mark.parametrize("mode", MODES)
def test_row_2_an_unlisted_app_with_no_principal_is_served(
    site: Site, mode: str
) -> None:
    assert call(mode, authorization=token("stranger")).status == 200


@pytest.mark.spec("HTTP-34")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD"])
def test_row_3_a_method_other_than_post_is_refused_with_allow(
    site: Site, mode: str, method: str
) -> None:
    served = call(mode, method=method)
    refused(served, 400, "invalid_argument", "a contract action is called with POST")
    assert served.headers["allow"] == ["POST"]


@pytest.mark.spec("HTTP-35")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("query", ["a=1", "x", "="])
def test_row_4_a_query_is_refused(site: Site, mode: str, query: str) -> None:
    refused(
        call(mode, query=query),
        400,
        "invalid_argument",
        "a contract call has no query string",
    )


@pytest.mark.parametrize("mode", MODES)
def test_row_5_plain_http_is_refused_when_the_host_redirects_to_https(
    site: Site, mode: str
) -> None:
    message = "a contract call to this host is made over HTTPS (SECURE_SSL_REDIRECT)"
    with override_settings(SECURE_SSL_REDIRECT=True):
        served = call(mode)
        refused(served, 400, "invalid_argument", message)
        assert "location" not in served.headers
    with override_settings(
        SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^ledger/"]
    ):
        assert call(mode).status == 200
    with override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    ):
        assert call(mode, fields=[("X-Forwarded-Proto", "https")]).status == 200


@pytest.mark.spec("HTTP-21", "HTTP-22", "HTTP-23")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "Neolith-Principal",
            "e30",
            "Neolith-Principal: a principal object has "
            "exactly the members issuer and subject, and optionally claims",
        ),
        ("Neolith-Principal", "", "Neolith-Principal: the principal is not JSON text"),
        ("Neolith-Principal", "ab+/", _endpoint._PRINCIPAL_FORM),
        ("Neolith-Principal", "eyJ=", _endpoint._PRINCIPAL_FORM),
        ("Neolith-Principal", "A" * 4097, _endpoint._PRINCIPAL_FORM),
        (
            "Neolith-Principal",
            "W1tbW1tbXV1dXV1d",
            "Neolith-Principal: a principal "
            "object has exactly the members issuer and subject, and optionally claims",
        ),
        (
            "Neolith-Principal",
            _context.b64url(b"[" * 300 + b"]" * 300),
            "Neolith-Principal: the principal is not a principal object",
        ),
        ("Neolith-Timeout", "0050", _endpoint._TIMEOUT_FORM),
        ("Neolith-Timeout", "0", _endpoint._TIMEOUT_FORM),
        ("Neolith-Timeout", "12345678901", _endpoint._TIMEOUT_FORM),
        ("Neolith-Timeout", "1 0", _endpoint._TIMEOUT_FORM),
        ("Neolith-Timeout", "+10", _endpoint._TIMEOUT_FORM),
        ("Neolith-Timeout", "\u0661\u0660", _endpoint._TIMEOUT_FORM),
        ("Idempotency-Key", "k", _endpoint._KEY_FORM),
        ("Idempotency-Key", '""', _endpoint._KEY_FORM),
        ("Idempotency-Key", '"k";a=1', _endpoint._KEY_FORM),
        ("Idempotency-Key", f'"{"k" * 256}"', _endpoint._KEY_FORM),
        ("Idempotency-Key", '"k\\n"', _endpoint._KEY_FORM),
        ("Idempotency-Key", '"é"', _endpoint._KEY_FORM),
    ],
)
def test_row_6_a_neolith_header_outside_its_form_is_invalid_argument(
    site: Site, mode: str, name: str, value: str, message: str
) -> None:
    refused(call(mode, fields=[(name, value)]), 400, "invalid_argument", message)
    assert site.books.seen == []


@pytest.mark.spec("HTTP-21", "HTTP-22", "HTTP-23")
@pytest.mark.parametrize("mode", MODES)
def test_row_6_each_header_s_nearest_admitted_form_passes(
    site: Site, mode: str, clock: Clock
) -> None:
    served = call(
        mode,
        fields=[
            ("Neolith-Principal", f" {PRINCIPAL}\t"),
            ("Neolith-Timeout", " 9999999999 "),
            ("Idempotency-Key", f'\t"{"k" * 255}" '),
        ],
    )
    assert served.status == 200
    (seen,) = site.books.seen
    assert seen.principal is not None
    assert seen.idempotency_key == "k" * 255
    assert seen.deadline == pytest.approx(1000 + 9999999.999)


@pytest.mark.spec("HTTP-24", "HTTP-25")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "traceparent",
    [
        None,
        f"01-{TRACE}-b7ad6b7169203331-01",  # a later version, read by its fields
        f"00-{TRACE.upper()}-b7ad6b7169203331-01",
        f"00-{'0' * 32}-b7ad6b7169203331-01",
        f"00-{TRACE}-{'0' * 16}-01",
        f"ff-{TRACE}-b7ad6b7169203331-01",
        f"00-{TRACE}-b7ad6b7169203331-01-x",
        "garbage",
    ],
    ids=[
        "absent",
        "version-01",
        "upper",
        "zero-trace",
        "zero-parent",
        "ff",
        "00-longer",
        "garbage",
    ],
)
def test_row_7_an_invalid_traceparent_starts_a_fresh_trace_and_drops_tracestate(
    site: Site, mode: str, traceparent: str | None
) -> None:
    fields = [("tracestate", "a=1")]
    if traceparent is not None:
        fields.append(("traceparent", traceparent))
    assert call(mode, fields=fields).status == 200
    (seen,) = site.books.seen
    if traceparent is not None and traceparent.startswith("01-"):
        assert (seen.trace_id, seen.tracestate) == (TRACE, "a=1")
    else:
        assert seen.trace_id != TRACE
        assert len(seen.trace_id) == 32
        assert seen.tracestate == ""


@pytest.mark.spec("HTTP-26", "HTTP-27")
@pytest.mark.parametrize("mode", MODES)
def test_row_7_tracestate_is_shortened_or_dropped_never_refused(
    site: Site, mode: str
) -> None:
    long = "z=" + "v" * 200
    members = [f"k{i}={'x' * 40}" for i in range(10)]
    value = ",".join([members[0], long, *members[1:]])
    assert len(value) > 512
    assert (
        call(mode, fields=[("traceparent", TRACEPARENT), ("tracestate", value)]).status
        == 200
    )
    assert (
        call(
            mode, fields=[("traceparent", TRACEPARENT), ("tracestate", "a=1,B=2")]
        ).status
        == 200
    )
    first, second = site.books.seen
    assert first.tracestate == ",".join(members)
    assert second.tracestate == ""


@pytest.mark.spec("HTTP-43")
@pytest.mark.parametrize("mode", MODES)
def test_row_8_the_budget_is_counted_from_before_the_first_backend_ran(
    site: Site, mode: str, clock: Clock
) -> None:
    site.backends[:] = [built(Watches(clock, cost=3.0))]
    refused(
        call(mode, fields=[("Neolith-Timeout", "2000")]),
        504,
        "timeout",
        "the deadline passed before the action was run",
    )
    assert call(mode, fields=[("Neolith-Timeout", "4000")]).status == 200
    (seen,) = site.books.seen
    assert seen.deadline == pytest.approx(1003.0 + 4.0)


@pytest.mark.spec("HTTP-36")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "media",
    [
        None,
        "",
        "text/plain",
        "application/json; charset=latin-1",
        "application/json; charset=utf-8; q=1",
        "application/json;charset=utf-8;charset=utf-8",
        "application/jsonx",
        "application/json; char\u017fet=utf-8",
        "application/json; charset='utf-8'",
        "application/json, application/json",
    ],
)
def test_row_9_a_content_type_outside_json_is_refused_with_accept_post(
    site: Site, mode: str, media: str | None
) -> None:
    served = call(mode, content_type=media)
    refused(
        served,
        400,
        "invalid_argument",
        "the request body is sent as Content-Type application/json",
    )
    assert served.headers["accept-post"] == ["application/json"]


@pytest.mark.spec("HTTP-36")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "media",
    [
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        "application/json;charset=UTF-8",
        'application/json \t; \tCHARSET="Utf-8"',
        " application/json ",
    ],
)
def test_row_9_each_admitted_spelling_of_json_is_served(
    site: Site, mode: str, media: str
) -> None:
    assert call(mode, content_type=media).status == 200


@pytest.mark.spec("HTTP-37")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("coding", ["gzip", "identity", ""])
def test_row_10_any_content_encoding_is_refused_with_accept_encoding(
    site: Site, mode: str, coding: str
) -> None:
    served = call(mode, fields=[("Content-Encoding", coding)])
    refused(
        served,
        400,
        "invalid_argument",
        "the request body is sent without Content-Encoding",
    )
    assert served.headers["accept-encoding"] == ["identity"]


@pytest.mark.spec("HTTP-38")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("length", [None, "x", "-12", "1e1", "12.0", "\u0661\u0662"])
def test_row_11_a_content_length_that_is_not_one_field_of_digits_is_refused(
    site: Site, mode: str, length: str | None
) -> None:
    refused(
        call(mode, content_length=length),
        400,
        "invalid_argument",
        "the request body is sent with one Content-Length of digits",
    )


@pytest.mark.spec("HTTP-38", "HTTP-36")
def test_row_11_repeated_body_headers_under_asgi_are_joined_and_refused(
    site: Site,
) -> None:
    lengths = asgi(
        "POST",
        POST,
        "",
        [
            ("Host", "testserver"),
            ("Authorization", token()),
            ("Content-Type", "application/json"),
            ("Content-Length", "12"),
            ("Content-Length", "12"),
        ],
        ENTRY,
    )
    refused(
        lengths,
        400,
        "invalid_argument",
        "the request body is sent with one Content-Length of digits",
    )
    types = asgi(
        "POST",
        POST,
        "",
        [
            ("Host", "testserver"),
            ("Authorization", token()),
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
            ("Content-Length", "12"),
        ],
        ENTRY,
    )
    refused(
        types,
        400,
        "invalid_argument",
        "the request body is sent as Content-Type application/json",
    )


@pytest.mark.spec("HTTP-38")
@pytest.mark.parametrize("mode", MODES)
def test_row_11_leading_zeros_and_ows_are_one_length(site: Site, mode: str) -> None:
    assert call(mode, content_length=" 0012\t").status == 200


@pytest.mark.spec("HTTP-40")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("length", ["13", "1" + "0" * 30, "9" * 5000])
def test_row_12_a_content_length_above_the_limit_is_refused_before_reading(
    site: Site, mode: str, length: str
) -> None:
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=12):
        refused(
            call(mode, content_length=length),
            400,
            "invalid_argument",
            "the request body is larger than this host admits, 12 bytes",
        )
        assert call(mode, content_length="12").status == 200


@pytest.mark.spec("HTTP-40")
@pytest.mark.parametrize("mode", MODES)
def test_row_12_with_no_limit_any_readable_length_is_read(
    site: Site, mode: str
) -> None:
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=None):
        assert call(mode).status == 200
        refused(
            call(mode, content_length="9" * 30),
            400,
            "invalid_argument",
            f"the request body is larger than this host admits, {sys.maxsize} bytes",
        )


@pytest.mark.spec("HTTP-39")
@pytest.mark.parametrize("mode", MODES)
def test_row_12_a_body_shorter_than_its_content_length_is_refused(
    site: Site, mode: str
) -> None:
    refused(
        call(mode, content_length="40"),
        400,
        "invalid_argument",
        "the request body ended before its Content-Length",
    )


@pytest.mark.spec("HTTP-39")
def test_row_12_under_asgi_a_body_longer_than_its_content_length_is_refused(
    site: Site,
) -> None:
    refused(
        call("asgi", content_length="3"),
        400,
        "invalid_argument",
        "the request body is longer than its Content-Length",
    )


@pytest.mark.spec("CON-42", "CON-16")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\xef\xbb\xbf" + ENTRY, "the request is not JSON text"),
        (b"[]", "the request does not fit"),
        (b'{"amount":1,"amount":2}', "the request holds a member name twice"),
        (b'{"amount":"5"}', "the request does not fit"),
        (b"[" * 300 + b"]" * 300, "the request is nested too deeply"),
    ],
)
def test_row_13_a_body_the_receive_check_refuses_is_invalid_argument(
    site: Site, mode: str, body: bytes, message: str
) -> None:
    served = call(mode, body=body)
    assert served.status == 400
    error = served.error()
    assert error["kind"] == "invalid_argument"
    assert error["message"].startswith(message)
    assert site.books.seen == []


@pytest.mark.spec("HTTP-42", "TRN-35")
@pytest.mark.parametrize("mode", MODES)
def test_unauthenticated_comes_first_whatever_the_method_query_headers_or_body(
    site: Site, mode: str
) -> None:
    served = call(
        mode,
        method="GET",
        query="a=1",
        authorization=None,
        body=b"not json",
        content_type="text/plain",
        content_length="99",
        fields=[("Neolith-Timeout", "0050"), ("Content-Encoding", "gzip")],
    )
    refused(served, 401, "unauthenticated", _caller.NO_AUTHORIZATION)


@pytest.mark.spec("HTTP-42")
@pytest.mark.parametrize("mode", MODES)
def test_a_backend_runs_before_the_body_is_read_with_the_filter_in_place(
    site: Site, mode: str
) -> None:
    watching = Watches()
    site.backends[:] = [built(watching)]
    assert call(mode).status == 200
    assert watching.read == [False]
    (active,) = watching.filters
    assert isinstance(active, _endpoint._Filter)


@pytest.mark.spec("HTTP-28")
@pytest.mark.parametrize("mode", MODES)
def test_headers_spec_03_does_not_name_change_nothing(
    site: Site, mode: str, calls: pytest.LogCaptureFixture
) -> None:
    served = call(
        mode,
        fields=[
            ("Neolith-Foo", "1"),
            ("Neolith-Bar-2", "anything at all"),
            ("X-Request-Id", "abc"),
            ("Cookie", "sessionid=abc"),
        ],
    )
    assert served.status == 200
    ignored = sorted(
        r.getMessage() for r in calls.records if r.levelno == logging.DEBUG
    )
    assert ignored == [
        "ledger.v1: ignored the header neolith-bar-2",
        "ledger.v1: ignored the header neolith-foo",
    ]


# Owned paths that are not contract paths, and the Host


@pytest.mark.spec("HTTP-30")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/ledger/v1/reverse"),
        ("POST", "/ledger/v1/post/"),
        ("POST", "/ledger/v1/Post"),
        ("GET", "/ledger/v1/"),
        ("DELETE", "/ledger/v1/a/b"),
        ("GET", "/.well-known/neolith/"),
        ("GET", "/.well-known/neolith/openapi.yaml"),
    ],
)
def test_an_owned_path_that_names_no_action_is_an_empty_404(
    site: Site, mode: str, method: str, path: str
) -> None:
    served = call(mode, method=method, path=path, authorization=None)
    assert served.status == 404
    assert served.body == b""
    assert served.header("Content-Type") is None


@pytest.mark.parametrize("mode", MODES)
def test_a_path_the_host_does_not_own_reaches_the_deployer_s_urls(
    site: Site, mode: str
) -> None:
    for path in ("/ledger/v2/post", "/ledger/v1", "/ledger", "/.well-known/other"):
        served = call(mode, method="GET", path=path, authorization=None)
        assert served.status == 404
        assert b"kind" not in served.body
        assert served.header("Content-Type") == "text/html; charset=utf-8"


@pytest.mark.spec("HTTP-29")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", [POST, _endpoint.HEALTH, "/ledger/v1/none"])
def test_a_host_this_host_does_not_serve_gets_django_s_400(
    site: Site, mode: str, path: str
) -> None:
    serve = wsgi if mode == "wsgi" else asgi
    served = serve(
        "POST",
        path,
        "",
        [("Host", "evil.example"), ("Authorization", token())],
        ENTRY,
    )
    assert served.status == 400
    with contextlib.suppress(ValueError):
        assert "kind" not in json.loads(served.body)
    assert site.books.seen == []


# The well-known paths


@pytest.mark.spec("HTTP-31", "HTTP-2")
@pytest.mark.parametrize("mode", MODES)
def test_the_document_is_served_with_get_and_its_headers_with_head(
    site: Site, mode: str
) -> None:
    got = call(mode, method="GET", path=_endpoint.OPENAPI, authorization=None)
    assert got.status == 200
    document = _openapi.document([Contract.of(Ledger)])
    assert got.body == document
    assert got.header("Content-Type") == "application/json"
    head = call(mode, method="HEAD", path=_endpoint.OPENAPI, authorization=None)
    assert (head.status, head.body) == (200, b"")
    assert head.header("Content-Type") == got.header("Content-Type")
    assert (
        head.header("Content-Length")
        == got.header("Content-Length")
        == str(len(document))
    )


@pytest.mark.spec("HTTP-31", "HTTP-47")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_health_is_an_empty_200(site: Site, mode: str, method: str) -> None:
    served = call(mode, method=method, path=_endpoint.HEALTH, authorization=None)
    assert (served.status, served.body) == (200, b"")
    assert served.header("Content-Type") is None


@pytest.mark.spec("HTTP-32")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", [_endpoint.OPENAPI, _endpoint.HEALTH])
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_another_method_on_a_well_known_path_is_405(
    site: Site, mode: str, path: str, method: str
) -> None:
    served = call(mode, method=method, path=path, authorization=None)
    assert (served.status, served.body) == (405, b"")
    assert served.headers["allow"] == ["GET, HEAD"]


@pytest.mark.spec("HTTP-33")
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path", [_endpoint.OPENAPI, _endpoint.HEALTH])
def test_a_query_on_a_well_known_path_is_400(site: Site, mode: str, path: str) -> None:
    served = call(mode, method="GET", path=path, query="v=2", authorization=None)
    assert (served.status, served.body) == (400, b"")
