"""A stored secret that will not decrypt is an error at every caller, never "no secret".

Each test stores a real ciphertext under one key, swaps ENCRYPTION_KEY, and asserts the
caller fails visibly instead of proceeding without the credential.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

import app.utils.encryption as enc
from app.core.config import settings
from app.models.auth_config import AuthConfig
from app.models.email_notification_config import EmailNotificationConfig
from app.models.user_asr_settings import UserASRSettings
from app.models.user_llm_settings import UserLLMSettings
from app.models.watch_source import WatchSource


@pytest.fixture
def stale_token(monkeypatch):
    """A ciphertext written under a key that is no longer configured."""
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", enc.generate_encryption_key())
    token = enc.encrypt_api_key("sk-original-secret")
    assert token is not None
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", enc.generate_encryption_key())
    return token


def test_totp_secret_decrypt_failure_is_a_value_error(stale_token):
    from app.auth.mfa import MFAService

    with pytest.raises(ValueError, match="Failed to decrypt TOTP secret"):
        MFAService.decrypt_totp_secret(stale_token)


def test_llm_row_with_undecryptable_key_raises_value_error(stale_token):
    from app.services.llm_factory import LLMFactoryMixin

    row = UserLLMSettings(provider="openai", api_key=stale_token)
    with pytest.raises(ValueError, match="could not be decrypted"):
        LLMFactoryMixin.credentials_from_row(row)


def test_asr_config_with_undecryptable_key_raises_instead_of_unauthenticated(stale_token):
    from app.services.asr.factory import ASRProviderFactory

    cfg = UserASRSettings(
        id=1, provider="deepgram", api_key=stale_token, access_key_id=None, model_name="m"
    )
    with pytest.raises(ValueError, match="cannot be decrypted"):
        ASRProviderFactory.create_from_db_config(cfg)


def test_smtp_password_failure_does_not_fall_back_to_unauthenticated(stale_token):
    from app.services import watch_email_service as svc

    cfg = EmailNotificationConfig(
        provider="smtp",
        smtp_host="mail.example.com",
        smtp_port=587,
        smtp_use_tls=True,
        smtp_username="user",
        encrypted_smtp_password=stale_token,
        exchange_server=None,
        exchange_username=None,
        exchange_domain=None,
        encrypted_exchange_password=None,
        from_address=None,
    )
    with pytest.raises(enc.SecretDecryptionError):
        svc._smtp_params(cfg)
    ok, message = svc.send_email(cfg, ["a@example.com"], "s", "<p>b</p>")
    assert ok is False
    assert "sk-original-secret" not in message


def test_watch_source_s3_secret_failure_raises(stale_token):
    from app.services.watch_sources.base import create_client

    source = WatchSource(source_type="s3", encrypted_s3_secret_key=stale_token)
    with pytest.raises(enc.SecretDecryptionError):
        create_client(source)


def test_watch_source_smb_password_failure_raises(stale_token):
    from app.services.watch_sources.base import create_client

    source = WatchSource(
        source_type="smb", encrypted_smb_password=stale_token, encrypted_s3_secret_key=None
    )
    with pytest.raises(enc.SecretDecryptionError):
        create_client(source)


def test_scheduled_job_secret_failure_raises_not_none(stale_token, monkeypatch):
    from app.services import scheduled_job_settings as sjs

    monkeypatch.setattr(sjs.sss, "get_setting", lambda _s, _k: stale_token)
    with pytest.raises(enc.SecretDecryptionError):
        sjs.read_encrypted_secret(MagicMock(spec=Session), "k")


def test_bedrock_blob_failure_raises_not_empty_connection(stale_token):
    from app.services.bedrock_settings import connection_from_encrypted

    with pytest.raises(enc.SecretDecryptionError):
        connection_from_encrypted(stale_token)


def test_task_secret_handoff_wrong_key_is_unavailable(stale_token, monkeypatch):
    import app.utils.task_secret_handoff as handoff

    class _Redis:
        def get(self, _key):
            return stale_token

    monkeypatch.setattr(handoff, "get_request_redis", lambda: _Redis())
    with pytest.raises(handoff.TaskSecretUnavailableError):
        handoff.fetch_task_secret("ref", context="ctx")


def test_auth_config_undecryptable_secret_reads_unset_and_keeps_ciphertext(stale_token):
    from app.services.auth_config_service import AuthConfigService

    row = AuthConfig(config_key="ldap.bind_password", config_value=stale_token, is_sensitive=True)

    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.first.return_value = row
    assert AuthConfigService.get_config(db, "ldap.bind_password") is None
    assert row.config_value == stale_token


def test_queue_depths_outage_is_unknown_not_zero(monkeypatch):
    import app.core.celery_metrics as cm
    from app.utils.stats_helpers import get_queue_depths

    def _down(**_kwargs):
        raise RedisError("down")

    monkeypatch.setattr(cm, "queue_snapshot", _down)
    assert get_queue_depths() is None


def test_secret_decryption_error_renders_a_409_with_stable_code_and_no_text():
    from app.main import app as real_app

    probe = FastAPI()
    for exc_type, handler in real_app.exception_handlers.items():
        if exc_type is enc.SecretDecryptionError:
            probe.add_exception_handler(exc_type, handler)

    @probe.get("/boom")
    def _boom():
        raise enc.SecretDecryptionError("ciphertext v3:AAAA:BBBB:CCCC")

    resp = TestClient(probe).get("/boom")
    assert resp.status_code == 409
    assert resp.json()["code"] == enc.STORED_VALUE_UNREADABLE_CODE
    assert "v3:" not in resp.text
