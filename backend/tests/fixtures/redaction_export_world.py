"""A profane, redactable transcript and the admin / user switches around it.

Shared by the export gate suites (``api/test_transcript_export_redaction_gate.py`` for the
synchronous formats, ``api/test_document_export_redaction_gate.py`` for DOCX/PDF) so every
export surface is driven by the same file, the same words and the same policy toggles.
"""

from __future__ import annotations

import uuid as uuid_pkg

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment

PROFANITY = "damn"
CLEAN_TEXT = "the quarterly numbers look fine"
SENSITIVE_TEXT = f"this is a {PROFANITY} mess and here is more of it"

USER_PREFS = "/api/user-settings/redaction"
ADMIN_POLICY = "/api/admin/redaction-policy"


def set_export_locked(client, super_admin_token_headers, locked: bool) -> None:
    resp = client.post(
        f"{ADMIN_POLICY}/update",
        json={"force_export_redacted": locked},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK


def enable_user_redaction(client, user_token_headers) -> None:
    resp = client.put(
        USER_PREFS,
        json={"enabled": True, "categories": ["profanity"]},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK


def make_redactable_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="export673.wav",
        title="export673",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        redaction_status="done",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    speaker = Speaker(
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        display_name="Alice",
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    start = SENSITIVE_TEXT.find(PROFANITY)
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.5,
            text=CLEAN_TEXT,
        )
    )
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=1.5,
            end_time=3.0,
            text=SENSITIVE_TEXT,
            redactions=[
                {
                    "char_start": start,
                    "char_end": start + len(PROFANITY),
                    "category": "profanity",
                    "entity_type": "PROFANITY",
                    "detector": "wordlist",
                    "confidence": 1.0,
                }
            ],
        )
    )
    db_session.commit()
    return media_file
