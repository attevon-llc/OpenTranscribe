"""Real-time speaker-to-cluster assignment and cluster membership writes."""

import logging
import math
from typing import TYPE_CHECKING
from uuid import uuid4

from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.services.clustering.thresholds import CLUSTER_ASSIGNMENT_THRESHOLD
from app.services.clustering.thresholds import GENDER_CROSS_THRESHOLD
from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_cleanup import clusters_holding_speakers
from app.services.speaker_cleanup import identity_conflicts_with_cluster
from app.services.speaker_cleanup import speaker_blocked_from_cluster

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def find_or_create_cluster(
    self: "SpeakerClusteringService",
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
        speaker = self.db.query(Speaker).filter(Speaker.id == speaker_id).with_for_update().first()
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

        # Privacy policy (#1280): clustering stores a centroid per speaker, so a file
        # whose owner or organization has voiceprints off gets none (asked fresh).
        if not self._privacy.voiceprints(int(speaker.media_file_id)).enabled:
            logger.info("Speaker %s not clustered: voiceprints off by privacy policy", speaker_id)
            return None

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

                # A correction forbids it, or the speaker is already somebody else.
                if speaker_blocked_from_cluster(
                    self.db, speaker_id, cluster
                ) or identity_conflicts_with_cluster(self.db, speaker, cluster):
                    logger.debug(
                        "Speaker %s blocked from cluster %s by constraint",
                        speaker_id,
                        cluster_uuid,
                    )
                    continue

                # Gender-aware threshold: cross-gender matches need higher similarity
                # Inferred gender only counts while the privacy policy allows it
                # (a person-confirmed gender always does).
                speaker_gender = self._privacy.speaker_gender(speaker)
                if speaker_gender and (cluster.member_count or 0) >= 2:
                    dominant = self._get_cluster_dominant_gender(cluster)
                    if (
                        dominant
                        and dominant != speaker_gender
                        and candidate["similarity"] < GENDER_CROSS_THRESHOLD
                    ):
                        logger.debug(
                            "Speaker %s gender (%s) differs from cluster %s dominant (%s), "
                            "sim %.3f < cross-gender threshold %.3f — skipping",
                            speaker_id,
                            speaker_gender,
                            cluster_uuid,
                            dominant,
                            candidate["similarity"],
                            GENDER_CROSS_THRESHOLD,
                        )
                        continue

                # Joining a promoted cluster links its profile; the centroid, the
                # profile embedding and any cluster the speaker was dropped from are
                # rebuilt by `_settle_assignments` once the caller commits (#1233).
                self._join(speaker, cluster, candidate["similarity"], margin)
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
        self._assigned = SpeakerImpact()
        return None


def cluster_speakers_for_file(
    self: "SpeakerClusteringService",
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

    if not self._privacy.voiceprints(media_file_id).enabled:
        logger.info("File %s not clustered: voiceprints off by privacy policy", media_file_id)
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
    # A background path: a residual is logged at ERROR by the shared cleanup and the
    # assignments themselves have committed, so the task carries on.
    self._settle_assignments()
    return clusters


def add_speaker_to_cluster(
    self: "SpeakerClusteringService",
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

    # Remove memberships in other clusters (speaker can only be in one). Reachable when
    # membership rows exist but ``cluster_id`` is null; each cluster dropped here still
    # averages this voice until ``_settle_assignments`` rebuilds it (issue #1233).
    displaced = clusters_holding_speakers(self.db, [int(speaker.id)]) - {str(cluster.uuid)}
    if displaced:
        self._assigned.cluster_uuids |= displaced
        self._assigned.owner_ids.add(int(cluster.user_id))
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


def bulk_add_speakers_to_cluster(
    self: "SpeakerClusteringService",
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


def create_singleton_cluster(
    self: "SpeakerClusteringService",
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
