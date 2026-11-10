"""What the erasure receipt captures before the destroy, checked against real rows.

The receipt's speaker-index check looks for ``profile_<uuid>`` and ``cluster_<uuid>`` documents
by the uuids captured here. An id that is not exactly the row's uuid matches no document, so a
surviving profile or centroid embedding would read as erased.
"""

from __future__ import annotations

from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerProfile
from app.services.erasure_receipt.capture import capture
from app.services.erasure_receipt.types import ErasureScope
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def test_profile_and_cluster_uuids_are_captured_as_the_rows_uuids(db_session):
    db = db_session
    org = make_org(db, "cap")
    user = make_user(db, "cap")
    profile = SpeakerProfile(user_id=user.id, organization_id=org.id, name="cap profile")
    cluster = SpeakerCluster(user_id=user.id, organization_id=org.id, label="cap cluster")
    db.add_all([profile, cluster])
    db.commit()

    for scope in (
        ErasureScope("user", user_id=user.id),
        ErasureScope("org_member", user_id=user.id, organization_id=org.id),
        ErasureScope("organization", organization_id=org.id),
    ):
        captured = capture(db, scope)
        assert captured.profile_uuids == [str(profile.uuid)], scope.kind
        assert captured.cluster_uuids == [str(cluster.uuid)], scope.kind
