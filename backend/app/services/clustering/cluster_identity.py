"""User-driven cluster identity changes: promote a cluster to a profile, unassign speakers."""

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfile
from app.services.permission_service import file_ids_in_scope
from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_cleanup import detach_from_cluster
from app.services.speaker_cleanup import raise_if_incomplete
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.utils.speaker_identity_detail import ORIGIN_CLUSTER_PROMOTED
from app.utils.speaker_identity_source import record_human_identity
from app.utils.speaker_labels import canonical_speaker_label_for_row

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def promote_cluster_to_profile(
    self: "SpeakerClusteringService",
    cluster_uuid: str,
    name: str,
    user_id: int,
    description: str | None = None,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> SpeakerProfile | None:
    """Convert a cluster to a SpeakerProfile and assign all members.

    Refused (``None``) when the cluster's members span tenants or belong to a
    tenant other than the cluster's: a profile never pools voiceprints across
    tenants.

    Args:
        cluster_uuid: UUID of the cluster to promote.
        name: Name for the new profile.
        user_id: Owner user ID.
        description: Optional profile description.
        organization_id: Tenant the cluster must belong to.

    Returns:
        The created SpeakerProfile, or None on error.

    Raises:
        SpeakerCleanupIncompleteError: The promotion committed, but a profile embedding or
            the cluster centroid could be neither rebuilt nor erased.
    """
    try:
        cluster = self._scoped_cluster_query(cluster_uuid, user_id, organization_id).first()
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
            int(s.id): s for s in self.db.query(Speaker).filter(Speaker.id.in_(speaker_ids)).all()
        }

        # Tenant scope: the promoted profile is of the cluster's tenant, and
        # every member's file must be of that same tenant.
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
        if member_orgs - {cluster.organization_id}:
            logger.warning(
                "Refusing to promote cluster %s: members span tenants %s",
                cluster_uuid,
                sorted(str(o) for o in member_orgs),
            )
            return None
        profile_org_id = cluster.organization_id

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

        # A member already linked elsewhere leaves that profile, whose embedding
        # still averages its voice until it is rebuilt (issue #1214).
        touched = SpeakerImpact(
            owner_ids={user_id},
            cluster_uuids={str(cluster.uuid)},
            profile_ids={int(profile.id)}
            | {int(s.profile_id) for s in speakers_by_id.values() if s.profile_id},
        )

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
                # A person named the cluster: its members are theirs (issue #1280, Q2).
                record_human_identity(speaker, user_id=user_id, origin=ORIGIN_CLUSTER_PROMOTED)

        self.db.commit()
        self._rename_tracker.flush(self.db)

    except Exception as e:
        logger.error("Error promoting cluster to profile: %s", e)
        self.db.rollback()
        self._rename_tracker.discard()
        return None

    # The new profile's embedding, every former profile's, and the cluster centroid
    # (profile-linked members weigh double in it) — one shared cleanup.
    raise_if_incomplete(reconcile_speaker_impact(self.db, touched))
    logger.info(
        "Promoted cluster %s to profile '%s' (%d speakers)",
        cluster_uuid,
        name,
        len(members),
    )
    return profile


def unassign_speakers(
    self: "SpeakerClusteringService",
    cluster_uuid: str,
    speaker_uuids: list[str],
    user_id: int,
    blacklist: bool = True,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> dict:
    """Unassign speakers from a cluster: that section is no longer that person.

    ``speaker_cleanup.detach_from_cluster`` applies the rule (issue #1233): the
    speakers leave the cluster and the cluster's promoted profile (a name that came
    from the profile is cleared, a name the user typed stays) and, with
    ``blacklist``, are recorded as cannot-link against every remaining member and as
    blacklisted from that profile, so no assignment path re-links them silently. The
    cluster and its profile remain for the other members; the centroid and profile
    embedding are rebuilt from them, and a cluster left with none is deleted with
    its centroid.

    Raises:
        ValueError: The cluster or the speakers were not found.
        SpeakerCleanupIncompleteError: The unassignment committed, but the centroid or
            profile embedding could be neither rebuilt nor erased.
    """
    cluster = self._scoped_cluster_query(cluster_uuid, user_id, organization_id).first()
    if not cluster:
        raise ValueError("Cluster not found")

    # Find speaker IDs (of the cluster's tenant only)
    speakers_to_remove = (
        self.db.query(Speaker)
        .filter(
            Speaker.uuid.in_(speaker_uuids),
            Speaker.user_id == user_id,
            Speaker.media_file_id.in_(file_ids_in_scope(cluster.organization_id)),
        )
        .all()
    )
    if not speakers_to_remove:
        raise ValueError("No valid speakers found")

    impact = detach_from_cluster(
        self.db,
        cluster,
        speakers_to_remove,
        blacklist=blacklist,
        tracker=self._rename_tracker,
    )
    impact.owner_ids.add(user_id)
    try:
        self.db.commit()
    except Exception:
        self.db.rollback()
        self._rename_tracker.discard()
        raise
    self._rename_tracker.flush(self.db)

    # The removed voices leave the centroid and the profile; an emptied cluster goes
    # with its centroid. The cluster and profile stay for everyone else.
    raise_if_incomplete(reconcile_speaker_impact(self.db, impact))

    return {
        "unassigned_count": len(speakers_to_remove),
        "message": f"Unassigned {len(speakers_to_remove)} speaker(s) from cluster",
    }
