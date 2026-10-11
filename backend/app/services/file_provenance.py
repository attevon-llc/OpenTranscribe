"""The one writer of ``media_file.processing_provenance`` (issue #1229).

Every pipeline stage that completes a step for a file records it through
:func:`record_stage`: provider, model, where it ran (this server or a cloud vendor) and
a few step-specific facts. The write is ONE ``UPDATE ... jsonb_set`` statement, so the
summary, topic and speaker-name tasks, which finish concurrently for the same file, can
never overwrite each other (no read-modify-write). The statement runs in a savepoint and
any failure is logged and swallowed: provenance must never break the write it accompanies.

The ``*_facts`` helpers turn what a stage already knows into canonical facts, so the call
sites in the pipeline stay one line. The read side (what the API shows) is
``services/file_provenance_view.py``.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.schemas.processing_provenance import STEP_ORDER
from app.schemas.processing_provenance import ProvenanceEntry
from app.services.pipeline_plan import PYANNOTE_AI

logger = logging.getLogger(__name__)

#: Canonical provider ids for speaker detection. The two local engines report themselves;
#: the pyannote.ai cloud service reports ``pyannote.ai`` (it used to say ``pyannote``,
#: indistinguishable from the local engine).
LOCAL_DIARIZATION_ENGINES = frozenset({"native", "pyannote"})

_FACT_KEYS = frozenset(ProvenanceEntry.model_fields) - {"completed_at"}

_MERGE_SQL = text(
    """
    UPDATE media_file
       SET processing_provenance = jsonb_set(
             CASE WHEN jsonb_typeof(processing_provenance -> 'steps') = 'object'
                  THEN processing_provenance
                  ELSE '{"v": 1, "steps": {}}'::jsonb END,
             ARRAY['steps', CAST(:stage AS text)],
             CAST(:entry AS jsonb),
             true)
     WHERE id = :file_id
    """
)

#: A new transcription run starts a fresh document: the steps of an earlier run (its
#: speakers, voiceprints, summary) no longer describe this transcript.
_NEW_RUN_SQL = text(
    """
    UPDATE media_file
       SET processing_provenance = jsonb_build_object(
             'v', 1, 'steps', jsonb_build_object(CAST(:stage AS text), CAST(:entry AS jsonb)))
     WHERE id = :file_id
    """
)


def record_stage(
    db: Session, file_id: int, stage: str, *, new_run: bool = False, **facts: Any
) -> bool:
    """Record that ``stage`` completed for ``file_id``, replacing that stage's earlier entry.

    Args:
        db: The stage's write session. The statement joins its transaction, so the record
            commits with the stage's own write.
        file_id: ``media_file.id``.
        stage: One of ``schemas.processing_provenance.STEP_ORDER``.
        new_run: Start a fresh document holding only this stage (transcription only).
        **facts: ``ProvenanceEntry`` fields. ``None`` values are dropped.

    Returns:
        True when written. A bad value (an over-long or URL-shaped model name) or a
        database error is logged and returns False.

    Raises:
        ValueError: ``stage`` is not a known step.
        TypeError: a fact name is not a ``ProvenanceEntry`` field (``base_url``,
            ``api_key`` and every other key are refused by construction).
    """
    if stage not in STEP_ORDER:
        raise ValueError(f"unknown provenance stage: {stage!r}")
    unknown = set(facts) - _FACT_KEYS
    if unknown:
        raise TypeError(f"not provenance facts: {sorted(unknown)}")
    try:
        entry = ProvenanceEntry(
            **{k: v for k, v in facts.items() if v is not None},
            completed_at=datetime.datetime.now(datetime.UTC),
        )
    except ValidationError as exc:
        logger.warning(
            "Provenance for file %s stage %s not recorded: %d invalid fact(s)",
            file_id,
            stage,
            exc.error_count(),
        )
        return False
    params = {
        "file_id": file_id,
        "stage": stage,
        "entry": entry.model_dump_json(exclude_none=True),
    }
    try:
        with db.begin_nested():
            db.execute(_NEW_RUN_SQL if new_run else _MERGE_SQL, params)
    except SQLAlchemyError as exc:
        logger.warning(
            "Provenance for file %s stage %s not recorded: %s", file_id, stage, type(exc).__name__
        )
        return False
    return True


def transcription_facts(asr_provider: str | None, asr_model: str | None) -> dict[str, Any]:
    """Facts for the transcription step, from the provider id and model that ran."""
    if asr_provider is None:
        return {"model": asr_model}
    from app.services.asr.factory import ASRProviderFactory

    local = asr_provider == "local"
    timing = (
        ASRProviderFactory.get_model_capabilities(asr_provider, asr_model)["timestamp_granularity"]
        if asr_model
        else None
    )
    return {
        "provider": asr_provider,
        "model": asr_model,
        "where": "server" if local else "cloud",
        "engine": "local" if local else "cloud",
        "timing": timing,
    }


def diarization_facts(
    provider: str | None, model: str | None, *, asr_provider: str | None = None
) -> dict[str, Any]:
    """Facts for the speaker-detection step from the engine that actually served it.

    ``asr_provider`` disambiguates the one collision left in the ids: a cloud transcription
    provider that returned its own speakers reports its own id, which for the pyannote.ai
    STT service is ``pyannote``, the same spelling as the local in-process engine.
    """
    if provider is None:
        return {"model": model}
    if asr_provider not in (None, "local") and provider == asr_provider:
        return {"provider": provider, "model": model, "where": "cloud", "engine": "asr"}
    if provider in LOCAL_DIARIZATION_ENGINES:
        return {"provider": provider, "model": model, "where": "server", "engine": "local"}
    if provider == PYANNOTE_AI:
        return {"provider": provider, "model": model, "where": "cloud", "engine": "pyannote_ai"}
    return {"provider": provider, "model": model, "where": "cloud", "engine": "asr"}


def inline_speakers_by(asr_result: Any, diarization_requested: bool) -> dict[str, Any]:
    """Who detected the speakers when a cloud transcription provider returned its own.

    Only when the request asked the provider to diarize AND the result carries speakers;
    otherwise empty (nothing detected them, or a separate diarizer did and says so).
    """
    if not (diarization_requested and getattr(asr_result, "has_speakers", False)):
        return {}
    return {"provider": asr_result.provider_name, "model": asr_result.model_name}


def record_transcription_run(
    db: Session,
    file_id: int,
    *,
    asr_provider: str | None,
    asr_model: str | None,
    diarization_provider: str | None,
    diarization_model: str | None,
    diarization_disabled: bool,
) -> None:
    """A transcript was saved: start a new run with its transcription and speaker steps.

    Speaker detection is recorded only when it is known: switched off, or an engine
    reported itself. A post stage that diarizes later records it then.
    """
    record_stage(
        db, file_id, "transcription", new_run=True, **transcription_facts(asr_provider, asr_model)
    )
    if diarization_disabled:
        record_stage(db, file_id, "diarization", engine="off")
    elif diarization_provider is not None:
        record_stage(
            db,
            file_id,
            "diarization",
            **diarization_facts(diarization_provider, diarization_model, asr_provider=asr_provider),
        )


def record_diarization(db: Session, file_id: int, provider: str | None, model: str | None) -> None:
    """A post stage (re-)detected speakers: update the speaker columns and the record."""
    from app.models.media import MediaFile

    media_file = db.get(MediaFile, file_id)
    if media_file is None:
        return
    media_file.diarization_provider = provider
    media_file.diarization_model = model
    media_file.diarization_disabled = False
    if provider is not None:
        # No ``asr_provider``: a post stage is a separate detector by definition, so a
        # local ``pyannote`` here is the engine even after the pyannote.ai STT service.
        record_stage(db, file_id, "diarization", **diarization_facts(provider, model))


def voiceprint_facts(embedding_service: Any) -> dict[str, Any]:
    """Facts for a voiceprint pass run by a ``SpeakerEmbeddingService``.

    Read AFTER extraction: a sidecar lost mid-run flips ``backend`` to ``pyannote``.
    Never raises: an attribute the service does not carry is recorded as unknown, because
    a provenance gap must not fail the voiceprint stage it describes.
    """
    from app.services.embedding_mode_service import MODE_V3
    from app.services.embedding_mode_service import MODE_V4
    from app.services.embedding_mode_service import EmbeddingModeService

    mode = getattr(embedding_service, "mode", None)
    return {
        "provider": getattr(embedding_service, "backend", None),
        "model": getattr(embedding_service, "model_name", None),
        "dims": EmbeddingModeService.get_embedding_dimension(mode)
        if mode in (MODE_V3, MODE_V4)
        else None,
        "where": "server",
        "execution": "embedding_stage",
    }


def record_inline_voiceprints(db: Session, file_id: int, dims: int) -> None:
    """Voiceprints taken from the diarizer's own per-speaker vectors (no extra pass)."""
    from app.models.media import MediaFile
    from app.services.embedding_mode_service import MODE_V3
    from app.services.embedding_mode_service import MODE_V4
    from app.services.embedding_mode_service import EmbeddingModeService

    engine = db.query(MediaFile.diarization_provider).filter(MediaFile.id == file_id).scalar()
    by_dims = {EmbeddingModeService.get_embedding_dimension(m): m for m in (MODE_V3, MODE_V4)}
    mode = by_dims.get(dims)
    record_stage(
        db,
        file_id,
        "voiceprints",
        provider=engine if engine in LOCAL_DIARIZATION_ENGINES else None,
        model=EmbeddingModeService.get_embedding_model_name(mode) if mode else None,
        dims=dims,
        where="server",
        execution="inline_native",
    )


def llm_facts(config: Any) -> dict[str, Any]:
    """Facts for an LLM step from the ``LLMConfig`` that served it.

    Never records ``base_url``. ``is_local_provider`` may resolve a hostname, so call
    this outside any database session.
    """
    from app.services.redaction.llm_guard import is_local_provider

    provider = getattr(config, "provider", None)
    return {
        "provider": str(provider.value if hasattr(provider, "value") else provider)
        if provider
        else None,
        "model": getattr(config, "model", None),
        "where": "server" if is_local_provider(config) else "cloud",
    }
