"""The diarization factory builds another provider's speaker detection from a saved config.

``create_for_source("asr_config", asr_config=row)`` and ``create_for_user`` with the
``asr_config`` source build an ``ASRDiarizationAdapter`` through
``ASRProviderFactory.create_from_db_config`` (the one decryption path), or refuse with the
fixed-sentence ``DiarizerConfigUnusableError``: no config, an unshared one, a local or
text-only model, a key that no longer decrypts, or the per-user provider capability off.
Building makes no network call; that is what lets the job path resolve it inside a short
read session and run ``diarize`` after the session closes (#1226).
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

SOURCE_KEY = "transcription_diarization_source"
POINTER_KEY = "transcription_diarization_asr_config_uuid"


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


def _choose(db, user, *, source="asr_config", pointer=None):
    from app.models import UserSetting

    db.add(UserSetting(user_id=user.id, setting_key=SOURCE_KEY, setting_value=source))
    if pointer is not None:
        db.add(UserSetting(user_id=user.id, setting_key=POINTER_KEY, setting_value=str(pointer)))
    db.commit()


def _activate(db, user, row):
    from app.models import UserSetting

    db.add(
        UserSetting(user_id=user.id, setting_key="active_asr_config_id", setting_value=str(row.id))
    )
    db.commit()


class TestCreateForSource:
    def test_a_speaker_config_becomes_an_adapter_without_any_network_call(
        self, db_session, normal_user
    ):
        row = _config(db_session, normal_user)
        with patch("deepgram.DeepgramClient") as client:
            provider = DiarizationProviderFactory.create_for_source("asr_config", asr_config=row)

        assert isinstance(provider, ASRDiarizationAdapter)
        assert provider.provider_name == "deepgram"
        assert provider.model_name == "nova-3"
        assert provider.supports_speaker_count() is False  # deepgram sends no hints
        assert client.call_count == 0

    def test_hints_follow_the_catalog(self, db_session, normal_user):
        row = _config(db_session, normal_user, provider="assemblyai", model="universal")
        provider = DiarizationProviderFactory.create_for_source("asr_config", asr_config=row)
        assert provider is not None
        assert provider.supports_speaker_count() is True

    @pytest.mark.parametrize("model", ["whisper-1", "gpt-4o-transcribe"])
    def test_a_text_only_model_is_refused(self, db_session, normal_user, model):
        row = _config(db_session, normal_user, provider="openai", model=model)
        with pytest.raises(DiarizerConfigUnusableError):
            DiarizationProviderFactory.create_for_source("asr_config", asr_config=row)

    def test_the_diarizing_openai_model_is_accepted(self, db_session, normal_user):
        """Control for the refusal above: model, not provider, decides."""
        row = _config(db_session, normal_user, provider="openai", model="gpt-4o-transcribe-diarize")
        provider = DiarizationProviderFactory.create_for_source("asr_config", asr_config=row)
        assert isinstance(provider, ASRDiarizationAdapter)

    def test_no_config_and_a_local_config_are_refused(self, db_session, normal_user):
        with pytest.raises(DiarizerConfigUnusableError):
            DiarizationProviderFactory.create_for_source("asr_config", asr_config=None)
        local = _config(db_session, normal_user, provider="local", model="large-v3", api_key=None)
        with pytest.raises(DiarizerConfigUnusableError):
            DiarizationProviderFactory.create_for_source("asr_config", asr_config=local)

    def test_a_key_that_no_longer_decrypts_is_a_fixed_sentence(self, db_session, normal_user):
        row = _config(db_session, normal_user)
        row.api_key = "v3:not-a-real-ciphertext"
        with pytest.raises(DiarizerConfigUnusableError) as info:
            DiarizationProviderFactory.create_for_source("asr_config", asr_config=row)
        assert str(info.value) == DiarizerConfigUnusableError.MESSAGE


class TestCreateForUser:
    def test_the_chosen_config_is_built(self, db_session, normal_user):
        row = _config(db_session, normal_user)
        _choose(db_session, normal_user, pointer=row.uuid)

        provider = DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

        assert isinstance(provider, ASRDiarizationAdapter)
        assert provider.provider_name == "deepgram"

    def test_a_missing_pointer_is_refused(self, db_session, normal_user):
        _choose(db_session, normal_user)
        with pytest.raises(DiarizerConfigUnusableError):
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

    def test_a_deleted_config_is_refused(self, db_session, normal_user):
        _choose(db_session, normal_user, pointer=uuid.uuid4())
        with pytest.raises(DiarizerConfigUnusableError):
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

    def test_another_users_unshared_config_is_refused(self, db_session, normal_user, other_user):
        theirs = _config(db_session, other_user, shared=False)
        _choose(db_session, normal_user, pointer=theirs.uuid)
        with pytest.raises(DiarizerConfigUnusableError):
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

    def test_the_capability_off_makes_the_config_unavailable(self, db_session, normal_user):
        row = _config(db_session, normal_user)
        _choose(db_session, normal_user, pointer=row.uuid)
        with (
            patch("app.core.capabilities.capability_enabled", return_value=False),
            pytest.raises(DiarizerConfigUnusableError),
        ):
            DiarizationProviderFactory.create_for_user(normal_user.id, db_session)

    def test_the_transcription_provider_itself_means_its_own_speakers(
        self, db_session, normal_user
    ):
        """Same provider and model as the active transcription: no second, billed call."""
        active = _config(db_session, normal_user)
        _activate(db_session, normal_user, active)
        same = _config(db_session, normal_user)
        _choose(db_session, normal_user, pointer=same.uuid)

        assert DiarizationProviderFactory.create_for_user(normal_user.id, db_session) is None

    def test_a_different_transcription_provider_keeps_the_adapter(self, db_session, normal_user):
        """Control for the test above."""
        active = _config(db_session, normal_user, provider="gladia", model="standard")
        _activate(db_session, normal_user, active)
        diarizer = _config(db_session, normal_user)
        _choose(db_session, normal_user, pointer=diarizer.uuid)

        provider = DiarizationProviderFactory.create_for_user(normal_user.id, db_session)
        assert isinstance(provider, ASRDiarizationAdapter)

    def test_the_deployment_lock_means_the_default(self, db_session, normal_user):
        row = _config(db_session, normal_user)
        _choose(db_session, normal_user, pointer=row.uuid)
        with patch(
            "app.core.locked_settings.locked_transcription_fields",
            return_value=frozenset({"diarization_source"}),
        ):
            assert DiarizationProviderFactory.create_for_user(normal_user.id, db_session) is None


def test_get_diarizer_asr_config_tolerates_a_malformed_pointer(db_session, normal_user):
    assert get_diarizer_asr_config(db_session, normal_user.id, "not-a-uuid") is None
    assert get_diarizer_asr_config(db_session, normal_user.id, None) is None
    row = _config(db_session, normal_user)
    found = get_diarizer_asr_config(db_session, normal_user.id, str(row.uuid))
    assert found is not None
    assert found.id == row.id
