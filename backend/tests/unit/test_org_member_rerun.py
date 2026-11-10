"""Automatic, time-bounded org-member re-erasure after a restore (issue #1355, work package M)."""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.prompt import UserSetting
from app.services import erasure_ledger_service as ledger
from app.services import gdpr_erasure_service
from app.services.erasure_org_member_rerun import ORG_MEMBER_RERUN_UNTIMED
from app.services.erasure_org_member_rerun import TIMED_TABLES
from app.services.erasure_org_member_rerun import cutoff_of
from app.services.erasure_org_member_rerun import rerun_state

ERASED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _quiet_externals(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gdpr_erasure_service, "_erase_speaker_voiceprints", lambda **kw: 0)


@pytest.fixture()
def world(db_session, normal_user):
    org = Organization(name="Rerun Org", slug=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    entry = ledger.record_request(
        db_session,
        subject_type="org_member",
        subject_user_id=normal_user.id,
        subject_user_uuid=normal_user.uuid,
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
        actor_kind="org_admin",
    )
    assert entry is not None
    entry.status = "complete"
    entry.completed_at = ERASED_AT
    db_session.commit()
    return org, entry


def _setting(db, user, org, key, created_at):
    row = UserSetting(
        user_id=user.id,
        organization_id=org.id,
        setting_key=key,
        setting_value="x",
        created_at=created_at,
    )
    db.add(row)
    db.commit()
    if created_at is None:  # the column default stamps "now" on insert; force the NULL
        row.created_at = None
        db.execute(text("UPDATE user_setting SET created_at = NULL WHERE id = :i"), {"i": row.id})
        db.commit()
    return row


def _keys(db, user, org):
    return {
        r.setting_key
        for r in db.query(UserSetting).filter_by(user_id=user.id, organization_id=org.id)
    }


def test_restored_rows_before_erasure_are_reerased(db_session, normal_user, world):
    org, entry = world
    _setting(db_session, normal_user, org, "restored", ERASED_AT - timedelta(days=3))

    state = rerun_state(db_session, entry)
    assert state.old_rows == 1

    ledger.record_resurrection(db_session, entry, store_key="pg.org_member")
    summary = gdpr_erasure_service.erase_org_member_data(
        db_session,
        normal_user.id,
        org.id,
        ledger_entry=entry,
        created_before=cutoff_of(entry),
    )

    assert summary["user_settings_deleted"] == 1
    assert _keys(db_session, normal_user, org) == set()
    db_session.refresh(entry)
    assert entry.completed_at == ERASED_AT  # the bound survives its own re-run
    assert entry.resurrections_detected == 1


def test_rows_created_after_erasure_survive(db_session, normal_user, world):
    org, entry = world
    _setting(db_session, normal_user, org, "restored", ERASED_AT - timedelta(days=3))
    _setting(db_session, normal_user, org, "legitimate-new", ERASED_AT + timedelta(days=2))

    ledger.record_resurrection(db_session, entry, store_key="pg.org_member")
    gdpr_erasure_service.erase_org_member_data(
        db_session,
        normal_user.id,
        org.id,
        ledger_entry=entry,
        created_before=cutoff_of(entry),
    )

    assert _keys(db_session, normal_user, org) == {"legitimate-new"}


def test_a_new_upload_alone_is_not_a_resurrection(db_session, normal_user, world):
    org, entry = world
    _setting(db_session, normal_user, org, "legitimate-new", ERASED_AT + timedelta(days=2))
    assert rerun_state(db_session, entry).old_rows == 0


def test_untimed_rows_are_reported_not_deleted(db_session, normal_user, world):
    org, entry = world
    _setting(db_session, normal_user, org, "no-timestamp", None)

    state = rerun_state(db_session, entry)
    assert (state.old_rows, state.untimed_rows) == (0, 1)

    ledger.record_resurrection(db_session, entry, store_key="pg.org_member")
    gdpr_erasure_service.erase_org_member_data(
        db_session, normal_user.id, org.id, ledger_entry=entry, created_before=cutoff_of(entry)
    )
    assert _keys(db_session, normal_user, org) == {"no-timestamp"}


def test_a_direct_erasure_request_still_erases_everything(db_session, normal_user, world):
    org, _ = world
    _setting(db_session, normal_user, org, "newer", ERASED_AT + timedelta(days=2))
    gdpr_erasure_service.erase_org_member_data(db_session, normal_user.id, org.id)
    assert _keys(db_session, normal_user, org) == set()


def test_the_bound_is_kept_through_a_deferral(db_session, normal_user, world):
    org, entry = world
    ledger.record_resurrection(db_session, entry, store_key="pg.org_member")
    ledger.record_outcome(db_session, entry, {"legal_holds_skipped": 1, "errors": []})
    db_session.refresh(entry)
    assert entry.status == "deferred"
    assert entry.completed_at == ERASED_AT
    assert cutoff_of(entry) == ERASED_AT


def test_every_table_the_rerun_bounds_has_a_timestamp_column():
    for model, ts_name in TIMED_TABLES:
        assert hasattr(model, ts_name), f"{model.__name__} has no {ts_name}"
    assert ORG_MEMBER_RERUN_UNTIMED == {}


def test_an_unresurrected_entry_has_no_bound(world):
    _, entry = world
    assert isinstance(entry, ErasureLedgerEntry)
    assert cutoff_of(entry) is None


def _hand_over_to_the_erased_member(db, member, org):
    """A colleague's org recording and speaker profile, made BEFORE the member's erasure, handed
    to the member after it (the colleague left)."""
    from app.models.media import MediaFile
    from app.models.organization import OrganizationMembership
    from app.models.speaker import SpeakerProfile
    from app.services import org_content_transfer
    from tests.user_owned_rows import make_user

    colleague = make_user(db, "leaver")
    db.add_all(
        [
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="org:admin"),
            OrganizationMembership(organization_id=org.id, user_id=colleague.id, role="org:member"),
        ]
    )
    before = ERASED_AT - timedelta(days=30)
    recording = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="handed-over.wav",
        storage_path=f"rerun/{uuid_pkg.uuid4().hex}.wav",
        content_type="audio/wav",
        file_size=1,
        user_id=colleague.id,
        organization_id=org.id,
        upload_time=before,
    )
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        name="Handed Over",
        user_id=colleague.id,
        organization_id=org.id,
        created_at=before,
    )
    db.add_all([recording, profile])
    db.commit()
    result = org_content_transfer.transfer_org_content(
        db, organization_id=org.id, from_user_id=colleague.id, to_user_id=member.id
    )
    assert result.counts["files"] == 1 and result.counts["speaker_profiles"] == 1
    return int(recording.id), int(profile.id)


def test_content_handed_to_an_erased_member_is_not_a_resurrection(
    db_session, normal_user, world, monkeypatch
):
    """A handover is not a restore (v471). The handed-over rows keep their creation time, which
    predates the erasure; judged by it, the re-run "re-erased" a colleague's recordings."""
    from app.models.media import MediaFile
    from app.models.speaker import SpeakerProfile

    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)
    org, entry = world
    file_id, profile_id = _hand_over_to_the_erased_member(db_session, normal_user, org)

    assert rerun_state(db_session, entry).old_rows == 0

    summary = gdpr_erasure_service.erase_org_member_data(
        db_session, normal_user.id, org.id, ledger_entry=entry, created_before=ERASED_AT
    )

    assert summary["media_files_deleted"] == 0
    assert summary["speaker_profiles_deleted"] == 0
    db_session.expire_all()
    assert db_session.get(MediaFile, file_id) is not None
    assert db_session.get(SpeakerProfile, profile_id) is not None


def test_the_time_bounded_voiceprint_erasure_keeps_handed_over_documents(
    db_session, normal_user, world, monkeypatch
):
    from app.services.erasure_org_member_rerun import acquired_after

    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)
    calls: list[dict] = []

    def record(**kw) -> int:
        calls.append(kw)
        return 0

    monkeypatch.setattr(gdpr_erasure_service, "_erase_speaker_voiceprints", record)
    org, entry = world
    file_id, profile_id = _hand_over_to_the_erased_member(db_session, normal_user, org)

    handed = acquired_after(db_session, normal_user.id, org.id, ERASED_AT)
    gdpr_erasure_service.erase_org_member_data(
        db_session, normal_user.id, org.id, ledger_entry=entry, created_before=ERASED_AT
    )

    assert handed.file_ids == (file_id,) and handed.profile_ids == (profile_id,)
    [call] = calls
    assert {"terms": {"media_file_id": [file_id]}} in call["keep"]
    assert {"terms": {"profile_id": [profile_id]}} in call["keep"]
