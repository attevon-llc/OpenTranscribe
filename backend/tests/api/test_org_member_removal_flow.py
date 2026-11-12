"""Removing a member over HTTP keeps the organization content and schedules the personal rest (#1104).

The community edition has no membership or IdP removal path of its own; the one removal an
operator can perform is deleting the member's account, which runs the transfer first. This drives
that on the real route: ``DELETE /api/users/{uuid}``. The managed edition calls the same service
from its removal hook, which is not part of this repository.
"""

from __future__ import annotations

import pytest

from app.models.collection import Collection
from app.models.media import MediaFile
from app.models.user import User
from app.services import user_deletion_service

REMOVED = "/api/admin/removed-members"
RESTORE = "/api/admin/removed-members/{}/restore"


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    monkeypatch.setattr("app.services.account_purge.purge_account_external_copies", lambda plan: [])
    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)
    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", lambda *a, **k: True)
    monkeypatch.setattr("app.services.audit_outbox.deliver_staged", lambda db: None)
    monkeypatch.setattr(
        user_deletion_service.token_service, "stamp_user_revocation_epoch", lambda uuid: None
    )


def test_removing_a_member_keeps_org_content_and_holds_the_personal_files_for_the_grace(
    client, gov, super_admin_token_headers
):
    org_file = gov.file(gov.alice)
    personal = gov.file(gov.alice, org=False)
    collection = gov.collection(gov.alice)
    gov.put(collection, org_file)
    org_file_id, personal_id, collection_id = org_file.id, personal.id, collection.id
    alice_uuid = str(gov.alice.uuid)

    response = client.delete(f"/api/users/{alice_uuid}", headers=super_admin_token_headers)

    assert response.status_code == 204, response.text
    gov.db.expire_all()
    survivor = gov.db.get(MediaFile, org_file_id)
    assert survivor is not None and survivor.user_id == gov.admin.id
    kept = gov.db.get(Collection, collection_id)
    assert kept is not None and kept.user_id == gov.admin.id
    # Personal files wait out the grace period instead of vanishing.
    assert gov.db.get(MediaFile, personal_id) is not None
    assert gov.db.get(User, gov.alice.id).is_active is False
    pending = client.get(REMOVED, headers=super_admin_token_headers).json()
    assert [p["subject_user_uuid"] for p in pending] == [alice_uuid]


def test_an_administrator_can_restore_a_removed_member_inside_the_grace(
    client, gov, super_admin_token_headers
):
    personal = gov.file(gov.alice, org=False)
    personal_id = personal.id
    client.delete(f"/api/users/{gov.alice.uuid}", headers=super_admin_token_headers)
    ledger_uuid = client.get(REMOVED, headers=super_admin_token_headers).json()[0]["ledger_uuid"]

    restored = client.post(RESTORE.format(ledger_uuid), headers=super_admin_token_headers)

    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "cancelled"
    gov.db.expire_all()
    assert gov.db.get(User, gov.alice.id).is_active is True
    assert gov.db.get(MediaFile, personal_id) is not None
    assert client.get(REMOVED, headers=super_admin_token_headers).json() == []


def test_restoring_an_unknown_removal_is_a_404(client, gov, super_admin_token_headers):
    response = client.post(
        RESTORE.format("00000000-0000-4000-8000-000000000000"), headers=super_admin_token_headers
    )
    assert response.status_code == 404


def test_deleting_the_last_org_admin_with_content_is_refused(
    client, gov, super_admin_token_headers
):
    f = gov.file(gov.admin)
    file_id = f.id

    response = client.delete(f"/api/users/{gov.admin.uuid}", headers=super_admin_token_headers)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "no_transfer_target"
    gov.db.expire_all()
    assert gov.db.get(MediaFile, file_id).user_id == gov.admin.id
