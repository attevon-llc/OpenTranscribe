"""The diarization factory builds another provider's speaker detection from a saved config.

``create_for_engine("asr_config", user_id, db, config_uuid=...)`` (the engine the pipeline
plan names) builds an ``ASRDiarizationAdapter`` through
``ASRProviderFactory.create_from_db_config`` (the one decryption path), or refuses with the
fixed-sentence ``DiarizerConfigUnusableError``: no config, an unshared one, a local or
text-only model, a key that no longer decrypts, or the per-user provider capability off.
Building makes no network call; that is what lets the parallel cloud path and the post stage
build it inside a short read session and run ``diarize`` after the session closes (#1226).
"Same provider and model as the transcription" is the plan's decision (it resolves to that
provider's own speakers), so the factory never sees it.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.services.diarization.asr_adapter import ASRDiarizationAdapter
from app.services.diarization.errors import DiarizerConfigUnusableError
from app.services.diarization.factory import DiarizationProviderFactory
from app.services.diarization.factory import get_diarizer_asr_config
from app.utils.encryption import encrypt_api_key


def _config(db, user, provider="deepgram", model="nova-3", *, shared=False, api_key="dg-key"):
    from app.models.user_asr_settings import UserASRSettings

    row = UserASRSettings(
        user_id=user.id,
        name=f"diarizer-{uuid.uuid4().hex[:8]}",
        provider=provider,
        model_name=model,
        api_key=encrypt_api_key(api_key) if api_key else None,
        is_shared=shared,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _build(db, user, config_uuid):
    return DiarizationProviderFactory.create_for_engine(
        "asr_config", user.id, db, config_uuid=str(config_uuid) if config_uuid else None
    )


def test_a_speaker_config_becomes_an_adapter_without_any_network_call(db_session, normal_user):
    row = _config(db_session, normal_user)
    with patch("deepgram.DeepgramClient") as client:
        provider = _build(db_session, normal_user, row.uuid)

    assert isinstance(provider, ASRDiarizationAdapter)
    assert provider.provider_name == "deepgram"
    assert provider.model_name == "nova-3"
    assert provider.supports_speaker_count() is False  # deepgram sends no hints
    assert client.call_count == 0


def test_hints_follow_the_catalog(db_session, normal_user):
    row = _config(db_session, normal_user, provider="assemblyai", model="universal")
    assert _build(db_session, normal_user, row.uuid).supports_speaker_count() is True


@pytest.mark.parametrize("model", ["whisper-1", "gpt-4o-transcribe"])
def test_a_text_only_model_is_refused(db_session, normal_user, model):
    row = _config(db_session, normal_user, provider="openai", model=model)
    with pytest.raises(DiarizerConfigUnusableError):
        _build(db_session, normal_user, row.uuid)


def test_the_diarizing_openai_model_is_accepted(db_session, normal_user):
    """Control for the refusal above: model, not provider, decides."""
    row = _config(db_session, normal_user, provider="openai", model="gpt-4o-transcribe-diarize")
    assert isinstance(_build(db_session, normal_user, row.uuid), ASRDiarizationAdapter)


def test_a_local_config_is_refused(db_session, normal_user):
    local = _config(db_session, normal_user, provider="local", model="large-v3", api_key=None)
    with pytest.raises(DiarizerConfigUnusableError):
        _build(db_session, normal_user, local.uuid)


def test_a_key_that_no_longer_decrypts_is_a_fixed_sentence(db_session, normal_user):
    row = _config(db_session, normal_user)
    row.api_key = "v3:not-a-real-ciphertext"
    db_session.commit()
    with pytest.raises(DiarizerConfigUnusableError) as info:
        _build(db_session, normal_user, row.uuid)
    assert str(info.value) == DiarizerConfigUnusableError.MESSAGE


@pytest.mark.parametrize("pointer", [None, "deleted"])
def test_a_missing_or_deleted_config_is_refused(db_session, normal_user, pointer):
    config_uuid = uuid.uuid4() if pointer == "deleted" else None
    with pytest.raises(DiarizerConfigUnusableError):
        _build(db_session, normal_user, config_uuid)


def test_another_users_unshared_config_is_refused(db_session, normal_user, other_user):
    theirs = _config(db_session, other_user, shared=False)
    with pytest.raises(DiarizerConfigUnusableError):
        _build(db_session, normal_user, theirs.uuid)


def test_the_capability_off_makes_the_config_unavailable(db_session, normal_user):
    row = _config(db_session, normal_user)
    with (
        patch("app.core.capabilities.capability_enabled", return_value=False),
        pytest.raises(DiarizerConfigUnusableError),
    ):
        _build(db_session, normal_user, row.uuid)


def test_get_diarizer_asr_config_tolerates_a_malformed_pointer(db_session, normal_user):
    assert get_diarizer_asr_config(db_session, normal_user.id, "not-a-uuid") is None
    assert get_diarizer_asr_config(db_session, normal_user.id, None) is None
    row = _config(db_session, normal_user)
    found = get_diarizer_asr_config(db_session, normal_user.id, str(row.uuid))
    assert found is not None
    assert found.id == row.id
