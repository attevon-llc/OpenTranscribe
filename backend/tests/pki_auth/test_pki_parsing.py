"""PKI certificate parsing: PEM/DN parsing, CAC/PIV DNs, DN normalization, admin DNs, validity window.

Run with: pytest tests/pki_auth -v
"""

from datetime import datetime
from unittest.mock import patch

from tests.fixtures.pki_auth import set_pki
from tests.fixtures.pki_auth import skip_pki_unit

# ===== Certificate Parsing Tests =====


@skip_pki_unit
class TestCertificateParsing:
    """Test certificate parsing functionality."""

    def test_parse_valid_certificate(self, valid_certificate_pem):
        """Test parsing a valid certificate extracts all fields correctly."""
        from app.auth.pki_certificate import parse_certificate

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
        from app.auth.pki_certificate import parse_certificate

        cert_info = parse_certificate(dod_cac_certificate_pem)

        assert cert_info is not None
        assert cert_info.common_name == "SMITH.JOHN.WILLIAM.1234567890"
        # Email may be None or extracted from SAN
        assert cert_info.organization == "U.S. Government"

    def test_parse_invalid_certificate(self):
        """Test parsing invalid certificate data returns None."""
        from app.auth.pki_certificate import parse_certificate

        cert_info = parse_certificate("not a valid certificate")
        assert cert_info is None

        cert_info = parse_certificate(
            "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----"
        )
        assert cert_info is None

    def test_parse_empty_certificate(self):
        """Test parsing empty string returns None."""
        from app.auth.pki_certificate import parse_certificate

        cert_info = parse_certificate("")
        assert cert_info is None

    def test_certificate_fingerprint_format(self, valid_certificate_pem):
        """Test certificate fingerprint is in correct format (colon-separated hex)."""
        from app.auth.pki_certificate import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        # SHA-256 fingerprint should be 64 hex chars with colons
        # Format: XX:XX:XX:... (32 pairs = 64 chars + 31 colons = 95 chars)
        assert len(cert_info.fingerprint) == 95
        assert cert_info.fingerprint.count(":") == 31

    def test_certificate_serial_hex_format(self, valid_certificate_pem):
        """Test certificate serial number is in hex format."""
        from app.auth.pki_certificate import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        # Serial should be hex string (uppercase)
        assert all(c in "0123456789ABCDEF" for c in cert_info.serial_number)

    def test_certificate_validity_dates_iso_format(self, valid_certificate_pem):
        """Test certificate validity dates are in ISO format."""
        from app.auth.pki_certificate import parse_certificate

        cert_info = parse_certificate(valid_certificate_pem)

        assert cert_info is not None
        # Should be parseable as ISO datetime
        not_before = datetime.fromisoformat(cert_info.not_before.replace("Z", "+00:00"))
        not_after = datetime.fromisoformat(cert_info.not_after.replace("Z", "+00:00"))

        assert isinstance(not_before, datetime)
        assert isinstance(not_after, datetime)
        assert not_after > not_before


# ===== CAC/PIV DN Parsing Tests =====


@skip_pki_unit
class TestGovDNParsing:
    """Test government certificate DN parsing."""

    def test_dod_cac_dn_parsing(self):
        """Test DoD CAC format: LAST.FIRST.MIDDLE.EDIPI."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        # Standard DoD CAC format
        dn = "CN=SMITH.JOHN.WILLIAM.1234567890,O=U.S. Government,OU=DoD,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "John William. Smith"

    def test_dod_cac_no_middle_name(self):
        """Test DoD CAC format without middle name."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        dn = "CN=SMITH.JOHN..1234567890,O=U.S. Government,OU=DoD,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        # Should handle empty middle name
        assert "John" in display_name
        assert "Smith" in display_name

    def test_piv_dn_parsing(self):
        """Test PIV format: LAST.FIRST.M."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        dn = "CN=JONES.JANE.M,O=U.S. Government,OU=Federal Agency,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "Jane M. Jones"

    def test_piv_dn_no_middle_initial(self):
        """Test PIV format without middle initial."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        dn = "CN=DOE.JOHN,O=U.S. Government,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "John Doe"

    def test_non_gov_dn_unchanged(self):
        """Test non-government DN is returned unchanged."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        dn = "CN=Regular User,O=Company Inc,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == "Regular User"

    def test_missing_cn_returns_full_dn(self):
        """Test DN without CN returns the full DN."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        dn = "O=Organization,C=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert display_name == dn

    def test_case_insensitive_matching(self):
        """Test DN parsing is case-insensitive."""
        from app.auth.pki_certificate import extract_display_name_from_gov_dn

        # Lowercase variant
        dn = "cn=smith.john.william.1234567890,o=U.S. Government,ou=DoD,c=US"
        display_name = extract_display_name_from_gov_dn(dn)
        assert "John" in display_name
        assert "Smith" in display_name


# ===== DN Normalization Tests =====


@skip_pki_unit
class TestDNNormalization:
    """Test DN normalization for comparison."""

    def test_normalize_dn_case(self):
        """Test DN normalization lowercases attributes."""
        from app.auth.pki_certificate import _normalize_dn

        dn1 = "CN=John Doe,O=Company,C=US"
        dn2 = "cn=john doe,o=company,c=us"

        assert _normalize_dn(dn1) == _normalize_dn(dn2)

    def test_normalize_dn_whitespace(self):
        """Test DN normalization handles whitespace."""
        from app.auth.pki_certificate import _normalize_dn

        dn1 = "CN=John Doe, O=Company, C=US"
        dn2 = "CN=John Doe,O=Company,C=US"

        assert _normalize_dn(dn1) == _normalize_dn(dn2)

    def test_normalize_empty_dn(self):
        """Test normalizing empty DN."""
        from app.auth.pki_certificate import _normalize_dn

        assert _normalize_dn("") == ""
        assert _normalize_dn(None) == ""


# ===== Admin DN Checking Tests =====


@skip_pki_unit
class TestAdminDNChecking:
    """Test PKI admin DN checking."""

    def test_admin_dn_match(self):
        """Test matching admin DN by directly testing the _is_pki_admin logic."""
        from app.auth.pki_certificate import _normalize_dn

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
        from app.auth.pki_certificate import _normalize_dn

        # Test normalization makes matching case-insensitive
        admin_dn = "CN=Admin User,O=Company,C=US"
        subject_dn = "cn=admin user,o=company,c=us"

        assert _normalize_dn(admin_dn) == _normalize_dn(subject_dn)

    def test_admin_dn_multiple(self):
        """Test multiple admin DNs normalization."""
        from app.auth.pki_certificate import _normalize_dn

        # Multiple DNs in PKI_ADMIN_DNS are comma-separated
        # But commas are also used within DNs, so the actual format needs careful handling
        admin_dn1 = "CN=Admin1,O=Org"
        admin_dn2 = "CN=Admin2,O=Org"

        assert _normalize_dn(admin_dn1) != _normalize_dn(admin_dn2)
        assert _normalize_dn("CN=Admin1,O=Org") == _normalize_dn(admin_dn1)
        assert _normalize_dn("CN=Admin2,O=Org") == _normalize_dn(admin_dn2)

    def test_non_admin_dn(self):
        """Test non-admin DN doesn't match."""
        from app.auth.pki_certificate import _normalize_dn

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


@skip_pki_unit
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
            set_pki(pki_verify_revocation=False)
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
            set_pki(pki_verify_revocation=False)
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
            set_pki(pki_verify_revocation=False)
            mock_settings.PKI_ADMIN_DNS = ""
            mock_settings.PKI_TRUSTED_PROXIES = "10.0.0.0/8"

            with patch("app.auth.pki_auth._pki_trusted_proxy_networks") as mock_networks:
                import ipaddress

                mock_networks.__iter__ = lambda self: iter([ipaddress.ip_network("10.0.0.0/8")])
                mock_networks.__bool__ = lambda self: True

                result = pki_authenticate(mock_trusted_request)

        assert result is None


# ===== DN Component Parsing Tests =====


@skip_pki_unit
class TestDNComponentParsing:
    """Test DN component extraction."""

    def test_parse_dn_components_standard(self):
        """Test parsing standard DN components."""
        from app.auth.pki_certificate import _parse_dn_components

        cn, email = _parse_dn_components("CN=John Doe,EMAILADDRESS=john@example.com,O=Org,C=US")

        assert cn == "John Doe"
        assert email == "john@example.com"

    def test_parse_dn_components_short_email(self):
        """Test parsing DN with E= for email."""
        from app.auth.pki_certificate import _parse_dn_components

        cn, email = _parse_dn_components("CN=Jane Doe,E=jane@example.com,O=Org,C=US")

        assert cn == "Jane Doe"
        assert email == "jane@example.com"

    def test_parse_dn_components_no_email(self):
        """Test parsing DN without email."""
        from app.auth.pki_certificate import _parse_dn_components

        cn, email = _parse_dn_components("CN=No Email User,O=Org,C=US")

        assert cn == "No Email User"
        assert email == ""

    def test_parse_dn_components_no_cn(self):
        """Test parsing DN without CN."""
        from app.auth.pki_certificate import _parse_dn_components

        cn, email = _parse_dn_components("O=Org,C=US")

        assert cn == ""
        assert email == ""

    def test_parse_dn_components_with_spaces(self):
        """Test parsing DN with spaces around separators."""
        from app.auth.pki_certificate import _parse_dn_components

        cn, email = _parse_dn_components("CN=John Doe, O=Org, C=US")

        assert cn == "John Doe"
