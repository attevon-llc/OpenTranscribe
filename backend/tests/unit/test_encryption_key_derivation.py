"""Which KDF a stored secret pays for, and how often.

Every chat turn decrypts the LLM API key (``LLMService.credentials_from_row``). A v3
ciphertext derives its key with PBKDF2-SHA256 at 600k iterations (~350 ms of CPU), and that
used to run on every turn. New ciphertext (v4) derives with HKDF-SHA256, the ``cryptography``
docs' KDF for key material, and a v3 key is derived once per process and cached.

These tests count KDF calls; none of them measures time.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet

import app.utils.encryption as enc
from app.core.config import settings

GOLDEN_KEY = "golden-vector-test-key-not-a-secret"
# Written by the v3 encoder before v4 existed (salt bytes 0..15, nonce bytes 100..111), with
# DEFAULT_AAD. If this stops decrypting, every v3 row in every deployment is orphaned.
GOLDEN_V3 = (
    "v3:AAECAwQFBgcICQoLDA0ODw==:ZGVmZ2hpamtsbW5v:"
    "0hGzf_3NnBk2zf96WFi3BigTZ0V-to86XuQxogwY6BAQ9mcIW2w="
)
GOLDEN_V3_PLAINTEXT = "sk-golden-v3-plaintext"
# Same salt and nonce under v4. Pins HKDF_INFO_V4 and the salt handling: a change to either
# would orphan every v4 row.
GOLDEN_V4 = (
    "v4:AAECAwQFBgcICQoLDA0ODw==:ZGVmZ2hpamtsbW5v:"
    "b1KLA3HDMAeo9vd3jgvSGN1kfYiqYt89BnlWBcd8s9RMx7qmOkU="
)
GOLDEN_V4_PLAINTEXT = "sk-golden-v4-plaintext"

CHAT_TURNS = 5


@pytest.fixture(autouse=True)
def _cold_v3_key_cache():
    enc._v3_key.cache_clear()
    yield
    enc._v3_key.cache_clear()


@pytest.fixture
def golden_key(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", GOLDEN_KEY)


@pytest.fixture
def pbkdf2_calls(monkeypatch):
    """Count PBKDF2 derives while still running the real one."""
    calls: list[bytes] = []
    real = enc._derive_key_v3

    def _counting(password: bytes, salt: bytes) -> bytes:
        calls.append(salt)
        return real(password, salt)

    monkeypatch.setattr(enc, "_derive_key_v3", _counting)
    return calls


@pytest.fixture
def pbkdf2_forbidden(monkeypatch):
    """Make any PBKDF2 use fail, however it is reached."""

    class _NoPBKDF2:
        def __init__(self, *_a, **_kw):
            raise AssertionError("PBKDF2 must not run on this path")

    monkeypatch.setattr(enc, "PBKDF2HMAC", _NoPBKDF2)


@pytest.fixture
def hkdf_calls(monkeypatch):
    calls: list[bytes] = []
    real = enc._derive_key_v4

    def _counting(master_key: bytes, salt: bytes) -> bytes:
        calls.append(salt)
        return real(master_key, salt)

    monkeypatch.setattr(enc, "_derive_key_v4", _counting)
    return calls


def _legacy_fernet(plaintext: str) -> str:
    token = Fernet(enc._get_fernet_key()).encrypt(plaintext.encode())
    return base64.urlsafe_b64encode(token).decode()


def _chat_turn_api_key(stored: str) -> str | None:
    from app.models.user_llm_settings import UserLLMSettings
    from app.services.llm_service import LLMService

    row = UserLLMSettings(api_key=stored, provider="openai")
    api_key, _bedrock = LLMService.credentials_from_row(row)
    return api_key


# --- every version still decrypts --------------------------------------------------------


def test_v3_golden_ciphertext_still_decrypts(golden_key):
    assert enc.decrypt_api_key(GOLDEN_V3) == GOLDEN_V3_PLAINTEXT
    assert enc.decrypt_value(GOLDEN_V3) == GOLDEN_V3_PLAINTEXT


def test_v4_golden_ciphertext_decrypts_without_pbkdf2(golden_key, pbkdf2_forbidden):
    assert enc.decrypt_api_key(GOLDEN_V4) == GOLDEN_V4_PLAINTEXT


def test_legacy_fernet_round_trip(golden_key):
    assert enc.decrypt_api_key(_legacy_fernet("sk-legacy")) == "sk-legacy"
    assert enc.decrypt_value(_legacy_fernet("legacy-value")) == "legacy-value"


@pytest.mark.parametrize("plaintext", ["sk-simple", "unicode-中文", "a" * 1000])
def test_current_version_round_trip(golden_key, plaintext):
    token = enc.encrypt_api_key(plaintext)
    assert token is not None
    assert token.startswith(enc.ENCRYPTION_V4_PREFIX)
    assert enc.decrypt_api_key(token) == plaintext


def test_current_version_value_round_trip_is_bound_to_its_aad(golden_key):
    token = enc.encrypt_value("payload", aad=b"ctx-a")
    assert token is not None
    assert token.startswith(enc.ENCRYPTION_V4_PREFIX)
    assert enc.decrypt_value(token, aad=b"ctx-a") == "payload"
    with pytest.raises(enc.SecretDecryptionError):
        enc.decrypt_value(token, aad=b"ctx-b")


def test_v4_envelope_has_the_v3_length(golden_key):
    """Columns sized for a v3 ciphertext hold the v4 one: same salt, nonce and tag sizes."""
    assert len(GOLDEN_V4) == len(GOLDEN_V3)


def test_a_v3_ciphertext_relabelled_v4_does_not_decrypt(golden_key):
    """The two versions derive different keys from the same master key and salt."""
    relabelled = enc.ENCRYPTION_V4_PREFIX + GOLDEN_V3[len(enc.ENCRYPTION_V3_PREFIX) :]
    with pytest.raises(enc.SecretDecryptionError):
        enc.decrypt_api_key(relabelled)


# --- the read path pays PBKDF2 at most once per secret -----------------------------------


def test_chat_turns_on_a_v3_row_derive_pbkdf2_once(golden_key, pbkdf2_calls):
    keys = [_chat_turn_api_key(GOLDEN_V3) for _ in range(CHAT_TURNS)]

    assert keys == [GOLDEN_V3_PLAINTEXT] * CHAT_TURNS
    assert len(pbkdf2_calls) == 1


def test_chat_turns_on_a_v4_row_never_derive_pbkdf2(golden_key, pbkdf2_forbidden, hkdf_calls):
    token = enc.encrypt_api_key("sk-v4-row")
    assert token is not None
    hkdf_calls.clear()

    keys = [_chat_turn_api_key(token) for _ in range(CHAT_TURNS)]

    assert keys == ["sk-v4-row"] * CHAT_TURNS
    assert len(hkdf_calls) == CHAT_TURNS


def test_v3_key_cache_is_keyed_by_salt(golden_key, pbkdf2_calls):
    other_salt = bytes(range(16, 32))
    enc._v3_key(GOLDEN_KEY.encode(), bytes(range(16)))
    enc._v3_key(GOLDEN_KEY.encode(), other_salt)
    enc._v3_key(GOLDEN_KEY.encode(), other_salt)

    assert pbkdf2_calls == [bytes(range(16)), other_salt]


def test_v3_key_cache_never_serves_a_key_derived_from_another_master_key(
    golden_key, monkeypatch, pbkdf2_calls
):
    assert enc.decrypt_api_key(GOLDEN_V3) == GOLDEN_V3_PLAINTEXT

    monkeypatch.setattr(settings, "ENCRYPTION_KEY", enc.generate_encryption_key())
    with pytest.raises(enc.SecretDecryptionError):
        enc.decrypt_api_key(GOLDEN_V3)
    assert len(pbkdf2_calls) == 2


def test_v3_key_cache_is_bounded():
    assert enc._v3_key.cache_info().maxsize == enc.V3_KEY_CACHE_SIZE
    assert enc.V3_KEY_CACHE_SIZE > 0


# --- the write path uses HKDF ------------------------------------------------------------


def test_a_new_write_uses_hkdf_and_no_pbkdf2(golden_key, pbkdf2_forbidden, hkdf_calls):
    api_token = enc.encrypt_api_key("sk-new")
    value_token = enc.encrypt_value("value-new", aad=b"ctx")

    assert api_token is not None and api_token.startswith(enc.ENCRYPTION_V4_PREFIX)
    assert value_token is not None and value_token.startswith(enc.ENCRYPTION_V4_PREFIX)
    assert len(hkdf_calls) == 2


def test_the_self_test_uses_no_pbkdf2(golden_key, pbkdf2_forbidden):
    assert enc.test_encryption() is True


def test_auto_upgrade_rewrites_v3_and_legacy_to_v4(golden_key):
    for stored, plaintext in (
        (GOLDEN_V3, GOLDEN_V3_PLAINTEXT),
        (_legacy_fernet("sk-legacy"), "sk-legacy"),
    ):
        decrypted, upgraded = enc.decrypt_api_key(stored, auto_upgrade=True)
        assert decrypted == plaintext
        assert upgraded is not None and upgraded.startswith(enc.ENCRYPTION_V4_PREFIX)
        assert enc.decrypt_api_key(upgraded) == plaintext


def test_auto_upgrade_leaves_a_v4_ciphertext_alone(golden_key):
    assert enc.decrypt_api_key(GOLDEN_V4, auto_upgrade=True) == (GOLDEN_V4_PLAINTEXT, None)


# --- FIPS 140-3 mode ---------------------------------------------------------------------


def test_fips_140_3_mode_encrypts_v4_and_still_reads_v3(golden_key, monkeypatch):
    monkeypatch.setattr(settings, "FIPS_MODE", True)
    monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
    assert settings.fips_140_3_active

    token = enc.encrypt_api_key("sk-fips")
    assert token is not None and token.startswith(enc.ENCRYPTION_V4_PREFIX)
    assert enc.decrypt_api_key(token) == "sk-fips"
    assert enc.decrypt_api_key(GOLDEN_V3) == GOLDEN_V3_PLAINTEXT
    assert enc.test_encryption() is True
