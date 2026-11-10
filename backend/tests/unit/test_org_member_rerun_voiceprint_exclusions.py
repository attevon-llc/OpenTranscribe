"""The org-member re-run's voiceprint delete keeps BOTH what a hold freezes and what was handed over.

After a restore, ``erase_org_member_data(created_before=...)`` re-erases what the member had at the
original erasure. Two exclusions reach the same ``delete_by_query``: the speakers of a held file
(and the profile holding one of their voices), kept by document id (``held_voices``), and what the
organization handed to the member after the erasure, kept by ``media_file_id`` / ``profile_id``
terms (``acquired_after``). Each is pinned alone elsewhere
(``test_gdpr_erasure_held_voiceprints.py``, ``test_org_member_rerun.py``); this runs them together
against an evaluating index, so dropping either clause, or one replacing the other, leaves a
document missing instead of only a different request body.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core import embedding_models
from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services import erasure_ledger_service as ledger
from app.services import gdpr_erasure_service
from app.services import org_content_transfer
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user

pytestmark = pytest.mark.usefixtures("no_receipt_measurement", "no_erasure_extras")

ERASED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BEFORE = ERASED_AT - timedelta(days=30)
AFTER = ERASED_AT + timedelta(days=3)


@pytest.fixture()
def index(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    monkeypatch.setattr(
        "app.services.opensearch_service.remove_profile_embedding",
        lambda uuid: fake.delete_by_query(
            index=embedding_models.get_speaker_index_v4(),
            body={"query": {"ids": {"values": [f"profile_{uuid}"]}}},
        ),
    )
    monkeypatch.setattr(
        "app.services.gdpr_erasure_steps.purge_media_file",
        lambda db, media_file, **kw: {"deleted": True, "residual_errors": []},
    )
    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)
    return fake


def _recording(db, owner, org_id) -> MediaFile:
    row = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="meeting.wav",
        storage_path=f"rerun/{uuid_pkg.uuid4().hex}.wav",
        content_type="audio/wav",
        file_size=1,
        user_id=owner.id,
        organization_id=org_id,
        upload_time=BEFORE,
    )
    db.add(row)
    db.commit()
    return row


def _profile(db, owner, org_id, name) -> SpeakerProfile:
    row = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        name=name,
        user_id=owner.id,
        organization_id=org_id,
        created_at=BEFORE,
    )
    db.add(row)
    db.commit()
    return row


def _speaker(db, owner, media_file, profile=None) -> Speaker:
    row = Speaker(
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
        profile_id=profile.id if profile else None,
    )
    db.add(row)
    db.commit()
    return row


def test_a_held_voice_and_a_handed_over_voice_both_survive_one_rerun(db_session, index):
    db = db_session
    org = make_org(db, "both")
    member, colleague = make_user(db, "member"), make_user(db, "leaver")
    db.add_all(
        [
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="org:admin"),
            OrganizationMembership(organization_id=org.id, user_id=colleague.id, role="org:member"),
        ]
    )
    db.commit()

    # The member's own rows, all from before the erasure (a restore brought them back).
    held_file, free_file = _recording(db, member, org.id), _recording(db, member, org.id)
    db.execute(text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": held_file.id})
    kept_profile = _profile(db, member, org.id, "held voice")
    free_profile = _profile(db, member, org.id, "free voice")
    held = _speaker(db, member, held_file, kept_profile)
    free = _speaker(db, member, free_file, free_profile)

    # A colleague's recording and profile from before the erasure, handed over after it.
    handed_file = _recording(db, colleague, org.id)
    handed_profile = _profile(db, colleague, org.id, "handed over")
    handed = _speaker(db, colleague, handed_file, handed_profile)
    result = org_content_transfer.transfer_org_content(
        db, organization_id=org.id, from_user_id=colleague.id, to_user_id=member.id
    )
    assert result.counts["files"] == 1 and result.counts["speaker_profiles"] == 1

    entry = ledger.record_request(
        db,
        subject_type="org_member",
        subject_user_id=member.id,
        subject_user_uuid=member.uuid,
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
        actor_kind="org_admin",
    )
    assert entry is not None
    entry.status, entry.completed_at = "complete", ERASED_AT
    db.commit()

    # Every document carries the member's stamp (the transfer restamps the handed-over ones) and
    # its content's creation time, so the scope and time bound alone select all but ``new``.
    def doc(doc_id, *, media_file_id=None, profile_id=None, created_at=BEFORE, kind=None):
        index.add(
            doc_id,
            member.id,
            org.id,
            kind,
            media_file_id,
            profile_id=profile_id,
            created_at=created_at,
        )
        return doc_id

    survivors = {
        doc(str(held.uuid), media_file_id=held_file.id),
        doc(f"profile_{kept_profile.uuid}", profile_id=kept_profile.id, kind="profile"),
        doc(str(handed.uuid), media_file_id=handed_file.id),
        doc(f"profile_{handed_profile.uuid}", profile_id=handed_profile.id, kind="profile"),
        doc("created-after-the-erasure", created_at=AFTER),
    }
    erased = {
        doc(str(free.uuid), media_file_id=free_file.id),
        doc(f"profile_{free_profile.uuid}", profile_id=free_profile.id, kind="profile"),
    }

    summary = gdpr_erasure_service.erase_org_member_data(
        db, member.id, org.id, ledger_entry=entry, created_before=ERASED_AT
    )

    assert index.ids() == survivors, (
        f"erased that should survive: {survivors - index.ids()}; "
        f"survived that should be erased: {index.ids() & erased}"
    )
    assert summary["legal_holds_skipped"] >= 2, "the held file and its voice's profile defer"
    db.expire_all()
    assert db.get(SpeakerProfile, kept_profile.id) is not None, "a held voice's profile is frozen"
    assert db.get(SpeakerProfile, handed_profile.id) is not None, "handed over after the erasure"
    assert db.get(SpeakerProfile, free_profile.id) is None
    assert db.get(Speaker, held.id).profile_id == kept_profile.id
