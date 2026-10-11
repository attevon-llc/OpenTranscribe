"""Every service-layer path that moves a speaker out of a cluster or profile cleans up (#1214).

Cluster unassign/split/merge, cluster promotion, batch profile assignment, the file purge,
the retry reset and the reprocess clears all route through ``speaker_cleanup``. Driven
against real Postgres rows with the OpenSearch seams replaced by recorders
(``speaker_os_seams``); assertions are on stored/erased vectors and rows. The helper's own
rules are pinned in ``test_speaker_cleanup.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.models.media import SpeakerCluster
from app.models.media import SpeakerProfile
from app.services import file_cleanup_service as fcs
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


def _service(db):
    from app.services.speaker_clustering_service import SpeakerClusteringService

    return SpeakerClusteringService(db)


def test_unassign_rebuilds_the_centroid_and_deletes_an_emptied_cluster(
    db_session, normal_user, speaker_os_seams
):
    _fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    cluster_id, cluster_uuid = cluster.id, str(cluster.uuid)

    _service(db_session).unassign_speakers(cluster_uuid, [str(sa.uuid)], normal_user.id, False)
    assert cosine(speaker_os_seams["clusters"][cluster_uuid], VOICE_B) >= MATCH
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id).member_count == 1

    _service(db_session).unassign_speakers(cluster_uuid, [str(sb.uuid)], normal_user.id, False)
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id) is None
    assert speaker_os_seams["erased_clusters"] == [cluster_uuid], "emptied cluster kept centroid"


def test_split_rebuilds_both_centroids(db_session, normal_user, speaker_os_seams):
    _fa, _fb, sa, _sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)

    new = _service(db_session).split_cluster(str(cluster.uuid), [str(sa.uuid)], normal_user.id)

    assert new is not None
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_B) >= MATCH
    assert cosine(speaker_os_seams["clusters"][str(new.uuid)], VOICE_A) >= MATCH


def test_merge_deletes_the_source_with_its_centroid_and_rebuilds_the_target(
    db_session, normal_user, speaker_os_seams
):
    fa, fb = make_file(db_session, normal_user), make_file(db_session, normal_user)
    sa, sb = make_speaker(db_session, normal_user, fa), make_speaker(db_session, normal_user, fb)
    speaker_os_seams["voiceprints"].update({str(sa.uuid): VOICE_A, str(sb.uuid): VOICE_B})
    source = make_cluster(db_session, normal_user, sa)
    target = make_cluster(db_session, normal_user, sb)
    source_id, source_uuid = source.id, str(source.uuid)

    merged = _service(db_session).merge_clusters(source_uuid, str(target.uuid), normal_user.id)

    assert merged is not None and merged.member_count == 2
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, source_id) is None
    assert speaker_os_seams["erased_clusters"] == [source_uuid]
    expected = (np.array(VOICE_A) + np.array(VOICE_B)).tolist()
    assert cosine(speaker_os_seams["clusters"][str(target.uuid)], expected) >= MATCH


def test_promotion_rebuilds_the_profile_a_member_leaves(db_session, normal_user, speaker_os_seams):
    old = make_profile(db_session, normal_user, name="Old")
    _fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    keeper = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    speaker_os_seams["voiceprints"][str(keeper.uuid)] = VOICE_C
    sa.profile_id = keeper.profile_id = old.id
    db_session.commit()

    new = _service(db_session).promote_cluster_to_profile(str(cluster.uuid), "New", normal_user.id)

    assert new is not None
    assert cosine(speaker_os_seams["profiles"][str(old.uuid)], VOICE_C) >= MATCH, (
        "the profile sa left still averages its voice"
    )
    expected = (np.array(VOICE_A) + np.array(VOICE_B)).tolist()
    assert cosine(speaker_os_seams["profiles"][str(new.uuid)], expected) >= MATCH


def test_batch_assign_rebuilds_the_profile_a_speaker_leaves(
    db_session, normal_user, speaker_os_seams
):
    old = make_profile(db_session, normal_user, "Old")
    target = make_profile(db_session, normal_user, "T")
    _fa, _fb, sa, sb, _cl = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id = sb.profile_id = old.id
    db_session.commit()

    result = _service(db_session).batch_verify_speakers(
        [str(sa.uuid)], normal_user.id, action="assign", profile_uuid=str(target.uuid)
    )

    assert result["updated_count"] == 1
    assert cosine(speaker_os_seams["profiles"][str(old.uuid)], VOICE_B) >= MATCH
    assert cosine(speaker_os_seams["profiles"][str(target.uuid)], VOICE_A) >= MATCH


def test_purge_rebuilds_the_profile_of_a_deleted_files_speaker(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """#1211 kept the profile row but left its embedding averaging the purged file."""
    profile = make_profile(db_session, normal_user)
    fa, _fb, sa, sb, _cl = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id = sb.profile_id = profile.id
    db_session.commit()
    profile_id, profile_uuid = profile.id, str(profile.uuid)
    monkeypatch.setattr(fcs, "_purge_external_copies", lambda plan: [])

    result = fcs.purge_media_file(db_session, fa)

    assert result["deleted"] is True and result["residual_errors"] == []
    assert db_session.get(SpeakerProfile, profile_id) is not None, "a file purge keeps profiles"
    assert cosine(speaker_os_seams["profiles"][profile_uuid], VOICE_B) >= MATCH


def test_retry_reset_erases_the_voiceprints_it_deletes(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    from app.utils import task_utils

    monkeypatch.setattr(task_utils, "transcript_is_regenerable", lambda db, mf: True)
    monkeypatch.setattr("app.tasks.search_indexing_task.index_file_summary.delay", lambda *a: None)
    _fa, fb, _sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    victim = str(sb.uuid)

    assert task_utils.reset_file_for_retry(db_session, fb.id) is True

    assert speaker_os_seams["erased_speakers"] == [victim], "retry left the deleted voiceprint"
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_A) >= MATCH


@pytest.mark.parametrize("stages", [["transcription"], ["rediarize"]])
def test_reprocess_clear_erases_voiceprints_and_rebuilds_clusters(
    db_session, normal_user, speaker_os_seams, monkeypatch, stages
):
    from app.api.endpoints.files import reprocess

    monkeypatch.setattr("app.tasks.search_indexing_task.index_file_summary.delay", lambda *a: None)
    _fa, fb, _sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    victim = str(sb.uuid)

    reprocess.clear_selective_data(db_session, fb, stages)

    assert speaker_os_seams["erased_speakers"] == [victim]
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_A) >= MATCH
