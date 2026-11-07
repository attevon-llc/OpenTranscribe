"""Deployment-wide speech-detection (VAD) defaults, stored as ``transcription.default_vad_*``.

A user's own value wins; where a user has none, or the deployment locks the advanced settings
(``core/locked_settings``), these apply. With nothing stored the coded defaults in
``core/constants.py`` apply, so an untouched deployment behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
from app.core.constants import DEFAULT_VAD_MIN_SPEECH_MS
from app.core.constants import DEFAULT_VAD_SPEECH_PAD_MS
from app.core.constants import DEFAULT_VAD_THRESHOLD
from app.services.system_settings_service import get_settings_map
from app.services.system_settings_service import set_setting

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: field -> (SystemSettings key, coded default, minimum, maximum). Bounds match the
#: per-user ``TranscriptionSettings`` schema.
VAD_FIELDS: dict[str, tuple[str, float | int, float | int, float | int]] = {
    "vad_threshold": ("transcription.default_vad_threshold", DEFAULT_VAD_THRESHOLD, 0.1, 0.95),
    "vad_min_silence_ms": (
        "transcription.default_vad_min_silence_ms",
        DEFAULT_VAD_MIN_SILENCE_MS,
        100,
        5000,
    ),
    "vad_min_speech_ms": (
        "transcription.default_vad_min_speech_ms",
        DEFAULT_VAD_MIN_SPEECH_MS,
        50,
        5000,
    ),
    "vad_speech_pad_ms": (
        "transcription.default_vad_speech_pad_ms",
        DEFAULT_VAD_SPEECH_PAD_MS,
        0,
        2000,
    ),
}


@dataclass(frozen=True)
class DeploymentVadDefaults:
    """The effective deployment defaults for the four speech-detection values."""

    vad_threshold: float
    vad_min_silence_ms: int
    vad_min_speech_ms: int
    vad_speech_pad_ms: int


def _coerce(field: str, raw: str | None) -> float | int:
    _key, default, low, high = VAD_FIELDS[field]
    if raw is None or raw == "":
        return default
    try:
        value = float(raw) if isinstance(default, float) else int(raw)
    except ValueError:
        return default
    return value if low <= value <= high else default


def get_deployment_vad_defaults(db: Session) -> DeploymentVadDefaults:
    """The effective deployment defaults (stored value, else the coded default)."""
    stored = get_settings_map(db, [meta[0] for meta in VAD_FIELDS.values()])
    values = {field: _coerce(field, stored.get(meta[0])) for field, meta in VAD_FIELDS.items()}
    return DeploymentVadDefaults(
        vad_threshold=float(values["vad_threshold"]),
        vad_min_silence_ms=int(values["vad_min_silence_ms"]),
        vad_min_speech_ms=int(values["vad_min_speech_ms"]),
        vad_speech_pad_ms=int(values["vad_speech_pad_ms"]),
    )


def set_deployment_vad_defaults(
    db: Session, updates: dict[str, float | int]
) -> DeploymentVadDefaults:
    """Store the given fields; every value must be inside its bounds."""
    for field, value in updates.items():
        key, _default, low, high = VAD_FIELDS[field]
        if not low <= value <= high:
            raise ValueError(f"{field} must be between {low} and {high}")
        set_setting(db, key, value, f"Deployment default for {field}")
    return get_deployment_vad_defaults(db)
