"""User settings a deployment can lock through capability keys (issue #1109).

A capability that is off here means "the deployment owns this value": the
user's override is ignored and the deployment default applies. The same rule
runs at every point the value is used — the upload/reprocess endpoints, the
user-settings endpoint, and the transcription task that reads stored settings —
so a value saved before the lock, or sent by a client that ignores the hidden
UI, has no effect.

No platform-admin bypass: these describe the deployment, not a tier. The task
reads them with ``request=None`` (see ``app.core.capabilities``).
"""

from __future__ import annotations

import logging

from fastapi import Request

from app.core.capabilities import capability_enabled

logger = logging.getLogger(__name__)

MODEL_CHOICE = "transcription.model_choice"
DIARIZATION_SOURCE = "transcription.diarization_source"
ADVANCED = "transcription.advanced"

#: Transcription-settings API field -> capability that lets a user set it.
#: The stored ``UserSetting`` key is always ``"transcription_" + field``.
TRANSCRIPTION_FIELD_CAPABILITY: dict[str, str] = {
    "diarization_source": DIARIZATION_SOURCE,
    "vad_threshold": ADVANCED,
    "vad_min_silence_ms": ADVANCED,
    "vad_min_speech_ms": ADVANCED,
    "vad_speech_pad_ms": ADVANCED,
    "hallucination_silence_threshold": ADVANCED,
    "repetition_penalty": ADVANCED,
}


def locked_transcription_fields(request: Request | None = None) -> frozenset[str]:
    """Transcription-settings fields the user may not set in this deployment."""
    enabled = {
        key: capability_enabled(key, request)
        for key in set(TRANSCRIPTION_FIELD_CAPABILITY.values())
    }
    return frozenset(
        field for field, key in TRANSCRIPTION_FIELD_CAPABILITY.items() if not enabled[key]
    )


def locked_transcription_db_keys(request: Request | None = None) -> frozenset[str]:
    """``UserSetting`` keys whose stored value must be ignored in this deployment."""
    return frozenset(f"transcription_{field}" for field in locked_transcription_fields(request))


def effective_whisper_model(requested: str | None, request: Request | None) -> str | None:
    """The per-file model to honour: ``requested``, or None when model choice is locked.

    None means "use the deployment default", so a lightweight model named by the
    client can no longer route the file to the CPU transcription path.
    """
    if not requested or capability_enabled(MODEL_CHOICE, request):
        return requested
    logger.info(
        "Ignoring client-requested whisper_model=%r: model choice is managed by this deployment",
        requested,
    )
    return None
