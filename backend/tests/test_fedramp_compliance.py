"""
FedRAMP Compliance Test Suite

Tests for all FedRAMP security controls implemented in OpenTranscribe:
- Phase 1: FIPS 140-2 Password Hashing (IA-5)
- Phase 2: Password Policy Enforcement (IA-5)
- Phase 3: Multi-Factor Authentication (IA-2)
- Phase 4: Token Management & Revocation (AC-12)
- Phase 5: Audit Logging (AU-2/AU-3)
- Phase 6: Additional Controls (AC-2, AC-8, AC-10)

NOTE: These tests verify FedRAMP compliance features.
Currently skipped until all FedRAMP features are fully implemented.
Set RUN_FEDRAMP_TESTS=true to run these tests.
"""

from unittest.mock import patch

import pytest

# Runs by DEFAULT. This module was gated behind RUN_FEDRAMP_TESTS with the reason
# "FedRAMP compliance features in development" — but every test in it passes, and did so on the first run once the gate
# was lifted. The gate was stale: it kept 25 security tests out of every local run and
# out of CI, visible only as `s` in the progress dots, while reading as a deliberate
# decision someone had made. That is how `test_super_admin_can_export_audit_logs` came to
# assert `status_code in [200, 400]` — 400 being exactly 'could not export' — without
# anyone noticing (issue #431).
#
# The pre-merge gate still runs these; the difference is they now also run by default,
# so a regression surfaces on the commit that causes it rather than at merge time.


class TestPasswordPolicy:
    """Tests for Phase 2: Password Policy Enforcement (FedRAMP IA-5)"""

    def test_password_min_length_enforced(self):
        """Password must meet minimum length requirement."""
        from app.auth.password_policy import validate_password

        # Too short — message reads "must be at least 12 characters long"
        result = validate_password("Short1!", "test@example.com")
        assert not result.is_valid
        assert any("characters" in e.lower() for e in result.errors)

        # Meets minimum (12 chars default)
        result = validate_password("SecureP@ss12", "test@example.com")
        # May still fail other requirements, but not length

    def test_password_complexity_requirements(self):
        """Password must contain uppercase, lowercase, digit, and special char."""
        from app.auth.password_policy import validate_password

        # Missing uppercase
        result = validate_password("securepass123!", "test@example.com")
        assert not result.is_valid
        assert any("uppercase" in e.lower() for e in result.errors)

        # Missing lowercase
        result = validate_password("SECUREPASS123!", "test@example.com")
        assert not result.is_valid
        assert any("lowercase" in e.lower() for e in result.errors)

        # Missing digit
        result = validate_password("SecurePassword!", "test@example.com")
        assert not result.is_valid
        assert any("digit" in e.lower() for e in result.errors)

        # Missing special character
        result = validate_password("SecurePassword1", "test@example.com")
        assert not result.is_valid
        assert any("special" in e.lower() for e in result.errors)

        # All requirements met
        result = validate_password("SecureP@ssw0rd!", "test@example.com")
        assert result.is_valid

    def test_password_cannot_contain_email(self):
        """Password must not contain the user's email username."""
        from app.auth.password_policy import validate_password

        result = validate_password("TestUser@123!", "testuser@example.com")
        assert not result.is_valid
        assert any("email" in e.lower() or "user" in e.lower() for e in result.errors)

    def test_password_cannot_contain_name(self):
        """Password must not contain parts of user's name."""
        from app.auth.password_policy import validate_password

        result = validate_password("JohnSmith@123!", "john@example.com", full_name="John Smith")
        assert not result.is_valid
        assert any("name" in e.lower() for e in result.errors)

    def test_get_policy_requirements(self):
        """Policy requirements endpoint returns correct values."""
        from app.auth.password_policy import get_policy_requirements

        requirements = get_policy_requirements()
        # The EXACT key set, not five spot checks. This dict is returned verbatim
        # as the body of GET /api/auth/password-policy, so every key is a wire
        # contract the registration and change-password forms read. A per-key
        # `in` check cannot see a key that was renamed, dropped, or never added:
        # `min_age_hours` shipped without one and nothing failed.
        assert set(requirements) == {
            "enabled",
            "min_length",
            "require_uppercase",
            "require_lowercase",
            "require_digit",
            "require_special",
            "special_characters",
            "history_count",
            "max_age_days",
            "min_age_hours",
        }
        assert requirements["min_length"] >= 12  # FedRAMP minimum


class TestMFAService:
    """Tests for Phase 3: Multi-Factor Authentication (FedRAMP IA-2)"""

    def test_totp_secret_generation(self):
        """TOTP secret should be cryptographically secure."""
        from app.auth.mfa import MFAService

        secret1 = MFAService.generate_totp_secret()
        secret2 = MFAService.generate_totp_secret()

        # Secrets should be unique
        assert secret1 != secret2

        # Secret should be base32 encoded and proper length
        assert len(secret1) == 32  # 160 bits = 20 bytes = 32 base32 chars

    def test_provisioning_uri_format(self):
        """Provisioning URI should follow otpauth:// format."""
        from app.auth.mfa import MFAService

        secret = MFAService.generate_totp_secret()
        uri = MFAService.get_provisioning_uri(secret, "test@example.com", "TestApp")

        assert uri.startswith("otpauth://totp/")
        assert "test%40example.com" in uri or "test@example.com" in uri
        assert "secret=" in uri
        assert "issuer=" in uri

    def test_qr_code_generation(self):
        """QR code should be generated as valid base64 PNG."""
        import base64

        from app.auth.mfa import MFAService

        secret = MFAService.generate_totp_secret()
        uri = MFAService.get_provisioning_uri(secret, "test@example.com")
        qr_base64 = MFAService.generate_qr_code_base64(uri)

        # Should be valid base64
        decoded = base64.b64decode(qr_base64)
        # PNG magic bytes
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_totp_verification(self):
        """TOTP verification should accept valid codes."""
        import pyotp

        from app.auth.mfa import MFAService

        secret = MFAService.generate_totp_secret()
        totp = pyotp.TOTP(secret)
        valid_code = totp.now()

        # Valid code should pass
        assert MFAService.verify_totp(secret, valid_code)

        # Invalid code should fail
        assert not MFAService.verify_totp(secret, "000000")

        # Wrong format should fail
        assert not MFAService.verify_totp(secret, "12345")  # Too short
        assert not MFAService.verify_totp(secret, "abcdef")  # Not digits

    def test_backup_code_generation(self):
        """Backup codes should be unique and properly formatted."""
        from app.auth.mfa import MFAService

        codes = MFAService.generate_backup_codes(10)

        assert len(codes) == 10
        # All codes should be unique
        assert len(set(codes)) == 10
        # Codes should be formatted as XXXX-XXXX
        for code in codes:
            assert "-" in code
            assert len(code.replace("-", "")) == 8

    def test_backup_code_hashing_and_verification(self):
        """Backup codes should be securely hashed and verifiable."""
        from app.auth.mfa import MFAService
        from app.core.config import settings

        codes = MFAService.generate_backup_codes(5)
        hashed_codes = MFAService.hash_backup_codes(codes)

        # Hashes should differ from plaintext; scheme follows FIPS mode
        # (PBKDF2-SHA256 in FIPS mode, bcrypt otherwise)
        for code, hashed in zip(codes, hashed_codes, strict=True):
            assert code != hashed
            if settings.FIPS_MODE:
                assert hashed.startswith("$pbkdf2-sha256$")
            else:
                assert hashed.startswith(("$2b$", "$2a$", "$bcrypt-sha256$"))

        # Verification should work
        is_valid, matched_hash = MFAService.verify_backup_code(codes[0], hashed_codes)
        assert is_valid
        assert matched_hash == hashed_codes[0]

        # Invalid code should fail
        is_valid, matched_hash = MFAService.verify_backup_code("INVALID-CODE", hashed_codes)
        assert not is_valid
        assert matched_hash is None


class TestTokenService:
    """Tests for Phase 4: Token Management & Revocation (FedRAMP AC-12)"""

    def test_refresh_token_hash_storage(self):
        """Refresh tokens should be stored as hashes, not plaintext."""
        from app.auth.token_service import TokenService

        service = TokenService()
        token = "test_token_value"  # noqa: S105 - test fixture
        hashed = service._hash_token(token)

        # Hash should be SHA-512 (128 hex chars) per FIPS 140-3
        assert len(hashed) == 128
        assert token not in hashed

        # Same token should produce same hash
        assert service._hash_token(token) == hashed

    def test_token_revocation_key_format(self):
        """Revocation keys should follow expected pattern."""
        from app.auth.token_service import REVOKED_TOKEN_PREFIX

        jti = "test-jti-12345"
        expected_key = f"{REVOKED_TOKEN_PREFIX}{jti}"

        assert expected_key == "revoked:jti:test-jti-12345"


class TestFIPSPasswordHashing:
    """Tests for Phase 1: FIPS 140-2 Password Hashing"""

    def test_pbkdf2_available(self):
        """PBKDF2-SHA256 should be available for FIPS mode."""
        from passlib.hash import pbkdf2_sha256

        password = "TestPassword123!"  # noqa: S105 - test fixture
        hashed = pbkdf2_sha256.hash(password)

        assert pbkdf2_sha256.verify(password, hashed)
        assert hashed.startswith("$pbkdf2-sha256$")

    def test_password_verification_backward_compatible(self):
        """System should verify both bcrypt and PBKDF2 hashes."""
        from app.core.security import get_password_hash
        from app.core.security import verify_password

        password = "TestPassword123!"  # noqa: S105 - test fixture
        hashed = get_password_hash(password)

        # Should verify correctly
        assert verify_password(password, hashed)

        # Wrong password should fail
        assert not verify_password("WrongPassword!", hashed)


class TestAuditLogging:
    """Tests for Phase 5: Audit Logging (FedRAMP AU-2/AU-3)"""

    def test_audit_event_types_defined(self):
        """All required audit event types should be defined."""
        from app.auth.audit import AuditEventType

        required_events = [
            "AUTH_LOGIN_SUCCESS",
            "AUTH_LOGIN_FAILURE",
            "AUTH_LOGOUT",
            "AUTH_MFA_SETUP",
            "AUTH_MFA_VERIFY",
            "AUTH_PASSWORD_CHANGE",
            "AUTH_ACCOUNT_LOCKOUT",
            "AUTH_TOKEN_REFRESH",
            "AUTH_TOKEN_REVOKE",
        ]

        for event in required_events:
            assert hasattr(AuditEventType, event), f"Missing event type: {event}"

    def test_audit_log_format(self):
        """Audit logs should include required fields."""
        import json

        from app.auth.audit import AuditLogger

        logger = AuditLogger()

        # Mock the actual logging
        with patch.object(logger, "_logger") as mock_log:  # type: ignore[arg-type]
            logger.log_login_success(
                user_id=123,
                username="test@example.com",
                source_ip="192.168.1.1",
                user_agent="Mozilla/5.0",
                auth_method="local",
            )

            # Verify log was called
            mock_log.info.assert_called_once()
            log_message = mock_log.info.call_args[0][0]

            # Should be valid JSON
            log_data = json.loads(log_message)

            # Required fields
            assert "timestamp" in log_data
            assert "event_type" in log_data
            assert "user_id" in log_data
            assert "source_ip" in log_data


class TestAdditionalControls:
    """Tests for Phase 6: Additional FedRAMP Controls"""

    def test_login_banner_configuration(self):
        """Login banner settings should be configurable."""
        from app.core.config import settings

        # Settings should exist
        assert hasattr(settings, "LOGIN_BANNER_ENABLED")
        assert hasattr(settings, "LOGIN_BANNER_TEXT")

    def test_account_expiration_configuration(self):
        """Account expiration settings should be configurable."""
        from app.core.config import settings

        assert hasattr(settings, "ACCOUNT_INACTIVE_DAYS")
        assert hasattr(settings, "ACCOUNT_EXPIRATION_ENABLED")

    def test_session_limit_configuration(self):
        """Concurrent session limits should be configurable."""
        from app.core.config import settings

        assert hasattr(settings, "MAX_CONCURRENT_SESSIONS")


class TestSecurityHeaders:
    """Tests for security headers and CORS configuration"""

    def test_shipped_cors_origins_are_explicit(self):
        """The shipped default CORS list is explicit, never a wildcard.

        Renamed and unguarded. It was ``test_cors_not_wildcard_in_production`` reading
        ``if hasattr(settings, "CORS_ORIGINS"): assert "*" not in ...`` — which tested the
        *testing* config rather than production, and passed vacuously if the attribute were
        ever renamed. The production control (refuse to boot on a wildcard) is a different
        assertion and now has real coverage in
        ``test_production_secrets_guard.py::test_wildcard_cors_rejected_in_production``;
        this keeps the narrower, still-useful check on the default (issue #431).
        """
        from app.core.config import settings

        assert "*" not in settings.CORS_ORIGINS, (
            f"default CORS_ORIGINS must be explicit, got {settings.CORS_ORIGINS}"
        )


# Integration tests (require running app)
class TestIntegrationEndpoints:
    """Integration tests for API endpoints.

    Uses the conftest ``client`` fixture (savepoint-isolated DB session) —
    a bare TestClient here would commit registered users straight into the
    dev database (that leak is exactly how this comment came to exist).
    """

    def test_password_policy_endpoint(self, client):
        """GET /api/auth/password-policy should return policy."""
        response = client.get("/api/auth/password-policy")
        assert response.status_code == 200
        data = response.json()
        assert "min_length" in data
        assert "require_uppercase" in data

    def test_auth_methods_endpoint(self, client):
        """GET /api/auth/methods should return available methods."""
        response = client.get("/api/auth/methods")
        assert response.status_code == 200
        data = response.json()
        assert "methods" in data
        assert "local" in data["methods"]

    def test_weak_password_rejected(self, client):
        """Registration with weak password should fail."""
        response = client.post(
            "/api/auth/register",
            json={
                "email": "test@example.com",
                "password": "weak",
            },
        )
        assert response.status_code == 422, (
            response.text
        )  # schema validation rejects it, not the handler

    def test_strong_password_accepted(self, client):
        """Registration with strong password should succeed."""
        import uuid

        email = f"test-{uuid.uuid4()}@example.com"
        response = client.post(
            "/api/auth/register",
            json={
                "email": email,
                "password": "SecureP@ssw0rd123!",
                "full_name": "Test User",
            },
        )
        # Could be 200/201 for success, or 409 if email exists
        assert response.status_code == 200, response.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
