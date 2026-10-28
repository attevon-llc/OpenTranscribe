"""Cross-file speaker clustering under the privacy policy (#1280).

Clusters are voiceprint-derived and their gender mix is inferred, so both switches reach the
service: a scope with voiceprints off is neither clustered nor reclustered nor listed, and a
scope with gender off contributes no inferred gender to a cluster decision or a cluster
summary. Every case has its "on" control, and the OpenSearch reads and writes go through the
recorders in ``speaker_assignment_seams`` so what was stored or deleted is asserted directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.services.speaker_clustering_service import SpeakerClusteringService
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

VOICE = [1.0, 0.0, 0.0, 0.0]
OTHER = [0.0, 1.0, 0.0, 0.0]


def _speaker(db, user, org, seams, voice=None, **kw) -> Any:
    sp = make_speaker(db, user, file_in(db, user, org), **kw)
    if voice is not None:
        seams["voiceprints"][str(sp.uuid)] = voice
    return sp


def _in_org(db, cluster, org) -> None:
    cluster.organization_id = org.id if org is not None else None
    db.commit()


# ---------------------------------------------------------------------------
# Upload-time clustering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_a_files_speakers_are_clustered_only_when_voiceprints_are_allowed(
    db_session, normal_user, speaker_os_seams, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    sp = _speaker(db_session, normal_user, org, speaker_os_seams, VOICE)
    db_session.commit()

    clusters = SpeakerClusteringService(db_session).cluster_speakers_for_file(
        sp.media_file_id, normal_user.id
    )

    assert len(clusters) == (1 if allowed else 0)
    assert len(speaker_os_seams["clusters"]) == (1 if allowed else 0)
    db_session.expire_all()
    assert (db_session.get(Speaker, sp.id).cluster_id is not None) is allowed


@pytest.mark.parametrize("allowed", [True, False])
def test_a_single_speaker_gets_a_centroid_only_when_voiceprints_are_allowed(
    db_session, normal_user, speaker_os_seams, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    sp = _speaker(db_session, normal_user, org, speaker_os_seams, VOICE)
    db_session.commit()

    cluster = SpeakerClusteringService(db_session).find_or_create_cluster(
        sp.id, normal_user.id, VOICE
    )

    assert (cluster is not None) is allowed
    assert len(speaker_os_seams["clusters"]) == (1 if allowed else 0)


# ---------------------------------------------------------------------------
# Re-cluster: only the scopes that are on are rebuilt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("org_allowed", [True, False])
def test_reclustering_leaves_a_scope_with_voiceprints_off_exactly_as_it_is(
    db_session, normal_user, speaker_os_seams, org_allowed
):
    org = make_org(db_session, voiceprints=org_allowed)
    personal = [_speaker(db_session, normal_user, None, speaker_os_seams, VOICE) for _ in range(2)]
    in_org = [_speaker(db_session, normal_user, org, speaker_os_seams, OTHER) for _ in range(2)]
    personal_cluster = make_cluster(db_session, normal_user, *personal, label="personal")
    org_cluster = make_cluster(db_session, normal_user, *in_org, label="org")
    _in_org(db_session, org_cluster, org)
    speaker_os_seams["clusters"][str(personal_cluster.uuid)] = VOICE
    speaker_os_seams["clusters"][str(org_cluster.uuid)] = OTHER
    org_cluster_uuid, personal_cluster_uuid = str(org_cluster.uuid), str(personal_cluster.uuid)

    result = SpeakerClusteringService(db_session).batch_recluster(normal_user.id)

    # The scope that is on was cleared and rebuilt either way (control).
    assert personal_cluster_uuid in speaker_os_seams["deleted_centroids"]
    assert result["skipped_by_policy_scopes"] == (0 if org_allowed else 1)
    # The scope that is off was neither cleared nor touched.
    db_session.expire_all()
    org_cluster_survives = (
        db_session.query(SpeakerCluster).filter(SpeakerCluster.uuid == org_cluster_uuid).count()
        == 1
    )
    assert org_cluster_survives is (not org_allowed)
    assert (org_cluster_uuid in speaker_os_seams["deleted_centroids"]) is org_allowed
    assert (org_cluster_uuid in speaker_os_seams["clusters"]) is (not org_allowed)


@pytest.mark.parametrize("org_allowed", [True, False])
def test_reclustering_does_not_group_the_speakers_of_a_scope_that_has_no_clusters_yet(
    db_session, normal_user, speaker_os_seams, org_allowed
):
    org = make_org(db_session, voiceprints=org_allowed)
    personal = [_speaker(db_session, normal_user, None, speaker_os_seams, VOICE) for _ in range(2)]
    in_org = [_speaker(db_session, normal_user, org, speaker_os_seams, OTHER) for _ in range(3)]
    make_cluster(db_session, normal_user, *personal, label="personal")  # the user has a cluster
    org_speaker_ids = [sp.id for sp in in_org]  # ...but none in the organization yet

    result = SpeakerClusteringService(db_session).batch_recluster(normal_user.id)

    db_session.expire_all()
    grouped = [db_session.get(Speaker, i).cluster_id is not None for i in org_speaker_ids]
    # Control: with the switch on the same call groups the organization's speakers.
    assert grouped == [org_allowed] * 3
    assert result["skipped_by_policy_scopes"] == (0 if org_allowed else 1)


# ---------------------------------------------------------------------------
# Reads: lists and details
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_the_cluster_list_of_a_scope_with_voiceprints_off_is_empty(
    db_session, normal_user, speaker_os_seams, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    members = [_speaker(db_session, normal_user, org, speaker_os_seams, VOICE) for _ in range(2)]
    cluster = make_cluster(db_session, normal_user, *members)
    _in_org(db_session, cluster, org)

    listing = SpeakerClusteringService(db_session).list_clusters(
        normal_user.id, organization_id=org.id
    )
    detail = SpeakerClusteringService(db_session).get_cluster_detail(
        str(cluster.uuid), normal_user.id, organization_id=org.id
    )

    assert [c["uuid"] for c in listing["items"]] == ([str(cluster.uuid)] if allowed else [])
    assert (detail is not None) is allowed


# ---------------------------------------------------------------------------
# Inferred gender
# ---------------------------------------------------------------------------


def _gendered_cluster(db, user, org, seams, genders: list[tuple[str, bool]]) -> Any:
    members = [
        _speaker(
            db,
            user,
            org,
            seams,
            VOICE if i else OTHER,
            name=f"SPEAKER_{i:02d}",
            predicted_gender=gender,
            gender_confirmed_by_user=confirmed,
        )
        for i, (gender, confirmed) in enumerate(genders)
    ]
    cluster = make_cluster(db, user, *members)
    _in_org(db, cluster, org)
    return cluster, members


@pytest.mark.parametrize(("allowed", "expected"), [(True, "male"), (False, None)])
def test_the_dominant_gender_of_a_cluster_ignores_inferred_gender_while_off(
    db_session, normal_user, speaker_os_seams, allowed, expected
):
    org = make_org(db_session, gender=allowed)
    cluster, _ = _gendered_cluster(
        db_session, normal_user, org, speaker_os_seams, [("male", False), ("male", False)]
    )

    dominant = SpeakerClusteringService(db_session)._get_cluster_dominant_gender(cluster)

    assert dominant == expected


def test_a_confirmed_gender_still_decides_the_dominant_gender_while_off(
    db_session, normal_user, speaker_os_seams
):
    org = make_org(db_session, gender=False)
    cluster, _ = _gendered_cluster(
        db_session,
        normal_user,
        org,
        speaker_os_seams,
        [("male", False), ("male", False), ("female", True)],
    )

    assert SpeakerClusteringService(db_session)._get_cluster_dominant_gender(cluster) == "female"


@pytest.mark.parametrize("allowed", [True, False])
def test_the_cluster_list_counts_inferred_genders_only_while_the_switch_is_on(
    db_session, normal_user, speaker_os_seams, allowed
):
    org = make_org(db_session, gender=allowed)
    cluster, _ = _gendered_cluster(
        db_session, normal_user, org, speaker_os_seams, [("male", False), ("female", False)]
    )

    items = SpeakerClusteringService(db_session).list_clusters(
        normal_user.id, organization_id=org.id
    )["items"]

    composition = next(i for i in items if i["uuid"] == str(cluster.uuid))["gender_composition"]
    assert composition["total_with_gender"] == (2 if allowed else 0)
    assert composition["unknown_count"] == (0 if allowed else 2)


@pytest.mark.parametrize("allowed", [True, False])
def test_the_cluster_detail_shows_a_members_inferred_gender_only_while_the_switch_is_on(
    db_session, normal_user, speaker_os_seams, allowed
):
    org = make_org(db_session, gender=allowed)
    cluster, members = _gendered_cluster(
        db_session, normal_user, org, speaker_os_seams, [("male", False), ("female", True)]
    )
    for m in members:
        m.attribute_confidence = {"gender": 0.9}
    db_session.commit()

    detail = SpeakerClusteringService(db_session).get_cluster_detail(
        str(cluster.uuid), normal_user.id, organization_id=org.id
    )

    assert detail is not None
    by_uuid = {m["speaker_uuid"]: m for m in detail["members"]}
    inferred = by_uuid[str(members[0].uuid)]
    confirmed = by_uuid[str(members[1].uuid)]
    assert inferred["predicted_gender"] == ("male" if allowed else None)
    assert inferred["gender_confidence"] == (0.9 if allowed else None)
    # A person-confirmed gender is theirs and shows either way.
    assert confirmed["predicted_gender"] == "female"
    assert detail["gender_composition"]["total_with_gender"] == (2 if allowed else 1)


@pytest.mark.parametrize("allowed", [True, False])
def test_the_gender_outlier_analysis_has_nothing_to_analyse_while_the_switch_is_off(
    db_session, normal_user, speaker_os_seams, monkeypatch, allowed
):
    org = make_org(db_session, gender=allowed)
    cluster, members = _gendered_cluster(
        db_session,
        normal_user,
        org,
        speaker_os_seams,
        [("male", False), ("male", False), ("female", False)],
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embeddings_batch",
        lambda uuids: {u: speaker_os_seams["voiceprints"][u] for u in uuids},
    )

    result = SpeakerClusteringService(db_session).analyze_gender_outliers(
        str(cluster.uuid), normal_user.id, organization_id=org.id
    )

    assert [a["speaker_uuid"] for a in result["minority_analysis"]] == (
        [str(members[2].uuid)] if allowed else []
    )
    assert result["majority_gender"] == ("male" if allowed else "unknown")
