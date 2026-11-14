"""
Encryption utilities for secure storage of sensitive data like API keys.

Every new ciphertext is version 4:
- AES-256-GCM authenticated encryption, 96-bit random nonce, associated data (AAD)
- Per-secret key derived with HKDF-SHA256 (RFC 5869) from ``ENCRYPTION_KEY`` and a random
  128-bit salt. HKDF is the ``cryptography`` docs' KDF for deriving keys from key material
  and the extract-then-expand KDF of NIST SP 800-56C Rev 2, so it is FIPS-approved.
- Envelope ``v4:salt:nonce:ciphertext``.

Older versions still decrypt; nothing rewrites a stored row on read:
- v3 (``v3:salt:nonce:ciphertext``) derived its key with PBKDF2-SHA256 at 600k iterations.
  PBKDF2 is a password KDF (deliberately slow, ~350 ms of CPU here), and ``ENCRYPTION_KEY``
  is not a password. The v3 key is cached per (master key, salt) in a bounded LRU, so a
  process pays the derive once per stored secret instead of on every read (every chat
  turn decrypts the LLM key). A v3 row becomes v4 the next time its owner saves it.
- No prefix: legacy Fernet (AES-128-CBC), auto-detected.

⚠️ ``ENCRYPTION_KEY`` must be high-entropy key material (``generate_encryption_key()`` or
``openssl rand -hex 32``, which the installer writes). HKDF does not stretch a weak
password; the FIPS 140-3 boot check (``FIPS_VALIDATE_ENTROPY``) rejects a low-entropy one.

⚠️ ``settings.ENCRYPTION_ALGORITHM_V3`` is NOT read here, and must not be. The envelopes
record no algorithm field, so decrypt has to use exactly the algorithm encrypt used.
Dispatching on a mutable setting would make every ciphertext written before an operator
changed it undecryptable. The setting is instead validated at boot against
``core.config.IMPLEMENTED_ENCRYPTION_ALGORITHMS``
(``app.core.startup_validation._validate_production_secrets``), which refuses to start a FIPS 140-3
deployment configured for an algorithm this module does not implement.
"""

import base64
import functools
import logging
import os
from typing import Literal
from typing import overload

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import settings

logger = logging.getLogger(__name__)


# Stable machine-readable code for the 409 an undecryptable stored secret produces.
STORED_VALUE_UNREADABLE_CODE = "secret_undecryptable"


class EncryptionError(RuntimeError):
    """Base for failures of the encryption layer, distinct from an absent secret.

    Deliberately NOT a ``ValueError``: callers that narrow bad-input handling to
    ``ValueError`` must not swallow these into "no secret stored".
    """


class SecretDecryptionError(EncryptionError):
    """A stored secret exists but cannot be decrypted (wrong key or AAD, corrupt or tampered).

    Never carries the ciphertext or plaintext. Absence (an empty/None input) is not this
    error: ``decrypt_*`` returns ``None`` for that.
    """


class EncryptionKeyConfigError(EncryptionError):
    """``ENCRYPTION_KEY`` is missing or unusable.

    Deliberately NOT a ``ValueError``: callers narrow bad-ciphertext handling to
    ``ValueError``/``InvalidTag`` and return ``None``, which would turn a broken deployment
    into "no key stored". A misconfigured key must reach the operator as an error.
    """


ENCRYPTION_V3_PREFIX = "v3:"  # PBKDF2-SHA256 key; decrypt only
ENCRYPTION_V4_PREFIX = "v4:"  # HKDF-SHA256 key
# The version every new ciphertext is written in.
ENCRYPTION_CURRENT_PREFIX = ENCRYPTION_V4_PREFIX
# Deliberately a module constant, NOT settings.PBKDF2_ITERATIONS_V3 (C8), even though they
# share a name and a default today. The v3 envelope (v3:salt:nonce:ciphertext) records no
# iteration count, so decrypt has to re-derive the key with exactly the count encrypt used.
# Reading the mutable setting here would silently orphan every ciphertext already written
# under the old count the moment an operator raised it in .env. settings.PBKDF2_ITERATIONS_V3
# governs password hashing (core/security.py) only.
PBKDF2_ITERATIONS_V3 = 600000
SALT_SIZE = 16  # 128-bit salt, both versions (keeps the v4 envelope the v3 length)
NONCE_SIZE = 12  # 96-bit nonce (recommended for GCM)
KEY_SIZE = 32  # 256-bit key
# HKDF "info": binds the derived key to this use (RFC 5869 section 3.2). Changing it orphans
# every v4 ciphertext, exactly like changing the iteration count would orphan v3.
HKDF_INFO_V4 = b"opentranscribe secret encryption v4 AES-256-GCM"
# One entry per distinct stored v3 secret a process reads (32-byte key plus its salt and the
# master key reference). Bounded so a process that reads many secrets cannot grow without
# limit; a miss only costs one PBKDF2 derive.
V3_KEY_CACHE_SIZE = 1024

# Default AAD for API key encryption
DEFAULT_AAD = b"opentranscribe-api-key-v3"


def _get_master_key_material() -> bytes:
    """
    Get the master key material from settings.

    Returns:
        bytes: Raw key material for key derivation

    Raises:
        EncryptionKeyConfigError: If encryption key is not configured
    """
    key_string = settings.ENCRYPTION_KEY
    if not key_string:
        logger.error("ENCRYPTION_KEY is not configured")
        raise EncryptionKeyConfigError("ENCRYPTION_KEY not configured")
    return key_string.encode("utf-8")


def _derive_key_v3(password: bytes, salt: bytes) -> bytes:
    """
    Derive the 256-bit key of a v3 ciphertext with PBKDF2-SHA256 (600k iterations).

    Only decrypt reaches this, through :func:`_v3_key`. Never call it per read directly.

    Args:
        password: Master key material
        salt: The salt stored in the v3 envelope

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


@functools.lru_cache(maxsize=V3_KEY_CACHE_SIZE)
def _v3_key(master_key: bytes, salt: bytes) -> bytes:
    """The v3 key for ``salt``, derived once per process.

    Keyed on the master key as well as the salt, so a changed ``ENCRYPTION_KEY`` can never
    decrypt with a key derived from the old one. The iteration count is the
    ``PBKDF2_ITERATIONS_V3`` constant, fixed for every v3 envelope, so it needs no slot in the
    key. The cache holds derived keys, never plaintext, and they are no more sensitive than
    ``ENCRYPTION_KEY`` itself, which this process already holds. ``lru_cache`` is thread-safe;
    two threads missing at once may both derive, which is correct, only slower.
    """
    return _derive_key_v3(master_key, salt)


def _derive_key_v4(master_key: bytes, salt: bytes) -> bytes:
    """
    Derive the 256-bit key of a v4 ciphertext with HKDF-SHA256.

    Args:
        master_key: Master key material (``ENCRYPTION_KEY``)
        salt: The random salt stored in the v4 envelope

    Returns:
        32-byte derived key suitable for AES-256-GCM
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        info=HKDF_INFO_V4,
    ).derive(master_key)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _encrypt_aead(plaintext: bytes, aad: bytes = DEFAULT_AAD) -> str:
    """
    Encrypt data with AES-256-GCM under a fresh HKDF-derived key (the current version).

    Args:
        plaintext: Data to encrypt
        aad: Associated authenticated data (authenticated but not encrypted)

    Returns:
        Encrypted string: v4:base64(salt):base64(nonce):base64(ciphertext)
    """
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    derived_key = _derive_key_v4(_get_master_key_material(), salt)
    ciphertext = AESGCM(derived_key).encrypt(nonce, plaintext, aad)
    return f"{ENCRYPTION_CURRENT_PREFIX}{_b64(salt)}:{_b64(nonce)}:{_b64(ciphertext)}"


def _aead_prefix(encrypted_data: str) -> str | None:
    """The AES-GCM envelope version prefix of ``encrypted_data``, or None (legacy Fernet)."""
    for prefix in (ENCRYPTION_V4_PREFIX, ENCRYPTION_V3_PREFIX):
        if encrypted_data.startswith(prefix):
            return prefix
    return None


def _decrypt_aead(encrypted_data: str, aad: bytes = DEFAULT_AAD) -> bytes:
    """
    Decrypt a v3 or v4 AES-256-GCM envelope.

    Args:
        encrypted_data: Encrypted string with a v3: or v4: prefix
        aad: Associated authenticated data (must match encryption)

    Returns:
        Decrypted plaintext bytes

    Raises:
        ValueError: If data format is invalid
        InvalidTag: If authentication fails (wrong key or AAD, tampered data)
    """
    prefix = _aead_prefix(encrypted_data)
    if prefix is None:
        raise ValueError("Invalid encrypted data format: no v3/v4 prefix")

    parts = encrypted_data[len(prefix) :].split(":")
    if len(parts) != 3:
        raise ValueError("Invalid encrypted data format: expected salt:nonce:ciphertext")
    salt, nonce, ciphertext = (base64.urlsafe_b64decode(part) for part in parts)

    master_key = _get_master_key_material()
    if prefix == ENCRYPTION_V3_PREFIX:
        derived_key = _v3_key(master_key, salt)
    else:
        derived_key = _derive_key_v4(master_key, salt)
    return AESGCM(derived_key).decrypt(nonce, ciphertext, aad)


def _get_fernet_key() -> bytes:
    """
    Get or derive Fernet key for legacy decryption.

    Returns:
        bytes: Fernet-compatible encryption key

    Raises:
        EncryptionKeyConfigError: If encryption key is not configured
    """
    key_string = settings.ENCRYPTION_KEY
    if not key_string:
        logger.error("ENCRYPTION_KEY is not configured")
        raise EncryptionKeyConfigError("ENCRYPTION_KEY not configured")

    # If it's already a valid Fernet key, use it
    try:
        key = base64.urlsafe_b64decode(key_string)
    except ValueError:  # binascii.Error is a ValueError: the key is not base64
        # Derive from string for legacy compatibility
        key_material = key_string.encode("utf-8")
        # Pad or truncate to 32 bytes
        if len(key_material) < 32:
            key_material = key_material.ljust(32, b"\0")
        else:
            key_material = key_material[:32]

        return base64.urlsafe_b64encode(key_material)
    if len(key) == 32:
        # Valid Fernet key (32 bytes decoded)
        return base64.urlsafe_b64encode(key)
    # Try using it directly
    return key_string.encode("utf-8")


def _decrypt_fernet_legacy(encrypted_data: str) -> str:
    """
    Decrypt legacy Fernet-encrypted data.

    Args:
        encrypted_data: Base64-encoded Fernet ciphertext

    Returns:
        Decrypted plaintext string

    Raises:
        SecretDecryptionError: wrong key, or corrupt/tampered data
    """
    try:
        key = _get_fernet_key()
        fernet = Fernet(key)

        # Decode from base64
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode("ascii"))

        # Decrypt
        decrypted_data = fernet.decrypt(encrypted_bytes)
        return decrypted_data.decode("utf-8")

    except (InvalidToken, ValueError) as e:  # ValueError: malformed base64 / non-UTF-8
        raise SecretDecryptionError("stored secret cannot be decrypted") from e


def encrypt_api_key(api_key: str) -> str | None:
    """
    Encrypt an API key using AES-256-GCM (FIPS 140-3 compliant).

    Args:
        api_key: Plain text API key to encrypt

    Returns:
        Encrypted API key in the current (v4) format, or None if ``api_key`` is empty

    Raises:
        EncryptionKeyConfigError: If ENCRYPTION_KEY is not configured. Encryption failing
            must not look like "no secret": a caller storing the returned None would
            silently drop the user's credential.
    """
    if not api_key or not api_key.strip():
        return None

    return _encrypt_aead(api_key.encode("utf-8"))


def _decryption_failed() -> SecretDecryptionError:
    """Log once, without the ciphertext/plaintext/exception text, and build the error."""
    logger.error("Stored secret could not be decrypted (wrong key, corrupt or tampered)")
    return SecretDecryptionError("stored secret cannot be decrypted")


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
    - v4: / v3: prefix -> AES-256-GCM decryption (HKDF / cached PBKDF2 key)
    - No prefix -> Legacy Fernet decryption

    Args:
        encrypted_api_key: Encrypted API key from database
        auto_upgrade: If True, returns (decrypted, upgraded_encrypted) tuple
                     for data in an older version that should be re-encrypted

    Returns:
        If auto_upgrade=False: Decrypted API key as plain text, or None if the stored value
        is empty (absence)
        If auto_upgrade=True: Tuple of (decrypted_key, current-version ciphertext, or None
        when the stored value is already current)

    Raises:
        SecretDecryptionError: wrong key, corrupt or tampered data (never None).
        EncryptionKeyConfigError: If ENCRYPTION_KEY is not configured (an outage, not a
            missing value).
    """
    if not encrypted_api_key or not encrypted_api_key.strip():
        if auto_upgrade:
            return None, None
        return None

    try:
        if _aead_prefix(encrypted_api_key) is not None:
            decrypted = _decrypt_aead(encrypted_api_key).decode("utf-8")
        else:
            decrypted = _decrypt_fernet_legacy(encrypted_api_key)
    except (InvalidTag, ValueError) as e:
        raise _decryption_failed() from e
    if not auto_upgrade:
        return decrypted
    if encrypted_api_key.startswith(ENCRYPTION_CURRENT_PREFIX):
        return decrypted, None
    return decrypted, encrypt_api_key(decrypted)


def generate_encryption_key() -> str:
    """
    Generate a new secure encryption key.

    ``ENCRYPTION_KEY`` is key material, not a password: v4 derives each secret's key from it
    with HKDF, which does not stretch a guessable value. This function generates a 256-bit
    random value encoded as base64.

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

        encrypted = encrypt_api_key(test_data)
        if not encrypted:
            logger.error("Encryption returned None")
            return False

        if not encrypted.startswith(ENCRYPTION_CURRENT_PREFIX):
            logger.error("Encrypted data missing the %s prefix", ENCRYPTION_CURRENT_PREFIX)
            return False

        decrypted = decrypt_api_key(encrypted)
        if decrypted != test_data:
            logger.error("Decryption mismatch")
            return False

        logger.info("Encryption self-test passed")
        return True

    except (EncryptionError, InvalidTag, ValueError) as e:
        logger.error(f"Encryption test failed: {e}", exc_info=True)
        return False


def encrypt_value(value: str, aad: bytes | None = None) -> str | None:
    """Encrypt a sensitive value with AES-256-GCM, bound to optional associated data.

    The counterpart of ``decrypt_value``: the same ``aad`` must be supplied to decrypt,
    so a ciphertext cannot be replayed into a different context.

    Returns:
        Encrypted value in the current (v4) format, or None if the value is empty.

    Raises:
        EncryptionKeyConfigError: If ENCRYPTION_KEY is not configured.
    """
    if not value or not value.strip():
        return None
    return _encrypt_aead(value.encode("utf-8"), DEFAULT_AAD if aad is None else aad)


def decrypt_value(encrypted_value: str, aad: bytes | None = None) -> str | None:
    """
    Decrypt a stored sensitive value.

    Args:
        encrypted_value: Encrypted value from database
        aad: Optional associated authenticated data (must match encryption)

    Returns:
        Decrypted value as plain text, or None if the stored value is empty (absence)

    Raises:
        SecretDecryptionError: wrong key or AAD, corrupt or tampered data.
        EncryptionKeyConfigError: If ENCRYPTION_KEY is not configured.
    """
    if not encrypted_value or not encrypted_value.strip():
        return None

    try:
        if _aead_prefix(encrypted_value) is not None:
            effective_aad = aad if aad is not None else DEFAULT_AAD
            return _decrypt_aead(encrypted_value, effective_aad).decode("utf-8")
        return _decrypt_fernet_legacy(encrypted_value)
    except (InvalidTag, ValueError) as e:
        raise _decryption_failed() from e
