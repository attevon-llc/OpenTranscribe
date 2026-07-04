"""
Multi-Factor Authentication (MFA) service module.

Implements TOTP-based MFA following RFC 6238 (TOTP) and RFC 4226 (HOTP).
Compliant with FedRAMP IA-2 multi-factor authentication requirements.

Features:
- TOTP secret generation using cryptographically secure random bytes
- QR code provisioning URI generation for authenticator apps
- TOTP verification with configurable time window
- One-time use backup codes with secure hashing (bcrypt or PBKDF2-SHA256)
- TOTP secrets encrypted at rest using Fernet (AES-128-CBC) or AES-256-GCM

Security:
- TOTP secrets are encrypted before storage and decrypted on verification
- Backup codes are hashed with bcrypt (cost factor 12) or PBKDF2-SHA256 (600k iterations)

FIPS 140-3 Compliance Notes:
- Backup codes use PBKDF2-SHA256 with 600,000 iterations when FIPS_VERSION="140-3"
- TOTP uses SHA-1 by default per RFC 6238 - this is FIPS allowed for HMAC
  (NIST SP 800-131A Rev. 2 allows SHA-1 for HMAC-based applications like TOTP)
- SHA-256/SHA-512 TOTP available via TOTP_ALGORITHM setting for high-security
  environments with compatible authenticator apps
"""

import base64
import hashlib
import io
import logging
import secrets

import pyotp
import qrcode
from passlib.context import CryptContext

from app.core.config import settings
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)


def _create_backup_code_context() -> CryptContext:
    """
    Create password context for backup codes with FIPS 140-3 compliance.

    When FIPS_VERSION is "140-3", uses PBKDF2-SHA256 with 600k iterations
    per NIST SP 800-132 (2024) recommendations for high-security environments.

    For non-FIPS environments, bcrypt with cost factor 12 provides good
    protection against brute-force attacks on 8-character backup codes.

    Returns:
        CryptContext: Configured password hashing context
    """
    if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3":
        # Use PBKDF2-SHA256 with 600k iterations for FIPS 140-3.
        # FIPS_MODE is the master switch — FIPS_VERSION alone defaults to
        # "140-3" even on non-FIPS deployments (matches core/security.py).
        return CryptContext(
            schemes=["pbkdf2_sha256"],
            default="pbkdf2_sha256",
            pbkdf2_sha256__rounds=settings.PBKDF2_ITERATIONS_V3,
        )
    else:
        # Non-FIPS: bcrypt by default (cost factor 12 balances security and
        # performance). pbkdf2_sha256 stays verifiable so backup codes hashed
        # while the old FIPS_VERSION-only branch was active keep working.
        return CryptContext(
            schemes=["bcrypt", "pbkdf2_sha256"],
            default="bcrypt",
            bcrypt__rounds=12,
            pbkdf2_sha256__rounds=settings.PBKDF2_ITERATIONS_V3,
        )


# Initialize backup code context based on FIPS configuration
backup_code_context = _create_backup_code_context()


def get_totp_algorithm():
    """
    Get TOTP hash algorithm based on configuration.

    SHA-1 is the default per RFC 6238 and is FIPS-approved for HMAC-based
    applications (NIST SP 800-131A Rev. 2). SHA-256/SHA-512 are available
    for high-security environments with compatible authenticator apps.

    Note: Most authenticator apps (Google Authenticator, Microsoft Authenticator)
    only support SHA-1. Use SHA-256/SHA-512 only if you've verified compatibility.

    Returns:
        A hashlib hash constructor for pyotp (hashlib.sha1, sha256, or sha512)
    """
    algo_map = {
        "SHA1": hashlib.sha1,
        "SHA256": hashlib.sha256,
        "SHA512": hashlib.sha512,
    }
    return algo_map.get(settings.TOTP_ALGORITHM.upper(), hashlib.sha1)


def backup_codes_need_regeneration(hashed_codes: list[str]) -> bool:
    """
    Check if backup codes need regeneration for FIPS 140-3 upgrade.

    When migrating from non-FIPS to FIPS 140-3 mode, existing bcrypt-hashed
    backup codes should be regenerated with PBKDF2-SHA256. This function
    detects codes that need regeneration.

    Args:
        hashed_codes: List of hashed backup codes from the database

    Returns:
        bool: True if codes are hashed with bcrypt and we're in FIPS 140-3 mode
    """
    if settings.FIPS_VERSION != "140-3":
        return False

    if not hashed_codes:
        return False

    # Check if first code is bcrypt (starts with $2)
    # bcrypt hashes start with $2a$, $2b$, or $2y$
    return hashed_codes[0].startswith("$2")


class MFAService:
    """
    Service for handling TOTP-based Multi-Factor Authentication.

    Implements RFC 6238 TOTP with 30-second time step and configurable
    hash algorithm (SHA-1 default for Google Authenticator compatibility,
    SHA-256/SHA-512 for high-security environments).
    """

    # TOTP configuration
    TOTP_DIGITS = 6  # Standard 6-digit codes
    TOTP_INTERVAL = 30  # 30-second time step (RFC 6238 default)
    # TOTP_VALID_WINDOW is now configured via settings.TOTP_VALID_WINDOW

    # Backup code configuration
    BACKUP_CODE_LENGTH = 8  # 8 character backup codes
    BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # Exclude ambiguous chars

    @staticmethod
    def generate_totp_secret() -> str:
        """
        Generate a new TOTP secret using cryptographically secure random bytes.

        Returns a base32-encoded secret suitable for TOTP authentication.
        The secret is 160 bits (20 bytes) per RFC 4226 recommendation.

        Returns:
            str: Base32-encoded TOTP secret (plaintext, must be encrypted before storage)
        """
        # Generate 20 bytes (160 bits) of random data
        random_bytes = secrets.token_bytes(20)
        # Encode as base32 (standard for TOTP secrets)
        secret = base64.b32encode(random_bytes).decode("utf-8")
        logger.debug("Generated new TOTP secret")
        return secret

    @staticmethod
    def encrypt_totp_secret(secret: str) -> str:
        """
        Encrypt a TOTP secret for secure storage at rest.

        Uses Fernet symmetric encryption (AES-128-CBC) with the application's
        ENCRYPTION_KEY. This ensures TOTP secrets are not stored in plaintext.

        Args:
            secret: Base32-encoded TOTP secret (plaintext)

        Returns:
            str: Encrypted TOTP secret (base64-encoded)

        Raises:
            ValueError: If encryption fails
        """
        encrypted = encrypt_api_key(secret)
        if encrypted is None:
            logger.error("Failed to encrypt TOTP secret")
            raise ValueError("Failed to encrypt TOTP secret")
        logger.debug("TOTP secret encrypted for storage")
        return encrypted

    @staticmethod
    def decrypt_totp_secret(encrypted_secret: str) -> str:
        """
        Decrypt a TOTP secret from storage.

        Args:
            encrypted_secret: Encrypted TOTP secret from database

        Returns:
            str: Decrypted base32-encoded TOTP secret

        Raises:
            ValueError: If decryption fails
        """
        decrypted = decrypt_api_key(encrypted_secret)
        if decrypted is None:
            logger.error("Failed to decrypt TOTP secret")
            raise ValueError("Failed to decrypt TOTP secret")
        logger.debug("TOTP secret decrypted for verification")
        return decrypted

    @staticmethod
    def get_provisioning_uri(secret: str, email: str, issuer_name: str | None = None) -> str:
        """
        Generate a provisioning URI for authenticator apps.

        The URI follows the otpauth:// format specified in Google Authenticator's
        key URI format specification. Uses the configured TOTP algorithm from
        settings.TOTP_ALGORITHM.

        Args:
            secret: Base32-encoded TOTP secret
            email: User's email address (used as account name)
            issuer_name: Name of the issuer (defaults to settings.MFA_ISSUER_NAME)

        Returns:
            str: otpauth:// URI for QR code generation
        """
        issuer = issuer_name or settings.MFA_ISSUER_NAME
        algorithm = get_totp_algorithm()
        totp = pyotp.TOTP(
            secret,
            digits=MFAService.TOTP_DIGITS,
            interval=MFAService.TOTP_INTERVAL,
            digest=algorithm,
        )
        uri: str = totp.provisioning_uri(name=email, issuer_name=issuer)  # type: ignore[no-any-return]
        logger.debug(
            f"Generated provisioning URI for user: {email[:3]}*** (algorithm: {algorithm})"
        )
        return uri

    @staticmethod
    def generate_qr_code_base64(provisioning_uri: str) -> str:
        """
        Generate a QR code image as a base64-encoded PNG.

        Args:
            provisioning_uri: The otpauth:// URI to encode

        Returns:
            str: Base64-encoded PNG image data (without data URI prefix)
        """
        # Create QR code with high error correction
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        # Generate PNG image using PIL (installed via qrcode[pil])
        img = qr.make_image(fill_color="black", back_color="white")

        # Convert to base64
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        logger.debug("Generated QR code for MFA setup")
        return img_base64

    @staticmethod
    def verify_totp(secret: str, code: str) -> bool:
        """
        Verify a TOTP code against the secret.

        Allows for a configurable time window to account for clock drift
        between the server and the user's device. Uses the configured TOTP
        algorithm from settings.TOTP_ALGORITHM.

        Args:
            secret: Base32-encoded TOTP secret
            code: 6-digit TOTP code from user

        Returns:
            bool: True if the code is valid, False otherwise
        """
        if not code or not secret:
            return False

        # Clean the code (remove spaces/dashes if any)
        code = code.replace(" ", "").replace("-", "")

        # Validate code format
        if not code.isdigit() or len(code) != MFAService.TOTP_DIGITS:
            logger.warning("Invalid TOTP code format")
            return False

        try:
            algorithm = get_totp_algorithm()
            totp = pyotp.TOTP(
                secret,
                digits=MFAService.TOTP_DIGITS,
                interval=MFAService.TOTP_INTERVAL,
                digest=algorithm,
            )
            # verify() accepts valid_window parameter for clock drift tolerance
            is_valid: bool = totp.verify(code, valid_window=settings.TOTP_VALID_WINDOW)  # type: ignore[no-any-return]

            if is_valid:
                logger.debug("TOTP verification successful")
            else:
                logger.debug("TOTP verification failed - invalid code")

            return is_valid
        except Exception as e:
            logger.error(f"TOTP verification error: {str(e)}")
            return False

    @staticmethod
    def generate_backup_codes(count: int | None = None) -> list[str]:
        """
        Generate cryptographically secure one-time use backup codes.

        Backup codes are generated using the secrets module for FIPS-compliant
        entropy. Uses a restricted alphabet that excludes ambiguous characters
        (0, O, I, 1, L) for readability.

        Args:
            count: Number of backup codes to generate (defaults to settings.MFA_BACKUP_CODE_COUNT)

        Returns:
            list[str]: List of plaintext backup codes formatted as XXXX-XXXX
        """
        num_codes = count or settings.MFA_BACKUP_CODE_COUNT
        codes = []

        for _ in range(num_codes):
            # Generate random code using cryptographically secure random choice
            code = "".join(
                secrets.choice(MFAService.BACKUP_CODE_ALPHABET)
                for _ in range(MFAService.BACKUP_CODE_LENGTH)
            )
            # Format as XXXX-XXXX for readability
            formatted_code = f"{code[:4]}-{code[4:]}"
            codes.append(formatted_code)

        logger.debug(f"Generated {num_codes} backup codes")
        return codes

    @staticmethod
    def hash_backup_code(code: str) -> str:
        """
        Hash a backup code for secure storage.

        Uses PBKDF2-SHA256 with 600k iterations in FIPS 140-3 mode, or
        bcrypt with cost factor 12 in non-FIPS mode. Both provide strong
        resistance to brute-force attacks on 8-character backup codes.

        Args:
            code: Plaintext backup code

        Returns:
            str: Hash of the normalized backup code
        """
        # Normalize: remove formatting, uppercase
        normalized = code.replace("-", "").replace(" ", "").upper()
        # Hash with the configured algorithm (PBKDF2-SHA256 or bcrypt)
        return backup_code_context.hash(normalized)  # type: ignore[no-any-return]

    @staticmethod
    def verify_backup_code(code: str, hashed_codes: list[str]) -> tuple[bool, str | None]:
        """
        Verify a backup code against a list of hashed codes.

        Uses constant-time comparison via passlib to prevent timing attacks.
        Supports both PBKDF2-SHA256 (FIPS 140-3) and bcrypt hashes for
        backwards compatibility during migration.

        Args:
            code: Plaintext backup code from user
            hashed_codes: List of hashed backup codes (PBKDF2-SHA256 or bcrypt)

        Returns:
            tuple[bool, Optional[str]]: (is_valid, matched_hash)
                - is_valid: True if code matches one of the hashed codes
                - matched_hash: The hash that matched (for removal), or None if invalid
        """
        if not code or not hashed_codes:
            return False, None

        # Normalize the provided code
        normalized = code.replace("-", "").replace(" ", "").upper()

        # Check against each stored hash using passlib verify
        for stored_hash in hashed_codes:
            try:
                if backup_code_context.verify(normalized, stored_hash):
                    logger.info("Backup code verification successful")
                    return True, stored_hash
            except Exception as e:
                # Handle potential invalid hash format or algorithm mismatch
                # This can occur during migration from bcrypt to PBKDF2-SHA256
                logger.debug(f"Backup code hash verification error: {e}")
                continue

        logger.debug("Backup code verification failed - code not found")
        return False, None

    @staticmethod
    def hash_backup_codes(codes: list[str]) -> list[str]:
        """
        Hash a list of backup codes for secure storage.

        Uses PBKDF2-SHA256 in FIPS 140-3 mode, or bcrypt in non-FIPS mode.

        Args:
            codes: List of plaintext backup codes

        Returns:
            list[str]: List of hashed backup codes
        """
        return [MFAService.hash_backup_code(code) for code in codes]
