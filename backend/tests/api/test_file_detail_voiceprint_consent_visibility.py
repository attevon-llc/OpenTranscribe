"""The per-file voiceprint consent fields are the owner's and the organization admin's.

``voiceprint_consent`` and ``voiceprint_consent_at`` are the uploader's recorded claim. Anyone
else who can open the file (here a platform admin acting under a support-access grant) gets the
defaults: ``unspecified`` and ``null``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember

CONSENT_AT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def consented(support_world):
    w = support_world
    w.roles[w.owner.id] = (w.org.id, "org:member")
    # An organization collection is a read route to its files for the organization's members.
    collection = Collection(name="consent-visibility", user_id=w.owner.id, organization_id=w.org.id)
    w.db.add(collection)
    w.db.commit()
    w.db.add(CollectionMember(collection_id=collection.id, media_file_id=w.file.id))
    w.file.voiceprint_consent = "granted"
    w.file.voiceprint_consent_at = CONSENT_AT
    w.db.commit()
    return w


def _detail(w, headers):
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_owner_sees_the_recorded_consent(consented):
    body = _detail(consented, consented.owner_headers)
    assert body["voiceprint_consent"] == "granted"
    assert body["voiceprint_consent_at"] is not None


def test_an_organization_admin_sees_the_recorded_consent(consented):
    body = _detail(consented, consented.org_admin_headers)
    assert body["voiceprint_consent"] == "granted"
    assert body["voiceprint_consent_at"] is not None


def test_another_viewer_gets_the_defaults(consented):
    grant = consented.active_grant()
    body = _detail(consented, consented.with_grant(grant))
    assert body["uuid"] == str(consented.file.uuid)
    assert body["voiceprint_consent"] == "unspecified"
    assert body["voiceprint_consent_at"] is None
