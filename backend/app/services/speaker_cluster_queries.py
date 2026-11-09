"""Speaker-cluster reads and writes behind the cluster endpoints.

The endpoints decide tenancy (``file_ids_in_scope``, ``org_scope_pred``) and pass it in as SQL
criteria. Functions take a ``Session`` and plain arguments and never commit.
"""

from collections.abc import Sequence

from sqlalchemy import ColumnExpressionArgument
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember


def speaker_of_user_by_uuid(db: Session, speaker_uuid: str, user_id: int) -> Speaker | None:
    return (
        db.query(Speaker).filter(Speaker.uuid == speaker_uuid, Speaker.user_id == user_id).first()
    )


def longest_segment_of_speaker(db: Session, speaker_id: int) -> TranscriptSegment | None:
    """The speaker's longest segment; equal durations rank by id (storage order is unstable)."""
    return (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id == speaker_id)
        .order_by(
            (TranscriptSegment.end_time - TranscriptSegment.start_time).desc(),
            TranscriptSegment.id,
        )
        .first()
    )


def speaker_totals(
    db: Session, speaker_criteria: Sequence[ColumnExpressionArgument[bool]]
) -> tuple[int, int]:
    """``(speakers, speakers assigned to a cluster)`` among those matching the criteria."""
    total, clustered = (
        db.query(func.count(Speaker.id), func.count(Speaker.cluster_id))
        .filter(*speaker_criteria)
        .one()
    )
    return int(total), int(clustered)


def count_clusters(db: Session, criteria: Sequence[ColumnExpressionArgument[bool]]) -> int:
    return db.query(SpeakerCluster).filter(*criteria).count()


def cluster_matching(
    db: Session, criteria: Sequence[ColumnExpressionArgument[bool]]
) -> SpeakerCluster | None:
    """The first cluster satisfying every criterion (callers pass uuid, owner and tenant)."""
    return db.query(SpeakerCluster).filter(*criteria).first()


def dissolve_cluster_membership(db: Session, cluster_id: int) -> None:
    """Reset members' ``cluster_id`` and drop the membership rows; no commit.

    The cluster row and its centroid are removed afterwards by ``speaker_cleanup``.
    """
    db.query(Speaker).filter(Speaker.cluster_id == cluster_id).update({"cluster_id": None})
    db.query(SpeakerClusterMember).filter(SpeakerClusterMember.cluster_id == cluster_id).delete(
        synchronize_session=False
    )
