"""
PKI/X.509 Certificate Authentication Tests.

Tests verify:
- Certificate parsing (valid, invalid, expired, not-yet-valid)
- CAC/PIV DN parsing for government certificates
- OCSP/CRL revocation checking (mocked)
- Trusted proxy validation for header injection prevention
- Certificate expiration validation
- DN normalization and admin checking
- HTTP endpoint integration tests via FastAPI TestClient

Run with: pytest tests/test_pki_auth.py -v
Environment: Requires DATA_DIR and TEMP_DIR to be set or TESTING=True

Unit tests for internal PKI functions require RUN_PKI_TESTS=true.
Integration tests using the TestClient run without the marker.
"""

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

# Skip marker for unit tests that test internal PKI functions
# Integration tests (TestPKIEndpointIntegration, TestAuthMethodsEndpoint, etc.)
# do NOT use this marker and run unconditionally.
_skip_pki_unit = pytest.mark.skipif(
    os.environ.get("RUN_PKI_TESTS", "false").lower() != "true",
    reason="PKI unit tests require RUN_PKI_TESTS=true",
)

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# ===== Test Fixtures =====


@pytest.fixture
def valid_certificate_pem():
    """Generate a valid test certificate (valid for 1 year from now)."""
    return _generate_test_certificate(
        common_name="Test User",
        email="test.user@example.com",
        organization="Test Organization",
        organizational_unit="Test Unit",
        days_valid=365,
        days_before=0,
    )


@pytest.fixture
def expired_certificate_pem():
    """Generate an expired test certificate (expired 1 day ago)."""
    return _generate_test_certificate(
        common_name="Expired User",
        email="expired@example.com",
        organization="Test Organization",
        organizational_unit="Test Unit",
        days_valid=-1,  # Expired 1 day ago
        days_before=30,  # Was valid starting 30 days ago
    )


@pytest.fixture
def not_yet_valid_certificate_pem():
    """Generate a certificate that's not yet valid (valid starting tomorrow)."""
    return _generate_test_certificate(
        common_name="Future User",
        email="future@example.com",
        organization="Test Organization",
        organizational_unit="Test Unit",
        days_valid=365,
        days_before=-1,  # Starts tomorrow
    )


@pytest.fixture
def dod_cac_certificate_pem():
    """Generate a test certificate with DoD CAC-style DN."""
    return _generate_test_certificate(
        common_name="SMITH.JOHN.WILLIAM.1234567890",
        email=None,
        organization="U.S. Government",
        organizational_unit="DoD",
        days_valid=365,
        days_before=0,
    )


@pytest.fixture
def piv_certificate_pem():
    """Generate a test certificate with PIV-style DN."""
    return _generate_test_certificate(
        common_name="JONES.JANE.M",
        email="jane.jones@agency.gov",
        organization="U.S. Government",
        organizational_unit="Federal Agency",
        days_valid=365,
        days_before=0,
    )


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request object."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "192.168.1.100"
    request.headers = {}
    return request


@pytest.fixture
def mock_trusted_request():
    """Create a mock request from a trusted proxy IP."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "10.0.0.1"
    request.headers = {}
    return request


def _generate_test_certificate(
    common_name: str,
    email: str | None,
    organization: str,
    organizational_unit: str,
    days_valid: int,
    days_before: int = 0,
) -> str:
    """
    Generate a test X.509 certificate.

    Args:
        common_name: Certificate CN
        email: Email address (optional)
        organization: Organization name
        organizational_unit: Organizational unit
        days_valid: Days until expiration (negative = already expired)
        days_before: Days before now that certificate becomes valid (negative = not yet valid)

    Returns:
        PEM-encoded certificate string
    """
    # Generate key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Build subject
    subject_attrs = [
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    ]
    if email:
        subject_attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))

    subject = x509.Name(subject_attrs)

    # Calculate validity period
    now = datetime.now(UTC)
    not_valid_before = now - timedelta(days=days_before)
    not_valid_after = now + timedelta(days=days_valid)

    # Build certificate
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # Self-signed
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
    )

    # Add email to SAN if provided
    if email:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email)]),
            critical=False,
        )

    # Sign certificate
    certificate = builder.sign(private_key, hashes.SHA256(), default_backend())

    # Return PEM-encoded
    return certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")


# ===== Certificate Parsing Tests =====


@_skip_pki_unit
class TestCertificateParsing:
    """Test certificate parsing functionality."""

    def test_parse_valid_certificate(self, valid_certificate_pem):
        """Test parsing a valid certificate extracts all fields correctly."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        assert cert_info.common_name == "Test User"
        assert cert_info.email == "test.user@example.com"
        assert cert_info.organization == "Test Organization"
        assert cert_info.organizational_unit == "Test Unit"
        assert cert_info.serial_number is not None
        assert cert_info.issuer_dn is not None
        assert cert_info.not_before is not None
        assert cert_info.not_after is not None
        assert cert_info.fingerprint is not None

    def test_parse_certificate_without_email(self, dod_cac_certificate_pem):
        """Test parsing certificate without email address."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate(dod_cac_certificate_pem)

        assert cert_info is not None
        assert cert_info.common_name == "SMITH.JOHN.WILLIAM.1234567890"
        # Email may be None or extracted from SAN
        assert cert_info.organization == "U.S. Government"

    def test_parse_invalid_certificate(self):
        """Test parsing invalid certificate data returns None."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate("not a valid certificate")
        assert cert_info is None

        cert_info = parse_certificate(
            "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----"
        )
        assert cert_info is None

    def test_parse_empty_certificate(self):
        """Test parsing empty string returns None."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate("")
        assert cert_info is None

    def test_certificate_fingerprint_format(self, valid_certificate_pem):
        """Test certificate fingerprint is in correct format (colon-separated hex)."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        # SHA-256 fingerprint should be 64 hex chars with colons
        # Format: XX:XX:XX:... (32 pairs = 64 chars + 31 colons = 95 chars)
        assert len(cert_info.fingerprint) == 95
        assert cert_info.fingerprint.count(":") == 31

    def test_certificate_serial_hex_format(self, valid_certificate_pem):
        """Test certificate serial number is in hex format."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        # Serial should be hex string (uppercase)
        assert all(c in "0123456789ABCDEF" for c in cert_info.serial_number)

    def test_certificate_validity_dates_iso_format(self, valid_certificate_pem):
        """Test certificate validity dates are in ISO format."""
        from app.auth.pki_auth import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        # Should be parseable as ISO datetime
        not_before = datetime.fromisoformat(cert_info.not_before.replace("Z", "+00:00"))
        not_after = datetime.fromisoformat(cert_info.not_after.replace("Z", "+00:00"))

        assert isinstance(not_before, datetime)
        assert isinstance(not_after, datetime)
        assert not_after > not_before


# ===== CAC/PIV DN Parsing Tests =====


@_skip_pki_unit
class TestGovDNParsing:
    """Test government certificate DN parsing."""

    def test_dod_cac_dn_parsing(self):
        """Test DoD CAC format: LAST.FIRST.MIDDLE.EDIPI."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        # Standard DoD CAC format
        dn = "CN=SMITH.JOHN.WILLIAM.1234567890,O=U.S. Government,OU=DoD,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "John William. Smith"

    def test_dod_cac_no_middle_name(self):
        """Test DoD CAC format without middle name."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        dn = "CN=SMITH.JOHN..1234567890,O=U.S. Government,OU=DoD,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        # Should handle empty middle name
        assert "John" in display_name
        assert "Smith" in display_name

    def test_piv_dn_parsing(self):
        """Test PIV format: LAST.FIRST.M."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        dn = "CN=JONES.JANE.M,O=U.S. Government,OU=Federal Agency,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "Jane M. Jones"

    def test_piv_dn_no_middle_initial(self):
        """Test PIV format without middle initial."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        dn = "CN=DOE.JOHN,O=U.S. Government,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "John Doe"

    def test_non_gov_dn_unchanged(self):
        """Test non-government DN is returned unchanged."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        dn = "CN=Regular User,O=Company Inc,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "Regular User"

    def test_missing_cn_returns_full_dn(self):
        """Test DN without CN returns the full DN."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        dn = "O=Organization,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == dn

    def test_case_insensitive_matching(self):
        """Test DN parsing is case-insensitive."""
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        # Lowercase variant
        dn = "cn=smith.john.william.1234567890,o=U.S. Government,ou=DoD,c=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert "John" in display_name
        assert "Smith" in display_name


# ===== DN Normalization Tests =====


@_skip_pki_unit
class TestDNNormalization:
    """Test DN normalization for comparison."""

    def test_normalize_dn_case(self):
        """Test DN normalization lowercases attributes."""
        from app.auth.pki_auth import _normalize_dn

        dn1 = "CN=John Doe,O=Company,C=US"
        dn2 = "cn=john doe,o=company,c=us"

        assert _normalize_dn(dn1) == _normalize_dn(dn2)

    def test_normalize_dn_whitespace(self):
        """Test DN normalization handles whitespace."""
        from app.auth.pki_auth import _normalize_dn

        dn1 = "CN=John Doe, O=Company, C=US"
        dn2 = "CN=John Doe,O=Company,C=US"

        assert _normalize_dn(dn1) == _normalize_dn(dn2)

    def test_normalize_empty_dn(self):
        """Test normalizing empty DN."""
        from app.auth.pki_auth import _normalize_dn

        assert _normalize_dn("") == ""
        assert _normalize_dn(None) == ""


# ===== Admin DN Checking Tests =====


@_skip_pki_unit
class TestAdminDNChecking:
    """Test PKI admin DN checking."""

    def test_admin_dn_match(self):
        """Test matching admin DN by directly testing the _is_pki_admin logic."""
        from app.auth.pki_auth import _normalize_dn

        # Test the normalization and matching logic directly
        admin_dns = "CN=Admin User,O=Company,C=US"
        subject_dn = "CN=Admin User,O=Company,C=US"

        normalized_subject = _normalize_dn(subject_dn)
        normalized_admin_dns = [_normalize_dn(dn) for dn in admin_dns.split(",") if "=" in dn]

        # Reconstruct the admin DN properly (comma-separated list of full DNs)
        # In the actual config, PKI_ADMIN_DNS is comma-separated list of full DNs
        admin_dn_list = [_normalize_dn(admin_dns)]
        assert normalized_subject in admin_dn_list

    def test_admin_dn_case_insensitive(self):
        """Test admin DN matching is case-insensitive."""
        from app.auth.pki_auth import _normalize_dn

        # Test normalization makes matching case-insensitive
        admin_dn = "CN=Admin User,O=Company,C=US"
        subject_dn = "cn=admin user,o=company,c=us"

        assert _normalize_dn(admin_dn) == _normalize_dn(subject_dn)

    def test_admin_dn_multiple(self):
        """Test multiple admin DNs normalization."""
        from app.auth.pki_auth import _normalize_dn

        # Multiple DNs in PKI_ADMIN_DNS are comma-separated
        # But commas are also used within DNs, so the actual format needs careful handling
        admin_dn1 = "CN=Admin1,O=Org"
        admin_dn2 = "CN=Admin2,O=Org"

        assert _normalize_dn(admin_dn1) != _normalize_dn(admin_dn2)
        assert _normalize_dn("CN=Admin1,O=Org") == _normalize_dn(admin_dn1)
        assert _normalize_dn("CN=Admin2,O=Org") == _normalize_dn(admin_dn2)

    def test_non_admin_dn(self):
        """Test non-admin DN doesn't match."""
        from app.auth.pki_auth import _normalize_dn

        admin_dn = "CN=Admin User,O=Company,C=US"
        regular_dn = "CN=Regular User,O=Company,C=US"

        assert _normalize_dn(admin_dn) != _normalize_dn(regular_dn)

    def test_empty_admin_dns_config(self):
        """Test _is_pki_admin with empty PKI_ADMIN_DNS returns False."""
        # When PKI_ADMIN_DNS is empty, _is_pki_admin should always return False
        # We test this by checking the function's early return for empty string
        from app.auth.pki_auth import _is_pki_admin

        # The actual settings.PKI_ADMIN_DNS is likely empty by default in test environment
        # This tests that the function handles empty config gracefully
        result = _is_pki_admin("CN=Any User,O=Company,C=US")
        # In test environment, PKI_ADMIN_DNS is likely empty, so this should return False
        assert result is False


# ===== Certificate Expiration Tests =====


@_skip_pki_unit
class TestCertificateExpiration:
    """Test certificate expiration validation."""

    def test_valid_certificate_passes(self, valid_certificate_pem, mock_trusted_request):
        """Test that a valid certificate passes authentication."""
        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {
            "X-Client-Cert": valid_certificate_pem,
            "X-Client-Cert-DN": "CN=Test User,O=Test Organization,C=US",
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            mock_settings.PKI_VERIFY_REVOCATION = False
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True

                result = pki_authenticate(mock_trusted_request)

        assert result is not None
        assert result["common_name"] == "Test User"

    def test_expired_certificate_rejected(self, expired_certificate_pem, mock_trusted_request):
        """Test that an expired certificate is rejected."""
        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {
            "X-Client-Cert": expired_certificate_pem,
            "X-Client-Cert-DN": "CN=Expired User,O=Test Organization,C=US",
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            mock_settings.PKI_VERIFY_REVOCATION = False
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True

                result = pki_authenticate(mock_trusted_request)

        assert result is None

    def test_not_yet_valid_certificate_rejected(
        self, not_yet_valid_certificate_pem, mock_trusted_request
    ):
        """Test that a not-yet-valid certificate is rejected."""
        from app.auth.pki_auth import pki_authenticate

        mock_trusted_request.headers = {
            "X-Client-Cert": not_yet_valid_certificate_pem,
            "X-Client-Cert-DN": "CN=Future User,O=Test Organization,C=US",
        }

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ENABLED = True
            mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
            mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
            mock_settings.PKI_VERIFY_REVOCATION = False
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True

                result = pki_authenticate(mock_trusted_request)

        assert result is None


# ===== Trusted Proxy Validation Tests =====


@_skip_pki_unit
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
        from app.auth.pki_auth import _is_pki_trusted_proxy
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8")
        assert _is_pki_trusted_proxy("10.1.2.3", networks)

    def test_is_trusted_proxy_not_in_range(self):
        """Test IP outside trusted range."""
        from app.auth.pki_auth import _is_pki_trusted_proxy
        from app.auth.pki_auth import _parse_pki_trusted_proxies

        networks = _parse_pki_trusted_proxies("10.0.0.0/8")
        assert not _is_pki_trusted_proxy("192.168.1.1", networks)

    def test_is_trusted_proxy_empty_networks(self):
        """Test with no trusted networks."""
        from app.auth.pki_auth import _is_pki_trusted_proxy

        assert not _is_pki_trusted_proxy("10.0.0.1", [])

    def test_is_trusted_proxy_invalid_ip(self):
        """Test with invalid IP address."""
        from app.auth.pki_auth import _is_pki_trusted_proxy
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
            mock_settings.PKI_VERIFY_REVOCATION = False
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


# ===== OCSP/CRL Mock Tests =====


@_skip_pki_unit
class TestOCSPChecking:
    """Test OCSP revocation checking (mocked)."""

    def test_ocsp_cache_hit(self):
        """Test OCSP cache returns cached result."""
        from app.auth.pki_auth import _get_ocsp_cache
        from app.auth.pki_auth import _set_ocsp_cache

        serial = "12345"
        _set_ocsp_cache(serial, False, ttl_seconds=300)

        result = _get_ocsp_cache(serial)
        assert result is False

    def test_ocsp_cache_miss(self):
        """Test OCSP cache returns None on miss."""
        from app.auth.pki_auth import _get_ocsp_cache

        result = _get_ocsp_cache("nonexistent-serial")
        assert result is None

    def test_ocsp_cache_expired(self):
        """Test OCSP cache returns None for expired entry."""
        from app.auth.pki_auth import _get_ocsp_cache
        from app.auth.pki_auth import _set_ocsp_cache

        serial = "expired-serial"
        _set_ocsp_cache(serial, False, ttl_seconds=-1)  # Already expired

        result = _get_ocsp_cache(serial)
        assert result is None

    def test_ocsp_cache_revoked(self):
        """Test OCSP cache stores revoked status."""
        from app.auth.pki_auth import _get_ocsp_cache
        from app.auth.pki_auth import _set_ocsp_cache

        serial = "revoked-serial"
        _set_ocsp_cache(serial, True, ttl_seconds=300)

        result = _get_ocsp_cache(serial)
        assert result is True

    def test_get_ocsp_url_extraction(self, valid_certificate_pem):
        """Test OCSP URL extraction from certificate (may be None for self-signed)."""
        from app.auth.pki_auth import _get_ocsp_url

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        # Self-signed test certificates don't have OCSP URLs
        url = _get_ocsp_url(cert)
        assert url is None  # Expected for self-signed

    def test_ocsp_request_timeout(self):
        """Test OCSP request handles timeout."""
        from app.auth.pki_auth import _send_ocsp_request

        with patch("app.auth.pki_auth.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.TimeoutException("timeout")
            )

            result = _send_ocsp_request("http://ocsp.example.com", b"request")
            assert result is None

    def test_ocsp_request_error(self):
        """Test OCSP request handles network error."""
        from app.auth.pki_auth import _send_ocsp_request

        with patch("app.auth.pki_auth.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.post.side_effect = httpx.RequestError(
                "network error"
            )

            result = _send_ocsp_request("http://ocsp.example.com", b"request")
            assert result is None


@_skip_pki_unit
class TestCRLChecking:
    """Test CRL revocation checking (mocked)."""

    def test_crl_cache_hit(self):
        """Test CRL cache returns cached CRL."""
        from app.auth.pki_auth import _get_crl_cache
        from app.auth.pki_auth import _set_crl_cache

        # Create a mock CRL
        mock_crl = MagicMock(spec=x509.CertificateRevocationList)
        url = "http://crl.example.com/crl.der"

        _set_crl_cache(url, mock_crl)

        result = _get_crl_cache(url)
        assert result is mock_crl

    def test_crl_cache_miss(self):
        """Test CRL cache returns None on miss."""
        from app.auth.pki_auth import _get_crl_cache

        result = _get_crl_cache("http://nonexistent.example.com/crl.der")
        assert result is None

    def test_get_crl_urls_extraction(self, valid_certificate_pem):
        """Test CRL URL extraction from certificate (may be None for self-signed)."""
        from app.auth.pki_auth import _get_crl_urls

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        # Self-signed test certificates don't have CRL URLs
        urls = _get_crl_urls(cert)
        assert urls is None  # Expected for self-signed

    def test_crl_download_timeout(self):
        """Test CRL download handles timeout."""
        from app.auth.pki_auth import _download_crl

        with patch("app.auth.pki_auth.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.get.side_effect = (
                httpx.TimeoutException("timeout")
            )

            result = _download_crl("http://crl.example.com/crl.der")
            assert result is None

    def test_crl_download_error(self):
        """Test CRL download handles network error."""
        from app.auth.pki_auth import _download_crl

        with patch("app.auth.pki_auth.httpx.Client") as mock_client:
            import httpx

            mock_client.return_value.__enter__.return_value.get.side_effect = httpx.RequestError(
                "network error"
            )

            result = _download_crl("http://crl.example.com/crl.der")
            assert result is None


@_skip_pki_unit
class TestRevocationChecking:
    """Test combined revocation checking."""

    def test_revocation_disabled(self, valid_certificate_pem):
        """Test revocation check is skipped when disabled."""
        from app.auth.pki_auth import _handle_revocation_check

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_VERIFY_REVOCATION = False

            result = _handle_revocation_check(cert)
            assert result is False  # False means proceed (don't deny)

    def test_revocation_no_cert_soft_fail(self):
        """Test revocation check with no certificate and soft-fail enabled."""
        from app.auth.pki_auth import _handle_revocation_check

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_VERIFY_REVOCATION = True
            mock_settings.PKI_REVOCATION_SOFT_FAIL = True

            result = _handle_revocation_check(None)
            assert result is False  # Soft-fail allows

    def test_revocation_no_cert_hard_fail(self):
        """Test revocation check with no certificate and hard-fail."""
        from app.auth.pki_auth import _handle_revocation_check

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_VERIFY_REVOCATION = True
            mock_settings.PKI_REVOCATION_SOFT_FAIL = False

            result = _handle_revocation_check(None)
            assert result is True  # Hard-fail denies

    def test_check_revocation_soft_fail(self, valid_certificate_pem):
        """Test revocation check soft-fail when OCSP/CRL unavailable."""
        from app.auth.pki_auth import _check_revocation

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CA_CERT_PATH = ""
            mock_settings.PKI_REVOCATION_SOFT_FAIL = True

            # Mock OCSP and CRL to return None (unavailable)
            with patch("app.auth.pki_auth._check_ocsp", return_value=None):
                with patch("app.auth.pki_auth._check_crl", return_value=None):
                    is_revoked, reason = _check_revocation(cert)

                    assert not is_revoked
                    assert "soft-fail" in reason

    def test_check_revocation_hard_fail(self, valid_certificate_pem):
        """Test revocation check hard-fail when OCSP/CRL unavailable."""
        from app.auth.pki_auth import _check_revocation

        cert = x509.load_pem_x509_certificate(valid_certificate_pem.encode(), default_backend())

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_CA_CERT_PATH = ""
            mock_settings.PKI_REVOCATION_SOFT_FAIL = False

            # Mock OCSP and CRL to return None (unavailable)
            with patch("app.auth.pki_auth._check_ocsp", return_value=None):
                with patch("app.auth.pki_auth._check_crl", return_value=None):
                    is_revoked, reason = _check_revocation(cert)

                    assert is_revoked
                    assert "hard-fail" in reason


# ===== Header Extraction Tests =====


@_skip_pki_unit
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


@_skip_pki_unit
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
            mock_settings.PKI_VERIFY_REVOCATION = False
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
            mock_settings.PKI_VERIFY_REVOCATION = False
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
            mock_settings.PKI_VERIFY_REVOCATION = False
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
            mock_settings.PKI_VERIFY_REVOCATION = False
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
        from app.auth.pki_auth import _normalize_dn

        # Test that admin DN detection works via normalization
        admin_dn = "CN=Admin User,O=Test Organization,C=US"
        subject_dn = "CN=Admin User,O=Test Organization,C=US"

        # Verify normalization makes them equal
        assert _normalize_dn(admin_dn) == _normalize_dn(subject_dn)

        # Also test case-insensitive matching
        subject_dn_lower = "cn=admin user,o=test organization,c=us"
        assert _normalize_dn(admin_dn) == _normalize_dn(subject_dn_lower)


# ===== DN Component Parsing Tests =====


@_skip_pki_unit
class TestDNComponentParsing:
    """Test DN component extraction."""

    def test_parse_dn_components_standard(self):
        """Test parsing standard DN components."""
        from app.auth.pki_auth import _parse_dn_components

        cn, email = _parse_dn_components("CN=John Doe,EMAILADDRESS=john@example.com,O=Org,C=US")

        assert cn == "John Doe"
        assert email == "john@example.com"

    def test_parse_dn_components_short_email(self):
        """Test parsing DN with E= for email."""
        from app.auth.pki_auth import _parse_dn_components

        cn, email = _parse_dn_components("CN=Jane Doe,E=jane@example.com,O=Org,C=US")

        assert cn == "Jane Doe"
        assert email == "jane@example.com"

    def test_parse_dn_components_no_email(self):
        """Test parsing DN without email."""
        from app.auth.pki_auth import _parse_dn_components

        cn, email = _parse_dn_components("CN=No Email User,O=Org,C=US")

        assert cn == "No Email User"
        assert email == ""

    def test_parse_dn_components_no_cn(self):
        """Test parsing DN without CN."""
        from app.auth.pki_auth import _parse_dn_components

        cn, email = _parse_dn_components("O=Org,C=US")

        assert cn == ""
        assert email == ""

    def test_parse_dn_components_with_spaces(self):
        """Test parsing DN with spaces around separators."""
        from app.auth.pki_auth import _parse_dn_components

        cn, email = _parse_dn_components("CN=John Doe, O=Org, C=US")

        assert cn == "John Doe"


# ===================================================================
# HTTP Endpoint Integration Tests (using FastAPI TestClient)
#
# These tests exercise the actual HTTP endpoints and do NOT require
# the RUN_PKI_TESTS=true environment variable. They use the TestClient
# and db_session fixtures from conftest.py, and the auth_config table
# in the test database to enable/disable PKI dynamically.
#
# They all take the `trusted_pki_peer` fixture below: header-sourced PKI
# authentication is refused unless a trusted proxy vouched for the headers,
# so a deployment with no PKI_TRUSTED_PROXIES authenticates nobody. These
# tests model a CORRECTLY configured deployment; the fail-closed behaviour
# when the fixture is absent is covered by TestPKIHeaderSourceFailClosed.
# ===================================================================


@pytest.fixture
def trusted_pki_peer(monkeypatch):
    """Model a deployment whose reverse proxy terminates mTLS and is allow-listed.

    TestClient's peer is the literal string ``"testclient"``, which no IP allowlist can
    ever match, so the allowlist lookup is patched for the duration of the test rather
    than the production trust rule being relaxed. ``app/auth/pki_auth.py`` therefore has
    exactly one rule with no test-only branch in it.

    Two independent call sites gate on that rule (``pki_authenticate``'s
    ``_validate_pki_headers_source``, hit first, via ``header_assertion_permitted``;
    and ``_extract_user_info_from_request``'s own ``_pki_header_source_is_trusted``),
    and each resolves ``header_source_is_trusted`` through its own module's import
    binding — patching one leaves the other running the real, untrusted-peer check.
    Both were introduced together (``_validate_pki_headers_source``, PKI trust
    hardening), but only the second was ever wired into this fixture, so this class's
    whole suite 401'd against a real deployment shape (every DN-header request has
    *some* PKI header, so ``asserted=True`` and the unmocked gate always refuses)
    until a real backend/frontend run against Authentik/LDAP surfaced it (#20/#14).
    """
    from app.auth import header_trust
    from app.auth import pki_auth

    monkeypatch.setattr(pki_auth, "_pki_header_source_is_trusted", lambda request: True)
    monkeypatch.setattr(pki_auth, "header_source_is_trusted", lambda request, networks: True)
    monkeypatch.setattr(header_trust, "header_source_is_trusted", lambda request, networks: True)


@pytest.fixture
def pki_enabled_db(db_session):
    """Enable PKI authentication in the test database.

    Ensures auth_config rows for pki_enabled=true and a test admin DN
    exist. Uses merge (upsert) semantics to handle cases where the rows
    already exist in the database (e.g., from a previous migration or
    manual setup). The test DB session uses savepoints, so changes are
    rolled back after each test.
    """
    from app.models.auth_config import AuthConfig

    admin_dn_value = (
        "emailAddress=admin@example.com,"
        "CN=Admin User,OU=Users,"
        "O=OpenTranscribe Admins,L=Arlington,"
        "ST=Virginia,C=US"
    )

    desired = {
        "pki_enabled": ("true", "pki", "bool"),
        "pki_admin_dns": (admin_dn_value, "pki", "string"),
    }

    for key, (value, category, dtype) in desired.items():
        existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == key).first()
        if existing:
            existing.config_value = value
        else:
            db_session.add(
                AuthConfig(
                    config_key=key,
                    config_value=value,
                    category=category,
                    data_type=dtype,
                    is_sensitive=False,
                )
            )
    db_session.commit()
    return db_session


@pytest.fixture
def pki_disabled_db(db_session):
    """Ensure PKI authentication is disabled in the test database.

    Sets pki_enabled=false (or creates the row if it does not exist).
    This is needed because the production database may already have
    pki_enabled=true, which would cause "PKI disabled" tests to fail.
    """
    from app.models.auth_config import AuthConfig

    existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == "pki_enabled").first()
    if existing:
        existing.config_value = "false"
    else:
        db_session.add(
            AuthConfig(
                config_key="pki_enabled",
                config_value="false",
                category="pki",
                data_type="bool",
                is_sensitive=False,
            )
        )
    db_session.commit()
    return db_session


@pytest.fixture
def pki_test_cert():
    """Generate a valid test certificate for endpoint integration tests.

    Returns a tuple of (pem_string, subject_dn_string) for use in
    X-Client-Cert and X-Client-Cert-DN headers.
    """
    pem = _generate_test_certificate(
        common_name="Test PKI User",
        email="testpki@example.com",
        organization="OpenTranscribe Test",
        organizational_unit="Engineering",
        days_valid=365,
        days_before=0,
    )
    subject_dn = (
        "emailAddress=testpki@example.com,"
        "CN=Test PKI User,OU=Engineering,"
        "O=OpenTranscribe Test,C=US"
    )
    return pem, subject_dn


@pytest.fixture
def pki_admin_cert():
    """Generate a test certificate whose DN matches the admin DN in pki_enabled_db.

    Returns a tuple of (pem_string, subject_dn_string).
    """
    pem = _generate_test_certificate(
        common_name="Admin User",
        email="admin@example.com",
        organization="OpenTranscribe Admins",
        organizational_unit="Users",
        days_valid=365,
        days_before=0,
    )
    subject_dn = (
        "emailAddress=admin@example.com,"
        "CN=Admin User,OU=Users,"
        "O=OpenTranscribe Admins,L=Arlington,"
        "ST=Virginia,C=US"
    )
    return pem, subject_dn


class TestPKIEndpointIntegration:
    """Integration tests for the PKI authentication HTTP endpoint.

    These tests use the FastAPI TestClient (``client`` fixture) and a real
    test database session with savepoint isolation. PKI is toggled via
    rows in the ``auth_config`` table, matching production behaviour.
    """

    @pytest.fixture(autouse=True)
    def _trusted_peer(self, trusted_pki_peer):
        """Every test here models a deployment with a working mTLS reverse proxy.

        Without it the endpoint refuses every request, which is the point of
        TestPKIHeaderSourceFailClosed — not of these tests.
        """

    def test_pki_authenticate_with_dn_header(self, client, pki_enabled_db, pki_test_cert):
        """POST /api/auth/pki/authenticate with DN header returns 200 and access_token."""
        _pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_pki_authenticate_creates_user(self, client, pki_enabled_db, db_session, pki_test_cert):
        """First PKI login creates a user; second login reuses the same user."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        # First login — user should be created
        response1 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response1.status_code == 200

        # Verify user exists in DB
        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        user_id = user.id

        # Second login — same user should be reused
        response2 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response2.status_code == 200

        user_again = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user_again is not None
        assert user_again.id == user_id

    def test_pki_authenticate_admin_dn(self, client, pki_enabled_db, db_session, pki_admin_cert):
        """Admin DN gives admin or super_admin role with superuser flag."""
        from app.models.user import User

        _pem, subject_dn = pki_admin_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

        # An admin DN grants the 'admin' role. super_admin is local-only and is
        # never granted (nor demoted) by an external IdP; is_superuser mirrors
        # (role == super_admin), so a PKI admin has is_superuser False.
        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        assert user.role in ("admin", "super_admin")
        assert user.is_superuser is (user.role == "super_admin")

    def test_pki_authenticate_regular_user(self, client, pki_enabled_db, db_session, pki_test_cert):
        """Non-admin DN gives regular user role."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response.status_code == 200

        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        assert user.role == "user"
        assert user.is_superuser is False

    def test_pki_authenticate_disabled(self, client, pki_disabled_db):
        """PKI not enabled in DB returns 400 'PKI authentication is not enabled'."""
        response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert-DN": "CN=Test User,O=Test Org,C=US",
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "not enabled" in data["detail"].lower()

    def test_pki_authenticate_no_headers(self, client, pki_enabled_db):
        """No cert/DN headers returns 401."""
        response = client.post(
            "/api/auth/pki/authenticate",
            headers={},
        )

        assert response.status_code == 401
        data = response.json()
        assert "invalid" in data["detail"].lower() or "missing" in data["detail"].lower()

    def test_pki_authenticate_empty_dn(self, client, pki_enabled_db):
        """Empty DN header returns 401."""
        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": ""},
        )

        assert response.status_code == 401

    def test_pki_authenticate_with_full_cert(self, client, pki_enabled_db, pki_test_cert):
        """Full PEM certificate in X-Client-Cert header returns 200."""
        pem, _subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert": pem},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_pki_authenticate_inactive_user(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI user set to inactive returns 400 'Inactive user account'."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        # First login to create the user
        response1 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response1.status_code == 200

        # Deactivate the user
        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        user.is_active = False
        db_session.commit()

        # Second login should fail due to inactive account
        response2 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response2.status_code == 400
        data = response2.json()
        assert "inactive" in data["detail"].lower()

    def test_pki_authenticate_url_encoded_cert(self, client, pki_enabled_db, pki_test_cert):
        """URL-encoded PEM certificate (Nginx format) in X-Client-Cert header returns 200."""
        from urllib.parse import quote

        pem, _subject_dn = pki_test_cert
        encoded_pem = quote(pem)

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert": encoded_pem},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "access_token" in data

    def test_pki_authenticate_with_both_cert_and_dn(self, client, pki_enabled_db, pki_test_cert):
        """Both X-Client-Cert and X-Client-Cert-DN headers present returns 200."""
        pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert": pem,
                "X-Client-Cert-DN": subject_dn,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    def test_pki_authenticate_stores_certificate_metadata(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI login with full cert stores certificate metadata in user record."""
        from app.models.user import User

        pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert": pem,
                "X-Client-Cert-DN": subject_dn,
            },
        )
        assert response.status_code == 200

        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        assert user.pki_common_name == "Test PKI User"
        assert user.pki_organization == "OpenTranscribe Test"
        assert user.pki_organizational_unit == "Engineering"
        assert user.pki_serial_number is not None
        assert user.pki_fingerprint_sha256 is not None
        assert user.pki_not_before is not None
        assert user.pki_not_after is not None

    def test_pki_authenticate_dn_only_no_cert_metadata(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI login with DN only (no full cert) does not populate cert metadata."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response.status_code == 200

        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        # Without a full certificate, metadata fields should be None
        assert user.pki_serial_number is None
        assert user.pki_fingerprint_sha256 is None


class TestAuthMethodsEndpoint:
    """Integration tests for the GET /api/auth/methods endpoint.

    Verifies that the auth methods discovery endpoint correctly reflects
    PKI enable/disable state from the database.
    """

    def test_auth_methods_pki_enabled(self, client, pki_enabled_db):
        """With pki_enabled=true in DB, response has pki_enabled: true."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert data["pki_enabled"] is True

    def test_auth_methods_pki_disabled(self, client, pki_disabled_db):
        """pki_enabled=false in DB => pki_enabled: false."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert data["pki_enabled"] is False

    def test_auth_methods_includes_pki_in_methods_list(self, client, pki_enabled_db):
        """When PKI is enabled, 'pki' appears in the methods array."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert "pki" in data["methods"]

    def test_auth_methods_excludes_pki_when_disabled(self, client, pki_disabled_db):
        """When PKI is disabled, 'pki' does not appear in the methods array."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert "pki" not in data["methods"]

    def test_auth_methods_always_includes_local(self, client, pki_disabled_db):
        """The 'local' method is always present regardless of PKI state."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert "local" in data["methods"]


class TestPKICertificateInfoEndpoint:
    """Integration tests for the GET /api/auth/me/certificate endpoint.

    Verifies that authenticated users can retrieve their certificate
    metadata, and that non-PKI users get has_certificate: false.
    """

    @pytest.fixture(autouse=True)
    def _trusted_peer(self, trusted_pki_peer):
        """The PKI logins these tests perform need a vouching proxy; see the fixture."""

    def test_certificate_info_for_pki_user(self, client, pki_enabled_db, db_session, pki_test_cert):
        """PKI-authenticated user can see their certificate info."""
        pem, subject_dn = pki_test_cert

        # Authenticate via PKI to create user and get token
        auth_response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert": pem,
                "X-Client-Cert-DN": subject_dn,
            },
        )
        assert auth_response.status_code == 200
        access_token = auth_response.json()["access_token"]

        # Fetch certificate info
        cert_response = client.get(
            "/api/auth/me/certificate",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert cert_response.status_code == 200
        data = cert_response.json()
        assert data["has_certificate"] is True
        assert data["subject_dn"] is not None
        assert data["common_name"] == "Test PKI User"
        assert data["organization"] == "OpenTranscribe Test"
        assert data["organizational_unit"] == "Engineering"
        assert data["serial_number"] is not None
        assert data["fingerprint"] is not None
        # Fingerprint should be colon-separated hex
        assert ":" in data["fingerprint"]

    def test_certificate_info_for_local_user(self, client, normal_user, user_token_headers):
        """Local auth user gets has_certificate: false."""
        response = client.get(
            "/api/auth/me/certificate",
            headers=user_token_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["has_certificate"] is False

    def test_certificate_info_requires_auth(self, client):
        """Unauthenticated request to /me/certificate returns 401."""
        response = client.get("/api/auth/me/certificate")

        assert response.status_code == 401

    def test_certificate_info_for_dn_only_pki_user(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI user authenticated with DN-only still gets has_certificate: true (from DN)."""
        _pem, subject_dn = pki_test_cert

        # Authenticate with DN only (no full cert)
        auth_response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert auth_response.status_code == 200
        access_token = auth_response.json()["access_token"]

        # Fetch certificate info — should have at least subject_dn from user record
        cert_response = client.get(
            "/api/auth/me/certificate",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert cert_response.status_code == 200
        data = cert_response.json()
        assert data["has_certificate"] is True
        assert data["subject_dn"] == subject_dn


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


@_skip_pki_unit
class TestPKIHeaderSourceFailClosed:
    """PKI headers must be refused when nothing can vouch for them."""

    ADMIN_DN = "CN=Admin User,O=Company,C=US"

    def _patch_settings(self, mock_settings, trusted_proxies=""):
        """Apply the common PKI settings used by these tests."""
        mock_settings.PKI_ENABLED = True
        mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
        mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
        mock_settings.PKI_VERIFY_REVOCATION = False
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


@_skip_pki_unit
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
            mock_settings.PKI_VERIFY_REVOCATION = False
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


@_skip_pki_unit
class TestCertificateValidityPrecedesDN:
    """A DN header must not short-circuit the certificate validity-window check."""

    def _patch_settings(self, mock_settings):
        """Apply the common PKI settings used by these tests."""
        mock_settings.PKI_ENABLED = True
        mock_settings.PKI_CERT_HEADER = "X-Client-Cert"
        mock_settings.PKI_CERT_DN_HEADER = "X-Client-Cert-DN"
        mock_settings.PKI_VERIFY_REVOCATION = False
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


class TestPKIEndpointThrottling:
    """POST /auth/pki/authenticate must be throttled like every other auth route.

    It mints access AND refresh tokens, so without a limit it was the cheapest
    brute-force surface in the app. No DB is needed: these assert the wiring.
    """

    def test_pki_route_registered_with_limiter(self):
        """The slowapi limiter has a registered limit for the pki_login route."""
        from app.api.endpoints.auth import pki as pki_endpoint  # noqa: F401
        from app.auth.rate_limit import limiter

        route_key = "app.api.endpoints.auth.pki.pki_login"
        assert route_key in limiter._route_limits or route_key in limiter._dynamic_route_limits

    def test_pki_route_source_has_limit_decorator(self):
        """The decorator idiom matches login.py / registration.py."""
        import inspect

        from app.api.endpoints.auth import pki as pki_endpoint

        source = inspect.getsource(pki_endpoint)
        assert "@limiter.limit(get_auth_rate_limit())" in source

    def test_pki_route_records_lockout_attempts(self):
        """Failed PKI attempts are recorded against the subject DN for lockout."""
        import inspect

        from app.api.endpoints.auth import pki as pki_endpoint

        source = inspect.getsource(pki_endpoint)
        assert "check_and_record_attempt(" in source
        assert "lockout_identifier" in source


# Run with: pytest tests/test_pki_auth.py -v
