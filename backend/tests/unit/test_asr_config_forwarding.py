"""Regression tests for ASR provider config forwarding (issue #300).

`create_for_user` — the path that builds a provider for a real transcription job — used to
omit `access_key_id` in both of its branches while the "Test connection" endpoint passed it.
An AWS config therefore tested green but ran the actual job under whatever ambient
credentials boto3 resolved (env vars / IAM role), or failed with `NoCredentialsError`.

Both paths now go through `ASRProviderFactory.create_from_db_config`, so the tests below
assert on that shared helper *and* that `create_for_user` reaches it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.asr.aws_provider import AWSTranscribeProvider
from app.services.asr.factory import ASRProviderFactory
from app.utils.encryption import encrypt_api_key


class _FakeQuery:
    """Minimal SQLAlchemy query stand-in returning a canned row per model."""

    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _FakeDB:
    """Returns the UserSetting row first, then the UserASRSettings row."""

    def __init__(self, setting, cfg):
        self._setting = setting
        self._cfg = cfg

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if name == "UserASRSettings":
            return _FakeQuery(self._cfg)
        return _FakeQuery(self._setting)


def _aws_cfg(**overrides):
    """A stored AWS config row with encrypted credentials."""
    base = {
        "id": 7,
        "provider": "aws",
        "model_name": "standard",
        "base_url": None,
        "region": "us-west-2",
        "api_key": encrypt_api_key("SECRET-ACCESS-KEY"),
        "access_key_id": encrypt_api_key("AKIAEXAMPLEKEYID"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_create_from_db_config_forwards_both_aws_credentials():
    provider = ASRProviderFactory.create_from_db_config(_aws_cfg())

    assert isinstance(provider, AWSTranscribeProvider)
    assert provider._access_key_id == "AKIAEXAMPLEKEYID"
    assert provider._secret_access_key == "SECRET-ACCESS-KEY"
    assert provider._region == "us-west-2"


def test_create_for_user_matches_test_connection_credentials():
    """The job path and the saved-config test path must build identical credentials."""
    cfg = _aws_cfg()
    db = _FakeDB(setting=SimpleNamespace(setting_value="7"), cfg=cfg)

    job_provider = ASRProviderFactory.create_for_user(user_id=1, db=db)
    test_provider = ASRProviderFactory.create_from_db_config(cfg)

    assert isinstance(job_provider, AWSTranscribeProvider)
    assert isinstance(test_provider, AWSTranscribeProvider)
    assert job_provider._access_key_id == test_provider._access_key_id
    assert job_provider._secret_access_key == test_provider._secret_access_key
    assert job_provider._region == test_provider._region


def test_create_for_user_does_not_fall_back_to_ambient_aws_credentials(monkeypatch):
    """Ambient env credentials must not win over the stored config."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENTWRONGID")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-wrong-secret")

    db = _FakeDB(setting=SimpleNamespace(setting_value="7"), cfg=_aws_cfg())
    provider = ASRProviderFactory.create_for_user(user_id=1, db=db)

    assert isinstance(provider, AWSTranscribeProvider)
    assert provider._access_key_id == "AKIAEXAMPLEKEYID"
    assert provider._secret_access_key == "SECRET-ACCESS-KEY"


def test_aws_config_without_stored_key_still_allows_iam_role(monkeypatch):
    """No stored credentials → boto3's default chain, which is a supported setup."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFROMROLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "role-secret")

    cfg = _aws_cfg(api_key=None, access_key_id=None)
    provider = ASRProviderFactory.create_from_db_config(cfg)

    assert isinstance(provider, AWSTranscribeProvider)
    assert provider._access_key_id == "AKIAFROMROLE"
    assert provider._secret_access_key == "role-secret"


def test_local_provider_config_skips_decryption():
    cfg = SimpleNamespace(
        id=1,
        provider="local",
        model_name=None,
        base_url=None,
        region=None,
        api_key=None,
        access_key_id=None,
    )
    assert ASRProviderFactory.create_from_db_config(cfg).provider_name == "local"


def test_corrupt_stored_credential_raises():
    cfg = _aws_cfg(access_key_id="not-a-valid-ciphertext")
    with pytest.raises(ValueError, match="access key ID"):
        ASRProviderFactory.create_from_db_config(cfg)


def test_create_for_user_falls_back_to_local_on_corrupt_credential():
    """A corrupt stored credential must degrade to local, not crash the job."""
    db = _FakeDB(
        setting=SimpleNamespace(setting_value="7"),
        cfg=_aws_cfg(access_key_id="not-a-valid-ciphertext"),
    )
    assert ASRProviderFactory.create_for_user(user_id=1, db=db).provider_name == "local"


def test_non_aws_provider_ignores_access_key_id():
    """access_key_id is AWS-only; other providers must be unaffected by its presence."""
    cfg = _aws_cfg(provider="deepgram", model_name="nova-3")
    provider = ASRProviderFactory.create_from_db_config(cfg)
    assert provider.provider_name == "deepgram"
