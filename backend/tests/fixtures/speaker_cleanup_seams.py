"""Rows and OpenSearch stand-ins for the speaker-cleanup suites (issue #1214).

``speaker_os_seams`` replaces every OpenSearch seam ``services/speaker_cleanup`` reaches
— voiceprint reads, centroid and profile-embedding writes, the verified erasures, the
reindex dispatch — with recorders, so a test asserts on what was stored and erased and
on the Postgres rows, never on a call sequence. Everything else (the ORM, the cleanup
rules, the endpoints) runs for real against the savepoint-isolated ``db_session``.
Registered in the root conftest's ``pytest_plugins``; the row builders are imported by
the suites directly.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import numpy as np
import pytest

VOICE_A = [1.0, 0.0, 0.0, 0.0]
VOICE_B = [0.0, 1.0, 0.0, 0.0]
VOICE_C = [0.0, 0.0, 1.0, 0.0]


def cosine(u: list[float], v: list[float]) -> float:
    a, b = np.array(u, dtype=np.float64), np.array(v, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def make_file(db, user) -> Any:
    from app.models.media import FileStatus
    from app.models.media import MediaFile

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


def make_speaker(db, user, media_file, name: str = "SPEAKER_00", **kw: Any) -> Any:
    from app.models.media import Speaker

    sp = Speaker(
        uuid=uuid_pkg.uuid4(), name=name, user_id=user.id, media_file_id=media_file.id, **kw
    )
    db.add(sp)
    db.flush()
    return sp


def make_cluster(db, user, *speakers: Any, promoted_to: int | None = None, label="c") -> Any:
    """A committed cluster; both membership records (rows and ``cluster_id``) are set."""
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember

    cl = SpeakerCluster(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        label=label,
        member_count=len(speakers),
        promoted_to_profile_id=promoted_to,
    )
    db.add(cl)
    db.flush()
    for sp in speakers:
        db.add(SpeakerClusterMember(cluster_id=cl.id, speaker_id=sp.id, confidence=0.9))
        sp.cluster_id = cl.id
    db.commit()
    return cl


def make_profile(db, user, name: str = "P") -> Any:
    from app.models.media import SpeakerProfile

    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=user.id, name=name)
    db.add(profile)
    db.flush()
    return profile


def make_pair(db, user, seams: dict[str, Any]) -> tuple[Any, Any, Any, Any, Any]:
    """Two files, one speaker each (voiceprints A and B), and one cluster over both."""
    fa, fb = make_file(db, user), make_file(db, user)
    sa, sb = make_speaker(db, user, fa), make_speaker(db, user, fb)
    seams["voiceprints"].update({str(sa.uuid): VOICE_A, str(sb.uuid): VOICE_B})
    return fa, fb, sa, sb, make_cluster(db, user, sa, sb)


@pytest.fixture
def speaker_os_seams(monkeypatch) -> dict[str, Any]:
    """Every OpenSearch seam the speaker cleanup touches, recording what it was asked to do.

    Knobs: ``store_ok`` / ``profile_store_ok`` make the centroid / profile write refuse;
    ``erase_error`` makes every erasure report a failure through the cleanup's own
    ``fail`` callback, the way the real survivor counts do.
    """
    from app.services import file_cleanup_service as fcs

    rec: dict[str, Any] = {
        "voiceprints": {},
        "clusters": {},
        "profiles": {},
        "erased_speakers": [],
        "erased_clusters": [],
        "erased_profiles": [],
        "reindexed": [],
        "store_ok": True,
        "profile_store_ok": True,
        "erase_error": None,
    }

    def get(speaker_uuid):
        return rec["voiceprints"].get(str(speaker_uuid))

    monkeypatch.setattr("app.services.opensearch_service.get_speaker_embedding", get)
    monkeypatch.setattr("app.services.profile_embedding_service.get_speaker_embedding", get)

    def store_cluster(cluster_uuid, user_id, embedding, **kwargs):
        if rec["store_ok"]:
            rec["clusters"][str(cluster_uuid)] = list(embedding)
        return rec["store_ok"]

    monkeypatch.setattr("app.services.opensearch_service.store_cluster_embedding", store_cluster)

    def store_profile(*, profile_uuid, embedding, **kwargs):
        if rec["profile_store_ok"]:
            rec["profiles"][str(profile_uuid)] = list(embedding)
        return rec["profile_store_ok"]

    monkeypatch.setattr("app.services.opensearch_service.store_profile_embedding", store_profile)
    monkeypatch.setattr("app.services.opensearch_service.remove_profile_embedding", lambda u: True)

    def eraser(kind: str, store: str):
        def erase(uuids, fail):
            rec[f"erased_{kind}"].extend(str(u) for u in uuids)
            for u in uuids:
                rec[store].pop(str(u), None)
            if uuids and rec["erase_error"]:
                fail(kind, rec["erase_error"])

        return erase

    monkeypatch.setattr(fcs, "_erase_speaker_docs", eraser("speakers", "voiceprints"))
    monkeypatch.setattr(fcs, "_erase_cluster_docs", eraser("clusters", "clusters"))
    monkeypatch.setattr(fcs, "_erase_profile_docs", eraser("profiles", "profiles"))

    from app.services.search.reindex_dispatch import ReindexDispatch

    rec["reindex_outcome"] = ReindexDispatch.QUEUED

    def queue_reindex(*, file_id, file_uuid, user_id, priority=None):
        rec["reindexed"].append(int(file_id))
        return rec["reindex_outcome"]

    def reindex(*, file_id, file_uuid, user_id, priority=None):
        return queue_reindex(file_id=file_id, file_uuid=file_uuid, user_id=user_id) is (
            ReindexDispatch.QUEUED
        )

    monkeypatch.setattr(
        "app.services.search.reindex_dispatch.queue_transcript_reindex", queue_reindex
    )
    monkeypatch.setattr("app.services.search.reindex_dispatch.dispatch_transcript_reindex", reindex)

    # The relabelled speakers' own documents (#1233): recorded, keyed by speaker uuid.
    rec["doc_profiles"] = {}
    rec["doc_names"] = {}

    def doc_profile(*, speaker_uuid, profile_id, profile_uuid, verified=False, display_name=None):
        rec["doc_profiles"][str(speaker_uuid)] = profile_id
        return {"result": "updated"}

    def doc_name(speaker_uuid, display_name):
        rec["doc_names"][str(speaker_uuid)] = display_name
        return {"result": "updated"}

    monkeypatch.setattr("app.services.opensearch_service.update_speaker_profile", doc_profile)
    monkeypatch.setattr("app.services.opensearch_service.update_speaker_display_name", doc_name)
    return rec
