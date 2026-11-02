"""A voiceprint purge skips files under a legal hold or in quarantine, at request AND execution time.

The owner's per-file opt-out (``PATCH /files/{uuid}/voiceprint-consent``) and the queued purge
task both ask ``hold_guard``; a held file keeps its voiceprints, a normal file is purged.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC
from datetime import datetime

import pytest
from fastapi import status

from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import MediaFile
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from app.services.privacy.purge_file_guard import guard_files_scope
from app.tasks.privacy_purge_task import privacy_purge_task
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_file as _make_file


def make_file(db, user):
    # make_file's fixed storage path collides on the unique key once a test needs two files.
    mf = _make_file(db, user)
    mf.storage_path = f"user_{user.id}/{mf.uuid}.wav"
    db.flush()
    return mf


def _org_hold(db, org, media_file, *, released: bool = False) -> ContentHold:
    hold = ContentHold(
        organization_id=org.id,
        scope="file",
        reason="pending litigation matter",
        released_at=datetime.now(UTC) if released else None,
    )
    db.add(hold)
    db.flush()
    db.add(ContentHoldFile(hold_id=hold.id, media_file_id=media_file.id))
    db.flush()
    return hold


@pytest.fixture
def sent(monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(
        privacy_purge_task,
        "apply_async",
        lambda args=None, kwargs=None, **o: queued.append(kwargs),
    )
    clear_write_backstop_cache()
    return queued


def _patch(client, headers, mf, state="declined"):
    return client.patch(
        f"/api/files/{mf.uuid}/voiceprint-consent", headers=headers, json={"state": state}
    )


def _consent(db, mf) -> str:
    db.expire_all()
    return str(db.get(MediaFile, mf.id).voiceprint_consent)


def test_platform_held_file_is_refused_with_409_and_nothing_changes(
    client, user_token_headers, db_session, normal_user, sent
):
    mf = make_file(db_session, normal_user)
    mf.legal_hold = True
    db_session.commit()

    response = _patch(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"
    assert response.json()["detail"]["held_by"] == ["platform"]
    assert _consent(db_session, mf) == "unspecified"
    assert sent == []


def test_org_held_file_is_refused_with_409_and_nothing_changes(
    client, user_token_headers, db_session, normal_user, sent
):
    mf = make_file(db_session, normal_user)
    _org_hold(db_session, make_org(db_session), mf)
    db_session.commit()

    response = _patch(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["held_by"] == ["organization"]
    assert "holds" not in response.json()["detail"]
    assert _consent(db_session, mf) == "unspecified"
    assert sent == []


def test_quarantined_file_is_not_found_and_nothing_changes(
    client, user_token_headers, db_session, normal_user, sent
):
    mf = make_file(db_session, normal_user)
    mf.is_quarantined = True
    db_session.commit()

    response = _patch(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _consent(db_session, mf) == "unspecified"
    assert sent == []


def test_released_org_hold_does_not_block_the_opt_out(
    client, user_token_headers, db_session, normal_user, sent
):
    mf = make_file(db_session, normal_user)
    _org_hold(db_session, make_org(db_session), mf, released=True)
    db_session.commit()

    response = _patch(client, user_token_headers, mf)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert len(sent) == 1


def test_normal_file_still_queues_the_purge(
    client, user_token_headers, db_session, normal_user, sent
):
    mf = make_file(db_session, normal_user)
    db_session.commit()

    assert _patch(client, user_token_headers, mf).status_code == status.HTTP_200_OK
    assert sent[0]["scope"]["file_ids"] == [mf.id]


def test_execution_time_guard_narrows_the_scope_to_purgeable_files(db_session, normal_user):
    free = make_file(db_session, normal_user)
    platform = make_file(db_session, normal_user)
    platform.legal_hold = True
    org_held = make_file(db_session, normal_user)
    _org_hold(db_session, make_org(db_session), org_held)
    quarantined = make_file(db_session, normal_user)
    quarantined.is_quarantined = True
    db_session.flush()
    ids = (free.id, platform.id, org_held.id, quarantined.id)

    narrowed = guard_files_scope(db_session, purge.PurgeScope(kind="files", file_ids=ids))
    nothing = guard_files_scope(db_session, purge.PurgeScope(kind="files", file_ids=ids[1:]))

    assert narrowed is not None and narrowed.file_ids == (free.id,)
    assert nothing is None


def test_guard_passes_non_file_scopes_through(db_session):
    scope = purge.PurgeScope(kind="instance_all")

    assert guard_files_scope(db_session, scope) is scope


def test_task_aborts_without_purging_when_every_file_is_held(db_session, normal_user, monkeypatch):
    mf = make_file(db_session, normal_user)
    mf.legal_hold = True
    db_session.commit()
    ran: list[object] = []

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(purge, "run_purge_pass", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(purge, "targets_still_enabled", lambda *a, **k: [])
    monkeypatch.setattr("app.tasks.privacy_purge_task.session_scope", scope)

    result = privacy_purge_task.run(
        {"kind": "files", "file_ids": [mf.id]}, ["voiceprints"], None, purge_reason="opt_out"
    )

    assert result == {"status": "aborted", "reason": "files_protected"}
    assert ran == []
