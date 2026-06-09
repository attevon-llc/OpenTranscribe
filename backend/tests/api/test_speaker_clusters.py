"""Characterization tests for ``api/endpoints/speaker_clusters.py``.

Wave-3 (speakers domain). Pins the CURRENT observable behavior of the speaker
clustering endpoints (prefix ``/api/speaker-clusters``):

- ``GET  ""``                          (list, pagination)
- ``GET  /stats``                      (aggregate stats)
- ``GET  /unverified/inbox``           (inbox, pagination)
- ``POST /recluster``                  (dispatch — no-opped by SKIP_CELERY)
- ``POST /batch-verify``               (batch ops)
- ``GET  /{uuid}`` ``PUT`` ``DELETE``  (cluster detail / update / delete)
- ``POST /{uuid}/analyze-outliers`` ``/unassign`` ``/promote`` ``/split``
- ``POST /{src}/merge/{tgt}``          (merge)
- ``GET  /speakers/{uuid}/media-preview``

Cross-user isolation in this module is by-construction: the service filters every
read/mutation by ``user_id``, so a non-owner sees an EMPTY list (no 403) and a
foreign UUID is simply "not found" (404). These tests pin that, plus the
malformed-UUID 404 fixed in this branch (see ``_require_uuid``).

The 12 real benchmark clusters + 16 real speakers (admin-owned) are read but
NEVER mutated; all mutations use savepoint rows owned by ``normal_user``.

Run: ``venv/bin/pytest tests/api/test_speaker_clusters.py -v -n0``
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster

PREFIX = "/api/speaker-clusters"


def _make_cluster(db_session, owner, *, label=None, member_count=0) -> SpeakerCluster:
    cluster = SpeakerCluster(
        user_id=owner.id,
        label=label,
        member_count=member_count,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    return cluster


def _make_file_and_speaker(db_session, owner, *, cluster_id=None):
    mf = MediaFile(
        user_id=owner.id,
        filename="clus.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    spk = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_00", cluster_id=cluster_id)
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return mf, spk


# ---------------------------------------------------------------------------
# List / stats / inbox
# ---------------------------------------------------------------------------


def test_list_clusters_unauthorized(client):
    assert client.get(PREFIX).status_code == status.HTTP_401_UNAUTHORIZED


def test_list_clusters_envelope(client, user_token_headers):
    resp = client.get(PREFIX, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    for key in ("items", "total", "page", "per_page", "pages"):
        assert key in body, f"missing key {key!r}"
    assert body["page"] == 1
    assert body["per_page"] == 20
    assert isinstance(body["items"], list)


def test_list_clusters_shows_own(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user, label="MyCluster")
    resp = client.get(PREFIX, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert str(cluster.uuid) in {c["uuid"] for c in resp.json()["items"]}


def test_list_clusters_excludes_other_users(
    client, other_user_auth_headers, normal_user, db_session
):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.get(PREFIX, headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert str(cluster.uuid) not in {c["uuid"] for c in resp.json()["items"]}


def test_list_clusters_per_page_over_max_422(client, user_token_headers):
    resp = client.get(PREFIX, headers=user_token_headers, params={"per_page": 500})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_clusters_page_zero_422(client, user_token_headers):
    resp = client.get(PREFIX, headers=user_token_headers, params={"page": 0})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_stats_owner_200(client, user_token_headers, normal_user, db_session):
    _make_cluster(db_session, normal_user)
    resp = client.get(f"{PREFIX}/stats", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    for key in ("total_speakers", "clustered_speakers", "total_clusters", "coverage_pct"):
        assert key in body
    assert body["total_clusters"] >= 1


def test_stats_unauthorized(client):
    assert client.get(f"{PREFIX}/stats").status_code == status.HTTP_401_UNAUTHORIZED


def test_inbox_envelope(client, user_token_headers):
    resp = client.get(f"{PREFIX}/unverified/inbox", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    for key in ("items", "total", "page", "per_page", "pages"):
        assert key in body
    assert isinstance(body["items"], list)


# ---------------------------------------------------------------------------
# recluster (dispatch no-opped by SKIP_CELERY autouse fixture)
# ---------------------------------------------------------------------------


def test_recluster_dispatch_started(client, user_token_headers):
    resp = client.post(f"{PREFIX}/recluster", headers=user_token_headers, json={})
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    body = resp.json()
    assert body["status"] == "started"
    assert body["task_id"] == "test-task-id"  # fake AsyncResult id from conftest


def test_recluster_bad_threshold_422(client, user_token_headers):
    resp = client.post(f"{PREFIX}/recluster", headers=user_token_headers, json={"threshold": 2.0})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_recluster_unauthorized(client):
    assert client.post(f"{PREFIX}/recluster", json={}).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# batch-verify validation
# ---------------------------------------------------------------------------


def test_batch_verify_empty_list_422(client, user_token_headers):
    """speaker_uuids has min_length=1."""
    resp = client.post(
        f"{PREFIX}/batch-verify",
        headers=user_token_headers,
        json={"speaker_uuids": [], "action": "skip"},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_batch_verify_unknown_speaker_skips_gracefully(
    client, user_token_headers, normal_user, db_session
):
    """A nonexistent speaker UUID is counted as failed, not a 500."""
    resp = client.post(
        f"{PREFIX}/batch-verify",
        headers=user_token_headers,
        json={"speaker_uuids": [str(uuid.uuid4())], "action": "skip"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert "updated_count" in resp.json()


# ---------------------------------------------------------------------------
# Cluster detail / update / delete  (user-scoped: foreign == 404)
# ---------------------------------------------------------------------------


def test_get_cluster_detail_owner_200(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user, label="Detail")
    resp = client.get(f"{PREFIX}/{cluster.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["uuid"] == str(cluster.uuid)


def test_get_cluster_detail_other_user_404(
    client, other_user_auth_headers, normal_user, db_session
):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.get(f"{PREFIX}/{cluster.uuid}", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_get_cluster_detail_nonexistent_404(client, user_token_headers):
    resp = client.get(f"{PREFIX}/{uuid.uuid4()}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_get_cluster_detail_malformed_uuid_404(client, user_token_headers):
    """BUGFIX (this branch): a malformed cluster UUID used to hit a raw
    ``WHERE uuid = '<garbage>'`` against a Postgres uuid column → DataError → an
    unhandled 500 + poisoned transaction. ``_require_uuid`` now returns the same
    404 the route gives for an unknown cluster."""
    resp = client.get(f"{PREFIX}/not-a-uuid", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_update_cluster_owner_200(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user, label="Before")
    resp = client.put(
        f"{PREFIX}/{cluster.uuid}",
        headers=user_token_headers,
        json={"label": "After", "description": "d"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json()["label"] == "After"
    assert resp.json()["description"] == "d"


def test_update_cluster_other_user_404(client, other_user_auth_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.put(
        f"{PREFIX}/{cluster.uuid}",
        headers=other_user_auth_headers,
        json={"label": "stolen"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_update_cluster_malformed_uuid_404(client, user_token_headers):
    resp = client.put(f"{PREFIX}/not-a-uuid", headers=user_token_headers, json={"label": "x"})
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_delete_cluster_owner_204(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    cid = cluster.id
    resp = client.delete(f"{PREFIX}/{cluster.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert db_session.query(SpeakerCluster).filter(SpeakerCluster.id == cid).first() is None


def test_delete_cluster_clears_speaker_refs(client, user_token_headers, normal_user, db_session):
    """Deleting a cluster nulls the cluster_id on its member speakers (not delete)."""
    cluster = _make_cluster(db_session, normal_user)
    _mf, spk = _make_file_and_speaker(db_session, normal_user, cluster_id=cluster.id)
    spk_id = spk.id
    resp = client.delete(f"{PREFIX}/{cluster.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    surviving = db_session.query(Speaker).filter(Speaker.id == spk_id).first()
    assert surviving is not None
    assert surviving.cluster_id is None


def test_delete_cluster_other_user_404(client, other_user_auth_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.delete(f"{PREFIX}/{cluster.uuid}", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


# ---------------------------------------------------------------------------
# analyze-outliers / unassign / promote / split / merge
# ---------------------------------------------------------------------------


def test_analyze_outliers_other_user_404(client, other_user_auth_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(f"{PREFIX}/{cluster.uuid}/analyze-outliers", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_analyze_outliers_malformed_uuid_404(client, user_token_headers):
    resp = client.post(f"{PREFIX}/not-a-uuid/analyze-outliers", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_unassign_other_user_404(client, other_user_auth_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/{cluster.uuid}/unassign",
        headers=other_user_auth_headers,
        json={"speaker_uuids": [str(uuid.uuid4())], "blacklist": True},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


def test_unassign_empty_speakers_422(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/{cluster.uuid}/unassign",
        headers=user_token_headers,
        json={"speaker_uuids": []},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_promote_other_user_returns_400(client, other_user_auth_headers, normal_user, db_session):
    """promote_cluster_to_profile returns None for a foreign/unknown cluster →
    the endpoint maps that to 400 'Failed to promote cluster'."""
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/{cluster.uuid}/promote",
        headers=other_user_auth_headers,
        json={"name": "NewProfile"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Failed to promote cluster"


def test_promote_missing_name_422(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(f"{PREFIX}/{cluster.uuid}/promote", headers=user_token_headers, json={})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_split_empty_speakers_422(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(
        f"{PREFIX}/{cluster.uuid}/split",
        headers=user_token_headers,
        json={"speaker_uuids": []},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_merge_self_400(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(f"{PREFIX}/{cluster.uuid}/merge/{cluster.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Cannot merge cluster with itself"


def test_merge_foreign_clusters_400(client, other_user_auth_headers, normal_user, db_session):
    """Merging two foreign clusters → service returns None → 400."""
    src = _make_cluster(db_session, normal_user)
    tgt = _make_cluster(db_session, normal_user)
    resp = client.post(f"{PREFIX}/{src.uuid}/merge/{tgt.uuid}", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Failed to merge clusters"


def test_merge_malformed_uuid_404(client, user_token_headers, normal_user, db_session):
    cluster = _make_cluster(db_session, normal_user)
    resp = client.post(f"{PREFIX}/not-a-uuid/merge/{cluster.uuid}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Cluster not found"


# ---------------------------------------------------------------------------
# media-preview (speaker, user-scoped)
# ---------------------------------------------------------------------------


def test_media_preview_owner_200(client, user_token_headers, normal_user, db_session):
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.get(f"{PREFIX}/speakers/{spk.uuid}/media-preview", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    body = resp.json()
    assert body["speaker_uuid"] == str(spk.uuid)
    assert "media_url" in body


def test_media_preview_other_user_404(client, other_user_auth_headers, normal_user, db_session):
    _mf, spk = _make_file_and_speaker(db_session, normal_user)
    resp = client.get(
        f"{PREFIX}/speakers/{spk.uuid}/media-preview", headers=other_user_auth_headers
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker not found"


def test_media_preview_malformed_uuid_404(client, user_token_headers):
    resp = client.get(f"{PREFIX}/speakers/not-a-uuid/media-preview", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "Speaker not found"
