"""The parts of the contract endpoints that are not one row of a table:
header readers as properties, the response log and Django's error mail,
Boot's ``neolith.W003``, the set-up refusal of a document that cannot be
built, and ``neolith run``'s refusal of a repeated body header (spec 03;
ADR 0011 as amended for step 5; ADR 0027).
"""

import asyncio
import json
import logging
import re
import socket
import sys
import threading
from collections.abc import Iterator

import django
import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.utils.log import log_response
from django.views.debug import ExceptionReporter
from hypothesis import given
from hypothesis import strategies as st

from neolith import _boot, _endpoint, _http, _openapi, _server
from neolith._connector import InProcess
from neolith.callers import CallerBackend
from neolith.middleware import RootMiddleware
from tests.endpoint_site import (
    ENTRY,
    MODES,
    POST,
    PRINCIPAL,
    TRACE,
    TRACEPARENT,
    Books,
    Fails,
    Ledger,
    Refuses,
    Served,
    Site,
    built,
    call,
    publish,
    token,
)
from tests.endpoint_site import _asgi as endpoints_async
from tests.endpoint_site import asgi as endpoints_asgi
from tests.endpoint_site import wsgi as endpoints_wsgi
from tests.hosting import in_fresh_process


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Site:
    return publish(monkeypatch)


_CLIENT_TRACESTATE = _http._FIELDS["tracestate"]


# tracestate (HTTP-25 to HTTP-27)

_KEY = st.from_regex(re.compile(r"[a-z][a-z0-9_*/-]{0,20}"), fullmatch=True)
_VALUE = st.from_regex(
    re.compile(r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,30}[\x21-\x2b\x2d-\x3c\x3e-\x7e]"),
    fullmatch=True,
)
_MEMBER = st.builds(lambda k, v: f"{k}={v}", _KEY, _VALUE)


@pytest.mark.spec("HTTP-25", "HTTP-26", "HTTP-27")
@given(st.text(alphabet=st.characters(codec="latin-1"), max_size=700))
def test_whatever_tracestate_arrives_the_host_passes_on_one_the_client_sends(
    text: str,
) -> None:
    kept = _endpoint._tracestate(text)
    assert kept == "" or _CLIENT_TRACESTATE.fullmatch(kept)
    assert len(kept) <= 512


@pytest.mark.spec("HTTP-25", "HTTP-26")
@given(
    st.lists(st.one_of(_MEMBER, st.just(""), st.just(" \t")), min_size=1, max_size=32),
    st.sampled_from([",", " ,", ", ", "\t,\t"]),
)
def test_a_list_is_passed_on_as_its_members_and_shortened_only_past_512(
    members: list[str], comma: str
) -> None:
    kept = _endpoint._tracestate(comma.join(members))
    whole = ",".join(m for m in members if m.strip(" \t"))
    if len(whole) <= 512:
        assert kept == whole
    else:
        # Whole entries only, in their order, and as many as fit once the
        # long ones and then the last ones went.
        assert kept.split(",") == [m for m in kept.split(",") if m in whole.split(",")]
        assert len(kept) <= 512


@pytest.mark.spec("HTTP-26")
def test_entries_longer_than_128_go_first_then_entries_from_the_end() -> None:
    short = [f"s{i}={'x' * 60}" for i in range(8)]  # 63 characters each
    long = "l=" + "y" * 130
    value = ",".join([short[0], long, *short[1:]])
    assert _endpoint._tracestate(value) == ",".join(short[:8])
    many = [f"s{i}={'x' * 60}" for i in range(9)]  # 575 characters joined
    assert _endpoint._tracestate(",".join(many)) == ",".join(many[:8])


@pytest.mark.spec("HTTP-27")
@pytest.mark.parametrize(
    "value",
    [
        ",".join(f"k{i}=1" for i in range(33)),  # 33 list-members
        "a=1,,B=2",
        "a=1;b=2",
        "a= ",
        "a=1,b",
        "a=é",
    ],
)
def test_a_value_that_is_not_a_list_of_1_to_32_members_is_dropped(value: str) -> None:
    assert _endpoint._tracestate(value) == ""


# traceparent (HTTP-24)

_HEX = "0123456789abcdef"


@pytest.mark.spec("HTTP-24")
@given(
    st.text(alphabet=_HEX, min_size=32, max_size=32).filter(lambda t: t.strip("0")),
    st.text(alphabet=_HEX, min_size=16, max_size=16).filter(lambda t: t.strip("0")),
    st.text(alphabet=_HEX, min_size=2, max_size=2),
    st.text(alphabet=_HEX, min_size=2, max_size=2).filter(lambda v: v != "ff"),
)
def test_a_valid_traceparent_of_any_version_continues_its_trace(
    trace: str, parent: str, flags: str, version: str
) -> None:
    value = f"{version}-{trace}-{parent}-{flags}"
    assert _endpoint._trace_id(value) == trace
    later = f"{value}-more" if version != "00" else value
    assert _endpoint._trace_id(later) == trace


@pytest.mark.spec("HTTP-24")
@given(st.text(max_size=80))
def test_any_other_traceparent_starts_a_fresh_trace(text: str) -> None:
    found = _endpoint._trace_id(text)
    if found is not None:
        assert re.fullmatch(r"[0-9a-f]{32}", found)
        assert text.strip(" \t")[3:35] == found


# Content-Type (HTTP-36)


@pytest.mark.spec("HTTP-36")
@given(
    st.sampled_from(["application/json", "APPLICATION/Json"]),
    st.sampled_from(
        ["", ";charset=utf-8", ' \t;\t charset="UTF-8"', "; Charset=Utf-8"]
    ),
)
def test_every_spelling_rfc_9110_treats_as_json_is_admitted(
    media: str, parameter: str
) -> None:
    assert _endpoint._MEDIA_TYPE.fullmatch(media + parameter)


@pytest.mark.spec("HTTP-36")
@given(st.text(max_size=60))
def test_only_those_spellings_are_admitted(text: str) -> None:
    if _endpoint._MEDIA_TYPE.fullmatch(text):
        assert text.isascii()
        folded = re.sub(r"[ \t]", "", text.lower())
        assert folded in (
            "application/json",
            "application/json;charset=utf-8",
            'application/json;charset="utf-8"',
        )


# The response log, and what Django's error mail holds


@pytest.mark.spec("HTTP-41")
@pytest.mark.parametrize("mode", MODES)
def test_each_contract_response_is_logged_once_without_the_request(
    site: Site, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="neolith.calls")
    caplog.set_level(logging.DEBUG, logger="django.request")
    assert call(mode).status == 200
    assert call(mode, authorization=None).status == 401
    assert call(mode, method="GET").status == 400
    lines = [r for r in caplog.records if r.name == "neolith.calls"]
    assert [r.levelno for r in lines] == [
        logging.INFO,
        logging.WARNING,
        logging.WARNING,
    ]
    assert all(not hasattr(r, "request") for r in caplog.records)
    # Marked logged, so Django's log_response wrote nothing with the request.
    assert [r for r in caplog.records if r.name == "django.request"] == []
    assert "refused at step 1" in lines[1].getMessage()
    assert (
        "refused at step 3: a contract action is called with POST"
        in lines[2].getMessage()
    )


class _OnLoop(logging.Handler):
    """Notes, for each record, whether it was emitted on an event loop."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[bool] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.seen.append(False)
        else:
            self.seen.append(True)


@pytest.mark.parametrize(
    "backend",
    [None, Refuses("the badge is revoked"), Fails()],
    ids=["admitted", "refused", "failed"],
)
def test_under_asgi_no_outcome_is_logged_on_the_event_loop(
    site: Site, caplog: pytest.LogCaptureFixture, backend: CallerBackend | None
) -> None:
    """A log handler can block, as Django's mail to ADMINS does, and on the
    loop it would hold every other request of the worker.
    """
    caplog.set_level(logging.INFO, logger="neolith.calls")
    if backend is not None:
        site.backends[:] = [built(backend)]
    noted = _OnLoop()
    loggers = [logging.getLogger(name) for name in ("neolith.calls", "django.neolith")]
    for logger in loggers:
        logger.addHandler(noted)
    try:
        call("asgi")
    finally:
        for logger in loggers:
            logger.removeHandler(noted)
    assert noted.seen
    assert not any(noted.seen)


class _Blocking(logging.Handler):
    """Blocks each emit until ``released`` is set, as a slow mail to ADMINS
    would, and notes whether it was released or gave up waiting.
    """

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.released = threading.Event()
        self.freed: list[bool] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.started.set()
        self.freed.append(self.released.wait(10))


@pytest.mark.parametrize(
    "backend",
    [None, Refuses("the badge is revoked"), Fails()],
    ids=["admitted", "refused", "failed"],
)
def test_under_asgi_a_blocking_log_handler_never_holds_the_loop(
    site: Site, caplog: pytest.LogCaptureFixture, backend: CallerBackend | None
) -> None:
    """A heartbeat on the loop releases the handler once it has started, which
    it can only do while the handler blocks a thread other than the loop's;
    on the loop, the handler would give up after 10 seconds instead.
    """
    caplog.set_level(logging.INFO, logger="neolith.calls")
    if backend is not None:
        site.backends[:] = [built(backend)]
    blocking = _Blocking()
    loggers = [logging.getLogger(name) for name in ("neolith.calls", "django.neolith")]

    async def heartbeat() -> None:
        while not blocking.started.is_set():
            await asyncio.sleep(0.001)
        blocking.released.set()

    async def both() -> Served:
        beating = asyncio.ensure_future(heartbeat())
        fields = [
            ("Host", "testserver"),
            ("Authorization", token()),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(ENTRY))),
        ]
        served = await endpoints_async("POST", POST, "", fields, ENTRY)
        await beating
        return served

    for logger in loggers:
        logger.addHandler(blocking)
    try:
        served = asyncio.run(both())
    finally:
        for logger in loggers:
            logger.removeHandler(blocking)
    assert served.status in (200, 401, 500)
    assert blocking.freed
    assert all(blocking.freed)


@pytest.mark.parametrize(
    ("authorization", "shown"),
    [
        (lambda: token(identity="other.v1"), "; iss orders, kid orders-1"),
        (lambda: "Bearer e30.e30.e30", "; iss malformed, kid malformed"),
    ],
)
def test_a_refusal_at_step_1_logs_the_token_s_iss_and_kid_only_in_their_form(
    site: Site,
    caplog: pytest.LogCaptureFixture,
    authorization: object,
    shown: str,
) -> None:
    caplog.set_level(logging.INFO, logger="neolith.calls")
    sent = authorization()  # type: ignore[operator]
    call("wsgi", authorization=sent)
    (line,) = [r.getMessage() for r in caplog.records if r.name == "neolith.calls"]
    assert line.endswith(shown)
    assert sent.split(" ")[1] not in line


@pytest.mark.parametrize("version", [django.VERSION[:2]])
def test_log_response_skips_a_marked_response_on_this_django(
    version: tuple[int, int], caplog: pytest.LogCaptureFixture
) -> None:
    """Django's own mark, private to it: run at both ends of the matrix."""
    caplog.set_level(logging.DEBUG, logger="django.request")
    marked = HttpResponse(status=500)
    marked._has_been_logged = True  # type: ignore[attr-defined]  # Django's mark
    log_response("x", response=marked, request=RequestFactory().get("/"))
    unmarked = HttpResponse(status=500)
    log_response("x", response=unmarked, request=RequestFactory().get("/"))
    assert len([r for r in caplog.records if r.name == "django.request"]) == 1


def _admins() -> list[object]:
    # Django 6 takes plain addresses; 5.2 takes (name, address) pairs.
    if django.VERSION >= (6, 0):
        return ["ops@example.com"]
    return [("ops", "ops@example.com")]


# Django 6.1 asks for MAILERS before it sends any mail; the test sends to the
# outbox pytest-django sets up.
@pytest.mark.filterwarnings("ignore:Django 2028 will not have a default mailer")
@pytest.mark.parametrize("mode", MODES)
def test_an_internal_mails_the_cause_and_never_the_principal_or_token(
    site: Site, mode: str, mailoutbox: list[mail.EmailMessage]
) -> None:
    sent = token()
    with override_settings(ADMINS=_admins(), DEBUG=False):
        served = call(
            mode,
            body=b'{"amount":500}',
            authorization=sent,
            fields=[("Neolith-Principal", PRINCIPAL)],
        )
    assert served.status == 500
    assert served.error()["kind"] == "internal"
    (message,) = mailoutbox
    text = f"{message.subject}{message.body}"
    assert "the books are closed" in text  # the cause is mailed
    for secret in (PRINCIPAL, sent, sent.split(" ")[1], "u-81f3"):
        assert secret not in text


@pytest.mark.filterwarnings("ignore:Django 2028 will not have a default mailer")
@pytest.mark.parametrize("mode", MODES)
def test_a_host_this_host_does_not_serve_mails_no_principal(
    site: Site, mode: str, mailoutbox: list[mail.EmailMessage]
) -> None:
    serve = endpoints_wsgi if mode == "wsgi" else endpoints_asgi
    with override_settings(ADMINS=_admins(), DEBUG=False):
        served = serve(
            "POST",
            POST,
            "",
            [("Host", "evil.example"), ("Neolith-Principal", PRINCIPAL)],
            ENTRY,
        )
    assert served.status == 400
    (message,) = mailoutbox
    assert "DisallowedHost" in f"{message.subject}{message.body}"
    assert PRINCIPAL not in message.body


@pytest.mark.parametrize("version", [django.VERSION[:2]])
def test_an_html_report_of_an_endpoint_failure_shows_no_header_or_principal(
    site: Site, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int]
) -> None:
    caught: list[BaseException] = []

    def failing(request: object) -> bytes:
        raise RuntimeError("the body cannot be read")

    monkeypatch.setattr(_endpoint, "_body", failing)
    monkeypatch.setattr(
        _endpoint, "_cause", lambda message, error: caught.append(error)
    )
    served = call("wsgi", fields=[("Neolith-Principal", PRINCIPAL)])
    assert served.status == 500
    (error,) = caught
    with override_settings(DEBUG=False):
        html = ExceptionReporter(
            None, type(error), error, error.__traceback__, is_email=True
        ).get_traceback_html()
    assert "_admitted" in html
    assert PRINCIPAL not in html
    assert "u-81f3" not in html


def test_the_reporter_filter_hides_every_neolith_header_whatever_debug_says(
    site: Site,
) -> None:
    request = RequestFactory().post(
        POST, HTTP_NEOLITH_PRINCIPAL=PRINCIPAL, HTTP_NEOLITH_FOO="x"
    )
    hidden = _endpoint._Filter()
    for debug in (True, False):
        with override_settings(DEBUG=debug):
            assert hidden.is_active(request)
            meta = hidden.get_safe_request_meta(request)
            assert meta["HTTP_NEOLITH_PRINCIPAL"] != PRINCIPAL
            assert meta["HTTP_NEOLITH_FOO"] != "x"
            request.exception_reporter_filter = hidden
            try:
                raise RuntimeError("probe")
            except RuntimeError:
                report = ExceptionReporter(request, *sys.exc_info())
                assert PRINCIPAL not in report.get_traceback_text()
                assert PRINCIPAL not in report.get_traceback_html()


# Health means ready (HTTP-47)


@pytest.mark.spec("HTTP-47")
def test_no_handler_serves_health_in_a_process_whose_boot_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_boot, "_kept", _boot._Kept("host: refused", tests=True))
    with pytest.raises(ImproperlyConfigured, match="host: refused"):
        RootMiddleware(lambda request: HttpResponse())


# Boot's warning about a URL the host shadows (neolith.W003)


def test_boot_warns_about_each_contract_path_and_prefix_a_pattern_matches(
    site: Site,
) -> None:
    with override_settings(ROOT_URLCONF="tests.endpoint_urls"):
        found = _boot.shadow_messages()
    assert [(m.id, m.level) for m in found] == [("neolith.W003", 30)] * 3
    texts = [m.msg for m in found]
    assert texts[0].startswith(
        "ledger.v1: the host answers /ledger/v1/ itself, so the URL pattern "
    )
    assert any("/ledger/v1/post" in text for text in texts)
    assert any("/ledger/v1/clear" in text for text in texts)


def test_boot_says_nothing_when_no_pattern_matches_and_is_silenced(
    site: Site, caplog: pytest.LogCaptureFixture
) -> None:
    assert _boot.shadow_messages() == []
    caplog.set_level(logging.WARNING, logger="neolith.host")
    with override_settings(
        ROOT_URLCONF="tests.endpoint_urls", SILENCED_SYSTEM_CHECKS=["neolith.W003"]
    ):
        _boot._warn_shadowed()
    assert [r for r in caplog.records if "W003" in r.getMessage()] == []


# A document that cannot be built stops set-up


def test_a_document_that_cannot_be_built_is_a_fault_of_neolith(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(contracts: object) -> bytes:
        raise ValueError("a keyword the projection does not know")

    monkeypatch.setattr(_openapi, "document", broken)
    with pytest.raises(ImproperlyConfigured) as raised:
        _endpoint.table({})
    assert str(raised.value) == (
        "host: the OpenAPI document of the provided contracts cannot be built: "
        "a keyword the projection does not know; this is a fault of neolith, not "
        "of an app, so please report it"
    )


def test_set_up_stops_when_the_document_cannot_be_built() -> None:
    printed = in_fresh_process(
        """
import django
from django.core.exceptions import ImproperlyConfigured
from neolith import _openapi

def broken(contracts):
    raise ValueError("a keyword the projection does not know")

_openapi.document = broken
try:
    django.setup()
except ImproperlyConfigured as error:
    print(error)
""",
        ["tests.host_apps.stock", "tests.host_apps.couriers", "neolith"],
    )
    assert "this is a fault of neolith, not of an app" in printed


# neolith run: a repeated body header, and what its server hands Django


@pytest.fixture
def live(site: Site) -> Iterator[int]:
    server = _server.listen("127.0.0.1", 0, ipv6=False)
    server.set_app(WSGIHandler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def _exchange(port: int, head: str, body: bytes = b"") -> tuple[int, bytes, bool]:
    """Send one request; return the status, the body and whether the server
    closed the connection after it.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(
            head.replace("\n", "\r\n").encode("latin-1") + b"\r\n" + body
        )
        received = b""
        while chunk := connection.recv(65536):
            received += chunk
            if b"\r\n\r\n" in received:
                header, _, rest = received.partition(b"\r\n\r\n")
                length = re.search(rb"(?i)content-length: *([0-9]+)", header)
                if length and len(rest) >= int(length[1]):
                    break
        status = int(received.split(b" ", 2)[1])
        rest = received.partition(b"\r\n\r\n")[2]
        connection.settimeout(2)
        try:
            closed = connection.recv(1) == b""
        except TimeoutError:
            closed = False
    return status, rest, closed


def _head(
    path: str, *extra: str, length: int = len(ENTRY), authorization: object = True
) -> str:
    return "\n".join(
        [
            f"POST {path} HTTP/1.1",
            "Host: testserver",
            *([f"Authorization: {token()}"] if authorization else []),
            "Content-Type: application/json",
            f"Content-Length: {length}",
            *extra,
            "",
        ]
    )


def _responses(port: int, *heads: str) -> list[tuple[int, dict[str, str], bytes]]:
    """Send each request on one connection, in turn; return each response's
    status, header fields and body, framed by its Content-Length.
    """
    found = []
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as connection,
        connection.makefile("rb") as reader,
    ):
        for head in heads:
            body = ENTRY if head.startswith("POST") else b""
            connection.sendall(head.replace("\n", "\r\n").encode() + b"\r\n" + body)
            status = int(reader.readline().split(b" ", 2)[1])
            fields: dict[str, str] = {}
            while (line := reader.readline()) not in (b"\r\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                fields[name.strip().lower()] = value.strip()
            size = (
                0
                if status == 204 or head.startswith("HEAD")
                else int(fields["content-length"])
            )
            found.append((status, fields, reader.read(size)))
    return found


@pytest.mark.parametrize(
    "heads",
    [
        (_head(POST), _head(POST)),
        (_head(POST, authorization=None), _head(POST)),
        ("GET /.well-known/neolith/health HTTP/1.1\nHost: testserver\n", _head(POST)),
        ("HEAD /.well-known/neolith/health HTTP/1.1\nHost: testserver\n", _head(POST)),
        ("GET /ledger/v1/none HTTP/1.1\nHost: testserver\n", _head(POST)),
    ],
    ids=["reply", "refusal", "health", "health-head", "not-found"],
)
def test_neolith_run_keeps_the_connection_after_each_answer(
    live: int, site: Site, heads: tuple[str, str]
) -> None:
    """Each response the host writes states its length, so a caller's pool
    can send the next call on the same connection. A 204 states none (RFC
    9110, section 8.6), and Django's development server closes the
    connection after it.
    """
    first, second = _responses(live, *heads)
    assert first[1].get("connection") != "close"
    assert (second[0], second[2]) == (200, ENTRY)


@pytest.mark.parametrize("name", ["Content-Type", "Content-Length", "content-length"])
def test_neolith_run_refuses_a_repeated_content_type_or_length(
    live: int, name: str
) -> None:
    value = "application/json" if name == "Content-Type" else str(len(ENTRY))
    status, _, closed = _exchange(live, _head(POST, f"{name}: {value}"), ENTRY)
    assert status == 400
    assert closed


def test_neolith_run_serves_a_call_and_reads_the_path_decoded(
    live: int, site: Site
) -> None:
    status, body, _ = _exchange(live, _head("/ledger%2Fv1/p%6Fst"), ENTRY)
    assert (status, body) == (200, ENTRY)


def test_neolith_run_drops_a_header_spelled_with_an_underscore(
    live: int, site: Site
) -> None:
    status, _, _ = _exchange(
        live,
        _head(POST, f"traceparent: {TRACEPARENT}", "Neolith_Principal: e30"),
        ENTRY,
    )
    assert status == 200
    (seen,) = site.books.seen
    assert seen.principal is None
    assert seen.trace_id == TRACE


def test_neolith_run_joins_a_repeated_principal_which_then_fails_its_form(
    live: int, site: Site
) -> None:
    status, body, _ = _exchange(
        live,
        _head(
            POST, f"Neolith-Principal: {PRINCIPAL}", f"Neolith-Principal: {PRINCIPAL}"
        ),
        ENTRY,
    )
    assert status == 400
    assert json.loads(body)["message"] == _endpoint._PRINCIPAL_FORM


@pytest.mark.parametrize("mode", MODES)
def test_the_same_refusal_reads_the_same_in_process_and_over_http(
    site: Site, mode: str
) -> None:
    served = call(
        mode,
        authorization=token("stranger"),
        fields=[("Neolith-Principal", PRINCIPAL)],
    )
    in_process = InProcess(Ledger, Books(), app="ledger", principal_from=("orders",))
    refusal = in_process._refusal("stranger", True)
    assert refusal is not None
    assert served.error() == {"kind": refusal.kind, "message": refusal.message}
