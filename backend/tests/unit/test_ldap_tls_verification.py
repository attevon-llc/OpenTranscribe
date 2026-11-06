"""The directory's TLS certificate is verified on every LDAP connection, unless explicitly off.

ldap3's ``Tls`` defaults to ``validate=ssl.CERT_NONE`` (``ldap3/core/tls.py``), and a ``Server``
built without one gets that for LDAPS, while ``Connection.start_tls`` builds a bare ``Tls()`` for
StartTLS. ``_get_ldap_server`` therefore always passes a ``Tls``: ``CERT_REQUIRED`` by default,
``ldap_ca_cert_file`` for a private CA, ``CERT_NONE`` only under ``ldap_tls_skip_verify`` (which
warns on every connection). A CA file that cannot be used refuses the connection.

Three layers: the ``Tls`` that gets built (real ldap3 ``Server``, no sockets), every connection
path that must use it (login, directory sync, admin "Test connection"), and real handshakes
against a local TLS listener, the only proof that ldap3 enforces what was configured.
"""

from __future__ import annotations

import dataclasses
import datetime
import ipaddress
import logging
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ldap3
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from ldap3.core.exceptions import LDAPException

from app.api.endpoints.auth_config_connection_tests import _test_ldap_connection
from app.auth import ldap_directory
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import ldap_authenticate
from app.auth.ldap_directory import LdapCaFileError
from app.auth.ldap_directory import _connect_and_bind
from app.auth.ldap_directory import _get_ldap_server
from app.auth.ldap_probe import ldap_directory_session
from app.core import config as core_config
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import install

_WARNING = "certificate verification is DISABLED"


# --------------------------------------------------------------------------- certificates


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _validity(builder: x509.CertificateBuilder) -> x509.CertificateBuilder:
    now = datetime.datetime.now(datetime.UTC)
    return builder.not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(
        now + datetime.timedelta(hours=1)
    )


class _Pki:
    """A throwaway CA and a server certificate it signed, written under ``tmp_path``."""

    def __init__(self, tmp_path: Path, *, san: list[x509.GeneralName]) -> None:
        ca_key = _key()
        ca_cert = _validity(
            x509.CertificateBuilder()
            .subject_name(_name("test directory CA"))
            .issuer_name(_name("test directory CA"))
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        ).sign(ca_key, hashes.SHA256())
        leaf_key = _key()
        leaf = _validity(
            x509.CertificateBuilder()
            .subject_name(_name("directory"))
            .issuer_name(ca_cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
        ).sign(ca_key, hashes.SHA256())

        self.ca_file = tmp_path / "ca.pem"
        self.ca_file.write_bytes(_pem(ca_cert))
        self.cert_file = tmp_path / "server.pem"
        self.cert_file.write_bytes(_pem(leaf))
        self.key_file = tmp_path / "server.key"
        self.key_file.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


@pytest.fixture
def pki(tmp_path: Path) -> _Pki:
    """A server certificate for the host name ``localhost``."""
    return _Pki(tmp_path, san=[x509.DNSName("localhost")])


@pytest.fixture
def pki_ip(tmp_path: Path) -> _Pki:
    """A server certificate whose only name is the IP address ``127.0.0.1``."""
    return _Pki(tmp_path, san=[x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])


@pytest.fixture
def pki_wrong_name(tmp_path: Path) -> _Pki:
    """A certificate from a trusted CA that does not name the host being connected to."""
    return _Pki(tmp_path, san=[x509.DNSName("other.example")])


# --------------------------------------------------------------------------- the Tls object


def _cfg(**overrides: Any) -> LdapConfig:
    base = LdapConfig(enabled=True, server="ldap.example", port=636, use_ssl=True)
    return dataclasses.replace(base, **overrides)


@pytest.mark.parametrize(
    "cfg",
    [
        _cfg(use_ssl=True),
        _cfg(server="ldaps://ldap.example", use_ssl=False),
        _cfg(port=389, use_ssl=False, use_tls=True),
    ],
    ids=["ldaps", "ldaps-url", "starttls"],
)
def test_the_default_requires_a_valid_certificate_for_every_transport(cfg):
    tls = _get_ldap_server(cfg).tls

    assert tls.validate == ssl.CERT_REQUIRED
    assert tls.ca_certs_file is None


def test_the_ca_file_is_handed_to_ldap3(pki):
    server = _get_ldap_server(_cfg(ca_cert_file=str(pki.ca_file)))

    assert server.tls.validate == ssl.CERT_REQUIRED
    assert server.tls.ca_certs_file == str(pki.ca_file)


def test_a_blank_ca_path_means_the_system_store(pki):
    assert _get_ldap_server(_cfg(ca_cert_file="   ")).tls.ca_certs_file is None


def test_skipping_verification_is_the_only_way_to_cert_none():
    assert _get_ldap_server(_cfg(tls_skip_verify=True)).tls.validate == ssl.CERT_NONE


def test_skipping_verification_wins_over_a_ca_file(tmp_path):
    cfg = _cfg(tls_skip_verify=True, ca_cert_file=str(tmp_path / "does-not-exist.pem"))

    assert _get_ldap_server(cfg).tls.validate == ssl.CERT_NONE


def test_a_missing_ca_file_refuses_instead_of_falling_back(tmp_path):
    with pytest.raises(LdapCaFileError) as raised:
        _get_ldap_server(_cfg(ca_cert_file=str(tmp_path / "missing.pem")))

    assert isinstance(raised.value, LDAPException)
    assert "CA certificate file" in str(raised.value)


def test_a_ca_directory_refuses(tmp_path):
    with pytest.raises(LdapCaFileError):
        _get_ldap_server(_cfg(ca_cert_file=str(tmp_path)))


def test_a_ca_file_that_is_not_pem_refuses(tmp_path):
    junk = tmp_path / "junk.pem"
    junk.write_text("this is not a certificate\n")

    with pytest.raises(LdapCaFileError):
        _get_ldap_server(_cfg(ca_cert_file=str(junk)))


# --------------------------------------------------------------------------- the warning


class _Conn:
    """A Connection whose bind succeeds; only the call order and options matter here."""

    instances: list[_Conn] = []

    def __init__(self, server: Any, user: str = "", password: str = "", **options: Any) -> None:
        self.server = server
        self.options = options
        _Conn.instances.append(self)

    def open(self) -> None:
        return None

    def start_tls(self) -> bool:
        return True

    def bind(self) -> bool:
        return True

    def unbind(self) -> bool:
        return True


@pytest.fixture
def fake_connection(monkeypatch: pytest.MonkeyPatch) -> type[_Conn]:
    _Conn.instances = []
    monkeypatch.setattr(ldap_directory, "Connection", _Conn)
    return _Conn


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_every_unverified_connection_warns(fake_connection, caplog):
    cfg = _cfg(tls_skip_verify=True)
    server = _get_ldap_server(cfg)

    with caplog.at_level(logging.WARNING, logger=ldap_directory.logger.name):
        _connect_and_bind(cfg, server, "cn=a", "pw")
        _connect_and_bind(cfg, server, "cn=b", "pw")

    assert sum(_WARNING in m for m in _warnings(caplog)) == 2


def test_a_verified_connection_does_not_warn(fake_connection, caplog):
    cfg = _cfg()

    with caplog.at_level(logging.WARNING, logger=ldap_directory.logger.name):
        assert _connect_and_bind(cfg, _get_ldap_server(cfg), "cn=a", "pw") is not None

    assert not [m for m in _warnings(caplog) if _WARNING in m]


def test_referrals_are_not_followed(fake_connection):
    """A referral target would be upgraded with a default, non-verifying ``Tls``."""
    cfg = _cfg()

    _connect_and_bind(cfg, _get_ldap_server(cfg), "cn=a", "pw")

    assert fake_connection.instances[0].options["auto_referrals"] is False


# --------------------------------------------------------------------------- every path


def _servers_built(directory: Any) -> list[dict[str, Any]]:
    return [call[2] for call in directory.calls if call[0] == "server"]


def _env_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "LDAP_ENABLED": True,
        "LDAP_SERVER": "ldap.test",
        "LDAP_PORT": 636,
        "LDAP_USE_SSL": True,
        "LDAP_USE_TLS": False,
        "LDAP_BIND_DN": SERVICE_DN,
        "LDAP_BIND_PASSWORD": SERVICE_CREDENTIAL,
        "LDAP_SEARCH_BASE": SEARCH_BASE,
        "LDAP_USERNAME_ATTR": "uid",
        "LDAP_USER_SEARCH_FILTER": "(uid={username})",
        "LDAP_CA_CERT_FILE": "",
        "LDAP_TLS_SKIP_VERIFY": False,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(core_config.settings, name, value)


def _login_directory(monkeypatch: pytest.MonkeyPatch) -> Any:
    directory = install(monkeypatch)
    entry = FakeEntry("uid=alice,dc=test", {"uid": "alice", "mail": "alice@test.example"})
    directory.add_user(entry, "alice-pw")
    return directory


def test_login_verifies_by_default(monkeypatch):
    _env_settings(monkeypatch)
    directory = _login_directory(monkeypatch)

    assert ldap_authenticate("alice", "alice-pw") is not None

    (kwargs,) = _servers_built(directory)
    assert kwargs["tls"].validate == ssl.CERT_REQUIRED


def test_login_passes_the_ca_file_through(monkeypatch, pki):
    _env_settings(monkeypatch, LDAP_CA_CERT_FILE=str(pki.ca_file))
    directory = _login_directory(monkeypatch)

    assert ldap_authenticate("alice", "alice-pw") is not None

    (kwargs,) = _servers_built(directory)
    assert kwargs["tls"].ca_certs_file == str(pki.ca_file)


def test_login_with_an_unusable_ca_file_is_refused_before_any_connection(monkeypatch, tmp_path):
    _env_settings(monkeypatch, LDAP_CA_CERT_FILE=str(tmp_path / "missing.pem"))
    directory = _login_directory(monkeypatch)

    assert ldap_authenticate("alice", "alice-pw") is None

    assert directory.connections == []


def test_login_honours_the_opt_out(monkeypatch):
    _env_settings(monkeypatch, LDAP_TLS_SKIP_VERIFY=True)
    directory = _login_directory(monkeypatch)

    assert ldap_authenticate("alice", "alice-pw") is not None

    (kwargs,) = _servers_built(directory)
    assert kwargs["tls"].validate == ssl.CERT_NONE


def test_the_directory_sync_session_verifies(monkeypatch, pki):
    directory = install(monkeypatch)
    cfg = _cfg(
        server="ldap.test",
        bind_dn=SERVICE_DN,
        bind_password=SERVICE_CREDENTIAL,
        ca_cert_file=str(pki.ca_file),
    )

    with ldap_directory_session(cfg):
        pass

    (kwargs,) = _servers_built(directory)
    assert (kwargs["tls"].validate, kwargs["tls"].ca_certs_file) == (
        ssl.CERT_REQUIRED,
        str(pki.ca_file),
    )


def _test_request(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ldap_server": "ldap.test",
        "ldap_port": 636,
        "ldap_use_ssl": True,
        "ldap_use_tls": False,
        "ldap_bind_dn": SERVICE_DN,
        "ldap_bind_password": SERVICE_CREDENTIAL,
    }
    base.update(overrides)
    return base


def test_the_connection_test_verifies_by_default(fake_connection):
    assert _test_ldap_connection(_test_request()).success is True

    assert fake_connection.instances[0].server.tls.validate == ssl.CERT_REQUIRED


def test_the_connection_test_uses_the_submitted_ca_file(fake_connection, pki):
    result = _test_ldap_connection(_test_request(ldap_ca_cert_file=str(pki.ca_file)))

    assert result.success is True
    assert fake_connection.instances[0].server.tls.ca_certs_file == str(pki.ca_file)


@pytest.mark.parametrize("submitted", [True, "true", "1", "yes"])
def test_the_connection_test_honours_the_opt_out(fake_connection, submitted):
    assert _test_ldap_connection(_test_request(ldap_tls_skip_verify=submitted)).success is True

    assert fake_connection.instances[0].server.tls.validate == ssl.CERT_NONE


@pytest.mark.parametrize("submitted", [False, "false", "", None, "maybe", 0])
def test_the_connection_test_keeps_verifying_unless_clearly_told_not_to(fake_connection, submitted):
    assert _test_ldap_connection(_test_request(ldap_tls_skip_verify=submitted)).success is True

    assert fake_connection.instances[0].server.tls.validate == ssl.CERT_REQUIRED


def test_the_connection_test_with_an_unusable_ca_file_connects_to_nothing(
    fake_connection, tmp_path
):
    result = _test_ldap_connection(_test_request(ldap_ca_cert_file=str(tmp_path / "missing.pem")))

    assert result.success is False
    assert "CA certificate file" in result.message
    assert str(tmp_path) not in result.message
    assert fake_connection.instances == []


def test_the_connection_test_warns_when_it_skips_verification(fake_connection, caplog):
    with caplog.at_level(logging.WARNING, logger=ldap_directory.logger.name):
        _test_ldap_connection(_test_request(ldap_tls_skip_verify=True))

    assert any(_WARNING in m for m in _warnings(caplog))


# --------------------------------------------------------------------------- real handshakes


def _start_tls_ok(request: bytes) -> bytes:
    """LDAPMessage { messageID <the request's>, extendedResp { success, "", "" } }."""
    message_id = request[4]  # 30 <len> 02 01 <id> ...: ldap3's ids stay below 128
    return bytes([0x30, 0x0C, 0x02, 0x01, message_id, 0x78, 0x07, 0x0A, 0x01, 0x00, 0x04, 0, 4, 0])


@pytest.fixture
def tls_listener() -> Iterator[Any]:
    """A localhost TLS server; ``starttls`` mode speaks just enough LDAP to upgrade."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    stop = threading.Event()
    sockets: list[socket.socket] = []

    def serve(pki: _Pki, starttls: bool) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pki.cert_file, pki.key_file)
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except OSError:
                return
            sockets.append(raw)
            try:
                if starttls:
                    raw.settimeout(5)
                    raw.sendall(_start_tls_ok(raw.recv(4096)))
                context.wrap_socket(raw, server_side=True).recv(1)
            except (OSError, ssl.SSLError):
                pass

    class _Listener:
        port: int = listener.getsockname()[1]

        def start(self, pki: _Pki, *, starttls: bool = False) -> int:
            threading.Thread(target=serve, args=(pki, starttls), daemon=True).start()
            return self.port

    yield _Listener()
    stop.set()
    listener.close()
    for sock in sockets:
        sock.close()


def _real_server(pki: _Pki, port: int, **overrides: Any) -> ldap3.Server:
    server = _get_ldap_server(
        _cfg(server="localhost", port=port, timeout=5, ca_cert_file=str(pki.ca_file), **overrides)
    )
    server.get_info = ldap3.NONE  # reading the rootDSE would need a real directory
    return server


def _open(server: ldap3.Server) -> ldap3.Connection:
    conn = ldap3.Connection(server, auto_bind=False, receive_timeout=5, auto_referrals=False)
    conn.open()
    return conn


def test_ldaps_with_the_right_ca_completes_the_handshake(tls_listener, pki):
    conn = _open(_real_server(pki, tls_listener.start(pki)))

    assert isinstance(conn.socket, ssl.SSLSocket)
    conn.unbind()


def test_ldaps_with_an_untrusted_certificate_is_refused(tls_listener, pki):
    port = tls_listener.start(pki)
    server = _get_ldap_server(_cfg(server="localhost", port=port, timeout=5))

    with pytest.raises(LDAPException, match="certificate verify failed"):
        _open(server)


def test_ldap3_cannot_verify_a_connection_made_by_ip_address(tls_listener, pki_ip):
    """ldap3's hostname check (``ldap3/utils/tls_backport.py``) "does not accept IP addresses".

    Python 3.12 removed ``ssl.match_hostname``, so ldap3 uses its backport, which only
    compares DNS names. The documentation tells operators to name the server by host name.
    """
    port = tls_listener.start(pki_ip)
    server = _real_server(pki_ip, port)
    server.host = "127.0.0.1"

    with pytest.raises(LDAPException, match="doesn't match any name"):
        _open(server)


def test_ldaps_with_a_certificate_for_another_name_is_refused(tls_listener, pki_wrong_name):
    port = tls_listener.start(pki_wrong_name)
    with pytest.raises(LDAPException, match="doesn't match any name"):
        _open(_real_server(pki_wrong_name, port))


def test_ldaps_accepts_an_untrusted_certificate_only_when_told_to(tls_listener, pki):
    port = tls_listener.start(pki)
    server = _get_ldap_server(_cfg(server="localhost", port=port, timeout=5, tls_skip_verify=True))
    server.get_info = ldap3.NONE

    conn = _open(server)

    assert isinstance(conn.socket, ssl.SSLSocket)
    conn.unbind()


def test_a_refused_ldaps_connection_never_reaches_a_bind(tls_listener, pki):
    cfg = _cfg(server="localhost", port=tls_listener.start(pki), timeout=5)

    with pytest.raises(LDAPException):
        _connect_and_bind(cfg, _get_ldap_server(cfg), SERVICE_DN, SERVICE_CREDENTIAL)


def _starttls(server: ldap3.Server) -> bool:
    conn = ldap3.Connection(server, auto_bind=False, receive_timeout=5, auto_referrals=False)
    conn.open()
    try:
        return bool(conn.start_tls())
    finally:
        conn.unbind()


def test_starttls_with_the_right_ca_upgrades(tls_listener, pki):
    server = _real_server(pki, tls_listener.start(pki, starttls=True), use_ssl=False, use_tls=True)

    assert _starttls(server) is True


def test_starttls_with_an_untrusted_certificate_is_refused(tls_listener, pki):
    port = tls_listener.start(pki, starttls=True)
    server = _get_ldap_server(
        _cfg(server="localhost", port=port, timeout=5, use_ssl=False, use_tls=True)
    )
    server.get_info = ldap3.NONE

    with pytest.raises(LDAPException):
        _starttls(server)


def test_starttls_with_a_certificate_for_another_name_is_refused(tls_listener, pki_wrong_name):
    port = tls_listener.start(pki_wrong_name, starttls=True)
    server = _real_server(pki_wrong_name, port, use_ssl=False, use_tls=True)

    with pytest.raises(LDAPException):
        _starttls(server)
