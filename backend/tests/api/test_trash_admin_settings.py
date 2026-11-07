"""The trash window setting and the trashed-storage counts in the admin stats (issue #1101).

``files.trash_retention_days`` (default 30, 1 to 90) rides on the existing retention config
routes, admin-only like the rest of them. A change applies to files trashed from then on; a
file already in the trash keeps the ``purge_after`` it was given. The admin stats keep
trashed files in the totals (they still hold storage) and say how much of it is in the trash.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from app.services import trash_service

BASE = "/api/admin/settings/retention-config"


@pytest.fixture
def no_trash_setting(db_session):
    db_session.query(SystemSettings).filter(
        SystemSettings.key == "files.trash_retention_days"
    ).delete(synchronize_session=False)
    db_session.commit()


def _file(db, owner, size: int) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"stats_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=size,
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.commit()
    return media_file


def test_the_trash_window_defaults_to_30_days(client, admin_token_headers, no_trash_setting):
    response = client.get(BASE, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["trash_retention_days"] == 30


def test_an_admin_sets_the_window_and_only_new_trash_uses_it(
    client, admin_token_headers, normal_user, db_session, no_trash_setting
):
    before = _file(db_session, normal_user, 10)
    trash_service.trash_files(db_session, [before], actor=normal_user, request=None, bypass=None)
    old_purge_after = before.purge_after

    response = client.put(BASE, headers=admin_token_headers, json={"trash_retention_days": 7})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["trash_retention_days"] == 7

    after = _file(db_session, normal_user, 10)
    trash_service.trash_files(db_session, [after], actor=normal_user, request=None, bypass=None)
    db_session.expire_all()
    assert after.purge_after is not None and after.deleted_at is not None
    assert (after.purge_after - after.deleted_at).days == 7
    assert db_session.get(MediaFile, before.id).purge_after == old_purge_after


@pytest.mark.parametrize("days", [0, 91])
def test_the_window_is_bounded(client, admin_token_headers, days):
    response = client.put(BASE, headers=admin_token_headers, json={"trash_retention_days": days})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_a_plain_user_cannot_change_the_window(client, user_token_headers):
    response = client.put(BASE, headers=user_token_headers, json={"trash_retention_days": 1})

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_the_admin_stats_count_trashed_files_and_their_storage(
    client, admin_token_headers, normal_user, db_session
):
    def stats() -> dict:
        response = client.get("/api/admin/stats", headers=admin_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        files: dict = response.json()["files"]
        return files

    start = stats()
    media_file = _file(db_session, normal_user, 12345)
    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    end = stats()
    assert end["trashed_files"] - start["trashed_files"] == 1
    assert end["trashed_bytes"] - start["trashed_bytes"] == 12345
    assert end["total"] - start["total"] == 1, "a trashed file still holds storage"
