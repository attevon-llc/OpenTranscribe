"""Organization content moves to an administrator when a member leaves (#1104).

Real Postgres. The search side is a recording fake client: what matters is WHICH documents the
restamp selects (the moved files, never a held file the departing member keeps).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError

from app.auth.audit_events import AuditEventType
from app.core.exceptions import ConflictError
from app.models.collection import Collection
from app.models.media import MediaFile
from app.models.speaker import SpeakerProfile
from app.models.tag import Tag
from app.services import audit_outbox
from app.services import org_content_transfer as oct_
from app.services import user_deletion_service
from app.services.org_content_transfer import restamp_documents


class FakeOpenSearch:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.indices = self
        self.fail_first = fail_first

    def exists(self, index: str) -> bool:
        del index
        return True

    def update_by_query(self, index, body, **_kw):
        if self.fail_first:
            self.fail_first -= 1
            raise OSConnectionError("N/A", "down", Exception("down"))
        self.calls.append((index, body["query"]))
        return {"updated": 1, "version_conflicts": 0, "failures": []}


@pytest.fixture()
def dispatched(monkeypatch) -> list[dict]:
    """Restamp messages the transfer queued (the task itself is tested in its own cases)."""
    sent: list[dict] = []
    monkeypatch.setattr(
        "app.core.celery.celery_app.send_task",
        lambda name, **kw: sent.append({"name": name, **kw}),
    )
    return sent


@pytest.fixture()
def search(dispatched: list[dict]) -> list[dict]:
    return dispatched


@pytest.fixture()
def staged(monkeypatch) -> list[dict]:
    written: list[dict] = []
    real = audit_outbox.stage

    def _stage(db, **kw):
        written.append(kw)
        return real(db, **kw)

    monkeypatch.setattr(audit_outbox, "stage", _stage)
    return written


def _world(gov):
    """``gov`` already joins admin (org:admin), alice and bob (members)."""
    return gov


def _make(gov, user, role: str, org=None) -> None:
    """Set a member's role, or add the membership when there is none yet."""
    from app.models.organization import OrganizationMembership

    row = (
        gov.db.query(OrganizationMembership)
        .filter_by(organization_id=(org or gov.org).id, user_id=user.id)
        .first()
    )
    if row is None:
        gov.join(user, org=org, role=role)
    else:
        row.role = role
        gov.db.commit()


def _owner_of(db, model, row_id) -> int:
    db.expire_all()
    return int(db.get(model, row_id).user_id)


def test_org_content_moves_and_personal_and_foreign_rows_stay(gov, search, staged):
    _world(gov)
    db = gov.db
    org_file = gov.file(gov.alice)
    trashed = gov.file(
        gov.alice,
        deleted_at=datetime.now(UTC),
        purge_after=datetime.now(UTC),
        trash_origin="user",
    )
    personal = gov.file(gov.alice, org=False)
    foreign = gov.file(gov.alice, org=gov.other_org)
    bobs = gov.file(gov.bob)
    coll = gov.collection(gov.alice)
    tag = Tag(name="t-org", user_id=gov.alice.id, organization_id=gov.org.id)
    db.add(tag)
    db.commit()

    result = oct_.transfer_org_content(
        db, organization_id=gov.org.id, from_user_id=gov.alice.id, actor=gov.admin
    )

    assert result.to_user_id == gov.admin.id
    assert result.counts["files"] == 2
    assert result.counts["collections"] == 1
    assert result.counts["tags"] == 1
    for f in (org_file, trashed):
        assert _owner_of(db, MediaFile, f.id) == gov.admin.id
    assert _owner_of(db, MediaFile, personal.id) == gov.alice.id
    assert _owner_of(db, MediaFile, foreign.id) == gov.alice.id
    assert _owner_of(db, MediaFile, bobs.id) == gov.bob.id
    assert _owner_of(db, Collection, coll.id) == gov.admin.id
    assert _owner_of(db, Tag, tag.id) == gov.admin.id
    event = [e for e in staged if e["event_type"] is AuditEventType.ORG_CONTENT_TRANSFER]
    assert len(event) == 1 and event[0]["organization_id"] == gov.org.id
    assert "filename" not in str(event[0]["details"])


def test_a_held_file_is_not_re_owned_and_is_reported(gov, search):
    _world(gov)
    held = gov.file(gov.alice, legal_hold=True)
    free = gov.file(gov.alice)

    result = oct_.transfer_org_content(
        gov.db, organization_id=gov.org.id, from_user_id=gov.alice.id
    )

    assert result.skipped_held == 1
    assert _owner_of(gov.db, MediaFile, held.id) == gov.alice.id
    assert _owner_of(gov.db, MediaFile, free.id) == gov.admin.id
    assert [m["kwargs"]["file_ids"] for m in search] == [[free.id]]


def test_restamp_selects_only_docs_not_already_on_the_new_owner(gov):
    fake = FakeOpenSearch()
    restamp_documents(
        to_user_id=gov.admin.id, file_ids=[7], profile_ids=[3], cluster_uuids=["c"], client=fake
    )
    assert fake.calls
    assert all(
        q["bool"]["must_not"] == [{"term": {"user_id": gov.admin.id}}] for _i, q in fake.calls
    )
    assert any({"terms": {"file_id": [7]}} in q["bool"]["filter"] for _i, q in fake.calls)


def test_the_transfer_queues_one_restamp_with_the_moved_ids(gov, search):
    f = gov.file(gov.alice)
    oct_.transfer_org_content(gov.db, organization_id=gov.org.id, from_user_id=gov.alice.id)
    assert [(m["name"], m["kwargs"]["file_ids"], m["kwargs"]["to_user_id"]) for m in search] == [
        ("org_content_transfer.restamp", [f.id], gov.admin.id)
    ]


def test_the_restamp_task_retries_a_failed_first_attempt_and_then_succeeds(monkeypatch, dispatched):
    from app.tasks.org_content_transfer_task import restamp

    fake = FakeOpenSearch(fail_first=1)
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)
    outcome = restamp.apply(
        kwargs={"to_user_id": 5, "file_ids": [7], "profile_ids": [], "cluster_uuids": []}
    )
    assert outcome.successful(), outcome.traceback
    assert fake.fail_first == 0 and fake.calls
    assert [m["name"] for m in dispatched] == ["update_file_access_index"]


def test_the_restamp_task_declares_its_retry_policy():
    from app.tasks.org_content_transfer_task import restamp

    assert restamp.acks_late is True
    assert restamp.max_retries and restamp.retry_backoff
    assert issubclass(OSConnectionError, restamp.autoretry_for)


def test_a_quarantined_file_is_re_owned_and_stays_quarantined(gov, search):
    quarantined = gov.file(
        gov.alice,
        is_quarantined=True,
        quarantine_reason="takedown",
        pre_quarantine_status="completed",
    )
    oct_.transfer_org_content(gov.db, organization_id=gov.org.id, from_user_id=gov.alice.id)
    gov.db.expire_all()
    row = gov.db.get(MediaFile, quarantined.id)
    assert row.user_id == gov.admin.id
    assert row.is_quarantined is True and row.quarantine_reason == "takedown"
    assert [m["kwargs"]["file_ids"] for m in search] == [[quarantined.id]]


def test_a_name_conflict_leaves_only_the_colliding_row(gov, search):
    _world(gov)
    db = gov.db
    mine = SpeakerProfile(name="Dana", user_id=gov.admin.id, organization_id=gov.org.id)
    clash = SpeakerProfile(name="Dana", user_id=gov.alice.id, organization_id=gov.org.id)
    free = SpeakerProfile(name="Eli", user_id=gov.alice.id, organization_id=gov.org.id)
    db.add_all([mine, clash, free])
    db.commit()

    result = oct_.transfer_org_content(db, organization_id=gov.org.id, from_user_id=gov.alice.id)

    assert result.skipped_conflict == {"speaker_profiles": 1}
    assert _owner_of(db, SpeakerProfile, clash.id) == gov.alice.id
    assert _owner_of(db, SpeakerProfile, free.id) == gov.admin.id


@pytest.mark.parametrize(
    ("target", "code"),
    [("alice", "same_user"), ("bob", "not_an_org_admin")],
)
def test_bad_targets_are_refused_and_nothing_moves(gov, search, target, code):
    _world(gov)
    f = gov.file(gov.alice)
    with pytest.raises(oct_.TransferError) as err:
        oct_.transfer_org_content(
            gov.db,
            organization_id=gov.org.id,
            from_user_id=gov.alice.id,
            to_user_id=getattr(gov, target).id,
        )
    assert err.value.code == code
    assert _owner_of(gov.db, MediaFile, f.id) == gov.alice.id


def test_no_other_admin_means_no_target(gov, search):
    _make(gov, gov.admin, "org:member")
    _make(gov, gov.alice, "org:admin")
    gov.file(gov.alice)
    with pytest.raises(oct_.TransferError) as err:
        oct_.transfer_org_content(gov.db, organization_id=gov.org.id, from_user_id=gov.alice.id)
    assert err.value.code == "no_transfer_target"


def test_the_owner_is_preferred_as_default_target(gov):
    _make(gov, gov.bob, "org:admin")
    gov.org.owner_user_id = gov.bob.id
    gov.db.commit()
    assert oct_.default_target(gov.db, gov.org.id, from_user_id=gov.alice.id) == gov.bob.id


# --- account deletion hook ---------------------------------------------------------------------


@pytest.fixture()
def _no_external(monkeypatch):
    monkeypatch.setattr("app.services.account_purge.purge_account_external_copies", lambda plan: [])
    monkeypatch.setattr(
        user_deletion_service.token_service, "stamp_user_revocation_epoch", lambda uuid: None
    )


def test_deleting_an_account_keeps_its_org_files_with_the_admin(gov, search, _no_external):
    _world(gov)
    org_file = gov.file(gov.alice)
    personal = gov.file(gov.alice, org=False)
    org_file_id, personal_id = org_file.id, personal.id

    user_deletion_service.delete_user_account(gov.db, gov.alice)

    gov.db.expire_all()
    assert gov.db.get(MediaFile, personal_id) is None
    survivor = gov.db.get(MediaFile, org_file_id)
    assert survivor is not None and survivor.user_id == gov.admin.id


def test_deleting_the_last_admins_account_is_refused_while_content_remains(
    gov, search, _no_external
):
    _make(gov, gov.admin, "org:member")
    _make(gov, gov.alice, "org:admin")
    f = gov.file(gov.alice)
    with pytest.raises(ConflictError) as refused:
        user_deletion_service.delete_user_account(gov.db, gov.alice)
    assert isinstance(refused.value.detail, dict)
    assert refused.value.detail["code"] == "no_transfer_target"
    assert "Add another administrator first" in refused.value.detail["message"]
    assert _owner_of(gov.db, MediaFile, f.id) == gov.alice.id


def _pipeline_speaker(db, file: MediaFile, label: str = "SPEAKER_00"):
    """A speaker as ``speaker_processor`` creates it: ``organization_id`` left NULL, with one
    diarized segment pointing at it."""
    import uuid as uuid_pkg

    from app.models.media import TranscriptSegment
    from app.models.speaker import Speaker

    speaker = Speaker(
        name=label,
        uuid=uuid_pkg.uuid4(),
        user_id=file.user_id,
        media_file_id=file.id,
        verified=False,
    )
    db.add(speaker)
    db.flush()
    segment = TranscriptSegment(
        media_file_id=file.id, speaker_id=speaker.id, start_time=0.0, end_time=1.0, text="hi"
    )
    db.add(segment)
    db.commit()
    assert speaker.organization_id is None
    return speaker, segment


def test_deleting_an_account_keeps_the_handed_over_files_speakers_and_voiceprints(
    gov, search, monkeypatch
):
    from app.models.media import Task
    from app.models.media import TranscriptSegment
    from app.models.speaker import Speaker
    from app.models.topic import TopicSuggestion

    _world(gov)
    db = gov.db
    org_file = gov.file(gov.alice)
    speaker, segment = _pipeline_speaker(db, org_file)
    topic = TopicSuggestion(media_file_id=org_file.id, user_id=gov.alice.id, suggested_tags=[])
    task = Task(
        id=f"task-transfer-{org_file.uuid}",
        user_id=gov.alice.id,
        media_file_id=org_file.id,
        task_type="transcription",
        status="completed",
    )
    db.add_all([topic, task])
    db.commit()
    ids = (org_file.id, speaker.id, segment.id, topic.id, task.id)
    speaker_uuid = str(speaker.uuid)
    plans: list = []

    def record_plan(plan) -> list:
        plans.append(plan)
        return []

    monkeypatch.setattr("app.services.account_purge.purge_account_external_copies", record_plan)
    monkeypatch.setattr(
        user_deletion_service.token_service, "stamp_user_revocation_epoch", lambda uuid: None
    )

    user_deletion_service.delete_user_account(db, gov.alice)

    db.expire_all()
    file_id, speaker_id, segment_id, topic_id, task_id = ids
    assert db.get(MediaFile, file_id).user_id == gov.admin.id
    kept = db.get(Speaker, speaker_id)
    assert kept is not None and kept.user_id == gov.admin.id
    assert db.get(TranscriptSegment, segment_id).speaker_id == speaker_id
    assert db.get(TopicSuggestion, topic_id).user_id == gov.admin.id
    assert db.get(Task, task_id).user_id == gov.admin.id
    # The voiceprint document is erased only through the plan; the handed-over speaker is not
    # in it, and the restamp re-points the file's speaker documents at the admin.
    assert len(plans) == 1
    assert speaker_uuid not in plans[0].speaker_uuids
    assert file_id not in [f["file_id"] for f in plans[0].files]
    restamped = [m["kwargs"] for m in search if m["name"] == oct_.RESTAMP_TASK]
    assert restamped and file_id in restamped[0]["file_ids"]


def test_a_held_files_speakers_stay_with_their_owner_even_when_tenant_stamped(gov, search):
    from app.models.speaker import Speaker

    _world(gov)
    held = gov.file(gov.alice, legal_hold=True)
    speaker, _segment = _pipeline_speaker(gov.db, held)
    speaker.organization_id = gov.org.id  # a stamp must not pull it off its frozen file
    gov.db.commit()

    result = oct_.transfer_org_content(
        gov.db, organization_id=gov.org.id, from_user_id=gov.alice.id, actor=gov.admin
    )

    assert result.counts["speakers"] == 0
    assert _owner_of(gov.db, Speaker, speaker.id) == gov.alice.id
