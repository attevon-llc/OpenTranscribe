"""Every HTTP route that removes a speaker or moves it out of a cluster/profile cleans up (#1214).

Each route is driven through the real app (routing, auth, the endpoint, the service and
``speaker_cleanup``) against savepoint-isolated Postgres rows; only the OpenSearch seams
are recorders (``speaker_os_seams``). The live-stack proof with real OpenSearch documents
is ``tests/integration/test_deletion_residue_live.py``; this suite covers the paths and
failure modes that one cannot reach cheaply — the 503 contract, profile relabelling, the
background halves of merge and profile moves.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import VOICE_B
from tests.fixtures.speaker_cleanup_seams import VOICE_C
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_pair
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

MATCH = 0.99999
# Equal-weight mean of VOICE_A and VOICE_B (cosine ignores the scale).
A_PLUS_B = [a + b for a, b in zip(VOICE_A, VOICE_B, strict=True)]


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def test_delete_speaker_rebuilds_cluster_and_profile_and_erases_the_voiceprint(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    profile = make_profile(db_session, normal_user)
    fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id = sb.profile_id = profile.id
    db_session.commit()
    victim, cluster_id = str(sa.uuid), cluster.id

    resp = client.delete(f"/api/speakers/{victim}", headers=_headers(user_token_headers))

    assert resp.status_code == 204, resp.text
    assert speaker_os_seams["erased_speakers"] == [victim]
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_B) >= MATCH
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], VOICE_B) >= MATCH
    assert speaker_os_seams["reindexed"] == [fa.id], "the file's index planes still name it"
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id).member_count == 1


def test_delete_speaker_reports_a_surviving_copy_as_503_without_driver_text(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    _fa, _fb, sa, _sb, _cluster = make_pair(db_session, normal_user, speaker_os_seams)
    speaker_os_seams["store_ok"] = False
    speaker_os_seams["erase_error"] = "ConnectionError: opensearch:9200 refused"
    victim = str(sa.uuid)

    resp = client.delete(f"/api/speakers/{victim}", headers=_headers(user_token_headers))

    assert resp.status_code == 503, resp.text
    from app.services.speaker_cleanup import CLEANUP_INCOMPLETE_MESSAGE

    assert resp.json() == {"detail": CLEANUP_INCOMPLETE_MESSAGE}
    db_session.expire_all()
    assert db_session.query(Speaker).filter(Speaker.uuid == victim).count() == 0, (
        "the delete itself committed; only the cleanup is reported incomplete"
    )


def test_reassigning_the_last_segment_cleans_up_the_orphaned_speaker(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    fa, _fb, sa, _sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    keeper = make_speaker(db_session, normal_user, fa, name="SPEAKER_01")
    seg = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=fa.id,
        speaker_id=sa.id,
        start_time=0.0,
        end_time=1.0,
        text="hello",
    )
    db_session.add(seg)
    db_session.commit()
    victim = str(sa.uuid)

    resp = client.put(
        f"/api/transcripts/segments/{seg.uuid}/speaker",
        json={"speaker_uuid": str(keeper.uuid)},
        headers=_headers(user_token_headers),
    )

    assert resp.status_code == 200, resp.text
    assert speaker_os_seams["erased_speakers"] == [victim]
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_B) >= MATCH


def test_merging_speakers_drops_the_source_from_its_cluster_before_answering(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    fa, _fb, sa, _sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    target = make_speaker(db_session, normal_user, fa, name="SPEAKER_01")
    speaker_os_seams["voiceprints"][str(target.uuid)] = VOICE_C
    db_session.commit()

    resp = client.post(
        f"/api/speakers/{sa.uuid}/merge/{target.uuid}", headers=_headers(user_token_headers)
    )

    assert resp.status_code == 200, resp.text
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_B) >= MATCH
    # The source voiceprint is folded into the target by the background task, which
    # reads it first — so the endpoint must NOT have erased it.
    assert speaker_os_seams["erased_speakers"] == []


def test_the_merge_background_half_rebuilds_the_target_and_proves_the_source_gone(
    db_session, normal_user, speaker_os_seams
):
    from app.api.endpoints.speakers import _reconcile_speakers_after_merge

    fa = make_file(db_session, normal_user)
    target = make_speaker(db_session, normal_user, fa)
    other = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    speaker_os_seams["voiceprints"].update({str(target.uuid): VOICE_A, str(other.uuid): VOICE_B})
    cluster = make_cluster(db_session, normal_user, target, other)
    source_uuid = str(uuid_pkg.uuid4())

    residual = _reconcile_speakers_after_merge(db_session, source_uuid, target.id)

    assert residual == []
    assert speaker_os_seams["erased_speakers"] == [source_uuid]
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], A_PLUS_B) >= MATCH


def test_deleting_a_cluster_erases_its_centroid_and_keeps_the_speakers(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    _fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    cluster_id, cluster_uuid = cluster.id, str(cluster.uuid)

    resp = client.delete(
        f"/api/speaker-clusters/{cluster_uuid}", headers=_headers(user_token_headers)
    )

    assert resp.status_code == 204, resp.text
    assert speaker_os_seams["erased_clusters"] == [cluster_uuid]
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id) is None
    survivors = db_session.query(Speaker).filter(Speaker.id.in_([sa.id, sb.id])).all()
    assert len(survivors) == 2 and all(s.cluster_id is None for s in survivors)


def test_deleting_a_cluster_reports_a_centroid_it_could_not_erase(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    _fa, _fb, _sa, _sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    speaker_os_seams["erase_error"] = "TransportError(500)"

    resp = client.delete(
        f"/api/speaker-clusters/{cluster.uuid}", headers=_headers(user_token_headers)
    )

    assert resp.status_code == 503, resp.text
    from app.services.speaker_cleanup import CLEANUP_INCOMPLETE_MESSAGE

    assert resp.json() == {"detail": CLEANUP_INCOMPLETE_MESSAGE}


def test_deleting_a_profile_takes_its_name_and_voice_back(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    """Members named by the profile lose the name; a member named otherwise keeps it."""
    profile = make_profile(db_session, normal_user, name="Dana")
    _fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id, sa.display_name, sa.verified = profile.id, "Dana", True
    sb.profile_id, sb.display_name, sb.verified = profile.id, "Dee", True
    cluster.promoted_to_profile_id, cluster.label = profile.id, "Dana"
    emptied = make_cluster(db_session, normal_user, promoted_to=profile.id, label="Dana")
    db_session.commit()
    profile_uuid, cluster_id, emptied_id = str(profile.uuid), cluster.id, emptied.id
    emptied_uuid = str(emptied.uuid)

    resp = client.delete(
        f"/api/speaker-profiles/profiles/{profile_uuid}", headers=_headers(user_token_headers)
    )

    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    a, b = db_session.get(Speaker, sa.id), db_session.get(Speaker, sb.id)
    assert (a.display_name, a.profile_id, a.verified) == (None, None, False)
    assert (b.display_name, b.profile_id) == ("Dee", None), "a user-set name must survive"
    kept = db_session.get(SpeakerCluster, cluster_id)
    assert kept.label is None and kept.promoted_to_profile_id is None
    assert db_session.get(SpeakerCluster, emptied_id) is None, "the empty promoted cluster stayed"
    assert speaker_os_seams["erased_profiles"] == [profile_uuid]
    assert speaker_os_seams["erased_clusters"] == [emptied_uuid]
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], A_PLUS_B) >= MATCH
    assert db_session.query(SpeakerProfile).filter(SpeakerProfile.uuid == profile_uuid).count() == 0


def test_a_manual_profile_move_rebuilds_the_profile_the_speaker_left(
    db_session, normal_user, speaker_os_seams
):
    """It used to be rebuilt only for an auto-labeled speaker; a manual move kept the voice."""
    from app.api.endpoints.speakers import _handle_profile_embedding_updates

    old = make_profile(db_session, normal_user, "Old")
    new = make_profile(db_session, normal_user, "New")
    _fa, _fb, sa, sb, _cl = make_pair(db_session, normal_user, speaker_os_seams)
    sb.profile_id = old.id
    sa.profile_id = new.id  # already moved by the PUT; the task runs after
    db_session.commit()

    residual = _handle_profile_embedding_updates(db_session, sa.id, old.id, new.id, False)

    assert residual == []
    assert cosine(speaker_os_seams["profiles"][str(old.uuid)], VOICE_B) >= MATCH
    assert cosine(speaker_os_seams["profiles"][str(new.uuid)], VOICE_A) >= MATCH


def test_rejecting_a_suggestion_rebuilds_the_profile_the_speaker_left(
    client, user_token_headers, db_session, normal_user, speaker_os_seams
):
    profile = make_profile(db_session, normal_user)
    _fa, _fb, sa, sb, _cl = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id = sb.profile_id = profile.id
    db_session.commit()

    resp = client.post(
        f"/api/speakers/{sa.uuid}/verify?action=reject", headers=_headers(user_token_headers)
    )

    assert resp.status_code == 200, resp.text
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], VOICE_B) >= MATCH
