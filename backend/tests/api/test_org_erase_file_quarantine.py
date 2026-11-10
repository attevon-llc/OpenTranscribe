"""``/org-admin/gdpr/erase-file`` keeps a platform takedown out of an organization admin's reach.

A quarantined recording is hidden from an organization admin on every read and delete path
(``delete_permissions.get_deletable_file`` answers 404). The single-file erasure destroys
irreversibly and skips the trash, so it must answer the same 404 and destroy nothing; a trashed
recording stays erasable (Art. 17 covers the trash).
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership


def _org_admin_file(db, user, org_context):
    org = Organization(
        external_org_id=f"org_qe_{uuid_pkg.uuid4().hex[:8]}", name="qe org", is_active=True
    )
    db.add(org)
    db.commit()
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:admin"))
    db.commit()
    org_context(org_id=org.id, org_role="org:admin", only_for=user.id)
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="e2e-delres-cccccccc.wav",
        storage_path="media/e2e-delres-cccccccc.wav",
        content_type="audio/wav",
        file_size=1,
        user_id=user.id,
        organization_id=org.id,
        status="completed",
    )
    db.add(media)
    db.commit()
    return media


@pytest.mark.usefixtures("organizations_capability_on")
def test_a_quarantined_file_is_404_and_untouched(
    client, db_session, normal_user, user_token_headers, org_context
):
    media = _org_admin_file(db_session, normal_user, org_context)
    db_session.execute(
        text("UPDATE media_file SET is_quarantined = true WHERE id = :i"), {"i": media.id}
    )
    db_session.commit()

    with patch("app.services.gdpr_erasure_service.erase_file", return_value={}) as erase:
        response = client.post(
            f"/api/org-admin/gdpr/erase-file/{media.uuid}", headers=user_token_headers
        )

    assert response.status_code == 404, response.text
    assert erase.call_count == 0


@pytest.mark.usefixtures("organizations_capability_on")
def test_a_trashed_file_is_still_erased(
    client, db_session, normal_user, user_token_headers, org_context
):
    media = _org_admin_file(db_session, normal_user, org_context)
    db_session.execute(
        text(
            "UPDATE media_file SET deleted_at = :t, purge_after = :t, trash_origin = 'user' "
            "WHERE id = :i"
        ),
        {"t": datetime.now(UTC), "i": media.id},
    )
    db_session.commit()

    with patch("app.services.gdpr_erasure_service.erase_file", return_value={}) as erase:
        response = client.post(
            f"/api/org-admin/gdpr/erase-file/{media.uuid}", headers=user_token_headers
        )

    assert response.status_code == 200, response.text
    assert erase.call_count == 1
