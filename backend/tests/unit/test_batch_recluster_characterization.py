"""Characterization of ``SpeakerClusteringService.batch_recluster`` (repo cleanup W8b).

``batch_recluster`` was one ~740-line method (cyclomatic complexity 93) and is being cut into
named steps. These tests pin what it DOES, from outside, with the arithmetic recomputed here
with numpy rather than copied from the code: the summary it returns, the numbers it writes on
a cluster (quality, tightest pair, separation, representative, per-member confidence), what it
stores in OpenSearch, which speakers a gender or a cannot-link evicts from an AHC group, how a
centroid-store failure changes the status, and the progress it reports.

Real Postgres rows; OpenSearch answered by the recorders in ``speaker_os_seams`` /
``speaker_assignment_seams``. Each eviction test has its control (the same voices without the
condition stay grouped), so a pass cannot come from a path that groups nothing.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCannotLink
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.services.speaker_clustering_service import SpeakerClusteringService
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")


def _unit(v: list[float]) -> np.ndarray:
    a = np.array(v, dtype=np.float64)
    unit: np.ndarray = a / np.linalg.norm(a)
    return unit


# Person 1 (the profile's voice and a look-alike), person 2 (a group of three), a stranger.
VA = [1.0, 0.2, 0.0, 0.0, 0.0, 0.0]
VB = [1.0, 0.0, 0.2, 0.0, 0.0, 0.0]
VY = [1.0, 0.1, 0.1, 0.0, 0.0, 0.0]  # matches the profile cluster, alone in the AHC pass
VE = [0.0, 0.0, 0.0, 1.0, 0.1, 0.0]
VF = [0.0, 0.0, 0.0, 1.0, 0.0, 0.1]
VG = [0.0, 0.0, 0.0, 1.0, -0.1, 0.0]
VX = [0.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def _voiced(db, user, seams, voice, *, profile=None, **kw) -> Any:
    sp = make_speaker(db, user, make_file(db, user), **kw)
    seams["voiceprints"][str(sp.uuid)] = list(voice)
    if profile is not None:
        sp.profile_id, sp.verified, sp.display_name = profile.id, True, profile.name
    db.flush()
    return sp


def _members(db, cluster) -> set[int]:
    return {
        int(m.speaker_id)
        for m in db.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.cluster_id == cluster.id
        )
    }


def _cluster_of(db, speaker) -> SpeakerCluster | None:
    db.expire_all()
    cid = db.get(Speaker, speaker.id).cluster_id
    return db.get(SpeakerCluster, cid) if cid is not None else None


# ---------------------------------------------------------------------------
# One full pass: Phase 1 (profile), Phase 2 (AHC), Phase 2.5 (singleton -> profile cluster)
# ---------------------------------------------------------------------------


@pytest.fixture
def full_pass(db_session, normal_user, speaker_os_seams) -> dict[str, Any]:
    profile = make_profile(db_session, normal_user, "Dana")
    world: dict[str, Any] = {"profile": profile}
    world["a"] = _voiced(db_session, normal_user, speaker_os_seams, VA, profile=profile)
    world["b"] = _voiced(db_session, normal_user, speaker_os_seams, VB, profile=profile)
    for key, voice in (("e", VE), ("f", VF), ("g", VG), ("y", VY), ("x", VX)):
        world[key] = _voiced(db_session, normal_user, speaker_os_seams, voice)
    db_session.commit()
    world["progress"] = []
    world["result"] = SpeakerClusteringService(db_session).batch_recluster(
        normal_user.id, progress_callback=lambda *args: world["progress"].append(args)
    )
    db_session.expire_all()
    return world


def test_the_summary_counts_every_phase(full_pass):
    assert full_pass["result"] == {
        "status": "completed",
        "total_speakers": 7,
        "profile_clusters": 1,
        "profile_speakers_assigned": 2,
        "similarity_clusters": 1,
        "similarity_speakers_assigned": 3,
        "clusters_created": 2,
        "speakers_assigned": 6,
        "singletons": 1,
        "centroid_failures": 0,
        "cleanup_residual_stages": [],
        "skipped_by_policy_scopes": 0,
    }


def test_phase_one_builds_the_profile_cluster_and_phase_two_and_a_half_adds_the_match(
    db_session, full_pass
):
    profile_cluster = (
        db_session.query(SpeakerCluster)
        .filter(SpeakerCluster.promoted_to_profile_id == full_pass["profile"].id)
        .one()
    )
    assert profile_cluster.label == "Dana"
    assert full_pass["profile"].source_cluster_id == profile_cluster.id
    assert _members(db_session, profile_cluster) == {full_pass[k].id for k in ("a", "b", "y")}, (
        "A and B by profile; Y joined by centroid match"
    )
    assert profile_cluster.member_count == 3
    y = db_session.get(Speaker, full_pass["y"].id)
    assert (y.profile_id, y.display_name) == (full_pass["profile"].id, "Dana")


def test_the_ahc_cluster_carries_the_recomputed_statistics(db_session, full_pass, speaker_os_seams):
    cluster = _cluster_of(db_session, full_pass["e"])
    assert cluster is not None and cluster.promoted_to_profile_id is None
    ids = [full_pass[k].id for k in ("e", "f", "g")]
    assert _members(db_session, cluster) == set(ids)

    raw = np.array([VE, VF, VG], dtype=np.float32)
    centroid = raw.mean(axis=0)
    centroid = centroid / np.linalg.norm(centroid)
    normed = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    expected_conf = dict(zip(("e", "f", "g"), normed @ centroid, strict=True))

    assert cluster.quality_score == pytest.approx(float(np.mean(normed @ centroid)), abs=1e-5)
    pair = normed @ normed.T
    assert cluster.min_similarity == pytest.approx(
        float(np.min(pair[np.triu_indices(3, k=1)])), abs=1e-5
    )
    assert cluster.representative_speaker_id in ids
    for key, want in expected_conf.items():
        member = (
            db_session.query(SpeakerClusterMember)
            .filter(
                SpeakerClusterMember.cluster_id == cluster.id,
                SpeakerClusterMember.speaker_id == full_pass[key].id,
            )
            .one()
        )
        assert member.confidence == pytest.approx(float(want), abs=1e-5)
    stored = np.array(speaker_os_seams["clusters"][str(cluster.uuid)])
    assert stored == pytest.approx(centroid, abs=1e-5)


def test_separation_scores_use_only_the_embeddings_the_pass_loaded(db_session, full_pass):
    """The profile cluster's A and B were never loaded (they have a profile), so its centroid
    for the separation score is the one joined voice, Y; the score is 1 - the nearest
    other cluster, rounded to four places, on both clusters."""
    profile_cluster = (
        db_session.query(SpeakerCluster)
        .filter(SpeakerCluster.promoted_to_profile_id == full_pass["profile"].id)
        .one()
    )
    ahc_cluster = _cluster_of(db_session, full_pass["e"])
    y_centroid = np.array(VY, dtype=np.float32)
    y_centroid = y_centroid / np.linalg.norm(y_centroid)
    efg = np.mean(np.array([VE, VF, VG], dtype=np.float32), axis=0)
    efg = efg / np.linalg.norm(efg)
    want = round(1.0 - float(y_centroid @ efg), 4)
    assert profile_cluster.separation_score == pytest.approx(want, abs=1e-6)
    assert ahc_cluster is not None and ahc_cluster.separation_score == pytest.approx(want, abs=1e-6)


def test_the_loners_stay_unclustered(db_session, full_pass):
    assert _cluster_of(db_session, full_pass["x"]) is None


def test_progress_is_reported_in_order(full_pass):
    assert full_pass["progress"] == [
        (1, 5, "Clearing old clusters...", 0.05),
        (2, 5, "Grouping by verified profiles...", 0.15),
        (3, 5, "Loading unlabeled embeddings...", 0.25),
        (4, 5, "Computing cosine similarities...", 0.4),
        (4, 5, "Computing cosine similarities...", pytest.approx(0.7)),
        (4, 5, "Clustering with AHC (complete linkage)...", 0.75),
        (5, 5, "Creating cluster records...", 0.9),
    ]


def test_a_failing_progress_callback_does_not_stop_the_pass(
    db_session, normal_user, speaker_os_seams
):
    voices = [VE, VF, VG]
    speakers = [_voiced(db_session, normal_user, speaker_os_seams, v) for v in voices]
    db_session.commit()

    def boom(*_args: Any) -> None:
        raise RuntimeError("the UI went away")

    result = SpeakerClusteringService(db_session).batch_recluster(
        normal_user.id, progress_callback=boom
    )

    assert result["similarity_clusters"] == 1
    assert _cluster_of(db_session, speakers[0]) is not None


# ---------------------------------------------------------------------------
# Eviction from an AHC group
# ---------------------------------------------------------------------------

# Three of one voice and a fourth that is similar enough to group at threshold 0.5 but sits
# below the 0.70 eviction bar against the majority centroid.
MAJOR = [[1.0, 0.0, 0.0], [1.0, 0.05, 0.0], [1.0, 0.0, 0.05]]
MINOR = [0.6, 0.8, 0.0]


# Unit-length voices (as real embeddings are) whose nearest-to-centroid order is unambiguous.
CHAIN = [_unit(v).tolist() for v in ([1.0, 0.0, 0.0], [1.0, 0.05, 0.0], [1.0, 0.45, 0.0])]


def _gender_group(db, user, seams, genders: tuple[str | None, ...]) -> list[Any]:
    speakers = [
        _voiced(db, user, seams, v, predicted_gender=g)
        for v, g in zip([*MAJOR[:2], MINOR], genders, strict=True)
    ]
    db.commit()
    return speakers


def test_a_weak_minority_gender_member_is_evicted_from_the_group(
    db_session, normal_user, speaker_os_seams
):
    major_a, major_b, minor = _gender_group(
        db_session, normal_user, speaker_os_seams, ("male", "male", "female")
    )

    SpeakerClusteringService(db_session).batch_recluster(normal_user.id, threshold=0.5)

    cluster = _cluster_of(db_session, major_a)
    assert cluster is not None and _members(db_session, cluster) == {major_a.id, major_b.id}
    assert _cluster_of(db_session, minor) is None


def test_control_without_a_gender_mismatch_the_same_voices_stay_grouped(
    db_session, normal_user, speaker_os_seams
):
    major_a, major_b, minor = _gender_group(
        db_session, normal_user, speaker_os_seams, ("male", "male", "male")
    )

    SpeakerClusteringService(db_session).batch_recluster(normal_user.id, threshold=0.5)

    cluster = _cluster_of(db_session, major_a)
    assert cluster is not None
    assert _members(db_session, cluster) == {major_a.id, major_b.id, minor.id}


def test_a_group_without_a_two_thirds_majority_evicts_nobody(
    db_session, normal_user, speaker_os_seams
):
    """Two men, two women, same weak voice split: no clear majority, so no eviction."""
    speakers = [
        _voiced(db_session, normal_user, speaker_os_seams, v, predicted_gender=g)
        for v, g in zip(
            [MAJOR[0], MAJOR[1], MINOR, [0.6, 0.8, 0.05]],
            ("male", "male", "female", "female"),
            strict=True,
        )
    ]
    db_session.commit()

    SpeakerClusteringService(db_session).batch_recluster(normal_user.id, threshold=0.5)

    clusters = {c.id for s in speakers if (c := _cluster_of(db_session, s)) is not None}
    assert len(clusters) >= 1
    grouped = [s for s in speakers if _cluster_of(db_session, s) is not None]
    assert len(grouped) == 4, "a 2/2 split has no majority to side with"


def test_a_cannot_link_evicts_the_member_furthest_from_the_group(
    db_session, normal_user, speaker_os_seams
):
    near, mid, far = (_voiced(db_session, normal_user, speaker_os_seams, v) for v in CHAIN)
    db_session.add(SpeakerCannotLink(speaker_id=near.id, cannot_link_speaker_id=far.id))
    db_session.commit()

    SpeakerClusteringService(db_session).batch_recluster(normal_user.id, threshold=0.5)

    cluster = _cluster_of(db_session, near)
    assert cluster is not None and _members(db_session, cluster) == {near.id, mid.id}
    assert _cluster_of(db_session, far) is None


def test_control_without_the_cannot_link_all_three_group(db_session, normal_user, speaker_os_seams):
    near, mid, far = (_voiced(db_session, normal_user, speaker_os_seams, v) for v in CHAIN)
    db_session.commit()

    SpeakerClusteringService(db_session).batch_recluster(normal_user.id, threshold=0.5)

    cluster = _cluster_of(db_session, near)
    assert cluster is not None and _members(db_session, cluster) == {near.id, mid.id, far.id}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failed_centroid_store_makes_the_pass_partial_but_keeps_the_clusters(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    speakers = [_voiced(db_session, normal_user, speaker_os_seams, v) for v in (VE, VF, VG)]
    db_session.commit()

    def refuse(**_kwargs: Any) -> bool:
        raise RuntimeError("opensearch is down")

    monkeypatch.setattr("app.services.opensearch_service.store_cluster_embedding", refuse)

    result = SpeakerClusteringService(db_session).batch_recluster(normal_user.id)

    assert result["status"] == "partial_failure"
    assert result["centroid_failures"] == 1 and result["clusters_created"] == 1
    assert _cluster_of(db_session, speakers[0]) is not None


@pytest.mark.parametrize("bad", [0.49, 0.96])
def test_an_out_of_range_threshold_is_refused_before_anything_is_cleared(
    db_session, normal_user, speaker_os_seams, bad
):
    from tests.fixtures.speaker_cleanup_seams import make_cluster

    a = _voiced(db_session, normal_user, speaker_os_seams, VE)
    b = _voiced(db_session, normal_user, speaker_os_seams, VF)
    old = make_cluster(db_session, normal_user, a, b)
    old_uuid = str(old.uuid)
    speaker_os_seams["clusters"][old_uuid] = VE

    with pytest.raises(ValueError, match="Threshold must be in"):
        SpeakerClusteringService(db_session).batch_recluster(normal_user.id, threshold=bad)

    assert speaker_os_seams["deleted_centroids"] == []
    assert old_uuid in speaker_os_seams["clusters"]


def test_an_embedding_of_another_width_is_skipped_not_fatal(
    db_session, normal_user, speaker_os_seams
):
    trio = [_voiced(db_session, normal_user, speaker_os_seams, v) for v in (VE, VF, VG)]
    odd = _voiced(db_session, normal_user, speaker_os_seams, [1.0, 0.0, 0.0])
    db_session.commit()

    result = SpeakerClusteringService(db_session).batch_recluster(normal_user.id)

    cluster = _cluster_of(db_session, trio[0])
    assert cluster is not None and _members(db_session, cluster) == {s.id for s in trio}
    assert _cluster_of(db_session, odd) is None
    assert result["singletons"] == 1
