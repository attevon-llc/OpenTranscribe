"""An encryption outage must not read as "no secret stored".

``encrypt_api_key`` / ``decrypt_api_key`` / ``encrypt_value`` / ``decrypt_value`` used to catch
``Exception`` and return ``None``. With ``ENCRYPTION_KEY`` unset, that made a broken deployment
look like an empty credential: callers saved ``NULL`` for a user's SMTP password, or reported
"no API key". Only bad ciphertext (wrong key, tampering, corrupt value) may still yield ``None``,
and it is now logged with its traceback.
"""

from __future__ import annotations

import logging

import pytest
from redis.exceptions import RedisError

import app.utils.encryption as enc
import app.utils.task_secret_handoff as handoff
from app.core.config import settings

LOG = "app.utils.encryption"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", enc.generate_encryption_key())


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "")


def _api_token(plaintext: str) -> str:
    token = enc.encrypt_api_key(plaintext)
    assert token is not None
    return token


def _value_token(plaintext: str, aad: bytes | None = None) -> str:
    token = enc.encrypt_value(plaintext, aad=aad)
    assert token is not None
    return token


def _has_traceback(caplog, logger_name: str) -> bool:
    return any(r.name == logger_name and r.exc_info for r in caplog.records)


def test_encrypt_api_key_raises_when_key_unconfigured(no_key):
    with pytest.raises(enc.EncryptionKeyConfigError):
        enc.encrypt_api_key("sk-secret")


def test_encrypt_value_raises_when_key_unconfigured(no_key):
    with pytest.raises(enc.EncryptionKeyConfigError):
        enc.encrypt_value("sk-secret")


def test_empty_input_is_still_none_not_an_error(no_key):
    assert enc.encrypt_api_key("  ") is None
    assert enc.encrypt_value("") is None
    assert enc.decrypt_api_key("") is None
    assert enc.decrypt_value("  ") is None


def test_decrypt_raises_for_v3_when_key_unconfigured(key, monkeypatch):
    token = _api_token("sk-secret")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "")
    with pytest.raises(enc.EncryptionKeyConfigError):
        enc.decrypt_api_key(token)
    with pytest.raises(enc.EncryptionKeyConfigError):
        enc.decrypt_value(token)


def test_decrypt_raises_for_legacy_when_key_unconfigured(no_key):
    with pytest.raises(enc.EncryptionKeyConfigError):
        enc.decrypt_api_key("Z0FBQUFBQmxsZWdhY3k=")


def test_wrong_key_is_none_and_logged_with_traceback(key, monkeypatch, caplog):
    token = _api_token("sk-secret")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", enc.generate_encryption_key())
    with caplog.at_level(logging.ERROR, logger=LOG):
        assert enc.decrypt_api_key(token) is None
    assert _has_traceback(caplog, LOG)
    assert "sk-secret" not in caplog.text


def test_wrong_key_with_auto_upgrade_returns_pair_of_none(key, monkeypatch):
    token = _api_token("sk-secret")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", enc.generate_encryption_key())
    assert enc.decrypt_api_key(token, auto_upgrade=True) == (None, None)


def test_decrypt_value_wrong_aad_is_none_and_logged(key, caplog):
    token = _value_token("payload", aad=b"ctx-a")
    with caplog.at_level(logging.ERROR, logger=LOG):
        assert enc.decrypt_value(token, aad=b"ctx-b") is None
    assert _has_traceback(caplog, LOG)


def test_malformed_ciphertext_is_none_and_logged(key, caplog):
    with caplog.at_level(logging.ERROR, logger=LOG):
        assert enc.decrypt_value("v3:not-enough-parts") is None
    assert _has_traceback(caplog, LOG)


def test_unexpected_error_in_decrypt_propagates(key, monkeypatch):
    token = _api_token("sk-secret")

    def _bug(*_a, **_kw):
        raise RuntimeError("programming error")

    monkeypatch.setattr(enc, "_decrypt_v3", _bug)
    with pytest.raises(RuntimeError):
        enc.decrypt_api_key(token)
    with pytest.raises(RuntimeError):
        enc.decrypt_value(token)


def test_encryption_self_test_reports_false_with_traceback(no_key, caplog):
    with caplog.at_level(logging.ERROR, logger=LOG):
        assert enc.test_encryption() is False
    assert _has_traceback(caplog, LOG)


def test_encryption_self_test_passes_when_configured(key):
    assert enc.test_encryption() is True


def test_non_base64_key_still_derives_a_legacy_fernet_key(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "not base64 !!! but a usable string")
    assert len(enc._get_fernet_key()) == 44


def test_stash_task_secret_reports_unconfigured_key_as_unavailable(no_key):
    with pytest.raises(handoff.TaskSecretUnavailableError):
        handoff.stash_task_secret({"password": "pw"}, context="ctx")


def test_discard_logs_redis_failure_with_traceback(monkeypatch, caplog):
    class _Down:
        def delete(self, _key):
            raise RedisError("down")

    monkeypatch.setattr(handoff, "get_request_redis", lambda: _Down())
    with caplog.at_level(logging.WARNING, logger=handoff.__name__):
        handoff.discard_task_secret("ref")
    assert _has_traceback(caplog, handoff.__name__)


def test_discard_does_not_swallow_a_programming_error(monkeypatch):
    class _Buggy:
        def delete(self, _key):
            raise TypeError("bug")

    monkeypatch.setattr(handoff, "get_request_redis", lambda: _Buggy())
    with pytest.raises(TypeError):
        handoff.discard_task_secret("ref")
