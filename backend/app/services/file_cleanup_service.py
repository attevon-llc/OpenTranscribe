"""Canonical destroy for a media file: :func:`purge_media_file`.

Every delete path (interactive, bulk, retention, orphan cleanup, GDPR erasure) goes through
:func:`purge_media_file`: the legal-hold refusal, then the three-phase destroy (read a
plain-data plan, destroy the object-storage and OpenSearch copies with no transaction open,
then delete the row and scrub chat citations), then the speaker-aggregate refresh. The
OpenSearch erasers live in ``opensearch_erasure``, the account-scoped destroy in
``account_purge`` and the periodic stuck-file cleanup in ``file_cleanup_cycle``.
"""

import logging
from typing import TYPE_CHECKING
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import ColumnElement
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.services.hold_guard import LEGAL_HOLD_REFUSAL
from app.services.hold_guard import hold_state
from app.services.opensearch_erasure import _cleanup_opensearch_for_file
from app.services.storage_versions import UNVERIFIED_VERSIONS_ERROR

if TYPE_CHECKING:
    from app.services.speaker_cleanup import SpeakerImpact

logger = logging.getLogger(__name__)


def delete_file_storage_artifacts(
    file_id: int, artifacts: dict[str, Any], unverified: list[str] | None = None
) -> bool:
    """Delete every object-storage artifact for a media file.

    Covers the original, its thumbnail, its playback rendition, and the regenerable
    derived cache
    (subtitle-embedded videos + extracted audio under ``processed-videos/derived/``).
    Single source of truth shared by the interactive delete endpoint and the
    retention/auto-delete path so neither can orphan storage. Best-effort per
    artifact — a failure on one never blocks the rest.

    **Takes no DB session.** It used to take the caller's, purely to hand it to
    ``VideoProcessingService.clear_derived_cache`` (which takes no session),
    which put up to seven MinIO round trips inside the caller's transaction.
    :func:`_load_purge_plan` reads the filename instead.

    A missing object is NOT a failure: S3/MinIO deletes are idempotent and do
    not raise for an absent key, so anything that does raise is a real one.

    Args:
        file_id: Internal media file id — the derived-cache keys are keyed on it.
        artifacts: Plain values read in the caller's DB phase —
            ``filename``, ``storage_path``, ``thumbnail_path`` and ``playback_path``.
        unverified: Out-list. Each artifact whose delete ran but whose old object versions
            could not be checked for (``GetBucketVersioning`` / ``ListBucketVersions``
            refused) is appended, so the caller can record an erasure as unverified rather
            than complete. Those artifacts do not make the return value False.

    Returns:
        True when every artifact this file has was deleted or was already
        absent; False when at least one may still be in object storage.
        ``purge_media_file`` turns a False into a residual error so a GDPR
        erasure that left the media itself behind cannot audit as complete —
        which is why this reports "all clear" rather than the narrower "the
        original was deleted" it used to. The per-artifact detail is logged.
    """
    from app.services.minio_service import delete_file

    all_deleted = True
    for path_key in ("storage_path", "thumbnail_path", "playback_path"):
        path = artifacts.get(path_key)
        if not path:
            continue
        try:
            removed = delete_file(str(path))
            logger.info(f"Deleted MinIO object {path}")
            if getattr(removed, "unverified", False) and unverified is not None:
                unverified.append(path_key)
        except Exception as minio_err:  # one artifact never blocks the rest
            all_deleted = False
            logger.warning(f"Could not delete MinIO object {path}: {minio_err}", exc_info=True)

    # Regenerable derived cache — duplicates that must not outlive the original.
    # "Regenerable" describes how they were made, not what they hold: these are
    # subtitle-burned video and extracted audio, i.e. the media itself.
    filename = artifacts.get("filename")
    if filename:
        try:
            from app.services.minio_bucket_admin import MinIOService
            from app.services.video_processing_service import VideoProcessingService

            cleared = VideoProcessingService(MinIOService()).clear_derived_cache(
                int(file_id), str(filename)
            )
            if getattr(cleared, "unverified", False) and unverified is not None:
                unverified.append("derived_cache")
        except Exception as cache_err:
            all_deleted = False
            logger.warning(
                f"Derived-cache cleanup failed for file {file_id}: {cache_err}", exc_info=True
            )

    return all_deleted


def _refresh_surviving_centroids(
    db: Session, owner_id: int, plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """Bring every cluster and profile the purged file's speakers belonged to in line.

    A centroid is the mean of its members' voiceprints and a profile embedding the mean
    of its linked speakers', so after the file's speakers are gone both still average in
    the deleted voices (issue #1211; profiles since #1214). This hands the clusters and
    profiles read into the plan BEFORE the row delete to
    ``speaker_cleanup.reconcile_speaker_impact`` — the one implementation every speaker
    removal path shares: an emptied cluster (promoted or not) is deleted with its
    centroid, any other is recomputed from its remaining members, a profile's embedding
    is recomputed from its remaining members, and whatever cannot be rebuilt is erased
    rather than left stale. Scoped to the file owner's own clusters.

    Returns:
        One ``{"stage", "file_uuid", "error"}`` dict per centroid or profile embedding
        that may still carry the deleted file's contribution. Never raises.
    """
    from app.services.speaker_cleanup import SpeakerImpact
    from app.services.speaker_cleanup import reconcile_speaker_impact

    file_uuid = plan["file_uuid"]
    residual: list[dict[str, Any]] = []
    if plan.get("cluster_read_failed"):
        residual.append(
            {
                "stage": "clusters",
                "file_uuid": file_uuid,
                "error": "could not enumerate the file's clusters",
            }
        )
    impact = SpeakerImpact(
        owner_ids={int(owner_id)},
        cluster_uuids=set(plan.get("cluster_uuids") or []),
        profile_ids=set(plan.get("profile_ids") or []),
    )
    for entry in reconcile_speaker_impact(db, impact):
        logger.warning(f"Centroid refresh incomplete for file {file_uuid}: {entry}")
        residual.append({**entry, "file_uuid": file_uuid})
    return residual


def scrub_chat_citations(db: Session, file_uuids: list[str]) -> int:
    """Drop every persisted chat citation that names one of these files. Does not commit.

    A citation stores a verbatim ``snippet`` of the transcript it quotes (unmasked
    whenever the answering model was local — see ``chat/citations.build_citation``),
    plus the file's title and UUID, in ``chat_message.citations``: a copy of the
    recording's text in Postgres with **no foreign key back to the file**, so no
    cascade reaches it. Deleting the file used to leave every such quote readable
    in the conversation history and its export, in the deleting user's chats and in
    every other user's chat that cited a shared file.

    The WHOLE entry goes, not just ``snippet`` — the same decision
    ``api/endpoints/chat/citation_takedown.py`` records for quarantine: a title and a
    link still name the recording. ``message.content`` (the assistant's own prose) is
    left alone for the reason recorded there too; its ``[n]`` marker dangles.

    Conversation SCOPES (``chat_conversation.context`` / ``chat_project.scope``) are
    deliberately NOT touched: they hold UUIDs, not content, and an emptied scope means
    "every transcript I can access" — removing the last UUID would widen retrieval.

    Args:
        db: The caller's session; the UPDATE joins its transaction.
        file_uuids: UUIDs of the files being destroyed.

    Returns:
        Number of chat messages rewritten.
    """
    if not file_uuids:
        return 0
    from sqlalchemy import or_
    from sqlalchemy import select

    from app.models.chat import ChatMessage

    # Probe first, through the GIN index (JSONB ``@>`` via ``.contains``): the UPDATE's EXISTS
    # scanned every message and matched nothing on 54 of 54 profiled calls (#1337). One OR arm
    # per file, so a big batch is chunked rather than building an unbounded statement.
    rewritten = 0
    for start in range(0, len(file_uuids), _CITATION_PROBE_CHUNK):
        chunk = [str(u) for u in file_uuids[start : start + _CITATION_PROBE_CHUNK]]
        probe = (
            select(ChatMessage.id)
            .where(or_(*(ChatMessage.citations.contains([{"file_uuid": u}]) for u in chunk)))
            .limit(1)
        )
        if db.execute(probe).first():
            rewritten += _rewrite_citations(db, chunk)
    return rewritten


#: File uuids per probe statement.
_CITATION_PROBE_CHUNK = 200


def _rewrite_citations(db: Session, file_uuids: list[str]) -> int:
    """The set-based UPDATE behind :func:`scrub_chat_citations`; only called once a probe hit."""
    from sqlalchemy import text

    result = db.execute(
        text(
            """
            UPDATE chat_message SET citations = (
                SELECT COALESCE(jsonb_agg(c.value ORDER BY c.ordinality), '[]'::jsonb)
                FROM jsonb_array_elements(chat_message.citations) WITH ORDINALITY AS c
                WHERE (c.value ->> 'file_uuid') IS NULL
                   OR NOT ((c.value ->> 'file_uuid') = ANY(:uuids))
            )
            WHERE jsonb_typeof(citations) = 'array'
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(chat_message.citations) AS e
                WHERE (e.value ->> 'file_uuid') = ANY(:uuids)
              )
            """
        ),
        {"uuids": [str(u) for u in file_uuids]},
    )
    return int(getattr(result, "rowcount", 0) or 0)


def scrub_orphaned_citations(db: Session) -> int:
    """Drop every chat citation whose file no longer exists. Does not commit.

    The recovery half of the batched scrub (:func:`scrub_deferred_citations`): a batch
    caller scrubs AFTER its row deletes, so a process killed in between leaves citations
    naming destroyed files. This pass, run by the daily ``chat.retention_sweep``, finds
    them by the absence of the ``media_file`` row (trashed files keep their row, so their
    citations stay). Idempotent; a set-based pass, with the probe on ``media_file.uuid``.

    Returns:
        Number of chat messages rewritten.
    """
    from sqlalchemy import text

    # A malformed file_uuid casts to NULL and is left alone: only a well-formed UUID with no
    # media_file row is an orphan.
    result = db.execute(
        text(
            """
            UPDATE chat_message SET citations = (
                SELECT COALESCE(jsonb_agg(c.value ORDER BY c.ordinality), '[]'::jsonb)
                FROM jsonb_array_elements(chat_message.citations) WITH ORDINALITY AS c
                WHERE CASE WHEN (c.value ->> 'file_uuid') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN (c.value ->> 'file_uuid')::uuid END IS NULL
                   OR EXISTS (SELECT 1 FROM media_file m WHERE m.uuid = CASE WHEN (c.value ->> 'file_uuid') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN (c.value ->> 'file_uuid')::uuid END)
            )
            WHERE jsonb_typeof(citations) = 'array'
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(chat_message.citations) AS e
                WHERE CASE WHEN (e.value ->> 'file_uuid') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN (e.value ->> 'file_uuid')::uuid END IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM media_file m WHERE m.uuid = CASE WHEN (e.value ->> 'file_uuid') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN (e.value ->> 'file_uuid')::uuid END)
              )
            """
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _load_purge_plan(db: Session, file: MediaFile) -> dict[str, Any]:
    """Read everything the external destroy needs, then hand back PLAIN DATA.

    No ORM instance leaves this function. ``file.speakers`` in particular is a
    lazy relationship: reading it from the OpenSearch phase reopened a
    transaction in the middle of half a dozen cluster round trips, which is the
    exact back door the three-phase rule exists to close.

    Args:
        db: The caller's session, used only for the reads below.
        file: The media file about to be destroyed.

    Returns:
        ``file_id``, ``file_uuid``, ``owner_id``, ``filename``,
        ``storage_path``, ``thumbnail_path``, ``playback_path``, ``speaker_uuids`` and — when the
        speaker enumeration itself failed — ``speaker_read_error``; plus the
        ``cluster_uuids`` and ``profile_ids`` the file's speakers belong to (or
        ``cluster_read_failed``), read here because the CASCADE erases that record.
    """
    plan: dict[str, Any] = {
        "file_id": int(file.id),
        "file_uuid": str(file.uuid),
        "owner_id": int(file.user_id),
        "filename": str(file.filename) if file.filename else None,
        "storage_path": str(file.storage_path) if file.storage_path else None,
        "thumbnail_path": str(file.thumbnail_path) if file.thumbnail_path else None,
        "playback_path": str(file.playback_path) if file.playback_path else None,
        "speaker_uuids": [],
        "speaker_read_error": None,
        "cluster_uuids": [],
        "profile_ids": [],
        "cluster_read_failed": False,
    }
    try:
        plan["speaker_uuids"] = [str(speaker.uuid) for speaker in list(file.speakers)]
    except SQLAlchemyError as e:  # a failed load is itself a miss
        logger.warning("Could not load the speakers of file %s", plan["file_uuid"], exc_info=True)
        plan["speaker_read_error"] = str(e)
    try:
        impact = _speaker_impact_of_file(db, file)
        plan["cluster_uuids"] = sorted(impact.cluster_uuids)
        plan["profile_ids"] = sorted(impact.profile_ids)
    except Exception:  # a failed load is itself a miss
        logger.warning(
            "Could not enumerate the clusters holding file %s's speakers",
            plan["file_uuid"],
            exc_info=True,
        )
        plan["cluster_read_failed"] = True
    return plan


def _speaker_impact_of_file(db: Session, file: MediaFile) -> "SpeakerImpact":
    """The clusters and profiles that count one of this file's speakers as a member.

    Must be read BEFORE the row delete: ``ON DELETE CASCADE`` removes the membership rows
    with the speakers, after which nothing records which centroids and profile embeddings
    averaged them in. Read by ``speaker_cleanup.capture_speaker_impact``, the same capture
    every per-speaker removal path uses; column-only, so no ORM instance escapes the plan.
    """
    from app.models.speaker import Speaker
    from app.services.speaker_cleanup import capture_speaker_impact

    speaker_ids = [
        int(row[0]) for row in db.query(Speaker.id).filter(Speaker.media_file_id == file.id)
    ]
    return capture_speaker_impact(db, speaker_ids, removed=False, reindex=False)


def _purge_external_copies(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Destroy every copy of a file that lives outside Postgres.

    Object storage first, then OpenSearch. **Takes no session and opens none**,
    so the caller's transaction is closed for the whole of it.

    Args:
        plan: The plain-data plan from :func:`_load_purge_plan`.

    Returns:
        One ``{"stage", "file_uuid", "error"}`` dict per store whose documents
        or objects may have survived. Empty means every store confirmed them gone.
    """
    file_uuid = plan["file_uuid"]
    residual: list[dict[str, Any]] = []

    unverified: list[str] = []
    storage_ok = delete_file_storage_artifacts(
        plan["file_id"],
        {
            "filename": plan["filename"],
            "storage_path": plan["storage_path"],
            "thumbnail_path": plan["thumbnail_path"],
            "playback_path": plan.get("playback_path"),
        },
        unverified,
    )
    if not storage_ok:
        residual.append(
            {
                "stage": "storage",
                "file_uuid": file_uuid,
                "error": "one or more object-storage artifacts could not be deleted",
            }
        )

    if unverified:
        residual.append(
            {
                "stage": "storage_unverified",
                "file_uuid": file_uuid,
                "error": UNVERIFIED_VERSIONS_ERROR,
            }
        )

    residual.extend(_cleanup_opensearch_for_file(plan, file_uuid) or [])
    _clear_reindex_coordination_keys(file_uuid)
    return residual


def _clear_reindex_coordination_keys(file_uuid: str) -> None:
    """Drop the Redis keys the search sweep and reindex dispatcher claim for a file.

    Both are TTL'd claims, but a delete inside the window left them behind, named after a
    file that no longer exists. Best effort: they expire on their own, so an unreachable
    Redis must not turn a finished delete into a failure.
    """
    try:
        from app.core.redis import get_request_redis
        from app.services.search.reindex_dispatch import reindex_coordination_keys

        get_request_redis().delete(*reindex_coordination_keys(file_uuid))
    except (RedisError, OSError) as err:
        logger.debug(f"purge {file_uuid}: reindex coordination keys not cleared: {err}")


#: ``error`` of a purge stopped at the row delete because the file stopped being destroyable
#: while its external copies were being destroyed (a hold landed, or it left ``eligible``).
PURGE_ABORTED_AFTER_DESTROY = "Purge stopped before the row delete; the file is kept"


def _lock_and_recheck(
    db: Session, file_id: int, eligible: ColumnElement[bool] | None
) -> tuple[str, Any] | None:
    """Row-lock ``file_id`` ``FOR UPDATE`` and say why it must not be destroyed now, else None.

    ``("gone", None)``, ``("held", HoldState)`` or ``("ineligible", None)``. The lock lasts until
    the caller's next commit or rollback (PostgreSQL row locks end with the transaction). It is
    what makes the answer hold until then: placing a hold writes ``media_file.legal_hold`` or
    inserts a ``content_hold_file`` row, whose foreign key check needs ``FOR KEY SHARE`` on
    this row, and both conflict with ``FOR UPDATE``.
    """
    locked = db.execute(
        select(MediaFile.id).where(MediaFile.id == file_id).with_for_update(of=MediaFile)
    ).first()
    if locked is None:
        return "gone", None
    hold = hold_state(db, file_id)
    if hold.held:
        return "held", hold
    if eligible is not None and (
        db.execute(select(MediaFile.id).where(MediaFile.id == file_id, eligible)).first() is None
    ):
        return "ineligible", None
    return None


def purge_media_file(
    db: Session,
    file: MediaFile,
    *,
    defer_citation_scrub: list[str] | None = None,
    eligible: ColumnElement[bool] | None = None,
) -> dict:
    """Canonical destroy for a MediaFile and ALL associated data.

    Single source of truth shared by every delete path — interactive single/force
    delete, bulk delete, N-day retention, and orphan cleanup — so a change here applies
    everywhere and no path can drift or leak. Steps (each best-effort, DB delete is the
    commit point):

    1. Object storage: original + thumbnail + playback rendition + regenerable derived cache.
    2. OpenSearch: speaker embeddings (v3+v4), transcript doc, transcript chunks, summaries.
    3. Database row (CASCADE removes child rows), plus every chat citation quoting the
       file (:func:`scrub_chat_citations` — no foreign key reaches those).
    4. Redis caches for the owner.
    5. Speaker clusters and profiles the file's speakers belonged to
       (``speaker_cleanup``): a cluster the CASCADE emptied is deleted with its centroid,
       any other cluster and every such profile has its embedding recomputed from the
       remaining members, and an aggregate that cannot be rebuilt is erased.

    ``defer_citation_scrub``: a batch caller passes a shared list; the file's UUID is
    appended after the row delete instead of scrubbing per file, and the caller makes
    one :func:`scrub_deferred_citations` call for the whole batch.

    SpeakerProfile records are intentionally preserved; their embeddings no longer
    average in the deleted file's voices.

    **A file under an active legal hold is REFUSED before step 1.** A hold is either the
    platform's (``media_file.legal_hold``) or an organization's (an active ``content_hold``);
    ``hold_guard.hold_state`` answers for both. The hold is
    the source of truth that keeps DMCA/litigation evidence alive (the S3 object
    lock only mirrors it, best-effort), and this destroy is irreversible, so the
    check lives here rather than only in each caller's query: a guard in one
    query protects one caller, and this function has four. It fails closed — the
    refusal is logged at ERROR and reported as ``deleted: False`` with
    ``refused_legal_hold: True`` (and ``hold_state``, the :class:`~app.services.hold_guard.HoldState`
    an endpoint turns into the 409 body), never as a quiet no-op that a caller reading
    only ``deleted`` could mistake for a completed destroy. Clear the hold
    (``takedown_service.release_file``) to make a held file deletable; there is
    deliberately no override argument, because an argument that skips the guard
    is the guard's own failure mode.

    **The check runs twice, each time under a ``FOR UPDATE`` row lock**
    (:func:`_lock_and_recheck`): before the external copies are touched, and again right
    before the row delete. The lock cannot span phase 2 (see "Session lifetime" below), so a
    hold placed while storage and OpenSearch are being destroyed is only seen by the second
    check, and ``content_hold_file`` is ``ON DELETE CASCADE``: without it the row delete
    silently took the new hold's link, the transcript, the speakers and the summary with it.
    When the second check finds the file held, the row is NOT deleted: the result is
    ``deleted: False``, ``refused_legal_hold: True``, ``hold_state`` and
    ``aborted_after_destroy: True``, logged at ERROR. The hold then keeps everything Postgres
    still has; the object-storage and search copies may already be gone, which the flag says.

    ``eligible`` (optional) is the caller's own selection predicate on ``MediaFile``
    (``trash_purge`` passes its ``_due()``). Both checks also require it, so a file that left
    the caller's selection mid-destroy (quarantined, for the trash purge) is kept the same way,
    with ``aborted_after_destroy: True`` but no ``refused_legal_hold``.

    ``is_quarantined`` is deliberately **not** part of this check. Quarantine is
    a review state an admin can legitimately end by deleting the offending
    upload; it is the unattended retention sweep that must never destroy a file
    under review, and that predicate lives on the sweep's query
    (``tasks/cleanup._select_expired_files``).

    Returns ``{"deleted": bool, "file_uuid": str, "error": str | None,
    "residual_errors": list[dict]}``, plus ``refused_legal_hold: True`` on the
    refusal above. Never raises.

    ``deleted`` reports the **database row** only. ``residual_errors`` is
    non-empty when a copy of the file's data may still exist in object storage
    or OpenSearch, and steps 1 and 2 are best-effort in the sense that one
    failing store does not stop the others — NOT in the sense that their
    failures are invisible. A caller that treats ``deleted: True`` as "every
    copy is gone" is wrong: the GDPR erasure path must surface
    ``residual_errors`` so an incomplete erasure audits as PARTIAL.

    **Session lifetime.** Three phases: read the plan, destroy the external
    copies with **no transaction open**, then delete the row. Steps 1 and 2 used
    to run on the caller's live session — a request session in the interactive
    and GDPR paths — so a MinIO outage or a slow OpenSearch cluster held
    ``ACCESS SHARE`` on ``media_file`` for the whole destroy. The ``db.commit()``
    between phases 1 and 2 is what actually ends that transaction; it is safe
    because this function already commits (the row delete below), so no caller
    can be relying on it to leave work pending.
    """
    file_uuid = str(file.uuid)
    file_id = int(file.id)
    residual: list[dict[str, Any]] = []

    try:
        # Phase 1 — check under the row lock, then read (Postgres only).
        stop = _lock_and_recheck(db, file_id, eligible)
        if stop is not None:
            # No rollback: a refusal before anything was destroyed leaves the caller's
            # transaction as it was, as it always has.
            return _stopped(file_uuid, stop, residual, after_destroy=False)
        plan = _load_purge_plan(db, file)
        owner_id = plan["owner_id"]
        db.commit()

        # Phase 2 — object storage + OpenSearch. NO transaction is held here.
        residual.extend(_purge_external_copies(plan))

        # Phase 3 — write, after the same check under a fresh row lock (docstring). The
        # citation scrub rides the row delete's transaction, so a failed delete never
        # leaves the history scrubbed for a file that still exists.
        stop = _lock_and_recheck(db, file_id, eligible)
        if stop is not None:
            # Nothing was written since phase 1's commit: ending the transaction only
            # releases the row lock.
            db.commit()
            return _stopped(file_uuid, stop, residual, after_destroy=True)
        if defer_citation_scrub is None:
            scrub_chat_citations(db, [file_uuid])
        db.delete(file)
        db.commit()
        logger.info(f"purge_media_file: deleted file {file_uuid} from database")
        if defer_citation_scrub is not None:
            defer_citation_scrub.append(file_uuid)

        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_all_for_user(owner_id)
        except RedisError:
            logger.warning(
                "purge_media_file: cache invalidation failed (non-critical)", exc_info=True
            )

        residual.extend(_refresh_surviving_centroids(db, owner_id, plan))

        return {
            "deleted": True,
            "file_uuid": file_uuid,
            "error": None,
            "residual_errors": residual,
        }

    except Exception as e:
        db.rollback()
        # "error" is rendered verbatim into an HTTPException detail by
        # files/crud.py's caller -- never interpolate the raw exception text,
        # only its class name. The real cause is still diagnosable via the log.
        logger.exception(f"purge_media_file: failed to delete file {file_uuid}")
        return {
            "deleted": False,
            "file_uuid": file_uuid,
            "error": f"Delete failed ({type(e).__name__})",
            "residual_errors": residual,
        }


def _stopped(
    file_uuid: str, stop: tuple[str, Any], residual: list[dict[str, Any]], *, after_destroy: bool
) -> dict:
    """The ``purge_media_file`` result for a file one of its row-locked checks kept."""
    reason, hold = stop
    result: dict[str, Any] = {"deleted": False, "file_uuid": file_uuid, "residual_errors": residual}
    if after_destroy:
        result["aborted_after_destroy"] = True
    if reason == "held":
        logger.error(
            f"purge_media_file: REFUSED to delete file {file_uuid} — it is under an "
            f"active legal hold ({', '.join(hold.held_by)}). Release the hold before deleting."
            + (
                " The hold landed after its external copies were destroyed."
                if after_destroy
                else ""
            )
        )
        result.update(error=LEGAL_HOLD_REFUSAL, refused_legal_hold=True, hold_state=hold)
    elif reason == "gone":
        logger.warning(f"purge_media_file: file {file_uuid} was already deleted")
        result["error"] = "File already deleted"
    else:
        logger.error(f"purge_media_file: file {file_uuid} no longer matches the purge selection")
        result["error"] = PURGE_ABORTED_AFTER_DESTROY if after_destroy else "File not eligible"
    return result


def auto_delete_media_file(
    db: Session, file: MediaFile, *, defer_citation_scrub: list[str] | None = None
) -> dict:
    """Backwards-compatible alias for the canonical :func:`purge_media_file`."""
    return purge_media_file(db, file, defer_citation_scrub=defer_citation_scrub)


def scrub_deferred_citations(file_uuids: list[str]) -> int:
    """Run the one set-based citation scrub for a whole purge batch, in its own session.

    Callers that purge many files pass a shared list as ``defer_citation_scrub`` and call
    this once afterwards (in a ``finally``), so the ``chat_message`` scan happens once per
    batch instead of once per destroyed file (issue #1337, V7). The window this opens
    is a process kill between the last row delete and this call; the per-file path closed
    it by scrubbing inside the delete's transaction.
    """
    if not file_uuids:
        return 0
    from app.db.session_utils import session_scope

    with session_scope() as db:
        return scrub_chat_citations(db, file_uuids)
