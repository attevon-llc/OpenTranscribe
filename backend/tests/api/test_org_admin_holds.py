"""``/api/org-admin/holds`` over HTTP: an organization's legal holds (#1276).

The routes hold the power to make a file undeletable, so the cases that matter are the
boundaries: who may call (an organization administrator, behind a capability no superuser can
bypass), what an administrator can reach (their own organization's files, collections and holds,
and nothing else, with one 404 for "not yours" and "missing"), and what a hold does to the things
around it (the file page, the collection routes, the audit trail, the reason).
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.content_hold import ContentHold
from app.services.hold_guard import hold_state

HOLDS = "/api/org-admin/holds"
REASON = "Litigation matter 2026-14 (privileged)"
MATTER = "CASE-2026-0042"


@pytest.fixture()
def holds_on():
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.legal_hold": True}
    )
    yield
    reset_capability_resolver()


@pytest.fixture()
def holds_off():
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.legal_hold": False}
    )
    yield
    reset_capability_resolver()


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


@pytest.fixture()
def freed(monkeypatch) -> list:
    """Files the erasure sweep was told are no longer held."""
    notified: list = []
    monkeypatch.setattr(
        "app.tasks.erasure_reconciliation.notify_hold_released", lambda f: notified.append(f.uuid)
    )
    return notified


@pytest.fixture(autouse=True)
def _no_storage(monkeypatch):
    monkeypatch.setattr("app.services.minio_service.set_object_legal_hold", lambda *a, **k: True)


def _place(client, gov, *files, scope="file", **extra):
    body = {"scope": scope, "reason": REASON, "matter_ref": MATTER, **extra}
    if scope == "file":
        body["file_uuids"] = [str(f.uuid) for f in files]
    with gov.acting_as(gov.admin):
        return client.post(HOLDS, json=body)


# --------------------------------------------------------------------------- #
# Who may call
# --------------------------------------------------------------------------- #


def test_every_route_is_a_404_while_the_capability_is_off(client, gov, holds_off):
    f = gov.file(gov.alice)
    with gov.acting_as(gov.admin):
        assert client.get(HOLDS).status_code == status.HTTP_404_NOT_FOUND
        assert (
            client.post(
                HOLDS, json={"scope": "file", "file_uuids": [str(f.uuid)], "reason": REASON}
            ).status_code
            == status.HTTP_404_NOT_FOUND
        )
    assert not hold_state(gov.db, f.id).held


def test_a_plain_member_cannot_list_or_place(client, gov, holds_on):
    f = gov.file(gov.alice)
    with gov.acting_as(gov.alice):
        assert client.get(HOLDS).status_code == status.HTTP_403_FORBIDDEN
        placed = client.post(
            HOLDS, json={"scope": "file", "file_uuids": [str(f.uuid)], "reason": REASON}
        )
    assert placed.status_code == status.HTTP_403_FORBIDDEN
    assert not hold_state(gov.db, f.id).held


def test_a_superuser_is_not_a_way_around_the_capability(client, gov, holds_off, db_session):
    """No platform bypass: with the key off a super_admin gets the same 404 as anyone."""
    gov.admin.role, gov.admin.is_superuser = "super_admin", True
    db_session.commit()
    with gov.acting_as(gov.admin):
        assert client.get(HOLDS).status_code == status.HTTP_404_NOT_FOUND


def test_platform_staff_without_an_organization_role_cannot_release_a_hold(
    client, gov, holds_on, db_session
):
    f = gov.file(gov.alice)
    hold_uuid = _place(client, gov, f).json()["uuid"]
    staff = gov.user("staff")
    staff.role, staff.is_superuser = "super_admin", True
    db_session.commit()
    with gov.acting_as(staff, org=None):
        response = client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert hold_state(gov.db, f.id).held


# --------------------------------------------------------------------------- #
# Placing, reading, releasing
# --------------------------------------------------------------------------- #


def test_placing_a_file_hold_holds_the_file_and_shows_the_reason_to_admins_only(
    client, gov, holds_on
):
    f = gov.file(gov.alice, title="Retail review")
    response = _place(client, gov, f)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    body = response.json()
    assert body["scope"] == "file" and body["active"] is True and body["file_count"] == 1
    assert body["reason"] == REASON and body["matter_ref"] == MATTER
    assert body["placed_by_display"] == gov.admin.full_name
    assert hold_state(gov.db, f.id).org_hold_uuids == (body["uuid"],)

    with gov.acting_as(gov.admin):
        detail = client.get(f"{HOLDS}/{body['uuid']}")
    assert [x["uuid"] for x in detail.json()["files"]] == [str(f.uuid)]


def test_the_file_page_says_held_to_everyone_and_gives_the_ids_only_to_an_org_admin(
    client, gov, holds_on
):
    """Alice's file is held; Alice (a member) learns it is held, the admin (reading a file of
    their own, since an admin has no read route into members' files) also gets the hold ids."""
    mine = gov.file(gov.alice)
    admins = gov.file(gov.admin)
    mine_hold = _place(client, gov, mine).json()["uuid"]
    admins_hold = _place(client, gov, admins).json()["uuid"]

    with gov.acting_as(gov.alice):
        member = client.get(f"/api/files/{mine.uuid}").json()["hold"]
    with gov.acting_as(gov.admin):
        admin = client.get(f"/api/files/{admins.uuid}").json()["hold"]

    assert member == {"held": True, "held_by": ["organization"], "hold_uuids": []}
    assert admin == {"held": True, "held_by": ["organization"], "hold_uuids": [admins_hold]}
    assert mine_hold != admins_hold
    # The control: an unheld file is not marked.
    free = gov.file(gov.alice)
    with gov.acting_as(gov.alice):
        assert client.get(f"/api/files/{free.uuid}").json()["hold"]["held"] is False


@pytest.mark.parametrize("reason", ["", "too short", "x" * 2001])
def test_a_reason_outside_its_bounds_is_a_422_and_holds_nothing(client, gov, holds_on, reason):
    f = gov.file(gov.alice)
    with gov.acting_as(gov.admin):
        response = client.post(
            HOLDS, json={"scope": "file", "file_uuids": [str(f.uuid)], "reason": reason}
        )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert not hold_state(gov.db, f.id).held


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "file"},
        {"scope": "file", "file_uuids": [], "collection_uuid": str(uuid_pkg.uuid4())},
        {"scope": "collection"},
        {"scope": "collection", "collection_uuid": str(uuid_pkg.uuid4()), "file_uuids": ["x"]},
        {"scope": "everything", "file_uuids": []},
    ],
)
def test_a_scope_must_name_exactly_its_own_target(client, gov, holds_on, body):
    with gov.acting_as(gov.admin):
        response = client.post(HOLDS, json={"reason": REASON, **body})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_another_organizations_personal_and_missing_files_are_one_404(client, gov, holds_on):
    foreign = gov.file(gov.outsider, org=gov.other_org)
    personal = gov.file(gov.alice, org=None)
    mine = gov.file(gov.alice)
    answers = []
    for target in (foreign, personal, None):
        uuid = str(target.uuid) if target is not None else str(uuid_pkg.uuid4())
        with gov.acting_as(gov.admin):
            answers.append(
                client.post(HOLDS, json={"scope": "file", "file_uuids": [uuid], "reason": REASON})
            )
    assert {r.status_code for r in answers} == {status.HTTP_404_NOT_FOUND}
    assert len({r.text for r in answers}) == 1, "foreign, personal and missing must look alike"
    # One foreign file poisons the whole request: nothing is held, not even the file that is ours.
    with gov.acting_as(gov.admin):
        mixed = client.post(
            HOLDS,
            json={
                "scope": "file",
                "file_uuids": [str(mine.uuid), str(foreign.uuid)],
                "reason": REASON,
            },
        )
    assert mixed.status_code == status.HTTP_404_NOT_FOUND
    for f in (foreign, personal, mine):
        assert not hold_state(gov.db, f.id).held


def test_another_organizations_hold_is_a_404_to_read_and_release(client, gov, holds_on):
    other_admin = gov.user("other_admin")
    gov.join(other_admin, gov.other_org, role="org:admin")
    foreign_file = gov.file(other_admin, org=gov.other_org)
    with gov.acting_as(other_admin, org=gov.other_org, role="org:admin"):
        theirs = client.post(
            HOLDS, json={"scope": "file", "file_uuids": [str(foreign_file.uuid)], "reason": REASON}
        ).json()["uuid"]

    with gov.acting_as(gov.admin):
        listed = client.get(HOLDS).json()
        read = client.get(f"{HOLDS}/{theirs}")
        released = client.post(f"{HOLDS}/{theirs}/release", json={"reason": REASON})
    assert listed["total"] == 0 and listed["holds"] == []
    assert read.status_code == status.HTTP_404_NOT_FOUND
    assert released.status_code == status.HTTP_404_NOT_FOUND
    assert hold_state(gov.db, foreign_file.id).held, "the foreign hold is untouched"


def test_release_frees_the_file_notifies_the_sweep_and_keeps_the_record(
    client, gov, holds_on, freed
):
    f = gov.file(gov.alice)
    hold_uuid = _place(client, gov, f).json()["uuid"]
    with gov.acting_as(gov.admin):
        released = client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
        again = client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
        active = client.get(HOLDS).json()
        history = client.get(HOLDS, params={"active_only": "false"}).json()

    assert released.status_code == status.HTTP_200_OK, released.text
    assert released.json()["active"] is False and released.json()["release_reason"] == REASON
    assert released.json()["released_by_display"] == gov.admin.full_name
    assert again.status_code == status.HTTP_409_CONFLICT
    assert not hold_state(gov.db, f.id).held
    assert freed == [f.uuid]
    assert active["total"] == 0
    assert [h["uuid"] for h in history["holds"]] == [hold_uuid] and history["total"] == 1


def test_releasing_one_of_two_holds_does_not_free_the_file_or_notify(client, gov, holds_on, freed):
    f = gov.file(gov.alice)
    first = _place(client, gov, f).json()["uuid"]
    second = _place(client, gov, f).json()["uuid"]
    with gov.acting_as(gov.admin):
        assert client.post(f"{HOLDS}/{first}/release", json={"reason": REASON}).status_code == 200
    assert hold_state(gov.db, f.id).org_hold_uuids == (second,)
    assert freed == []


# --------------------------------------------------------------------------- #
# Collection holds
# --------------------------------------------------------------------------- #


def test_a_collection_hold_covers_members_and_files_added_later_and_blocks_leaving(
    client, gov, holds_on
):
    collection = gov.collection(gov.alice, name="Matter 14")
    early, late = gov.file(gov.alice), gov.file(gov.alice)
    gov.put(collection, early)
    placed = _place(client, gov, scope="collection", collection_uuid=str(collection.uuid))
    assert placed.status_code == status.HTTP_201_CREATED, placed.text
    hold_uuid = placed.json()["uuid"]
    assert placed.json()["collection_name"] == "Matter 14" and placed.json()["file_count"] == 1
    assert hold_state(gov.db, early.id).held

    base = f"/api/collections/{collection.uuid}"
    with gov.acting_as(gov.alice):
        added = client.post(f"{base}/media", json={"media_file_ids": [str(late.uuid)]})
        removal = client.request(
            "DELETE", f"{base}/media", json={"media_file_ids": [str(early.uuid)]}
        )
        deletion = client.delete(base)
    assert added.status_code == status.HTTP_200_OK, added.text
    assert hold_state(gov.db, late.id).held, "a file added later is held at once"
    for refused in (removal, deletion):
        assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
        assert refused.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"
        assert refused.json()["detail"]["held_by"] == ["organization"]
        assert "holds" not in refused.json()["detail"], "a member is not given the hold ids"
    with gov.acting_as(gov.admin):
        admin_refusal = client.delete(base)
    assert admin_refusal.json()["detail"]["holds"] == [hold_uuid]
    assert not gov.db.query(ContentHold).filter_by(uuid=hold_uuid).one().released_at

    with gov.acting_as(gov.alice):
        header = client.get(base).json()["hold"]
    assert header == {"held": True, "held_by": ["organization"], "hold_uuids": []}

    with gov.acting_as(gov.admin):
        client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
    with gov.acting_as(gov.alice):
        assert client.get(base).json()["hold"]["held"] is False
        freed_removal = client.request(
            "DELETE", f"{base}/media", json={"media_file_ids": [str(early.uuid)]}
        )
        assert freed_removal.status_code == status.HTTP_200_OK
        assert client.delete(base).status_code == status.HTTP_200_OK


def test_an_empty_collection_can_be_held_and_then_cannot_be_deleted(client, gov, holds_on):
    collection = gov.collection(gov.alice)
    assert (
        _place(client, gov, scope="collection", collection_uuid=str(collection.uuid)).status_code
        == status.HTTP_201_CREATED
    )
    with gov.acting_as(gov.alice):
        assert client.delete(f"/api/collections/{collection.uuid}").status_code == 409


def test_the_collection_list_marks_a_held_collection(client, gov, holds_on):
    held, free = gov.collection(gov.alice, name="held one"), gov.collection(gov.alice, name="free")
    _place(client, gov, scope="collection", collection_uuid=str(held.uuid))
    with gov.acting_as(gov.alice):
        rows = {c["name"]: c["hold"]["held"] for c in client.get("/api/collections").json()}
    assert rows["held one"] is True and rows["free"] is False


def test_a_restricted_collection_cannot_be_held_by_someone_who_cannot_see_it(client, gov, holds_on):
    """An org admin sees every collection of their org (rank 3), so this holds for them;
    what the route must not do is accept a collection of another organization."""
    foreign = gov.collection(gov.outsider, org=gov.other_org)
    with gov.acting_as(gov.admin):
        response = client.post(
            HOLDS,
            json={"scope": "collection", "collection_uuid": str(foreign.uuid), "reason": REASON},
        )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert gov.db.query(ContentHold).count() == 0


# --------------------------------------------------------------------------- #
# Audit and the reason
# --------------------------------------------------------------------------- #


def test_placing_and_releasing_are_audited_without_the_reason_or_matter(
    client, gov, holds_on, events
):
    f = gov.file(gov.alice)
    hold_uuid = _place(client, gov, f).json()["uuid"]
    with gov.acting_as(gov.admin):
        client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})

    placed = [e for e in events if e["event_type"] is AuditEventType.CONTENT_HOLD_PLACE]
    released = [e for e in events if e["event_type"] is AuditEventType.CONTENT_HOLD_RELEASE]
    assert len(placed) == 1 and len(released) == 1
    for event in placed + released:
        assert event["organization_id"] == gov.org.id
        assert event["tenant_scope"] == "org"
        assert event["resource_type"] == "content_hold"
        assert event["resource_uuid"] == hold_uuid
        assert event["user_id"] == gov.admin.id
        assert event["details"] == {"scope": "file", "file_count": 1}
    blob = json.dumps(events, default=str)
    assert REASON not in blob and MATTER not in blob


def test_a_refused_delete_is_audited_as_a_refusal(client, gov, holds_on, events):
    f = gov.file(gov.alice)
    _place(client, gov, f)
    with gov.acting_as(gov.alice):
        assert client.delete(f"/api/files/{f.uuid}").status_code == status.HTTP_409_CONFLICT

    refused = [e for e in events if e["event_type"] is AuditEventType.CONTENT_HOLD_REFUSED]
    assert len(refused) == 1
    assert refused[0]["resource_uuid"] == str(f.uuid)
    assert refused[0]["user_id"] == gov.alice.id
    assert refused[0]["details"] == {"action": "delete", "held_by": ["organization"]}
