"""The HTTP surface of undoing and making speaker corrections (issue #1233, review pass).

The UI path a user takes when an unassignment was wrong is the Inbox: the unassigned
speaker shows up there, and the per-speaker "assign" / "accept" goes through
``/speaker-clusters/batch-verify``. That route refuses a corrected speaker unless the
request explicitly overrides the correction for that one speaker. Rejecting an
identification (``/speakers/{uuid}/verify?action=reject``) is itself a correction.

Driven through the real app against savepoint-isolated rows; OpenSearch is the recorders
of ``speaker_os_seams`` / ``speaker_assignment_seams``.
"""

from __future__ import annotations

import pytest

from app.models.media import Speaker
from app.models.media import SpeakerCannotLink
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfileBlacklist
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import VOICE_B
from tests.fixtures.speaker_cleanup_seams import VOICE_C
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

MATCH = 0.99999
NAME = "Dana"


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def _promoted_trio(db, user, seams):
    profile = make_profile(db, user, NAME)
    speakers = []
    for voice in (VOICE_A, VOICE_B, VOICE_C):
        sp = make_speaker(db, user, make_file(db, user))
        sp.profile_id, sp.display_name, sp.verified = profile.id, NAME, True
        seams["voiceprints"][str(sp.uuid)] = voice
        speakers.append(sp)
    cluster = make_cluster(db, user, *speakers, promoted_to=profile.id, label=NAME)
    return profile, cluster, speakers


def _blacklisted(db, speaker_id: int, profile_id: int) -> bool:
    count: int = (
        db.query(SpeakerProfileBlacklist)
        .filter(
            SpeakerProfileBlacklist.speaker_id == speaker_id,
            SpeakerProfileBlacklist.profile_id == profile_id,
        )
        .count()
    )
    return count == 1


def _batch(client, headers, speakers, profile, **extra):
    body = {
        "speaker_uuids": [str(s.uuid) for s in speakers],
        "action": "assign",
        "profile_uuid": str(profile.uuid),
        **extra,
    }
    return client.post("/api/speaker-clusters/batch-verify", json=body, headers=headers)


def test_inbox_assign_with_an_explicit_override_undoes_a_wrong_unassignment(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    headers = _headers(user_token_headers)
    profile, cluster, (a, _b, c) = _promoted_trio(db_session, normal_user, speaker_os_seams)
    resp = client.post(
        f"/api/speaker-clusters/{cluster.uuid}/unassign",
        json={"speaker_uuids": [str(c.uuid)], "blacklist": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    refused = _batch(client, headers, [c], profile)
    assert refused.status_code == 200, refused.text
    assert refused.json()["blocked_speaker_uuids"] == [str(c.uuid)], "control: refused"

    resp = _batch(client, headers, [c], profile, override_unassignment=True)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["updated_count"], body["failed_count"], body["blocked_speaker_uuids"]) == (
        1,
        0,
        [],
    ), body
    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    assert (c.profile_id, c.display_name) == (profile.id, NAME)
    assert not _blacklisted(db_session, c.id, profile.id)
    assert (
        db_session.query(SpeakerCannotLink)
        .filter(
            SpeakerCannotLink.speaker_id == c.id, SpeakerCannotLink.cannot_link_speaker_id == a.id
        )
        .count()
        == 0
    ), "the cannot-link with the profile's own speakers survived the explicit override"
    expected = [x + y + z for x, y, z in zip(VOICE_A, VOICE_B, VOICE_C, strict=True)]
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], expected) >= MATCH, (
        "the profile embedding never heard the voice the user put back"
    )


def test_an_override_for_a_batch_is_refused_and_lifts_nothing(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    headers = _headers(user_token_headers)
    profile, cluster, (a, _b, c) = _promoted_trio(db_session, normal_user, speaker_os_seams)
    client.post(
        f"/api/speaker-clusters/{cluster.uuid}/unassign",
        json={"speaker_uuids": [str(c.uuid)], "blacklist": True},
        headers=headers,
    )

    resp = _batch(client, headers, [c, a], profile, override_unassignment=True)

    assert resp.status_code == 422, resp.text
    db_session.expire_all()
    assert _blacklisted(db_session, c.id, profile.id)
    assert db_session.get(Speaker, c.id).profile_id is None


def test_the_speaker_list_never_suggests_an_identity_the_user_unassigned(
    client, user_token_headers, db_session, normal_user, speaker_os_seams, monkeypatch
):
    """The file view's speaker editor: Dana is offered to the look-alike, not to C."""
    headers = _headers(user_token_headers)
    profile, cluster, (_a, _b, c) = _promoted_trio(db_session, normal_user, speaker_os_seams)
    client.post(
        f"/api/speaker-clusters/{cluster.uuid}/unassign",
        json={"speaker_uuids": [str(c.uuid)], "blacklist": True},
        headers=headers,
    )
    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    d = make_speaker(db_session, normal_user, c.media_file, name="SPEAKER_01")
    speaker_os_seams["voiceprints"][str(d.uuid)] = VOICE_C
    db_session.commit()

    def knn(speaker_embeddings, user_id, threshold, organization_id=None):
        hit = {
            "profile_id": profile.id,
            "profile_name": NAME,
            "similarity": 0.9,
            "speaker_count": 2,
        }
        return {suuid: [dict(hit)] for suuid in speaker_embeddings}

    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", object())
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embeddings_batch",
        lambda uuids: {u: speaker_os_seams["voiceprints"][u] for u in uuids},
    )
    monkeypatch.setattr("app.services.opensearch_service.msearch_profile_knn_batch", knn)
    monkeypatch.setattr(
        "app.services.smart_speaker_suggestion_service._check_opensearch_profiles_exist",
        lambda *a, **k: True,
    )

    resp = client.get("/api/speakers", params={"file_id": str(c.media_file.uuid)}, headers=headers)

    assert resp.status_code == 200, resp.text
    offered = {row["uuid"]: [s["name"] for s in row["profile_suggestions"]] for row in resp.json()}
    assert offered[str(d.uuid)] == [NAME], "control: the uncorrected look-alike is offered Dana"
    assert offered[str(c.uuid)] == [], "the speaker the user unassigned is offered Dana again"


def test_rejecting_a_linked_identification_is_an_unassignment(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    """Reject on a member of the profile's cluster: out of the cluster and the profile."""
    headers = _headers(user_token_headers)
    profile, cluster, (a, b, c) = _promoted_trio(db_session, normal_user, speaker_os_seams)

    resp = client.post(
        f"/api/speakers/{c.uuid}/verify", params={"action": "reject"}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    assert (c.profile_id, c.display_name, c.cluster_id) == (None, None, None)
    assert (
        db_session.query(SpeakerClusterMember)
        .filter(SpeakerClusterMember.speaker_id == c.id)
        .count()
        == 0
    )
    assert _blacklisted(db_session, c.id, profile.id)
    partners = {
        int(r.cannot_link_speaker_id)
        for r in db_session.query(SpeakerCannotLink).filter(SpeakerCannotLink.speaker_id == c.id)
    }
    assert partners == {a.id, b.id}
    survivors = [x + y for x, y in zip(VOICE_A, VOICE_B, strict=True)]
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], survivors) >= MATCH
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], survivors) >= MATCH
