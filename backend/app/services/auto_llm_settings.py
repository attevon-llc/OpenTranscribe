"""Whether the automatic AI steps after a transcription run for a user (issue #1148).

Two steps run on their own once a file is transcribed and each sends transcript text to
the configured AI provider: LLM speaker-name suggestions and topic extraction (which feeds
tag and collection suggestions). One resolver answers "is this on for this user" for every
automatic dispatch site and for the recovery sweep, so they cannot disagree.

Resolution per flag: user ``UserSetting`` row > system ``auto_llm.*`` setting > ``True``.
The default is on so nothing changes for anyone who never touches the switch.

Only the AUTOMATIC dispatch consults these. Manual triggers (the "suggest speaker names" and
"extract topics" buttons, an explicit reprocess stage list) never do. Topic extraction queued
as a companion of a manual summary (``ai_insights_companion``) is automatic: the user did not
ask for topics by name, so it does.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.env import parse_bool

SPEAKER_ID_USER_KEY = "auto_llm_speaker_identification"
TOPIC_USER_KEY = "auto_topic_extraction"
SPEAKER_ID_SYSTEM_KEY = "auto_llm.speaker_identification"
TOPIC_SYSTEM_KEY = "auto_llm.topic_extraction"


@dataclass(frozen=True)
class AutoLlmFlags:
    """What the owner of a file has chosen for the automatic AI steps."""

    speaker_identification: bool
    topic_extraction: bool


def resolve_auto_llm_flags(db: Session, user_id: int) -> AutoLlmFlags:
    """Resolve the user's automatic-AI switches (user > system default > True)."""
    return resolve_auto_llm_flags_for_users(db, [user_id])[user_id]


def resolve_auto_llm_flags_for_users(db: Session, user_ids: list[int]) -> dict[int, AutoLlmFlags]:
    """Resolve the switches for several users in two queries (used by the recovery sweep)."""
    from app.models.prompt import UserSetting
    from app.services.system_settings_service import get_settings_map

    system = get_settings_map(db, [SPEAKER_ID_SYSTEM_KEY, TOPIC_SYSTEM_KEY])
    system_speaker = parse_bool(system.get(SPEAKER_ID_SYSTEM_KEY), True)
    system_topic = parse_bool(system.get(TOPIC_SYSTEM_KEY), True)

    rows = (
        db.query(UserSetting.user_id, UserSetting.setting_key, UserSetting.setting_value)
        .filter(
            UserSetting.user_id.in_(user_ids),
            UserSetting.setting_key.in_([SPEAKER_ID_USER_KEY, TOPIC_USER_KEY]),
        )
        .all()
    )
    chosen: dict[int, dict[str, str]] = {}
    for uid, key, value in rows:
        chosen.setdefault(uid, {})[key] = str(value)

    return {
        uid: AutoLlmFlags(
            speaker_identification=parse_bool(
                chosen.get(uid, {}).get(SPEAKER_ID_USER_KEY), system_speaker
            ),
            topic_extraction=parse_bool(chosen.get(uid, {}).get(TOPIC_USER_KEY), system_topic),
        )
        for uid in user_ids
    }
