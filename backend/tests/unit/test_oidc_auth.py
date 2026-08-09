"""Unit tests for the OIDC authentication helpers.

Covers:
- PKCE code verifier validation (RFC 7636)
- PKCE pair generation
- Certificate claim extraction from OIDC tokens (IdP-brokered PKI / gov flows)
- Realm-fallback URL construction
"""

from app.auth.oidc import OIDCConfig
from app.auth.oidc import generate_pkce_pair
from app.auth.oidc import validate_pkce_code_verifier
from app.auth.oidc.claims import _extract_certificate_claims
from app.auth.oidc.endpoints import _get_realm_urls

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**kwargs: object) -> OIDCConfig:
    return OIDCConfig(
        enabled=bool(kwargs.get("enabled", True)),
        server_url=str(kwargs.get("server_url", "https://keycloak.example.com")),
        internal_url=str(kwargs.get("internal_url", "")),
        realm=str(kwargs.get("realm", "opentranscribe")),
        client_id=str(kwargs.get("client_id", "transcribe")),
        client_secret=str(kwargs.get("client_secret", "secret")),
    )


# ---------------------------------------------------------------------------
# PKCE — RFC 7636
# ---------------------------------------------------------------------------


class TestValidatePkceCodeVerifier:
    def test_valid_verifier(self):
        # 64-char string of unreserved chars
        verifier = "a" * 64
        assert validate_pkce_code_verifier(verifier) is True

    def test_min_length_accepted(self):
        assert validate_pkce_code_verifier("a" * 43) is True

    def test_max_length_accepted(self):
        assert validate_pkce_code_verifier("a" * 128) is True

    def test_too_short_rejected(self):
        assert validate_pkce_code_verifier("a" * 42) is False

    def test_too_long_rejected(self):
        assert validate_pkce_code_verifier("a" * 129) is False

    def test_empty_rejected(self):
        assert validate_pkce_code_verifier("") is False

    def test_all_unreserved_chars_accepted(self):
        # Use all allowed character classes
        verifier = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" * 2)[:64]
        assert validate_pkce_code_verifier(verifier) is True

    def test_space_rejected(self):
        verifier = "a" * 63 + " "
        assert validate_pkce_code_verifier(verifier) is False

    def test_plus_sign_rejected(self):
        # '+' is NOT an unreserved character per RFC 7636
        verifier = "a" * 63 + "+"
        assert validate_pkce_code_verifier(verifier) is False

    def test_equals_sign_rejected(self):
        # base64 padding chars must not appear in the verifier
        verifier = "a" * 63 + "="
        assert validate_pkce_code_verifier(verifier) is False


class TestGeneratePkcePair:
    def test_returns_tuple_of_two_strings(self):
        verifier, challenge = generate_pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)

    def test_verifier_passes_validation(self):
        verifier, _ = generate_pkce_pair()
        assert validate_pkce_code_verifier(verifier) is True

    def test_verifier_length_in_range(self):
        verifier, _ = generate_pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_is_base64url(self):
        _, challenge = generate_pkce_pair()
        # base64url uses only A-Z, a-z, 0-9, - and _ (no padding =)
        import re

        assert re.fullmatch(r"[A-Za-z0-9\-_]+", challenge)

    def test_each_call_produces_unique_pair(self):
        v1, c1 = generate_pkce_pair()
        v2, c2 = generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2

    def test_challenge_is_s256_of_verifier(self):
        """Verify the challenge is SHA-256(verifier) base64url-encoded per RFC 7636 §4.2."""
        import base64
        import hashlib

        verifier, challenge = generate_pkce_pair()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected


# ---------------------------------------------------------------------------
# Certificate claim extraction (IdP-brokered PKI / government OIDC flows)
# ---------------------------------------------------------------------------


class TestExtractCertificateClaims:
    """An X.509 authenticator can inject cert metadata into OIDC tokens
    using either the short claim names (cert_dn, cert_serial …) or the
    x509_cert_* aliases. Both must be supported for government PKI compliance.
    """

    def test_short_claim_names(self):
        payload = {
            "cert_dn": "CN=DOE.JOHN.1234567890,OU=USA,O=U.S. Government,C=US",
            "cert_serial": "AABBCCDD",
            "cert_issuer": "CN=DOD CA-59",
            "cert_org": "U.S. Government",
            "cert_ou": "USA",
            "cert_valid_from": "2024-01-01T00:00:00Z",
            "cert_valid_until": "2027-01-01T00:00:00Z",
            "cert_fingerprint": "AA:BB:CC:DD",
        }
        result = _extract_certificate_claims(payload)
        assert result["cert_dn"] == "CN=DOE.JOHN.1234567890,OU=USA,O=U.S. Government,C=US"
        assert result["cert_serial"] == "AABBCCDD"
        assert result["cert_issuer"] == "CN=DOD CA-59"
        assert result["cert_org"] == "U.S. Government"
        assert result["cert_ou"] == "USA"
        assert result["cert_valid_from"] == "2024-01-01T00:00:00Z"
        assert result["cert_valid_until"] == "2027-01-01T00:00:00Z"
        assert result["cert_fingerprint"] == "AA:BB:CC:DD"

    def test_x509_alias_claim_names(self):
        """The X.509 authenticator uses the x509_cert_* prefix by default."""
        payload = {
            "x509_cert_dn": "CN=DOE.JANE.9876543210,OU=USA,O=U.S. Government,C=US",
            "x509_cert_serial": "11223344",
            "x509_cert_issuer": "CN=DOD CA-60",
            "x509_cert_org": "U.S. Government",
            "x509_cert_ou": "USA",
            "x509_cert_not_before": "2024-06-01T00:00:00Z",
            "x509_cert_not_after": "2027-06-01T00:00:00Z",
            "x509_cert_sha256_fingerprint": "FF:EE:DD:CC",
        }
        result = _extract_certificate_claims(payload)
        assert result["cert_dn"] == "CN=DOE.JANE.9876543210,OU=USA,O=U.S. Government,C=US"
        assert result["cert_serial"] == "11223344"
        assert result["cert_issuer"] == "CN=DOD CA-60"
        assert result["cert_org"] == "U.S. Government"
        assert result["cert_ou"] == "USA"
        assert result["cert_valid_from"] == "2024-06-01T00:00:00Z"
        assert result["cert_valid_until"] == "2027-06-01T00:00:00Z"
        assert result["cert_fingerprint"] == "FF:EE:DD:CC"

    def test_short_claims_take_priority_over_aliases(self):
        """If both claim forms are present, short names win."""
        payload = {
            "cert_dn": "CN=SHORT",
            "x509_cert_dn": "CN=ALIAS",
            "cert_serial": "AAAA",
            "x509_cert_serial": "BBBB",
        }
        result = _extract_certificate_claims(payload)
        assert result["cert_dn"] == "CN=SHORT"
        assert result["cert_serial"] == "AAAA"

    def test_empty_token_returns_all_none(self):
        result = _extract_certificate_claims({})
        assert all(v is None for v in result.values())

    def test_partial_claims_fills_missing_with_none(self):
        payload = {"cert_dn": "CN=ONLY_DN"}
        result = _extract_certificate_claims(payload)
        assert result["cert_dn"] == "CN=ONLY_DN"
        assert result["cert_serial"] is None
        assert result["cert_issuer"] is None
        assert result["cert_fingerprint"] is None

    def test_gov_space_separated_dn(self):
        """Government X.509 format: CN=LastName FirstName emailusername.
        The email username has no @domain — just the local part.
        """
        payload = {
            "cert_dn": "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US",
            "cert_issuer": "CN=DOD ID CA-59,OU=PKI,OU=DoD,O=U.S. Government,C=US",
            "cert_serial": "AABB1122",
        }
        result = _extract_certificate_claims(payload)
        assert result["cert_dn"] == "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
        assert "DOD ID CA-59" in result["cert_issuer"]
        assert result["cert_serial"] == "AABB1122"

    def test_piv_cert_issuer_format(self):
        """PIV cards use a different CA chain than CAC cards."""
        payload = {
            "cert_dn": "CN=Smith Jane jsmith,OU=Agency,O=U.S. Government,C=US",
            "cert_issuer": "CN=Federal Bridge CA G4,OU=FPKI,O=U.S. Government,C=US",
        }
        result = _extract_certificate_claims(payload)
        assert "Federal Bridge" in result["cert_issuer"]
        assert "jsmith" in result["cert_dn"]


# ---------------------------------------------------------------------------
# Government DN display-name parsing (shared path for PKI and IdP-brokered PKI)
# ---------------------------------------------------------------------------


class TestGovDNDisplayName:
    """Tests for extract_display_name_from_gov_dn covering the space-separated format
    that government deployments use when the IdP acts as the X.509 broker.
    """

    def _parse(self, dn: str) -> str:
        from app.auth.pki_auth import extract_display_name_from_gov_dn

        return extract_display_name_from_gov_dn(dn)

    # --- Government space-separated format ---

    def test_gov_format_basic(self):
        """CN=LastName FirstName emailusername → 'FirstName LastName'."""
        assert self._parse("CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US") == "John Doe"

    def test_gov_format_email_user_ignored(self):
        """The email username token (no @domain) must not appear in the display name."""
        result = self._parse("CN=Smith Jane jsmith123,OU=Agency,O=U.S. Government,C=US")
        assert result == "Jane Smith"
        assert "jsmith123" not in result

    def test_gov_format_case_normalised(self):
        """Mixed-case input is title-cased correctly."""
        assert (
            self._parse("CN=WILLIAMS ROBERT rwilliams,O=U.S. Government,C=US") == "Robert Williams"
        )

    def test_gov_format_hyphenated_surname(self):
        """Hyphenated last names are preserved as-is after title-casing."""
        result = self._parse("CN=Garcia-Lopez Maria mgarcia,O=U.S. Government,C=US")
        assert "Maria" in result
        assert "Garcia-Lopez" in result

    # --- Existing dot-separated formats still work ---

    def test_dod_cac_dot_format_still_works(self):
        assert self._parse("CN=SMITH.JOHN.W.1234567890,O=U.S. Government,C=US") == "John W. Smith"

    def test_piv_dot_format_still_works(self):
        assert self._parse("CN=DOE.JANE.M,O=U.S. Government,C=US") == "Jane M. Doe"

    # --- Edge cases ---

    def test_two_token_cn_returned_as_is(self):
        """A plain 'First Last' CN with no email token is returned unchanged."""
        result = self._parse("CN=John Doe,O=Company,C=US")
        assert result == "John Doe"

    def test_no_cn_returns_full_dn(self):
        dn = "O=Organization,C=US"
        assert self._parse(dn) == dn


# ---------------------------------------------------------------------------
# PKI admin DN parsing (same semicolon-delimiter fix as LDAP)
# ---------------------------------------------------------------------------


class TestIsPkiAdmin:
    """_is_pki_admin() must use semicolons to split PKI_ADMIN_DNS because full
    DNs contain commas internally.
    """

    def _check(self, subject_dn: str, admin_dns_setting: str) -> bool:
        from unittest.mock import patch

        from app.auth.pki_auth import _is_pki_admin

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ADMIN_DNS = admin_dns_setting
            return _is_pki_admin(subject_dn)

    def test_single_full_dn_matches(self):
        dn = "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
        assert self._check(dn, dn) is True

    def test_single_full_dn_no_comma_split(self):
        """A full DN must not be split on its internal commas."""
        dn = "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
        # Setting contains the full DN — should match, not fragment it
        assert self._check(dn, "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US") is True

    def test_multiple_admin_dns_semicolon_separated(self):
        dn = "CN=Smith Jane jsmith,OU=Agency,O=U.S. Government,C=US"
        setting = (
            "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
            ";CN=Smith Jane jsmith,OU=Agency,O=U.S. Government,C=US"
        )
        assert self._check(dn, setting) is True

    def test_non_admin_dn_denied(self):
        setting = "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
        assert self._check("CN=Other User ouser,OU=Agency,O=U.S. Government,C=US", setting) is False

    def test_empty_setting_denies_all(self):
        assert self._check("CN=Anyone,O=Gov,C=US", "") is False

    def test_case_insensitive_match(self):
        dn_upper = "CN=DOE JOHN JDOE,OU=AGENCY,O=U.S. GOVERNMENT,C=US"
        dn_lower = "cn=doe john jdoe,ou=agency,o=u.s. government,c=us"
        assert self._check(dn_upper, dn_lower) is True


# ---------------------------------------------------------------------------
# IdP-brokered-PKI admin promotion
# ---------------------------------------------------------------------------


class TestOIDCPkiAdminPromotion:
    """When the IdP acts as the X.509/PKI broker, a user whose cert DN is in
    PKI_ADMIN_DNS must receive admin status even if the provider role is not
    assigned.
    """

    def _make_payload(self, roles: list, cert_dn: str | None = None) -> dict:
        payload: dict = {
            "sub": "user-uuid-123",
            "email": "jdoe@agency.gov",
            "name": "John Doe",
            "preferred_username": "jdoe",
            "realm_access": {"roles": roles},
        }
        if cert_dn:
            payload["cert_dn"] = cert_dn
        return payload

    def test_admin_via_provider_role(self):
        payload = self._make_payload(roles=["admin"])
        cert_claims = _extract_certificate_claims(payload)
        roles = payload.get("realm_access", {}).get("roles", [])
        assert "admin" in roles
        assert cert_claims["cert_dn"] is None

    def test_admin_via_cert_dn_when_no_provider_role(self):
        """Cert DN in PKI_ADMIN_DNS must grant admin even without the provider role."""
        from unittest.mock import patch

        from app.auth.pki_auth import _is_pki_admin

        gov_dn = "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
        payload = self._make_payload(roles=["user"], cert_dn=gov_dn)

        cert_claims = _extract_certificate_claims(payload)
        roles = payload.get("realm_access", {}).get("roles", [])
        is_admin = "admin" in roles  # False — no provider role

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ADMIN_DNS = gov_dn
            if not is_admin and cert_claims.get("cert_dn"):
                if _is_pki_admin(cert_claims["cert_dn"]):
                    is_admin = True

        assert is_admin is True

    def test_non_admin_cert_dn_not_promoted(self):
        """A cert DN not in PKI_ADMIN_DNS must not grant admin."""
        from unittest.mock import patch

        from app.auth.pki_auth import _is_pki_admin

        gov_dn = "CN=Doe John jdoe,OU=Agency,O=U.S. Government,C=US"
        other_dn = "CN=Smith Jane jsmith,OU=Agency,O=U.S. Government,C=US"
        payload = self._make_payload(roles=["user"], cert_dn=other_dn)

        cert_claims = _extract_certificate_claims(payload)
        is_admin = False

        with patch("app.auth.pki_auth.settings") as mock_settings:
            mock_settings.PKI_ADMIN_DNS = gov_dn  # only jdoe is admin
            if not is_admin and cert_claims.get("cert_dn"):
                if _is_pki_admin(cert_claims["cert_dn"]):
                    is_admin = True

        assert is_admin is False

    def test_no_cert_claims_uses_role_only(self):
        """When no cert claims are present, admin is role-based only."""
        payload = self._make_payload(roles=["user"])
        cert_claims = _extract_certificate_claims(payload)
        assert cert_claims["cert_dn"] is None
        # No cert → no PKI admin check possible → role-based only


# ---------------------------------------------------------------------------
# Realm-fallback URL construction
# ---------------------------------------------------------------------------


class TestGetRealmUrls:
    def test_standard_urls_built_from_server_and_realm(self):
        cfg = _cfg(server_url="https://keycloak.example.com", realm="myrealm")
        urls = _get_realm_urls(cfg)
        assert urls["authorization"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/auth"
        )
        assert urls["token"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/token"
        )
        assert urls["certs"] == (
            "https://keycloak.example.com/realms/myrealm/protocol/openid-connect/certs"
        )

    def test_internal_url_used_for_backend_calls(self):
        cfg = _cfg(
            server_url="https://keycloak.example.com",
            internal_url="http://keycloak:8080",
            realm="myrealm",
        )
        urls = _get_realm_urls(cfg, internal=True)
        # token and certs use internal URL; auth still uses external
        assert urls["token"].startswith("http://keycloak:8080")
        assert urls["authorization"].startswith("https://keycloak.example.com")

    def test_fallback_to_server_url_when_no_internal(self):
        cfg = _cfg(server_url="https://keycloak.example.com", internal_url="", realm="myrealm")
        urls = _get_realm_urls(cfg, internal=True)
        assert urls["token"].startswith("https://keycloak.example.com")
