"""
Speaker Clustering Service for pre-clustering unnamed speakers across files.

Uses a hybrid approach:
- Real-time: kNN against cluster centroids in OpenSearch + threshold assignment
- Batch: GPU-accelerated cosine similarity (PyTorch) with AHC (complete linkage)

Cluster centroids are stored in OpenSearch with document_type="cluster".
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.services.clustering import admin_ops
from app.services.clustering import assignment
from app.services.clustering import centroids
from app.services.clustering import cluster_identity
from app.services.clustering import gender
from app.services.clustering import queries
from app.services.clustering import recluster
from app.services.clustering import similarity
from app.services.clustering import verification
from app.services.permission_service import org_scope_pred
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import organizations_with_voiceprints_off
from app.services.speaker_cleanup import SpeakerImpact
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.services.speaker_identity_moves import join_cluster_identity
from app.services.speaker_rename_tracker import SpeakerRenameTracker

logger = logging.getLogger(__name__)


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
        # Every aggregate an assignment made stale — the joined clusters, the clusters a
        # joining speaker was dropped from, the profiles it entered or left — settled
        # through ``speaker_cleanup`` once the assignments commit (issue #1233).
        self._assigned = SpeakerImpact()
        # The members the last ``merge_clusters`` left in the source because an
        # unassignment forbids them from the target; the endpoint reports them.
        self.blocked_speaker_uuids: list[str] = []
        self._privacy_view: PrivacyView | None = None

    def _settle_assignments(self) -> list[dict[str, Any]]:
        """Rebuild what this instance's committed assignments touched. Never raises.

        Call after the commit, never before: a rolled-back assignment must not reach
        OpenSearch. Returns the cleanup's residuals (already logged at ERROR).
        """
        impact, self._assigned = self._assigned, SpeakerImpact()
        if impact.is_empty():
            return []
        return reconcile_speaker_impact(self.db, impact)

    def _join(
        self, speaker: Speaker, cluster: SpeakerCluster, confidence: float, margin: float | None
    ) -> None:
        """Add a speaker to a cluster and take on its identity, recording what is stale."""
        self._add_speaker_to_cluster(speaker, cluster, confidence, margin=margin)
        self._assigned.owner_ids.add(int(cluster.user_id))
        self._assigned.cluster_uuids.add(str(cluster.uuid))
        # Every caller places a speaker by comparing voices (upload-time clustering, Re-cluster),
        # so the identity it takes is a voiceprint's, not a person's (issue #1280).
        touched_profiles = join_cluster_identity(
            self.db,
            speaker,
            cluster,
            self._rename_tracker,
            source="voiceprint",
            confidence=confidence,
        )
        if touched_profiles:
            self._assigned.profile_ids |= touched_profiles
            self._assigned.relabel_speaker_ids.add(int(speaker.id))

    def _scoped_cluster_query(
        self, cluster_uuid: str, user_id: int, organization_id: OrgScope = UNSCOPED
    ) -> Any:
        """The caller's cluster ``cluster_uuid``, restricted to one tenant.

        ``organization_id``: int = that org, None = personal clusters only,
        ``UNSCOPED`` = no tenant gate (background/legacy callers).
        """
        query = self.db.query(SpeakerCluster).filter(
            SpeakerCluster.uuid == cluster_uuid, SpeakerCluster.user_id == user_id
        )
        pred = org_scope_pred(SpeakerCluster.organization_id, organization_id)
        return query.filter(pred) if pred is not None else query

    def _media_file_org_id(self, media_file_id: int | None) -> int | None:
        """Resolve the tenant scope of a speaker via its media file's org."""
        if media_file_id is None:
            return None
        row = self.db.query(MediaFile.organization_id).filter(MediaFile.id == media_file_id).first()
        return int(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------
    # Privacy policy (#1280)
    # ------------------------------------------------------------------

    @property
    def _privacy(self) -> PrivacyView:
        """The per-instance policy view. One service instance is one unit of work."""
        if self._privacy_view is None:
            self._privacy_view = PrivacyView(self.db)
        return self._privacy_view

    def _voiceprint_off_scopes(
        self, user_id: int, organization_id: OrgScope = UNSCOPED
    ) -> set[int | None]:
        """Organization scopes (None = personal) of this user's data with voiceprints off.

        A concrete ``organization_id`` asks about that one scope. ``UNSCOPED`` asks about
        every scope the user has a cluster OR a speaker in, which is what a read or a rebuild
        that spans scopes needs: a scope that has speakers and no cluster yet is exactly the
        one a re-cluster would otherwise group for the first time.
        """
        if isinstance(organization_id, _Unscoped):
            cluster_scopes = (
                self.db.query(SpeakerCluster.organization_id)
                .filter(SpeakerCluster.user_id == user_id)
                .distinct()
                .all()
            )
            speaker_scopes = (
                self.db.query(MediaFile.organization_id)
                .join(Speaker, Speaker.media_file_id == MediaFile.id)
                .filter(Speaker.user_id == user_id)
                .distinct()
                .all()
            )
            candidates: set[int | None] = {
                (int(r[0]) if r[0] is not None else None)
                for r in (*cluster_scopes, *speaker_scopes)
            }
        else:
            candidates = {organization_id}
        return organizations_with_voiceprints_off(self.db, user_id, candidates)

    @staticmethod
    def _scope_not_in(column: Any, off: set[int | None]) -> Any:
        """A filter keeping rows whose organization is NOT one of ``off`` (None = personal)."""
        from sqlalchemy import and_
        from sqlalchemy import or_

        ints = [o for o in off if o is not None]
        if None in off and ints:
            return and_(column.isnot(None), column.notin_(ints))
        if None in off:
            return column.isnot(None)
        return or_(column.is_(None), column.notin_(ints))

    # The implementations live in ``app.services.clustering``; each takes the service as ``self``.
    find_or_create_cluster = assignment.find_or_create_cluster
    cluster_speakers_for_file = assignment.cluster_speakers_for_file
    _add_speaker_to_cluster = assignment.add_speaker_to_cluster
    _bulk_add_speakers_to_cluster = assignment.bulk_add_speakers_to_cluster
    _create_singleton_cluster = assignment.create_singleton_cluster

    largest_recluster_partition = recluster.largest_recluster_partition
    batch_recluster = recluster.batch_recluster
    _compute_similarity_groups = similarity.compute_similarity_groups

    merge_clusters = admin_ops.merge_clusters
    split_cluster = admin_ops.split_cluster
    promote_cluster_to_profile = cluster_identity.promote_cluster_to_profile
    unassign_speakers = cluster_identity.unassign_speakers

    analyze_gender_outliers = gender.analyze_gender_outliers
    _get_cluster_dominant_gender = gender.get_cluster_dominant_gender

    get_unverified_speakers = verification.get_unverified_speakers
    batch_verify_speakers = verification.batch_verify_speakers

    list_clusters = queries.list_clusters
    get_cluster_detail = queries.get_cluster_detail

    _update_cluster_centroid = centroids.update_cluster_centroid
    _get_speaker_embedding = centroids.get_speaker_embedding
