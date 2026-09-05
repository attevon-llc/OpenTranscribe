"""``export_locked`` must be enforced for EVERY transcript export format, not just subtitles.

Issue #673: the admin "mandate censored exports for all users" floor was enforced in exactly
one backend path (``files/subtitles.py``). Every other export format — txt/json/csv/srt/vtt —
was serialized **client-side** in the browser from data already in memory, so it never
consulted the setting at all: an admin who enabled the floor got the guarantee for subtitle
downloads and nothing else.

The fix moves txt/json/csv/srt/vtt serialization to a new backend endpoint,
``GET /api/files/{uuid}/export``, which resolves the same ``EffectiveRedactionConfig`` the
subtitle export does and applies the identical fail-closed rule: ``export_locked`` always wins
over a caller's ``redact=false``, regardless of format.

These tests enumerate every format so a new format added later without wiring the gate fails
here, rather than reproducing the exact bug shape this issue found (issue #673's own "Suggested
fix" #2).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment

PROFANITY = "damn"
CLEAN_TEXT = "the quarterly numbers look fine"
SENSITIVE_TEXT = f"this is a {PROFANITY} mess and here is more of it"

ALL_FORMATS = ("txt", "json", "csv", "srt", "vtt")

_USER_PREFS = "/api/user-settings/redaction"
_ADMIN_POLICY = "/api/admin/redaction-policy"


def _set_export_locked(client, super_admin_token_headers, locked: bool) -> None:
    resp = client.post(
        f"{_ADMIN_POLICY}/update",
        json={"force_export_redacted": locked},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK


def _enable_user_redaction(client, user_token_headers) -> None:
    resp = client.put(
        _USER_PREFS,
        json={"enabled": True, "categories": ["profanity"]},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK


def _make_file(db_session, owner) -> MediaFile:
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


def _export(client, headers, media_file, fmt: str, *, redact: bool | None = None):
    params = {"format": fmt}
    if redact is not None:
        params["redact"] = "false" if redact is False else "true"
    return client.get(f"/api/files/{media_file.uuid}/export", headers=headers, params=params)


# ---------------------------------------------------------------------------
# export_locked ON: every format must mask, and a reveal request cannot bypass it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_masks_every_format(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    _enable_user_redaction(client, user_token_headers)
    _set_export_locked(client, super_admin_token_headers, True)
    media_file = _make_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text, (
        f"{fmt} export leaked unmasked content under export_locked"
    )
    assert CLEAN_TEXT in response.text


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_cannot_be_bypassed_by_redact_false(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """The owner's own ``?redact=false`` reveal request must not defeat the admin floor."""
    _enable_user_redaction(client, user_token_headers)
    _set_export_locked(client, super_admin_token_headers, True)
    media_file = _make_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt, redact=False)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text, f"{fmt} export bypassed export_locked via redact=false"


# ---------------------------------------------------------------------------
# export_locked OFF: an owner who disabled redaction gets the raw content back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_off_and_redaction_disabled_exports_unmasked(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    _set_export_locked(client, super_admin_token_headers, False)
    media_file = _make_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY in response.text, f"{fmt} export masked content with no policy enforcing it"


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_off_but_user_enabled_masks_by_default(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """A user's own redaction preference still applies; export_locked being off just
    means it is not admin-mandated."""
    _enable_user_redaction(client, user_token_headers)
    _set_export_locked(client, super_admin_token_headers, False)
    media_file = _make_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY not in response.text


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_export_locked_off_owner_reveal_shows_original(
    client, super_admin_token_headers, user_token_headers, normal_user, db_session, fmt
):
    """Without the admin floor, the owner's own ``?redact=false`` reveals their content."""
    _enable_user_redaction(client, user_token_headers)
    _set_export_locked(client, super_admin_token_headers, False)
    media_file = _make_file(db_session, normal_user)

    response = _export(client, user_token_headers, media_file, fmt, redact=False)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert PROFANITY in response.text


# ---------------------------------------------------------------------------
# Misc endpoint behaviour
# ---------------------------------------------------------------------------


def test_unsupported_format_is_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/export",
        headers=user_token_headers,
        params={"format": "pdf"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_export_requires_auth(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/export", params={"format": "txt"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
