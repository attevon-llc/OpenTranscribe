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

import ast
import hashlib
from pathlib import Path

import pytest

# Runs by DEFAULT. This module was gated behind RUN_FIPS_TESTS with the reason
# "FIPS 140-3 upgrade in development" — but every test in it passes, and did so on the first run once the gate
# was lifted. The gate was stale: it kept 39 security tests out of every local run and
# out of CI, visible only as `s` in the progress dots, while reading as a deliberate
# decision someone had made. That is how `test_super_admin_can_export_audit_logs` came to
# assert `status_code in [200, 400]` — 400 being exactly 'could not export' — without
# anyone noticing (issue #431).
#
# The pre-merge gate still runs these; the difference is they now also run by default,
# so a regression surfaces on the commit that causes it rather than at merge time.
from app.core.config import settings
from tests.jwt_compat import jwt

#: Published explicitly rather than read from the ambient environment, so each case
#: exercises a real branch and gives the same verdict whichever mode the suite was
#: launched in — the rule ``tests/unit/test_jwt_algorithm_downgrade.py`` established.
FIPS_MODE_MATRIX = [
    pytest.param(False, "140-2", id="non-fips-140-2"),
    pytest.param(False, "140-3", id="non-fips-140-3"),
    pytest.param(True, "140-2", id="fips-140-2"),
    pytest.param(True, "140-3", id="fips-140-3"),
]

#: HS512 wants a 512-bit key; ``config.py`` warns when ``JWT_SECRET_KEY`` is shorter.
HS512_SECRET = "fips-suite-hs512-secret-padded-to-sixty-four-bytes-exactly-01234"

SUBJECT = "019ec90a-1b2c-7def-8000-00000000fa17"


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


class TestAccessTokenAlgorithmInvariant:
    """Every access-token ISSUER must sign with the algorithm the request path VERIFIES.

    ``api/endpoints/auth/dependencies.py``'s ``get_current_user`` (:577) and
    ``get_optional_current_user`` (:783) both decode with
    ``algorithms=[settings.JWT_ALGORITHM]`` and nothing else. An issuer that selects a
    different algorithm — for any reason, FIPS included — mints tokens no authenticated
    request can verify. That is the invariant, and it is why the FIPS-aware
    ``_get_jwt_algorithm()`` was deleted from ``core/security.py`` rather than wired
    into the login path.

    This class replaces ``TestFIPS140_3JWT::test_hs512_jwt_creation``, which asserted
    HS512 on ``core.security.create_access_token`` — a function **no login path
    imports**. Every production login (local, PKI, OIDC, SAML, proxy, MFA enrolment)
    goes through ``auth.direct_auth.create_access_token``, which has never
    FIPS-branched, so the old test could not fail whatever the real path did.

    HS256 is not a crypto defect: HMAC (FIPS 198-1) over SHA-256 (FIPS 180-4) is an
    approved algorithm at 128-bit security strength (SP 800-57 Pt.1 R5), well above the
    112-bit floor of SP 800-131A Rev.2.
    """

    @staticmethod
    def _issue_from_every_issuer() -> dict[str, str]:
        """Mint an access token from each issuer that exists in the codebase."""
        from app.auth.direct_auth import create_access_token as production_issuer
        from app.core.security import create_access_token as core_issuer

        return {
            # THE production issuer — imported by every login endpoint.
            "app.auth.direct_auth.create_access_token": production_issuer(
                {"sub": SUBJECT, "role": "user"}
            ),
            # Second implementation, reached only from tests. Held to the same rule so
            # it cannot become a template for re-introducing the divergence.
            "app.core.security.create_access_token": core_issuer(subject=SUBJECT),
        }

    @pytest.mark.parametrize(("fips_mode", "fips_version"), FIPS_MODE_MATRIX)
    def test_issuers_sign_with_the_algorithm_the_request_path_accepts(
        self, monkeypatch, fips_mode, fips_version
    ):
        monkeypatch.setattr(settings, "FIPS_MODE", fips_mode)
        monkeypatch.setattr(settings, "FIPS_VERSION", fips_version)

        tokens = self._issue_from_every_issuer()
        assert tokens, "no issuers under test — this test is not looking at anything"

        for name, token in tokens.items():
            algorithm = jwt.get_unverified_header(token)["alg"]
            assert algorithm == settings.JWT_ALGORITHM, (
                f"{name} signed with {algorithm} while the request path decodes with "
                f"[{settings.JWT_ALGORITHM}] (dependencies.py:577,783) — every "
                f"authenticated request with this token would 401"
            )
            # And prove it, rather than only comparing strings: decoding with exactly
            # the verifier's algorithm list must succeed.
            jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])

    def test_hs512_is_reachable_by_setting_jwt_algorithm(self, monkeypatch):
        """The documented way to actually run HS512 — one setting, both issuers.

        ``JWT_ALGORITHM`` is what issuers *and* verifiers read, so moving it moves the
        whole plane together. ``JWT_ALGORITHM_V3`` does not: nothing issues from it.
        """
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", HS512_SECRET)
        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS512")

        tokens = self._issue_from_every_issuer()
        assert tokens, "no issuers under test"

        for name, token in tokens.items():
            assert jwt.get_unverified_header(token)["alg"] == "HS512", name
            jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS512"])

    def test_jwt_algorithm_v3_is_a_verifier_setting_only(self, monkeypatch):
        """Setting ``JWT_ALGORITHM_V3`` alone must change no issued token.

        It is read by ``core.security.verify_token``'s dual-accept list and by
        ``auth.token_service``; treating it as an issuance knob is exactly the
        confusion that produced the dead code path.
        """
        monkeypatch.setattr(settings, "FIPS_MODE", True)
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
        monkeypatch.setattr(settings, "JWT_ALGORITHM_V3", "HS512")

        tokens = self._issue_from_every_issuer()
        assert tokens, "no issuers under test"

        for name, token in tokens.items():
            assert jwt.get_unverified_header(token)["alg"] == settings.JWT_ALGORITHM, name


class TestFIPS140_3JWT:
    """Test FIPS 140-3 JWT compliance."""

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

    def test_jwt_alg_version_claim_tracks_the_algorithm_actually_used(self, monkeypatch):
        """``alg_version`` is derived from the signing algorithm, not from FIPS mode.

        It used to be asserted against ``FIPS_MODE``, which was only ever right by
        accident: the claim is computed from the algorithm, and the algorithm never
        depended on FIPS mode on any path a login reached.
        """
        from app.core.security import create_access_token

        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS256")
        payload = jwt.decode(
            create_access_token(subject=SUBJECT),
            settings.JWT_SECRET_KEY,
            algorithms=["HS256"],
        )
        assert payload["alg_version"] == "v2"

        monkeypatch.setattr(settings, "JWT_SECRET_KEY", HS512_SECRET)
        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS512")
        payload = jwt.decode(
            create_access_token(subject=SUBJECT),
            settings.JWT_SECRET_KEY,
            algorithms=["HS512"],
        )
        assert payload["alg_version"] == "v3"


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

    def test_backup_codes_need_regeneration_fires_only_under_fips(self, monkeypatch):
        """The detector must be gated on ``FIPS_MODE``, not on ``FIPS_VERSION`` alone.

        ``FIPS_VERSION`` defaults to ``"140-3"`` on **every** deployment, so the old
        ``FIPS_VERSION != "140-3"`` gate reported "regenerate" for bcrypt hashes that
        are the correct, current format on an ordinary non-FIPS install. The old form
        of this test asserted that behaviour, so it locked the bug in.
        """
        from app.auth.mfa import backup_codes_need_regeneration

        bcrypt_hashes = ["$2b$12$somehashvalue"]
        pbkdf2_hashes = ["$pbkdf2-sha256$600000$somehash"]

        # Non-FIPS deployment carrying the DEFAULT FIPS_VERSION: nothing to regenerate.
        monkeypatch.setattr(settings, "FIPS_MODE", False)
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
        assert not backup_codes_need_regeneration(bcrypt_hashes), (
            "bcrypt backup codes are the correct format on a non-FIPS deployment; "
            "flagging them would tell every ordinary user to regenerate"
        )

        # FIPS 140-3 actually on: bcrypt is legacy, PBKDF2 is current.
        monkeypatch.setattr(settings, "FIPS_MODE", True)
        assert backup_codes_need_regeneration(bcrypt_hashes)
        assert not backup_codes_need_regeneration(pbkdf2_hashes)

        # FIPS on but pinned to 140-2: PBKDF2-SHA256 is not required, so quiet.
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-2")
        assert not backup_codes_need_regeneration(bcrypt_hashes)


class TestFIPSBackupCodeCompatibility:
    """Turning FIPS on must not lock users out of their own backup codes.

    ``_create_backup_code_context()``'s FIPS branch used to be
    ``schemes=["pbkdf2_sha256"]`` with bcrypt absent — unlike
    ``core.security._create_password_context``, which lists it. Every backup code in the
    database was bcrypt, so on the first login after FIPS was enabled
    ``backup_code_context.verify()`` raised ``UnknownHashError``,
    ``verify_backup_code`` swallowed it at DEBUG, and the user was permanently locked
    out of the credential that exists for when they have lost their phone — with no
    trace above DEBUG.
    """

    LEGACY_CODE = "ABCD-1234"

    @staticmethod
    def _legacy_bcrypt_hash(code: str) -> str:
        """Hash *code* the way a pre-FIPS deployment did: plain bcrypt.

        Work factor 4 (bcrypt's minimum), not the production 12 — the SCHEME is what is
        under test, and cost 12 costs ~370 ms per call for nothing. The rounds are
        embedded in the hash, so verification is unaffected.
        """
        from passlib.hash import bcrypt

        normalized = code.replace("-", "").replace(" ", "").upper()
        return bcrypt.using(rounds=4).hash(normalized)  # type: ignore[no-any-return]

    @staticmethod
    def _enable_fips_140_3(monkeypatch) -> None:
        """Flip the deployment into FIPS 140-3 and rebuild the module-global context."""
        from app.auth import mfa

        monkeypatch.setattr(settings, "FIPS_MODE", True)
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
        monkeypatch.setattr(mfa, "backup_code_context", mfa._create_backup_code_context())

    def test_legacy_bcrypt_backup_code_still_verifies_after_enabling_fips(self, monkeypatch):
        from app.auth.mfa import MFAService

        stored = [self._legacy_bcrypt_hash(self.LEGACY_CODE)]
        assert stored[0].startswith("$2"), "fixture did not produce a bcrypt hash"

        self._enable_fips_140_3(monkeypatch)

        is_valid, matched_hash = MFAService.verify_backup_code(self.LEGACY_CODE, stored)
        assert is_valid, (
            "a backup code issued before FIPS was enabled no longer verifies — the "
            "user is permanently locked out; bcrypt must stay registered (verify-only) "
            "in the FIPS branch of _create_backup_code_context()"
        )
        assert matched_hash == stored[0]

    def test_wrong_code_is_still_rejected_against_a_legacy_hash_under_fips(self, monkeypatch):
        """The control: accepting legacy hashes must not accept the wrong code."""
        from app.auth.mfa import MFAService

        stored = [self._legacy_bcrypt_hash(self.LEGACY_CODE)]
        self._enable_fips_140_3(monkeypatch)

        is_valid, matched_hash = MFAService.verify_backup_code("ZZZZ-9999", stored)
        assert not is_valid
        assert matched_hash is None

    def test_new_backup_codes_under_fips_are_pbkdf2_never_bcrypt(self, monkeypatch):
        """Verify-only means verify-only: bcrypt must never be the DEFAULT under FIPS."""
        from app.auth.mfa import MFAService

        self._enable_fips_140_3(monkeypatch)

        hashed = MFAService.hash_backup_code(self.LEGACY_CODE)
        assert hashed.startswith("$pbkdf2-sha256$"), (
            f"FIPS mode hashed a new backup code as {hashed[:20]!r}; bcrypt is "
            f"registered for verification only"
        )

    def test_mixed_bcrypt_and_pbkdf2_code_lists_both_verify_under_fips(self, monkeypatch):
        """The real shape of a migrating deployment: old and new hashes side by side."""
        from app.auth.mfa import MFAService

        legacy_hash = self._legacy_bcrypt_hash(self.LEGACY_CODE)
        self._enable_fips_140_3(monkeypatch)

        new_code = "EFGH-5678"
        stored = [legacy_hash, MFAService.hash_backup_code(new_code)]

        legacy_valid, legacy_match = MFAService.verify_backup_code(self.LEGACY_CODE, stored)
        assert legacy_valid
        assert legacy_match == stored[0]

        new_valid, new_match = MFAService.verify_backup_code(new_code, stored)
        assert new_valid
        assert new_match == stored[1]


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


class TestFIPSSafeNonSecurityHashes:
    """Non-security MD5 must declare ``usedforsecurity=False`` or it RAISES under FIPS.

    On a host whose OpenSSL enforces FIPS, ``hashlib.md5()`` raises unless the caller
    clears ``usedforsecurity``. Two call sites carried only ``# noqa: S324 # nosec
    B324`` — which silences the *linters* and changes nothing at runtime. The reviewer
    who wrote them judged "not security" correctly and stopped one step short: that
    judgement is exactly what ``usedforsecurity=False`` encodes to the runtime.

    Consequence of leaving them: every thumbnail fetch 500s
    (``api/endpoints/files/streaming.py``) and every transcription task raises at model
    load (``transcription/config.py``). An availability defect, not a security one.
    """

    def test_every_md5_call_in_the_app_declares_usedforsecurity_false(self):
        """Repo-wide invariant, checked by AST over ``backend/app``."""
        app_dir = Path(__file__).resolve().parents[1] / "app"
        sources = sorted(app_dir.rglob("*.py"))
        assert len(sources) > 100, (
            f"expected to scan the whole backend app tree, found {len(sources)} files "
            f"under {app_dir} — the scanner is pointed at the wrong place"
        )

        md5_call_sites = []
        offenders = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "md5"):
                    continue
                site = f"{path.relative_to(app_dir.parent)}:{node.lineno}"
                md5_call_sites.append(site)
                cleared = any(
                    keyword.arg == "usedforsecurity"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                    for keyword in node.keywords
                )
                if not cleared:
                    offenders.append(site)

        # Guard the guard: a detector that matches nothing reports zero findings, which
        # is indistinguishable from a clean tree. If md5 is genuinely gone from
        # backend/app, delete this test rather than letting it pass vacuously.
        assert md5_call_sites, (
            "found no hashlib.md5 call sites at all in backend/app — the detector is "
            "not looking at anything"
        )
        assert not offenders, (
            "hashlib.md5() without usedforsecurity=False raises on a FIPS-enforcing "
            f"OpenSSL: {offenders}"
        )

    def test_config_hash_survives_a_fips_enforcing_hashlib(self, monkeypatch):
        """Behavioural half, with the FIPS refusal SIMULATED.

        No FIPS-enforcing OpenSSL is available in this environment, so the raise is
        modelled rather than observed: CPython's ``_hashlib`` surfaces the provider's
        refusal as ``ValueError('[digital envelope routines] unsupported')`` when MD5
        is requested with ``usedforsecurity`` still set. The stub reproduces that
        contract; what the test proves is that the production call clears the flag.
        """
        from app.transcription import config as transcription_config

        real_md5 = hashlib.md5

        class FipsEnforcingHashlib:
            @staticmethod
            def md5(data=b"", *, usedforsecurity=True):
                if usedforsecurity:
                    raise ValueError("[digital envelope routines] unsupported")
                return real_md5(data, usedforsecurity=False)

        monkeypatch.setattr(transcription_config, "hashlib", FipsEnforcingHashlib)

        digest = transcription_config.TranscriptionConfig().config_hash()

        assert len(digest) == 12
        assert digest == transcription_config.TranscriptionConfig().config_hash()


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
