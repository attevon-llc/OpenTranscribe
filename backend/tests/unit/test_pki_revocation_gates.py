"""CRL verification and the revocation decision fail closed, against real signed CRLs.

``_check_revocation`` turns OCSP and CRL results into the login decision; a CRL is
downloaded over the network, so one the configured CA did not sign must count as no CRL
at all, and "every source inconclusive" is decided by ``pki_revocation_soft_fail`` alone.
``_handle_revocation_check`` (``auth/pki_auth.py``) is the gate ``pki_authenticate``
consults. These tests pin each branch of that chain.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature

from app.auth import pki_auth
from app.auth import pki_revocation
from app.core.config import settings
from app.core.process_auth_settings import publish_process_auth_setting
from tests.fixtures.pki_chain import CRL_URLS
from tests.fixtures.pki_chain import crl
from tests.fixtures.pki_chain import make_chain
from tests.fixtures.pki_chain import pem

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_crl_state():
    publish_process_auth_setting("pki_crl_cache_seconds", 3600)
    publish_process_auth_setting("pki_ocsp_timeout_seconds", 5)
    pki_revocation._crl_cache.clear()
    yield
    pki_revocation._crl_cache.clear()


@pytest.fixture(scope="module")
def chain():
    return make_chain()


def _revoked_crl(chain) -> bytes:
    return crl(chain.ca_cert, chain.ca_key, (chain.leaf_cert.serial_number, 12345))


class TestCrlSignature:
    def test_ca_signed_crl_is_trusted(self, chain):
        parsed = pki_revocation._parse_and_verify_crl(_revoked_crl(chain), "u", chain.ca_cert)
        assert parsed is not None
        assert parsed.get_revoked_certificate_by_serial_number(chain.leaf_cert.serial_number)
        assert parsed.issuer == chain.ca_cert.subject

    def test_crl_signed_by_another_key_is_refused(self, chain):
        forged = crl(chain.ca_cert, chain.rogue_key, ())
        assert pki_revocation._parse_and_verify_crl(forged, "u", chain.ca_cert) is None

    def test_crl_without_an_issuer_to_verify_it_is_refused(self, chain):
        assert pki_revocation._parse_and_verify_crl(_revoked_crl(chain), "u", None) is None

    def test_invalid_signature_exception_is_unverified(self, chain):
        fake = MagicMock()
        fake.is_signature_valid.side_effect = InvalidSignature()
        assert pki_revocation._verify_crl_signature(fake, chain.ca_cert) is False

    def test_unexpected_verification_error_is_unverified(self, chain):
        fake = MagicMock()
        fake.is_signature_valid.side_effect = TypeError("unsupported key")
        assert pki_revocation._verify_crl_signature(fake, chain.ca_cert) is False


class TestCheckCrl:
    def test_listed_serial_is_revoked(self, chain):
        with patch("app.auth.pki_revocation._download_crl", return_value=_revoked_crl(chain)):
            assert pki_revocation._check_crl(chain.leaf_cert, chain.ca_cert) is True

    def test_unlisted_serial_is_valid(self, chain):
        clean = crl(chain.ca_cert, chain.ca_key, (999,))
        with patch("app.auth.pki_revocation._download_crl", return_value=clean):
            assert pki_revocation._check_crl(chain.leaf_cert, chain.ca_cert) is False

    def test_forged_clean_crl_is_inconclusive_not_valid(self, chain):
        forged = crl(chain.ca_cert, chain.rogue_key, ())
        with patch("app.auth.pki_revocation._download_crl", return_value=forged):
            assert pki_revocation._check_crl(chain.leaf_cert, chain.ca_cert) is None

    def test_every_download_failing_is_inconclusive(self, chain):
        with patch("app.auth.pki_revocation._download_crl", return_value=None):
            assert pki_revocation._check_crl(chain.leaf_cert, chain.ca_cert) is None

    def test_a_failing_first_url_falls_through_to_the_next(self, chain):
        payloads = {CRL_URLS[0]: None, CRL_URLS[1]: _revoked_crl(chain)}
        with patch("app.auth.pki_revocation._download_crl", side_effect=payloads.get):
            assert pki_revocation._check_crl(chain.leaf_cert, chain.ca_cert) is True

    def test_non_200_download_is_no_crl(self):
        reply = MagicMock(status_code=404, content=b"not found")
        with patch("app.auth.pki_revocation.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value = reply
            assert pki_revocation._download_crl("http://crl.example.test/x.crl") is None


class TestCrlCache:
    def test_expired_entry_is_dropped(self, chain):
        parsed = x509.load_der_x509_crl(_revoked_crl(chain))
        pki_revocation._crl_cache["u"] = (parsed, time.time() - 1)
        assert pki_revocation._get_crl_cache("u") is None
        assert "u" not in pki_revocation._crl_cache

    def test_cache_is_bounded_lru(self, chain, monkeypatch):
        monkeypatch.setattr(settings, "PKI_CRL_CACHE_MAX_SIZE", 2)
        parsed = x509.load_der_x509_crl(_revoked_crl(chain))
        for url in ("a", "b", "c"):
            pki_revocation._set_crl_cache(url, parsed)
        assert list(pki_revocation._crl_cache) == ["b", "c"]


class TestIssuerLoad:
    def test_unparseable_ca_file_is_no_issuer(self, tmp_path):
        bad = tmp_path / "ca.pem"
        bad.write_text("not a certificate")
        publish_process_auth_setting("pki_ca_cert_path", str(bad))
        assert pki_revocation._load_issuer_certificate() is None

    def test_readable_ca_file_loads(self, chain, tmp_path):
        good = tmp_path / "ca.pem"
        good.write_text(pem(chain.ca_cert))
        publish_process_auth_setting("pki_ca_cert_path", str(good))
        assert pki_revocation._load_issuer_certificate() == chain.ca_cert


def _decide(chain, ocsp_result, crl_result, *, soft_fail: bool, issuer=True):
    publish_process_auth_setting("pki_revocation_soft_fail", soft_fail)
    with (
        patch(
            "app.auth.pki_revocation._load_issuer_certificate",
            return_value=chain.ca_cert if issuer else None,
        ),
        patch("app.auth.pki_revocation._check_ocsp", return_value=ocsp_result) as o,
        patch("app.auth.pki_revocation._check_crl", return_value=crl_result) as c,
    ):
        return pki_revocation._check_revocation(chain.leaf_cert), o, c


class TestRevocationDecision:
    def test_ocsp_revoked_denies_without_consulting_crl(self, chain):
        (revoked, reason), _, c = _decide(chain, True, False, soft_fail=True)
        assert (revoked, reason) == (True, "Certificate revoked (OCSP)")
        assert not c.called

    def test_ocsp_good_allows(self, chain):
        (revoked, reason), _, _ = _decide(chain, False, True, soft_fail=False)
        assert (revoked, reason) == (False, "Certificate valid (OCSP)")

    def test_inconclusive_ocsp_falls_back_to_crl_with_the_issuer(self, chain):
        (revoked, reason), _, c = _decide(chain, None, True, soft_fail=True)
        assert (revoked, reason) == (True, "Certificate revoked (CRL)")
        assert c.call_args.args == (chain.leaf_cert, chain.ca_cert)

    def test_crl_good_allows(self, chain):
        (revoked, _), _, _ = _decide(chain, None, False, soft_fail=False)
        assert revoked is False

    def test_both_inconclusive_hard_fail_denies(self, chain):
        (revoked, reason), _, _ = _decide(chain, None, None, soft_fail=False)
        assert (revoked, reason) == (True, "Revocation check failed (hard-fail)")

    def test_both_inconclusive_soft_fail_allows(self, chain):
        (revoked, reason), _, _ = _decide(chain, None, None, soft_fail=True)
        assert (revoked, reason) == (False, "Revocation check failed (soft-fail)")

    def test_no_issuer_skips_ocsp_and_hard_fails(self, chain):
        (revoked, _), o, _ = _decide(chain, False, None, soft_fail=False, issuer=False)
        assert revoked is True
        assert not o.called


class TestLoginRevocationGate:
    def _gate(self, cert, *, verify: bool, soft_fail: bool, result=(False, "ok")):
        publish_process_auth_setting("pki_verify_revocation", verify)
        publish_process_auth_setting("pki_revocation_soft_fail", soft_fail)
        with patch("app.auth.pki_auth._check_revocation", return_value=result) as check:
            return pki_auth._handle_revocation_check(cert), check

    def test_disabled_never_checks(self, chain):
        denied, check = self._gate(chain.leaf_cert, verify=False, soft_fail=False)
        assert denied is False
        assert not check.called

    def test_revoked_certificate_is_denied(self, chain):
        denied, _ = self._gate(chain.leaf_cert, verify=True, soft_fail=True, result=(True, "r"))
        assert denied is True

    def test_valid_certificate_is_allowed(self, chain):
        denied, check = self._gate(chain.leaf_cert, verify=True, soft_fail=False)
        assert denied is False
        assert check.called

    def test_missing_certificate_hard_fail_denies(self):
        denied, _ = self._gate(None, verify=True, soft_fail=False)
        assert denied is True

    def test_missing_certificate_soft_fail_allows(self):
        denied, _ = self._gate(None, verify=True, soft_fail=True)
        assert denied is False
