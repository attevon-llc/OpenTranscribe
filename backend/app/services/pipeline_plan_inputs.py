"""Gather a user's :class:`~app.services.pipeline_plan.PlanInputs` from the database.

Reuses, never re-derives: the active ASR config comes from
``ASRProviderFactory.resolve_active_config`` (the same precedence a job uses), the #1109
lock from ``locked_transcription_fields``, the pyannote.ai key from
``has_pyannote_credential``, and capabilities from the ASR catalog. DB reads only; no
provider is built and nothing is decrypted or contacted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Request

from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import VALID_DIARIZATION_SOURCES
from app.services.pipeline_plan import DiarizerConfigInfo
from app.services.pipeline_plan import PipelinePlan
from app.services.pipeline_plan import PlanInputs
from app.services.pipeline_plan import resolve_pipeline_plan

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SOURCE_KEY = "transcription_diarization_source"
DIARIZER_CONFIG_KEY = "transcription_diarization_asr_config_uuid"

#: Passed for an override that was not supplied (``None`` is a real value for the uuid).
_UNSET = object()


def _stored(db: Session, user_id: int) -> dict[str, str]:
    from app import models

    rows = (
        db.query(models.UserSetting.setting_key, models.UserSetting.setting_value)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_([SOURCE_KEY, DIARIZER_CONFIG_KEY]),
        )
        .all()
    )
    return {str(key): str(value) for key, value in rows if value}


def _diarizer_config(
    db: Session, user_id: int, config_uuid: str | None, request: Request | None
) -> DiarizerConfigInfo | None:
    """The saved ASR configuration named as the speaker detector, if the user may use it.

    The lookup (own or shared, ``asr.user_providers`` capability) is the diarization
    factory's ``get_diarizer_asr_config``, the same one the job path builds the adapter from.
    """
    from app.services.asr.factory import ASRProviderFactory
    from app.services.diarization.factory import get_diarizer_asr_config

    cfg = get_diarizer_asr_config(db, user_id, config_uuid, request)
    if cfg is None:
        return None
    provider, model = str(cfg.provider or "local"), str(cfg.model_name or "")
    caps = ASRProviderFactory.get_model_capabilities(provider, model)
    return DiarizerConfigInfo(
        provider=provider,
        model=model,
        uuid=str(cfg.uuid),
        returns_speakers=provider != "local" and caps["supports_diarization"],
        speaker_hints=frozenset(caps["speaker_count_hints"]),
    )


def load_plan_inputs(
    db: Session,
    user_id: int,
    *,
    request: Request | None = None,
    whisper_model: str | None = None,
    disable_diarization: bool | None = None,
    diarization_source: str | None = None,
    diarization_asr_config_uuid: object = _UNSET,
    gpu_split: bool | None = None,
    active_asr_config: object = _UNSET,
) -> PlanInputs:
    """The inputs the user's next file would be planned with.

    Args:
        db: An open session (reads only).
        user_id: Whose settings.
        request: The HTTP request, for capability resolution; ``None`` in a task.
        whisper_model: A per-file model; a lightweight one means CPU transcription.
        disable_diarization: The per-file "skip speaker detection".
        diarization_source: Plan as if this source were stored (the settings PUT).
        diarization_asr_config_uuid: Plan as if this diarizer config were stored.
        gpu_split: Whether the split GPU topology is live; ``None`` reads the request flag.
        active_asr_config: Plan as if this ``UserASRSettings`` row (``None``: no row) were
            the active transcription config (the ASR-side switches, issue #1225).
    """
    from app.core.config import settings
    from app.core.constants import gpu_split_enabled
    from app.core.locked_settings import locked_transcription_fields
    from app.services.asr.factory import ASRProviderFactory
    from app.services.diarization.factory import get_pyannote_credential
    from app.transcription.config import LIGHTWEIGHT_MODELS
    from app.transcription.config import TranscriptionConfig

    if active_asr_config is _UNSET:
        provider, model, cfg = ASRProviderFactory.resolve_active_config(user_id, db)
    else:
        from app.models.user_asr_settings import UserASRSettings

        target = active_asr_config if isinstance(active_asr_config, UserASRSettings) else None
        provider, model, cfg = ASRProviderFactory.resolve_config(target)
    caps = ASRProviderFactory.get_model_capabilities(provider, model)

    stored = _stored(db, user_id)
    source = diarization_source or stored.get(SOURCE_KEY, DEFAULT_DIARIZATION_SOURCE)
    config_uuid = (
        stored.get(DIARIZER_CONFIG_KEY)
        if diarization_asr_config_uuid is _UNSET
        else diarization_asr_config_uuid
    )
    locked = "diarization_source" in locked_transcription_fields(request)
    if locked:
        source, config_uuid = DEFAULT_DIARIZATION_SOURCE, None
    if source not in VALID_DIARIZATION_SOURCES:
        logger.warning("Unknown stored diarization source %r for user %d", source, user_id)
        source = DEFAULT_DIARIZATION_SOURCE
    if disable_diarization:
        source = "off"

    has_key, pyannote_model = False, None
    if source == "pyannote":
        from app.services.diarization.pyannote_provider import current_pyannote_model

        credential = get_pyannote_credential(user_id, db)
        has_key = bool(credential is not None and credential.api_key)
        pyannote_model = current_pyannote_model(credential.model_name if credential else None)

    return PlanInputs(
        deployment_mode=str(settings.DEPLOYMENT_MODE).lower(),
        asr_provider=provider,
        asr_model=model or None,
        asr_config_uuid=str(cfg.uuid) if cfg is not None else None,
        asr_returns_speakers=bool(caps["supports_diarization"]),
        asr_speaker_hints=frozenset(caps["speaker_count_hints"]),
        asr_timing=caps["timestamp_granularity"],
        diarization_source=source,
        source_locked=locked,
        diarizer_backend=TranscriptionConfig._resolve_diarizer_backend(),
        has_pyannote_key=has_key,
        pyannote_model=pyannote_model,
        diarization_config=(
            _diarizer_config(db, user_id, str(config_uuid) if config_uuid else None, request)
            if source == "asr_config"
            else None
        ),
        cpu_whisper_model=whisper_model if whisper_model in LIGHTWEIGHT_MODELS else None,
        gpu_split=gpu_split_enabled() if gpu_split is None else gpu_split,
    )


def plan_for_user(db: Session, user_id: int, **kwargs) -> PipelinePlan:
    """``resolve_pipeline_plan(load_plan_inputs(...))``."""
    return resolve_pipeline_plan(load_plan_inputs(db, user_id, **kwargs))
