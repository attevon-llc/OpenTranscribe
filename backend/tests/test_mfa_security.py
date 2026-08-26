"""
MFA Security Test Suite.

Tests for MFA security features:
- MFA token blacklist Redis failure modes (fail-secure fix)
- Token replay prevention
- Backup code exhaustion
- TOTP window configuration

These tests verify the security guarantees of the MFA implementation.

NOTE: Currently skipped until MFA security features are fully verified.
Set RUN_MFA_TESTS=true to run these tests.
"""

from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pyotp
import pytest
from fastapi import HTTPException
from tests.jwt_compat import jwt

# Runs by DEFAULT. This module was gated behind RUN_MFA_TESTS with the reason
# "MFA security tests need review" — but every test in it passes, and did so on the first run once the gate
# was lifted. The gate was stale: it kept 40 security tests out of every local run and
# out of CI, visible only as `s` in the progress dots, while reading as a deliberate
# decision someone had made. That is how `test_super_admin_can_export_audit_logs` came to
# assert `status_code in [200, 400]` — 400 being exactly 'could not export' — without
# anyone noticing (issue #431).
#
# The pre-merge gate still runs these; the difference is they now also run by default,
# so a regression surfaces on the commit that causes it rather than at merge time.

from app.auth.mfa import MFAService
from app.core.config import settings

# ruff: noqa: I001 - import ordering handled by auto-formatter


class TestMFATokenBlacklistRedisFailure:
    """Tests for MFA token blacklist Redis failure modes (fail-secure fix).

    When MFA_REQUIRE_REDIS is True, the system should fail securely
    (deny access) if Redis is unavailable.
    """

    @pytest.fixture
    def mock_redis_unavailable(self):
        """Mock Redis as unavailable (returns None)."""
        with patch("app.api.endpoints.auth.mfa_tokens.get_redis_client", return_value=None):
            yield

    @pytest.fixture
    def mock_redis_exception(self):
        """Mock Redis raising an exception."""
        mock_client = MagicMock()
        mock_client.set.side_effect = Exception("Redis connection error")
        mock_client.exists.side_effect = Exception("Redis connection error")
        with patch("app.api.endpoints.auth.mfa_tokens.get_redis_client", return_value=mock_client):
            yield

    def test_blacklist_token_redis_unavailable_fail_secure(self, mock_redis_unavailable):
        """When MFA_REQUIRE_REDIS=True and Redis unavailable, blacklisting should raise 503."""
        from app.api.endpoints.auth import _blacklist_mfa_token

        with patch.object(settings, "MFA_REQUIRE_REDIS", True):
            with pytest.raises(HTTPException) as exc_info:
                _blacklist_mfa_token("test-jti-12345", 300)

            assert exc_info.value.status_code == 503
            assert "Auth service unavailable" in exc_info.value.detail

    def test_blacklist_token_redis_unavailable_fail_open(self, mock_redis_unavailable):
        """When MFA_REQUIRE_REDIS=False and Redis unavailable, blacklisting should return False."""
        from app.api.endpoints.auth import _blacklist_mfa_token

        with patch.object(settings, "MFA_REQUIRE_REDIS", False):
            result = _blacklist_mfa_token("test-jti-12345", 300)
            assert result is False

    def test_blacklist_token_redis_exception_fail_secure(self, mock_redis_exception):
        """When MFA_REQUIRE_REDIS=True and Redis raises, blacklisting should raise 503."""
        from app.api.endpoints.auth import _blacklist_mfa_token

        with patch.object(settings, "MFA_REQUIRE_REDIS", True):
            with pytest.raises(HTTPException) as exc_info:
                _blacklist_mfa_token("test-jti-12345", 300)

            assert exc_info.value.status_code == 503
            assert "Auth service unavailable" in exc_info.value.detail

    def test_blacklist_token_redis_exception_fail_open(self, mock_redis_exception):
        """When MFA_REQUIRE_REDIS=False and Redis raises, blacklisting returns False."""
        from app.api.endpoints.auth import _blacklist_mfa_token

        with patch.object(settings, "MFA_REQUIRE_REDIS", False):
            result = _blacklist_mfa_token("test-jti-12345", 300)
            assert result is False

    def test_check_blacklist_redis_unavailable_fail_secure(self, mock_redis_unavailable):
        """When MFA_REQUIRE_REDIS=True and Redis unavailable, check returns True (blocked)."""
        from app.api.endpoints.auth import _is_mfa_token_blacklisted

        with patch.object(settings, "MFA_REQUIRE_REDIS", True):
            # Should return True (assume blacklisted) for fail-secure
            result = _is_mfa_token_blacklisted("test-jti-12345")
            assert result is True

    def test_check_blacklist_redis_unavailable_fail_open(self, mock_redis_unavailable):
        """When MFA_REQUIRE_REDIS=False and Redis unavailable, check returns False."""
        from app.api.endpoints.auth import _is_mfa_token_blacklisted

        with patch.object(settings, "MFA_REQUIRE_REDIS", False):
            result = _is_mfa_token_blacklisted("test-jti-12345")
            assert result is False

    def test_check_blacklist_redis_exception_fail_secure(self, mock_redis_exception):
        """When MFA_REQUIRE_REDIS=True and Redis raises, check returns True (blocked)."""
        from app.api.endpoints.auth import _is_mfa_token_blacklisted

        with patch.object(settings, "MFA_REQUIRE_REDIS", True):
            # Should return True (assume blacklisted) for fail-secure
            result = _is_mfa_token_blacklisted("test-jti-12345")
            assert result is True

    def test_check_blacklist_redis_exception_fail_open(self, mock_redis_exception):
        """When MFA_REQUIRE_REDIS=False and Redis raises, check returns False."""
        from app.api.endpoints.auth import _is_mfa_token_blacklisted

        with patch.object(settings, "MFA_REQUIRE_REDIS", False):
            result = _is_mfa_token_blacklisted("test-jti-12345")
            assert result is False

    def test_blacklist_success_with_redis(self):
        """When Redis is available, blacklisting should succeed."""
        from app.api.endpoints.auth import _blacklist_mfa_token

        mock_client = MagicMock()
        mock_client.set.return_value = True

        with patch("app.api.endpoints.auth.mfa_tokens.get_redis_client", return_value=mock_client):
            result = _blacklist_mfa_token("test-jti-12345", 300)

            assert result is True
            mock_client.set.assert_called_once()
            # Verify the key format
            call_args = mock_client.set.call_args
            assert "mfa:jti:test-jti-12345" in call_args[0][0]
            assert call_args[1]["ex"] == 300

    def test_check_blacklist_success_with_redis(self):
        """When Redis is available, blacklist check should work correctly."""
        from app.api.endpoints.auth import _is_mfa_token_blacklisted

        mock_client = MagicMock()

        # Test token not blacklisted
        mock_client.exists.return_value = 0
        with patch("app.api.endpoints.auth.mfa_tokens.get_redis_client", return_value=mock_client):
            result = _is_mfa_token_blacklisted("test-jti-clean")
            assert result is False

        # Test token is blacklisted
        mock_client.exists.return_value = 1
        with patch("app.api.endpoints.auth.mfa_tokens.get_redis_client", return_value=mock_client):
            result = _is_mfa_token_blacklisted("test-jti-used")
            assert result is True


class TestMFATokenReplayPrevention:
    """Tests for MFA token replay prevention.

    MFA tokens should be single-use. After successful verification,
    the token's JTI should be blacklisted.
    """

    @pytest.fixture
    def mfa_token_data(self):
        """Create a valid MFA token payload."""
        now = datetime.now(UTC)
        return {
            "sub": str(uuid4()),
            "role": "user",
            "type": "mfa",
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }

    @pytest.fixture
    def valid_mfa_token(self, mfa_token_data):
        """Create a valid encoded MFA token."""
        return jwt.encode(mfa_token_data, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    def test_verify_mfa_token_rejects_already_used(self, mfa_token_data, valid_mfa_token):
        """MFA token should be rejected if JTI is already blacklisted."""
        from app.api.endpoints.auth import _verify_mfa_token

        # Mock that the token is already blacklisted (jti from mfa_token_data is embedded in token)
        with patch(
            "app.api.endpoints.auth.mfa_tokens._is_mfa_token_blacklisted", return_value=True
        ):
            with pytest.raises(HTTPException) as exc_info:
                _verify_mfa_token(valid_mfa_token)

            assert exc_info.value.status_code == 401
            assert "already been used" in exc_info.value.detail

    def test_verify_mfa_token_accepts_fresh_token(self, mfa_token_data, valid_mfa_token):
        """MFA token should be accepted if JTI is not blacklisted."""
        from app.api.endpoints.auth import _verify_mfa_token

        # Mock that the token is not blacklisted
        with patch(
            "app.api.endpoints.auth.mfa_tokens._is_mfa_token_blacklisted", return_value=False
        ):
            user_uuid_str, user_role, jti = _verify_mfa_token(valid_mfa_token)

            assert user_uuid_str == mfa_token_data["sub"]
            assert user_role == mfa_token_data["role"]
            assert jti == mfa_token_data["jti"]

    def test_verify_mfa_token_rejects_non_mfa_type(self):
        """Tokens without type='mfa' should be rejected."""
        from app.api.endpoints.auth import _verify_mfa_token

        now = datetime.now(UTC)
        access_token_data = {
            "sub": str(uuid4()),
            "role": "user",
            "type": "access",  # Not MFA type
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=30),
        }
        access_token = jwt.encode(
            access_token_data, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )

        with patch(
            "app.api.endpoints.auth.mfa_tokens._is_mfa_token_blacklisted", return_value=False
        ):
            with pytest.raises(HTTPException) as exc_info:
                _verify_mfa_token(access_token)

            assert exc_info.value.status_code == 401
            assert "Invalid MFA token" in exc_info.value.detail

    def test_verify_mfa_token_rejects_expired(self, mfa_token_data):
        """Expired MFA tokens should be rejected."""
        from app.api.endpoints.auth import _verify_mfa_token

        # Create an expired token
        now = datetime.now(UTC)
        mfa_token_data["iat"] = now - timedelta(minutes=10)
        mfa_token_data["exp"] = now - timedelta(minutes=5)

        expired_token = jwt.encode(
            mfa_token_data, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )

        with pytest.raises(HTTPException) as exc_info:
            _verify_mfa_token(expired_token)

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_mfa_token_blacklisted_after_verification(self):
        """After successful MFA verification, the token JTI should be blacklisted."""
        from app.api.endpoints.auth import _blacklist_mfa_token

        jti = str(uuid4())
        mfa_token_ttl = 300  # 5 minutes

        mock_client = MagicMock()
        mock_client.set.return_value = True

        with patch("app.api.endpoints.auth.mfa_tokens.get_redis_client", return_value=mock_client):
            result = _blacklist_mfa_token(jti, mfa_token_ttl)

            assert result is True
            # Verify correct Redis call
            mock_client.set.assert_called_once()
            call_args = mock_client.set.call_args
            assert f"mfa:jti:{jti}" == call_args[0][0]


class TestBackupCodeExhaustion:
    """Tests for backup code exhaustion scenarios."""

    def test_backup_code_generation_count(self):
        """Backup codes should generate the configured number of codes."""
        codes = MFAService.generate_backup_codes(10)
        assert len(codes) == 10

        codes = MFAService.generate_backup_codes(5)
        assert len(codes) == 5

    def test_backup_code_uniqueness(self):
        """All backup codes should be unique."""
        codes = MFAService.generate_backup_codes(100)
        assert len(set(codes)) == 100

    def test_backup_code_verification_removes_used_code(self):
        """Used backup code should be removable from the list."""
        codes = MFAService.generate_backup_codes(5)
        hashed_codes = MFAService.hash_backup_codes(codes)

        # Verify first code
        is_valid, matched_hash = MFAService.verify_backup_code(codes[0], hashed_codes)
        assert is_valid
        assert matched_hash == hashed_codes[0]

        # Simulate removal of used code
        remaining_codes = [h for h in hashed_codes if h != matched_hash]
        assert len(remaining_codes) == 4

        # Same code should now fail verification
        is_valid, matched_hash = MFAService.verify_backup_code(codes[0], remaining_codes)
        assert not is_valid
        assert matched_hash is None

    def test_backup_code_exhaustion_all_codes_used(self):
        """When all backup codes are used, no more codes should be valid."""
        codes = MFAService.generate_backup_codes(3)
        hashed_codes = MFAService.hash_backup_codes(codes)
        remaining = list(hashed_codes)

        # Use all codes
        for code in codes:
            is_valid, matched_hash = MFAService.verify_backup_code(code, remaining)
            assert is_valid
            remaining = [h for h in remaining if h != matched_hash]

        # No codes left
        assert len(remaining) == 0

        # Any code should now fail
        for code in codes:
            is_valid, _ = MFAService.verify_backup_code(code, remaining)
            assert not is_valid

    def test_backup_code_empty_list_fails(self):
        """Verification against empty code list should fail."""
        is_valid, matched_hash = MFAService.verify_backup_code("ABCD-1234", [])
        assert not is_valid
        assert matched_hash is None

    def test_backup_code_empty_code_fails(self):
        """Empty code string should fail verification."""
        codes = MFAService.generate_backup_codes(3)
        hashed_codes = MFAService.hash_backup_codes(codes)

        is_valid, matched_hash = MFAService.verify_backup_code("", hashed_codes)
        assert not is_valid
        assert matched_hash is None

    def test_backup_code_wrong_format_fails(self):
        """Incorrectly formatted codes should fail."""
        codes = MFAService.generate_backup_codes(3)
        hashed_codes = MFAService.hash_backup_codes(codes)

        # Try various invalid formats
        invalid_codes = [
            "INVALID-CODE",  # Wrong code
            "ABC",  # Too short
            "ABCDEFGHIJKLMNOP",  # Too long
            "12345678",  # All digits (invalid alphabet)
        ]

        for invalid_code in invalid_codes:
            is_valid, matched_hash = MFAService.verify_backup_code(invalid_code, hashed_codes)
            assert not is_valid
            assert matched_hash is None

    def test_backup_code_case_insensitive(self):
        """Backup code verification should be case insensitive."""
        codes = MFAService.generate_backup_codes(1)
        hashed_codes = MFAService.hash_backup_codes(codes)

        # Original code (uppercase)
        original = codes[0]

        # Lowercase should also work
        lowercase = original.lower()
        is_valid, matched_hash = MFAService.verify_backup_code(lowercase, hashed_codes)
        assert is_valid is True
        # The matched hash is what the caller REMOVES from the user's remaining codes.
        # Returning True with no hash would leave a single-use code usable forever, and
        # the sibling refusal tests above already assert `matched_hash is None` — so
        # without this the pair proves nothing about the accepted case.
        assert matched_hash == hashed_codes[0]

    def test_backup_code_ignores_formatting(self):
        """Backup code verification should ignore dashes and spaces."""
        codes = MFAService.generate_backup_codes(1)
        hashed_codes = MFAService.hash_backup_codes(codes)

        # Original code (XXXX-XXXX format)
        original = codes[0]

        # Without dash
        no_dash = original.replace("-", "")
        is_valid, matched_hash = MFAService.verify_backup_code(no_dash, hashed_codes)
        assert is_valid is True
        assert matched_hash == hashed_codes[0]

        # With spaces
        with_spaces = f"{no_dash[:4]} {no_dash[4:]}"
        is_valid, matched_hash = MFAService.verify_backup_code(with_spaces, list(hashed_codes))
        assert is_valid is True
        # Same code, so the SAME stored hash must come back — a normaliser that
        # accepted the spaced form but matched a different entry would burn the wrong
        # single-use code and leave this one live.
        assert matched_hash == hashed_codes[0]


class TestTOTPWindowConfiguration:
    """Tests for configurable TOTP window (valid_window parameter)."""

    def test_totp_valid_window_setting_exists(self):
        """TOTP_VALID_WINDOW setting should exist in config."""
        assert hasattr(settings, "TOTP_VALID_WINDOW")
        assert isinstance(settings.TOTP_VALID_WINDOW, int)

    def test_totp_valid_window_default_value(self):
        """Default TOTP_VALID_WINDOW must be exactly 1 (±30 seconds).

        ``>= 0`` (the old assertion) passes at ANY non-negative value, including a
        misconfigured ``10`` — a ±5-minute TOTP replay window in production. Proven by
        mutation: changing the default from 1 to 10 in ``core/config.py`` left the old
        assertion (and every other test in this class) green.
        """
        assert settings.TOTP_VALID_WINDOW == 1

    def test_totp_verification_uses_configurable_window(self, monkeypatch):
        """A previous-step code — refused at window=0 — must be ACCEPTED once the
        window is widened to 1, through ``MFAService.verify_totp`` itself.

        The old version of this test only asserted the CURRENT code verifies, which is
        true at any window size (including a misconfigured window that doesn't limit
        anything) and proves nothing about the window being enforced at all. This
        exercises the actual claim a "configurable window" makes: the same code, same
        secret, same server clock — accepted or refused purely as a function of the
        setting.
        """
        secret = MFAService.generate_totp_secret()
        totp = pyotp.TOTP(secret, interval=30, digits=6)
        now = datetime.now(UTC)
        previous_step_code = totp.at(now - timedelta(seconds=30))

        monkeypatch.setattr(type(settings), "TOTP_VALID_WINDOW", 0)
        assert MFAService.verify_totp(secret, previous_step_code) is False

        monkeypatch.setattr(type(settings), "TOTP_VALID_WINDOW", 1)
        assert MFAService.verify_totp(secret, previous_step_code) is True

    def test_totp_verification_accepts_within_window(self, monkeypatch):
        """At the default window (1), a code from the boundary — the previous 30s
        step — must be accepted; a code two steps back must not.

        The old version of this test only checked the CENTER of the window (the
        current-time code), which is the one case that verifies at ANY window size
        (including 0) and says nothing about whether "within window" means anything.
        This checks the actual edge the setting is supposed to draw.
        """
        secret = MFAService.generate_totp_secret()
        totp = pyotp.TOTP(secret, interval=30, digits=6)
        now = datetime.now(UTC)

        monkeypatch.setattr(type(settings), "TOTP_VALID_WINDOW", 1)
        previous_step_code = totp.at(now - timedelta(seconds=30))
        two_steps_back_code = totp.at(now - timedelta(seconds=60))

        assert MFAService.verify_totp(secret, previous_step_code) is True
        assert MFAService.verify_totp(secret, two_steps_back_code) is False

    def test_totp_verification_with_custom_window(self, monkeypatch):
        """A window of 2 accepts a code two steps back that window=1 refuses.

        Distinct from the "uses configurable window" test above (window 0 -> 1,
        checking the exact boundary those two windows disagree on): this checks a
        SECOND pair of settings values disagree on a DIFFERENT boundary, so the
        behaviour isn't coincidentally tied to one specific window transition.
        """
        secret = MFAService.generate_totp_secret()
        totp = pyotp.TOTP(secret, interval=30, digits=6)
        now = datetime.now(UTC)
        two_steps_back_code = totp.at(now - timedelta(seconds=60))

        monkeypatch.setattr(type(settings), "TOTP_VALID_WINDOW", 1)
        assert MFAService.verify_totp(secret, two_steps_back_code) is False

        monkeypatch.setattr(type(settings), "TOTP_VALID_WINDOW", 2)
        assert MFAService.verify_totp(secret, two_steps_back_code) is True

    def test_totp_invalid_code_rejected_regardless_of_window(self):
        """Completely invalid codes should be rejected regardless of window size."""
        secret = MFAService.generate_totp_secret()

        # Static invalid code
        assert not MFAService.verify_totp(secret, "000000")

        # Wrong format
        assert not MFAService.verify_totp(secret, "12345")  # Too short
        assert not MFAService.verify_totp(secret, "abcdef")  # Not digits
        assert not MFAService.verify_totp(secret, "")  # Empty

    def test_totp_empty_secret_rejected(self):
        """TOTP verification with empty secret should fail safely."""
        assert not MFAService.verify_totp("", "123456")

    def test_totp_window_zero_only_accepts_current(self, monkeypatch):
        """With TOTP_VALID_WINDOW=0, only the exact current code should work.

        Goes through ``MFAService.verify_totp`` — the app's own code — rather than a
        raw ``pyotp.TOTP.verify()`` call. The previous version of this test called
        ``pyotp`` directly with an explicit ``valid_window=0`` kwarg it invented for the
        occasion, which proves a third-party library invariant, not anything about this
        codebase: the app's actual `verify_totp` reads ``settings.TOTP_VALID_WINDOW``,
        and a test that never sets that setting and never calls that function cannot
        catch a regression in either. Proven by mutation — changing
        ``TOTP_VALID_WINDOW``'s default from 1 to 10 in ``core/config.py`` left the old
        version of this test (and the rest of this class) green.

        The ONLY half is still the point: a replay of the previous OR next 30-second
        step is the attack the narrow window exists to refuse, so both are asserted
        rejected, not just that the current code is accepted (equally true of a
        misconfigured ``valid_window=10``).
        """
        secret = MFAService.generate_totp_secret()
        totp = pyotp.TOTP(secret, interval=30, digits=6)

        monkeypatch.setattr(type(settings), "TOTP_VALID_WINDOW", 0)

        now = datetime.now(UTC)
        current_code = totp.at(now)
        previous_step_code = totp.at(now - timedelta(seconds=30))
        next_step_code = totp.at(now + timedelta(seconds=30))

        assert MFAService.verify_totp(secret, current_code) is True
        assert MFAService.verify_totp(secret, previous_step_code) is False
        assert MFAService.verify_totp(secret, next_step_code) is False

    def test_totp_verification_code_format_normalization(self):
        """TOTP codes should be normalized (remove spaces/dashes)."""
        secret = MFAService.generate_totp_secret()
        totp = pyotp.TOTP(secret, interval=30, digits=6)
        current_code = totp.now()

        # Code with spaces should work
        spaced_code = f"{current_code[:3]} {current_code[3:]}"
        assert MFAService.verify_totp(secret, spaced_code)

        # Code with dashes should work
        dashed_code = f"{current_code[:3]}-{current_code[3:]}"
        assert MFAService.verify_totp(secret, dashed_code)


class TestMFAServiceIntegration:
    """Integration tests for MFA service components."""

    def test_full_mfa_setup_flow(self):
        """Test complete MFA setup flow: generate, encrypt, decrypt, verify."""
        # Generate secret
        secret = MFAService.generate_totp_secret()
        assert len(secret) == 32  # Base32 encoded 20 bytes

        # Encrypt for storage
        encrypted = MFAService.encrypt_totp_secret(secret)
        assert encrypted != secret
        assert encrypted.startswith("v3:")  # FIPS 140-3 format

        # Decrypt for verification
        decrypted = MFAService.decrypt_totp_secret(encrypted)
        assert decrypted == secret

        # Generate and verify TOTP
        totp = pyotp.TOTP(secret)
        current_code = totp.now()
        assert MFAService.verify_totp(secret, current_code)

    def test_backup_code_flow(self):
        """Test complete backup code flow: generate, hash, verify, exhaust."""
        # Generate codes
        codes = MFAService.generate_backup_codes(3)
        assert len(codes) == 3

        # Hash codes
        hashed_codes = MFAService.hash_backup_codes(codes)
        assert len(hashed_codes) == 3

        # Verify and consume codes one by one
        remaining = list(hashed_codes)
        for i, code in enumerate(codes):
            is_valid, matched_hash = MFAService.verify_backup_code(code, remaining)
            assert is_valid, f"Code {i} should be valid"
            remaining = [h for h in remaining if h != matched_hash]

        # All codes exhausted
        assert len(remaining) == 0

        # No code works now
        for code in codes:
            is_valid, _ = MFAService.verify_backup_code(code, remaining)
            assert not is_valid

    def test_provisioning_uri_contains_required_elements(self):
        """Provisioning URI should contain all required elements for authenticator apps."""
        secret = MFAService.generate_totp_secret()
        email = "test@example.com"
        issuer = "TestApp"

        uri = MFAService.get_provisioning_uri(secret, email, issuer)

        # Check URI format
        assert uri.startswith("otpauth://totp/")
        assert "secret=" in uri
        assert "issuer=" in uri
        # Email should be URL encoded or present
        assert "test" in uri and "example.com" in uri

    def test_qr_code_generation_valid_png(self):
        """QR code generation should produce valid PNG data."""
        import base64

        secret = MFAService.generate_totp_secret()
        uri = MFAService.get_provisioning_uri(secret, "test@example.com")

        qr_base64 = MFAService.generate_qr_code_base64(uri)

        # Should be valid base64
        decoded = base64.b64decode(qr_base64)

        # Should be a valid PNG (magic bytes)
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"


class TestMFATokenCreation:
    """Tests for MFA token creation."""

    def test_create_mfa_token_structure(self):
        """MFA token should have correct structure."""
        from app.api.endpoints.auth import _create_mfa_token

        user_uuid = str(uuid4())
        user_role = "admin"

        token = _create_mfa_token(user_uuid, user_role)

        # Decode and verify structure
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])

        assert payload["sub"] == user_uuid
        assert payload["role"] == user_role
        assert payload["type"] == "mfa"
        assert "jti" in payload
        assert "iat" in payload
        assert "exp" in payload

    def test_create_mfa_token_expiration(self):
        """MFA token should expire after configured time."""
        from app.api.endpoints.auth import _create_mfa_token

        user_uuid = str(uuid4())

        token = _create_mfa_token(user_uuid, "user")

        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])

        # Check expiration is within expected range
        now = datetime.now(UTC).timestamp()
        expected_exp = now + (settings.MFA_TOKEN_EXPIRE_MINUTES * 60)

        # Allow 5 second tolerance
        assert abs(payload["exp"] - expected_exp) < 5

    def test_create_mfa_token_unique_jti(self):
        """Each MFA token should have a unique JTI."""
        from app.api.endpoints.auth import _create_mfa_token

        user_uuid = str(uuid4())

        token1 = _create_mfa_token(user_uuid, "user")
        token2 = _create_mfa_token(user_uuid, "user")

        payload1 = jwt.decode(token1, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        payload2 = jwt.decode(token2, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])

        assert payload1["jti"] != payload2["jti"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
