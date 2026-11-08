"""Organization content moves to an administrator when a member leaves (#1104).

Real Postgres. The search side is a recording fake client: what matters is WHICH documents the
restamp selects (the moved files, never a held file the departing member keeps).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
from fastapi import status

from app.auth.audit_events import AuditEventType
from app.core.exceptions import ConflictError
from app.models.collection import Collection
from app.models.media import MediaFile
from app.models.speaker import SpeakerProfile
from app.models.tag import Tag
from app.services import audit_outbox
from app.services import org_content_transfer as oct_
from app.services import user_deletion_service

pytestmark = pytest.mark.usefixtures("organizations_capability_on")


class FakeOpenSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.indices = self

    def exists(self, index: str) -> bool:
        del index
        return True

    def update_by_query(self, index, body, **_kw):
        self.calls.append((index, body["query"]))
        return {"updated": 1, "version_conflicts": 0, "failures": []}


@pytest.fixture()
def search(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: fake)
    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)
    return fake


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
    selected = [c for _i, q in search.calls for c in q["bool"]["filter"] if "terms" in c]
    ids = {i for c in selected for v in c["terms"].values() for i in v if isinstance(i, int)}
    assert free.id in ids and held.id not in ids


def test_restamp_selects_only_docs_not_already_on_the_new_owner(gov, search):
    _world(gov)
    f = gov.file(gov.alice)
    oct_.transfer_org_content(gov.db, organization_id=gov.org.id, from_user_id=gov.alice.id)
    assert search.calls
    assert all(
        q["bool"]["must_not"] == [{"term": {"user_id": gov.admin.id}}] for _i, q in search.calls
    )
    assert any({"terms": {"file_id": [f.id]}} in q["bool"]["filter"] for _i, q in search.calls)


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


# --- API ---------------------------------------------------------------------------------------


def _url(user) -> str:
    return f"/api/org-admin/members/{user.uuid}/transfer-content"


def test_api_admin_transfers_and_plain_member_cannot(client, gov, search):
    _world(gov)
    f = gov.file(gov.alice)
    with gov.acting_as(gov.alice):
        assert client.post(_url(gov.alice), json={}).status_code == status.HTTP_403_FORBIDDEN
    assert _owner_of(gov.db, MediaFile, f.id) == gov.alice.id
    with gov.acting_as(gov.admin):
        response = client.post(_url(gov.alice), json={})
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["counts"]["files"] == 1 and body["to_user_uuid"] == str(gov.admin.uuid)
    assert _owner_of(gov.db, MediaFile, f.id) == gov.admin.id


def test_api_cannot_reach_another_organizations_member(client, gov, search):
    _world(gov)
    _make(gov, gov.outsider, "org:member", org=gov.other_org)
    f = gov.file(gov.outsider, org=gov.other_org)
    with gov.acting_as(gov.admin):
        response = client.post(_url(gov.outsider), json={})
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _owner_of(gov.db, MediaFile, f.id) == gov.outsider.id


def test_api_target_must_be_an_admin(client, gov, search):
    _world(gov)
    with gov.acting_as(gov.admin):
        response = client.post(_url(gov.alice), json={"to_user_uuid": str(gov.bob.uuid)})
    assert response.status_code == status.HTTP_409_CONFLICT
    assert "not_an_org_admin" in response.text


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
    with pytest.raises(ConflictError):
        user_deletion_service.delete_user_account(gov.db, gov.alice)
    assert _owner_of(gov.db, MediaFile, f.id) == gov.alice.id
