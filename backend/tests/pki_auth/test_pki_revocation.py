"""PKI revocation checking: OCSP, CRL and the combined revocation policy (all HTTP mocked).

Run with: pytest tests/pki_auth -v
"""

from unittest.mock import MagicMock
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from tests.fixtures.pki_auth import set_pki
from tests.fixtures.pki_auth import skip_pki_unit

# ===== OCSP/CRL Mock Tests =====


@skip_pki_unit
class TestOCSPChecking:
    """Test OCSP revocation checking (mocked)."""

    def test_ocsp_cache_hit(self):
        """Test OCSP cache returns cached result."""
        from app.auth.pki_ocsp import _get_ocsp_cache
        from app.auth.pki_ocsp import _set_ocsp_cache

        serial = "12345"
        _set_ocsp_cache(serial, False, ttl_seconds=300)

        result = _get_ocsp_cache(serial)
        assert result is False

    def test_ocsp_cache_miss(self):
        """Test OCSP cache returns None on miss."""
        from app.auth.pki_ocsp import _get_ocsp_cache

        result = _get_ocsp_cache("nonexistent-serial")
        assert result is None

    def test_ocsp_cache_expired(self):
        """Test OCSP cache returns None for expired entry."""
        from app.auth.pki_ocsp import _get_ocsp_cache
        from app.auth.pki_ocsp import _set_ocsp_cache

        serial = "expired-serial"
        _set_ocsp_cache(serial, False, ttl_seconds=-1)  # Already expired

        result = _get_ocsp_cache(serial)
        assert result is None

    def test_ocsp_cache_revoked(self):
        """Test OCSP cache stores revoked status."""
        from app.auth.pki_ocsp import _get_ocsp_cache
        from app.auth.pki_ocsp import _set_ocsp_cache

        serial = "revoked-serial"
        _set_ocsp_cache(serial, True, ttl_seconds=300)

        result = _get_ocsp_cache(serial)
        assert result is True

    def test_get_ocsp_url_extraction(self, valid_certificate_pem):
        """Test OCSP URL extraction from certificate (may be None for self-signed)."""
        from app.auth.pki_ocsp import _get_ocsp_url

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        # Self-signed test certificates don't have OCSP URLs
        url = _get_ocsp_url(cert)
        assert url is None  # Expected for self-signed

    def test_ocsp_request_timeout(self):
        """Test OCSP request handles timeout."""
        from app.auth.pki_ocsp import _send_ocsp_request

        with patch("app.auth.pki_ocsp.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.TimeoutException("timeout")
            )

            result = _send_ocsp_request("http://ocsp.example.com", b"request")
            assert result is None

    def test_ocsp_request_error(self):
        """Test OCSP request handles network error."""
        from app.auth.pki_ocsp import _send_ocsp_request

        with patch("app.auth.pki_ocsp.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.post.side_effect = httpx.RequestError(
                "network error"
            )

            result = _send_ocsp_request("http://ocsp.example.com", b"request")
            assert result is None


@skip_pki_unit
class TestCRLChecking:
    """Test CRL revocation checking (mocked)."""

    def test_crl_cache_hit(self):
        """Test CRL cache returns cached CRL."""
        from app.auth.pki_revocation import _get_crl_cache
        from app.auth.pki_revocation import _set_crl_cache

        # Create a mock CRL
        mock_crl = MagicMock(spec=x509.CertificateRevocationList)
        url = "http://crl.example.com/crl.der"

        _set_crl_cache(url, mock_crl)

        result = _get_crl_cache(url)
        assert result is mock_crl

    def test_crl_cache_miss(self):
        """Test CRL cache returns None on miss."""
        from app.auth.pki_revocation import _get_crl_cache

        result = _get_crl_cache("http://nonexistent.example.com/crl.der")
        assert result is None

    def test_get_crl_urls_extraction(self, valid_certificate_pem):
        """Test CRL URL extraction from certificate (may be None for self-signed)."""
        from app.auth.pki_revocation import _get_crl_urls

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        # Self-signed test certificates don't have CRL URLs
        urls = _get_crl_urls(cert)
        assert urls is None  # Expected for self-signed

    def test_crl_download_timeout(self):
        """Test CRL download handles timeout."""
        from app.auth.pki_revocation import _download_crl

        with patch("app.auth.pki_revocation.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.get.side_effect = (
                httpx.TimeoutException("timeout")
            )

            result = _download_crl("http://crl.example.com/crl.der")
            assert result is None

    def test_crl_download_error(self):
        """Test CRL download handles network error."""
        from app.auth.pki_revocation import _download_crl

        with patch("app.auth.pki_revocation.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.get.side_effect = httpx.RequestError(
                "network error"
            )

            result = _download_crl("http://crl.example.com/crl.der")
            assert result is None


@skip_pki_unit
class TestRevocationChecking:
    """Test combined revocation checking."""

    def test_revocation_disabled(self, valid_certificate_pem):
        """Test revocation check is skipped when disabled."""
        from app.auth.pki_auth import _handle_revocation_check

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        set_pki(pki_verify_revocation=False)

        result = _handle_revocation_check(cert)
        assert result is False  # False means proceed (don't deny)

    def test_revocation_no_cert_soft_fail(self):
        """Test revocation check with no certificate and soft-fail enabled."""
        from app.auth.pki_auth import _handle_revocation_check

        set_pki(pki_verify_revocation=True, pki_revocation_soft_fail=True)

        result = _handle_revocation_check(None)
        assert result is False  # Soft-fail allows

    def test_revocation_no_cert_hard_fail(self):
        """Test revocation check with no certificate and hard-fail."""
        from app.auth.pki_auth import _handle_revocation_check

        set_pki(pki_verify_revocation=True, pki_revocation_soft_fail=False)

        result = _handle_revocation_check(None)
        assert result is True  # Hard-fail denies

    def test_check_revocation_soft_fail(self, valid_certificate_pem):
        """Test revocation check soft-fail when OCSP/CRL unavailable."""
        from app.auth.pki_revocation import _check_revocation

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        set_pki(pki_ca_cert_path="", pki_revocation_soft_fail=True)

        # Mock OCSP and CRL to return None (unavailable)
        with patch("app.auth.pki_revocation._check_ocsp", return_value=None):
            with patch("app.auth.pki_revocation._check_crl", return_value=None):
                is_revoked, reason = _check_revocation(cert)

                assert not is_revoked
                assert "soft-fail" in reason

    def test_check_revocation_hard_fail(self, valid_certificate_pem):
        """Test revocation check hard-fail when OCSP/CRL unavailable."""
        from app.auth.pki_revocation import _check_revocation

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        set_pki(pki_ca_cert_path="", pki_revocation_soft_fail=False)

        # Mock OCSP and CRL to return None (unavailable)
        with patch("app.auth.pki_revocation._check_ocsp", return_value=None):
            with patch("app.auth.pki_revocation._check_crl", return_value=None):
                is_revoked, reason = _check_revocation(cert)

                assert is_revoked
                assert "hard-fail" in reason
