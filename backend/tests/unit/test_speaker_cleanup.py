"""The shared speaker cleanup leaves no copy of a removed voice in any aggregate (#1214).

``speaker_cleanup`` is the one implementation every speaker-removal path calls: after a
speaker is deleted or leaves a cluster or profile, every cluster centroid and profile
embedding that averaged it in is rebuilt from the survivors, an emptied cluster is
deleted with its centroid, and an aggregate that cannot be rebuilt is erased rather than
left stale. These tests drive it, and the service-layer paths that use it, against real
Postgres rows; only the OpenSearch seams are replaced (``speaker_os_seams``), recording
what was written and erased, so the assertions are on outcomes, not on calls.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfile
from app.services import speaker_cleanup as sc
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


def _remove(db, speaker):
    """Capture, delete and commit — the order every removal path follows."""
    impact = sc.capture_speaker_impact(db, [speaker.id], removed=True, reindex=True)
    db.delete(speaker)
    db.commit()
    return impact


# ------------------------------------------------------------------------- the helper itself


def test_capture_reads_both_membership_records_and_only_the_owners_clusters(
    db_session, normal_user, other_user, speaker_os_seams
):
    fa = make_file(db_session, normal_user)
    sa = make_speaker(db_session, normal_user, fa)
    via_member = make_cluster(db_session, normal_user, sa)
    via_fk = SpeakerCluster(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, member_count=0)
    theirs = SpeakerCluster(uuid=uuid_pkg.uuid4(), user_id=other_user.id, member_count=0)
    db_session.add_all([via_fk, theirs])
    db_session.flush()
    db_session.add(SpeakerClusterMember(cluster_id=theirs.id, speaker_id=sa.id))
    sa.cluster_id = via_fk.id
    db_session.commit()

    impact = sc.capture_speaker_impact(db_session, [sa.id], removed=True, reindex=True)

    assert impact.cluster_uuids == {str(via_member.uuid), str(via_fk.uuid)}
    assert impact.erase_speaker_uuids == {str(sa.uuid)}
    assert impact.reindex_file_ids == {fa.id}
    assert impact.owner_ids == {normal_user.id}


def test_a_removed_speaker_leaves_the_centroid_and_its_voiceprint(
    db_session, normal_user, speaker_os_seams
):
    fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    cluster_id, victim = cluster.id, str(sa.uuid)
    impact = _remove(db_session, sa)

    residual = sc.reconcile_speaker_impact(db_session, impact)

    assert residual == []
    assert cosine(speaker_os_seams["clusters"][str(cluster.uuid)], VOICE_B) >= MATCH
    assert speaker_os_seams["erased_speakers"] == [victim]
    assert speaker_os_seams["reindexed"] == [fa.id]
    db_session.expire_all()
    row = db_session.get(SpeakerCluster, cluster_id)
    # One member left: kept, as upload-time clustering keeps singletons for matching.
    assert row.member_count == 1
    assert row.representative_speaker_id == sb.id


def test_an_emptied_cluster_is_deleted_with_its_centroid_even_when_promoted(
    db_session, normal_user, speaker_os_seams
):
    fa = make_file(db_session, normal_user)
    sa = make_speaker(db_session, normal_user, fa)
    speaker_os_seams["voiceprints"][str(sa.uuid)] = VOICE_A
    profile = make_profile(db_session, normal_user)
    cluster = make_cluster(db_session, normal_user, sa, promoted_to=profile.id)
    profile.source_cluster_id = cluster.id
    db_session.commit()
    cluster_id, cluster_uuid, profile_id = cluster.id, str(cluster.uuid), profile.id
    impact = _remove(db_session, sa)

    assert sc.reconcile_speaker_impact(db_session, impact) == []

    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id) is None
    assert speaker_os_seams["erased_clusters"] == [cluster_uuid]
    survivor = db_session.get(SpeakerProfile, profile_id)
    assert survivor is not None and survivor.source_cluster_id is None


@pytest.mark.parametrize("why", ["unreadable-survivor", "refused-write"])
def test_a_centroid_that_cannot_be_rebuilt_is_erased_not_kept(
    db_session, normal_user, speaker_os_seams, why
):
    _fa, _fb, sa, sb, cluster = make_pair(db_session, normal_user, speaker_os_seams)
    if why == "unreadable-survivor":
        speaker_os_seams["voiceprints"].pop(str(sb.uuid))
    else:
        speaker_os_seams["store_ok"] = False
    impact = _remove(db_session, sa)

    assert sc.reconcile_speaker_impact(db_session, impact) == []
    assert speaker_os_seams["erased_clusters"] == [str(cluster.uuid)]
    assert str(cluster.uuid) not in speaker_os_seams["clusters"]


def test_a_centroid_neither_rebuilt_nor_erased_is_reported_with_a_fixed_message(
    db_session, normal_user, speaker_os_seams
):
    _fa, _fb, sa, _sb, _cluster = make_pair(db_session, normal_user, speaker_os_seams)
    speaker_os_seams["store_ok"] = False
    speaker_os_seams["erase_error"] = "ConnectionError: opensearch:9200 refused"
    impact = _remove(db_session, sa)

    residual = sc.reconcile_speaker_impact(db_session, impact)

    assert {r["stage"] for r in residual} == {"speakers", "clusters"}
    with pytest.raises(sc.SpeakerCleanupIncompleteError) as excinfo:
        sc.raise_if_incomplete(residual)
    assert excinfo.value.message == sc.CLEANUP_INCOMPLETE_MESSAGE
    assert "opensearch" not in excinfo.value.message.lower()
    assert excinfo.value.stages == ["clusters", "speakers"]
    sc.raise_if_incomplete([])  # nothing incomplete: nothing raised


def test_another_users_cluster_is_never_touched(
    db_session, normal_user, other_user, speaker_os_seams
):
    _fa, _fb, sa, _sb, own = make_pair(db_session, normal_user, speaker_os_seams)
    fo = make_file(db_session, other_user)
    so = make_speaker(db_session, other_user, fo)
    speaker_os_seams["voiceprints"][str(so.uuid)] = VOICE_C
    theirs = make_cluster(db_session, other_user, so)
    impact = _remove(db_session, sa)
    impact.cluster_uuids.add(str(theirs.uuid))  # a stale impact naming another's cluster

    assert sc.reconcile_speaker_impact(db_session, impact) == []
    assert set(speaker_os_seams["clusters"]) == {str(own.uuid)}


def test_a_profile_is_rebuilt_from_its_remaining_members(db_session, normal_user, speaker_os_seams):
    profile = make_profile(db_session, normal_user)
    _fa, _fb, sa, sb, _cl = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id = sb.profile_id = profile.id
    db_session.commit()
    impact = _remove(db_session, sa)

    assert sc.reconcile_speaker_impact(db_session, impact) == []
    assert cosine(speaker_os_seams["profiles"][str(profile.uuid)], VOICE_B) >= MATCH


@pytest.mark.parametrize("why", ["no-members", "refused-write"])
def test_a_profile_that_cannot_be_rebuilt_is_erased(db_session, normal_user, speaker_os_seams, why):
    profile = make_profile(db_session, normal_user)
    _fa, _fb, sa, sb, _cl = make_pair(db_session, normal_user, speaker_os_seams)
    sa.profile_id = profile.id
    if why == "refused-write":
        sb.profile_id = profile.id
        speaker_os_seams["profile_store_ok"] = False
    profile.embedding_count = 2
    db_session.commit()
    profile_id, profile_uuid = profile.id, str(profile.uuid)
    impact = _remove(db_session, sa)

    assert sc.reconcile_speaker_impact(db_session, impact) == []
    assert speaker_os_seams["erased_profiles"] == [profile_uuid]
    db_session.expire_all()
    assert db_session.get(SpeakerProfile, profile_id).embedding_count == 0


class _ClaimStore:
    """SET NX semantics for the reindex debounce claim; ``claimed`` pre-takes the key."""

    def __init__(self, claimed: bool) -> None:
        self.keys: set[str] = set()
        self.claimed = claimed

    def set(self, key, value, nx=False, ex=None):
        if self.claimed or (nx and key in self.keys):
            return None
        self.keys.add(key)
        return True

    def delete(self, key) -> int:
        self.keys.discard(key)
        return 1


@pytest.mark.parametrize("state", ["publish-fails", "already-pending"])
def test_a_reindex_that_could_not_be_queued_is_a_residual(
    db_session, normal_user, monkeypatch, state
):
    """Item 5 of #1233: the real dispatch path, its broker publish failing (or the debounce
    window already holding the reindex). A failure is a residual; already-pending is not."""
    from unittest.mock import MagicMock

    media_file = make_file(db_session, normal_user)
    db_session.commit()
    task = MagicMock()
    task.apply_async.side_effect = RuntimeError("broker unreachable")
    monkeypatch.setattr(
        "app.core.redis.get_redis", lambda: _ClaimStore(claimed=state == "already-pending")
    )
    monkeypatch.setattr("app.tasks.search_indexing_task.index_transcript_search_task", task)

    residual = sc.reconcile_speaker_impact(
        db_session,
        sc.SpeakerImpact(owner_ids={normal_user.id}, reindex_file_ids={media_file.id}),
    )

    if state == "publish-fails":
        assert [r["stage"] for r in residual] == [sc.STAGE_INDEX]
        task.apply_async.assert_called_once()
    else:
        assert residual == []
        task.apply_async.assert_not_called()
