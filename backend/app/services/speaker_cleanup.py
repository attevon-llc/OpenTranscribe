"""Leave every store correct after a speaker is deleted or leaves a cluster or profile (#1214).

A voice is copied into three aggregates besides its own voiceprint document: the
``cluster_<uuid>`` centroid of every cluster it is a member of (a weighted mean of the
members' voiceprints), the ``profile_<uuid>`` embedding of the profile it is linked to
(a mean of the members' voiceprints), and the transcript index planes, which carry its
label. #1211 rebuilt the surviving centroids when a FILE was deleted; every per-speaker
path (delete, orphan cleanup after a segment reassignment, speaker merge, cluster
unassign/split/merge/delete, profile delete, profile reassignment) left the old
aggregate in place, so the removed voice stayed searchable inside it.

This module is the ONE implementation of "after this change, make the aggregates agree
with Postgres", shared by all of them and by ``file_cleanup_service.purge_media_file``.
Callers follow two steps around their own write:

1. :func:`capture_speaker_impact` **before** the change, while Postgres still records
   which clusters and profiles held the speaker (``ON DELETE CASCADE`` removes the
   membership rows with the speaker, after which nothing says which centroid averaged
   it in).
2. :func:`reconcile_speaker_impact` **after** the change has committed. It never raises;
   it returns one residual per store that may still hold the removed voice, and a
   request path turns a non-empty result into :class:`SpeakerCleanupIncompleteError`.

The rules it applies, each the product's existing one rather than a new one:

* A cluster left with **no** members is deleted: its row, and its centroid document,
  verified gone by a survivor count. That includes a promoted cluster —
  ``batch_recluster`` recreates promoted clusters from profiles, so nothing depends on
  an empty one, and leaving it listed it as a group with nobody in it.
* A cluster left with **one** member is kept, with a centroid equal to that member's
  voiceprint: upload-time clustering keeps singletons for matching, ``list_clusters``
  hides them and "Re-cluster All" only rebuilds groups of two or more.
* Any other cluster gets ``member_count`` and its centroid recomputed from the
  survivors (``SpeakerClusteringService._update_cluster_centroid(strict=True)``).
* A profile's embedding is recomputed from its remaining members
  (``ProfileEmbeddingService.update_profile_embedding(strict=True)``).
* **A centroid or profile embedding that cannot be rebuilt is erased** (exactly #1211's
  fallback): an absent aggregate is rebuilt by the next recluster/profile update, a
  stale one is a copy of the removed voice.
* The files whose segments lost their speaker are re-indexed, which rebuilds the chunk,
  digest and summary planes' speaker roster from Postgres.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import SearchIndexError

logger = logging.getLogger(__name__)

_Fail = Callable[[str, object], None]

#: Shown to an API client when the change committed but an aggregate may still hold the
#: removed voice. Fixed text: the residual details stay in the server log.
CLEANUP_INCOMPLETE_MESSAGE = (
    "The change was saved, but derived speaker data could not be fully cleaned up. "
    "The failure has been logged for repair."
)

#: Residual ``stage`` names. Fixed vocabulary, so they are safe to log and to audit.
STAGE_VOICEPRINTS = "speakers"
STAGE_CLUSTERS = "clusters"
STAGE_PROFILES = "profiles"
STAGE_INDEX = "transcript_index"


class SpeakerCleanupIncompleteError(SearchIndexError):
    """A speaker change committed, but an aggregate may still carry the removed voice.

    A ``SearchIndexError``, so ``main.py``'s handler answers 503 with the fixed
    :data:`CLEANUP_INCOMPLETE_MESSAGE`; driver text never reaches the client.
    """

    def __init__(self, residual: list[dict[str, Any]]):
        super().__init__(CLEANUP_INCOMPLETE_MESSAGE)
        self.stages = sorted({str(r.get("stage")) for r in residual})


@dataclass
class SpeakerImpact:
    """Plain data naming everything a speaker change touches. No ORM instances.

    Attributes:
        owner_ids: Owners whose clusters may be rewritten. A cluster of any other user is
            never touched, even if a stale impact names it.
        erase_speaker_uuids: Speakers whose OWN voiceprint documents must be gone.
        erase_profile_uuids: Profiles whose embedding documents must be gone (the
            profile row was deleted).
        cluster_uuids: Clusters that held, or now hold, a speaker that moved.
        profile_ids: Profiles whose membership (or a member's weight) changed.
        reindex_file_ids: Files whose transcript index planes name a speaker that left.
    """

    owner_ids: set[int] = field(default_factory=set)
    erase_speaker_uuids: set[str] = field(default_factory=set)
    erase_profile_uuids: set[str] = field(default_factory=set)
    cluster_uuids: set[str] = field(default_factory=set)
    profile_ids: set[int] = field(default_factory=set)
    reindex_file_ids: set[int] = field(default_factory=set)

    def __or__(self, other: SpeakerImpact) -> SpeakerImpact:
        return SpeakerImpact(
            owner_ids=self.owner_ids | other.owner_ids,
            erase_speaker_uuids=self.erase_speaker_uuids | other.erase_speaker_uuids,
            erase_profile_uuids=self.erase_profile_uuids | other.erase_profile_uuids,
            cluster_uuids=self.cluster_uuids | other.cluster_uuids,
            profile_ids=self.profile_ids | other.profile_ids,
            reindex_file_ids=self.reindex_file_ids | other.reindex_file_ids,
        )


def clusters_holding_speakers(db: Session, speaker_ids: Iterable[int]) -> set[str]:
    """UUIDs of the clusters that count one of these speakers as a member.

    Reads both representations of membership — the ``speaker_cluster_member`` rows the
    centroid is computed from and ``Speaker.cluster_id`` — and only clusters of the
    speaker's own owner. Column-only query, no ORM instance escapes.
    """
    from sqlalchemy import or_

    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember

    ids = sorted({int(i) for i in speaker_ids})
    if not ids:
        return set()
    member_of = (
        db.query(SpeakerClusterMember.cluster_id)
        .filter(SpeakerClusterMember.speaker_id.in_(ids))
        .scalar_subquery()
    )
    rows = (
        db.query(SpeakerCluster.uuid)
        .join(Speaker, Speaker.user_id == SpeakerCluster.user_id)
        .filter(
            Speaker.id.in_(ids),
            or_(SpeakerCluster.id.in_(member_of), SpeakerCluster.id == Speaker.cluster_id),
        )
        .distinct()
        .all()
    )
    return {str(row[0]) for row in rows}


def capture_speaker_impact(
    db: Session,
    speaker_ids: Iterable[int],
    *,
    removed: bool,
    reindex: bool,
) -> SpeakerImpact:
    """Read, BEFORE the change, what a change to these speakers will leave stale.

    Args:
        db: The caller's session; only reads.
        speaker_ids: The speakers being deleted or moved.
        removed: The speakers are being deleted — their own voiceprints must go too.
        reindex: Their files' index planes must be rebuilt (their segments change speaker).

    Returns:
        The :class:`SpeakerImpact` to hand to :func:`reconcile_speaker_impact`.
    """
    from app.models.media import Speaker

    ids = sorted({int(i) for i in speaker_ids})
    impact = SpeakerImpact()
    if not ids:
        return impact
    rows = (
        db.query(Speaker.uuid, Speaker.user_id, Speaker.profile_id, Speaker.media_file_id)
        .filter(Speaker.id.in_(ids))
        .all()
    )
    for speaker_uuid, user_id, profile_id, media_file_id in rows:
        impact.owner_ids.add(int(user_id))
        if removed:
            impact.erase_speaker_uuids.add(str(speaker_uuid))
        if profile_id is not None:
            impact.profile_ids.add(int(profile_id))
        if reindex and media_file_id is not None:
            impact.reindex_file_ids.add(int(media_file_id))
    impact.cluster_uuids = clusters_holding_speakers(db, ids)
    return impact


def reconcile_speaker_impact(db: Session, impact: SpeakerImpact) -> list[dict[str, Any]]:
    """Bring every aggregate the impact names in line with Postgres. Never raises.

    Runs after the caller's change has COMMITTED: the rows are the truth here, and a
    rolled-back change must never reach OpenSearch.

    Returns:
        One ``{"stage", "error"}`` dict per store that may still carry the removed voice
        (``stage`` is one of the ``STAGE_*`` names). Empty means every aggregate was
        rebuilt or verifiably erased. Each residual is also logged at ERROR.
    """
    from app.services import file_cleanup_service as fcs

    residual: list[dict[str, Any]] = []

    def _fail(stage: str, err: object) -> None:
        residual.append({"stage": stage, "error": str(err)})

    fcs._erase_speaker_docs(sorted(impact.erase_speaker_uuids), _fail)
    fcs._erase_profile_docs(sorted(impact.erase_profile_uuids), _fail)
    _refresh_clusters(db, impact, _fail)
    _refresh_profiles(db, impact, _fail)
    _reindex_files(db, impact, _fail)
    _invalidate_caches(impact)

    if residual:
        logger.error(
            "Speaker cleanup incomplete: %d store(s) may still hold a removed voice "
            "(stages: %s): %s",
            len(residual),
            sorted({r["stage"] for r in residual}),
            residual,
        )
    return residual


def raise_if_incomplete(residual: list[dict[str, Any]]) -> None:
    """Request-path form: a non-empty residual becomes :class:`SpeakerCleanupIncompleteError`."""
    if residual:
        raise SpeakerCleanupIncompleteError(residual)


def _refresh_clusters(db: Session, impact: SpeakerImpact, fail: _Fail) -> None:
    """Delete emptied clusters, recompute the rest; erase what cannot be rebuilt."""
    if not impact.cluster_uuids or not impact.owner_ids:
        return
    from sqlalchemy import func

    from app.models.media import SpeakerCluster
    from app.models.media import SpeakerClusterMember
    from app.services import file_cleanup_service as fcs
    from app.services.speaker_clustering_service import CentroidRecomputeError
    from app.services.speaker_clustering_service import SpeakerClusteringService

    to_erase: list[str] = []
    try:
        clusters = (
            db.query(SpeakerCluster)
            .filter(
                SpeakerCluster.uuid.in_(sorted(impact.cluster_uuids)),
                SpeakerCluster.user_id.in_(sorted(impact.owner_ids)),
            )
            .all()
        )
        service = SpeakerClusteringService(db)
        for cluster in clusters:
            cluster_uuid = str(cluster.uuid)
            remaining = int(
                db.query(func.count(SpeakerClusterMember.id))
                .filter(SpeakerClusterMember.cluster_id == cluster.id)
                .scalar()
                or 0
            )
            if remaining == 0:
                to_erase.append(cluster_uuid)
                db.delete(cluster)
                continue
            cluster.member_count = remaining  # type: ignore[assignment]
            try:
                service._update_cluster_centroid(cluster, int(cluster.user_id), strict=True)
            except CentroidRecomputeError as e:
                logger.warning(f"Centroid of cluster {cluster_uuid} not rebuildable ({e}); erasing")
                to_erase.append(cluster_uuid)
        db.commit()
    except Exception:  # reported below, never swallowed
        db.rollback()
        logger.exception("Could not refresh the clusters a speaker change touched")
        fail(STAGE_CLUSTERS, "could not refresh cluster centroids")
        return

    fcs._erase_cluster_docs(to_erase, fail)


def _refresh_profiles(db: Session, impact: SpeakerImpact, fail: _Fail) -> None:
    """Recompute each profile's embedding from its members; erase what cannot be rebuilt."""
    if not impact.profile_ids:
        return
    from app.models.media import SpeakerProfile
    from app.services import file_cleanup_service as fcs
    from app.services.profile_embedding_service import ProfileEmbeddingRecomputeError
    from app.services.profile_embedding_service import ProfileEmbeddingService

    to_erase: list[str] = []
    for profile_id in sorted(impact.profile_ids):
        try:
            row = db.query(SpeakerProfile.uuid).filter(SpeakerProfile.id == profile_id).first()
            if row is None:
                continue  # deleted with the change; its document is the deleter's to erase
            profile_uuid = str(row[0])
            try:
                ProfileEmbeddingService.update_profile_embedding(db, profile_id, strict=True)
            except ProfileEmbeddingRecomputeError as e:
                logger.warning(
                    f"Embedding of profile {profile_uuid} not rebuildable ({e}); erasing"
                )
                db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).update(
                    {SpeakerProfile.embedding_count: 0}
                )
                db.commit()
                to_erase.append(profile_uuid)
        except Exception:  # reported below, never swallowed
            db.rollback()
            logger.exception(f"Could not refresh profile {profile_id} after a speaker change")
            fail(STAGE_PROFILES, "could not refresh a profile embedding")

    fcs._erase_profile_docs(to_erase, fail)


def _reindex_files(db: Session, impact: SpeakerImpact, fail: _Fail) -> None:
    """Rebuild the index planes of every file whose segments lost their speaker.

    The same debounced dispatch every #666 path uses. ``dispatch_transcript_reindex``
    never raises and logs a failed dispatch with its traceback itself (it cannot tell its
    caller a failure from "already queued"); a failure to even find the files is a
    residual here, because nothing else would ever rebuild a roster that is merely stale.
    """
    if not impact.reindex_file_ids:
        return
    from app.models.media import MediaFile
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    try:
        rows = (
            db.query(MediaFile.id, MediaFile.uuid, MediaFile.user_id)
            .filter(MediaFile.id.in_(sorted(impact.reindex_file_ids)))
            .all()
        )
        db.commit()
    except Exception:  # reported below, never swallowed
        db.rollback()
        logger.exception("Could not read the files whose index planes name a removed speaker")
        fail(STAGE_INDEX, "could not queue the transcript reindex")
        return
    for file_id, file_uuid, user_id in rows:
        dispatch_transcript_reindex(file_id=int(file_id), file_uuid=str(file_uuid), user_id=user_id)


def _invalidate_caches(impact: SpeakerImpact) -> None:
    for owner_id in sorted(impact.owner_ids):
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_speakers(owner_id)
            redis_cache.invalidate_user_files(owner_id)
        except Exception as e:  # caches expire on their own TTL
            logger.debug(f"Speaker cleanup: cache invalidation failed (non-critical): {e}")
