"""The HTTP transport's client over TLS (ADR 0028 Decisions 4, 6 and 9).

The provider's certificates come from test authorities made at run time, by
trustme or, where an extension has to be left out, by cryptography, which
trustme depends on; no key is committed. The client trusts them through the
one seam of Decision 6, the function that builds its TLS context.
"""

import contextlib
import datetime
import os
import socket
import ssl
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import trustme
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from neolith import _http
from neolith._http import Origin, Pool, Pools, Response
from tests.http_peer import OK, Provider, Script, call, fails, read_request

Trust = Callable[[ssl.SSLContext], None]


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def trust(monkeypatch: pytest.MonkeyPatch) -> Callable[[Trust], None]:
    """Make the client's context as in production, then trust a test authority."""
    real = _http._make_context

    def use(trusting: Trust) -> None:
        def make() -> ssl.SSLContext:
            context = real()
            trusting(context)
            return context

        monkeypatch.setattr(_http, "_make_context", make)

    return use


def tls(
    server: ssl.SSLContext,
    inner: Script,
    *,
    close_notify: bool = True,
    names: list[str | None] | None = None,
) -> Script:
    """A provider script run over TLS, as the server side of the handshake."""
    if names is not None:

        def record(sock: ssl.SSLObject, name: str, _: ssl.SSLContext) -> None:
            names.append(name)

        server.sni_callback = record

    def script(sock: socket.socket) -> None:
        secured = server.wrap_socket(sock, server_side=True)
        try:
            inner(secured)
            if close_notify:
                with contextlib.suppress(OSError):
                    secured.unwrap()
        finally:
            secured.close()

    return script


def serve(cert: trustme.LeafCert) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(context)
    return context


@pytest.fixture
def secure() -> Iterator[Callable[..., tuple[Pool, Provider]]]:
    made: list[tuple[Pools, Provider]] = []

    def make(
        *scripts: Script, url: str = "https://provider.test"
    ) -> tuple[Pool, Provider]:
        provider = Provider(scripts)
        pools = Pools()
        pool = pools.pool(Origin.parse(url))
        pool._opener = provider.open
        made.append((pools, provider))
        return pool, provider

    yield make
    for pools, provider in made:
        pools.close()
        provider.finish()


def answer_once(response: bytes) -> Script:
    def script(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(response)

    return script


def answer_and_hold(response: bytes) -> Script:
    """Answer, then keep the connection open until the client ends it."""

    def script(sock: socket.socket) -> None:
        read_request(sock)
        sock.sendall(response)
        read_request(sock)

    return script


def test_a_valid_chain_is_trusted_and_the_name_is_sent(
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
) -> None:
    trust(ca.configure_trust)
    names: list[str | None] = []
    pool, _ = secure(
        tls(serve(ca.issue_cert("provider.test")), answer_once(OK), names=names)
    )
    assert call(pool) == Response(200, b"{}")
    assert names == ["provider.test"]


def test_an_address_is_checked_against_the_certificate_and_not_sent(
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
) -> None:
    trust(ca.configure_trust)
    names: list[str | None] = []
    pool, _ = secure(
        tls(serve(ca.issue_cert("127.0.0.1")), answer_once(OK), names=names),
        url="https://127.0.0.1:8443",
    )
    assert call(pool) == Response(200, b"{}")
    assert names == [None]


@pytest.mark.parametrize(
    ("issued", "url", "reason"),
    [
        ("provider.test", "https://other.test", "Hostname mismatch"),
        ("127.0.0.2", "https://127.0.0.1", "IP address mismatch"),
    ],
)
def test_a_certificate_for_another_name_does_not_verify(
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
    issued: str,
    url: str,
    reason: str,
) -> None:
    trust(ca.configure_trust)
    pool, _ = secure(tls(serve(ca.issue_cert(issued)), answer_once(OK)), url=url)
    error = fails(pool)
    assert error.kind == "unavailable"
    assert error.message.startswith(
        f"the provider at {url} could not be reached: "
        "its TLS certificate did not verify ("
    )
    assert reason in error.message
    assert error.phase == "before writing"


def test_an_untrusted_chain_does_not_verify(
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
) -> None:
    trust(trustme.CA().configure_trust)
    pool, _ = secure(tls(serve(ca.issue_cert("provider.test")), answer_once(OK)))
    error = fails(pool)
    assert error.message == (
        "the provider at https://provider.test could not be reached: its TLS "
        "certificate did not verify (unable to get local issuer certificate)"
    )


def test_a_server_that_is_not_tls_fails_the_handshake(
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
) -> None:
    pool, _ = secure(lambda sock: sock.sendall(OK))
    error = fails(pool)
    assert error.message == (
        "the provider at https://provider.test could not be reached: "
        "the TLS handshake failed"
    )


# An authority's certificates under VERIFY_X509_STRICT, on every Python


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _usage(*, sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=sign,
        crl_sign=sign,
        encipher_only=False,
        decipher_only=False,
    )


def _issue(tmp_path: Path, *, authority_key_id: bool) -> tuple[bytes, ssl.SSLContext]:
    """An authority, and a server context with a leaf it issued."""
    start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    end = start + datetime.timedelta(days=3)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("neolith test authority"))
        .issuer_name(_name("neolith test authority"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_usage(sign=True), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name("provider.test"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_usage(sign=False), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("provider.test")]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    )
    if authority_key_id:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
    leaf = builder.sign(ca_key, hashes.SHA256())
    chain = tmp_path / "chain.pem"
    chain.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(chain)
    return ca_cert.public_bytes(serialization.Encoding.PEM), server


@pytest.mark.parametrize("authority_key_id", [False, True], ids=["without", "with"])
def test_a_certificate_without_an_authority_key_identifier_never_verifies(
    tmp_path: Path,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
    authority_key_id: bool,
) -> None:
    authority, server = _issue(tmp_path, authority_key_id=authority_key_id)
    trust(lambda context: context.load_verify_locations(cadata=authority.decode()))
    pool, _ = secure(tls(server, answer_once(OK)))
    if authority_key_id:
        assert call(pool) == Response(200, b"{}")
    else:
        error = fails(pool)
        assert "its TLS certificate did not verify (" in error.message
        assert "Authority Key Identifier" in error.message


# The context itself


def test_the_context_verifies_strictly_from_tls_1_2_on_every_python() -> None:
    context = _http._make_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    default = ssl.create_default_context()
    assert context.options == default.options
    assert context.cert_store_stats() == default.cert_store_stats()


def test_sslkeylogfile_is_never_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
) -> None:
    keys = tmp_path / "keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keys))
    assert _http._make_context().keylog_filename is None
    trust(ca.configure_trust)
    pool, _ = secure(tls(serve(ca.issue_cert("provider.test")), answer_once(OK)))
    assert call(pool) == Response(200, b"{}")
    assert not keys.exists()
    assert os.environ["SSLKEYLOGFILE"] == str(keys)


# What TLS holds back from the reader


@pytest.mark.parametrize("chunk", [_http._CHUNK, 4], ids=["buffered", "pending"])
def test_a_second_response_in_the_same_record_is_never_read_by_the_next_call(
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
    monkeypatch: pytest.MonkeyPatch,
    chunk: int,
) -> None:
    # Read 4 bytes at a time, the first response (40 bytes) leaves the
    # client's own buffer empty, and only SSLSocket.pending() sees the rest.
    monkeypatch.setattr(_http, "_CHUNK", chunk)
    trust(ca.configure_trust)
    server = serve(ca.issue_cert("provider.test"))
    stale = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nstale"
    pool, provider = secure(
        tls(server, answer_and_hold(OK + stale)),
        tls(server, answer_once(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nfresh")),
    )
    assert call(pool) == Response(200, b"{}")
    assert call(pool) == Response(200, b"fresh")
    assert provider.opened == 2


@pytest.mark.parametrize("close_notify", [True, False], ids=["close_notify", "ragged"])
def test_a_body_to_the_end_needs_close_notify(
    ca: trustme.CA,
    trust: Callable[[Trust], None],
    secure: Callable[..., tuple[Pool, Provider]],
    close_notify: bool,
) -> None:
    trust(ca.configure_trust)
    server = serve(ca.issue_cert("provider.test"))
    pool, _ = secure(
        tls(
            server, answer_once(b"HTTP/1.1 200 OK\r\n\r\n{}"), close_notify=close_notify
        )
    )
    if close_notify:
        assert call(pool) == Response(200, b"{}")
    else:
        error = fails(pool)
        assert error.message == (
            "the provider at https://provider.test did not answer in HTTP/1.1"
        )
        assert error.cause
