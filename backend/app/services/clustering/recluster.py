"""On-demand re-clustering of one user's speakers (profile grouping, then AHC)."""

import logging
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.services.clustering.recluster_groups import Groups
from app.services.clustering.recluster_groups import create_similarity_clusters
from app.services.clustering.recluster_groups import evict_cannot_linked
from app.services.clustering.recluster_groups import evict_gender_outliers
from app.services.clustering.recluster_groups import load_cannot_link_pairs
from app.services.clustering.recluster_groups import validate_groups
from app.services.clustering.recluster_profiles import group_by_profile
from app.services.clustering.recluster_profiles import match_singletons_to_profile_clusters
from app.services.clustering.similarity import Report
from app.services.clustering.similarity import ahc_groups_per_tenant
from app.services.clustering.similarity import load_unlabeled_embeddings
from app.services.clustering.thresholds import CLUSTER_ASSIGNMENT_THRESHOLD
from app.services.speaker_cleanup import SpeakerImpact

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def largest_recluster_partition(self: "SpeakerClusteringService", user_id: int) -> int:
    """Size of the largest similarity matrix ``batch_recluster`` would build.

    Mirrors Phase 2's input (the user's speakers without a profile) and its
    tenant partitioning (the speaker's file organization), counted in SQL so a
    dispatcher can route by it without loading embeddings. An upper bound: a
    speaker with no stored embedding is counted but dropped by the service.
    """
    from sqlalchemy import func

    counts = (
        self.db.query(func.count(Speaker.id))
        .join(MediaFile, MediaFile.id == Speaker.media_file_id)
        .filter(Speaker.user_id == user_id, Speaker.profile_id.is_(None))
        .group_by(MediaFile.organization_id)
        .all()
    )
    return max((int(c) for (c,) in counts), default=0)


def _progress_reporter(progress_callback: Any | None) -> Report:
    """A reporter that forwards to ``progress_callback`` and never lets it break the pass."""

    def _report(step: int, total: int, msg: str, pct: float) -> None:
        if progress_callback:
            try:
                progress_callback(step, total, msg, pct)
            except Exception as e:
                logger.debug("Progress callback failed: %s", e)

    return _report


# ----------------------------------------------------------------------
# Step 1: clear
# ----------------------------------------------------------------------


def _clear_old_clusters(
    self: "SpeakerClusteringService", user_id: int, off_scopes: set[int | None]
) -> None:
    """Clear ALL old clusters (including promoted); Phase 1 recreates promoted ones."""
    old_clusters_query = self.db.query(SpeakerCluster).filter(SpeakerCluster.user_id == user_id)
    if off_scopes:
        old_clusters_query = old_clusters_query.filter(
            self._scope_not_in(SpeakerCluster.organization_id, off_scopes)
        )
    old_clusters = old_clusters_query.all()
    for oc in old_clusters:
        # Remove centroid from OpenSearch
        try:
            from app.services.opensearch_service import delete_cluster_embedding

            delete_cluster_embedding(str(oc.uuid))
        except Exception:
            logger.debug("Failed to remove centroid for cluster %s", oc.uuid)
        self.db.query(Speaker).filter(Speaker.cluster_id == oc.id).update({"cluster_id": None})
        self.db.delete(oc)
    self.db.flush()


# ----------------------------------------------------------------------
# Step 5: cluster records, then Phase 2.5 and the separation scores
# ----------------------------------------------------------------------


def _speakers_of_multi_member_groups(
    self: "SpeakerClusteringService", groups: Groups
) -> tuple[list[int], dict[int, Speaker]]:
    """Batch-fetch all speakers that belong to multi-member groups."""
    multi_member_ids: list[int] = []
    for member_ids in groups.values():
        if len(member_ids) >= 2:
            multi_member_ids.extend(member_ids)

    speakers_by_id: dict[int, Speaker] = {}
    if multi_member_ids:
        batch_speakers = self.db.query(Speaker).filter(Speaker.id.in_(multi_member_ids)).all()
        speakers_by_id = {int(s.id): s for s in batch_speakers}
        self._privacy.preload_files(int(s.media_file_id) for s in batch_speakers)
    return multi_member_ids, speakers_by_id


def _refresh_speaker_index() -> None:
    """Single index refresh after all batch centroid writes.

    Must happen BEFORE the Phase 2.5 kNN queries.
    """
    try:
        from app.services.opensearch_service import get_active_speaker_index
        from app.services.opensearch_service import opensearch_client

        active_index = get_active_speaker_index()
        if opensearch_client:
            opensearch_client.indices.refresh(index=active_index)
    except Exception as e:
        logger.warning("Failed to refresh speaker index: %s", e)


def _apply_separation_scores(
    self: "SpeakerClusteringService",
    user_id: int,
    off_scopes: set[int | None],
    emb_cache: dict[int, list[float]],
) -> None:
    """Compute separation scores for all multi-member clusters."""
    all_cluster_centroids: list[np.ndarray] = []
    all_clusters_for_sep: list[SpeakerCluster] = []
    separation_query = self.db.query(SpeakerCluster).filter(
        SpeakerCluster.user_id == user_id,
        SpeakerCluster.member_count >= 2,
    )
    if off_scopes:
        separation_query = separation_query.filter(
            self._scope_not_in(SpeakerCluster.organization_id, off_scopes)
        )
    for cluster_record in separation_query.all():
        member_ids_q = [
            int(m.speaker_id)
            for m in self.db.query(SpeakerClusterMember)
            .filter(SpeakerClusterMember.cluster_id == cluster_record.id)
            .all()
        ]
        member_embs_sep = [emb_cache[sid] for sid in member_ids_q if sid in emb_cache]
        if member_embs_sep:
            c = np.mean(np.array(member_embs_sep, dtype=np.float32), axis=0)
            c_norm = np.linalg.norm(c)
            if c_norm > 1e-8:
                c = c / c_norm
                all_cluster_centroids.append(c)
                all_clusters_for_sep.append(cluster_record)

    if len(all_cluster_centroids) >= 2:
        centroid_matrix = np.array(all_cluster_centroids, dtype=np.float32)
        c_sim = centroid_matrix @ centroid_matrix.T
        np.fill_diagonal(c_sim, -1.0)  # Exclude self-similarity
        for i, cr in enumerate(all_clusters_for_sep):
            max_neighbor_sim = float(np.max(c_sim[i]))
            cr.separation_score = round(  # type: ignore[assignment]
                1.0 - max_neighbor_sim, 4
            )


def _recluster_status(
    centroid_failures: int, clusters_created: int, cleanup_residual: list[dict[str, Any]]
) -> str:
    """``partial_failure`` when over 10% of the centroids failed or the cleanup left residue."""
    if (
        centroid_failures > 0
        and clusters_created > 0
        and centroid_failures / clusters_created > 0.1
    ) or cleanup_residual:
        return "partial_failure"
    return "completed"


def batch_recluster(
    self: "SpeakerClusteringService",
    user_id: int,
    threshold: float = CLUSTER_ASSIGNMENT_THRESHOLD,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Two-phase re-clustering: profile-aware grouping then AHC discovery.

    Phase 1 (deterministic): Group speakers that already share the same
    verified profile_id into clusters — no embedding math required.

    Phase 2 (similarity): For remaining unlabeled speakers, build a full
    cosine similarity matrix from OpenSearch embeddings and discover
    clusters via AHC (Agglomerative Hierarchical Clustering) with
    complete linkage.

    Args:
        user_id: Owner user ID.
        threshold: Cosine similarity threshold for Phase 2.
        progress_callback: Optional callable(step, total, message, progress)
            for reporting progress to the caller.

    Returns:
        Summary dict with cluster counts and stats.
    """
    try:
        if threshold < 0.5 or threshold > 0.95:
            raise ValueError(f"Threshold must be in [0.5, 0.95], got {threshold}")

        # Privacy policy (#1280): a scope (organization, or personal) with voiceprints
        # off is left exactly as it is. Its clusters are neither cleared nor rebuilt
        # and its speakers are neither grouped nor matched; only scopes that are on are
        # reclustered. Read fresh, at run time, so a switch flipped after this was
        # queued is honoured.
        off_scopes = self._voiceprint_off_scopes(user_id)
        if off_scopes:
            logger.info(
                "Re-clustering user %d skips scopes with voiceprints off: %s",
                user_id,
                sorted(off_scopes, key=lambda o: -1 if o is None else o),
            )

        report = _progress_reporter(progress_callback)

        report(1, 5, "Clearing old clusters...", 0.05)
        _clear_old_clusters(self, user_id, off_scopes)

        report(2, 5, "Grouping by verified profiles...", 0.15)
        profile = group_by_profile(self, user_id, off_scopes)

        report(3, 5, "Loading unlabeled embeddings...", 0.25)
        unlabeled = load_unlabeled_embeddings(self, user_id, off_scopes, threshold)
        emb_cache = unlabeled.emb_cache
        groups, group_org = ahc_groups_per_tenant(self, unlabeled, threshold, report)

        report(5, 5, "Creating cluster records...", 0.9)
        evict_cannot_linked(groups, load_cannot_link_pairs(self, unlabeled.ordered_ids), emb_cache)
        multi_member_ids, speakers_by_id = _speakers_of_multi_member_groups(self, groups)
        evict_gender_outliers(self, groups, speakers_by_id, emb_cache)
        sim_clusters, sim_assigned, centroid_failures = create_similarity_clusters(
            self, user_id, groups, group_org, emb_cache, speakers_by_id
        )
        clusters_created = profile.clusters_created + sim_clusters
        speakers_assigned = profile.speakers_assigned + sim_assigned

        validate_groups(groups, emb_cache, threshold, sim_clusters)
        _refresh_speaker_index()

        # Phase 2.5: match singletons against profile centroids
        grouped_ids = set(multi_member_ids) if multi_member_ids else set()
        singleton_ids = [sid for sid in unlabeled.ordered_ids if sid not in grouped_ids]
        if singleton_ids and profile.clusters_created > 0:
            speakers_assigned += match_singletons_to_profile_clusters(
                self,
                user_id,
                off_scopes,
                threshold,
                singleton_ids,
                profile.clusters,
                emb_cache,
            )

        total_speakers = len(profile.profiled_speaker_ids) + unlabeled.total
        singletons = total_speakers - speakers_assigned

        _apply_separation_scores(self, user_id, off_scopes, emb_cache)

        self.db.commit()
        # Phase 2.5 joins and the stale-link repair relabelled speakers and changed
        # profile membership; their clusters' centroids and the profiles' embeddings
        # are rebuilt from the committed rows (issue #1233). Logged, not raised: the
        # re-cluster itself has committed.
        self._rename_tracker.flush(self.db)
        cleanup_residual = self._settle_assignments()
        logger.info(
            "Re-clustering complete for user %d: "
            "%d profile clusters (%d speakers), "
            "%d similarity clusters (%d speakers), "
            "%d singletons",
            user_id,
            profile.clusters_created,
            profile.speakers_assigned,
            sim_clusters,
            sim_assigned,
            singletons,
        )

        return {
            "status": _recluster_status(centroid_failures, clusters_created, cleanup_residual),
            "total_speakers": total_speakers,
            "profile_clusters": profile.clusters_created,
            "profile_speakers_assigned": profile.speakers_assigned,
            "similarity_clusters": sim_clusters,
            "similarity_speakers_assigned": sim_assigned,
            "clusters_created": clusters_created,
            "speakers_assigned": speakers_assigned,
            "singletons": singletons,
            "centroid_failures": centroid_failures,
            "cleanup_residual_stages": sorted({str(r["stage"]) for r in cleanup_residual}),
            "skipped_by_policy_scopes": len(off_scopes),
        }

    except Exception as e:
        logger.error("Error in batch re-clustering: %s", e)
        self.db.rollback()
        self._rename_tracker.discard()
        self._assigned = SpeakerImpact()
        raise
