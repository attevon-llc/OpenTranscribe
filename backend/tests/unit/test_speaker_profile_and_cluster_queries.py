"""Service-level behaviour of the speaker profile, collection and cluster queries."""

from __future__ import annotations

import uuid

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerCollection
from app.models.speaker import SpeakerCollectionMember
from app.models.speaker import SpeakerProfile
from app.services import speaker_cluster_queries as clusters
from app.services import speaker_profile_queries as profiles
from tests.fixtures.speaker_query_rows import file_row
from tests.fixtures.speaker_query_rows import segment_rows
from tests.fixtures.speaker_query_rows import speaker_row


def test_profile_counts_cover_available_files_only(db_session, normal_user):
    prof = SpeakerProfile(user_id=normal_user.id, name=f"P{uuid.uuid4().hex[:6]}")
    db_session.add(prof)
    db_session.flush()
    one, two = file_row(db_session, normal_user), file_row(db_session, normal_user)
    for mf in (one, one, two):
        speaker_row(db_session, normal_user, mf, profile_id=prof.id)
    speaker_row(
        db_session,
        normal_user,
        file_row(db_session, normal_user, is_quarantined=True),
        profile_id=prof.id,
    )
    counts = profiles.profile_media_instance_counts(db_session, [prof.id])
    assert counts == {prof.id: (2, 3)}
    assert profiles.speakers_linked_to(db_session, prof.id).__len__() == 4


def test_collection_counts_and_names(db_session, normal_user):
    col = SpeakerCollection(user_id=normal_user.id, name="Team")
    prof = SpeakerProfile(user_id=normal_user.id, name=f"P{uuid.uuid4().hex[:6]}")
    db_session.add_all([col, prof])
    db_session.flush()
    db_session.add(SpeakerCollectionMember(collection_id=col.id, speaker_profile_id=prof.id))
    db_session.flush()
    assert profiles.collection_member_counts(db_session, [col.id]) == {col.id: 1}
    assert profiles.collection_member_counts(db_session, []) == {}
    assert profiles.collection_by_exact_name(db_session, normal_user.id, "Team", None) is col
    assert (
        profiles.collection_by_exact_name(db_session, normal_user.id, "Team", 2_000_000_000) is None
    )
    assert profiles.list_profiles(
        db_session, SpeakerProfile.user_id == normal_user.id, collection_id=col.id
    ) == [prof]
    assert profiles.profile_ids_owned_by(db_session, normal_user.id) == {prof.id}


def test_cluster_helpers_count_dissolve_and_match(db_session, normal_user):
    mf = file_row(db_session, normal_user)
    cluster = SpeakerCluster(user_id=normal_user.id, label="c")
    db_session.add(cluster)
    db_session.flush()
    spk = speaker_row(db_session, normal_user, mf, cluster_id=cluster.id)
    assert clusters.speaker_totals(db_session, [Speaker.user_id == normal_user.id]) == (1, 1)
    assert clusters.count_clusters(db_session, [SpeakerCluster.user_id == normal_user.id]) == 1
    assert clusters.cluster_matching(db_session, [SpeakerCluster.uuid == cluster.uuid]) is cluster
    clusters.dissolve_cluster_membership(db_session, cluster.id)
    db_session.refresh(spk)
    assert spk.cluster_id is None
    assert clusters.speaker_totals(db_session, [Speaker.user_id == normal_user.id]) == (1, 0)


def test_longest_segment_breaks_ties_by_id(db_session, normal_user):
    mf = file_row(db_session, normal_user)
    spk = speaker_row(db_session, normal_user, mf)
    short, tied_first, tied_second = segment_rows(
        db_session, mf, spk, [(0.0, 1.0), (5.0, 3.0), (9.0, 3.0)]
    )
    assert clusters.longest_segment_of_speaker(db_session, spk.id) is tied_first
    assert clusters.speaker_of_user_by_uuid(db_session, str(spk.uuid), normal_user.id) is spk
    assert clusters.speaker_of_user_by_uuid(db_session, str(spk.uuid), normal_user.id + 1) is None
    assert short is not tied_first
