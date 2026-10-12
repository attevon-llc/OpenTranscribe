"""Effective speaker-attribute toggles for one user (issues #1200, #1148).

One resolver for every reader, so "attribute detection", "gender detection" and the
sidecar gender write cannot disagree about what the user chose.

Resolution per flag: user ``UserSetting`` row > system ``speaker_attribute.*`` setting >
default. Detection's default additionally honours ``SPEAKER_ATTRIBUTE_DETECTION_ENABLED``.
The native diarizer's own ``DIAR_NATIVE_GENDER`` env switch is a separate, additional hard
off applied where the sidecar runs (``transcription/diarizer_native.py``); it is not
consulted here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

_TRUTHY = ("true", "1", "yes", "on")


@dataclass(frozen=True)
class SpeakerAttributeFlags:
    """What the owner of a file has chosen for speaker attributes."""

    detection_enabled: bool
    gender_detection_enabled: bool
    show_on_cards: bool

    @property
    def gender_prediction_allowed(self) -> bool:
        """True only when attribute detection AND gender detection are both on."""
        return self.detection_enabled and self.gender_detection_enabled


def resolve_speaker_attribute_flags(db: Session, user_id: int) -> SpeakerAttributeFlags:
    """Resolve the user's speaker-attribute toggles (user > system > default)."""
    from app.models.prompt import UserSetting
    from app.services.system_settings_service import get_settings_map

    env_enabled = os.environ.get("SPEAKER_ATTRIBUTE_DETECTION_ENABLED", "true").lower() == "true"
    names = {
        "detection_enabled": ("speaker_attribute_detection_enabled", env_enabled),
        "gender_detection_enabled": ("speaker_attribute_gender_detection_enabled", True),
        "show_on_cards": ("speaker_attribute_show_on_cards", True),
    }

    system = get_settings_map(db, [f"speaker_attribute.{name}" for name in names])
    user_rows = (
        db.query(UserSetting.setting_key, UserSetting.setting_value)
        .filter(
            UserSetting.user_id == user_id,
            UserSetting.setting_key.in_([key for key, _ in names.values()]),
        )
        .all()
    )
    user = {key: value for key, value in user_rows}

    resolved: dict[str, bool] = {}
    for name, (user_key, default) in names.items():
        if user_key in user:
            resolved[name] = str(user[user_key]).lower() == "true"
            continue
        raw = system.get(f"speaker_attribute.{name}")
        resolved[name] = raw.lower() in _TRUTHY if raw is not None else default
    return SpeakerAttributeFlags(**resolved)
