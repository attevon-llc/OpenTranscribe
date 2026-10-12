"""Unassigning a speaker is a correction no assignment path may silently undo (#1233).

The rule: unassigning a speaker from a cluster means that section is no longer that
person. It leaves the cluster AND the cluster's promoted profile (a name that came from
the profile is cleared, a name the user typed stays), the blacklist is recorded both
speaker-to-speaker (cannot-link against every remaining member) and speaker-to-profile,
and the cluster and its profile remain for everyone else with their centroid and
embedding rebuilt from the survivors.

Each assignment path then gets the same test: unassign, re-run the path, and the speaker
must still be unassigned with its blacklist rows intact — plus a must-fire control, the
same path assigning a speaker that carries no correction, so a path that assigns nothing
at all cannot pass. Real Postgres rows; OpenSearch reads and writes answered by the
recorders in ``speaker_os_seams`` / ``speaker_assignment_seams`` with real cosine math.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from typing import Any

import numpy as np
import pytest

from app.models.media import Speaker
from app.models.media import SpeakerCannotLink
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfileBlacklist
from app.models.media import TranscriptSegment
from tests.fixtures.speaker_cleanup_seams import cosine
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

MATCH = 0.99999
NAME = "Dana"


def _unit(v: list[float]) -> list[float]:
    a = np.array(v, dtype=np.float64)
    unit: list[float] = (a / np.linalg.norm(a)).tolist()
    return unit


# Four L2-normalized voices of one person (pairwise cosine 0.92-0.96, every one above the
# 0.75 assignment bar; C and D a little off-centre so a cannot-link eviction is
# deterministic) and one of somebody else.
VA = _unit([1.0, 0.2, 0.0, 0.0])
VB = _unit([1.0, 0.0, 0.2, 0.0])
VC = _unit([1.0, 0.0, 0.0, 0.3])
VD = _unit([1.0, 0.0, 0.0, 0.35])
VX = _unit([0.0, 0.0, 0.0, 1.0])


def _mean(*voices: list[float]) -> list[float]:
    """Equal-weight mean: every member below carries a profile link (or none does)."""
    mean: list[float] = np.mean([np.array(v) for v in voices], axis=0).tolist()
    return mean


def _service(db):
    from app.services.speaker_clustering_service import SpeakerClusteringService

    return SpeakerClusteringService(db)


def _voiced(db, user, seams, voice, *, name=NAME, linked_to=None, display=None) -> Any:
    sp = make_speaker(db, user, make_file(db, user))
    seams["voiceprints"][str(sp.uuid)] = voice
    if linked_to is not None:
        sp.profile_id, sp.verified = linked_to.id, True
        sp.display_name = display if display is not None else name
    elif display is not None:
        sp.display_name = display
    db.flush()
    return sp


def _trio(db, user, seams, *, promoted=True, c_display=None) -> dict[str, Any]:
    """A, B, C: one person's voice; one cluster over the three, promoted to ``Dana``."""
    profile = make_profile(db, user, NAME) if promoted else None
    sa = _voiced(db, user, seams, VA, linked_to=profile)
    sb = _voiced(db, user, seams, VB, linked_to=profile)
    sc = _voiced(db, user, seams, VC, linked_to=profile, display=c_display)
    cluster = make_cluster(
        db, user, sa, sb, sc, promoted_to=profile.id if profile else None, label=NAME
    )
    seams["clusters"][str(cluster.uuid)] = _mean(VA, VB, VC)
    if profile is not None:
        seams["profiles"][str(profile.uuid)] = _mean(VA, VB, VC)
    return {"profile": profile, "a": sa, "b": sb, "c": sc, "cluster": cluster}


def _unassign_c(db, user, trio) -> None:
    _service(db).unassign_speakers(
        str(trio["cluster"].uuid), [str(trio["c"].uuid)], user.id, blacklist=True
    )
    db.expire_all()


def _assert_c_stays_unassigned(db, trio) -> None:
    """The correction survived: no membership, no profile link, every blacklist row."""
    c, a, b = (db.get(Speaker, trio[k].id) for k in ("c", "a", "b"))
    cluster_id = trio["cluster"].id
    assert c.cluster_id != cluster_id, "C is back in the cluster it was unassigned from"
    assert (
        db.query(SpeakerClusterMember)
        .filter(
            SpeakerClusterMember.cluster_id == cluster_id, SpeakerClusterMember.speaker_id == c.id
        )
        .count()
        == 0
    ), "C has a membership row in the cluster it was unassigned from"
    pairs = {
        (int(r.speaker_id), int(r.cannot_link_speaker_id))
        for r in db.query(SpeakerCannotLink).filter(
            (SpeakerCannotLink.speaker_id == c.id)
            | (SpeakerCannotLink.cannot_link_speaker_id == c.id)
        )
    }
    assert {(c.id, a.id), (a.id, c.id), (c.id, b.id), (b.id, c.id)} <= pairs, pairs
    if trio["profile"] is not None:
        assert c.profile_id != trio["profile"].id, "C is linked to the profile again"
        assert (
            db.query(SpeakerProfileBlacklist)
            .filter(
                SpeakerProfileBlacklist.speaker_id == c.id,
                SpeakerProfileBlacklist.profile_id == trio["profile"].id,
            )
            .count()
            == 1
        ), "the speaker-to-profile blacklist row is missing"


# --------------------------------------------------------------------------- the unassign rule


def test_unassign_from_a_promoted_cluster_unlinks_and_blacklists_both_ways(
    db_session, normal_user, speaker_os_seams
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    c_uuid = str(trio["c"].uuid)

    _unassign_c(db_session, normal_user, trio)

    c = db_session.get(Speaker, trio["c"].id)
    assert (c.profile_id, c.display_name, c.verified) == (None, None, False), (
        "a name that came from the profile must go with the profile link"
    )
    _assert_c_stays_unassigned(db_session, trio)
    # The cluster and profile remain for the others, rebuilt from them alone.
    cluster = db_session.get(SpeakerCluster, trio["cluster"].id)
    assert cluster is not None and cluster.member_count == 2
    assert cluster.promoted_to_profile_id == trio["profile"].id
    assert {s.profile_id for s in (trio["a"], trio["b"])} == {trio["profile"].id}
    survivors = _mean(VA, VB)
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], survivors) >= MATCH
    assert cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], survivors) >= MATCH
    # Control on the math: the stored aggregates no longer equal the three-voice mean.
    assert cosine(survivors, _mean(VA, VB, VC)) < MATCH
    assert speaker_os_seams["doc_profiles"].get(c_uuid, "unset") is None, (
        "the unlinked speaker's own voiceprint document still names the profile"
    )


def test_a_name_the_user_typed_survives_the_unassign(db_session, normal_user, speaker_os_seams):
    trio = _trio(db_session, normal_user, speaker_os_seams, c_display="Robert")

    _unassign_c(db_session, normal_user, trio)

    c = db_session.get(Speaker, trio["c"].id)
    assert c.display_name == "Robert", "a user-typed name is not the profile's to take back"
    assert c.profile_id is None
    _assert_c_stays_unassigned(db_session, trio)


def test_a_suggestion_of_the_profile_name_is_taken_back_too(
    db_session, normal_user, speaker_os_seams
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    trio["c"].suggested_name, trio["c"].confidence = NAME, 0.92
    db_session.commit()

    _unassign_c(db_session, normal_user, trio)

    c = db_session.get(Speaker, trio["c"].id)
    assert (c.suggested_name, c.confidence) == (None, None), (
        "a confident suggestion of the profile's name would still label C as Dana"
    )


def test_unassigning_from_an_unpromoted_cluster_keeps_an_unrelated_profile_link(
    db_session, normal_user, speaker_os_seams
):
    elsewhere = make_profile(db_session, normal_user, "Elsewhere")
    trio = _trio(db_session, normal_user, speaker_os_seams, promoted=False)
    trio["c"].profile_id, trio["c"].display_name = elsewhere.id, "Elsewhere"
    db_session.commit()

    _unassign_c(db_session, normal_user, trio)

    c = db_session.get(Speaker, trio["c"].id)
    assert (c.profile_id, c.display_name) == (elsewhere.id, "Elsewhere"), (
        "only the CLUSTER's identity is taken back; a link the cluster never gave stays"
    )
    _assert_c_stays_unassigned(db_session, trio)


def test_split_from_a_promoted_cluster_is_an_unassignment(
    db_session, normal_user, speaker_os_seams
):
    trio = _trio(db_session, normal_user, speaker_os_seams)

    new = _service(db_session).split_cluster(
        str(trio["cluster"].uuid), [str(trio["c"].uuid)], normal_user.id
    )

    assert new is not None
    db_session.expire_all()
    assert db_session.get(Speaker, trio["c"].id).cluster_id == new.id
    _assert_c_stays_unassigned(db_session, trio)
    assert cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], _mean(VA, VB)) >= MATCH


# --------------------------------------------------------------------------- every assignment path


def test_upload_time_clustering_honours_the_blacklist(db_session, normal_user, speaker_os_seams):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)

    clusters = _service(db_session).cluster_speakers_for_file(
        trio["c"].media_file_id, normal_user.id
    )

    db_session.expire_all()
    # The path ran (C was clustered) and put C in a cluster of its own, not the profile's.
    assert [cl.id for cl in clusters] == [db_session.get(Speaker, trio["c"].id).cluster_id]
    _assert_c_stays_unassigned(db_session, trio)


def test_control_upload_time_clustering_links_and_rebuilds_the_profile(
    db_session, normal_user, speaker_os_seams
):
    """Must-fire control, and item 4: joining a promoted cluster updates the PROFILE too."""
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD)
    db_session.commit()

    _service(db_session).cluster_speakers_for_file(d.media_file_id, normal_user.id)

    db_session.expire_all()
    d = db_session.get(Speaker, d.id)
    assert d.cluster_id == trio["cluster"].id and d.profile_id == trio["profile"].id
    assert (d.display_name, d.verified) == (NAME, True)
    expected = _mean(VA, VB, VD)
    assert cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], expected) >= MATCH, (
        "a voice joined the profile but the profile embedding never heard it"
    )
    assert cosine(speaker_os_seams["clusters"][str(trio["cluster"].uuid)], expected) >= MATCH


def test_joining_a_cluster_rebuilds_the_cluster_a_stale_membership_is_dropped_from(
    db_session, normal_user, speaker_os_seams
):
    """Item 2: membership rows exist but ``cluster_id`` is null; the dropped cluster rebuilds."""
    e = _voiced(db_session, normal_user, speaker_os_seams, VA)
    f = _voiced(db_session, normal_user, speaker_os_seams, VX)
    g = _voiced(db_session, normal_user, speaker_os_seams, VA)
    stale = make_cluster(db_session, normal_user, e, f, label=None)
    home = make_cluster(db_session, normal_user, g, label=None)
    e.cluster_id = None
    db_session.commit()
    speaker_os_seams["clusters"][str(stale.uuid)] = _mean(VA, VX)
    speaker_os_seams["clusters"][str(home.uuid)] = list(VA)

    _service(db_session).cluster_speakers_for_file(e.media_file_id, normal_user.id)

    db_session.expire_all()
    assert db_session.get(Speaker, e.id).cluster_id == home.id
    kept = db_session.get(SpeakerCluster, stale.id)
    assert kept is not None and kept.member_count == 1
    assert cosine(speaker_os_seams["clusters"][str(stale.uuid)], VX) >= MATCH, (
        "the cluster the speaker was dropped from still averages its voice"
    )


def test_recluster_unlinks_a_stale_link_the_user_already_unassigned(
    db_session, normal_user, speaker_os_seams
):
    """Data written before #1233: blacklisted from P but still linked to P."""
    trio = _trio(db_session, normal_user, speaker_os_seams)
    db_session.add(SpeakerProfileBlacklist(speaker_id=trio["c"].id, profile_id=trio["profile"].id))
    db_session.query(SpeakerClusterMember).filter(
        SpeakerClusterMember.speaker_id == trio["c"].id
    ).delete()
    trio["c"].cluster_id = None
    db_session.commit()

    _service(db_session).batch_recluster(normal_user.id)

    db_session.expire_all()
    c = db_session.get(Speaker, trio["c"].id)
    assert c.profile_id is None and c.display_name is None
    profile_cluster = (
        db_session.query(SpeakerCluster)
        .filter(SpeakerCluster.promoted_to_profile_id == trio["profile"].id)
        .one()
    )
    members = {
        int(m.speaker_id)
        for m in db_session.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.cluster_id == profile_cluster.id
        )
    }
    assert members == {trio["a"].id, trio["b"].id}, "re-cluster re-adopted the unassigned speaker"
    assert cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], _mean(VA, VB)) >= MATCH


def test_recluster_profile_matching_honours_the_blacklist(
    db_session, normal_user, speaker_os_seams
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)

    result = _service(db_session).batch_recluster(normal_user.id)

    assert result["status"] == "completed", result
    db_session.expire_all()
    c = db_session.get(Speaker, trio["c"].id)
    assert c.profile_id is None
    profile_cluster = (
        db_session.query(SpeakerCluster)
        .filter(SpeakerCluster.promoted_to_profile_id == trio["profile"].id)
        .one()
    )
    assert c.cluster_id != profile_cluster.id, "Phase 2.5 matched C back into the profile"


def test_control_recluster_profile_matching_links_a_matching_singleton(
    db_session, normal_user, speaker_os_seams
):
    profile = make_profile(db_session, normal_user, NAME)
    a = _voiced(db_session, normal_user, speaker_os_seams, VA, linked_to=profile)
    b = _voiced(db_session, normal_user, speaker_os_seams, VB, linked_to=profile)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD)
    db_session.commit()

    _service(db_session).batch_recluster(normal_user.id)

    db_session.expire_all()
    d = db_session.get(Speaker, d.id)
    profile_cluster = (
        db_session.query(SpeakerCluster)
        .filter(SpeakerCluster.promoted_to_profile_id == profile.id)
        .one()
    )
    assert d.cluster_id == profile_cluster.id
    assert (d.profile_id, d.display_name) == (profile.id, NAME), (
        "joining a profile's cluster must take the profile, as upload-time matching does"
    )
    expected = _mean(VA, VB, VD)
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], expected) >= MATCH
    assert cosine(speaker_os_seams["clusters"][str(profile_cluster.uuid)], expected) >= MATCH
    assert {a.profile_id, b.profile_id} == {profile.id}


@pytest.mark.parametrize("unassign", [True, False], ids=["unassigned", "control"])
def test_recluster_grouping_honours_the_cannot_link(
    db_session, normal_user, speaker_os_seams, unassign
):
    trio = _trio(db_session, normal_user, speaker_os_seams, promoted=False)
    if unassign:
        _unassign_c(db_session, normal_user, trio)

    _service(db_session).batch_recluster(normal_user.id)

    db_session.expire_all()
    a, b, c = (db_session.get(Speaker, trio[k].id) for k in ("a", "b", "c"))
    assert a.cluster_id is not None and a.cluster_id == b.cluster_id
    if unassign:
        assert c.cluster_id != a.cluster_id, "AHC regrouped a cannot-linked speaker"
    else:
        assert c.cluster_id == a.cluster_id, "control: one voice must form one group"


def _retroactive(db, monkeypatch, labelled) -> dict[str, int]:
    from app.api.endpoints import speaker_update

    monkeypatch.setattr(speaker_update, "_send_bulk_update_notification", lambda *a: None)
    return speaker_update.trigger_retroactive_matching(labelled, db)


def test_retroactive_matching_honours_the_blacklist(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)

    _retroactive(db_session, monkeypatch, db_session.get(Speaker, trio["a"].id))

    db_session.expire_all()
    c = db_session.get(Speaker, trio["c"].id)
    assert c.display_name is None and c.suggested_name is None, (
        "retroactive matching re-labelled (or re-suggested) the corrected speaker"
    )
    _assert_c_stays_unassigned(db_session, trio)


def test_control_retroactive_matching_links_an_uncorrected_match(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD)
    db_session.commit()

    result = _retroactive(db_session, monkeypatch, db_session.get(Speaker, trio["a"].id))

    db_session.expire_all()
    d = db_session.get(Speaker, d.id)
    assert result["auto_applied_count"] >= 1
    assert (d.profile_id, d.display_name) == (trio["profile"].id, NAME)
    assert (
        cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], _mean(VA, VB, VD)) >= MATCH
    )


@pytest.mark.parametrize("action", ["assign", "accept"])
def test_batch_verify_refuses_a_corrected_speaker_and_says_so(
    db_session, normal_user, speaker_os_seams, action
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD)
    if action == "accept":
        # A stale suggestion written after the unassign by any suggestion source.
        for sp in (db_session.get(Speaker, trio["c"].id), d):
            sp.suggested_name, sp.confidence = NAME, 0.9
    db_session.commit()

    result = _service(db_session).batch_verify_speakers(
        [str(trio["c"].uuid), str(d.uuid)],
        normal_user.id,
        action=action,
        profile_uuid=str(trio["profile"].uuid) if action == "assign" else None,
    )

    assert (result["updated_count"], result["failed_count"]) == (1, 1), result
    assert any(str(trio["c"].uuid) in e for e in result["errors"]), result
    db_session.expire_all()
    c = db_session.get(Speaker, trio["c"].id)
    assert c.display_name is None
    _assert_c_stays_unassigned(db_session, trio)
    # Control in the same batch: the uncorrected speaker WAS named.
    assert db_session.get(Speaker, d.id).display_name == NAME


def test_promotion_does_not_reach_a_speaker_unassigned_before_it(
    db_session, normal_user, speaker_os_seams
):
    trio = _trio(db_session, normal_user, speaker_os_seams, promoted=False)
    _unassign_c(db_session, normal_user, trio)

    profile = _service(db_session).promote_cluster_to_profile(
        str(trio["cluster"].uuid), "Promoted", normal_user.id
    )

    assert profile is not None
    db_session.expire_all()
    assert db_session.get(Speaker, trio["c"].id).profile_id is None
    assert db_session.get(Speaker, trio["a"].id).profile_id == profile.id, "control"
    _assert_c_stays_unassigned(db_session, trio)


def test_merging_clusters_leaves_a_corrected_member_behind_and_links_the_rest(
    db_session, normal_user, speaker_os_seams
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    e = _voiced(db_session, normal_user, speaker_os_seams, VD)
    c = db_session.get(Speaker, trio["c"].id)
    source = make_cluster(db_session, normal_user, c, e, label=None)
    source_id = source.id
    service = _service(db_session)

    merged = service.merge_clusters(str(source.uuid), str(trio["cluster"].uuid), normal_user.id)

    assert merged is not None
    assert service.blocked_speaker_uuids == [str(c.uuid)]
    db_session.expire_all()
    assert db_session.get(Speaker, c.id).cluster_id == source_id, "C must stay where it was"
    assert db_session.get(SpeakerCluster, source_id).member_count == 1
    _assert_c_stays_unassigned(db_session, trio)
    e = db_session.get(Speaker, e.id)
    assert (e.cluster_id, e.profile_id, e.display_name) == (
        trio["cluster"].id,
        trio["profile"].id,
        NAME,
    ), "control: the uncorrected member moved and took the target's identity"
    expected = _mean(VA, VB, VD)
    assert cosine(speaker_os_seams["clusters"][str(trio["cluster"].uuid)], expected) >= MATCH
    assert cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], expected) >= MATCH


def _matching_service(db):
    from app.services.speaker_matching_service import SpeakerMatchingService

    return SpeakerMatchingService(db, None)


def test_profile_propagation_honours_the_blacklist(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    trio = _trio(db_session, normal_user, speaker_os_seams, c_display="Robert")
    _unassign_c(db_session, normal_user, trio)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD, display="Danny")
    db_session.commit()
    svc = _matching_service(db_session)
    monkeypatch.setattr(
        svc, "_get_speaker_embedding_for_propagation", lambda sid: (VA, str(trio["a"].uuid))
    )

    svc._propagate_profile_assignment(trio["a"].id, trio["profile"].id, normal_user.id)

    db_session.expire_all()
    _assert_c_stays_unassigned(db_session, trio)
    # Control: the uncorrected look-alike was linked, and the profile heard it.
    assert db_session.get(Speaker, d.id).profile_id == trio["profile"].id
    assert (
        cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], _mean(VA, VB, VD)) >= MATCH
    )


def test_a_re_matched_speaker_is_not_linked_to_a_profile_it_was_unassigned_from(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD)
    db_session.commit()
    svc = _matching_service(db_session)
    monkeypatch.setattr(svc, "find_and_store_speaker_matches", lambda *a, **k: [])
    monkeypatch.setattr(svc, "_get_speaker_embedding_for_propagation", lambda sid: (None, None))
    monkeypatch.setattr(
        "app.services.opensearch_service.get_profile_embedding",
        lambda u: speaker_os_seams["profiles"].get(str(u)),
    )
    # Imported by name at module scope there.
    monkeypatch.setattr(
        "app.services.speaker_matching_service.add_speaker_embedding", lambda **kw: True
    )
    match = {
        "profile_id": trio["profile"].id,
        "suggested_name": NAME,
        "confidence": 0.9,
        "auto_accept": True,
    }

    for speaker_id in (trio["c"].id, d.id):
        sp = db_session.get(Speaker, speaker_id)
        voice = np.array(speaker_os_seams["voiceprints"][str(sp.uuid)])
        svc._handle_speaker_match(sp, dict(match), voice, normal_user.id, sp.media_file_id)
    db_session.commit()

    db_session.expire_all()
    _assert_c_stays_unassigned(db_session, trio)
    c = db_session.get(Speaker, trio["c"].id)
    assert c.display_name is None and c.suggested_name is None
    assert db_session.get(Speaker, d.id).profile_id == trio["profile"].id, "control"


def test_segment_reassignment_rebuilds_the_drifted_aggregates_and_relinks_nothing(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """Item 3: the target's voiceprint drifts; every centroid and profile averaging it follows."""
    from app.tasks import speaker_embedding_task as seb

    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    a = db_session.get(Speaker, trio["a"].id)
    seg = TranscriptSegment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=a.media_file_id,
        speaker_id=a.id,
        start_time=0.0,
        end_time=2.0,
        text="hello",
    )
    db_session.add(seg)
    db_session.commit()
    pushed = [0.0, 1.0, 0.0, 0.0]

    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(seb, "session_scope", scope)
    monkeypatch.setattr(seb, "_extract_segment_embedding", lambda plan: np.array(pushed))

    result = seb.update_speaker_embedding_on_reassignment.apply(
        args=[str(seg.uuid), "unused", str(a.uuid), None, normal_user.id]
    ).get()

    assert result["status"] == "success", result
    new_a = _unit(((np.array(VA) + np.array(pushed)) / 2).tolist())
    assert cosine(speaker_os_seams["voiceprints"][str(a.uuid)], new_a) >= MATCH
    expected = _mean(new_a, VB)
    assert cosine(speaker_os_seams["clusters"][str(trio["cluster"].uuid)], expected) >= MATCH, (
        "the cluster centroid still averages the voiceprint from before the reassignment"
    )
    assert cosine(speaker_os_seams["profiles"][str(trio["profile"].uuid)], expected) >= MATCH
    db_session.expire_all()
    _assert_c_stays_unassigned(db_session, trio)


def test_an_llm_suggestion_never_brings_back_a_name_the_user_unassigned(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """A >=0.75 suggestion moves the canonical label, so it is a re-label, not a hint."""
    from app.tasks import speaker_identification_task as sit

    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    d = _voiced(db_session, normal_user, speaker_os_seams, VD)
    db_session.commit()

    @contextlib.contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(sit, "session_scope", scope)
    for speaker_id in (trio["c"].id, d.id):
        sp = db_session.get(Speaker, speaker_id)
        prediction = {"speaker_label": sp.name, "predicted_name": NAME, "confidence": 0.9}
        sit._store_speaker_predictions(sp.media_file_id, {"speaker_predictions": [prediction]})

    db_session.expire_all()
    assert db_session.get(Speaker, trio["c"].id).suggested_name is None
    assert db_session.get(Speaker, d.id).suggested_name == NAME, "control"


# --------------------------------------------------------------------------- the explicit override


def test_typing_the_name_on_one_speaker_overrides_the_correction(
    db_session, normal_user, speaker_os_seams
):
    from app.api.endpoints.speaker_update import auto_create_or_assign_profile

    trio = _trio(db_session, normal_user, speaker_os_seams)
    _unassign_c(db_session, normal_user, trio)
    c = db_session.get(Speaker, trio["c"].id)
    c.display_name = NAME

    assert auto_create_or_assign_profile(c, NAME, db_session) is True
    db_session.commit()

    db_session.expire_all()
    c = db_session.get(Speaker, c.id)
    assert c.profile_id == trio["profile"].id
    assert (
        db_session.query(SpeakerProfileBlacklist)
        .filter(SpeakerProfileBlacklist.speaker_id == c.id)
        .count()
        == 0
    ), "the user's explicit naming must lift their own earlier correction"
    assert (
        db_session.query(SpeakerCannotLink)
        .filter(
            SpeakerCannotLink.speaker_id == c.id,
            SpeakerCannotLink.cannot_link_speaker_id.in_([trio["a"].id, trio["b"].id]),
        )
        .count()
        == 0
    ), "a cannot-link against the profile's own speakers would keep C out of its cluster"
