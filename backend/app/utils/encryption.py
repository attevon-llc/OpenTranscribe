"""
Encryption utilities for secure storage of sensitive data like API keys.

Version 3 (FIPS 140-3 compliant):
- Uses AES-256-GCM authenticated encryption
- Key derivation via PBKDF2-SHA256 with 600k iterations
- 96-bit random nonce per encryption
- Associated data (AAD) support for authenticated encryption

Backward compatibility:
- Legacy Fernet (AES-128-CBC) data is auto-detected and decrypted
- Re-encryption with AES-256-GCM on access is supported

⚠️ ``settings.ENCRYPTION_ALGORITHM_V3`` is NOT read here, and must not be. The v3 envelope
is ``v3:salt:nonce:ciphertext`` — it records no algorithm field — so decrypt has to use
exactly the algorithm encrypt used. Dispatching on a mutable setting would make every
ciphertext written before an operator changed it undecryptable. The setting is instead
validated at boot against ``core.config.IMPLEMENTED_ENCRYPTION_ALGORITHMS``
(``app.main._validate_production_secrets``), which refuses to start a FIPS 140-3
deployment configured for an algorithm this module does not implement.
"""

import base64
import logging
import os
from typing import Literal
from typing import overload

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import settings

logger = logging.getLogger(__name__)

# V3 encryption constants (FIPS 140-3 compliant)
ENCRYPTION_V3_PREFIX = "v3:"
# Deliberately a module constant, NOT settings.PBKDF2_ITERATIONS_V3 (C8), even though they
# share a name and a default today. The v3 envelope (v3:salt:nonce:ciphertext) records no
# iteration count, so decrypt has to re-derive the key with exactly the count encrypt used.
# Reading the mutable setting here would silently orphan every ciphertext already written
# under the old count the moment an operator raised it in .env. settings.PBKDF2_ITERATIONS_V3
# governs password hashing (core/security.py) only.
PBKDF2_ITERATIONS_V3 = 600000
SALT_SIZE = 16  # 128-bit salt
NONCE_SIZE = 12  # 96-bit nonce (recommended for GCM)
KEY_SIZE = 32  # 256-bit key

# Default AAD for API key encryption
DEFAULT_AAD = b"opentranscribe-api-key-v3"


def _get_master_key_material() -> bytes:
    """
    Get the master key material from settings.

    Returns:
        bytes: Raw key material for key derivation

    Raises:
        ValueError: If encryption key is not configured
    """
    try:
        key_string = settings.ENCRYPTION_KEY
        if not key_string:
            raise ValueError("ENCRYPTION_KEY not configured")
        return key_string.encode("utf-8")
    except Exception as e:
        logger.error(f"Failed to get master key material: {e}")
        raise ValueError("Invalid encryption key configuration") from e


def _derive_key_v3(password: bytes, salt: bytes) -> bytes:
    """
    Derive a 256-bit encryption key using PBKDF2-SHA256.

    Args:
        password: Master key material
        salt: Random salt for key derivation

    Returns:
        32-byte derived key suitable for AES-256-GCM
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=PBKDF2_ITERATIONS_V3,
        backend=default_backend(),
    )
    return kdf.derive(password)


def _encrypt_v3(plaintext: bytes, aad: bytes = DEFAULT_AAD) -> str:
    """
    Encrypt data using AES-256-GCM with PBKDF2 key derivation.

    Args:
        plaintext: Data to encrypt
        aad: Associated authenticated data (authenticated but not encrypted)

    Returns:
        Encrypted string in format: v3:base64(salt):base64(nonce):base64(ciphertext)
    """
    # Generate random salt and nonce
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)

    # Derive key from master key material
    master_key = _get_master_key_material()
    derived_key = _derive_key_v3(master_key, salt)

    # Encrypt with AES-256-GCM
    aesgcm = AESGCM(derived_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

    # Encode components as base64
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
    ciphertext_b64 = base64.urlsafe_b64encode(ciphertext).decode("ascii")

    # Return formatted string with version prefix
    return f"{ENCRYPTION_V3_PREFIX}{salt_b64}:{nonce_b64}:{ciphertext_b64}"


def _decrypt_v3(encrypted_data: str, aad: bytes = DEFAULT_AAD) -> bytes:
    """
    Decrypt data encrypted with AES-256-GCM.

    Args:
        encrypted_data: Encrypted string with v3: prefix
        aad: Associated authenticated data (must match encryption)

    Returns:
        Decrypted plaintext bytes

    Raises:
        ValueError: If data format is invalid
        Exception: If decryption fails (authentication failure, etc.)
    """
    # Remove v3: prefix and split components
    if not encrypted_data.startswith(ENCRYPTION_V3_PREFIX):
        raise ValueError("Invalid v3 encrypted data format")

    data = encrypted_data[len(ENCRYPTION_V3_PREFIX) :]
    parts = data.split(":")

    if len(parts) != 3:
        raise ValueError("Invalid v3 encrypted data format: expected salt:nonce:ciphertext")

    salt_b64, nonce_b64, ciphertext_b64 = parts

    # Decode base64 components
    salt = base64.urlsafe_b64decode(salt_b64)
    nonce = base64.urlsafe_b64decode(nonce_b64)
    ciphertext = base64.urlsafe_b64decode(ciphertext_b64)

    # Derive key from master key material
    master_key = _get_master_key_material()
    derived_key = _derive_key_v3(master_key, salt)

    # Decrypt with AES-256-GCM
    aesgcm = AESGCM(derived_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, aad)

    return plaintext


def _get_fernet_key() -> bytes:
    """
    Get or derive Fernet key for legacy decryption.

    Returns:
        bytes: Fernet-compatible encryption key

    Raises:
        ValueError: If encryption key is invalid
    """
    try:
        key_string = settings.ENCRYPTION_KEY

        # If it's already a valid Fernet key, use it
        try:
            key = base64.urlsafe_b64decode(key_string)
            if len(key) == 32:
                # Valid Fernet key (32 bytes decoded)
                return base64.urlsafe_b64encode(key)
            # Try using it directly
            return key_string.encode("utf-8")
        except Exception:
            # Derive from string for legacy compatibility
            key_material = key_string.encode("utf-8")
            # Pad or truncate to 32 bytes
            if len(key_material) < 32:
                key_material = key_material.ljust(32, b"\0")
            else:
                key_material = key_material[:32]

            return base64.urlsafe_b64encode(key_material)
    except Exception as e:
        logger.error(f"Failed to get Fernet key: {e}")
        raise ValueError("Invalid encryption key configuration") from e


def _decrypt_fernet_legacy(encrypted_data: str) -> str | None:
    """
    Decrypt legacy Fernet-encrypted data.

    Args:
        encrypted_data: Base64-encoded Fernet ciphertext

    Returns:
        Decrypted plaintext string, or None if decryption fails
    """
    try:
        key = _get_fernet_key()
        fernet = Fernet(key)

        # Decode from base64
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode("ascii"))

        # Decrypt
        decrypted_data = fernet.decrypt(encrypted_bytes)
        return decrypted_data.decode("utf-8")

    except InvalidToken:
        logger.debug("Fernet token invalid - may not be legacy format")
        return None
    except Exception as e:
        logger.error(f"Failed to decrypt legacy Fernet data: {e}")
        return None


def encrypt_api_key(api_key: str) -> str | None:
    """
    Encrypt an API key using AES-256-GCM (FIPS 140-3 compliant).

    Args:
        api_key: Plain text API key to encrypt

    Returns:
        Encrypted API key in v3 format, or None if encryption fails
    """
    if not api_key or not api_key.strip():
        return None

    try:
        return _encrypt_v3(api_key.encode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to encrypt API key: {e}")
        return None


@overload
def decrypt_api_key(encrypted_api_key: str, auto_upgrade: Literal[False] = False) -> str | None: ...


@overload
def decrypt_api_key(
    encrypted_api_key: str, auto_upgrade: Literal[True]
) -> tuple[str | None, str | None]: ...


def decrypt_api_key(
    encrypted_api_key: str, auto_upgrade: bool = False
) -> str | None | tuple[str | None, str | None]:
    """
    Decrypt an API key from storage.

    Automatically detects encryption version:
    - v3: prefix -> AES-256-GCM decryption
    - No prefix -> Legacy Fernet decryption

    Args:
        encrypted_api_key: Encrypted API key from database
        auto_upgrade: If True, returns (decrypted, upgraded_encrypted) tuple
                     for legacy data that should be re-encrypted

    Returns:
        If auto_upgrade=False: Decrypted API key as plain text, or None if decryption fails
        If auto_upgrade=True: Tuple of (decrypted_key, new_v3_encrypted_key or None)
    """
    if not encrypted_api_key or not encrypted_api_key.strip():
        if auto_upgrade:
            return None, None
        return None

    try:
        # Check for v3 encryption
        if encrypted_api_key.startswith(ENCRYPTION_V3_PREFIX):
            decrypted = _decrypt_v3(encrypted_api_key).decode("utf-8")
            if auto_upgrade:
                return decrypted, None  # Already v3, no upgrade needed
            return decrypted

        # Try legacy Fernet decryption
        legacy_decrypted = _decrypt_fernet_legacy(encrypted_api_key)

        if legacy_decrypted is None:
            if auto_upgrade:
                return None, None
            return None

        if auto_upgrade:
            # Re-encrypt with v3 for upgrade
            new_encrypted = encrypt_api_key(legacy_decrypted)
            return legacy_decrypted, new_encrypted

        return legacy_decrypted

    except Exception as e:
        logger.error(f"Failed to decrypt API key: {e}")
        if auto_upgrade:
            return None, None
        return None


def generate_encryption_key() -> str:
    """
    Generate a new secure encryption key.

    For v3 encryption, any high-entropy string can be used as the master key
    since PBKDF2 is used for key derivation. This function generates a
    256-bit random value encoded as base64.

    Returns:
        Base64-encoded 256-bit key suitable for ENCRYPTION_KEY environment variable
    """
    key = os.urandom(32)  # 256-bit random key
    return base64.urlsafe_b64encode(key).decode("ascii")


def test_encryption() -> bool:
    """
    Test encryption/decryption functionality.

    Returns:
        True if encryption is working correctly, False otherwise
    """
    try:
        test_data = "test_api_key_12345"

        # Test v3 encryption
        encrypted = encrypt_api_key(test_data)
        if not encrypted:
            logger.error("V3 encryption returned None")
            return False

        if not encrypted.startswith(ENCRYPTION_V3_PREFIX):
            logger.error("V3 encrypted data missing prefix")
            return False

        # Test v3 decryption
        decrypted = decrypt_api_key(encrypted)
        if decrypted != test_data:
            logger.error("V3 decryption mismatch")
            return False

        logger.info("V3 encryption test passed")
        return True

    except Exception as e:
        logger.error(f"Encryption test failed: {e}")
        return False


def decrypt_value(encrypted_value: str, aad: bytes | None = None) -> str | None:
    """
    Decrypt a stored sensitive value.

    Args:
        encrypted_value: Encrypted value from database
        aad: Optional associated authenticated data (must match encryption)

    Returns:
        Decrypted value as plain text, or None if decryption fails
    """
    if not encrypted_value or not encrypted_value.strip():
        return None

    try:
        # Check for v3 encryption with custom AAD
        if encrypted_value.startswith(ENCRYPTION_V3_PREFIX):
            effective_aad = aad if aad is not None else DEFAULT_AAD
            return _decrypt_v3(encrypted_value, effective_aad).decode("utf-8")

        # Legacy Fernet decryption (doesn't support custom AAD)
        return _decrypt_fernet_legacy(encrypted_value)

    except Exception as e:
        logger.error(f"Failed to decrypt value: {e}")
        return None
