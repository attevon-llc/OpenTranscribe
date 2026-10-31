"""User-driven cluster restructuring: merge two clusters, split speakers out of one."""

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.services.permission_service import file_ids_in_scope
from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_cleanup import raise_if_incomplete
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.services.speaker_identity_moves import detach_from_cluster
from app.services.speaker_identity_moves import join_cluster_identity
from app.services.speaker_identity_rules import identity_conflicts_with_cluster
from app.services.speaker_identity_rules import speaker_blocked_from_cluster

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def _move_members_to_target(
    self: "SpeakerClusteringService", source: SpeakerCluster, target: SpeakerCluster
) -> tuple[set[str], list[Speaker]]:
    """Move the source's members into the target. Returns (blocked speaker uuids, moved)."""
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

    # A member the user unassigned from the target (or from its profile, or from
    # one of its members) is not moved: it stays in the source, which then
    # survives with it (issue #1233). So is a member that is already somebody
    # other than the target's person, unless that somebody is the source's own
    # profile — merging two named clusters says the two names are one person.
    # Decided before anything moves, so the target's membership is the one the
    # user looked at.
    source_profile_id = source.promoted_to_profile_id

    def _stays_behind(speaker: Speaker) -> bool:
        if speaker_blocked_from_cluster(self.db, int(speaker.id), target):
            return True
        if source_profile_id is not None and speaker.profile_id == source_profile_id:
            return False
        return identity_conflicts_with_cluster(self.db, speaker, target)

    self.blocked_speaker_uuids = [
        str(speakers_by_id[int(m.speaker_id)].uuid)
        for m in source_members
        if int(m.speaker_id) in speakers_by_id and _stays_behind(speakers_by_id[int(m.speaker_id)])
    ]
    blocked = set(self.blocked_speaker_uuids)
    moved: list[Speaker] = []
    for member in source_members:
        speaker = speakers_by_id.get(int(member.speaker_id))
        if speaker is not None and str(speaker.uuid) in blocked:
            continue
        if int(member.speaker_id) in existing_speaker_ids:
            self.db.delete(member)
        else:
            member.cluster_id = target.id  # type: ignore[assignment]
        if speaker:
            speaker.cluster_id = target.id  # type: ignore[assignment]
            moved.append(speaker)
    return blocked, moved


def _adopt_target_profile(
    self: "SpeakerClusteringService",
    target: SpeakerCluster,
    moved: list[Speaker],
    source_profile_id: int | None,
    user_id: int,
) -> None:
    """Merging into a promoted cluster says the moved speakers are that person:
    they take its profile, unless already linked to some third profile."""
    for speaker in moved:
        if speaker.profile_id is None or speaker.profile_id == source_profile_id:
            touched = join_cluster_identity(
                self.db,
                speaker,
                target,
                self._rename_tracker,
                source="human",
                user_id=user_id,
            )
            if touched:
                self._assigned.profile_ids |= touched
                self._assigned.relabel_speaker_ids.add(int(speaker.id))


def merge_clusters(
    self: "SpeakerClusteringService",
    source_uuid: str,
    target_uuid: str,
    user_id: int,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> SpeakerCluster | None:
    """Merge source cluster into target. All members move to target.

    The emptied source is deleted with its centroid and the target's centroid is
    rebuilt from its new member set by ``speaker_cleanup`` after the move commits.

    Args:
        source_uuid: UUID of the cluster to dissolve.
        target_uuid: UUID of the cluster to absorb members.
        user_id: Owner user ID.
        organization_id: Tenant both clusters must belong to.

    Returns:
        The target cluster after merge, or None on error.

    Raises:
        SpeakerCleanupIncompleteError: The merge committed, but a centroid could be
            neither rebuilt nor erased.
    """
    try:
        # Lock both clusters in consistent UUID order to prevent deadlocks
        first_uuid, second_uuid = sorted([source_uuid, target_uuid])
        self._scoped_cluster_query(first_uuid, user_id, organization_id).with_for_update().first()
        if first_uuid != second_uuid:
            self._scoped_cluster_query(
                second_uuid, user_id, organization_id
            ).with_for_update().first()

        source = self._scoped_cluster_query(source_uuid, user_id, organization_id).first()
        target = self._scoped_cluster_query(target_uuid, user_id, organization_id).first()

        if not source or not target:
            logger.warning(
                "Cluster not found for merge: source=%s, target=%s", source_uuid, target_uuid
            )
            return None
        if source.organization_id != target.organization_id:
            logger.warning(
                "Refusing to merge clusters of different tenants: %s -> %s",
                source_uuid,
                target_uuid,
            )
            return None

        blocked, moved = _move_members_to_target(self, source, target)
        source_profile_id = source.promoted_to_profile_id

        self._assigned.owner_ids.add(user_id)
        self._assigned.cluster_uuids |= {source_uuid, target_uuid}
        if target.promoted_to_profile_id is not None:
            _adopt_target_profile(self, target, moved, source_profile_id, user_id)

        self.db.commit()
        self._rename_tracker.flush(self.db)

    except Exception as e:
        logger.error("Error merging clusters: %s", e)
        self.db.rollback()
        self._rename_tracker.discard()
        self._assigned = SpeakerImpact()
        return None

    # The source is empty now (unless it kept blocked members): the shared cleanup
    # deletes it with its centroid and rebuilds the target's from the merged membership.
    raise_if_incomplete(self._settle_assignments())
    logger.info(
        "Merged cluster %s into %s (%d member(s) blocked by an unassignment)",
        source_uuid,
        target_uuid,
        len(blocked),
    )
    return target  # type: ignore[no-any-return]


def split_cluster(
    self: "SpeakerClusteringService",
    cluster_uuid: str,
    speaker_uuids: list[str],
    user_id: int,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> SpeakerCluster | None:
    """Split specified speakers into a new cluster.

    A split says the moved speakers are someone other than the members that stay, so
    it is an unassignment (issue #1233): they leave the source through
    ``speaker_identity_moves.detach_from_cluster`` — unlinked from a promoted source's
    profile, and recorded as cannot-link against the members that stay and as
    blacklisted from that profile — then form the new, unpromoted cluster together.

    Args:
        cluster_uuid: UUID of the source cluster.
        speaker_uuids: UUIDs of speakers to move to the new cluster.
        user_id: Owner user ID.
        organization_id: Tenant the source cluster must belong to.

    Returns:
        The newly created cluster, or None on error.

    Raises:
        SpeakerCleanupIncompleteError: The split committed, but a centroid could be
            neither rebuilt nor erased.
    """
    try:
        source = (
            self._scoped_cluster_query(cluster_uuid, user_id, organization_id)
            .with_for_update()
            .first()
        )
        if not source:
            logger.warning("Cluster %s not found", cluster_uuid)
            return None

        # Resolve speaker IDs — only speakers of the source cluster's tenant
        speakers_to_move = (
            self.db.query(Speaker)
            .filter(
                Speaker.uuid.in_(speaker_uuids),
                Speaker.user_id == user_id,
                Speaker.media_file_id.in_(file_ids_in_scope(source.organization_id)),
            )
            .all()
        )
        if not speakers_to_move:
            logger.warning("No valid speakers to split")
            return None

        speakers_to_move = [s for s in speakers_to_move if s.cluster_id == source.id]
        if not speakers_to_move:
            logger.warning("No speakers from source cluster to split")
            return None

        # Leave the source as an unassignment: membership, promoted profile, blacklist.
        impact = detach_from_cluster(
            self.db, source, speakers_to_move, blacklist=True, tracker=self._rename_tracker
        )

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

        for speaker in speakers_to_move:
            member = SpeakerClusterMember(
                uuid=uuid4(),
                cluster_id=new_cluster.id,
                speaker_id=speaker.id,
                confidence=0.0,
            )
            self.db.add(member)
            speaker.cluster_id = new_cluster.id  # type: ignore[assignment]

        new_cluster.member_count = len(speakers_to_move)  # type: ignore[assignment]
        impact.cluster_uuids.add(str(new_cluster.uuid))
        impact.owner_ids.add(user_id)
        self.db.commit()
        self._rename_tracker.flush(self.db)

    except Exception as e:
        logger.error("Error splitting cluster: %s", e)
        self.db.rollback()
        self._rename_tracker.discard()
        return None

    # Both centroids change: the source loses the moved voices (and is deleted with
    # its centroid if nobody is left), the new cluster is built from them; a promoted
    # source's profile loses them too.
    raise_if_incomplete(reconcile_speaker_impact(self.db, impact))
    logger.info("Split %d speakers from cluster %s", len(speakers_to_move), cluster_uuid)
    return new_cluster
