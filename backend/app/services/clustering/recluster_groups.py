"""Group-level steps of a re-cluster: eviction rules, cluster records and validation.

``batch_recluster`` (``recluster.py``) discovers AHC groups; the functions here take those
groups (``{(partition, label): [speaker id, ...]}``) and apply the post-AHC rules to them, then
write one ``SpeakerCluster`` per surviving multi-member group.
"""

import logging
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple
from uuid import uuid4

import numpy as np

from app.models.speaker import Speaker
from app.models.speaker import SpeakerCannotLink
from app.models.speaker import SpeakerCluster
from app.services.clustering.centroids import cluster_geometry_scores
from app.services.clustering.thresholds import GENDER_OUTLIER_EVICT_THRESHOLD

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)

Groups = dict[tuple[int, int], list[int]]


# ----------------------------------------------------------------------
# Cannot-link enforcement
# ----------------------------------------------------------------------


def load_cannot_link_pairs(
    self: "SpeakerClusteringService", ordered_ids: list[int]
) -> set[tuple[int, int]]:
    """Every recorded cannot-link pair that involves one of ``ordered_ids``."""
    all_cannot_links = (
        self.db.query(SpeakerCannotLink)
        .filter(
            SpeakerCannotLink.speaker_id.in_(ordered_ids)
            | SpeakerCannotLink.cannot_link_speaker_id.in_(ordered_ids)
        )
        .all()
    )
    cannot_link_set: set[tuple[int, int]] = set()
    for cl in all_cannot_links:
        cannot_link_set.add((int(cl.speaker_id), int(cl.cannot_link_speaker_id)))
    return cannot_link_set


def _weaker_of_pair(
    sid_a: int,
    sid_b: int,
    member_ids_g: list[int],
    evicted: set[int],
    emb_cache: dict[int, list[float]],
) -> int:
    """The speaker of a cannot-linked pair to evict: the one further from the group centroid."""
    g_embs = [emb_cache[s] for s in member_ids_g if s in emb_cache and s not in evicted]
    if not g_embs:
        return sid_b
    g_cent = np.mean(g_embs, axis=0)
    g_norm = np.linalg.norm(g_cent)
    if g_norm > 1e-8:
        g_cent = g_cent / g_norm
    sim_a = float(np.dot(emb_cache[sid_a], g_cent)) if sid_a in emb_cache else 0.0
    sim_b = float(np.dot(emb_cache[sid_b], g_cent)) if sid_b in emb_cache else 0.0
    return sid_b if sim_a >= sim_b else sid_a


def evict_cannot_linked(
    groups: Groups,
    cannot_link_set: set[tuple[int, int]],
    emb_cache: dict[int, list[float]],
) -> None:
    """Post-AHC constraint enforcement: remove speakers that violate cannot-link constraints
    from their assigned groups (in place)."""
    if not cannot_link_set:
        return
    for label_key in list(groups.keys()):
        member_ids_g = groups[label_key]
        if len(member_ids_g) < 2:
            continue
        # Find violating speakers in this group
        evicted: set[int] = set()
        for i, sid_a in enumerate(member_ids_g):
            for sid_b in member_ids_g[i + 1 :]:
                if (sid_a, sid_b) in cannot_link_set or (
                    sid_b,
                    sid_a,
                ) in cannot_link_set:
                    # Evict the speaker with lower similarity to group centroid
                    evicted.add(_weaker_of_pair(sid_a, sid_b, member_ids_g, evicted, emb_cache))
        if evicted:
            groups[label_key] = [s for s in member_ids_g if s not in evicted]
            logger.debug(
                "Constraint enforcement: evicted %d speaker(s) from AHC group",
                len(evicted),
            )


# ----------------------------------------------------------------------
# Gender-aware eviction
# ----------------------------------------------------------------------


def _gender_members(
    self: "SpeakerClusteringService",
    member_ids_g: list[int],
    speakers_by_id: dict[int, Speaker],
) -> dict[str, list[int]]:
    """The group's members by gender, as the privacy policy allows each one's to count."""
    g_counts: dict[str, list[int]] = {}
    for sid in member_ids_g:
        s = speakers_by_id.get(sid)
        # Inferred gender only counts while the privacy policy allows it.
        g = self._privacy.speaker_gender(s) if s else None
        if g:
            g_counts.setdefault(g, []).append(sid)
    return g_counts


def _minority_evictions(
    sorted_g: list[tuple[str, list[int]]],
    g_cent: Any,
    emb_cache: dict[int, list[float]],
) -> list[int]:
    """Minority-gender members whose similarity to the majority centroid is below the bar."""
    dominant_g = sorted_g[0][0]
    evict_ids: list[int] = []
    for minority_g, minority_ids in sorted_g[1:]:
        for sid in minority_ids:
            if sid not in emb_cache:
                continue
            sim = float(np.dot(emb_cache[sid], g_cent))
            if sim < GENDER_OUTLIER_EVICT_THRESHOLD:
                evict_ids.append(sid)
                logger.debug(
                    "Gender eviction: speaker %d (%s) from group, "
                    "sim %.3f < %.3f to %s-dominant centroid",
                    sid,
                    minority_g,
                    sim,
                    GENDER_OUTLIER_EVICT_THRESHOLD,
                    dominant_g,
                )
    return evict_ids


def _group_gender_evictions(
    self: "SpeakerClusteringService",
    member_ids_g: list[int],
    speakers_by_id: dict[int, Speaker],
    emb_cache: dict[int, list[float]],
) -> list[int]:
    """The members of one group to evict for a gender mismatch AND a weak embedding."""
    g_counts = _gender_members(self, member_ids_g, speakers_by_id)
    if len(g_counts) < 2:
        return []
    # Find dominant gender (must be clear majority: >= 2/3)
    sorted_g = sorted(g_counts.items(), key=lambda x: len(x[1]), reverse=True)
    dominant_g, dominant_ids = sorted_g[0]
    total_gendered = sum(len(v) for v in g_counts.values())
    if len(dominant_ids) < total_gendered * 2 / 3:
        return []  # No clear majority — skip
    # Compute group centroid from majority members
    maj_embs = [emb_cache[sid] for sid in dominant_ids if sid in emb_cache]
    if not maj_embs:
        return []
    g_cent = np.mean(maj_embs, axis=0)
    g_norm = np.linalg.norm(g_cent)
    if g_norm > 1e-8:
        g_cent = g_cent / g_norm
    # Check minority members: evict only if sim < threshold
    return _minority_evictions(sorted_g, g_cent, emb_cache)


def evict_gender_outliers(
    self: "SpeakerClusteringService",
    groups: Groups,
    speakers_by_id: dict[int, Speaker],
    emb_cache: dict[int, list[float]],
) -> None:
    """Gender-aware eviction (in place).

    For each group, if there's a clear dominant gender, evict minority-gender members
    whose embedding similarity to the group centroid is below the eviction threshold.
    Dual signal required: gender mismatch + weak embedding.
    """
    gender_evicted_total = 0
    for label_key in list(groups.keys()):
        member_ids_g = groups[label_key]
        if len(member_ids_g) < 3:
            continue
        evict_ids = _group_gender_evictions(self, member_ids_g, speakers_by_id, emb_cache)
        if evict_ids:
            groups[label_key] = [s for s in member_ids_g if s not in evict_ids]
            gender_evicted_total += len(evict_ids)
    if gender_evicted_total:
        logger.info(
            "Gender-aware eviction: removed %d speaker(s) from AHC groups",
            gender_evicted_total,
        )


# ----------------------------------------------------------------------
# Cluster records
# ----------------------------------------------------------------------


class _Geometry(NamedTuple):
    """A group's member embeddings, centroid and each member's similarity to it."""

    member_embs: list[list[float]]
    per_member_conf: dict[int, float]
    arr: Any
    centroid: Any


def _centroid_and_confidence(member_ids: list[int], emb_cache: dict[int, list[float]]) -> _Geometry:
    """Compute centroid and per-member confidence from cached embeddings before adding
    members to the cluster."""
    member_embs = [emb_cache[sid] for sid in member_ids if sid in emb_cache]
    per_member_conf: dict[int, float] = {}
    arr = centroid = None
    if member_embs:
        arr = np.array(member_embs, dtype=np.float32)
        centroid = np.mean(arr, axis=0)
        c_norm = np.linalg.norm(centroid)
        if c_norm > 1e-8:
            centroid = centroid / c_norm
        # Cosine similarity of each member to the L2-normalized centroid
        arr_norms = np.linalg.norm(arr, axis=1, keepdims=True).clip(1e-8)
        arr_normed = arr / arr_norms
        sims = arr_normed @ centroid  # centroid already L2-normalized
        emb_idx = 0
        for sid in member_ids:
            if sid in emb_cache:
                per_member_conf[sid] = float(max(0.0, min(1.0, sims[emb_idx])))
                emb_idx += 1
    return _Geometry(member_embs, per_member_conf, arr, centroid)


def _score_cluster(cluster: SpeakerCluster, geometry: _Geometry, member_ids: list[int]) -> None:
    """Set the cluster's quality score, tightest pair and representative speaker."""
    quality_score, min_similarity = cluster_geometry_scores(geometry.arr, geometry.centroid)
    cluster.quality_score = quality_score  # type: ignore[assignment]
    cluster.min_similarity = min_similarity  # type: ignore[assignment]

    cluster.representative_speaker_id = member_ids[0]  # type: ignore[assignment]


def _store_centroid(
    cluster: SpeakerCluster, user_id: int, centroid: Any, cluster_org: int | None
) -> bool:
    """Write the cluster's centroid to OpenSearch. False when it could not be stored."""
    from app.services.opensearch_service import store_cluster_embedding

    try:
        store_cluster_embedding(
            cluster_uuid=str(cluster.uuid),
            user_id=user_id,
            embedding=centroid.tolist(),
            label=cluster.label,
            refresh=False,
            organization_id=cluster_org,
        )
    except Exception as e:
        logger.warning(
            "Could not store centroid for cluster %s: %s",
            cluster.uuid,
            e,
        )
        return False
    return True


def create_similarity_clusters(
    self: "SpeakerClusteringService",
    user_id: int,
    groups: Groups,
    group_org: dict[tuple[int, int], int | None],
    emb_cache: dict[int, list[float]],
    speakers_by_id: dict[int, Speaker],
) -> tuple[int, int, int]:
    """Create a cluster record for each AHC group of two or more.

    Returns ``(clusters created, speakers assigned, centroid store failures)``.
    """
    sim_clusters = 0
    sim_assigned = 0
    centroid_failures = 0
    for gkey, member_ids in groups.items():
        if len(member_ids) < 2:
            continue

        # Stamp the group's tenant scope (all members share it — the
        # similarity graph was partitioned per scope above).
        cluster_org = group_org.get(gkey)
        cluster = SpeakerCluster(
            uuid=uuid4(),
            user_id=user_id,
            organization_id=cluster_org,
            member_count=0,
        )
        self.db.add(cluster)
        self.db.flush()

        geometry = _centroid_and_confidence(member_ids, emb_cache)

        # Bulk-add all members with per-member confidence
        cluster_speakers = [speakers_by_id[sid] for sid in member_ids if sid in speakers_by_id]
        added = self._bulk_add_speakers_to_cluster(
            cluster_speakers,
            cluster,
            geometry.per_member_conf or 0.0,
        )
        sim_assigned += added

        if geometry.member_embs:
            _score_cluster(cluster, geometry, member_ids)
            if not _store_centroid(cluster, user_id, geometry.centroid, cluster_org):
                centroid_failures += 1

        sim_clusters += 1
    return sim_clusters, sim_assigned, centroid_failures


# ----------------------------------------------------------------------
# Post-clustering validation
# ----------------------------------------------------------------------


def validate_groups(
    groups: Groups,
    emb_cache: dict[int, list[float]],
    threshold: float,
    sim_clusters: int,
) -> None:
    """Verify all pairwise similarities within each AHC cluster meet the threshold.

    Log warnings for any violating pairs (indicates numerical edge cases).
    """
    if not (sim_clusters > 0 and emb_cache):
        return
    violations = 0
    for _lbl, member_ids_v in groups.items():
        if len(member_ids_v) < 2:
            continue
        vecs = [emb_cache[sid] for sid in member_ids_v if sid in emb_cache]
        if len(vecs) < 2:
            continue
        varr = np.array(vecs, dtype=np.float32)
        vnorms = np.linalg.norm(varr, axis=1, keepdims=True).clip(1e-8)
        varr_n = varr / vnorms
        sim_mat = varr_n @ varr_n.T
        iu = np.triu_indices(len(vecs), k=1)
        min_sim = float(np.min(sim_mat[iu]))
        if min_sim < threshold - 0.01:  # 1% tolerance
            violations += 1
            logger.warning(
                "Cluster validation: group with %d members "
                "has min pairwise similarity %.4f (threshold=%.2f)",
                len(member_ids_v),
                min_sim,
                threshold,
            )
    if violations:
        logger.warning(
            "Post-clustering validation: %d/%d clusters have sub-threshold pairs",
            violations,
            sim_clusters,
        )
    else:
        logger.info(
            "Post-clustering validation: all %d AHC clusters pass threshold check (>=%.2f)",
            sim_clusters,
            threshold,
        )
