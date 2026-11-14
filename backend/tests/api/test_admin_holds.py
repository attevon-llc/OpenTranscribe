"""``/api/admin/holds`` over HTTP: the instance administrator's legal holds (v467).

A platform hold is the community edition's legal hold: an instance administrator (``admin`` or
``super_admin``) holds files or a collection of ANY tenant without taking them down. The cases
that matter are the boundaries: who may call, what a hold reaches (any tenant's file or
collection, with one 404 for "missing"), that an organization's hold is out of reach here and a
platform hold out of reach there, that nothing is quarantined, and that the reason never leaves
the administrator's record.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from fastapi import status
from sqlalchemy import select

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.models.audit_outbox import AuditOutbox
from app.models.media import MediaFile
from app.services import audit_outbox
from app.services import content_hold_service as holds
from app.services.hold_guard import hold_state

HOLDS = "/api/admin/holds"
REASON = "Preservation demand 2026-77 (privileged)"
MATTER = "CASE-2026-0077"


@pytest.fixture(autouse=True)
def _no_storage(monkeypatch):
    monkeypatch.setattr("app.services.minio_service.set_object_legal_hold", lambda *a, **k: True)


@pytest.fixture()
def operator(gov):
    user = gov.user("operator")
    user.role = "admin"
    gov.db.commit()
    return user


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    """Logged events (refusals, metadata reads) and staged ones (place/release: the outbox)."""
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda *a, **kw: written.append({"args": a, **kw}))
    real_stage = audit_outbox.stage

    def _stage(db, **kw):
        written.append(kw)
        return real_stage(db, **kw)

    monkeypatch.setattr(audit_outbox, "stage", _stage)
    return written


@pytest.fixture()
def freed(monkeypatch) -> list:
    notified: list = []
    monkeypatch.setattr(
        "app.tasks.erasure_reconciliation.notify_hold_released", lambda f: notified.append(f.uuid)
    )
    return notified


def _place(client, gov, who, *files, scope="file", **extra):
    body = {"scope": scope, "reason": REASON, "matter_ref": MATTER, **extra}
    if files:
        body["file_uuids"] = [str(f.uuid) for f in files]
    with gov.acting_as(who, org=None):
        return client.post(HOLDS, json=body)


# --------------------------------------------------------------------------- #
# Who may call
# --------------------------------------------------------------------------- #


def test_a_regular_user_can_neither_read_nor_place_nor_release(client, gov, operator):
    f = gov.file(gov.alice)
    hold_uuid = _place(client, gov, operator, f).json()["uuid"]
    for who in (gov.alice, gov.admin):  # an organization admin is not an instance admin
        with gov.acting_as(who):
            answers = [
                client.get(HOLDS),
                client.get(f"{HOLDS}/{hold_uuid}"),
                client.post(
                    HOLDS, json={"scope": "file", "file_uuids": [str(f.uuid)], "reason": REASON}
                ),
                client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON}),
            ]
        assert {r.status_code for r in answers} == {status.HTTP_403_FORBIDDEN}
        assert all(REASON not in r.text for r in answers)
    assert hold_state(gov.db, f.id).platform_hold_uuids == (hold_uuid,)
    assert [str(h.uuid) for h in gov.new_holds()] == [hold_uuid]


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_admins_of_either_rank_place_and_release(client, gov, role, freed):
    staff = gov.user(role)
    staff.role, staff.is_superuser = role, role == "super_admin"
    gov.db.commit()
    f = gov.file(gov.alice)
    placed = _place(client, gov, staff, f)
    assert placed.status_code == status.HTTP_201_CREATED, placed.text
    with gov.acting_as(staff, org=None):
        released = client.post(f"{HOLDS}/{placed.json()['uuid']}/release", json={"reason": REASON})
    assert released.status_code == status.HTTP_200_OK, released.text
    assert not hold_state(gov.db, f.id).held and freed == [f.uuid]


def test_the_routes_need_no_capability_even_with_every_organization_key_off(client, gov, operator):
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": False, "org.legal_hold": False}
    )
    try:
        f = gov.file(gov.alice)
        assert _place(client, gov, operator, f).status_code == status.HTTP_201_CREATED
    finally:
        reset_capability_resolver()
    assert hold_state(gov.db, f.id).held


# --------------------------------------------------------------------------- #
# Placing, reading, releasing
# --------------------------------------------------------------------------- #


def test_any_tenants_files_can_be_held_and_nothing_is_quarantined(client, gov, operator):
    org_file = gov.file(gov.alice, title="Retail review")
    foreign = gov.file(gov.outsider, org=gov.other_org)
    personal = gov.file(gov.bob, org=None)
    response = _place(client, gov, operator, org_file, foreign, personal)

    assert response.status_code == status.HTTP_201_CREATED, response.text
    body = response.json()
    assert body["authority"] == "platform" and body["organization_uuid"] is None
    assert body["scope"] == "file" and body["active"] is True and body["file_count"] == 3
    assert body["reason"] == REASON and body["matter_ref"] == MATTER
    assert body["placed_by_display"] == operator.full_name
    for f in (org_file, foreign, personal):
        state = hold_state(gov.db, f.id)
        assert state.held_by == ["platform"] and state.platform_hold_uuids == (body["uuid"],)
        gov.db.refresh(f)
        assert f.is_quarantined is False and f.legal_hold is False, "a hold is not a takedown"

    with gov.acting_as(operator, org=None):
        detail = client.get(f"{HOLDS}/{body['uuid']}").json()
    assert sorted(x["uuid"] for x in detail["files"]) == sorted(
        str(f.uuid) for f in (org_file, foreign, personal)
    )
    # The owner still opens the file and is told who holds it, never why.
    with gov.acting_as(gov.alice):
        page = client.get(f"/api/files/{org_file.uuid}")
    assert page.status_code == status.HTTP_200_OK
    assert page.json()["hold"] == {"held": True, "held_by": ["platform"], "hold_uuids": []}
    assert REASON not in page.text and MATTER not in page.text


def test_an_instance_admin_reading_a_held_file_gets_the_platform_hold_uuid(client, gov, operator):
    mine = gov.file(operator, org=None)
    hold_uuid = _place(client, gov, operator, mine).json()["uuid"]
    with gov.acting_as(operator, org=None):
        assert client.get(f"/api/files/{mine.uuid}").json()["hold"]["hold_uuids"] == [hold_uuid]


def test_a_missing_file_is_a_404_and_one_poisons_the_whole_request(client, gov, operator):
    real = gov.file(gov.alice)
    missing = _place(client, gov, operator, scope="file", file_uuids=[str(uuid_pkg.uuid4())])
    mixed = _place(
        client, gov, operator, scope="file", file_uuids=[str(real.uuid), str(uuid_pkg.uuid4())]
    )
    assert missing.status_code == mixed.status_code == status.HTTP_404_NOT_FOUND
    assert missing.text == mixed.text
    assert not hold_state(gov.db, real.id).held
    assert gov.new_holds() == []


@pytest.mark.parametrize("reason", ["", "too short", "x" * 2001])
def test_a_reason_outside_its_bounds_is_a_422_and_holds_nothing(client, gov, operator, reason):
    f = gov.file(gov.alice)
    with gov.acting_as(operator, org=None):
        response = client.post(
            HOLDS, json={"scope": "file", "file_uuids": [str(f.uuid)], "reason": reason}
        )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert not hold_state(gov.db, f.id).held


def test_release_keeps_the_record_and_a_second_release_is_a_409(client, gov, operator, freed):
    f = gov.file(gov.alice)
    hold_uuid = _place(client, gov, operator, f).json()["uuid"]
    with gov.acting_as(operator, org=None):
        released = client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
        again = client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
        active = gov.own_platform_listing(client.get(HOLDS).json(), active_only=True)
        history = gov.own_platform_listing(
            client.get(HOLDS, params={"active_only": "false"}).json(), active_only=False
        )
    assert released.status_code == status.HTTP_200_OK, released.text
    assert released.json()["active"] is False and released.json()["release_reason"] == REASON
    assert released.json()["released_by_display"] == operator.full_name
    assert again.status_code == status.HTTP_409_CONFLICT
    assert not hold_state(gov.db, f.id).held and freed == [f.uuid]
    assert active == {"holds": [], "total": 0}
    assert [h["uuid"] for h in history["holds"]] == [hold_uuid] and history["total"] == 1


def test_a_file_an_organization_also_holds_stays_held_after_the_platform_release(
    client, gov, operator, freed
):
    f = gov.file(gov.alice)
    hold_uuid = _place(client, gov, operator, f).json()["uuid"]
    holds.place_file_hold(gov.db, org_id=gov.org.id, actor=gov.admin, files=[f], reason=REASON)
    gov.db.commit()
    with gov.acting_as(operator, org=None):
        client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})
    assert hold_state(gov.db, f.id).held_by == ["organization"]
    assert freed == []


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


def test_a_collection_hold_covers_any_tenants_collection_and_files_added_later(
    client, gov, operator
):
    foreign = gov.collection(gov.outsider, name="Matter 77", org=gov.other_org)
    early, late = (gov.file(gov.outsider, org=gov.other_org) for _ in range(2))
    gov.put(foreign, early)
    placed = _place(client, gov, operator, scope="collection", collection_uuid=str(foreign.uuid))
    assert placed.status_code == status.HTTP_201_CREATED, placed.text
    assert placed.json()["collection_name"] == "Matter 77" and placed.json()["file_count"] == 1
    gov.put(foreign, late)
    assert hold_state(gov.db, late.id).platform_hold_uuids == (placed.json()["uuid"],)


def test_a_held_collection_tells_its_owner_platform_and_refuses_deletion(client, gov, operator):
    collection = gov.collection(gov.alice)
    gov.put(collection, gov.file(gov.alice))
    _place(client, gov, operator, scope="collection", collection_uuid=str(collection.uuid))
    with gov.acting_as(gov.alice):
        header = client.get(f"/api/collections/{collection.uuid}").json()["hold"]
        refused = client.delete(f"/api/collections/{collection.uuid}")
    assert header == {"held": True, "held_by": ["platform"], "hold_uuids": []}
    assert refused.status_code == status.HTTP_409_CONFLICT, refused.text
    assert refused.json()["detail"]["held_by"] == ["platform"]
    assert "holds" not in refused.json()["detail"]
    assert REASON not in json.dumps(header) + refused.text


def test_a_missing_collection_is_a_404(client, gov, operator):
    response = _place(
        client, gov, operator, scope="collection", collection_uuid=str(uuid_pkg.uuid4())
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert gov.new_holds() == []


# --------------------------------------------------------------------------- #
# Two authorities, kept apart
# --------------------------------------------------------------------------- #


def test_an_organizations_hold_is_a_404_here_and_never_listed(client, gov, operator):
    f = gov.file(gov.alice)
    org_hold = holds.place_file_hold(
        gov.db, org_id=gov.org.id, actor=gov.admin, files=[f], reason=REASON
    ).hold
    gov.db.commit()
    with gov.acting_as(operator, org=None):
        listed = gov.own_platform_listing(
            client.get(HOLDS, params={"active_only": "false"}).json(), active_only=False
        )
        read = client.get(f"{HOLDS}/{org_hold.uuid}")
        released = client.post(f"{HOLDS}/{org_hold.uuid}/release", json={"reason": REASON})
    assert listed == {"holds": [], "total": 0}
    assert read.status_code == released.status_code == status.HTTP_404_NOT_FOUND
    assert REASON not in read.text + released.text
    gov.db.refresh(org_hold)
    assert org_hold.active


# --------------------------------------------------------------------------- #
# Audit and the reason
# --------------------------------------------------------------------------- #


def test_place_and_release_are_audited_with_the_authority_and_without_the_reason(
    client, gov, operator, events
):
    f = gov.file(gov.outsider, org=gov.other_org)
    hold_uuid = _place(client, gov, operator, f).json()["uuid"]
    with gov.acting_as(operator, org=None):
        client.post(f"{HOLDS}/{hold_uuid}/release", json={"reason": REASON})

    staged = [
        e
        for e in events
        if e.get("event_type")
        in (AuditEventType.CONTENT_HOLD_PLACE, AuditEventType.CONTENT_HOLD_RELEASE)
    ]
    assert [e["event_type"] for e in staged] == [
        AuditEventType.CONTENT_HOLD_PLACE,
        AuditEventType.CONTENT_HOLD_RELEASE,
    ]
    for event in staged:
        assert event["resource_type"] == "content_hold"
        assert event["resource_uuid"] == hold_uuid
        assert event["user_id"] == operator.id
        assert event["organization_id"] is None
        assert event["details"] == {
            "authority": "platform",
            "scope": "file",
            "file_count": 1,
            "file_uuids": [str(f.uuid)],
        }
    assert REASON not in json.dumps(events, default=str)
    assert MATTER not in json.dumps(events, default=str)
    rows = gov.db.execute(
        select(AuditOutbox.event_type, AuditOutbox.payload).where(
            AuditOutbox.payload["resource_uuid"].astext == hold_uuid
        )
    ).all()
    assert sorted(r.event_type for r in rows) == ["content.hold.place", "content.hold.release"]
    assert REASON not in json.dumps([r.payload for r in rows])


def test_a_refused_delete_says_platform_and_records_the_refusal(client, gov, operator, events):
    f = gov.file(gov.alice)
    _place(client, gov, operator, f)
    with gov.acting_as(gov.alice):
        response = client.delete(f"/api/files/{f.uuid}")
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["held_by"] == ["platform"]
    assert "holds" not in response.json()["detail"]
    assert REASON not in response.text
    refused = [e for e in events if e.get("event_type") is AuditEventType.CONTENT_HOLD_REFUSED]
    assert len(refused) == 1
    assert refused[0]["details"] == {"action": "delete", "held_by": ["platform"]}
    gov.db.expire_all()
    assert gov.db.get(MediaFile, f.id).deleted_at is None
