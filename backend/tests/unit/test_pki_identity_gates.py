"""Who ``pki_authenticate`` says the caller is, and whether that is an admin.

Pins the identity gates that the endpoint suite does not reach: the DB-resolved trusted
proxy allowlist must override the import-time ``.env`` one at every call site, a broken
certificate must not fall through to the DN header, ``mutual_tls`` refuses a DN alone,
identity comes from the certificate *subject*, and admin DN matching is exact.
"""

from __future__ import annotations

import ipaddress
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.auth import pki_auth
from app.auth.pki_certificate import _parse_dn_components
from app.auth.pki_certificate import parse_certificate
from app.core.process_auth_settings import publish_process_auth_setting
from tests.fixtures.pki_chain import issue
from tests.fixtures.pki_chain import make_chain
from tests.fixtures.pki_chain import pem

pytestmark = pytest.mark.unit

CERT_H = "X-Test-Cert"
DN_H = "X-Test-DN"
DN = "CN=Jane Admin,O=Agency,C=US"


@pytest.fixture(autouse=True)
def _no_revocation():
    publish_process_auth_setting("pki_verify_revocation", False)


@pytest.fixture(scope="module")
def chain():
    return make_chain()


def _request(host: str, headers: dict[str, str]) -> MagicMock:
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = host
    request.headers = headers
    return request


def _auth(request, *, proxies: str | None, mode: str | None = None, admin: str | None = None):
    return pki_auth.pki_authenticate(
        request,
        admin_dns_config=admin,
        pki_mode=mode,
        trusted_proxies_config=proxies,
        cert_header=CERT_H,
        cert_dn_header=DN_H,
    )


@pytest.fixture
def env_trusts_10():
    """The import-time .env allowlist trusts 10/8; the DB value is what must win."""
    with patch(
        "app.auth.pki_auth._pki_trusted_proxy_networks", [ipaddress.ip_network("10.0.0.0/8")]
    ):
        yield


class TestDbAllowlistWins:
    def test_db_narrowed_allowlist_refuses_a_dn_header(self, env_trusts_10):
        assert _auth(_request("10.1.1.1", {DN_H: DN}), proxies="192.168.0.0/16") is None

    def test_db_narrowed_allowlist_refuses_a_certificate(self, chain, env_trusts_10):
        headers = {CERT_H: pem(chain.leaf_cert)}
        assert _auth(_request("10.1.1.1", headers), proxies="192.168.0.0/16") is None

    def test_db_allowlist_admits_a_peer_env_does_not(self):
        with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
            result = _auth(_request("192.168.5.5", {DN_H: DN}), proxies="192.168.0.0/16")
        assert result is not None
        assert result["subject_dn"] == DN

    def test_dn_only_trust_check_uses_the_db_networks(self, env_trusts_10):
        request = _request("10.1.1.1", {DN_H: DN})
        networks = [ipaddress.ip_network("192.168.0.0/16")]
        info = pki_auth._extract_user_info_from_request(request, None, None, networks=networks)
        assert info is None

    def test_header_source_trust_uses_the_db_networks(self, env_trusts_10):
        request = _request("10.1.1.1", {})
        networks = [ipaddress.ip_network("192.168.0.0/16")]
        assert pki_auth._pki_header_source_is_trusted(request, networks) is False


class TestHeaderSourceValidation:
    @pytest.mark.parametrize("header", [CERT_H, DN_H])
    def test_either_header_alone_from_an_untrusted_peer_is_refused(self, header, env_trusts_10):
        request = _request("203.0.113.9", {header: "x"})
        assert (
            pki_auth._validate_pki_headers_source(
                request, None, cert_header=CERT_H, cert_dn_header=DN_H
            )
            is False
        )


class TestCertificateGates:
    def test_unparseable_certificate_does_not_fall_back_to_the_dn(self, env_trusts_10):
        headers = {CERT_H: "-----BEGIN CERTIFICATE-----\ngarbage\n", DN_H: DN}
        assert _auth(_request("10.1.1.1", headers), proxies=None) is None

    def test_mutual_tls_refuses_a_dn_header_without_the_certificate(self, env_trusts_10):
        request = _request("10.1.1.1", {DN_H: DN})
        assert _auth(request, proxies=None, mode=pki_auth.PKI_MODE_MUTUAL_TLS) is None

    def test_header_mode_accepts_a_trusted_dn(self, env_trusts_10):
        result = _auth(_request("10.1.1.1", {DN_H: DN}), proxies=None)
        assert result is not None
        assert (result["subject_dn"], result["common_name"]) == (DN, "Jane Admin")

    def test_certificate_identity_is_its_subject_not_its_issuer(self, chain, env_trusts_10):
        result = _auth(_request("10.1.1.1", {CERT_H: pem(chain.leaf_cert)}), proxies=None)
        assert result is not None
        assert result["subject_dn"] == chain.leaf_cert.subject.rfc4514_string()
        assert result["issuer_dn"] == chain.ca_cert.subject.rfc4514_string()

    def test_revoked_certificate_is_refused(self, chain, env_trusts_10):
        publish_process_auth_setting("pki_verify_revocation", True)
        with patch("app.auth.pki_auth._check_revocation", return_value=(True, "revoked")):
            result = _auth(_request("10.1.1.1", {CERT_H: pem(chain.leaf_cert)}), proxies=None)
        assert result is None


class TestAdminDns:
    def test_db_admin_list_grants_admin(self, env_trusts_10):
        with patch("app.auth.pki_auth.settings") as s:
            s.PKI_ADMIN_DNS = ""
            result = _auth(_request("10.1.1.1", {DN_H: DN}), proxies=None, admin=DN)
        assert result is not None
        assert result["is_admin"] is True

    def test_db_admin_list_overrides_env(self):
        with patch("app.auth.pki_auth.settings") as s:
            s.PKI_ADMIN_DNS = "CN=Env Admin,O=Agency,C=US"
            assert pki_auth._is_pki_admin("CN=Env Admin,O=Agency,C=US", DN) is False
            assert pki_auth._is_pki_admin(DN, DN) is True

    @pytest.mark.parametrize(
        "subject", ["CN=Jane", "CN=Jane Admin,O=Agency", "CN=Jane Admin,O=Agency,C=U"]
    )
    def test_a_partial_dn_is_not_an_admin(self, subject):
        assert pki_auth._is_pki_admin(subject, DN) is False

    def test_whitespace_and_attribute_case_are_normalised(self):
        assert pki_auth._is_pki_admin(" cn = Jane Admin , o = Agency , c = US ", DN) is True


class TestCertificateParsing:
    def test_subject_is_reported_separately_from_issuer(self, chain):
        info = parse_certificate(pem(chain.leaf_cert))
        assert info is not None
        assert info.subject_dn == chain.leaf_cert.subject.rfc4514_string()
        assert info.subject_dn != info.issuer_dn

    def test_email_is_read_from_the_san(self, chain):
        leaf, _ = issue(chain.ca_cert, chain.ca_key, "San User", email_in_san="san@example.com")
        info = parse_certificate(pem(leaf))
        assert info is not None
        assert info.email == "san@example.com"

    def test_fingerprint_is_sha256(self, chain):
        info = parse_certificate(pem(chain.leaf_cert))
        assert info is not None
        assert len(info.fingerprint.split(":")) == 32

    def test_cn_is_only_read_from_a_cn_rdn(self):
        assert _parse_dn_components("CN=Real,OU=XCN=Fake")[0] == "Real"
