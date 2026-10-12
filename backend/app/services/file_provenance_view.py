"""What the API shows about how a file was processed (issue #1229).

``build_processing_steps`` turns ``media_file.processing_provenance`` (written only by
``services/file_provenance``) into six display-ready rows, in processing order. A file
processed before recording began has no document; its rows are composed from the older
columns and marked ``recorded="legacy"``, and anything those columns cannot say is
``not_recorded``. Nothing is inferred: a legacy ``diarization_provider = 'pyannote'``
could be the local engine or the pyannote.ai service, so its location stays unknown.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.schemas.processing_provenance import STEP_ORDER
from app.schemas.processing_provenance import ProcessingProvenance
from app.schemas.processing_provenance import ProcessingStepOut
from app.schemas.processing_provenance import ProvenanceEntry
from app.schemas.processing_provenance import StepName
from app.schemas.processing_provenance import Where

logger = logging.getLogger(__name__)

#: Display names for ids that are not cloud transcription vendors. Proper nouns only, so
#: they need no translation.
_ENGINE_DISPLAY = {
    "local": "Whisper",
    "native": "diar-native",
    "pyannote": "PyAnnote",
    "pyannote.ai": "pyannote.ai",
}
_LLM_DISPLAY = {
    "openai": "OpenAI",
    "vllm": "vLLM",
    "ollama": "Ollama",
    "anthropic": "Anthropic",
    "claude": "Anthropic",
    "openrouter": "OpenRouter",
    "bedrock": "Amazon Bedrock",
}
_LLM_STEPS = frozenset({"summary", "topics", "speaker_names"})
_LEGACY_VOICEPRINT_MODEL = {
    "v3": "pyannote/embedding",
    "v4": "pyannote/wespeaker-voxceleb-resnet34-LM",
}


def _vendor_display(provider: str) -> str | None:
    from app.services.asr.factory import ASR_PROVIDER_CATALOG

    entry = ASR_PROVIDER_CATALOG.get(provider)
    return str(entry["display_name"]) if entry and provider != "local" else None


def _display(step: StepName, provider: str | None, engine: str | None) -> str | None:
    if provider is None:
        return None
    if step in _LLM_STEPS:
        return _LLM_DISPLAY.get(provider)
    if step != "voiceprints" and engine in ("asr", "cloud"):
        return _vendor_display(provider) or provider
    if provider == "local" and step != "transcription":
        # The local speaker engine could not say which one served (``local`` fallback id):
        # the model and "this server" say what is known; "Whisper" would be wrong here.
        return None
    return _ENGINE_DISPLAY.get(provider) or _vendor_display(provider) or provider


def _parse(raw: Any, file_id: Any) -> ProcessingProvenance | None:
    if raw is None:
        return None
    try:
        document: ProcessingProvenance = ProcessingProvenance.model_validate(raw)
        return document
    except ValidationError:
        logger.warning("Ignoring malformed processing_provenance on file %s", file_id)
        return None


def _from_entry(step: StepName, entry: ProvenanceEntry) -> ProcessingStepOut:
    if entry.engine == "off":
        return ProcessingStepOut(step=step, status="off", recorded="full")
    return ProcessingStepOut(
        step=step,
        status="done",
        recorded="full",
        provider=entry.provider,
        provider_display=_display(step, entry.provider, entry.engine),
        model=entry.model,
        where=entry.where,
        timing=entry.timing if step == "transcription" else None,
    )


def _not_recorded(step: StepName) -> ProcessingStepOut:
    return ProcessingStepOut(step=step, status="not_recorded", recorded="none")


def _legacy_where(provider: str) -> Where | None:
    if provider in ("local", "native"):
        return "server"
    if provider != "pyannote" and _vendor_display(provider) is not None:
        return "cloud"
    return None


def _legacy(step: StepName, media_file: Any) -> ProcessingStepOut:
    """Compose a step from the pre-#1229 columns, never claiming more than they say."""
    if step == "transcription":
        provider = media_file.asr_provider
        if provider is None:
            return _not_recorded(step)
        return ProcessingStepOut(
            step=step,
            status="done",
            recorded="legacy",
            provider=provider,
            provider_display=_ENGINE_DISPLAY["local"]
            if provider == "local"
            else _vendor_display(provider) or provider,
            model=media_file.asr_model,
            where=_legacy_where(provider),
        )
    if step in ("diarization", "voiceprints") and media_file.diarization_disabled:
        return ProcessingStepOut(step=step, status="off", recorded="legacy")
    if step == "diarization":
        provider = media_file.diarization_provider
        if provider is None:
            return _not_recorded(step)
        return ProcessingStepOut(
            step=step,
            status="done",
            recorded="legacy",
            provider=provider,
            provider_display=_ENGINE_DISPLAY.get(provider) or _vendor_display(provider) or provider,
            model=media_file.diarization_model,
            where=_legacy_where(provider),
        )
    if step == "voiceprints":
        model = _LEGACY_VOICEPRINT_MODEL.get(media_file.embedding_mode or "")
        if model is None or media_file.diarization_provider is None:
            return _not_recorded(step)
        # Voiceprints have only ever been computed on this server (design plan A9).
        return ProcessingStepOut(
            step=step, status="done", recorded="legacy", model=model, where="server"
        )
    if step == "summary":
        summary = media_file.summary_data if isinstance(media_file.summary_data, dict) else {}
        raw_metadata = summary.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        provider, model = metadata.get("provider"), metadata.get("model")
        if not provider and not model:
            return _not_recorded(step)
        return ProcessingStepOut(
            step=step,
            status="done",
            recorded="legacy",
            provider=str(provider) if provider else None,
            provider_display=_LLM_DISPLAY.get(str(provider)) if provider else None,
            model=str(model) if model else None,
        )
    return _not_recorded(step)


def build_processing_steps(media_file: Any) -> list[ProcessingStepOut]:
    """Six rows, in processing order, for a file the caller is allowed to read."""
    document = _parse(media_file.processing_provenance, getattr(media_file, "uuid", None))
    steps = document.steps if document is not None else {}
    # A recorded transcription means the current run post-dates recording, so a missing
    # step has simply not completed (or was skipped) in that run.
    run_recorded = "transcription" in steps
    diarization_off = "diarization" in steps and steps["diarization"].engine == "off"

    rows: list[ProcessingStepOut] = []
    for step in STEP_ORDER:
        entry = steps.get(step)
        if entry is not None:
            rows.append(_from_entry(step, entry))
        elif run_recorded and step == "voiceprints" and diarization_off:
            rows.append(ProcessingStepOut(step=step, status="off", recorded="full"))
        elif run_recorded:
            rows.append(ProcessingStepOut(step=step, status="not_run", recorded="full"))
        else:
            rows.append(_legacy(step, media_file))
    return rows
