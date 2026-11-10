"""A GDPR erasure keeps the voiceprints and profile links of a file under a legal hold.

A hold freezes the file as it stands (``hold_guard``), and re-diarizing it to rebuild a voiceprint
is refused while the hold stands, so destroying one cannot be undone. The erasure skips the held
file itself; these pin that its speaker documents, the profile its speaker is linked to and the
cluster centroid holding that voice survive too, that the rest of the subject's voiceprints still
go, and that the receipt does not report what the hold keeps as residual.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core import embedding_models
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerProfile
from app.services import gdpr_erasure_service as erasure
from app.services.erasure_receipt import stores_external
from app.services.erasure_receipt.capture import capture
from app.services.erasure_receipt.types import ErasureScope
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user

pytestmark = pytest.mark.usefixtures("no_receipt_measurement", "no_erasure_extras")


@pytest.fixture()
def index(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    monkeypatch.setattr(
        "app.services.opensearch_service.remove_profile_embedding",
        lambda uuid: fake.delete_by_query(
            index=embedding_models.get_speaker_index_v4(),
            body={"query": {"ids": {"values": [f"profile_{uuid}"]}}},
        ),
    )
    monkeypatch.setattr(
        "app.services.gdpr_erasure_steps.purge_media_file",
        lambda db, media_file, **kw: {"deleted": True, "residual_errors": []},
    )
    return fake


def _world(db, index, org_id):
    user = make_user(db, "hv")
    held_file, free_file = make_file(db, user, org_id), make_file(db, user, org_id)
    db.execute(text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": held_file.id})
    kept_profile = SpeakerProfile(user_id=user.id, organization_id=org_id, name="held voice")
    free_profile = SpeakerProfile(user_id=user.id, organization_id=org_id, name="free voice")
    db.add_all([kept_profile, free_profile])
    db.commit()
    held = Speaker(
        user_id=user.id, media_file_id=held_file.id, name="SPEAKER_00", profile_id=kept_profile.id
    )
    free = Speaker(
        user_id=user.id, media_file_id=free_file.id, name="SPEAKER_00", profile_id=free_profile.id
    )
    cluster = SpeakerCluster(user_id=user.id, organization_id=org_id, label="held cluster")
    db.add_all([held, free, cluster])
    db.commit()
    db.add(SpeakerClusterMember(cluster_id=cluster.id, speaker_id=held.id))
    db.commit()
    docs = {
        "held": str(held.uuid),
        "free": str(free.uuid),
        "kept_profile": f"profile_{kept_profile.uuid}",
        "free_profile": f"profile_{free_profile.uuid}",
        "cluster": f"cluster_{cluster.uuid}",
    }
    for kind, doc_id in docs.items():
        doc_type = {"kept_profile": "profile", "free_profile": "profile", "cluster": "cluster"}
        index.add(doc_id, user.id, org_id, doc_type.get(kind))
    return user, held, kept_profile, docs


@pytest.mark.parametrize("kind", ["user", "org_member"])
def test_held_files_voiceprints_and_profile_link_survive_the_erasure(db_session, index, kind):
    db = db_session
    org = make_org(db, "hv")
    user, held, kept_profile, docs = _world(db, index, org.id)
    if kind == "user":
        scope = ErasureScope("user", user_id=user.id)
    else:
        scope = ErasureScope("org_member", user_id=user.id, organization_id=org.id)
    captured = capture(db, scope)

    if kind == "user":
        summary = erasure.erase_user(db, user.id)
    else:
        summary = erasure.erase_org_member_data(db, user.id, org.id)

    assert index.ids() == {docs["held"], docs["kept_profile"], docs["cluster"]}
    db.expire_all()
    assert db.get(SpeakerProfile, kept_profile.id) is not None
    assert db.get(Speaker, held.id).profile_id == kept_profile.id
    assert summary["legal_holds_skipped"] >= 2, "the file and the profile are both deferred"
    # What the hold keeps is not residual.
    assert stores_external._os_speakers(scope, captured) == 0
