"""Read side of clusters: the paginated list and the per-cluster detail."""

import logging
import math
from collections import defaultdict
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import case
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import subqueryload

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.services.clustering.gender import compute_gender_composition
from app.services.permission_service import org_scope_pred
from app.services.privacy.redact_view import redact_speaker_rows
from app.utils.sql_like import escape_like

if TYPE_CHECKING:
    from app.services.speaker_clustering_service import SpeakerClusteringService

logger = logging.getLogger(__name__)


def list_clusters(
    self: "SpeakerClusteringService",
    user_id: int,
    page: int = 1,
    per_page: int = 20,
    has_label: bool | None = None,
    search: str | None = None,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any]:
    """List the caller's clusters of one tenant with pagination and filtering.

    Args:
        user_id: Owner user ID.
        page: Page number (1-based).
        per_page: Items per page.
        has_label: Filter by whether cluster has a label.
        search: Search in cluster labels.

    Returns:
        Dict with items, total, page, per_page, pages.
    """
    tenant_filters = [SpeakerCluster.user_id == user_id]
    tenant_pred = org_scope_pred(SpeakerCluster.organization_id, organization_id)
    if tenant_pred is not None:
        tenant_filters.append(tenant_pred)
    # Privacy policy (#1280): clusters are voiceprint-derived, so a scope with voiceprints
    # off lists none (they are hidden, not deleted: re-enabling brings them back).
    off_scopes = self._voiceprint_off_scopes(user_id, organization_id)
    if off_scopes:
        tenant_filters.append(self._scope_not_in(SpeakerCluster.organization_id, off_scopes))
    # A cluster is a GROUP. Upload-time clustering keeps a one-speaker cluster per unmatched
    # speaker so a later file can match it, but "Re-cluster All" rebuilds only groups of
    # two or more; listing the singletons made the tab fill up after an upload and empty
    # after a recluster. They stay in the table for matching and are still reviewed in the
    # inbox, which lists every unverified speaker whatever its cluster. A named or promoted
    # cluster is always shown.
    tenant_filters.append(
        (SpeakerCluster.member_count >= 2)
        | SpeakerCluster.label.isnot(None)
        | SpeakerCluster.promoted_to_profile_id.isnot(None)
    )
    query = (
        self.db.query(SpeakerCluster)
        .options(
            subqueryload(SpeakerCluster.promoted_to_profile),
        )
        .filter(*tenant_filters)
    )

    if has_label is True:
        query = query.filter(SpeakerCluster.label.isnot(None))
    elif has_label is False:
        query = query.filter(SpeakerCluster.label.is_(None))

    if search:
        query = query.filter(SpeakerCluster.label.ilike(f"%{escape_like(search)}%", escape="\\"))

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

    base_count_query = self.db.query(SpeakerCluster).filter(*tenant_filters)
    labeled_count = base_count_query.filter(
        (SpeakerCluster.label.isnot(None)) | (SpeakerCluster.promoted_to_profile_id.isnot(None))
    ).count()
    unlabeled_count = base_count_query.filter(
        SpeakerCluster.label.is_(None),
        SpeakerCluster.promoted_to_profile_id.is_(None),
    ).count()
    # Most recent cluster update = last time clustering ran
    last_clustered_at = (
        self.db.query(sa_func.max(SpeakerCluster.updated_at)).filter(*tenant_filters).scalar()
    )
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page
    clusters = query.offset(offset).limit(per_page).all()

    # Batch-query gender composition for this page's clusters. Inferred gender counts
    # only while the member's owner/organization allows it (#1280); a person-confirmed
    # gender always does, so the query keeps the two apart.
    cluster_ids = [int(c.id) for c in clusters]
    gender_by_cluster: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if cluster_ids:
        from sqlalchemy import func as sa_func

        gender_rows = (
            self.db.query(
                SpeakerClusterMember.cluster_id,
                Speaker.predicted_gender,
                Speaker.gender_confirmed_by_user,
                MediaFile.user_id,
                MediaFile.organization_id,
                sa_func.count().label("cnt"),
            )
            .join(Speaker, SpeakerClusterMember.speaker_id == Speaker.id)
            .join(MediaFile, MediaFile.id == Speaker.media_file_id)
            .filter(SpeakerClusterMember.cluster_id.in_(cluster_ids))
            .group_by(
                SpeakerClusterMember.cluster_id,
                Speaker.predicted_gender,
                Speaker.gender_confirmed_by_user,
                MediaFile.user_id,
                MediaFile.organization_id,
            )
            .all()
        )
        for row in gender_rows:
            shown = row[1]
            if shown and not row[2]:
                org = int(row[4]) if row[4] is not None else None
                if not self._privacy.gender_for_scope(int(row[3]), org).enabled:
                    shown = None
            gender_key = shown if shown else "__unknown__"
            gender_by_cluster[int(row[0])][gender_key] += int(row[5])

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
        gender_composition = compute_gender_composition(gc_members)

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
    self: "SpeakerClusteringService",
    cluster_uuid: str,
    user_id: int,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any] | None:
    """Get cluster detail with all members.

    Args:
        cluster_uuid: Cluster UUID.
        user_id: Owner user ID.

    Returns:
        Cluster detail dict, or None if not found.
    """
    detail_query = (
        self.db.query(SpeakerCluster)
        .options(
            joinedload(SpeakerCluster.members)
            .joinedload(SpeakerClusterMember.speaker)
            .joinedload(Speaker.media_file),
            joinedload(SpeakerCluster.promoted_to_profile),
        )
        .filter(SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id)
    )
    detail_pred = org_scope_pred(SpeakerCluster.organization_id, organization_id)
    if detail_pred is not None:
        detail_query = detail_query.filter(detail_pred)
    cluster = detail_query.first()
    if not cluster:
        return None

    # Privacy policy (#1280): a cluster of a scope with voiceprints off is hidden, and
    # answers exactly like a missing one.
    if not self._privacy.voiceprints_for_scope(user_id, cluster.organization_id).enabled:
        return None

    members = cluster.members
    member_speakers = [m.speaker for m in members if m.speaker]
    redact_speaker_rows(self._privacy, member_speakers)

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
        members_with_segments = {r[0] for r in seg_rows if r[0] is not None}

    member_items = []
    for m in members:
        speaker = m.speaker
        if not speaker:
            continue

        media_file = speaker.media_file

        # `redact_speaker_rows` above already withheld an inferred gender and its
        # confidence the policy forbids, so these reads are the policy-filtered values.
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

    gender_composition = compute_gender_composition(member_items)
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
