"""A cloud plan whose provider cannot be built fails the file visibly (issue #1236, item 1).

``ASRProviderFactory.create_for_user`` degrades a saved config it cannot load (a key that no
longer decrypts, a refused endpoint, a removed row) to the env default or to local Whisper,
and the transcription stage used to read "local" as "no cloud provider" and fall through to
the local engine: the user's file was transcribed on this server against their choice (full
image), or failed deep inside the engine with a raw import error (lite, which ships no
Whisper). Now the stage raises ``CloudASRUnusableError``, whose text is a fixed sentence
classified as a permanent configuration failure, and nothing is transcribed.

Decided for both deployments: never fall back. The control in every table is the same call
with a provider that does build, which must come back unchanged.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.exceptions import ASRConfigurationError
from app.models.prompt import UserSetting
from app.models.user_asr_settings import UserASRSettings
from app.services.error_categorization_service import CLOUD_ASR_UNUSABLE_MESSAGE
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.utils.error_classification import ErrorCategory
from tests.unit._plan_contexts import make_plan

_CORE = "app.tasks.transcription.core"


class _Provider:
    def __init__(self, name: str) -> None:
        self.provider_name = name


@contextmanager
def _no_session():
    yield None


def _build(result):
    """``create_for_user`` stand-in: return ``result``, or raise it when it is an error."""

    def _create(user_id, db):
        if isinstance(result, BaseException):
            raise result
        return result

    return _create


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_Provider("local"), id="degraded-to-local"),
        pytest.param(_Provider("assemblyai"), id="degraded-to-another-vendor"),
        pytest.param(ValueError("key does not decrypt"), id="construction-raised"),
        pytest.param(
            ASRConfigurationError("WhisperX transcription is not available"), id="lite-refusal"
        ),
    ],
)
def test_a_cloud_plan_never_falls_back(outcome):
    from app.tasks.transcription.core import CloudASRUnusableError
    from app.tasks.transcription.core import _cloud_asr_provider_or_none

    plan = make_plan("cloud_speakers", "provider")  # plans deepgram
    with (
        patch(f"{_CORE}.session_scope", _no_session),
        patch("app.services.asr.factory.ASRProviderFactory.create_for_user", _build(outcome)),
        pytest.raises(CloudASRUnusableError) as failed,
    ):
        _cloud_asr_provider_or_none(1, plan)

    assert str(failed.value) == CLOUD_ASR_UNUSABLE_MESSAGE


def test_the_planned_provider_is_returned():
    """Control: the provider the plan names builds, and is what the stage transcribes with."""
    from app.tasks.transcription.core import _cloud_asr_provider_or_none

    built = _Provider("deepgram")
    with (
        patch(f"{_CORE}.session_scope", _no_session),
        patch("app.services.asr.factory.ASRProviderFactory.create_for_user", _build(built)),
    ):
        assert _cloud_asr_provider_or_none(1, make_plan("cloud_speakers", "provider")) is built


def test_a_local_plan_builds_no_provider():
    from app.tasks.transcription.core import _cloud_asr_provider_or_none

    def _must_not_build(user_id, db):
        raise AssertionError("built a provider for a local plan")

    with patch("app.services.asr.factory.ASRProviderFactory.create_for_user", _must_not_build):
        assert _cloud_asr_provider_or_none(1, make_plan("local_gpu", "provider")) is None


def test_the_failure_is_a_permanent_configuration_error_with_a_fixed_sentence():
    from app.tasks.transcription.core import CloudASRUnusableError

    failure = ErrorCategorizationService.classify_failure(str(CloudASRUnusableError()))

    assert failure.reason == UserErrorReason.PIPELINE_NOT_RUNNABLE
    assert failure.retry_category == ErrorCategory.CONFIGURATION_REQUIRED
    assert failure.user_message == CLOUD_ASR_UNUSABLE_MESSAGE


# --- the real factory, the real database ---------------------------------------------------


@pytest.mark.parametrize("mode", ["full", "lite"])
def test_a_saved_key_that_no_longer_decrypts_fails_the_run(
    db_session, normal_user, monkeypatch, mode
):
    """The degrade path end to end: the active Deepgram config's key cannot be decrypted, so
    the factory falls back to local (full) or refuses local (lite). Either way: fail."""
    from app.tasks.transcription.core import CloudASRUnusableError
    from app.tasks.transcription.core import _cloud_asr_provider_or_none

    @contextmanager
    def _bridged():
        yield db_session

    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", mode)
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    cfg = UserASRSettings(
        user_id=normal_user.id,
        name=f"deepgram-{uuid_pkg.uuid4().hex[:6]}",
        provider="deepgram",
        model_name="nova-3",
        api_key="v3:this-is-not-ciphertext",
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    db_session.add(
        UserSetting(
            user_id=normal_user.id, setting_key="active_asr_config_id", setting_value=str(cfg.id)
        )
    )
    db_session.commit()

    with (
        patch(f"{_CORE}.session_scope", _bridged),
        pytest.raises(CloudASRUnusableError),
    ):
        _cloud_asr_provider_or_none(normal_user.id, make_plan("cloud_speakers", "provider"))


# --- the stage: nothing is transcribed, the file fails with the sentence ------------------------


def test_the_gpu_stage_fails_the_file_with_the_fixed_sentence():
    """The stage resolves the provider before any download or engine call, so the raise
    here is also proof nothing was transcribed; the failure policy gets the sentence."""
    from app.tasks.transcription import core as core_module
    from app.tasks.transcription.core import CloudASRUnusableError

    failures: list[str] = []

    def _record_failure(ctx, task_id, raw_error, error_type):
        failures.append(raw_error)
        return {"status": "error"}

    context = {
        "task_id": f"task-{uuid_pkg.uuid4().hex[:8]}",
        "file_uuid": str(uuid_pkg.uuid4()),
        "file_id": 1,
        "user_id": 1,
        "storage_path": "files/test.wav",
        "file_name": "test.wav",
        "content_type": "audio/wav",
        "pipeline_plan": make_plan("cloud_speakers", "provider").to_context(),
    }
    with (
        patch(f"{_CORE}.session_scope", _no_session),
        patch(f"{_CORE}.update_task_status"),
        patch(f"{_CORE}._superseded_or_none", return_value=None),
        patch(f"{_CORE}._handle_transcription_failure", _record_failure),
        patch(
            "app.services.asr.factory.ASRProviderFactory.create_for_user",
            _build(_Provider("local")),
        ),
        pytest.raises(CloudASRUnusableError),
    ):
        core_module.transcribe_gpu_task.__wrapped__(context)

    # The failure policy received the fixed sentence, never the factory's own text.
    assert failures == [CLOUD_ASR_UNUSABLE_MESSAGE]
