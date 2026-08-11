"""
FIPS 140-3 Cryptographic Compliance Tests.

Tests verify:
- PBKDF2-SHA256 with 600,000 iterations for password hashing
- AES-256-GCM encryption with proper key derivation
- HS512 JWT signing algorithm
- SHA-512 token hashing
- Backward compatibility with FIPS 140-2 algorithms

NOTE: These tests are for the FIPS 140-3 upgrade planned in the compliance plan.
Currently using FIPS 140-2 compatible algorithms.
Set RUN_FIPS_TESTS=true to run these tests.
"""

import hashlib
import os

import pytest

from tests.jwt_compat import jwt

# Skip all tests - FIPS 140-3 upgrade in development
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_FIPS_TESTS", "false").lower() != "true",
    reason="FIPS 140-3 upgrade in development (set RUN_FIPS_TESTS=true to run)",
)

from app.core.config import settings


class TestFIPS140_3PasswordHashing:
    """Test FIPS 140-3 password hashing compliance."""

    def test_pbkdf2_sha256_iterations(self):
        """Verify PBKDF2 uses 600,000 iterations in FIPS 140-3 mode."""
        from app.core.security import pwd_context

        # Hash a test password
        test_password = "TestPassword123!"
        hashed = pwd_context.hash(test_password)

        # PBKDF2-SHA256 only applies when FIPS mode is actually enabled
        # (the app gates on FIPS_MODE *and* FIPS_VERSION — see core/security.py)
        if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3":
            assert "$pbkdf2-sha256$" in hashed
            # Extract iteration count from hash format: $pbkdf2-sha256$<rounds>$...
            parts = hashed.split("$")
            if len(parts) >= 3:
                rounds = int(parts[2])
                assert rounds == settings.PBKDF2_ITERATIONS_V3
        else:
            # Non-FIPS default is bcrypt_sha256
            assert hashed.startswith(("$bcrypt-sha256$", "$2"))

    def test_password_verification(self):
        """Test password verification works correctly."""
        from app.core.security import get_password_hash
        from app.core.security import verify_password

        password = "SecurePassword123!"
        hashed = get_password_hash(password)

        assert verify_password(password, hashed)
        assert not verify_password("WrongPassword", hashed)

    def test_password_hash_format_fips_mode(self):
        """Verify password hash format in FIPS mode uses PBKDF2-SHA256."""
        from app.core.security import get_password_hash

        password = "TestPassword123!"
        hashed = get_password_hash(password)

        if settings.FIPS_MODE:
            # FIPS mode should use PBKDF2-SHA256
            assert "$pbkdf2-sha256$" in hashed
        else:
            # Non-FIPS mode uses bcrypt_sha256 (or legacy bcrypt / pbkdf2)
            assert hashed.startswith(("$bcrypt-sha256$", "$2", "$pbkdf2"))

    def test_password_upgrade_detection(self):
        """Test that legacy passwords are flagged for upgrade."""
        from app.core.security import pwd_context

        # Legacy bcrypt is listed in `deprecated` on BOTH context branches
        # (app/core/security.py), so it is flagged for upgrade regardless of FIPS mode —
        # that is the auto-upgrade-on-verify policy, not an accident. Asserted
        # unconditionally: the old form only asserted under FIPS 140-3, so in every other
        # mode this test verified nothing (issue #431).
        bcrypt_hash = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4.S.V/C.aX3RqzjO"

        assert pwd_context.needs_update(bcrypt_hash), (
            "plain bcrypt must be flagged for upgrade; check `deprecated=` in "
            "_create_password_context"
        )

    def test_pbkdf2_iterations_v3_config(self):
        """Verify PBKDF2_ITERATIONS_V3 is set to 600,000."""
        assert settings.PBKDF2_ITERATIONS_V3 == 600000

    def test_verify_and_update_password(self):
        """Test password verification with hash upgrade support."""
        from app.core.security import get_password_hash
        from app.core.security import verify_and_update_password

        password = "TestPassword123!"
        hashed = get_password_hash(password)

        is_valid, new_hash = verify_and_update_password(password, hashed)
        assert is_valid

        # If using current algorithm, no upgrade needed
        if new_hash is None:
            # Hash is already current
            pass
        else:
            # New hash was generated
            assert len(new_hash) > 0

    def test_needs_rehash_for_fips_v3(self):
        """Test needs_rehash_for_fips_v3 function."""
        from app.core.security import needs_rehash_for_fips_v3

        # `needs_rehash_for_fips_v3` asks a question about the HASH, not about the current
        # mode — "would this satisfy FIPS 140-3?" — so bcrypt answers True either way. The
        # old form only asserted under FIPS_MODE, leaving the non-FIPS run vacuous even
        # though the contract is mode-independent (issue #431).
        bcrypt_hash = "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4.S.V/C.aX3RqzjO"

        assert needs_rehash_for_fips_v3(bcrypt_hash), (
            "bcrypt is not PBKDF2-SHA256, so it can never satisfy FIPS 140-3"
        )

        # A real PBKDF2 hash with insufficient iterations should also need rehash
        from passlib.hash import pbkdf2_sha256

        low_iteration_hash = pbkdf2_sha256.using(rounds=10000).hash("password123")
        if settings.FIPS_VERSION == "140-3":
            # This should need upgrade due to low iteration count (< 600,000)
            assert needs_rehash_for_fips_v3(low_iteration_hash)


class TestFIPS140_3Encryption:
    """Test FIPS 140-3 encryption compliance."""

    def test_aes_256_gcm_encryption(self):
        """Verify AES-256-GCM encryption works correctly."""
        from app.utils.encryption import decrypt_api_key
        from app.utils.encryption import encrypt_api_key

        test_data = "sensitive-api-key-12345"

        encrypted = encrypt_api_key(test_data)
        assert encrypted is not None
        assert encrypted != test_data

        decrypted = decrypt_api_key(encrypted)
        assert decrypted == test_data

    def test_encryption_version_prefix(self):
        """Verify FIPS 140-3 encrypted data has v3: prefix."""
        from app.utils.encryption import ENCRYPTION_V3_PREFIX
        from app.utils.encryption import encrypt_api_key

        encrypted = encrypt_api_key("test-data")
        assert encrypted is not None
        assert encrypted.startswith(ENCRYPTION_V3_PREFIX)

    def test_encryption_format(self):
        """Verify v3 encryption format: v3:salt:nonce:ciphertext."""
        from app.utils.encryption import ENCRYPTION_V3_PREFIX
        from app.utils.encryption import encrypt_api_key

        encrypted = encrypt_api_key("test-data")
        assert encrypted is not None

        # Remove prefix and check format
        data = encrypted[len(ENCRYPTION_V3_PREFIX) :]
        parts = data.split(":")
        assert len(parts) == 3  # salt:nonce:ciphertext

    def test_encryption_key_derivation(self):
        """Verify proper key derivation from ENCRYPTION_KEY."""
        from app.utils.encryption import KEY_SIZE
        from app.utils.encryption import _derive_key_v3

        key1 = _derive_key_v3(b"password", b"salt1234567890ab")
        key2 = _derive_key_v3(b"password", b"salt1234567890ab")
        key3 = _derive_key_v3(b"password", b"differentsalt12")

        # Same password + salt = same key
        assert key1 == key2
        # Different salt = different key
        assert key1 != key3
        # Key should be 32 bytes (256 bits)
        assert len(key1) == KEY_SIZE

    def test_encryption_randomness(self):
        """Verify each encryption produces unique ciphertext (due to random nonce)."""
        from app.utils.encryption import encrypt_api_key

        test_data = "same-data"

        encrypted1 = encrypt_api_key(test_data)
        encrypted2 = encrypt_api_key(test_data)

        # Same plaintext should produce different ciphertext due to random nonce
        assert encrypted1 != encrypted2

    def test_empty_string_encryption(self):
        """Test encryption handles empty strings correctly."""
        from app.utils.encryption import encrypt_api_key

        result = encrypt_api_key("")
        assert result is None

        result = encrypt_api_key("   ")
        assert result is None

    def test_encryption_decryption_roundtrip(self):
        """Test encryption/decryption roundtrip with various data."""
        from app.utils.encryption import decrypt_api_key
        from app.utils.encryption import encrypt_api_key

        test_cases = [
            "simple-key",
            "key-with-special-chars!@#$%^&*()",
            "unicode-key-\u4e2d\u6587-\u65e5\u672c\u8a9e",
            "a" * 1000,  # Long key
        ]

        for test_data in test_cases:
            encrypted = encrypt_api_key(test_data)
            assert encrypted is not None
            decrypted = decrypt_api_key(encrypted)
            assert decrypted == test_data, f"Failed for: {test_data[:50]}..."

    def test_auto_upgrade_parameter(self):
        """Test decrypt_api_key auto_upgrade parameter."""
        from app.utils.encryption import decrypt_api_key
        from app.utils.encryption import encrypt_api_key

        test_data = "test-api-key"
        encrypted = encrypt_api_key(test_data)
        assert encrypted is not None

        # With auto_upgrade=True, should return tuple
        decrypted, upgraded = decrypt_api_key(encrypted, auto_upgrade=True)
        assert decrypted == test_data
        # Already v3, no upgrade needed
        assert upgraded is None


class TestFIPS140_3JWT:
    """Test FIPS 140-3 JWT compliance."""

    def test_hs512_jwt_creation(self):
        """Verify JWTs are created with HS512 algorithm in FIPS 140-3 mode."""
        from app.core.security import create_access_token

        token = create_access_token(subject="test-user-uuid")

        # Decode header to check algorithm
        header = jwt.get_unverified_header(token)

        if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3":
            assert header["alg"] == "HS512"
        else:
            assert header["alg"] == "HS256"

    def test_jwt_algorithm_config(self):
        """Verify JWT algorithm configuration settings."""
        assert settings.JWT_ALGORITHM == "HS256"
        assert settings.JWT_ALGORITHM_V3 == "HS512"

    def test_jwt_contains_required_claims(self):
        """Verify JWT contains all required claims."""
        from app.core.security import create_access_token

        token = create_access_token(subject="test-uuid", additional_claims={"role": "admin"})

        # Decode with both algorithms for compatibility
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256", "HS512"])

        assert "sub" in payload
        assert "exp" in payload
        assert "iat" in payload
        assert "jti" in payload  # JWT ID for revocation
        assert "alg_version" in payload
        assert payload["role"] == "admin"

    def test_jwt_verify_token(self):
        """Test JWT token verification."""
        from app.core.security import create_access_token
        from app.core.security import verify_token

        token = create_access_token(subject="test-uuid")
        payload = verify_token(token)

        assert payload["sub"] == "test-uuid"

    def test_jwt_alg_version_claim(self):
        """Verify JWT includes algorithm version claim."""
        from app.core.security import create_access_token

        token = create_access_token(subject="test-uuid")
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256", "HS512"])

        if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3":
            assert payload["alg_version"] == "v3"
        else:
            assert payload["alg_version"] == "v2"


class TestFIPS140_3TokenHashing:
    """Test FIPS 140-3 token hashing compliance."""

    def test_sha512_token_hashing(self):
        """Verify refresh tokens are hashed with SHA-512."""
        from app.auth.token_service import TokenService

        token_service = TokenService()
        token = "test-refresh-token-12345"
        hashed = token_service._hash_token(token)

        # SHA-512 produces 128 hex characters
        assert len(hashed) == 128

        # Verify it's actually SHA-512
        expected = hashlib.sha512(token.encode()).hexdigest()
        assert hashed == expected

    def test_token_hash_consistency(self):
        """Verify same token produces same hash."""
        from app.auth.token_service import TokenService

        token_service = TokenService()
        token = "consistent-token-test"
        hash1 = token_service._hash_token(token)
        hash2 = token_service._hash_token(token)

        assert hash1 == hash2

    def test_token_hash_uniqueness(self):
        """Verify different tokens produce different hashes."""
        from app.auth.token_service import TokenService

        token_service = TokenService()
        hash1 = token_service._hash_token("token-1")
        hash2 = token_service._hash_token("token-2")

        assert hash1 != hash2


class TestFIPS140_3MFA:
    """Test FIPS 140-3 MFA compliance."""

    def test_backup_code_hashing(self):
        """Verify backup codes use PBKDF2-SHA256 in FIPS 140-3 mode."""
        from app.auth.mfa import MFAService

        codes = ["ABCD-1234", "EFGH-5678"]
        hashed = MFAService.hash_backup_codes(codes)

        if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3":
            # PBKDF2-SHA256 hashes start with $pbkdf2-sha256$
            for h in hashed:
                assert "$pbkdf2-sha256$" in h
        else:
            # Non-FIPS mode uses bcrypt
            for h in hashed:
                assert "$2" in h

    def test_backup_code_verification(self):
        """Test backup code verification."""
        from app.auth.mfa import MFAService

        codes = MFAService.generate_backup_codes(5)
        hashed = MFAService.hash_backup_codes(codes)

        # Test valid code
        is_valid, matched_hash = MFAService.verify_backup_code(codes[0], hashed)
        assert is_valid
        assert matched_hash == hashed[0]

        # Test invalid code
        is_valid, matched_hash = MFAService.verify_backup_code("INVALID-CODE", hashed)
        assert not is_valid
        assert matched_hash is None

    def test_backup_code_generation(self):
        """Test backup code generation."""
        from app.auth.mfa import MFAService

        codes = MFAService.generate_backup_codes(10)

        assert len(codes) == 10
        for code in codes:
            # Format: XXXX-XXXX
            assert len(code) == 9
            assert code[4] == "-"
            # Check alphabet (no ambiguous chars)
            for char in code.replace("-", ""):
                assert char in MFAService.BACKUP_CODE_ALPHABET

    def test_totp_sha1_allowed(self):
        """Document that SHA-1 TOTP is FIPS-allowed for HMAC."""
        import hashlib

        from app.auth.mfa import get_totp_algorithm

        # Returns a hashlib constructor for pyotp; SHA-1 is the default for
        # authenticator-app compatibility (FIPS-approved for HMAC use).
        algo = get_totp_algorithm()
        assert algo in (hashlib.sha1, hashlib.sha256, hashlib.sha512)

    def test_totp_secret_generation(self):
        """Test TOTP secret generation."""
        from app.auth.mfa import MFAService

        secret = MFAService.generate_totp_secret()

        # Should be base32 encoded
        assert len(secret) == 32  # 20 bytes base32 encoded
        # Should only contain base32 characters
        base32_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567="
        for char in secret:
            assert char in base32_chars

    def test_totp_secret_encryption(self):
        """Test TOTP secret encryption and decryption."""
        from app.auth.mfa import MFAService

        secret = MFAService.generate_totp_secret()

        encrypted = MFAService.encrypt_totp_secret(secret)
        assert encrypted != secret
        assert encrypted.startswith("v3:")

        decrypted = MFAService.decrypt_totp_secret(encrypted)
        assert decrypted == secret

    def test_backup_codes_need_regeneration(self):
        """Test backup code regeneration detection for FIPS 140-3 upgrade."""
        from app.auth.mfa import backup_codes_need_regeneration

        # Bcrypt hashes start with $2
        bcrypt_hashes = ["$2b$12$somehashvalue"]

        if settings.FIPS_VERSION == "140-3":
            assert backup_codes_need_regeneration(bcrypt_hashes)
        else:
            assert not backup_codes_need_regeneration(bcrypt_hashes)

        # PBKDF2 hashes should not need regeneration
        pbkdf2_hashes = ["$pbkdf2-sha256$600000$somehash"]
        assert not backup_codes_need_regeneration(pbkdf2_hashes)


class TestFIPS140_3TokenService:
    """Test FIPS 140-3 token service compliance."""

    def test_verify_token_with_fallback(self):
        """Test JWT verification with algorithm fallback."""
        from app.auth.token_service import TokenService

        token_service = TokenService()

        # Create token with current algorithm
        token_data = {"sub": "test-user", "type": "refresh"}
        token = token_service.create_token(token_data)

        # Should be able to verify
        payload = token_service.verify_token_with_fallback(token)
        assert payload["sub"] == "test-user"

    def test_token_needs_upgrade(self):
        """Test token upgrade detection for FIPS 140-3 migration."""
        from app.auth.token_service import TokenService

        token_service = TokenService()

        # Create a legacy HS256 token
        legacy_token = jwt.encode(
            {"sub": "test", "exp": 9999999999}, settings.JWT_SECRET_KEY, algorithm="HS256"
        )

        # In FIPS 140-3 mode, this should need upgrade
        needs_upgrade = token_service.token_needs_upgrade(legacy_token)

        if settings.FIPS_VERSION == "140-3":
            assert needs_upgrade
        else:
            assert not needs_upgrade

    def test_create_token_algorithm(self):
        """Test that token creation uses correct algorithm."""
        from app.auth.token_service import TokenService

        token_service = TokenService()
        token = token_service.create_token({"sub": "test"})

        header = jwt.get_unverified_header(token)

        if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3":
            assert header["alg"] == "HS512"
        else:
            assert header["alg"] == "HS256"


class TestFIPS140_3Migration:
    """Test migration from FIPS 140-2 to 140-3."""

    def test_password_auto_upgrade(self):
        """Test automatic password hash upgrade on login."""
        from app.core.security import get_password_hash
        from app.core.security import verify_and_update_password

        password = "TestPassword123!"
        new_hash = get_password_hash(password)

        is_valid, upgraded_hash = verify_and_update_password(password, new_hash)
        assert is_valid
        # If already using current algorithm, no upgrade needed
        # upgraded_hash would be None or the new hash

    def test_fips_migration_mode_config(self):
        """Verify FIPS migration mode configuration."""
        assert settings.FIPS_MIGRATION_MODE in ["compatible", "strict"]

    def test_fips_validate_entropy_config(self):
        """Verify FIPS entropy validation configuration."""
        assert isinstance(settings.FIPS_VALIDATE_ENTROPY, bool)


class TestFIPS140_3Constants:
    """Test FIPS 140-3 configuration constants."""

    def test_encryption_constants(self):
        """Verify encryption constants are correct."""
        from app.utils.encryption import KEY_SIZE
        from app.utils.encryption import NONCE_SIZE
        from app.utils.encryption import PBKDF2_ITERATIONS_V3
        from app.utils.encryption import SALT_SIZE

        assert PBKDF2_ITERATIONS_V3 == 600000
        assert SALT_SIZE == 16  # 128-bit salt
        assert NONCE_SIZE == 12  # 96-bit nonce (GCM recommended)
        assert KEY_SIZE == 32  # 256-bit key

    def test_settings_fips_version(self):
        """Verify FIPS version configuration."""
        assert settings.FIPS_VERSION in ["140-2", "140-3"]

    def test_encryption_algorithm_v3(self):
        """Verify encryption algorithm configuration."""
        assert settings.ENCRYPTION_ALGORITHM_V3 == "AES-256-GCM"


# Run with: pytest tests/test_fips_140_3.py -v
