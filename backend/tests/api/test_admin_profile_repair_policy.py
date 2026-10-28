"""``POST /admin/profile-embeddings/repair`` under the privacy policy (#1280, plan D8).

The repair recomputes every profile's voiceprint, deployment-wide. A profile whose owner scope
has voiceprints off is skipped and counted; when EVERY profile is off the request has nothing
it could do and says why with a 409 instead of an empty success. The batch recompute itself is
recorded rather than run, so the ids it was handed are asserted directly (the endpoint reads
every profile in the database, so the assertions are about this test's profiles only).
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import SpeakerProfile
from app.services.profile_embedding_service import ProfileEmbeddingService
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch

URL = "/api/admin/profile-embeddings/repair"


@pytest.fixture
def recompute(monkeypatch) -> list[list[int]]:
    batches: list[list[int]] = []

    def batch(db, profile_ids):
        batches.append(list(profile_ids))
        return {pid: True for pid in profile_ids}

    monkeypatch.setattr(
        ProfileEmbeddingService, "batch_update_profile_embeddings", staticmethod(batch)
    )
    return batches


def _profile(db, user, org) -> SpeakerProfile:
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name="P")
    profile.organization_id = org.id if org else None
    db.add(profile)
    db.commit()
    return profile


def test_the_repair_recomputes_allowed_profiles_and_skips_the_ones_that_are_off(
    client, super_admin_token_headers, db_session, normal_user, recompute
):
    # Organizations exist, so the deployment is multi-tenant: a deployment-wide repair needs
    # the deployment-scope right (super_admin), not a plain admin.
    on = _profile(db_session, normal_user, make_org(db_session))
    off = _profile(db_session, normal_user, make_org(db_session, voiceprints=False))

    resp = client.post(URL, headers=super_admin_token_headers)

    assert resp.status_code == 200
    assert resp.json()["skipped_by_policy"] >= 1
    assert on.id in recompute[0]
    assert off.id not in recompute[0]


def test_the_repair_answers_409_when_the_instance_has_voiceprints_off(
    client, admin_token_headers, db_session, normal_user, recompute
):
    _profile(db_session, normal_user, None)
    set_instance_switch(db_session, "voiceprints", False)

    resp = client.post(URL, headers=admin_token_headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"code": "voiceprints_disabled", "locked_by": "instance"}
    assert recompute == []


def test_a_plain_admin_cannot_run_the_deployment_wide_repair_once_organizations_exist(
    client, admin_token_headers, db_session, normal_user, recompute
):
    _profile(db_session, normal_user, make_org(db_session))

    resp = client.post(URL, headers=admin_token_headers)

    assert resp.status_code == 403
    assert recompute == []
