"""Cluster centroid recompute and speaker embedding lookup."""

import logging
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy.orm import joinedload

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


class CentroidRecomputeError(RuntimeError):
    """A strict centroid recompute could not replace the stored centroid."""


def cluster_geometry_scores(arr: np.ndarray | None, centroid: np.ndarray) -> tuple[float, float]:
    """Return a cluster's ``(quality_score, min_similarity)`` from its member voiceprints.

    ``quality_score`` is the mean cosine similarity of the members to the centroid, the same
    metric shown per member in the UI, so the cluster score agrees with the member scores.
    ``min_similarity`` is the tightest pairwise cosine similarity (the worst-case pair). A
    cluster with fewer than two members scores 1.0 on both.

    Args:
        arr: Member embeddings, one row per member, or None when there are none.
        centroid: The L2-normalized cluster centroid.

    Returns:
        ``(quality_score, min_similarity)``.
    """
    if arr is None or len(arr) < 2:
        return 1.0, 1.0
    normed = arr / np.linalg.norm(arr, axis=1, keepdims=True).clip(1e-8)
    sim_matrix = normed @ normed.T
    iu = np.triu_indices(len(arr), k=1)
    return float(np.mean(normed @ centroid)), float(np.min(sim_matrix[iu]))


def update_cluster_centroid(
    self: "SpeakerClusteringService",
    cluster: SpeakerCluster,
    user_id: int,
    refresh: str | bool = "wait_for",
    strict: bool = False,
) -> None:
    """Recalculate and store the cluster centroid embedding.

    Args:
        cluster: The cluster whose centroid is rebuilt from its current members.
        user_id: Owner, written onto the centroid document.
        refresh: OpenSearch refresh policy for the centroid write.
        strict: Raise :class:`CentroidRecomputeError` instead of leaving the stored
            centroid untouched when it cannot be rebuilt (no member voiceprint, a
            zero vector, or a failed write). Erasure needs this: a swallowed failure
            leaves the OLD centroid, which still averages in a deleted voiceprint.
    """
    members = (
        self.db.query(SpeakerClusterMember)
        .options(joinedload(SpeakerClusterMember.speaker))
        .filter(SpeakerClusterMember.cluster_id == cluster.id)
        .all()
    )

    embeddings: list[list[float]] = []
    valid_members: list = []
    best_quality = -1.0
    best_speaker_id = None

    for m in members:
        speaker = m.speaker
        if not speaker:
            continue

        emb = self._get_speaker_embedding(speaker)
        if emb is not None:
            embeddings.append(emb)
            valid_members.append(m)
            quality = float(m.confidence) if m.confidence else 0.0
            if quality > best_quality:
                best_quality = quality
                best_speaker_id = int(speaker.id)

    if not embeddings:
        if strict:
            raise CentroidRecomputeError(f"cluster {cluster.uuid} has no member voiceprint")
        return

    # Weighted mean centroid: verified/profile-linked speakers get 2x weight
    arr = np.array(embeddings, dtype=np.float32)
    weights = np.array(
        [2.0 if m.speaker and m.speaker.profile_id else 1.0 for m in valid_members],
        dtype=np.float32,
    )
    centroid = np.average(arr, axis=0, weights=weights)
    norm = np.linalg.norm(centroid)
    if norm > 1e-8:
        centroid = centroid / norm
    else:
        logger.warning(
            "Zero-vector centroid for cluster %s — skipping OpenSearch storage",
            cluster.uuid,
        )
        if strict:
            raise CentroidRecomputeError(f"cluster {cluster.uuid} centroid is a zero vector")
        return

    quality_score, min_similarity = cluster_geometry_scores(arr, centroid)
    cluster.quality_score = quality_score  # type: ignore[assignment]
    cluster.min_similarity = min_similarity  # type: ignore[assignment]

    if best_speaker_id:
        cluster.representative_speaker_id = best_speaker_id  # type: ignore[assignment]

    # Store in OpenSearch
    try:
        from app.services.opensearch_service import store_cluster_embedding

        stored = store_cluster_embedding(
            cluster_uuid=str(cluster.uuid),
            user_id=user_id,
            embedding=centroid.tolist(),
            label=cluster.label,
            refresh=refresh,
            organization_id=(
                int(cluster.organization_id) if cluster.organization_id is not None else None
            ),
        )
    except Exception as e:
        logger.warning("Could not update cluster centroid in OpenSearch: %s", e)
        if strict:
            raise CentroidRecomputeError(str(e)) from e
    else:
        if strict and not stored:
            raise CentroidRecomputeError(
                f"OpenSearch refused the centroid write for cluster {cluster.uuid}"
            )


def get_speaker_embedding(self: "SpeakerClusteringService", speaker: Speaker) -> list[float] | None:
    """Get embedding for a speaker from the active index."""
    try:
        from app.services.opensearch_service import get_speaker_embedding as opensearch_embedding

        return opensearch_embedding(str(speaker.uuid))
    except Exception as e:
        logger.warning("Could not get embedding for speaker %s: %s", speaker.uuid, e)
        return None
