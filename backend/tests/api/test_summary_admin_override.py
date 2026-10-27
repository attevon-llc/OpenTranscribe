"""Both platform admin roles may generate a summary while the system-wide switch is off.

The switch blocks plain users (423 on ``POST /files/{uuid}/summarize``, ``can_generate`` false in
``GET /files/{uuid}/summary-status``). ``admin`` and ``super_admin`` both pass it: ``super_admin``
used to be treated as an ordinary user because the check compared the role to the literal
``"admin"``. The ordinary user is the control in every test.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import TranscriptSegment

SWITCH = "app.utils.summary_settings.is_summary_enabled_system"


def _file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="override.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    db_session.add(
        TranscriptSegment(media_file_id=media_file.id, start_time=0.0, end_time=1.0, text="hello")
    )
    db_session.commit()
    return media_file


@pytest.fixture
def headers(user_token_headers, admin_token_headers, super_admin_token_headers):
    return {
        "user": user_token_headers,
        "admin": admin_token_headers,
        "super_admin": super_admin_token_headers,
    }


@pytest.fixture
def owners(normal_user, admin_user, super_admin_user):
    return {"user": normal_user, "admin": admin_user, "super_admin": super_admin_user}


@pytest.mark.parametrize(
    ("who", "allowed"), [("user", False), ("admin", True), ("super_admin", True)]
)
def test_summary_status_reports_can_generate_while_the_switch_is_off(
    client, db_session, headers, owners, who, allowed
):
    media_file = _file(db_session, owners[who])
    with (
        patch(SWITCH, return_value=False),
        patch(
            "app.api.endpoints.files.summary_status.is_llm_available",
            new=AsyncMock(return_value=True),
        ),
    ):
        response = client.get(f"/api/files/{media_file.uuid}/summary-status", headers=headers[who])
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["can_generate"] is allowed


@pytest.mark.parametrize(
    ("who", "status_code"), [("user", 423), ("admin", 200), ("super_admin", 200)]
)
def test_summarize_while_the_switch_is_off(client, db_session, headers, owners, who, status_code):
    media_file = _file(db_session, owners[who])
    with (
        patch(SWITCH, return_value=False),
        patch(
            "app.api.endpoints.summarization.is_llm_available",
            new=AsyncMock(return_value=True),
        ),
    ):
        response = client.post(f"/api/files/{media_file.uuid}/summarize", headers=headers[who])
    assert response.status_code == status_code, response.text
