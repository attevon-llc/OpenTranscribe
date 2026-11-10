"""PKI header trust: trusted-proxy validation, header extraction, fail-closed sources and the authenticate flow.

Run with: pytest tests/pki_auth -v
"""

from unittest.mock import patch

from tests.fixtures.pki_auth import set_pki
from tests.fixtures.pki_auth import skip_pki_unit

# ===== Trusted Proxy Validation Tests =====


@skip_pki_unit
class TestTrustedProxyValidation:
    """Test trusted proxy validation for PKI headers."""

    def test_parse_single_ip(self):
        """Test parsing a single IP address."""
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("192.168.1.1")

        assert len(networks) == 1
        import ipaddress

        assert ipaddress.ip_address("192.168.1.1") in networks[0]

    def test_parse_cidr_range(self):
        """Test parsing CIDR notation."""
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8")

        assert len(networks) == 1
        import ipaddress

        assert ipaddress.ip_address("10.1.2.3") in networks[0]
        assert ipaddress.ip_address("192.168.1.1") not in networks[0]

    def test_parse_multiple_proxies(self):
        """Test parsing multiple proxy addresses."""
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8, 192.168.1.1, 172.16.0.0/12")

        assert len(networks) == 3

    def test_parse_ipv6(self):
        """Test parsing IPv6 addresses."""
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("::1, 2001:db8::/32")

        assert len(networks) == 2

    def test_parse_empty_string(self):
        """Test parsing empty string."""
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("")
        assert networks == []

    def test_parse_invalid_address(self):
        """Test parsing invalid address is skipped."""
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("invalid, 10.0.0.1")
        assert len(networks) == 1

    def test_is_trusted_proxy_in_range(self):
        """Test IP within trusted range."""
        from app.auth.header_trust import ip_in_networks as _is_pki_trusted_proxy
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8")
        assert _is_pki_trusted_proxy("10.1.2.3", networks)

    def test_is_trusted_proxy_not_in_range(self):
        """Test IP outside trusted range."""
        from app.auth.header_trust import ip_in_networks as _is_pki_trusted_proxy
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8")
        assert not _is_pki_trusted_proxy("192.168.1.1", networks)

    def test_is_trusted_proxy_empty_networks(self):
        """Test with no trusted networks."""
        from app.auth.header_trust import ip_in_networks as _is_pki_trusted_proxy

        assert not _is_pki_trusted_proxy("10.0.0.1", [])

    def test_is_trusted_proxy_invalid_ip(self):
        """Test with invalid IP address."""
        from app.auth.header_trust import ip_in_networks as _is_pki_trusted_proxy
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8")
        assert not _is_pki_trusted_proxy("invalid", networks)

    def test_untrusted_proxy_with_pki_headers_rejected(self, valid_certificate_pem, mock_request):
        """Test that PKI headers from untrusted source are rejected."""
        from app.auth.pki_auth import pki_authenticate

        mock_request.headers = {
            "X-Client-Cert": valid_certificate_pem,
            "X-Client-Cert-DN": "CN=Test User,O=Test Organization,C=US",
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                # Set up the mock to behave like a list
                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True
                mock_networks.__len__ = lambda self: 1

                result = pki_authenticate(mock_request)

        # Should be rejected because 192.168.1.100 is not in 10.0.0.0/8
        assert result is None


# ===== Header Extraction Tests =====


@skip_pki_unit
class TestHeaderExtraction:
    """Test certificate header extraction."""

    def test_extract_certificate_from_headers(self, valid_certificate_pem, mock_request):
        """Test extracting certificate from headers."""
        from app.auth.pki_auth import extract_certificate_from_headers

        mock_request.headers = {"X-Client-Cert": valid_certificate_pem}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"

            cert = extract_certificate_from_headers(mock_request)
            assert cert == valid_certificate_pem

    def test_extract_certificate_missing_header(self, mock_request):
        """Test extracting certificate when header is missing."""
        from app.auth.pki_auth import extract_certificate_from_headers

        mock_request.headers = {}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"

            cert = extract_certificate_from_headers(mock_request)
            assert cert is None

    def test_extract_dn_from_headers(self, mock_request):
        """Test extracting DN from headers."""
        from app.auth.pki_auth import extract_dn_from_headers

        mock_request.headers = {"X-Client-Cert-DN": "CN=Test User,O=Org,C=US"}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"

            dn = extract_dn_from_headers(mock_request)
            assert dn == "CN=Test User,O=Org,C=US"

    def test_extract_dn_missing_header(self, mock_request):
        """Test extracting DN when header is missing."""
        from app.auth.pki_auth import extract_dn_from_headers

        mock_request.headers = {}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"

            dn = extract_dn_from_headers(mock_request)
            assert dn is None

    def test_extract_url_encoded_certificate(self, valid_certificate_pem, mock_request):
        """Test extracting URL-encoded certificate (Nginx format)."""
        from urllib.parse import quote

        from app.auth.pki_auth import extract_certificate_from_headers

        encoded_cert = quote(valid_certificate_pem)
        mock_request.headers = {"X-Client-Cert": encoded_cert}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"

            cert = extract_certificate_from_headers(mock_request)
            assert cert == valid_certificate_pem


# ===== PKI Authentication Integration Tests =====


@skip_pki_unit
class TestPKIAuthentication:
    """Test complete PKI authentication flow."""

    def test_pki_disabled(self, mock_request):
        """Test PKI authentication when disabled returns None from pki_authenticate.

        Note: Since pki_authenticate() no longer checks PKI_ENABLED (the caller does),
        this test verifies the function still succeeds with no headers. The actual
        "PKI disabled" behavior is tested via the HTTP endpoint in
        TestPKIEndpointIntegration.test_pki_authenticate_disabled.
        """
        from app.auth.pki_auth import pki_authenticate

        # With no headers and no trusted proxy config, pki_authenticate returns None
        # because there's no certificate or DN to authenticate with
        mock_request.headers = {}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_TRUSTED_PROXIES = ""

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
                result = pki_authenticate(mock_request)
            assert result is None

    def test_pki_no_certificate_or_dn(self, mock_request):
        """Test PKI authentication with no certificate or DN header."""
        from app.auth.pki_auth import pki_authenticate

        mock_request.headers = {}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_TRUSTED_PROXIES = ""

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
                result = pki_authenticate(mock_request)

        assert result is None

    def test_pki_dn_only_authentication(self, mock_trusted_request):
        """Test PKI authentication with DN header only (no full certificate)."""
        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {"X-Client-Cert-DN": "CN=Test User,O=Test Organization,C=US"}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True

                result = pki_authenticate(mock_trusted_request)

        assert result is not None
        assert result["subject_dn"] == "CN=Test User,O=Test Organization,C=US"
        assert result["common_name"] == "Test User"

    def test_pki_returns_user_data(self, valid_certificate_pem, mock_trusted_request):
        """Test PKI authentication returns complete user data."""
        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {
            "X-Client-Cert": valid_certificate_pem,
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True

                result = pki_authenticate(mock_trusted_request)

        assert result is not None
        assert "subject_dn" in result
        assert "common_name" in result
        assert "email" in result
        assert "is_admin" in result
        assert "serial_number" in result
        assert "issuer_dn" in result
        assert "fingerprint" in result

    def test_pki_admin_promotion(self, valid_certificate_pem, mock_trusted_request):
        """Test PKI admin detection is based on normalized DN matching."""
        from app.auth.pki_certificate import _normalize_dn

        # Test that admin DN detection works via normalization
        admin_dn = "CN=Admin User,O=Test Organization,C=US"
        subject_dn = "CN=Admin User,O=Test Organization,C=US"

        # Verify normalization makes them equal
        assert _normalize_dn(admin_dn) == _normalize_dn(subject_dn)

        # Also test case-insensitive matching
        subject_dn_lower = "cn=admin user,o=test organization,c=us"
        assert _normalize_dn(admin_dn) == _normalize_dn(subject_dn_lower)


# ===================================================================
# Fail-closed regression tests
#
# These pin the two header-injection holes found in the PKI audit:
#   1. An empty PKI_TRUSTED_PROXIES used to fail OPEN (warn + allow), so anyone
#      who could reach the backend directly could mint an admin session with a
#      forged DN header. That branch had no test at all, which is why the
#      fail-open survived.
#   2. A DN header must never be trusted unless either a certificate was
#      presented (and passed its validity-window check) or a configured trusted
#      proxy vouched for it.
# ===================================================================


@skip_pki_unit
class TestPKIHeaderSourceFailClosed:
    """PKI headers must be refused when nothing can vouch for them."""

    ADMIN_DN = "CN=Admin User,O=Company,C=US"

    def _patch_settings(self, mock_settings, trusted_proxies=""):
        """Apply the common PKI settings used by these tests."""
        mock_settings.PKI_ENABLED = True
        mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
        mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
        set_pki(pki_verify_revocation=False)
        mock_settings.PKI_ADMIN_DNS = self.ADMIN_DN
        mock_settings.PKI_TRUSTED_PROXIES = trusted_proxies

    def test_forged_dn_would_grant_admin(self):
        """The DN used below really is an admin DN — the refusal is what stops it."""
        from app.auth.pki_auth import _is_pki_admin

        assert _is_pki_admin(self.ADMIN_DN, self.ADMIN_DN) is True

    def test_empty_trusted_proxies_refuses_dn_header(self, mock_request):
        """No PKI_TRUSTED_PROXIES configured => a DN header is refused, not warned about."""
        from app.auth.pki_auth import pki_authenticate

        mock_request.headers = {"X-Client-Cert-DN": self.ADMIN_DN}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
                result = pki_authenticate(mock_request)

        assert result is None

    def test_empty_trusted_proxies_refuses_certificate(self, valid_certificate_pem, mock_request):
        """No PKI_TRUSTED_PROXIES configured => even a valid certificate is refused.

        Without an allowlist there is no proxy that terminated mTLS, so the certificate
        header is just another attacker-supplied string.
        """
        from app.auth.pki_auth import pki_authenticate

        mock_request.headers = {"X-Client-Cert": valid_certificate_pem}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
                result = pki_authenticate(mock_request)

        assert result is None

    def test_empty_trusted_proxies_validator_returns_false(self, mock_request):
        """_validate_pki_headers_source refuses (False), rather than allowing (True)."""
        from app.auth.pki_auth import _validate_pki_headers_source

        mock_request.headers = {"X-Client-Cert-DN": self.ADMIN_DN}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
                assert _validate_pki_headers_source(mock_request) is False

    def test_no_headers_still_allowed_without_trusted_proxies(self, mock_request):
        """A request with no PKI headers is not refused — other auth methods may run."""
        from app.auth.pki_auth import _validate_pki_headers_source

        mock_request.headers = {}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
                assert _validate_pki_headers_source(mock_request) is True

    def test_trust_rule_has_no_environment_escape(self, mock_request):
        """The trust rule is the allowlist and nothing else.

        There is no TESTING / DEBUG / relaxed-environment branch that would let an empty
        allowlist accept headers — the whole suite runs with TESTING=true and a relaxed
        ENVIRONMENT, so if such a branch existed this assertion would flip.
        """
        import inspect

        from app.auth import pki_auth

        with patch("app.auth.pki_auth._pki_trusted_proxy_networks", []):
            assert pki_auth._pki_header_source_is_trusted(mock_request) is False

        source = inspect.getsource(pki_auth._pki_header_source_is_trusted)
        for escape in ("TESTING", "DEBUG", "is_hardened", "ENVIRONMENT"):
            assert escape not in source, f"{escape} must not gate the PKI trust boundary"


@skip_pki_unit
class TestDNOnlyRequiresTrustedProxy:
    """A DN header with no certificate is only credible from a trusted proxy."""

    def test_dn_only_from_untrusted_peer_refused(self, mock_request):
        """DN-only request from an IP outside PKI_TRUSTED_PROXIES is refused."""
        import ipaddress

        from app.auth.pki_auth import pki_authenticate

        # mock_request.client.host is 192.168.1.100, outside 10.0.0.0/8
        mock_request.headers = {"X-Client-Cert-DN": "CN=Test User,O=Test Organization,C=US"}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            networks = [ipaddress.ip_network("10.0.0.0/8")]
            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", networks):
                result = pki_authenticate(mock_request)

        assert result is None

    def test_extract_user_info_refuses_untrusted_dn_only(self, mock_request):
        """The extraction helper itself refuses a DN-only claim from an untrusted peer.

        Defence in depth: even if the source validation upstream were bypassed, the DN
        must not become an identity without a certificate or a vouching proxy.
        """
        import ipaddress

        from app.auth.pki_auth import _extract_user_info_from_request

        mock_request.headers = {"X-Client-Cert-DN": "CN=Test User,O=Test Organization,C=US"}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"

            networks = [ipaddress.ip_network("10.0.0.0/8")]
            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", networks):
                result = _extract_user_info_from_request(mock_request, None, None)

        assert result is None

    def test_extract_user_info_accepts_trusted_dn_only(self, mock_trusted_request):
        """The same claim from a trusted proxy (10.0.0.1) is accepted."""
        import ipaddress

        from app.auth.pki_auth import _extract_user_info_from_request

        mock_trusted_request.headers = {"X-Client-Cert-DN": "CN=Test User,O=Test Org,C=US"}

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"

            networks = [ipaddress.ip_network("10.0.0.0/8")]
            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", networks):
                result = _extract_user_info_from_request(mock_trusted_request, None, None)

        assert result is not None
        assert result[0] == "CN=Test User,O=Test Org,C=US"
        assert result[1] == "Test User"


@skip_pki_unit
class TestCertificateValidityPrecedesDN:
    """A DN header must not short-circuit the certificate validity-window check."""

    def _patch_settings(self, mock_settings):
        """Apply the common PKI settings used by these tests."""
        mock_settings.PKI_ENABLED = True
        mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
        mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
        set_pki(pki_verify_revocation=False)
        mock_settings.PKI_ADMIN_DNS = ""
        mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

    def test_expired_cert_with_dn_header_refused(
        self, expired_certificate_pem, mock_trusted_request
    ):
        """An expired certificate is refused even though a valid-looking DN accompanies it."""
        import ipaddress

        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {
            "X-Client-Cert": expired_certificate_pem,
            "X-Client-Cert-DN": "CN=Expired User,O=Test Organization,C=US",
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            networks = [ipaddress.ip_network("10.0.0.0/8")]
            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", networks):
                result = pki_authenticate(mock_trusted_request)

        assert result is None

    def test_not_yet_valid_cert_with_dn_header_refused(
        self, not_yet_valid_certificate_pem, mock_trusted_request
    ):
        """A not-yet-valid certificate is refused even when a DN header is present."""
        import ipaddress

        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {
            "X-Client-Cert": not_yet_valid_certificate_pem,
            "X-Client-Cert-DN": "CN=Future User,O=Test Organization,C=US",
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            networks = [ipaddress.ip_network("10.0.0.0/8")]
            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", networks):
                result = pki_authenticate(mock_trusted_request)

        assert result is None

    def test_valid_cert_with_dn_header_still_uses_header_dn(
        self, valid_certificate_pem, mock_trusted_request
    ):
        """Backward compatibility: a checked certificate keeps the proxy's canonical DN."""
        import ipaddress

        from app.auth.pki_auth import pki_authenticate

        header_dn = "CN=Test User,O=Test Organization,C=US"
        mock_trusted_request.headers = {
            "X-Client-Cert": valid_certificate_pem,
            "X-Client-Cert-DN": header_dn,
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            self._patch_settings(mock_settings)

            networks = [ipaddress.ip_network("10.0.0.0/8")]
            with patch("app.auth.pki_auth._pki_trusted_proxy_networks", networks):
                result = pki_authenticate(mock_trusted_request)

        assert result is not None
        assert result["subject_dn"] == header_dn
        assert result["fingerprint"] is not None
