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

It is also the ONE implementation of the unassign rule and of the blacklist every
assignment path honours (issue #1233), at the bottom of this module:

* :func:`detach_from_cluster` — unassigning a speaker from a cluster means that section is
  no longer that person: it leaves the cluster, leaves the cluster's promoted profile
  (:func:`unlink_from_profile`: a name that came from the profile is cleared, a name the
  user typed stays) and, when blacklisting, is recorded as cannot-link against every
  remaining member and as blacklisted from the profile (:func:`record_unassignment`).
  The cluster and profile remain for everyone else.
* :func:`speaker_blocked_from_cluster` / :func:`speaker_blocked_from_profile` — the check
  every automatic assignment path makes before it links a speaker.
* :func:`join_cluster_identity` — joining a promoted cluster links its profile and takes
  its name, the one way every path propagates a cluster's identity.
* :func:`lift_profile_blacklist` — a user who explicitly names one speaker as that
  profile again overrides their own earlier correction; only explicit single-speaker
  actions call it.
* :func:`has_other_identity` / :func:`identity_conflicts_with_cluster` — an automatic
  path never renames a speaker that is already somebody else.
* A promoted cluster holds only its profile's speakers and unnamed ones: refreshing it
  releases a member since linked to another profile (decision 5 of #1233).

A correction outlives a re-run of its file through ``speaker_correction_carryover``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import SearchIndexError

if TYPE_CHECKING:
    from app.models.media import Speaker
    from app.models.media import SpeakerCluster
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

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
        relabel_speaker_ids: Speakers whose profile link or label changed, so their own
            voiceprint document's ``profile_id``/``display_name`` must be rewritten.
    """

    owner_ids: set[int] = field(default_factory=set)
    erase_speaker_uuids: set[str] = field(default_factory=set)
    erase_profile_uuids: set[str] = field(default_factory=set)
    cluster_uuids: set[str] = field(default_factory=set)
    profile_ids: set[int] = field(default_factory=set)
    reindex_file_ids: set[int] = field(default_factory=set)
    relabel_speaker_ids: set[int] = field(default_factory=set)

    def __or__(self, other: SpeakerImpact) -> SpeakerImpact:
        return SpeakerImpact(
            owner_ids=self.owner_ids | other.owner_ids,
            erase_speaker_uuids=self.erase_speaker_uuids | other.erase_speaker_uuids,
            erase_profile_uuids=self.erase_profile_uuids | other.erase_profile_uuids,
            cluster_uuids=self.cluster_uuids | other.cluster_uuids,
            profile_ids=self.profile_ids | other.profile_ids,
            reindex_file_ids=self.reindex_file_ids | other.reindex_file_ids,
            relabel_speaker_ids=self.relabel_speaker_ids | other.relabel_speaker_ids,
        )

    def is_empty(self) -> bool:
        """Nothing for :func:`reconcile_speaker_impact` to rebuild, erase or resync."""
        return not (
            self.erase_speaker_uuids
            or self.erase_profile_uuids
            or self.cluster_uuids
            or self.profile_ids
            or self.reindex_file_ids
            or self.relabel_speaker_ids
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


def profile_move_impact(
    db: Session,
    speaker_id: int,
    old_profile_id: int | None,
    new_profile_id: int | None,
    *,
    voiceprints_enabled: bool,
) -> SpeakerImpact:
    """Impact of a speaker moving between profiles (or into / out of one), read BEFORE reconcile.

    The privacy policy (#1280) restricts what is WRITTEN, never what is erased. The profile
    the speaker left, and the clusters it belongs to, are always named: their membership
    bookkeeping runs and an aggregate that still averages the departed voice is rebuilt or,
    while voiceprints are off and nothing can be rebuilt, erased (``strict`` refuses the
    recompute and the reconciler erases), exactly as for a deletion. The profile it JOINED is
    named only while voiceprints are on: folding a voice into an aggregate is the addition
    the switch forbids, so it is left as it was and re-enabling finds it intact.

    Args:
        db: The caller's session; only reads.
        speaker_id: The speaker whose profile link changed.
        old_profile_id: The profile it left, or ``None``.
        new_profile_id: The profile it joined, or ``None``.
        voiceprints_enabled: The policy answer for the speaker's file, read by the caller.
    """
    impact = capture_speaker_impact(db, [speaker_id], removed=False, reindex=False)
    gained = new_profile_id if new_profile_id != old_profile_id else None
    impact.profile_ids |= {p for p in (old_profile_id, new_profile_id) if p}
    if not voiceprints_enabled and gained is not None:
        impact.profile_ids.discard(gained)
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
    _resync_speaker_docs(db, impact.relabel_speaker_ids)
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
            if cluster.promoted_to_profile_id is not None:
                _release_members_of_other_profiles(db, cluster)
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

    The same debounced dispatch every #666 path uses. ``queue_transcript_reindex`` tells
    a failed dispatch apart from one already pending in the debounce window (issue
    #1233): the first is a residual — nothing else would ever rebuild a roster that is
    merely stale — the second is the reindex this change needs, already queued. Failing
    to even find the files is a residual for the same reason.
    """
    if not impact.reindex_file_ids:
        return
    from app.models.media import MediaFile
    from app.services.search.reindex_dispatch import ReindexDispatch
    from app.services.search.reindex_dispatch import queue_transcript_reindex

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
        outcome = queue_transcript_reindex(
            file_id=int(file_id), file_uuid=str(file_uuid), user_id=user_id
        )
        if outcome is ReindexDispatch.FAILED:
            fail(STAGE_INDEX, "could not queue the transcript reindex")


def _invalidate_caches(impact: SpeakerImpact) -> None:
    for owner_id in sorted(impact.owner_ids):
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_speakers(owner_id)
            redis_cache.invalidate_user_files(owner_id)
        except Exception as e:  # caches expire on their own TTL
            logger.debug(f"Speaker cleanup: cache invalidation failed (non-critical): {e}")


def _resync_speaker_docs(db: Session, speaker_ids: set[int]) -> None:
    """Write the relabelled speakers' Postgres identity onto their own voiceprint documents.

    A voiceprint document carries ``profile_id``/``display_name`` beside the vector, and a
    profile kNN suggestion reads the profile from it. Both writers log their own failures
    and a speaker with no indexed voiceprint has nothing to update, so this is not a
    residual: the label it repairs is metadata, the voice itself is untouched.
    """
    if not speaker_ids:
        return
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile
    from app.services.opensearch_service import update_speaker_display_name
    from app.services.opensearch_service import update_speaker_profile

    try:
        rows = (
            db.query(
                Speaker.uuid,
                Speaker.profile_id,
                SpeakerProfile.uuid,
                Speaker.verified,
                Speaker.display_name,
            )
            .outerjoin(SpeakerProfile, SpeakerProfile.id == Speaker.profile_id)
            .filter(Speaker.id.in_(sorted(speaker_ids)))
            .all()
        )
        db.commit()
    except Exception:  # logged; the document keeps its old label until the next speaker sync
        db.rollback()
        logger.exception("Could not read the relabelled speakers to resync their documents")
        return
    for speaker_uuid, profile_id, profile_uuid, verified, display_name in rows:
        update_speaker_profile(
            speaker_uuid=str(speaker_uuid),
            profile_id=int(profile_id) if profile_id is not None else None,
            profile_uuid=str(profile_uuid) if profile_uuid is not None else None,
            verified=bool(verified),
        )
        update_speaker_display_name(str(speaker_uuid), display_name)


# --------------------------------------------------------------------------------------
# The unassign rule and the blacklist every assignment path honours (issue #1233)
# --------------------------------------------------------------------------------------

#: ``reason`` written on the constraint rows a user's unassignment records.
REASON_UNASSIGNED = "unassigned_by_user"
#: ``reason`` written on the rows a speaker merge copies from the source to the target.
REASON_MERGED = "inherited_by_merge"
#: ``reason`` written when a user rejects a profile identification of a speaker.
REASON_REJECTED = "rejected_by_user"


def blacklisted_profile_ids(db: Session, speaker_ids: Iterable[int]) -> dict[int, set[int]]:
    """For each speaker, the profiles it was unassigned from (``speaker_profile_blacklist``)."""
    from app.models.media import SpeakerProfileBlacklist

    ids = sorted({int(i) for i in speaker_ids})
    out: dict[int, set[int]] = {i: set() for i in ids}
    if not ids:
        return out
    rows = (
        db.query(SpeakerProfileBlacklist.speaker_id, SpeakerProfileBlacklist.profile_id)
        .filter(SpeakerProfileBlacklist.speaker_id.in_(ids))
        .all()
    )
    for speaker_id, profile_id in rows:
        out[int(speaker_id)].add(int(profile_id))
    return out


def cannot_link_partners(db: Session, speaker_ids: Iterable[int]) -> dict[int, set[int]]:
    """For each speaker, every speaker it must never share a cluster with.

    Symmetric whichever direction a row was written in: unassign writes both, but a
    one-directional row (older data, a hand edit) must still keep the pair apart.
    """
    from sqlalchemy import or_

    from app.models.media import SpeakerCannotLink

    ids = sorted({int(i) for i in speaker_ids})
    out: dict[int, set[int]] = {i: set() for i in ids}
    if not ids:
        return out
    rows = (
        db.query(SpeakerCannotLink.speaker_id, SpeakerCannotLink.cannot_link_speaker_id)
        .filter(
            or_(
                SpeakerCannotLink.speaker_id.in_(ids),
                SpeakerCannotLink.cannot_link_speaker_id.in_(ids),
            )
        )
        .all()
    )
    for a, b in rows:
        if int(a) in out:
            out[int(a)].add(int(b))
        if int(b) in out:
            out[int(b)].add(int(a))
    return out


def blocked_profile_names(db: Session, speaker_ids: Iterable[int]) -> dict[int, set[str]]:
    """For each speaker, the casefolded names of every profile it is blocked from.

    For the paths that assign by NAME rather than by profile id (accepting a suggestion,
    a voice-match suggestion copied from another speaker's label): the profiles it was
    unassigned from, and the profiles its cannot-link partners are linked to.
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile

    ids = sorted({int(i) for i in speaker_ids})
    out: dict[int, set[str]] = {i: set() for i in ids}
    if not ids:
        return out
    blocked = blacklisted_profile_ids(db, ids)
    partners = cannot_link_partners(db, ids)
    partner_ids = sorted(set().union(*partners.values()))
    partner_profile: dict[int, int] = {}
    if partner_ids:
        partner_profile = {
            int(sid): int(pid)
            for sid, pid in db.query(Speaker.id, Speaker.profile_id)
            .filter(Speaker.id.in_(partner_ids), Speaker.profile_id.isnot(None))
            .all()
            if pid is not None
        }
    wanted = set().union(*blocked.values()) | set(partner_profile.values())
    names: dict[int, str] = {}
    if wanted:
        names = {
            int(pid): str(name)
            for pid, name in db.query(SpeakerProfile.id, SpeakerProfile.name)
            .filter(SpeakerProfile.id.in_(sorted(wanted)))
            .all()
        }
    for sid in ids:
        profile_ids = blocked[sid] | {
            partner_profile[p] for p in partners[sid] if p in partner_profile
        }
        out[sid] = {names[p].casefold() for p in profile_ids if p in names}
    return out


def speaker_blocked_from_profile(db: Session, speaker_id: int, profile_id: int | None) -> bool:
    """Whether a correction forbids linking this speaker to this profile.

    Blocked when the speaker was unassigned from the profile, or when it is cannot-link
    with a speaker currently linked to it: "not the same person as Y" while Y is Bob
    means "not Bob".
    """
    if profile_id is None:
        return False
    from app.models.media import Speaker

    sid = int(speaker_id)
    if int(profile_id) in blacklisted_profile_ids(db, [sid])[sid]:
        return True
    partners = cannot_link_partners(db, [sid])[sid]
    if not partners:
        return False
    linked = (
        db.query(Speaker.id)
        .filter(Speaker.id.in_(sorted(partners)), Speaker.profile_id == int(profile_id))
        .first()
    )
    return linked is not None


def speaker_blocked_from_cluster(db: Session, speaker_id: int, cluster: SpeakerCluster) -> bool:
    """Whether a correction forbids this speaker from joining this cluster.

    Blocked by the promoted profile (:func:`speaker_blocked_from_profile`) or by a
    cannot-link with any current member, read from both membership representations.
    Queries the rows rather than ``cluster.members``, which a caller that has just
    flushed new members may hold stale.
    """
    from sqlalchemy import or_

    from app.models.media import Speaker
    from app.models.media import SpeakerClusterMember

    if cluster.promoted_to_profile_id is not None and speaker_blocked_from_profile(
        db, speaker_id, int(cluster.promoted_to_profile_id)
    ):
        return True
    partners = cannot_link_partners(db, [int(speaker_id)])[int(speaker_id)]
    if not partners:
        return False
    member_of = (
        db.query(SpeakerClusterMember.speaker_id)
        .filter(SpeakerClusterMember.cluster_id == cluster.id)
        .scalar_subquery()
    )
    conflict = (
        db.query(Speaker.id)
        .filter(
            Speaker.id.in_(sorted(partners)),
            or_(Speaker.id.in_(member_of), Speaker.cluster_id == cluster.id),
        )
        .first()
    )
    return conflict is not None


def has_other_identity(db: Session, speaker: Speaker, profile_id: int) -> bool:
    """Whether the speaker is already somebody other than this profile.

    Linked to a different profile, or carrying a name (typed, or an accepted suggestion)
    that is not this profile's. A voice similarity never outranks an identity the speaker
    already has, so no automatic path takes such a speaker over (issue #1233).
    """
    from app.models.media import SpeakerProfile
    from app.utils.speaker_labels import is_assigned_speaker_name

    if speaker.profile_id is not None:
        return int(speaker.profile_id) != int(profile_id)
    if not is_assigned_speaker_name(speaker.display_name):
        return False
    name = db.query(SpeakerProfile.name).filter(SpeakerProfile.id == int(profile_id)).scalar()
    return name is None or str(speaker.display_name).strip().casefold() != str(name).casefold()


def identity_conflicts_with_cluster(db: Session, speaker: Speaker, cluster: SpeakerCluster) -> bool:
    """Whether joining this promoted cluster would rename a speaker that is someone else."""
    profile_id = cluster.promoted_to_profile_id
    return profile_id is not None and has_other_identity(db, speaker, int(profile_id))


def _release_members_of_other_profiles(db: Session, cluster: SpeakerCluster) -> None:
    """A promoted cluster holds only its profile's speakers and unnamed ones (decision 5).

    A member since linked to another profile (renamed, assigned, accepted) is that
    person's now: it leaves the cluster, so the centroid stops averaging it and new
    speakers of its voice stop matching this profile. Nothing is blacklisted — the link
    to the other profile already says who it is.
    """
    from sqlalchemy import or_

    from app.models.media import Speaker
    from app.models.media import SpeakerClusterMember

    member_of = (
        db.query(SpeakerClusterMember.speaker_id)
        .filter(SpeakerClusterMember.cluster_id == cluster.id)
        .scalar_subquery()
    )
    leaving = [
        int(r[0])
        for r in db.query(Speaker.id)
        .filter(
            or_(Speaker.id.in_(member_of), Speaker.cluster_id == cluster.id),
            Speaker.profile_id.isnot(None),
            Speaker.profile_id != cluster.promoted_to_profile_id,
        )
        .all()
    ]
    if not leaving:
        return
    db.query(SpeakerClusterMember).filter(
        SpeakerClusterMember.cluster_id == cluster.id,
        SpeakerClusterMember.speaker_id.in_(leaving),
    ).delete(synchronize_session="fetch")
    db.query(Speaker).filter(Speaker.id.in_(leaving), Speaker.cluster_id == cluster.id).update(
        {Speaker.cluster_id: None}, synchronize_session="fetch"
    )
    logger.info(
        "Cluster %s released %d member(s) now linked to another profile",
        cluster.uuid,
        len(leaving),
    )


def blocking_profile_ids_named(db: Session, speaker_id: int, name: str) -> set[int]:
    """The profiles called ``name`` that a correction blocks this speaker from.

    The id-level twin of :func:`blocked_profile_names`, for an explicit override that
    must lift exactly what blocked the name it is applying.
    """
    from app.models.media import Speaker
    from app.models.media import SpeakerProfile

    sid = int(speaker_id)
    candidates = set(blacklisted_profile_ids(db, [sid])[sid])
    partners = cannot_link_partners(db, [sid])[sid]
    if partners:
        candidates |= {
            int(pid)
            for (pid,) in db.query(Speaker.profile_id)
            .filter(Speaker.id.in_(sorted(partners)), Speaker.profile_id.isnot(None))
            .all()
            if pid is not None
        }
    if not candidates:
        return set()
    wanted = name.strip().casefold()
    return {
        int(pid)
        for pid, pname in db.query(SpeakerProfile.id, SpeakerProfile.name)
        .filter(SpeakerProfile.id.in_(sorted(candidates)))
        .all()
        if str(pname).casefold() == wanted
    }


def record_unassignment(
    db: Session,
    speaker_ids: Iterable[int],
    *,
    against_speaker_ids: Iterable[int],
    profile_id: int | None,
    reason: str = REASON_UNASSIGNED,
) -> None:
    """Record that these speakers are neither the others nor the profile. Idempotent.

    Cannot-link rows in both directions against every speaker in ``against_speaker_ids``,
    and a profile blacklist row when ``profile_id`` is set. Adds to the session; the
    caller commits with its own change.
    """
    from app.models.media import SpeakerCannotLink
    from app.models.media import SpeakerProfileBlacklist

    removed = sorted({int(i) for i in speaker_ids})
    others = sorted({int(i) for i in against_speaker_ids} - set(removed))
    if not removed:
        return
    if others:
        existing = set(
            db.query(SpeakerCannotLink.speaker_id, SpeakerCannotLink.cannot_link_speaker_id)
            .filter(
                SpeakerCannotLink.speaker_id.in_(removed + others),
                SpeakerCannotLink.cannot_link_speaker_id.in_(removed + others),
            )
            .all()
        )
        pairs = {(int(a), int(b)) for a, b in existing}
        for s in removed:
            for o in others:
                for a, b in ((s, o), (o, s)):
                    if (a, b) not in pairs:
                        pairs.add((a, b))
                        db.add(
                            SpeakerCannotLink(speaker_id=a, cannot_link_speaker_id=b, reason=reason)
                        )
    if profile_id is not None:
        blocked = blacklisted_profile_ids(db, removed)
        for s in removed:
            if int(profile_id) not in blocked[s]:
                db.add(SpeakerProfileBlacklist(speaker_id=s, profile_id=profile_id, reason=reason))
    db.flush()


def lift_profile_blacklist(db: Session, speaker_id: int, profile_id: int) -> None:
    """The user explicitly named this one speaker as this profile: drop the correction.

    Removes its blacklist row for the profile and its cannot-link rows with the speakers
    linked to the profile. Only explicit single-speaker actions call this (typing the
    name, assigning or accepting that profile for that speaker); every automatic and
    bulk path honours the blacklist instead. Adds to the session; the caller commits.
    """
    from sqlalchemy import and_
    from sqlalchemy import or_

    from app.models.media import Speaker
    from app.models.media import SpeakerCannotLink
    from app.models.media import SpeakerProfileBlacklist

    sid, pid = int(speaker_id), int(profile_id)
    db.query(SpeakerProfileBlacklist).filter(
        SpeakerProfileBlacklist.speaker_id == sid, SpeakerProfileBlacklist.profile_id == pid
    ).delete(synchronize_session=False)
    members = [
        int(r[0])
        for r in db.query(Speaker.id).filter(Speaker.profile_id == pid, Speaker.id != sid).all()
    ]
    if members:
        db.query(SpeakerCannotLink).filter(
            or_(
                and_(
                    SpeakerCannotLink.speaker_id == sid,
                    SpeakerCannotLink.cannot_link_speaker_id.in_(members),
                ),
                and_(
                    SpeakerCannotLink.speaker_id.in_(members),
                    SpeakerCannotLink.cannot_link_speaker_id == sid,
                ),
            )
        ).delete(synchronize_session=False)
    db.flush()


def carry_blacklist(db: Session, source_id: int, target_id: int) -> None:
    """A merge folds the source's voice into the target, so its corrections come along.

    Without this the merged-away section, which the user said is not Bob, would sit in a
    target nothing forbids from becoming Bob. A profile the target is already linked to
    is not copied: the user merged into it, which is the explicit statement.
    """
    from app.models.media import Speaker

    src, tgt = int(source_id), int(target_id)
    target_profile = db.query(Speaker.profile_id).filter(Speaker.id == tgt).scalar()
    for profile_id in sorted(blacklisted_profile_ids(db, [src])[src]):
        if target_profile is None or int(target_profile) != profile_id:
            record_unassignment(
                db, [tgt], against_speaker_ids=[], profile_id=profile_id, reason=REASON_MERGED
            )
    partners = cannot_link_partners(db, [src])[src] - {tgt}
    if partners:
        record_unassignment(
            db, [tgt], against_speaker_ids=partners, profile_id=None, reason=REASON_MERGED
        )


def unlink_from_profile(
    db: Session,
    speakers: Iterable[Speaker],
    profile_id: int,
    tracker: SpeakerRenameTracker,
) -> set[int]:
    """Take a profile's identity back from these speakers. Returns the ids it unlinked.

    The rule the profile delete established: the link goes, ``verified`` goes, and a
    display name or suggestion that IS the profile's name goes with it (it came from the
    profile), while a name the user set to something else survives. The chunk plane is
    told through ``tracker``, which the caller flushes after its commit. A speaker linked
    to a different profile is not touched.
    """
    from app.models.media import SpeakerProfile
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    name = db.query(SpeakerProfile.name).filter(SpeakerProfile.id == int(profile_id)).scalar()
    unlinked: set[int] = set()
    for speaker in speakers:
        if speaker.profile_id is None or int(speaker.profile_id) != int(profile_id):
            continue
        indexed_as = canonical_speaker_label_for_row(speaker)
        speaker.profile_id = None  # type: ignore[assignment]
        speaker.verified = False  # type: ignore[assignment]
        if name is not None and speaker.display_name == name:
            speaker.display_name = None  # type: ignore[assignment]
        if name is not None and speaker.suggested_name == name:
            speaker.suggested_name = None  # type: ignore[assignment]
            speaker.confidence = None  # type: ignore[assignment]
        tracker.record(
            int(speaker.media_file_id), indexed_as, canonical_speaker_label_for_row(speaker)
        )
        unlinked.add(int(speaker.id))
    return unlinked


def join_cluster_identity(
    db: Session, speaker: Speaker, cluster: SpeakerCluster, tracker: SpeakerRenameTracker
) -> set[int]:
    """Joining a promoted cluster links its profile and takes its name.

    The one way every assignment path propagates a cluster's identity. Refuses (returns
    an empty set, writes nothing) when the speaker is blocked from the profile.

    Returns:
        The profiles whose membership changed (the new one and any it left), for the
        caller's :class:`SpeakerImpact`; empty when nothing changed.
    """
    from app.models.media import SpeakerProfile
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    profile_id = cluster.promoted_to_profile_id
    if profile_id is None or speaker_blocked_from_profile(db, int(speaker.id), int(profile_id)):
        return set()
    name = db.query(SpeakerProfile.name).filter(SpeakerProfile.id == int(profile_id)).scalar()
    if name is None:
        return set()
    old = int(speaker.profile_id) if speaker.profile_id is not None else None
    indexed_as = canonical_speaker_label_for_row(speaker)
    speaker.profile_id = int(profile_id)  # type: ignore[assignment]
    speaker.display_name = name  # type: ignore[assignment]
    speaker.verified = True  # type: ignore[assignment]
    tracker.record(int(speaker.media_file_id), indexed_as, canonical_speaker_label_for_row(speaker))
    return {p for p in (old, int(profile_id)) if p is not None}


def detach_from_cluster(
    db: Session,
    cluster: SpeakerCluster,
    speakers: list[Speaker],
    *,
    blacklist: bool,
    tracker: SpeakerRenameTracker,
) -> SpeakerImpact:
    """Unassign speakers from a cluster: that section is no longer that person.

    Removes both membership representations, unlinks the cluster's promoted profile
    (:func:`unlink_from_profile`) and, when ``blacklist`` is set, records the speakers
    as cannot-link against every remaining member and as blacklisted from the profile.
    The cluster and its profile remain for the other members; the caller commits, flushes
    ``tracker`` and hands the returned impact to :func:`reconcile_speaker_impact`, which
    rebuilds the centroid and the profile embedding (an emptied cluster is deleted).
    """
    from app.models.media import SpeakerClusterMember

    remove_ids = sorted({int(s.id) for s in speakers})
    remaining = [
        int(r[0])
        for r in db.query(SpeakerClusterMember.speaker_id)
        .filter(
            SpeakerClusterMember.cluster_id == cluster.id,
            SpeakerClusterMember.speaker_id.notin_(remove_ids),
        )
        .all()
    ]
    impact = SpeakerImpact(owner_ids={int(cluster.user_id)}, cluster_uuids={str(cluster.uuid)})
    db.query(SpeakerClusterMember).filter(
        SpeakerClusterMember.cluster_id == cluster.id,
        SpeakerClusterMember.speaker_id.in_(remove_ids),
    ).delete(synchronize_session="fetch")
    for speaker in speakers:
        if speaker.cluster_id is not None and int(speaker.cluster_id) == int(cluster.id):
            speaker.cluster_id = None  # type: ignore[assignment]

    profile_id = (
        int(cluster.promoted_to_profile_id) if cluster.promoted_to_profile_id is not None else None
    )
    if profile_id is not None:
        unlinked = unlink_from_profile(db, speakers, profile_id, tracker)
        if unlinked:
            impact.profile_ids.add(profile_id)
            impact.relabel_speaker_ids |= unlinked
    if blacklist:
        record_unassignment(db, remove_ids, against_speaker_ids=remaining, profile_id=profile_id)
    cluster.member_count = len(remaining)  # type: ignore[assignment]
    return impact
