"""Removing a member over HTTP keeps the organization's content and erases the personal rest (#1104).

The community edition has no membership or IdP removal path of its own; the one removal an
operator can perform is deleting the member's account, which runs the transfer first. This drives
that on the real route: ``DELETE /api/users/{uuid}``. The managed edition calls the same service
from its removal hook, which is not part of this repository.
"""

from __future__ import annotations

import pytest

from app.models.collection import Collection
from app.models.media import MediaFile
from app.services import user_deletion_service


@pytest.fixture(autouse=True)
def _no_external(monkeypatch):
    monkeypatch.setattr("app.services.account_purge.purge_account_external_copies", lambda plan: [])
    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)
    monkeypatch.setattr(
        user_deletion_service.token_service, "stamp_user_revocation_epoch", lambda uuid: None
    )


def test_deleting_a_member_keeps_org_files_and_collections_with_the_admin(
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
    assert gov.db.get(MediaFile, personal_id) is None
    survivor = gov.db.get(MediaFile, org_file_id)
    assert survivor is not None and survivor.user_id == gov.admin.id
    kept = gov.db.get(Collection, collection_id)
    assert kept is not None and kept.user_id == gov.admin.id


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
