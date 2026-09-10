"""``GET /api/tags/{tag_uuid}/files`` must drop quarantined files (#817).

``tag_service._visible_to`` already excludes a quarantined file's id from the
"attached to an accessible file" arm when deciding whether the TAG itself is
visible — but the file-*listing* half, ``tag_collisions.files_for_tag``, was
never given the matching exclusion. So a takedown left the quarantined file's
title, status and duration fully readable through the tag's own file list, even
though the tag route otherwise treats quarantine correctly.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag


def _make_tagged_file(db_session, owner, tag, **file_overrides) -> MediaFile:
    """A file owned by ``owner``, tagged with ``tag``."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "tag_files_quarantine_probe.wav",
        "title": "tag_files_quarantine_probe",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(file_overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)

    db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag.id, source="manual"))
    db_session.commit()
    return media_file


def _make_owned_tag(db_session, owner) -> Tag:
    tag = Tag(uuid=uuid.uuid4(), name=f"probe-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


def test_quarantined_tagged_file_not_listed(client, user_token_headers, normal_user, db_session):
    """The finding: a takedown must remove the file from the tag's file list."""
    tag = _make_owned_tag(db_session, normal_user)
    _make_tagged_file(db_session, normal_user, tag, is_quarantined=True)

    response = client.get(f"/api/tags/{tag.uuid}/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "tag_files_quarantine_probe" not in response.text


def test_total_excludes_the_quarantined_file(client, user_token_headers, normal_user, db_session):
    """The total count must exclude the quarantined file too, not just the
    returned file list."""
    tag = _make_owned_tag(db_session, normal_user)
    _make_tagged_file(db_session, normal_user, tag, is_quarantined=True)

    response = client.get(f"/api/tags/{tag.uuid}/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["total"] == 0


def test_clean_tagged_file_still_listed(client, user_token_headers, normal_user, db_session):
    """The control, without which the two tests above would also pass if the
    route were simply broken for everyone."""
    tag = _make_owned_tag(db_session, normal_user)
    _make_tagged_file(db_session, normal_user, tag)

    response = client.get(f"/api/tags/{tag.uuid}/files", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert "tag_files_quarantine_probe" in response.text


def test_an_admin_can_still_see_their_own_quarantined_tagged_file(
    client, admin_token_headers, admin_user, db_session
):
    """An admin who cannot see a quarantined file cannot review or release it —
    the same bypass every other takedown surface grants admins."""
    tag = _make_owned_tag(db_session, admin_user)
    _make_tagged_file(db_session, admin_user, tag, is_quarantined=True)

    response = client.get(f"/api/tags/{tag.uuid}/files", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert "tag_files_quarantine_probe" in response.text
