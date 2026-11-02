"""``/admin/gdpr/erasures`` and ``/org-admin/gdpr/erasures`` over HTTP (issue #1353, R12/R14).

Receipts are seeded as rows in the test savepoint (the measurers are covered against real stores
in ``integration/test_erasure_receipt_e2e.py``); what is under test here is who may read which
receipt, that a read is audited with ids only, and that single-file erasure goes through the
ledger.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from app.models.erasure import ErasureLedgerEntry
from app.models.erasure import ErasureReceipt
from app.models.organization import Organization
from app.models.organization import OrganizationMembership

LIST = "/api/admin/gdpr/erasures"


def _org(db) -> Organization:
    row = Organization(
        external_org_id=f"org_rcpt_{uuid_pkg.uuid4().hex[:10]}", name="receipt org", is_active=True
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _entry(db, *, subject_type: str, org: Organization | None = None) -> ErasureLedgerEntry:
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    row = ErasureLedgerEntry(
        subject_type=subject_type,
        subject_user_id=987001
        if subject_type != "file" and subject_type != "organization"
        else None,
        subject_organization_id=org.id if org else None,
        subject_organization_uuid=org.uuid if org else None,
        subject_file_uuid=uuid_pkg.uuid4() if subject_type == "file" else None,
        status="complete",
        actor_kind="super_admin",
        sla_due_at=datetime.now(UTC) + timedelta(days=30),
        counters={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _receipt(db, entry, outcome="verified") -> ErasureReceipt:
    row = ErasureReceipt(
        ledger_entry_id=entry.id,
        attempt=1,
        phase="immediate",
        outcome=outcome,
        deleted={"pg.media_file": 2},
        de_attributed={},
        retained={"os.audit": 3},
        ttl={},
        residual={"pg.media_file": 0},
        unverified={},
        inventory_version=1,
        digest="b" * 64,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_super_admin_lists_erasures_with_the_latest_outcome(
    client, db_session, super_admin_token_headers
):
    entry = _entry(db_session, subject_type="user")
    _receipt(db_session, entry, "verified")
    response = client.get(LIST, headers=super_admin_token_headers)
    assert response.status_code == 200, response.text
    items = {i["uuid"]: i for i in response.json()["items"]}
    assert items[str(entry.uuid)]["latest_receipt_outcome"] == "verified"
    assert items[str(entry.uuid)]["subject_type"] == "user"
    body = response.text
    assert "987001" not in body, "no subject id leaves the API, only the ledger uuid"


def test_super_admin_reads_the_receipts_and_the_read_is_audited(
    client, db_session, super_admin_token_headers
):
    entry = _entry(db_session, subject_type="user")
    receipt = _receipt(db_session, entry)
    with patch("app.api.endpoints.erasure_receipts.audit_logger") as audit:
        response = client.get(f"{LIST}/{entry.uuid}/receipts", headers=super_admin_token_headers)
    assert response.status_code == 200, response.text
    [row] = response.json()
    assert row["uuid"] == str(receipt.uuid)
    assert row["outcome"] == "verified"
    assert row["deleted"] == {"pg.media_file": 2}
    assert row["retained_reasons"] == {
        "os.audit": "Art. 17(3)(b)/(e): the audit trail is a compliance record"
    }
    assert row["digest"] == "b" * 64
    audit.log.assert_called_once()
    details = audit.log.call_args.kwargs["details"]
    assert details == {"ledger_uuid": str(entry.uuid), "subject_type": "user"}
    assert audit.log.call_args.kwargs["event_type"].value == "gdpr.receipt.view"


def test_unknown_ledger_uuid_is_404(client, super_admin_token_headers):
    response = client.get(f"{LIST}/{uuid_pkg.uuid4()}/receipts", headers=super_admin_token_headers)
    assert response.status_code == 404


def test_unauthenticated_and_a_plain_user_are_both_403(client, user_token_headers):
    # The /admin tier answers 403 with no credential, like every other /admin route.
    assert client.get(LIST).status_code == 403
    assert client.get(LIST, headers=user_token_headers).status_code == 403


def test_a_platform_admin_who_is_not_super_admin_is_refused(client, admin_token_headers):
    assert client.get(LIST, headers=admin_token_headers).status_code == 403


@pytest.mark.usefixtures("organizations_capability_on")
class TestOrgAdminReads:
    @pytest.fixture
    def org_admin(self, db_session, normal_user, user_token_headers, org_context):
        org = _org(db_session)
        db_session.add(
            OrganizationMembership(organization_id=org.id, user_id=normal_user.id, role="org:admin")
        )
        db_session.commit()
        org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
        return org, user_token_headers

    def test_reads_the_receipts_of_its_own_members_erasure(self, client, db_session, org_admin):
        org, headers = org_admin
        entry = _entry(db_session, subject_type="org_member", org=org)
        _receipt(db_session, entry)
        response = client.get(
            f"/api/org-admin/gdpr/erasures/{entry.uuid}/receipts", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["subject_type"] == "org_member"

    def test_another_organizations_erasure_is_the_same_404_as_a_missing_one(
        self, client, db_session, org_admin
    ):
        _org_row, headers = org_admin
        foreign = _entry(db_session, subject_type="org_member", org=_org(db_session))
        _receipt(db_session, foreign)
        seen = client.get(f"/api/org-admin/gdpr/erasures/{foreign.uuid}/receipts", headers=headers)
        missing = client.get(
            f"/api/org-admin/gdpr/erasures/{uuid_pkg.uuid4()}/receipts", headers=headers
        )
        assert seen.status_code == missing.status_code == 404
        assert seen.json() == missing.json()

    def test_a_whole_organization_erasure_is_super_admin_only(self, client, db_session, org_admin):
        org, headers = org_admin
        entry = _entry(db_session, subject_type="organization", org=org)
        _receipt(db_session, entry)
        response = client.get(
            f"/api/org-admin/gdpr/erasures/{entry.uuid}/receipts", headers=headers
        )
        assert response.status_code == 404


def test_erase_file_goes_through_the_ledger_and_returns_a_receipt(
    client, db_session, super_admin_token_headers, normal_user
):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="e2e-delres-aaaaaaaa.wav",
        storage_path="media/e2e-delres-aaaaaaaa.wav",
        content_type="audio/wav",
        file_size=1,
        user_id=normal_user.id,
        status="completed",
    )
    db_session.add(media)
    db_session.commit()
    file_uuid = str(media.uuid)

    fake_receipt: dict[str, object] = {
        "uuid": None,
        "outcome": "verified",
        "residual_total": 0,
        "unverified_keys": [],
    }

    def _finish(db, entry, scope, captured, before, summary):
        assert scope.kind == "file" and scope.file_ids == (int(media.id),)
        summary["receipt"] = fake_receipt

    flow = Mock(begin=Mock(return_value=(object(), {})), finish=Mock(side_effect=_finish))
    with (
        patch(
            "app.services.gdpr_erasure_service.purge_media_file",
            return_value={"deleted": True, "residual_errors": []},
        ) as purge,
        patch("app.services.gdpr_erasure_service.receipt_flow", flow),
        patch("app.services.gdpr_erasure_service.erase_extras"),
        patch("app.services.gdpr_erasure_service.audit_logger"),
        patch("app.services.erasure_ledger_service.audit_logger"),
    ):
        response = client.post(
            f"/api/admin/gdpr/erase-file/{file_uuid}", headers=super_admin_token_headers
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert purge.call_count == 1
    assert body["receipt"] == fake_receipt
    entry = (
        db_session.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.uuid == uuid_pkg.UUID(body["ledger_uuid"]))
        .one()
    )
    assert entry.subject_type == "file"
    assert str(entry.subject_file_uuid) == file_uuid
    assert entry.status == "complete"


def test_erase_file_of_a_missing_file_is_404(client, super_admin_token_headers):
    response = client.post(
        f"/api/admin/gdpr/erase-file/{uuid_pkg.uuid4()}", headers=super_admin_token_headers
    )
    assert response.status_code == 404
