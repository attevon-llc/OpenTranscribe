"""Redaction must not be offered, or run, for a viewer whose policy masks nothing.

The file-detail footer used to show "Not yet redacted" + "Run redaction" to every owner,
and the bulk ``redact`` action happily queued a scan, even when the owner had redaction
off. The scan ran and succeeded, then nothing was masked, because masking is applied at
read time from the viewer's enabled categories. The button did something invisible.

"Enabled for the viewer" is ``EffectiveRedactionConfig.masks_anything``: the master
switch is on (user pref or admin force) AND at least one category would be masked.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.core import constants as C  # noqa: N812


@pytest.fixture(autouse=True)
def celery_is_live(monkeypatch):
    monkeypatch.setenv("SKIP_CELERY", "False")


@pytest.fixture
def broker(monkeypatch):
    from app.tasks import redaction_task

    published: list[dict] = []

    class _Task:
        id = "test-task-id"

    def delay(**kwargs):
        published.append(kwargs)
        return _Task()

    monkeypatch.setattr(redaction_task.redaction_detect_task, "delay", delay)
    return published


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


def _completed_file(db_session, owner):
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename=f"gate-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"gate-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language="en",
        status=FileStatus.COMPLETED,
        redaction_status=C.REDACTION_STATUS_DONE,
    )
    db_session.add(media)
    db_session.flush()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            start_time=0.0,
            end_time=5.0,
            text="hello there",
        )
    )
    db_session.flush()
    return media


def _bulk_redact(db_session, user, media):
    from app.api.deps_context import RequestContext
    from app.api.endpoints.files.management import BulkActionRequest
    from app.api.endpoints.files.management import bulk_file_action

    return bulk_file_action(
        request=BulkActionRequest(file_uuids=[str(media.uuid)], action="redact"),
        db=db_session,
        current_user=user,
        ctx=RequestContext(user=user),
    )


def test_masks_anything_requires_the_switch_and_a_category(db_session, normal_user):
    from app.services.redaction.config import resolve_effective_config

    assert resolve_effective_config(db_session, normal_user.id).masks_anything is False

    _set_prefs(db_session, normal_user, redaction_enabled="true")
    assert resolve_effective_config(db_session, normal_user.id).masks_anything is True


def test_enabled_with_no_categories_masks_nothing(db_session, normal_user):
    from app.services.redaction.config import resolve_effective_config

    _set_prefs(db_session, normal_user, redaction_enabled="true", redaction_categories="[]")
    assert resolve_effective_config(db_session, normal_user.id).masks_anything is False


def test_bulk_redact_is_refused_with_409_when_redaction_is_off(db_session, normal_user, broker):
    media = _completed_file(db_session, normal_user)

    with pytest.raises(HTTPException) as exc:
        _bulk_redact(db_session, normal_user, media)

    assert exc.value.status_code == 409
    assert "not enabled" in str(exc.value.detail).lower()
    assert broker == [], "a scan was queued for a viewer whose policy masks nothing"
    db_session.refresh(media)
    assert media.redaction_status == C.REDACTION_STATUS_DONE


def test_bulk_redact_still_runs_when_redaction_is_on(db_session, normal_user, broker):
    _set_prefs(db_session, normal_user, redaction_enabled="true")
    media = _completed_file(db_session, normal_user)

    results = _bulk_redact(db_session, normal_user, media)

    assert results[0].success is True
    assert [c["file_id"] for c in broker] == [media.id]


@pytest.mark.parametrize(
    ("prefs", "expected"),
    [({}, False), ({"redaction_enabled": "true"}, True)],
)
def test_file_detail_reports_redaction_enabled(db_session, normal_user, prefs, expected):
    from app.api.endpoints.files.crud import get_media_file_detail

    _set_prefs(db_session, normal_user, **prefs)
    media = _completed_file(db_session, normal_user)

    detail = get_media_file_detail(db_session, str(media.uuid), normal_user)

    assert detail.redaction_enabled is expected
