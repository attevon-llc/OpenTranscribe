"""Re-cluster phases that depend on profiles: Phase 1 grouping and Phase 2.5 singleton matching."""

import logging
from collections import defaultdict
from typing import TYPE_CHECKING
from typing import NamedTuple
from uuid import uuid4

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerProfile
from app.services.clustering.thresholds import CLUSTER_ASSIGNMENT_THRESHOLD
from app.services.speaker_cleanup import blacklisted_profile_ids
from app.services.speaker_cleanup import identity_conflicts_with_cluster
from app.services.speaker_cleanup import speaker_blocked_from_cluster
from app.services.speaker_cleanup import unlink_from_profile

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Step 2 (Phase 1): profile-based clustering. Pure DB query, no embeddings loaded.
# ----------------------------------------------------------------------


class ProfilePhase(NamedTuple):
    """What Phase 1 built: its counts, its clusters and the speakers it grouped."""

    clusters_created: int
    speakers_assigned: int
    clusters: dict[str, SpeakerCluster]
    profiled_speaker_ids: set[int]
    profile_count: int


def _unlink_stale_profile_links(
    self: "SpeakerClusteringService", profile_speakers: list[Speaker]
) -> None:
    """Take back the link of a speaker that is blacklisted from the profile it is linked to.

    A speaker still linked to a profile it was unassigned from is data written
    before #1233 made the unassign unlink: the blacklist is the later decision,
    so the link is taken back here instead of the profile cluster re-adopting it.
    """
    blacklisted = blacklisted_profile_ids(self.db, [int(s.id) for s in profile_speakers])
    stale_links = [s for s in profile_speakers if int(s.profile_id or 0) in blacklisted[int(s.id)]]
    for stale in stale_links:
        profile_id_left = int(stale.profile_id)  # type: ignore[arg-type]
        if unlink_from_profile(self.db, [stale], profile_id_left, self._rename_tracker):
            self._assigned.profile_ids.add(profile_id_left)
            self._assigned.relabel_speaker_ids.add(int(stale.id))
    if stale_links:
        # Autoflush is off: Phase 2 must see these as the unlabeled speakers they are.
        self.db.flush()
        logger.info(
            "Re-cluster: unlinked %d speaker(s) from profiles they were unassigned from",
            len(stale_links),
        )


def _create_profile_cluster(
    self: "SpeakerClusteringService", user_id: int, profile_id: int, members: list[Speaker]
) -> SpeakerCluster:
    """The cluster of one profile's linked speakers, in the profile's tenant scope."""
    profile = self.db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
    # Tenant scope: a profile-backed cluster lives in its profile's
    # scope (profiles are already org-stamped at creation).
    profile_org = (
        int(profile.organization_id) if profile and profile.organization_id is not None else None
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
    self._update_cluster_centroid(cluster, user_id, refresh=False)
    return cluster


def group_by_profile(
    self: "SpeakerClusteringService", user_id: int, off_scopes: set[int | None]
) -> ProfilePhase:
    """Group speakers that already share a verified profile into clusters (Phase 1)."""
    profile_speakers_query = self.db.query(Speaker).filter(
        Speaker.user_id == user_id,
        Speaker.profile_id.isnot(None),
    )
    if off_scopes:
        profile_speakers_query = profile_speakers_query.join(
            MediaFile, MediaFile.id == Speaker.media_file_id
        ).filter(self._scope_not_in(MediaFile.organization_id, off_scopes))
    profile_speakers = profile_speakers_query.all()

    _unlink_stale_profile_links(self, profile_speakers)
    self._assigned.owner_ids.add(user_id)

    profile_groups: dict[int, list[Speaker]] = defaultdict(list)
    profiled_speaker_ids: set[int] = set()
    for spk in profile_speakers:
        if spk.profile_id is None:  # unlinked just above
            continue
        profile_groups[int(spk.profile_id)].append(spk)
        profiled_speaker_ids.add(int(spk.id))

    clusters_created = 0
    speakers_assigned = 0
    phase1_profile_clusters: dict[str, SpeakerCluster] = {}
    for profile_id, members in profile_groups.items():
        if len(members) < 2:
            continue

        cluster = _create_profile_cluster(self, user_id, profile_id, members)
        speakers_assigned += len(members)
        clusters_created += 1
        phase1_profile_clusters[str(cluster.uuid)] = cluster

    logger.info(
        "Phase 1 (profile grouping): %d clusters, %d speakers (%d profiles) for user %d",
        clusters_created,
        speakers_assigned,
        len(profile_groups),
        user_id,
    )
    return ProfilePhase(
        clusters_created,
        speakers_assigned,
        phase1_profile_clusters,
        profiled_speaker_ids,
        len(profile_groups),
    )


def _profile_cluster_map(
    self: "SpeakerClusteringService",
    user_id: int,
    off_scopes: set[int | None],
    phase1_profile_clusters: dict[str, SpeakerCluster],
) -> dict[str, SpeakerCluster]:
    """Phase 1 clusters plus any pre-existing promoted clusters, by uuid."""
    profile_cluster_map: dict[str, SpeakerCluster] = dict(phase1_profile_clusters)
    promoted_query = self.db.query(SpeakerCluster).filter(
        SpeakerCluster.user_id == user_id,
        SpeakerCluster.promoted_to_profile_id.isnot(None),
    )
    if off_scopes:
        promoted_query = promoted_query.filter(
            self._scope_not_in(SpeakerCluster.organization_id, off_scopes)
        )
    for pc in promoted_query.all():
        profile_cluster_map[str(pc.uuid)] = pc
    return profile_cluster_map


def _join_first_matching_profile_cluster(
    self: "SpeakerClusteringService",
    spk: Speaker,
    emb: list[float],
    spk_org: int | None,
    user_id: int,
    threshold: float,
    profile_cluster_map: dict[str, SpeakerCluster],
) -> bool:
    """Put a singleton in the first profile cluster that matches it and allows it."""
    from app.services.opensearch_service import find_matching_clusters

    # Joining a profile cluster links the profile (as upload-time
    # matching does), so it takes the auto-link bar, never less.
    matches = find_matching_clusters(
        emb,
        user_id,
        k=5,
        threshold=max(threshold, CLUSTER_ASSIGNMENT_THRESHOLD),
        organization_id=int(spk_org) if spk_org is not None else None,
    )
    for match in matches:
        matched_cluster = profile_cluster_map.get(match["cluster_uuid"])
        if not matched_cluster:
            continue
        # A correction forbids it, or the speaker is someone else.
        if speaker_blocked_from_cluster(
            self.db, int(spk.id), matched_cluster
        ) or identity_conflicts_with_cluster(self.db, spk, matched_cluster):
            logger.debug(
                "Singleton %s blocked from cluster %s by constraint",
                spk.id,
                matched_cluster.uuid,
            )
            continue
        self._join(spk, matched_cluster, match["similarity"], None)
        return True  # Assigned to first valid match
    return False


def match_singletons_to_profile_clusters(
    self: "SpeakerClusteringService",
    user_id: int,
    off_scopes: set[int | None],
    threshold: float,
    singleton_ids: list[int],
    phase1_profile_clusters: dict[str, SpeakerCluster],
    emb_cache: dict[int, list[float]],
) -> int:
    """Phase 2.5: match singletons against profile centroids. Returns how many joined."""
    profile_cluster_map = _profile_cluster_map(self, user_id, off_scopes, phase1_profile_clusters)
    if not profile_cluster_map:
        return 0
    singleton_speakers = self.db.query(Speaker).filter(Speaker.id.in_(singleton_ids)).all()
    # Tenant gate: each singleton's cluster search is scoped by
    # ITS file's org (batch-resolved to avoid N queries).
    org_by_file: dict[int, int | None] = {
        int(fid): (int(org) if org is not None else None)
        for fid, org in self.db.query(MediaFile.id, MediaFile.organization_id)
        .filter(MediaFile.id.in_({int(s.media_file_id) for s in singleton_speakers}))
        .all()
    }
    matched_singletons = 0
    for spk in singleton_speakers:
        emb = emb_cache.get(int(spk.id))
        if emb is None:
            continue
        spk_org = org_by_file.get(int(spk.media_file_id))
        if _join_first_matching_profile_cluster(
            self, spk, emb, spk_org, user_id, threshold, profile_cluster_map
        ):
            matched_singletons += 1
    if matched_singletons:
        logger.info(
            "Phase 2.5: matched %d singletons to profile clusters",
            matched_singletons,
        )
    return matched_singletons
