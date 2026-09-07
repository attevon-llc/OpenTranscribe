"""
Speaker Clustering Service for pre-clustering unnamed speakers across files.

Uses a hybrid approach:
- Real-time: kNN against cluster centroids in OpenSearch + threshold assignment
- Batch: GPU-accelerated cosine similarity (PyTorch) with AHC (complete linkage)

Cluster centroids are stored in OpenSearch with document_type="cluster".
"""

import logging
import math
from collections import defaultdict
from typing import Any
from uuid import uuid4

import numpy as np
from sqlalchemy import case
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import subqueryload

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from app.services.speaker_rename_tracker import SpeakerRenameTracker
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)

# Clustering thresholds
# With complete linkage, 0.75 nearly guarantees same speaker identity
CLUSTER_ASSIGNMENT_THRESHOLD = 0.75
# Cross-gender cluster assignment requires higher similarity to prevent
# gender-mismatched speakers from joining clusters.  A speaker whose
# predicted gender differs from the cluster's dominant gender must exceed
# this threshold instead of CLUSTER_ASSIGNMENT_THRESHOLD.
GENDER_CROSS_THRESHOLD = 0.85
# In batch_recluster post-processing, minority-gender members whose
# embedding similarity to the group centroid falls below this value are
# auto-evicted.  Both signals (gender mismatch + weak embedding) must
# agree — this avoids removing legitimate same-speaker matches.
GENDER_OUTLIER_EVICT_THRESHOLD = 0.70
# Rows of the similarity chunk processed at a time.
# 500 rows x N cols x 4 bytes keeps each chunk under ~30 MB
# even at 15 000 unlabeled speakers.
SIM_CHUNK = 500


class SpeakerClusteringService:
    """Service for clustering speakers across media files.

    Tenant scope (cloud edition): clusters are tenant-scoped like every other
    speaker-plane object. ``SpeakerCluster.organization_id`` (NULL = personal)
    is stamped at creation from the member speakers' FILE org and mirrored
    onto the centroid doc (``store_cluster_embedding``); cluster-centroid kNN
    queries are gated by the searching speaker's file org
    (``_media_file_org_id`` -> ``find_matching_clusters``), so org files join
    org clusters and personal files join personal clusters — never across.
    ``batch_recluster`` partitions the Phase-2 similarity graph per tenant
    scope so a member's org and personal speakers are never AHC-linked.
    Community edition: every file org is NULL, one personal partition, no org
    field on any doc — behavior identical to the pre-tenant code.
    """

    def __init__(self, db: Session):
        self.db = db
        # A clustering pass relabels many speakers across many files at once, and
        # every one of those files was indexed long ago. Renames are recorded as
        # they happen and dispatched once per commit — never per row, which would
        # queue one update_by_query per speaker per file (issue #432).
        self._rename_tracker = SpeakerRenameTracker()

    def _media_file_org_id(self, media_file_id: int | None) -> int | None:
        """Resolve the tenant scope of a speaker via its media file's org."""
        if media_file_id is None:
            return None
        row = self.db.query(MediaFile.organization_id).filter(MediaFile.id == media_file_id).first()
        return int(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------
    # Real-time cluster assignment (called after transcription)
    # ------------------------------------------------------------------

    def find_or_create_cluster(
        self,
        speaker_id: int,
        user_id: int,
        embedding: list[float],
        threshold: float = CLUSTER_ASSIGNMENT_THRESHOLD,
    ) -> SpeakerCluster | None:
        """Assign a speaker to an existing cluster or create a new singleton.

        Uses kNN search against cluster centroids in OpenSearch.

        Args:
            speaker_id: Database ID of the speaker.
            user_id: Owner user ID.
            embedding: L2-normalized speaker embedding vector.
            threshold: Minimum cosine similarity to join an existing cluster.

        Returns:
            The cluster the speaker was assigned to, or None on error.
        """
        try:
            speaker = (
                self.db.query(Speaker).filter(Speaker.id == speaker_id).with_for_update().first()
            )
            if not speaker:
                logger.warning("Speaker %s not found", speaker_id)
                return None

            # Skip if already in a cluster
            if speaker.cluster_id:
                cluster = (
                    self.db.query(SpeakerCluster)
                    .filter(SpeakerCluster.id == speaker.cluster_id)
                    .first()
                )
                return cluster  # type: ignore[no-any-return]

            # Search for matching cluster centroids (gated by the file's org)
            from app.services.opensearch_service import find_matching_clusters

            organization_id = self._media_file_org_id(int(speaker.media_file_id))
            matches = find_matching_clusters(
                embedding, user_id, k=5, threshold=threshold, organization_id=organization_id
            )

            if matches:
                # Iterate through matches to find first non-blocked cluster
                for i, candidate in enumerate(matches):
                    margin = (
                        candidate["similarity"] - matches[i + 1]["similarity"]
                        if i + 1 < len(matches)
                        else 1.0
                    )
                    cluster_uuid = candidate["cluster_uuid"]
                    cluster = (
                        self.db.query(SpeakerCluster)
                        .filter(
                            SpeakerCluster.uuid == cluster_uuid,
                            SpeakerCluster.user_id == user_id,
                        )
                        .first()
                    )
                    if not cluster:
                        continue

                    # Check constraints before assigning
                    if self._is_speaker_blocked_from_cluster(speaker_id, cluster):
                        logger.debug(
                            "Speaker %s blocked from cluster %s by constraint",
                            speaker_id,
                            cluster_uuid,
                        )
                        continue

                    # Gender-aware threshold: cross-gender matches need higher similarity
                    if speaker.predicted_gender and (cluster.member_count or 0) >= 2:
                        dominant = self._get_cluster_dominant_gender(cluster)
                        if (
                            dominant
                            and dominant != speaker.predicted_gender
                            and candidate["similarity"] < GENDER_CROSS_THRESHOLD
                        ):
                            logger.debug(
                                "Speaker %s gender (%s) differs from cluster %s dominant (%s), "
                                "sim %.3f < cross-gender threshold %.3f — skipping",
                                speaker_id,
                                speaker.predicted_gender,
                                cluster_uuid,
                                dominant,
                                candidate["similarity"],
                                GENDER_CROSS_THRESHOLD,
                            )
                            continue

                    self._add_speaker_to_cluster(
                        speaker, cluster, candidate["similarity"], margin=margin
                    )
                    # Auto-propagate profile if cluster is promoted
                    if cluster.promoted_to_profile_id:
                        speaker.profile_id = cluster.promoted_to_profile_id
                        profile = (
                            self.db.query(SpeakerProfile)
                            .filter(SpeakerProfile.id == cluster.promoted_to_profile_id)
                            .first()
                        )
                        if profile:
                            self._rename_tracker.record(
                                int(speaker.media_file_id),
                                canonical_speaker_label_for_row(speaker),
                                str(profile.name),
                            )
                            speaker.display_name = profile.name
                            speaker.verified = True
                    self._update_cluster_centroid(cluster, user_id)
                    return cluster  # type: ignore[no-any-return]

            # No match — create a new singleton cluster in the file's scope
            cluster = self._create_singleton_cluster(
                speaker, user_id, embedding, organization_id=organization_id
            )
            return cluster  # type: ignore[no-any-return]

        except Exception as e:
            logger.error("Error in find_or_create_cluster for speaker %s: %s", speaker_id, e)
            self.db.rollback()
            self._rename_tracker.discard()
            return None

    def cluster_speakers_for_file(
        self,
        media_file_id: int,
        user_id: int,
    ) -> list[SpeakerCluster]:
        """Cluster all speakers in a media file after transcription.

        Args:
            media_file_id: Media file ID.
            user_id: Owner user ID.

        Returns:
            List of clusters speakers were assigned to.
        """
        speakers = (
            self.db.query(Speaker)
            .filter(
                Speaker.media_file_id == media_file_id,
                Speaker.user_id == user_id,
            )
            .all()
        )

        if not speakers:
            logger.info("No speakers found for media file %s", media_file_id)
            return []

        clusters: list[SpeakerCluster] = []
        for speaker in speakers:
            embedding = self._get_speaker_embedding(speaker)
            if embedding is None:
                continue
            cluster = self.find_or_create_cluster(int(speaker.id), user_id, embedding)
            if cluster:
                clusters.append(cluster)

        self.db.commit()
        # Any speaker that joined a promoted cluster was relabelled above; the
        # chunk plane still carries the diarizer label it was indexed with.
        self._rename_tracker.flush(self.db)
        return clusters

    # ------------------------------------------------------------------
    # Batch clustering (on-demand)
    # ------------------------------------------------------------------

    def batch_recluster(
        self,
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

            from app.services.opensearch_service import iter_speaker_embeddings

            def _report(step: int, total: int, msg: str, pct: float) -> None:
                if progress_callback:
                    try:
                        progress_callback(step, total, msg, pct)
                    except Exception as e:
                        logger.debug("Progress callback failed: %s", e)

            # ----------------------------------------------------------
            # Step 1: Clear ALL old clusters (including promoted)
            # Phase 1 will recreate promoted clusters from profiles.
            # ----------------------------------------------------------
            _report(1, 5, "Clearing old clusters...", 0.05)
            old_clusters = (
                self.db.query(SpeakerCluster).filter(SpeakerCluster.user_id == user_id).all()
            )
            for oc in old_clusters:
                # Remove centroid from OpenSearch
                try:
                    from app.services.opensearch_service import delete_cluster_embedding

                    delete_cluster_embedding(str(oc.uuid))
                except Exception:
                    logger.debug("Failed to remove centroid for cluster %s", oc.uuid)
                self.db.query(Speaker).filter(Speaker.cluster_id == oc.id).update(
                    {"cluster_id": None}
                )
                self.db.delete(oc)
            self.db.flush()

            clusters_created = 0
            speakers_assigned = 0
            centroid_failures = 0

            # ----------------------------------------------------------
            # Step 2 (Phase 1): Profile-based clustering
            # Pure DB query — no embeddings loaded.
            # ----------------------------------------------------------
            _report(2, 5, "Grouping by verified profiles...", 0.15)
            profile_speakers = (
                self.db.query(Speaker)
                .filter(
                    Speaker.user_id == user_id,
                    Speaker.profile_id.isnot(None),
                )
                .all()
            )

            profile_groups: dict[int, list[Speaker]] = defaultdict(list)
            profiled_speaker_ids: set[int] = set()
            for spk in profile_speakers:
                assert spk.profile_id is not None  # filtered on profile_id.isnot(None) above
                profile_groups[int(spk.profile_id)].append(spk)
                profiled_speaker_ids.add(int(spk.id))

            # Embedding cache: speaker_id -> embedding (reused across phases)
            emb_cache: dict[int, list[float]] = {}

            phase1_profile_clusters: dict[str, SpeakerCluster] = {}
            for profile_id, members in profile_groups.items():
                if len(members) < 2:
                    continue

                profile = (
                    self.db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
                )
                # Tenant scope: a profile-backed cluster lives in its profile's
                # scope (profiles are already org-stamped at creation).
                profile_org = (
                    int(profile.organization_id)
                    if profile and profile.organization_id is not None
                    else None
                )
                cluster = SpeakerCluster(
                    uuid=uuid4(),
                    user_id=user_id,
                    organization_id=profile_org,
                    member_count=0,
                    label=profile.name if profile else None,
                    promoted_to_profile_id=profile.id if profile else None,
                )
                self.db.add(cluster)
                self.db.flush()
                if profile:
                    profile.source_cluster_id = cluster.id

                self._bulk_add_speakers_to_cluster(members, cluster, 1.0)
                speakers_assigned += len(members)

                self._update_cluster_centroid(cluster, user_id, refresh=False)
                clusters_created += 1
                phase1_profile_clusters[str(cluster.uuid)] = cluster

            profile_clusters = clusters_created
            profile_assigned = speakers_assigned
            logger.info(
                "Phase 1 (profile grouping): %d clusters, %d speakers (%d profiles) for user %d",
                profile_clusters,
                profile_assigned,
                len(profile_groups),
                user_id,
            )

            # ----------------------------------------------------------
            # Step 3 (Phase 2): GPU-accelerated cosine similarity + AHC
            #
            # a) Query unlabeled speaker UUIDs from DB
            # b) Fetch embeddings from OpenSearch
            # c) Partition per tenant scope (the speaker's file org)
            # d) Per partition: chunked cosine matmul + AHC (complete
            #    linkage) — see _compute_similarity_groups
            # ----------------------------------------------------------
            _report(3, 5, "Loading unlabeled embeddings...", 0.25)
            unlabeled_rows = (
                self.db.query(Speaker.id, Speaker.uuid)
                .filter(
                    Speaker.user_id == user_id,
                    Speaker.profile_id.is_(None),
                )
                .all()
            )
            unlabeled_uuid_to_id: dict[str, int] = {str(r.uuid): int(r.id) for r in unlabeled_rows}
            unlabeled_uuids = list(unlabeled_uuid_to_id.keys())
            total_unlabeled = len(unlabeled_uuids)

            logger.info(
                "Phase 2: %d unlabeled speakers for user %d (threshold=%.2f)",
                total_unlabeled,
                user_id,
                threshold,
            )

            # Collect embeddings into aligned index arrays
            ordered_ids: list[int] = []
            emb_rows: list[list[float]] = []

            for batch in iter_speaker_embeddings(
                user_id, speaker_uuids=unlabeled_uuids, batch_size=500
            ):
                for item in batch:
                    sid = unlabeled_uuid_to_id.get(item["speaker_uuid"])
                    if sid is not None:
                        ordered_ids.append(sid)
                        emb_rows.append(item["embedding"])

            # Cache embeddings so centroid computation skips OpenSearch
            for i, sid in enumerate(ordered_ids):
                emb_cache[sid] = emb_rows[i]

            # Validate embedding dimensions are consistent
            if emb_rows:
                expected_dim = len(emb_rows[0])
                valid_indices = [i for i, e in enumerate(emb_rows) if len(e) == expected_dim]
                if len(valid_indices) < len(emb_rows):
                    skipped = len(emb_rows) - len(valid_indices)
                    logger.warning(
                        "Skipped %d embeddings with mismatched dimensions (expected %d)",
                        skipped,
                        expected_dim,
                    )
                    emb_rows = [emb_rows[i] for i in valid_indices]
                    ordered_ids = [ordered_ids[i] for i in valid_indices]

            # ----------------------------------------------------------
            # Tenant partitioning: the similarity graph is built PER tenant
            # scope (the speaker's FILE org, None = personal) so a member's
            # org and personal speakers are never AHC-linked into one
            # cluster. Community edition: every file org is NULL -> a single
            # personal partition -> identical grouping to the unpartitioned
            # code.
            # ----------------------------------------------------------
            org_by_speaker: dict[int, int | None] = {}
            if ordered_ids:
                for sid, org in (
                    self.db.query(Speaker.id, MediaFile.organization_id)
                    .join(MediaFile, MediaFile.id == Speaker.media_file_id)
                    .filter(Speaker.id.in_(ordered_ids))
                    .all()
                ):
                    org_by_speaker[int(sid)] = int(org) if org is not None else None

            partitions: dict[int | None, tuple[list[int], list[list[float]]]] = {}
            for sid, emb_row in zip(ordered_ids, emb_rows, strict=True):
                p_ids, p_rows = partitions.setdefault(org_by_speaker.get(sid), ([], []))
                p_ids.append(sid)
                p_rows.append(emb_row)
            del emb_rows

            # Groups keyed by (partition index, AHC label); group_org carries
            # each group's tenant scope through to cluster creation.
            groups: dict[tuple[int, int], list[int]] = {}
            group_org: dict[tuple[int, int], int | None] = {}
            for p_idx, (scope_org, (p_ids, p_rows)) in enumerate(partitions.items()):
                if len(p_ids) < 2:
                    continue
                p_groups = self._compute_similarity_groups(p_rows, p_ids, threshold, _report)
                for lbl, g_member_ids in p_groups.items():
                    groups[(p_idx, lbl)] = g_member_ids
                    group_org[(p_idx, lbl)] = scope_org
                logger.info(
                    "Phase 2 AHC (%s): %d groups (>= 2 members: %d) from %d speakers",
                    f"org {scope_org}" if scope_org is not None else "personal",
                    len(p_groups),
                    sum(1 for g in p_groups.values() if len(g) >= 2),
                    len(p_ids),
                )
            del partitions

            # ----------------------------------------------------------
            # Step 5: Create cluster records from AHC groups
            # ----------------------------------------------------------
            _report(5, 5, "Creating cluster records...", 0.9)
            sim_clusters = 0
            sim_assigned = 0

            # Post-AHC constraint enforcement: remove speakers that violate
            # cannot-link constraints from their assigned groups.
            from app.models.media import SpeakerCannotLink

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

            if cannot_link_set:
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
                                g_embs = [
                                    emb_cache[s]
                                    for s in member_ids_g
                                    if s in emb_cache and s not in evicted
                                ]
                                if g_embs:
                                    g_cent = np.mean(g_embs, axis=0)
                                    g_norm = np.linalg.norm(g_cent)
                                    if g_norm > 1e-8:
                                        g_cent = g_cent / g_norm
                                    sim_a = (
                                        float(np.dot(emb_cache[sid_a], g_cent))
                                        if sid_a in emb_cache
                                        else 0.0
                                    )
                                    sim_b = (
                                        float(np.dot(emb_cache[sid_b], g_cent))
                                        if sid_b in emb_cache
                                        else 0.0
                                    )
                                    evicted.add(sid_b if sim_a >= sim_b else sid_a)
                                else:
                                    evicted.add(sid_b)
                    if evicted:
                        groups[label_key] = [s for s in member_ids_g if s not in evicted]
                        logger.debug(
                            "Constraint enforcement: evicted %d speaker(s) from AHC group",
                            len(evicted),
                        )

            # Batch-fetch all speakers that belong to multi-member groups
            multi_member_ids: list[int] = []
            for member_ids in groups.values():
                if len(member_ids) >= 2:
                    multi_member_ids.extend(member_ids)

            speakers_by_id: dict[int, Speaker] = {}
            if multi_member_ids:
                batch_speakers = (
                    self.db.query(Speaker).filter(Speaker.id.in_(multi_member_ids)).all()
                )
                speakers_by_id = {int(s.id): s for s in batch_speakers}

            # Gender-aware eviction: for each group, if there's a clear
            # dominant gender, evict minority-gender members whose embedding
            # similarity to the group centroid is below the eviction threshold.
            # Dual signal required: gender mismatch + weak embedding.
            gender_evicted_total = 0
            for label_key in list(groups.keys()):
                member_ids_g = groups[label_key]
                if len(member_ids_g) < 3:
                    continue
                # Count genders in this group
                g_counts: dict[str, list[int]] = {}
                for sid in member_ids_g:
                    s = speakers_by_id.get(sid)
                    g = s.predicted_gender if s else None
                    if g:
                        g_counts.setdefault(g, []).append(sid)
                if len(g_counts) < 2:
                    continue
                # Find dominant gender (must be clear majority: >= 2/3)
                sorted_g = sorted(g_counts.items(), key=lambda x: len(x[1]), reverse=True)
                dominant_g, dominant_ids = sorted_g[0]
                total_gendered = sum(len(v) for v in g_counts.values())
                if len(dominant_ids) < total_gendered * 2 / 3:
                    continue  # No clear majority — skip
                # Compute group centroid from majority members
                maj_embs = [emb_cache[sid] for sid in dominant_ids if sid in emb_cache]
                if not maj_embs:
                    continue
                g_cent = np.mean(maj_embs, axis=0)
                g_norm = np.linalg.norm(g_cent)
                if g_norm > 1e-8:
                    g_cent = g_cent / g_norm
                # Check minority members: evict only if sim < threshold
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
                if evict_ids:
                    groups[label_key] = [s for s in member_ids_g if s not in evict_ids]
                    gender_evicted_total += len(evict_ids)
            if gender_evicted_total:
                logger.info(
                    "Gender-aware eviction: removed %d speaker(s) from AHC groups",
                    gender_evicted_total,
                )

            from app.services.opensearch_service import store_cluster_embedding

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

                # Compute centroid and per-member confidence from cached
                # embeddings before adding members to cluster
                member_embs = [emb_cache[sid] for sid in member_ids if sid in emb_cache]
                per_member_conf: dict[int, float] = {}
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

                # Bulk-add all members with per-member confidence
                cluster_speakers = [
                    speakers_by_id[sid] for sid in member_ids if sid in speakers_by_id
                ]
                added = self._bulk_add_speakers_to_cluster(
                    cluster_speakers,
                    cluster,
                    per_member_conf or 0.0,
                )
                sim_assigned += added

                if member_embs:
                    # Quality score: average centroid similarity (same metric
                    # shown per-member, so the cluster score is consistent
                    # with individual member scores users see in the UI).
                    # min_similarity: tightest pairwise cosine similarity
                    # (worst-case pair within the cluster).
                    if len(member_embs) >= 2:
                        normed = arr / np.linalg.norm(
                            arr,
                            axis=1,
                            keepdims=True,
                        ).clip(1e-8)
                        # Average centroid similarity for quality score
                        cluster.quality_score = float(  # type: ignore[assignment]
                            np.mean(normed @ centroid)
                        )
                        # Min pairwise for worst-case indicator
                        sim_mat = normed @ normed.T
                        iu = np.triu_indices(len(member_embs), k=1)
                        cluster.min_similarity = float(  # type: ignore[assignment]
                            np.min(sim_mat[iu])
                        )
                    else:
                        cluster.quality_score = 1.0  # type: ignore[assignment]
                        cluster.min_similarity = 1.0  # type: ignore[assignment]

                    cluster.representative_speaker_id = member_ids[0]  # type: ignore[assignment]

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
                        centroid_failures += 1

                sim_clusters += 1

            clusters_created += sim_clusters
            speakers_assigned += sim_assigned

            # ----------------------------------------------------------
            # Post-clustering validation: verify all pairwise similarities
            # within each AHC cluster meet the threshold. Log warnings
            # for any violating pairs (indicates numerical edge cases).
            # ----------------------------------------------------------
            if sim_clusters > 0 and emb_cache:
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
                        "Post-clustering validation: all %d AHC clusters "
                        "pass threshold check (>=%.2f)",
                        sim_clusters,
                        threshold,
                    )

            # Single index refresh after all batch centroid writes
            # (must happen BEFORE Phase 2.5 kNN queries)
            try:
                from app.services.opensearch_service import get_active_speaker_index
                from app.services.opensearch_service import opensearch_client

                active_index = get_active_speaker_index()
                if opensearch_client:
                    opensearch_client.indices.refresh(index=active_index)
            except Exception as e:
                logger.warning("Failed to refresh speaker index: %s", e)

            # ----------------------------------------------------------
            # Phase 2.5: Match singletons against profile centroids
            # ----------------------------------------------------------
            grouped_ids = set(multi_member_ids) if multi_member_ids else set()
            singleton_ids = [sid for sid in ordered_ids if sid not in grouped_ids]
            if singleton_ids and profile_clusters > 0:
                from app.services.opensearch_service import find_matching_clusters

                # Build map: include Phase 1 clusters + any pre-existing promoted clusters
                profile_cluster_map: dict[str, SpeakerCluster] = dict(phase1_profile_clusters)
                for pc in (
                    self.db.query(SpeakerCluster)
                    .filter(
                        SpeakerCluster.user_id == user_id,
                        SpeakerCluster.promoted_to_profile_id.isnot(None),
                    )
                    .all()
                ):
                    profile_cluster_map[str(pc.uuid)] = pc

                if profile_cluster_map:
                    singleton_speakers = (
                        self.db.query(Speaker).filter(Speaker.id.in_(singleton_ids)).all()
                    )
                    # Tenant gate: each singleton's cluster search is scoped by
                    # ITS file's org (batch-resolved to avoid N queries).
                    org_by_file: dict[int, int | None] = {
                        int(fid): (int(org) if org is not None else None)
                        for fid, org in self.db.query(MediaFile.id, MediaFile.organization_id)
                        .filter(
                            MediaFile.id.in_({int(s.media_file_id) for s in singleton_speakers})
                        )
                        .all()
                    }
                    matched_singletons = 0
                    for spk in singleton_speakers:
                        emb = emb_cache.get(int(spk.id))
                        if emb is None:
                            continue
                        spk_org = org_by_file.get(int(spk.media_file_id))
                        matches = find_matching_clusters(
                            emb,
                            user_id,
                            k=5,
                            threshold=threshold,
                            organization_id=int(spk_org) if spk_org is not None else None,
                        )
                        for match in matches:
                            matched_cluster = profile_cluster_map.get(match["cluster_uuid"])
                            if not matched_cluster:
                                continue
                            # Check constraints before assigning singleton
                            if self._is_speaker_blocked_from_cluster(int(spk.id), matched_cluster):
                                logger.debug(
                                    "Singleton %s blocked from cluster %s by constraint",
                                    spk.id,
                                    matched_cluster.uuid,
                                )
                                continue
                            self._add_speaker_to_cluster(spk, matched_cluster, match["similarity"])
                            speakers_assigned += 1
                            matched_singletons += 1
                            break  # Assigned to first valid match
                    if matched_singletons:
                        logger.info(
                            "Phase 2.5: matched %d singletons to profile clusters",
                            matched_singletons,
                        )

            total_speakers = len(profiled_speaker_ids) + total_unlabeled
            singletons = total_speakers - speakers_assigned

            # ----------------------------------------------------------
            # Compute separation scores for all multi-member clusters
            # ----------------------------------------------------------
            all_cluster_centroids: list[np.ndarray] = []
            all_clusters_for_sep: list[SpeakerCluster] = []
            for cluster_record in (
                self.db.query(SpeakerCluster)
                .filter(
                    SpeakerCluster.user_id == user_id,
                    SpeakerCluster.member_count >= 2,
                )
                .all()
            ):
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

            self.db.commit()
            logger.info(
                "Re-clustering complete for user %d: "
                "%d profile clusters (%d speakers), "
                "%d similarity clusters (%d speakers), "
                "%d singletons",
                user_id,
                profile_clusters,
                profile_assigned,
                sim_clusters,
                sim_assigned,
                singletons,
            )

            status = "completed"
            if (
                centroid_failures > 0
                and clusters_created > 0
                and centroid_failures / clusters_created > 0.1
            ):
                status = "partial_failure"

            return {
                "status": status,
                "total_speakers": total_speakers,
                "profile_clusters": profile_clusters,
                "profile_speakers_assigned": profile_assigned,
                "similarity_clusters": sim_clusters,
                "similarity_speakers_assigned": sim_assigned,
                "clusters_created": clusters_created,
                "speakers_assigned": speakers_assigned,
                "singletons": singletons,
                "centroid_failures": centroid_failures,
            }

        except Exception as e:
            logger.error("Error in batch re-clustering: %s", e)
            self.db.rollback()
            raise

    def _compute_similarity_groups(
        self,
        emb_rows: list[list[float]],
        ordered_ids: list[int],
        threshold: float,
        report: Any,
    ) -> dict[int, list[int]]:
        """Cosine similarity matrix + AHC (complete linkage) for one partition.

        PyTorch on CUDA for fast matrix operations, CPU fallback. Called once
        per tenant partition by ``batch_recluster`` — the returned groups map
        AHC labels to speaker ids and include singleton groups (filtered by
        the caller).

        GPU memory budget (worst case 15k x 512-dim float16):
          M           = 15000 x 512 x 2  ~  15 MB VRAM
          M.T         = transposed copy     ~  15 MB VRAM
          sim chunk   = 2000 x 15000 x 2  ~  60 MB VRAM
          peak total  ~  90 MB VRAM (trivial for any GPU)
        """
        # torch and scipy are imported here, not at module scope, per the repo convention
        # for heavy optional deps. This module is reachable from `app.api.router`, so a
        # top-level `import torch` was pulled into every process that imports `app.main` —
        # including all 48 pytest-xdist workers, at ~6.3 s of CPU each (issue #431).
        import torch
        from scipy.cluster.hierarchy import fcluster
        from scipy.cluster.hierarchy import linkage
        from scipy.spatial.distance import squareform

        n = len(emb_rows)
        if n < 2:
            return {}

        # Use GPU for large matrices, CPU for small ones.
        # GPU is faster for matmul but creates a CUDA context (~1.4GB)
        # that persists in prefork worker children. For small speaker
        # counts, CPU is fast enough and avoids the context overhead.
        use_gpu = torch.cuda.is_available() and n >= 500
        device = torch.device("cuda:0" if use_gpu else "cpu")
        dtype = torch.float16 if use_gpu else torch.float32
        logger.info(
            "Phase 2: computing similarities for %d speakers on %s (dim=%d)",
            n,
            device,
            len(emb_rows[0]),
        )

        report(4, 5, "Computing cosine similarities...", 0.4)

        # Pre-allocate CPU float32 array for full similarity matrix
        emb_matrix = np.array(emb_rows, dtype=np.float32)
        sim_full = np.empty((n, n), dtype=np.float32)

        try:
            M = torch.tensor(
                emb_matrix,
                dtype=dtype,
                device=device,
            )
            del emb_matrix

            # Safe L2-normalize: clamp norms to avoid NaN from zero-length embeddings
            norms = M.norm(dim=1, keepdim=True).clamp(min=1e-8)
            M = M / norms

            # Pre-transpose once — .T is not contiguous, make it so
            MT = M.T.contiguous()

            sim_chunk = 2000 if use_gpu else SIM_CHUNK
            total_chunks = math.ceil(n / sim_chunk)

            for ci in range(total_chunks):
                start = ci * sim_chunk
                end = min(start + sim_chunk, n)

                # (chunk, D) @ (D, N) -> (chunk, N) cosine sims
                sim_block = torch.mm(M[start:end], MT)
                sim_full[start:end] = sim_block.float().cpu().numpy()
                del sim_block

                report(
                    4,
                    5,
                    "Computing cosine similarities...",
                    0.4 + 0.3 * (ci + 1) / total_chunks,
                )

            del M, MT
        finally:
            if use_gpu:
                torch.cuda.empty_cache()
                # Release the CUDA context from this prefork child.
                # Without this, each child holds ~1.4GB+ of GPU memory
                # until it exits (max-tasks-per-child).
                torch.cuda.ipc_collect()
                import gc

                gc.collect()
                torch.cuda.empty_cache()

        # --- AHC with complete linkage ---
        report(4, 5, "Clustering with AHC (complete linkage)...", 0.75)

        # Ensure diagonal is exactly 1.0 and matrix is symmetric
        np.fill_diagonal(sim_full, 1.0)
        sim_full = np.maximum(sim_full, sim_full.T)

        # Convert similarity to distance
        dist = 1.0 - sim_full
        np.clip(dist, 0.0, 2.0, out=dist)
        np.fill_diagonal(dist, 0.0)
        del sim_full

        # scipy AHC
        condensed = squareform(dist, checks=False)
        del dist
        Z = linkage(condensed, method="complete")
        del condensed
        labels = fcluster(Z, t=1.0 - threshold, criterion="distance")
        del Z

        # Build groups from fcluster labels
        groups: dict[int, list[int]] = defaultdict(list)
        for idx, label in enumerate(labels):
            groups[int(label)].append(ordered_ids[idx])
        del labels

        return dict(groups)

    # ------------------------------------------------------------------
    # Cluster operations (merge, split, promote)
    # ------------------------------------------------------------------

    def merge_clusters(
        self,
        source_uuid: str,
        target_uuid: str,
        user_id: int,
    ) -> SpeakerCluster | None:
        """Merge source cluster into target. All members move to target.

        Args:
            source_uuid: UUID of the cluster to dissolve.
            target_uuid: UUID of the cluster to absorb members.
            user_id: Owner user ID.

        Returns:
            The target cluster after merge, or None on error.
        """
        try:
            # Lock both clusters in consistent UUID order to prevent deadlocks
            first_uuid, second_uuid = sorted([source_uuid, target_uuid])
            self.db.query(SpeakerCluster).filter(
                SpeakerCluster.uuid == first_uuid,
                SpeakerCluster.user_id == user_id,
            ).with_for_update().first()
            if first_uuid != second_uuid:
                self.db.query(SpeakerCluster).filter(
                    SpeakerCluster.uuid == second_uuid,
                    SpeakerCluster.user_id == user_id,
                ).with_for_update().first()

            source = (
                self.db.query(SpeakerCluster)
                .filter(SpeakerCluster.uuid == source_uuid, SpeakerCluster.user_id == user_id)
                .first()
            )
            target = (
                self.db.query(SpeakerCluster)
                .filter(SpeakerCluster.uuid == target_uuid, SpeakerCluster.user_id == user_id)
                .first()
            )

            if not source or not target:
                logger.warning(
                    "Cluster not found for merge: source=%s, target=%s", source_uuid, target_uuid
                )
                return None

            # Move members
            source_members = (
                self.db.query(SpeakerClusterMember)
                .filter(SpeakerClusterMember.cluster_id == source.id)
                .all()
            )

            # Batch-fetch existing speaker_ids in target to avoid per-member queries
            existing_speaker_ids = {
                int(r.speaker_id)
                for r in self.db.query(SpeakerClusterMember.speaker_id)
                .filter(SpeakerClusterMember.cluster_id == target.id)
                .all()
            }

            # Batch-fetch all speakers referenced by source members
            member_speaker_ids = [int(m.speaker_id) for m in source_members]
            speakers_by_id = {
                int(s.id): s
                for s in self.db.query(Speaker).filter(Speaker.id.in_(member_speaker_ids)).all()
            }

            for member in source_members:
                if int(member.speaker_id) in existing_speaker_ids:
                    self.db.delete(member)
                else:
                    member.cluster_id = target.id  # type: ignore[assignment]

                # Update speaker FK
                speaker = speakers_by_id.get(int(member.speaker_id))
                if speaker:
                    speaker.cluster_id = target.id  # type: ignore[assignment]

            # Update target count
            target.member_count = (  # type: ignore[assignment]
                self.db.query(SpeakerClusterMember)
                .filter(SpeakerClusterMember.cluster_id == target.id)
                .count()
            )

            # Remove source cluster centroid from OpenSearch
            try:
                from app.services.opensearch_service import delete_cluster_embedding

                delete_cluster_embedding(str(source.uuid))
            except Exception as e:
                logger.warning("Could not delete source cluster embedding: %s", e)

            self.db.delete(source)
            self._update_cluster_centroid(target, user_id)
            self.db.commit()

            logger.info("Merged cluster %s into %s", source_uuid, target_uuid)
            return target  # type: ignore[no-any-return]

        except Exception as e:
            logger.error("Error merging clusters: %s", e)
            self.db.rollback()
            return None

    def split_cluster(
        self,
        cluster_uuid: str,
        speaker_uuids: list[str],
        user_id: int,
    ) -> SpeakerCluster | None:
        """Split specified speakers into a new cluster.

        Args:
            cluster_uuid: UUID of the source cluster.
            speaker_uuids: UUIDs of speakers to move to the new cluster.
            user_id: Owner user ID.

        Returns:
            The newly created cluster, or None on error.
        """
        try:
            source = (
                self.db.query(SpeakerCluster)
                .filter(SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id)
                .with_for_update()
                .first()
            )
            if not source:
                logger.warning("Cluster %s not found", cluster_uuid)
                return None

            # Resolve speaker IDs
            speakers_to_move = (
                self.db.query(Speaker)
                .filter(Speaker.uuid.in_(speaker_uuids), Speaker.user_id == user_id)
                .all()
            )
            if not speakers_to_move:
                logger.warning("No valid speakers to split")
                return None

            speakers_to_move = [s for s in speakers_to_move if s.cluster_id == source.id]
            if not speakers_to_move:
                logger.warning("No speakers from source cluster to split")
                return None

            # Create new cluster (same tenant scope as the source — members
            # can only come from the source cluster).
            new_cluster = SpeakerCluster(
                uuid=uuid4(),
                user_id=user_id,
                organization_id=source.organization_id,
                member_count=0,
            )
            self.db.add(new_cluster)
            self.db.flush()

            # Move members
            for speaker in speakers_to_move:
                # Remove from source membership
                self.db.query(SpeakerClusterMember).filter(
                    SpeakerClusterMember.cluster_id == source.id,
                    SpeakerClusterMember.speaker_id == speaker.id,
                ).delete()

                # Add to new cluster
                member = SpeakerClusterMember(
                    uuid=uuid4(),
                    cluster_id=new_cluster.id,
                    speaker_id=speaker.id,
                    confidence=0.0,
                )
                self.db.add(member)
                speaker.cluster_id = new_cluster.id  # type: ignore[assignment]

            # Update counts
            new_cluster.member_count = len(speakers_to_move)  # type: ignore[assignment]
            source.member_count = (  # type: ignore[assignment]
                self.db.query(SpeakerClusterMember)
                .filter(SpeakerClusterMember.cluster_id == source.id)
                .count()
            )

            # Update centroids
            self._update_cluster_centroid(source, user_id)
            self._update_cluster_centroid(new_cluster, user_id)

            # Delete source if empty (fresh count to avoid stale ORM attribute)
            remaining = (
                self.db.query(SpeakerClusterMember)
                .filter(SpeakerClusterMember.cluster_id == source.id)
                .count()
            )
            if remaining == 0:
                try:
                    from app.services.opensearch_service import delete_cluster_embedding

                    delete_cluster_embedding(str(source.uuid))
                except Exception:
                    logger.debug("Failed to remove cluster embedding from OpenSearch")
                self.db.delete(source)

            self.db.commit()
            logger.info("Split %d speakers from cluster %s", len(speakers_to_move), cluster_uuid)
            return new_cluster

        except Exception as e:
            logger.error("Error splitting cluster: %s", e)
            self.db.rollback()
            return None

    def promote_cluster_to_profile(
        self,
        cluster_uuid: str,
        name: str,
        user_id: int,
        description: str | None = None,
    ) -> SpeakerProfile | None:
        """Convert a cluster to a SpeakerProfile and assign all members.

        Args:
            cluster_uuid: UUID of the cluster to promote.
            name: Name for the new profile.
            user_id: Owner user ID.
            description: Optional profile description.

        Returns:
            The created SpeakerProfile, or None on error.
        """
        try:
            cluster = (
                self.db.query(SpeakerCluster)
                .filter(SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id)
                .first()
            )
            if not cluster:
                logger.warning("Cluster %s not found", cluster_uuid)
                return None

            if cluster.promoted_to_profile_id:
                existing = (
                    self.db.query(SpeakerProfile)
                    .filter(SpeakerProfile.id == cluster.promoted_to_profile_id)
                    .first()
                )
                if existing:
                    logger.info(
                        "Cluster %s already promoted to profile %s", cluster_uuid, existing.uuid
                    )
                    return existing  # type: ignore[no-any-return]

            # Fetch members first so the new profile can inherit their tenant
            # scope (batch-fetch speakers to avoid per-member queries).
            members = (
                self.db.query(SpeakerClusterMember)
                .filter(SpeakerClusterMember.cluster_id == cluster.id)
                .all()
            )
            speaker_ids = [int(m.speaker_id) for m in members]
            speakers_by_id = {
                int(s.id): s
                for s in self.db.query(Speaker).filter(Speaker.id.in_(speaker_ids)).all()
            }

            # Tenant scope: a promoted profile inherits the members' file org
            # only when ALL members belong to the same org; any mix (or any
            # personal member) falls back to personal scope (None).
            member_file_ids = {int(s.media_file_id) for s in speakers_by_id.values()}
            member_orgs: set[int | None] = (
                {
                    int(r[0]) if r[0] is not None else None
                    for r in self.db.query(MediaFile.organization_id)
                    .filter(MediaFile.id.in_(member_file_ids))
                    .distinct()
                    .all()
                }
                if member_file_ids
                else set()
            )
            profile_org_id = member_orgs.pop() if len(member_orgs) == 1 else None

            # Create profile
            profile = SpeakerProfile(
                uuid=uuid4(),
                user_id=user_id,
                name=name,
                description=description,
                organization_id=profile_org_id,
            )
            self.db.add(profile)
            self.db.flush()

            # Link cluster and sync label
            cluster.promoted_to_profile_id = profile.id  # type: ignore[assignment]
            if not cluster.label:
                cluster.label = name  # type: ignore[assignment]

            for member in members:
                speaker = speakers_by_id.get(int(member.speaker_id))
                if speaker:
                    speaker.profile_id = profile.id  # type: ignore[assignment]
                    # Members span every file the cluster reaches into, each with
                    # its own indexed label — record before the overwrite (#432).
                    self._rename_tracker.record(
                        int(speaker.media_file_id),
                        canonical_speaker_label_for_row(speaker),
                        name,
                    )
                    speaker.display_name = name  # type: ignore[assignment]
                    speaker.verified = True  # type: ignore[assignment]

            # Update profile embedding
            from app.services.profile_embedding_service import ProfileEmbeddingService

            self.db.commit()
            self._rename_tracker.flush(self.db)
            ProfileEmbeddingService.update_profile_embedding(self.db, int(profile.id))

            logger.info(
                "Promoted cluster %s to profile '%s' (%d speakers)",
                cluster_uuid,
                name,
                len(members),
            )
            return profile

        except Exception as e:
            logger.error("Error promoting cluster to profile: %s", e)
            self.db.rollback()
            self._rename_tracker.discard()
            return None

    # ------------------------------------------------------------------
    # Outlier analysis and constraint-based unassignment
    # ------------------------------------------------------------------

    def analyze_gender_outliers(self, cluster_uuid: str, user_id: int) -> dict:
        """Analyze minority-gender speakers for potential outliers using embedding similarity.

        Uses batch OpenSearch mget + numpy matrix ops for speed.
        """
        from app.services.opensearch_service import get_speaker_embeddings_batch

        cluster = (
            self.db.query(SpeakerCluster)
            .filter(SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id)
            .first()
        )
        if not cluster:
            raise ValueError("Cluster not found")

        members = (
            self.db.query(SpeakerClusterMember)
            .filter(SpeakerClusterMember.cluster_id == cluster.id)
            .all()
        )

        # Load speakers with gender info
        speaker_ids = [m.speaker_id for m in members]
        speakers = self.db.query(Speaker).filter(Speaker.id.in_(speaker_ids)).all()
        speaker_map = {s.id: s for s in speakers}
        uuid_to_id = {str(s.uuid): s.id for s in speakers}

        # Determine majority/minority gender
        gender_counts: dict[str, int] = {}
        for s in speakers:
            if s.predicted_gender:
                gender_counts[s.predicted_gender] = gender_counts.get(s.predicted_gender, 0) + 1

        if len(gender_counts) < 2:
            return {
                "cluster_uuid": cluster_uuid,
                "majority_gender": list(gender_counts.keys())[0] if gender_counts else "unknown",
                "minority_gender": "unknown",
                "minority_analysis": [],
            }

        sorted_genders = sorted(gender_counts.items(), key=lambda x: x[1], reverse=True)
        majority_gender = sorted_genders[0][0]
        minority_gender = sorted_genders[1][0]

        # Batch-fetch all embeddings in one mget call
        speaker_uuids = [str(s.uuid) for s in speakers]
        raw_embeddings = get_speaker_embeddings_batch(speaker_uuids)

        embeddings: dict[int, np.ndarray] = {}
        for suuid, emb in raw_embeddings.items():
            sid = uuid_to_id.get(suuid)
            if sid is not None:
                embeddings[sid] = np.array(emb, dtype=np.float32)

        if not embeddings:
            return {
                "cluster_uuid": cluster_uuid,
                "majority_gender": majority_gender,
                "minority_gender": minority_gender,
                "minority_analysis": [],
            }

        # Vectorized centroid computation
        all_embs = np.array(list(embeddings.values()))
        centroid = all_embs.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-10

        majority_ids = [
            s.id for s in speakers if s.predicted_gender == majority_gender and s.id in embeddings
        ]
        minority_ids = [
            s.id for s in speakers if s.predicted_gender == minority_gender and s.id in embeddings
        ]

        # Build matrices for vectorized similarity
        minority_matrix = np.array([embeddings[sid] for sid in minority_ids])  # (M, D)
        majority_matrix = (
            np.array([embeddings[sid] for sid in majority_ids])
            if majority_ids
            else np.empty((0, 0))
        )

        # Vectorized: all minority sims to centroid at once
        sims_to_centroid = minority_matrix @ centroid  # (M,)

        # Vectorized: all minority avg sims to majority at once
        if majority_matrix.size > 0:
            sim_matrix = minority_matrix @ majority_matrix.T  # (M, N_majority)
            avg_sims_to_majority = sim_matrix.mean(axis=1)  # (M,)
        else:
            avg_sims_to_majority = np.zeros(len(minority_ids))

        # Vectorized: minority peer similarities
        if len(minority_ids) > 1:
            peer_matrix = minority_matrix @ minority_matrix.T  # (M, M)
            # Zero out diagonal (self-similarity) and average remaining
            np.fill_diagonal(peer_matrix, 0.0)
            avg_sims_to_peers = peer_matrix.sum(axis=1) / (len(minority_ids) - 1)
        else:
            avg_sims_to_peers = None

        analysis = []
        for i, sid in enumerate(minority_ids):
            s = speaker_map[sid]
            sim_c = float(sims_to_centroid[i])

            if sim_c < 0.70:
                recommendation = "likely_outlier"
            elif sim_c < 0.80:
                recommendation = "borderline"
            else:
                recommendation = "likely_valid"

            analysis.append(
                {
                    "speaker_uuid": str(s.uuid),
                    "speaker_name": s.display_name or s.name,
                    "predicted_gender": s.predicted_gender,
                    "sim_to_centroid": round(sim_c, 4),
                    "avg_sim_to_majority": round(float(avg_sims_to_majority[i]), 4),
                    "avg_sim_to_minority_peers": round(float(avg_sims_to_peers[i]), 4)
                    if avg_sims_to_peers is not None
                    else None,
                    "outlier_score": round(1.0 - sim_c, 4),
                    "recommendation": recommendation,
                }
            )

        return {
            "cluster_uuid": cluster_uuid,
            "majority_gender": majority_gender,
            "minority_gender": minority_gender,
            "minority_analysis": analysis,
        }

    def unassign_speakers(
        self,
        cluster_uuid: str,
        speaker_uuids: list[str],
        user_id: int,
        blacklist: bool = True,
    ) -> dict:
        """Unassign speakers from a cluster, optionally blacklisting them."""
        from app.models.media import SpeakerCannotLink
        from app.models.media import SpeakerProfileBlacklist

        cluster = (
            self.db.query(SpeakerCluster)
            .filter(SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id)
            .first()
        )
        if not cluster:
            raise ValueError("Cluster not found")

        # Find speaker IDs
        speakers_to_remove = (
            self.db.query(Speaker)
            .filter(Speaker.uuid.in_(speaker_uuids), Speaker.user_id == user_id)
            .all()
        )
        if not speakers_to_remove:
            raise ValueError("No valid speakers found")

        remove_ids = {s.id for s in speakers_to_remove}

        # Get remaining members
        remaining_member_ids = [
            m.speaker_id for m in cluster.members if m.speaker_id not in remove_ids
        ]

        # Remove cluster memberships
        self.db.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.cluster_id == cluster.id,
            SpeakerClusterMember.speaker_id.in_(remove_ids),
        ).delete(synchronize_session="fetch")

        # Set speaker.cluster_id = None
        for s in speakers_to_remove:
            s.cluster_id = None

        # Add blacklist constraints
        if blacklist:
            if cluster.promoted_to_profile_id:
                # Profile blacklist: one entry covers all current and future members
                for s in speakers_to_remove:
                    existing = (
                        self.db.query(SpeakerProfileBlacklist)
                        .filter(
                            SpeakerProfileBlacklist.speaker_id == s.id,
                            SpeakerProfileBlacklist.profile_id == cluster.promoted_to_profile_id,
                        )
                        .first()
                    )
                    if not existing:
                        self.db.add(
                            SpeakerProfileBlacklist(
                                speaker_id=s.id,
                                profile_id=cluster.promoted_to_profile_id,
                                reason="unassigned_by_user",
                            )
                        )
            else:
                # Cannot-link: pairwise between removed speakers and remaining members
                for s in speakers_to_remove:
                    for remaining_id in remaining_member_ids:
                        # Add both directions
                        for a, b in [(s.id, remaining_id), (remaining_id, s.id)]:
                            existing_link = (
                                self.db.query(SpeakerCannotLink)
                                .filter(
                                    SpeakerCannotLink.speaker_id == a,
                                    SpeakerCannotLink.cannot_link_speaker_id == b,
                                )
                                .first()
                            )
                            if not existing_link:
                                self.db.add(
                                    SpeakerCannotLink(
                                        speaker_id=a,
                                        cannot_link_speaker_id=b,
                                        reason="unassigned_by_user",
                                    )
                                )

        # Update cluster member_count
        cluster.member_count = len(remaining_member_ids)

        # Delete cluster if empty
        if cluster.member_count == 0:
            self.db.delete(cluster)
        else:
            # Recompute centroid
            self._update_cluster_centroid(cluster, user_id)

        self.db.commit()

        return {
            "unassigned_count": len(speakers_to_remove),
            "message": f"Unassigned {len(speakers_to_remove)} speaker(s) from cluster",
        }

    # ------------------------------------------------------------------
    # Inbox / unverified speakers
    # ------------------------------------------------------------------

    def get_unverified_speakers(
        self,
        user_id: int,
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Get paginated list of unverified speakers across all files.

        Prioritized by: cluster size (larger first), then suggestion confidence.

        Args:
            user_id: Owner user ID.
            page: Page number (1-based).
            per_page: Items per page.

        Returns:
            Dict with items, total, page, per_page, pages.
        """
        query = (
            self.db.query(Speaker)
            .options(
                joinedload(Speaker.media_file),
                joinedload(Speaker.cluster),
            )
            .filter(
                Speaker.user_id == user_id,
                Speaker.verified.is_(False),
                Speaker.profile_id.is_(None),
            )
            .outerjoin(SpeakerCluster, Speaker.cluster_id == SpeakerCluster.id)
            .order_by(
                SpeakerCluster.member_count.desc().nullslast(),
                Speaker.confidence.asc().nullslast(),
                Speaker.created_at.desc(),
            )
        )

        total = query.count()
        pages = max(1, math.ceil(total / per_page))
        offset = (page - 1) * per_page
        speakers = query.offset(offset).limit(per_page).all()

        items = []
        for speaker in speakers:
            media_file = speaker.media_file
            cluster = speaker.cluster

            items.append(
                {
                    "speaker_uuid": str(speaker.uuid),
                    "speaker_name": speaker.name,
                    "display_name": speaker.display_name,
                    "suggested_name": speaker.suggested_name,
                    "suggestion_source": speaker.suggestion_source,
                    "confidence": float(speaker.confidence) if speaker.confidence else None,
                    "media_file_uuid": str(media_file.uuid) if media_file else None,
                    "media_file_title": (
                        media_file.title or media_file.filename if media_file else None
                    ),
                    "media_file_duration": float(media_file.duration)
                    if media_file and media_file.duration
                    else None,
                    "cluster_uuid": str(cluster.uuid) if cluster else None,
                    "cluster_label": cluster.label if cluster else None,
                    "cluster_member_count": int(cluster.member_count or 0) if cluster else 0,
                    "verified": False,
                    "predicted_gender": speaker.predicted_gender,
                    "predicted_age_range": speaker.predicted_age_range,
                    "created_at": speaker.created_at.isoformat() if speaker.created_at else None,
                }
            )

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    def batch_verify_speakers(
        self,
        speaker_uuids: list[str],
        user_id: int,
        action: str = "accept",
        profile_uuid: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Batch verify/name multiple speakers.

        Args:
            speaker_uuids: List of speaker UUIDs to verify.
            user_id: Owner user ID.
            action: "accept" (apply suggestion), "assign" (assign to profile), "name" (set display_name), "skip" (mark as reviewed/skipped).
            profile_uuid: Profile UUID (for "assign" action).
            display_name: Display name (for "name" action).

        Returns:
            Summary dict with updated_count, failed_count, errors.
        """
        updated = 0
        failed = 0
        errors: list[str] = []

        profile = None
        if action == "assign" and profile_uuid:
            profile = (
                self.db.query(SpeakerProfile)
                .filter(SpeakerProfile.uuid == profile_uuid, SpeakerProfile.user_id == user_id)
                .first()
            )
            if not profile:
                return {
                    "updated_count": 0,
                    "failed_count": len(speaker_uuids),
                    "errors": ["Profile not found"],
                }

        # Batch-fetch all speakers to avoid per-UUID queries
        speakers_by_uuid = {
            str(s.uuid): s
            for s in self.db.query(Speaker)
            .filter(Speaker.uuid.in_([str(u) for u in speaker_uuids]))
            .all()
        }

        for suuid in speaker_uuids:
            try:
                speaker = speakers_by_uuid.get(str(suuid))
                if not speaker or int(speaker.user_id) != user_id:
                    errors.append(f"Speaker {suuid} not found")
                    failed += 1
                    continue

                # Every branch below overwrites the name the chunk plane was
                # indexed with, so each records the old one first (issue #432).
                # "accept" applies a DIFFERENT name per speaker, which is why the
                # tracker groups by new name rather than assuming one per batch.
                indexed_as = canonical_speaker_label_for_row(speaker)

                if action == "accept":
                    if speaker.suggested_name:
                        self._rename_tracker.record(
                            int(speaker.media_file_id), indexed_as, str(speaker.suggested_name)
                        )
                        speaker.display_name = speaker.suggested_name  # type: ignore[assignment]
                        speaker.verified = True  # type: ignore[assignment]
                        updated += 1
                    else:
                        errors.append(f"Speaker {suuid} has no suggestion to accept")
                        failed += 1

                elif action == "assign" and profile:
                    speaker.profile_id = profile.id  # type: ignore[assignment]
                    self._rename_tracker.record(
                        int(speaker.media_file_id), indexed_as, str(profile.name)
                    )
                    speaker.display_name = profile.name  # type: ignore[assignment]
                    speaker.verified = True  # type: ignore[assignment]
                    updated += 1

                elif action == "name" and display_name:
                    self._rename_tracker.record(
                        int(speaker.media_file_id), indexed_as, display_name
                    )
                    speaker.display_name = display_name  # type: ignore[assignment]
                    speaker.verified = True  # type: ignore[assignment]
                    updated += 1

                elif action == "skip":
                    # Mark as verified (reviewed) so it doesn't reappear in inbox
                    speaker.verified = True  # type: ignore[assignment]
                    # Only stamp provenance when there is none to destroy. "user_skipped"
                    # has no reader anywhere (backend or frontend), so overwriting a real
                    # "llm_analysis" here only ever erased the historical marker
                    # task_detection_service reads (issue #620 item 4).
                    if not speaker.suggestion_source:
                        speaker.suggestion_source = "user_skipped"  # type: ignore[assignment]
                    updated += 1

                else:
                    errors.append(f"Invalid action '{action}' or missing parameters")
                    failed += 1

            except Exception as e:
                errors.append(f"Error processing speaker {suuid}: {e}")
                failed += 1

        try:
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            # The renames never landed — dropping them keeps a later flush on the
            # same service instance from propagating a rolled-back name.
            self._rename_tracker.discard()
            return {"updated_count": 0, "failed_count": len(speaker_uuids), "errors": [str(e)]}

        self._rename_tracker.flush(self.db)
        return {"updated_count": updated, "failed_count": failed, "errors": errors}

    # ------------------------------------------------------------------
    # Cluster listing / detail
    # ------------------------------------------------------------------

    def list_clusters(
        self,
        user_id: int,
        page: int = 1,
        per_page: int = 20,
        has_label: bool | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """List clusters with pagination and filtering.

        Args:
            user_id: Owner user ID.
            page: Page number (1-based).
            per_page: Items per page.
            has_label: Filter by whether cluster has a label.
            search: Search in cluster labels.

        Returns:
            Dict with items, total, page, per_page, pages.
        """
        query = (
            self.db.query(SpeakerCluster)
            .options(
                subqueryload(SpeakerCluster.promoted_to_profile),
            )
            .filter(SpeakerCluster.user_id == user_id)
        )

        if has_label is True:
            query = query.filter(SpeakerCluster.label.isnot(None))
        elif has_label is False:
            query = query.filter(SpeakerCluster.label.is_(None))

        if search:
            safe_search = search.replace("%", r"\%").replace("_", r"\_")
            query = query.filter(SpeakerCluster.label.ilike(f"%{safe_search}%", escape="\\"))

        label_priority = case(
            (SpeakerCluster.label.isnot(None), 0),
            (SpeakerCluster.promoted_to_profile_id.isnot(None), 0),
            else_=1,
        )
        query = query.order_by(
            label_priority,
            SpeakerCluster.member_count.desc(),
            SpeakerCluster.quality_score.desc().nullslast(),
            SpeakerCluster.created_at.desc(),
        )

        total = query.count()

        # Counts for section headers (unfiltered by search/has_label)
        from sqlalchemy import func as sa_func

        base_count_query = self.db.query(SpeakerCluster).filter(SpeakerCluster.user_id == user_id)
        labeled_count = base_count_query.filter(
            (SpeakerCluster.label.isnot(None)) | (SpeakerCluster.promoted_to_profile_id.isnot(None))
        ).count()
        unlabeled_count = base_count_query.filter(
            SpeakerCluster.label.is_(None),
            SpeakerCluster.promoted_to_profile_id.is_(None),
        ).count()
        # Most recent cluster update = last time clustering ran
        last_clustered_at = (
            self.db.query(sa_func.max(SpeakerCluster.updated_at))
            .filter(SpeakerCluster.user_id == user_id)
            .scalar()
        )
        pages = max(1, math.ceil(total / per_page))
        offset = (page - 1) * per_page
        clusters = query.offset(offset).limit(per_page).all()

        # Batch-query gender composition for this page's clusters
        cluster_ids = [int(c.id) for c in clusters]
        gender_by_cluster: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        if cluster_ids:
            from sqlalchemy import func as sa_func

            gender_rows = (
                self.db.query(
                    SpeakerClusterMember.cluster_id,
                    Speaker.predicted_gender,
                    sa_func.count().label("cnt"),
                )
                .join(Speaker, SpeakerClusterMember.speaker_id == Speaker.id)
                .filter(SpeakerClusterMember.cluster_id.in_(cluster_ids))
                .group_by(SpeakerClusterMember.cluster_id, Speaker.predicted_gender)
                .all()
            )
            for row in gender_rows:
                gender_key = row[1] if row[1] else "__unknown__"
                gender_by_cluster[int(row[0])][gender_key] = int(row[2])

        items = []
        for cluster in clusters:
            promoted_profile = cluster.promoted_to_profile

            # Generate avatar URL for promoted profiles
            promoted_avatar_url = None
            if promoted_profile and promoted_profile.avatar_path:
                try:
                    from app.services.minio_service import get_file_url

                    promoted_avatar_url = get_file_url(promoted_profile.avatar_path, expires=3600)
                except Exception as e:
                    logger.debug("Failed to get avatar URL for profile: %s", e)

            # Build gender composition from batch query
            cid = int(cluster.id)
            gc = gender_by_cluster.get(cid, {})
            gc_members = []
            for g_key, cnt in gc.items():
                gender_val = None if g_key == "__unknown__" else g_key
                gc_members.extend([{"predicted_gender": gender_val}] * cnt)
            gender_composition = self._compute_gender_composition(gc_members)

            items.append(
                {
                    "uuid": str(cluster.uuid),
                    "label": cluster.label,
                    "description": cluster.description,
                    "user_id": int(cluster.user_id),
                    "member_count": int(cluster.member_count or 0),
                    "promoted_to_profile_id": cluster.promoted_to_profile_id,
                    "promoted_to_profile_uuid": str(promoted_profile.uuid)
                    if promoted_profile
                    else None,
                    "promoted_to_profile_name": promoted_profile.name if promoted_profile else None,
                    "promoted_to_profile_avatar_url": promoted_avatar_url,
                    "suggested_name": cluster.suggested_name,
                    "quality_score": float(cluster.quality_score)
                    if cluster.quality_score is not None
                    else None,
                    "min_similarity": float(cluster.min_similarity)
                    if cluster.min_similarity is not None
                    else None,
                    "separation_score": float(cluster.separation_score)
                    if cluster.separation_score is not None
                    else None,
                    "gender_composition": gender_composition,
                    "created_at": cluster.created_at,
                    "updated_at": cluster.updated_at,
                }
            )

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "labeled_count": labeled_count,
            "unlabeled_count": unlabeled_count,
            "last_clustered_at": last_clustered_at,
        }

    def get_cluster_detail(
        self,
        cluster_uuid: str,
        user_id: int,
    ) -> dict[str, Any] | None:
        """Get cluster detail with all members.

        Args:
            cluster_uuid: Cluster UUID.
            user_id: Owner user ID.

        Returns:
            Cluster detail dict, or None if not found.
        """
        cluster = (
            self.db.query(SpeakerCluster)
            .options(
                joinedload(SpeakerCluster.members)
                .joinedload(SpeakerClusterMember.speaker)
                .joinedload(Speaker.media_file),
                joinedload(SpeakerCluster.promoted_to_profile),
            )
            .filter(SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id)
            .first()
        )
        if not cluster:
            return None

        members = cluster.members

        # Check which members have transcript segments (= playable
        # via source media URL + seek to segment timestamps).
        member_speaker_ids = [int(m.speaker.id) for m in members if m.speaker]
        members_with_segments: set[int] = set()
        if member_speaker_ids:
            from sqlalchemy import func as sa_func

            seg_rows = (
                self.db.query(TranscriptSegment.speaker_id)
                .filter(TranscriptSegment.speaker_id.in_(member_speaker_ids))
                .group_by(TranscriptSegment.speaker_id)
                .having(sa_func.count() > 0)
                .all()
            )
            members_with_segments = {int(r[0]) for r in seg_rows}

        member_items = []
        for m in members:
            speaker = m.speaker
            if not speaker:
                continue

            media_file = speaker.media_file

            gender_conf = (
                speaker.attribute_confidence.get("gender") if speaker.attribute_confidence else None
            )

            member_items.append(
                {
                    "uuid": str(m.uuid),
                    "speaker_uuid": str(speaker.uuid),
                    "speaker_name": speaker.name,
                    "display_name": speaker.display_name,
                    "suggested_name": speaker.suggested_name,
                    "media_file_uuid": str(media_file.uuid) if media_file else None,
                    "media_file_title": (
                        media_file.title or media_file.filename if media_file else None
                    ),
                    "confidence": float(m.confidence or 0.0),
                    "margin": float(m.margin) if m.margin is not None else None,
                    "verified": bool(speaker.verified),
                    "predicted_gender": speaker.predicted_gender,
                    "predicted_age_range": speaker.predicted_age_range,
                    "gender_confidence": float(gender_conf) if gender_conf is not None else None,
                    "gender_confirmed_by_user": bool(speaker.gender_confirmed_by_user),
                    "has_audio_clip": int(speaker.id) in members_with_segments,
                    "created_at": m.created_at,
                }
            )

        gender_composition = self._compute_gender_composition(member_items)
        promoted_profile = cluster.promoted_to_profile

        return {
            "uuid": str(cluster.uuid),
            "label": cluster.label,
            "description": cluster.description,
            "user_id": int(cluster.user_id),
            "member_count": int(cluster.member_count or 0),
            "promoted_to_profile_id": cluster.promoted_to_profile_id,
            "promoted_to_profile_uuid": str(promoted_profile.uuid) if promoted_profile else None,
            "promoted_to_profile_name": promoted_profile.name if promoted_profile else None,
            "suggested_name": cluster.suggested_name,
            "quality_score": float(cluster.quality_score)
            if cluster.quality_score is not None
            else None,
            "min_similarity": float(cluster.min_similarity)
            if cluster.min_similarity is not None
            else None,
            "separation_score": float(cluster.separation_score)
            if cluster.separation_score is not None
            else None,
            "gender_composition": gender_composition,
            "created_at": cluster.created_at,
            "updated_at": cluster.updated_at,
            "members": member_items,
        }

    @staticmethod
    def _compute_gender_composition(member_items: list[dict]) -> dict:
        """Compute gender composition summary for a list of cluster members."""
        male = sum(1 for m in member_items if m.get("predicted_gender") == "male")
        female = sum(1 for m in member_items if m.get("predicted_gender") == "female")
        total_g = male + female
        unknown = len(member_items) - total_g
        if total_g == 0:
            return {
                "male_count": 0,
                "female_count": 0,
                "unknown_count": unknown,
                "total_with_gender": 0,
                "dominant_gender": None,
                "gender_coherence": None,
                "gender_label": None,
                "has_gender_conflict": False,
            }
        dominant = "male" if male >= female else "female"
        dominant_count = max(male, female)
        coherence = dominant_count / total_g
        conflict = min(male, female) > 0
        if coherence == 1.0:
            label = f"100% {dominant.title()}"
        else:
            label = f"{dominant_count}/{total_g} {dominant.title()}"
        return {
            "male_count": male,
            "female_count": female,
            "unknown_count": unknown,
            "total_with_gender": total_g,
            "dominant_gender": dominant,
            "gender_coherence": round(coherence, 3),
            "gender_label": label,
            "has_gender_conflict": conflict,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_cluster_dominant_gender(self, cluster: SpeakerCluster) -> str | None:
        """Get the dominant predicted gender of a cluster's members."""
        from sqlalchemy import func as sa_func

        rows = (
            self.db.query(Speaker.predicted_gender, sa_func.count())
            .join(SpeakerClusterMember, SpeakerClusterMember.speaker_id == Speaker.id)
            .filter(
                SpeakerClusterMember.cluster_id == cluster.id,
                Speaker.predicted_gender.isnot(None),
            )
            .group_by(Speaker.predicted_gender)
            .all()
        )
        if not rows:
            return None
        dominant: str | None = max(rows, key=lambda r: r[1])[0]
        return dominant

    def _is_speaker_blocked_from_cluster(self, speaker_id: int, cluster: SpeakerCluster) -> bool:
        """Check if a speaker is blocked from joining a cluster by constraints."""
        from app.models.media import SpeakerCannotLink
        from app.models.media import SpeakerProfileBlacklist

        # Check 1: Profile blacklist
        if cluster.promoted_to_profile_id:
            blocked = (
                self.db.query(SpeakerProfileBlacklist)
                .filter(
                    SpeakerProfileBlacklist.speaker_id == speaker_id,
                    SpeakerProfileBlacklist.profile_id == cluster.promoted_to_profile_id,
                )
                .first()
            )
            if blocked:
                return True

        # Check 2: Cannot-link with any cluster member
        member_ids = [m.speaker_id for m in cluster.members]
        if member_ids:
            conflicts = (
                self.db.query(SpeakerCannotLink)
                .filter(
                    SpeakerCannotLink.speaker_id == speaker_id,
                    SpeakerCannotLink.cannot_link_speaker_id.in_(member_ids),
                )
                .count()
            )
            if conflicts > 0:
                return True

        return False

    def _add_speaker_to_cluster(
        self,
        speaker: Speaker,
        cluster: SpeakerCluster,
        confidence: float,
        margin: float | None = None,
    ) -> None:
        """Add a speaker to a cluster (creates membership record).

        Args:
            speaker: Speaker to add.
            cluster: Target cluster.
            confidence: Cosine similarity to cluster centroid.
            margin: Difference between best and second-best match similarity.
        """
        existing = (
            self.db.query(SpeakerClusterMember)
            .filter(
                SpeakerClusterMember.cluster_id == cluster.id,
                SpeakerClusterMember.speaker_id == speaker.id,
            )
            .first()
        )
        if existing:
            return

        # Remove memberships in other clusters (speaker can only be in one)
        self.db.query(SpeakerClusterMember).filter(
            SpeakerClusterMember.speaker_id == speaker.id,
            SpeakerClusterMember.cluster_id != cluster.id,
        ).delete()

        member = SpeakerClusterMember(
            uuid=uuid4(),
            cluster_id=cluster.id,
            speaker_id=speaker.id,
            confidence=confidence,
            margin=margin,
        )
        self.db.add(member)
        speaker.cluster_id = cluster.id  # type: ignore[assignment]

        self.db.flush()
        cluster.member_count = (  # type: ignore[assignment]
            self.db.query(SpeakerClusterMember)
            .filter(SpeakerClusterMember.cluster_id == cluster.id)
            .count()
        )

    def _bulk_add_speakers_to_cluster(
        self,
        speakers: list[Speaker],
        cluster: SpeakerCluster,
        confidence: float | dict[int, float] = 0.0,
    ) -> int:
        """Add multiple speakers to a cluster in a single flush.

        Filters out speakers already in the cluster before adding.

        Args:
            confidence: Either a single float applied to all members,
                or a dict mapping speaker.id -> confidence for
                per-member values.

        Returns:
            Number of speakers added.
        """
        per_member = isinstance(confidence, dict)

        # Filter out speakers already in this cluster
        existing_speaker_ids = {
            int(r.speaker_id)
            for r in self.db.query(SpeakerClusterMember.speaker_id)
            .filter(SpeakerClusterMember.cluster_id == cluster.id)
            .all()
        }
        speakers = [s for s in speakers if int(s.id) not in existing_speaker_ids]

        count = 0
        for speaker in speakers:
            conf = confidence.get(int(speaker.id), 0.0) if per_member else confidence  # type: ignore[union-attr]
            member = SpeakerClusterMember(
                uuid=uuid4(),
                cluster_id=cluster.id,
                speaker_id=speaker.id,
                confidence=conf,
            )
            self.db.add(member)
            speaker.cluster_id = cluster.id  # type: ignore[assignment]
            count += 1

        self.db.flush()
        cluster.member_count = (  # type: ignore[assignment]
            self.db.query(SpeakerClusterMember)
            .filter(SpeakerClusterMember.cluster_id == cluster.id)
            .count()
        )
        return count

    def _create_singleton_cluster(
        self,
        speaker: Speaker,
        user_id: int,
        embedding: list[float],
        organization_id: int | None = None,
    ) -> SpeakerCluster:
        """Create a new cluster with one speaker, in the speaker's tenant scope."""
        cluster = SpeakerCluster(
            uuid=uuid4(),
            user_id=user_id,
            organization_id=organization_id,
            member_count=1,
            quality_score=1.0,
            min_similarity=1.0,
        )
        self.db.add(cluster)
        self.db.flush()

        member = SpeakerClusterMember(
            uuid=uuid4(),
            cluster_id=cluster.id,
            speaker_id=speaker.id,
            confidence=1.0,
        )
        self.db.add(member)
        speaker.cluster_id = cluster.id  # type: ignore[assignment]
        self.db.flush()

        # Validate embedding is not a zero or NaN vector
        emb_norm = sum(x * x for x in embedding) ** 0.5
        if emb_norm < 0.01 or math.isnan(emb_norm):
            logger.warning(
                "Zero-vector embedding for speaker %s — skipping centroid storage",
                speaker.uuid,
            )
            return cluster

        # Store centroid in OpenSearch
        try:
            from app.services.opensearch_service import store_cluster_embedding

            store_cluster_embedding(
                cluster_uuid=str(cluster.uuid),
                user_id=user_id,
                embedding=embedding,
                organization_id=organization_id,
            )
        except Exception as e:
            logger.warning("Could not store cluster centroid in OpenSearch: %s", e)

        return cluster

    def _update_cluster_centroid(
        self,
        cluster: SpeakerCluster,
        user_id: int,
        refresh: str | bool = "wait_for",
    ) -> None:
        """Recalculate and store the cluster centroid embedding."""
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
            return

        # Quality score: average centroid similarity (consistent with
        # per-member confidence shown in the UI).
        # min_similarity: tightest pairwise cosine similarity.
        if len(embeddings) >= 2:
            normed = arr / np.linalg.norm(arr, axis=1, keepdims=True).clip(1e-8)
            cluster.quality_score = float(np.mean(normed @ centroid))  # type: ignore[assignment]
            sim_matrix = normed @ normed.T
            iu = np.triu_indices(len(embeddings), k=1)
            cluster.min_similarity = float(np.min(sim_matrix[iu]))  # type: ignore[assignment]
        else:
            cluster.quality_score = 1.0  # type: ignore[assignment]
            cluster.min_similarity = 1.0  # type: ignore[assignment]

        if best_speaker_id:
            cluster.representative_speaker_id = best_speaker_id  # type: ignore[assignment]

        # Store in OpenSearch
        try:
            from app.services.opensearch_service import store_cluster_embedding

            store_cluster_embedding(
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

    def _get_speaker_embedding(self, speaker: Speaker) -> list[float] | None:
        """Get embedding for a speaker from the active index."""
        try:
            from app.services.opensearch_service import get_speaker_embedding

            return get_speaker_embedding(str(speaker.uuid))
        except Exception as e:
            logger.warning("Could not get embedding for speaker %s: %s", speaker.uuid, e)
            return None
