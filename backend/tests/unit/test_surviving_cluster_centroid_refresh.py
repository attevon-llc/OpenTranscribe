"""A purge must rebuild the centroid of every cluster that lost a member but kept others.

A centroid is the mean of its members' voiceprints. ``purge_media_file`` erased the deleted
speakers' own voiceprints and the clusters it emptied, but a cluster that kept another
member kept a ``cluster_<uuid>`` document that still averaged the deleted voiceprint in
(issue #1211). These tests drive the real ``purge_media_file`` against real Postgres rows
and replace only the OpenSearch seams, so they assert on what the purge decided to write
and erase, and on the DB state it left, not on the call sequence.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import numpy as np
import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfile
from app.services import file_cleanup_service as fcs

VOICE_A = [1.0, 0.0, 0.0, 0.0]
VOICE_B = [0.0, 1.0, 0.0, 0.0]
VOICE_C = [0.0, 0.0, 1.0, 0.0]


def _file(db, user) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="x.wav",
        user_id=user.id,
        storage_path=f"user_{user.id}/x.wav",
        file_size=10,
        content_type="audio/wav",
        status=FileStatus.COMPLETED,
    )
    db.add(mf)
    db.flush()
    return mf


def _speaker(db, user, media_file) -> Speaker:
    sp = Speaker(uuid=uuid_pkg.uuid4(), name="S", user_id=user.id, media_file_id=media_file.id)
    db.add(sp)
    db.flush()
    return sp


def _cluster(db, user, *speakers, promoted_to=None) -> SpeakerCluster:
    cl = SpeakerCluster(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        label="c",
        member_count=len(speakers),
        promoted_to_profile_id=promoted_to,
    )
    db.add(cl)
    db.flush()
    for sp in speakers:
        db.add(SpeakerClusterMember(cluster_id=cl.id, speaker_id=sp.id, confidence=0.9))
    db.commit()
    return cl


@pytest.fixture
def os_seams(monkeypatch):
    """Replace every external store the purge touches; record what it writes and erases."""
    rec: dict[str, Any] = {"stored": {}, "erased": [], "voiceprints": {}, "store_ok": True}

    monkeypatch.setattr(fcs, "_purge_external_copies", lambda plan: [])
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embedding",
        lambda speaker_uuid: rec["voiceprints"].get(str(speaker_uuid)),
    )

    def fake_store(cluster_uuid, user_id, embedding, **kwargs):
        if rec["store_ok"]:
            rec["stored"][str(cluster_uuid)] = (user_id, list(embedding))
        return rec["store_ok"]

    monkeypatch.setattr("app.services.opensearch_service.store_cluster_embedding", fake_store)
    monkeypatch.setattr(
        "app.services.opensearch_service.delete_cluster_embedding", lambda cluster_uuid: True
    )

    def fake_erase(cluster_uuids, fail):
        rec["erased"].extend(cluster_uuids)
        if rec.get("erase_error"):
            fail("clusters", rec["erase_error"])

    monkeypatch.setattr(fcs, "_erase_cluster_docs", fake_erase)
    return rec


def _two_files(db_session, normal_user, os_seams):
    fa, fb = _file(db_session, normal_user), _file(db_session, normal_user)
    sa, sb = _speaker(db_session, normal_user, fa), _speaker(db_session, normal_user, fb)
    os_seams["voiceprints"] = {str(sa.uuid): VOICE_A, str(sb.uuid): VOICE_B}
    return fa, fb, sa, sb


def test_survivor_centroid_is_rebuilt_from_the_remaining_member(db_session, normal_user, os_seams):
    fa, fb, sa, sb = _two_files(db_session, normal_user, os_seams)
    cluster = _cluster(db_session, normal_user, sa, sb)
    cluster_uuid, cluster_id = str(cluster.uuid), cluster.id

    result = fcs.purge_media_file(db_session, fa)

    assert result["deleted"] is True
    assert result["residual_errors"] == []
    owner, centroid = os_seams["stored"][cluster_uuid]
    assert owner == normal_user.id
    # The remaining member's voiceprint, not the (0.707, 0.707) two-member average.
    assert np.allclose(centroid, VOICE_B, atol=1e-6)
    db_session.expire_all()
    row = db_session.get(SpeakerCluster, cluster_id)
    assert row.member_count == 1
    assert row.quality_score == 1.0
    assert row.representative_speaker_id == sb.id
    assert os_seams["erased"] == []


def test_a_cluster_the_purge_emptied_is_still_deleted_not_recomputed(
    db_session, normal_user, os_seams
):
    fa, _fb, sa, _sb = _two_files(db_session, normal_user, os_seams)
    cluster = _cluster(db_session, normal_user, sa)
    cluster_id = cluster.id

    result = fcs.purge_media_file(db_session, fa)

    assert result["deleted"] is True
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id) is None
    assert os_seams["stored"] == {}


def test_an_unrebuildable_centroid_is_erased_so_no_stale_copy_survives(
    db_session, normal_user, os_seams
):
    fa, _fb, sa, sb = _two_files(db_session, normal_user, os_seams)
    cluster = _cluster(db_session, normal_user, sa, sb)
    os_seams["voiceprints"].pop(str(sb.uuid))  # the survivor's voiceprint is unreadable

    result = fcs.purge_media_file(db_session, fa)

    assert os_seams["stored"] == {}
    assert os_seams["erased"] == [str(cluster.uuid)]
    assert result["residual_errors"] == []


def test_a_refused_centroid_write_falls_back_to_erasure(db_session, normal_user, os_seams):
    fa, _fb, sa, sb = _two_files(db_session, normal_user, os_seams)
    cluster = _cluster(db_session, normal_user, sa, sb)
    os_seams["store_ok"] = False

    result = fcs.purge_media_file(db_session, fa)

    assert os_seams["erased"] == [str(cluster.uuid)]
    assert result["residual_errors"] == []


def test_a_centroid_that_can_be_neither_rebuilt_nor_erased_is_reported(
    db_session, normal_user, os_seams
):
    fa, _fb, sa, sb = _two_files(db_session, normal_user, os_seams)
    _cluster(db_session, normal_user, sa, sb)
    os_seams["store_ok"] = False
    os_seams["erase_error"] = "opensearch down"

    result = fcs.purge_media_file(db_session, fa)

    assert result["deleted"] is True
    assert [(r["stage"], r["file_uuid"], r["error"]) for r in result["residual_errors"]] == [
        ("clusters", str(fa.uuid), "opensearch down")
    ]


def test_a_failed_cluster_enumeration_is_reported_not_swallowed(
    db_session, normal_user, os_seams, monkeypatch, caplog
):
    fa, _fb, sa, sb = _two_files(db_session, normal_user, os_seams)
    _cluster(db_session, normal_user, sa, sb)

    def boom(db, file):
        raise RuntimeError("cluster table unreadable")

    monkeypatch.setattr(fcs, "_clusters_holding_file_speakers", boom)

    result = fcs.purge_media_file(db_session, fa)

    errors = [r["error"] for r in result["residual_errors"]]
    assert errors == ["could not enumerate the file's clusters"]
    assert {r["stage"] for r in result["residual_errors"]} == {"clusters"}
    # The driver's text belongs in the log, never in a value that can reach a response or audit row.
    assert "cluster table unreadable" in caplog.text


def test_a_promoted_cluster_left_with_no_members_has_its_centroid_erased(
    db_session, normal_user, os_seams
):
    fa, _fb, sa, _sb = _two_files(db_session, normal_user, os_seams)
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="P")
    db_session.add(profile)
    db_session.flush()
    cluster = _cluster(db_session, normal_user, sa, promoted_to=profile.id)
    cluster_uuid, cluster_id = str(cluster.uuid), cluster.id

    fcs.purge_media_file(db_session, fa)

    assert os_seams["erased"] == [cluster_uuid]
    assert os_seams["stored"] == {}
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, cluster_id).member_count == 0


def test_another_users_cluster_is_never_touched(
    db_session, normal_user, other_user, os_seams, monkeypatch
):
    fa, _fb, sa, sb = _two_files(db_session, normal_user, os_seams)
    own = _cluster(db_session, normal_user, sa, sb)
    fo = _file(db_session, other_user)
    so = _speaker(db_session, other_user, fo)
    os_seams["voiceprints"][str(so.uuid)] = VOICE_C
    theirs = _cluster(db_session, other_user, so)

    # Even if a stale plan named the other user's cluster, the owner scope must exclude it.
    real = fcs._clusters_holding_file_speakers
    monkeypatch.setattr(
        fcs,
        "_clusters_holding_file_speakers",
        lambda db, file: [*real(db, file), str(theirs.uuid)],
    )

    result = fcs.purge_media_file(db_session, fa)

    assert result["residual_errors"] == []
    assert set(os_seams["stored"]) == {str(own.uuid)}
    db_session.expire_all()
    assert db_session.get(SpeakerCluster, theirs.id).member_count == 1
