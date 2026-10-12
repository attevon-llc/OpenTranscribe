"""kNN stand-ins for the speaker ASSIGNMENT paths (issue #1233).

``speaker_os_seams`` records what the shared cleanup stores and erases. The assignment
paths also READ OpenSearch to decide what to assign: cluster-centroid kNN
(``find_or_create_cluster``, re-cluster Phase 2.5), the re-cluster embedding scan, the
speaker kNN behind profile propagation, the labelled speaker's voiceprint in
retroactive matching, and the voiceprint documents a segment reassignment rewrites.
``speaker_assignment_seams`` answers every one of those from the SAME recorders, by real
cosine over the recorded vectors, so a test's assignment decision and its centroid math
come from one source of truth. Registered in the root conftest's ``pytest_plugins``.

Deliberately imports nothing from ``app`` at module scope: the red-first runs copy this
file into a ``git archive`` tree of the code before the fix.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fixtures.speaker_cleanup_seams import cosine


@pytest.fixture
def speaker_assignment_seams(
    speaker_os_seams: dict[str, Any], db_session, monkeypatch
) -> dict[str, Any]:
    """Extend ``speaker_os_seams`` with the reads the assignment paths make.

    Adds ``rec["segment_counts"]`` (voiceprint document segment counts),
    ``rec["doc_orgs"]`` (the tenant each voiceprint write stamped) and
    ``rec["deleted_centroids"]`` (re-cluster's wholesale centroid deletes).
    """
    from app.models.media import Speaker

    rec = speaker_os_seams
    rec["segment_counts"] = {}
    rec["deleted_centroids"] = []

    def find_matching_clusters(embedding, user_id, k=5, threshold=0.75, organization_id=None):
        scored = sorted(
            ((cosine(list(embedding), c), u) for u, c in rec["clusters"].items()), reverse=True
        )
        return [
            {"cluster_uuid": u, "similarity": s, "label": None} for s, u in scored if s >= threshold
        ][:k]

    def iter_speaker_embeddings(user_id, speaker_uuids=None, batch_size=200):
        batch = [
            {"speaker_uuid": u, "embedding": rec["voiceprints"][u]}
            for u in (speaker_uuids or [])
            if u in rec["voiceprints"]
        ]
        if batch:
            yield batch

    def delete_cluster_embedding(cluster_uuid):
        rec["deleted_centroids"].append(str(cluster_uuid))
        rec["clusters"].pop(str(cluster_uuid), None)
        return True

    monkeypatch.setattr(
        "app.services.opensearch_service.find_matching_clusters", find_matching_clusters
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.iter_speaker_embeddings", iter_speaker_embeddings
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.delete_cluster_embedding", delete_cluster_embedding
    )
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", None)

    def get_voiceprint(speaker_uuid):
        return rec["voiceprints"].get(str(speaker_uuid))

    # Imported by name at module scope there, so the module attribute is what is read.
    monkeypatch.setattr("app.api.endpoints.speaker_update.get_speaker_embedding", get_voiceprint)

    def similarity_search(
        embedding,
        user_id,
        index_name="speakers",
        min_raw_cosine=0.7,
        max_results=50,
        exclude_ids=None,
        organization_id=None,
    ):
        excluded = set(exclude_ids or [])
        ids = {
            str(u): int(i)
            for u, i in db_session.query(Speaker.uuid, Speaker.id)
            .filter(Speaker.user_id == user_id)
            .all()
        }
        hits = []
        for speaker_uuid, voice in rec["voiceprints"].items():
            sid = ids.get(speaker_uuid)
            if sid is None or sid in excluded:
                continue
            similarity = cosine(list(embedding), voice)
            if similarity >= min_raw_cosine:
                hits.append({"speaker_id": int(sid), "similarity": similarity})
        return sorted(hits, key=lambda h: -h["similarity"])[:max_results]

    monkeypatch.setattr(
        "app.services.similarity_service.SimilarityService.opensearch_similarity_search",
        staticmethod(similarity_search),
    )

    def get_speaker_document(speaker_uuid):
        voice = rec["voiceprints"].get(str(speaker_uuid))
        if voice is None:
            return None
        return {"embedding": voice, "segment_count": rec["segment_counts"].get(speaker_uuid, 1)}

    rec["doc_orgs"] = {}

    def add_speaker_embedding(*, speaker_uuid, embedding, segment_count=1, **kwargs):
        rec["voiceprints"][str(speaker_uuid)] = list(embedding)
        rec["segment_counts"][str(speaker_uuid)] = segment_count
        # The real write replaces the whole document, tenant stamp included.
        rec["doc_orgs"][str(speaker_uuid)] = kwargs.get("organization_id")
        return True

    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_document", get_speaker_document
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.add_speaker_embedding", add_speaker_embedding
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.update_speaker_segment_count", lambda u, c: True
    )
    return rec
