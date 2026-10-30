"""Speaker CRUD endpoints (create / get / update / delete) and sub-router wiring."""

import logging
import uuid

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints import speakers_debug
from app.api.endpoints import speakers_merge
from app.api.endpoints import speakers_occurrences
from app.api.endpoints import speakers_suggestions
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.speakers_list import list_speakers
from app.api.endpoints.speakers_profile_workflow import _get_media_file_uuid
from app.api.endpoints.speakers_profile_workflow import _handle_profile_action
from app.db.base import get_db
from app.models.speaker import Speaker
from app.models.user import User
from app.schemas.speaker import Speaker as SpeakerSchema
from app.schemas.speaker import SpeakerUpdate
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.request_context import RequestContext
from app.services.speaker_cleanup import capture_speaker_impact
from app.services.speaker_cleanup import raise_if_incomplete
from app.services.speaker_cleanup import reconcile_speaker_impact
from app.services.speaker_provenance_view import attach_speaker_provenance
from app.services.speaker_status_service import SpeakerStatusService
from app.services.uuid_helpers import require_speaker_access
from app.utils.speaker_identity_detail import ORIGIN_CONFIRMED
from app.utils.speaker_identity_detail import ORIGIN_CREATED
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_CREATED
from app.utils.speaker_identity_detail import ORIGIN_TYPED
from app.utils.speaker_identity_source import after_name_cleared
from app.utils.speaker_identity_source import clear_suggestion
from app.utils.speaker_identity_source import record_human_identity
from app.utils.speaker_labels import canonical_speaker_label_for_row
from app.utils.speaker_labels import is_assigned_speaker_name

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

router = APIRouter()


# Whitelist of fields that can be updated via the speaker update endpoint
SPEAKER_UPDATABLE_FIELDS = {"name", "display_name", "suggested_name", "verified"}


# =============================================================================
# STATIC ROUTES FIRST - These must come before parameterized routes
# =============================================================================


@router.post("", response_model=SpeakerSchema)
def create_speaker(
    speaker: SpeakerUpdate,
    media_file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Create a new speaker for a specific media file
    """
    from app.services.uuid_helpers import get_file_by_uuid_with_permission

    # Get media file by UUID and verify permission (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db,
        media_file_uuid,
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        min_permission="editor",
    )

    # Generate a UUID for the new speaker
    speaker_uuid = str(uuid.uuid4())

    new_speaker = Speaker(
        name=speaker.name,
        uuid=speaker_uuid,
        user_id=current_user.id,
        organization_id=media_file.organization_id,
        media_file_id=media_file.id,  # Use internal integer ID
        verified=speaker.verified if speaker.verified is not None else False,
    )

    # If display_name is provided, mark as verified. A blank one names no one, so it is not
    # stored: a name is always stored with the record of the person who gave it.
    if speaker.display_name and speaker.display_name.strip():
        new_speaker.display_name = speaker.display_name  # type: ignore[assignment]
        new_speaker.verified = True  # type: ignore[assignment]
        record_human_identity(new_speaker, user_id=current_user.id, origin=ORIGIN_CREATED)

    db.add(new_speaker)
    db.commit()
    db.refresh(new_speaker)

    # Add computed status fields
    attach_speaker_provenance(db, [new_speaker], ctx)
    SpeakerStatusService.add_computed_status(new_speaker)

    return new_speaker


# Registered here rather than on a sub-router: an empty path cannot be included with an
# empty prefix.
router.get("", response_model=None)(list_speakers)


# =============================================================================
# ROUTE ORDERING IS CRITICAL IN FASTAPI
#
# FastAPI matches routes in definition order. More specific routes must be
# defined BEFORE less specific ones, otherwise the generic routes will
# intercept requests meant for specific nested paths.
#
# CORRECT ORDER:
#   1. Static routes (no parameters): /, /cleanup-orphaned-embeddings, etc.
#   2. Nested parameterized routes: /{uuid}/verify, /{uuid}/cross-media
#   3. Single-param routes (catch-all): /{uuid}
#
# WRONG: /{uuid} before /{uuid}/cross-media
#   -> Request to /speakers/abc/cross-media matches /{uuid} with uuid="abc"
# =============================================================================


# --- SECTION: NESTED PARAMETERIZED ROUTES (must come before single-param routes) ---


# Sub-routers are included here, in the exact original registration order. FastAPI matches
# in definition order: static routes first, then nested parameterized routes, then the
# single-param catch-alls (get/update/delete below). Do not reorder.
router.include_router(speakers_debug.router)
router.include_router(speakers_occurrences.router)
router.include_router(speakers_suggestions.router)
router.include_router(speakers_merge.router)


# --- SECTION: SINGLE-PARAM ROUTES (catch-all, must come last) ---


@router.get("/{speaker_uuid}", response_model=SpeakerSchema)
def get_speaker(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Get details of a specific speaker with computed status
    """
    # Tenant, quarantine and file-level access (own or shared via collection).
    speaker = require_speaker_access(
        db, speaker_uuid, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
    )

    # Privacy policy (#1280): hide what the owner's current switches forbid showing.
    redact_speaker_rows(db, [speaker])
    attach_speaker_provenance(db, [speaker], ctx)
    SpeakerStatusService.add_computed_status(speaker)

    return speaker


def _set_no_cache_headers(response: Response) -> None:
    """Set cache-busting headers on response."""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _apply_verification_on_display_name(speaker: Speaker, speaker_update: SpeakerUpdate) -> None:
    """Mark speaker as verified when display name is set.

    Clears ``suggested_name``/``confidence`` — the *active pending suggestion*
    — so nothing reads a resolved speaker as still carrying one (issue #605).
    ``suggestion_source`` is deliberately left alone: every reader of it
    (``smart_speaker_suggestion_service``, ``speaker_profiles.py``, this
    module's own display-flag builder) already gates on ``suggested_name``/
    ``confidence`` being truthy too, so it never resurrects a "live"
    suggestion — but ``task_detection_service`` reads it standalone as its
    only signal for "has LLM speaker ID already run on this file", and
    clearing it here caused every manually-confirmed speaker's file to look
    unidentified again, re-offering (and re-dispatching) identification the
    user had just finished with (issue #603 follow-up regression).
    """
    if speaker_update.display_name is not None and speaker_update.display_name.strip():
        speaker.verified = True  # type: ignore[assignment]
        clear_suggestion(speaker)


def _record_identity_source_on_edit(
    speaker: Speaker,
    speaker_update: SpeakerUpdate,
    *,
    user_id: int,
    profile_action: str | None,
) -> None:
    """Record who made the speaker's identity after a ``PUT`` (issue #1280).

    Runs BEFORE :func:`_apply_verification_on_display_name`: the record names the suggestion
    the person accepted, which that function then withdraws.

    A typed name, a profile action (which needs a name) or an explicit ``verified: true`` is a
    person's word: ``human``. A cleared name keeps a machine link as ``voiceprint_link`` (it
    stays hideable and purgeable) and leaves nothing to describe when no link remains.
    Called before the chunk label is recomputed, which depends on it.
    """
    if "display_name" in speaker_update.model_fields_set:
        if is_assigned_speaker_name(speaker_update.display_name):
            record_human_identity(
                speaker,
                user_id=user_id,
                origin=ORIGIN_PROFILE_CREATED
                if profile_action == "create_new_profile"
                else ORIGIN_TYPED,
            )
        else:
            after_name_cleared(speaker)
    elif speaker_update.verified is True and (speaker.display_name or speaker.profile_id):
        record_human_identity(speaker, user_id=user_id, origin=ORIGIN_CONFIRMED)


def _propagate_speaker_rename_to_chunks(
    *,
    file_uuid: str | None,
    previous_chunk_name: str,
    new_display_name: str,
    profile_renames: list[tuple[str, str]],
    speaker_id: int | None = None,
) -> None:
    """Queue the chunk-plane rewrite for everything this request renamed.

    Two sources feed it: the speaker the request targeted, and — when the request
    carried ``profile_action="update_profile"`` — every other speaker the profile
    rename swept along, which may sit in other files entirely. Both go through
    ``dispatch_speaker_rename``, which coalesces per file.

    Best-effort by design: a rename that reached Postgres must not 500 because the
    broker was unreachable. The chunk plane stays stale until the next reindex,
    which is exactly the pre-#405 behaviour.

    ``new_display_name`` is the **canonical indexed label**
    (``canonical_speaker_label_for_row``), not the display name. Those differ in
    three reachable cases and all three used to propagate nothing:

    * **A cleared display name.** ``{"display_name": ""}`` is a legal
      ``SpeakerUpdate`` — it is how a user undoes a label. Postgres reverts to
      ``SPEAKER_01`` and a reindex would write ``SPEAKER_01``, but the falsy
      guard here returned early, so the chunk plane kept "Dana" forever and the
      search facet went on offering a name that existed nowhere else.
    * **An edit to ``name`` alone.** ``name`` is updatable too, and for a speaker
      with no ``display_name`` it is what the indexer writes — but dispatch used
      to key solely off ``display_name``.
    * **A confident suggestion with no ``display_name`` at all** (issue #605).
      The indexer writes ``suggested_name`` once ``confidence >= 0.75``; the old
      ``display_name or name`` rule here never looked at either field, so a
      speaker indexed under an LLM suggestion stayed keyed to the raw diarizer
      label and any rename computed the wrong ``old_names``.
    """
    if not new_display_name:
        return
    try:
        from app.tasks.rename_propagation_task import dispatch_speaker_rename

        renames: list[tuple[str | None, str | None]] = list(profile_renames)
        if file_uuid and previous_chunk_name:
            renames.append((file_uuid, previous_chunk_name))
        dispatch_speaker_rename(renames, new_display_name, speaker_id=speaker_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not queue chunk-plane speaker rename propagation: {exc}")


@router.put("/{speaker_uuid}", response_model=SpeakerSchema)
def update_speaker(
    speaker_uuid: str,
    speaker_update: SpeakerUpdate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Update a speaker's information including display name and verification status.

    This endpoint returns immediately after saving to PostgreSQL. Heavy processing
    (profile embeddings, OpenSearch updates, cross-media matching) happens in a
    background Celery task. A WebSocket notification is sent when processing completes.
    """
    from app.tasks.speaker_tasks import process_speaker_update_background

    speaker = require_speaker_access(
        db,
        speaker_uuid,
        current_user,
        organization_id=ctx.org_id,
        min_permission="editor",
        forbidden_detail="Requires editor permission",
        bypass=ctx.bypass,
    )

    speaker_id = speaker.id
    old_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    was_auto_labeled = speaker.suggested_name is not None and not speaker.verified
    media_file_id = int(speaker.media_file_id)
    # The exact string the chunk plane was indexed with — the SAME resolver the
    # chunk-index writers use (issue #605; previously a stale `display_name or
    # name` copy that stopped agreeing once the writers picked up a confident
    # suggestion). Captured here because the overwrite below is the last moment
    # it exists (issue #405).
    previous_chunk_name = canonical_speaker_label_for_row(speaker)
    speaker_file_uuid = _get_media_file_uuid(speaker, db)

    # Update speaker fields
    update_data = speaker_update.model_dump(exclude_unset=True)
    profile_action = update_data.pop("profile_action", None)

    for field, value in update_data.items():
        if field not in SPEAKER_UPDATABLE_FIELDS:
            logger.warning(f"Ignoring non-updatable field in speaker update: {field}")
            continue
        setattr(speaker, field, value)

    # Handle profile actions. Postgres writes only — they shape the immediate
    # response; the OpenSearch replay rides along with the background task below.
    renamed_profile_id, profile_chunk_renames = _handle_profile_action(
        profile_action, speaker_update, speaker, old_profile_id, current_user, db, ctx.bypass
    )

    # Handle verification when display name is set
    _record_identity_source_on_edit(
        speaker, speaker_update, user_id=current_user.id, profile_action=profile_action
    )
    _apply_verification_on_display_name(speaker, speaker_update)

    # Commit to PostgreSQL immediately
    db.commit()
    db.refresh(speaker)

    # Determine what changed
    display_name_changed = speaker_update.display_name is not None
    new_profile_id = int(speaker.profile_id) if speaker.profile_id else None
    display_name = str(speaker.display_name) if speaker.display_name else ""

    # Queue background processing for heavy operations
    # This handles: profile embeddings, OpenSearch updates, retroactive matching, cache clearing
    if display_name_changed or old_profile_id != new_profile_id:
        process_speaker_update_background.delay(
            speaker_uuid=str(speaker.uuid),
            user_id=current_user.id,
            display_name=display_name,
            speaker_id=speaker_id,
            old_profile_id=old_profile_id,
            new_profile_id=new_profile_id,
            was_auto_labeled=was_auto_labeled,
            display_name_changed=display_name_changed,
            media_file_id=media_file_id,
            renamed_profile_id=renamed_profile_id,
        )
        logger.info(f"Queued background processing for speaker {speaker_uuid}")

    # Rewrite the speaker name the transcript chunks were indexed with. Without
    # this, chat's speaker scope (an exact `terms` match on the CURRENT name) and
    # the search facet dropdown keep working off the pre-rename snapshot — see
    # app/tasks/rename_propagation_task.py (issue #405).
    #
    # Keyed on the CANONICAL indexed label (`canonical_speaker_label_for_row`,
    # the SAME resolver `search_indexing_task`/`reindex_task` use), not on
    # `display_name` alone. Keying on `display_name or name` missed three
    # things: a cleared display name, an edit to `name` alone, and — issue
    # #605 — a confident LLM/embedding suggestion the writer would have
    # indexed instead of the raw name. See `_propagate_speaker_rename_to_chunks`.
    #
    # ⚠️ Issue #844: this rewrite is keyword-only — the chunk plane's kNN vector
    # (`embedding`) is left stale on purpose. A rename doesn't change the audio, but
    # it DOES change `embedding_text` (which bakes in the speaker roster), so the
    # vector goes out of sync with what a reindex would produce; re-embedding every
    # chunk of a long recording for a cosmetic rename was judged not worth the cost.
    # Full reasoning is in `rename_propagation_task.propagate_speaker_rename`'s
    # docstring — this is deliberate, not the #666 residue #844 asked to confirm.
    # `test_speaker_rename_leaves_the_chunk_vector_unchanged` pins it.
    new_chunk_name = canonical_speaker_label_for_row(speaker)
    _propagate_speaker_rename_to_chunks(
        file_uuid=speaker_file_uuid,
        previous_chunk_name=previous_chunk_name,
        new_display_name=new_chunk_name,
        profile_renames=profile_chunk_renames,
        speaker_id=speaker_id,
    )

    # Invalidate caches so speaker lists and file data refresh
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_speakers(current_user.id)
        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    # Add computed status for immediate response
    redact_speaker_rows(db, [speaker])
    attach_speaker_provenance(db, [speaker], ctx)
    SpeakerStatusService.add_computed_status(speaker)
    _set_no_cache_headers(response)

    return speaker


@router.delete("/{speaker_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_speaker(
    speaker_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> None:
    """Delete one speaker row from a media file's diarization result.

    Consumed by the file-detail speaker panel. Authorization is broader than
    ownership: the owner or an admin always passes, and a non-owner needs
    ``editor`` on the underlying media file via ``PermissionService`` — a ``viewer``
    on a shared file gets 403, since deleting a speaker rewrites the transcript's
    attribution for everyone the file is shared with.

    This removes the per-file speaker, not the cross-media ``SpeakerProfile`` it may
    be assigned to; the profile and its other occurrences survive. After the row
    delete commits, ``speaker_cleanup`` removes every copy of the voice (issue #1214):
    the speaker's voiceprint from every speaker index (verified), its clusters'
    centroids rebuilt from the remaining members (an emptied cluster is deleted with
    its centroid), its profile's embedding rebuilt from the remaining members, and the
    file re-indexed so no index plane still names the speaker. An aggregate that can be
    neither rebuilt nor erased answers 503 with a fixed message — the delete itself has
    committed — and is logged at ERROR for repair.
    """
    speaker = require_speaker_access(
        db,
        speaker_uuid,
        current_user,
        organization_id=ctx.org_id,
        min_permission="editor",
        forbidden_detail="Requires editor permission",
        bypass=ctx.bypass,
    )

    # Read before the delete: the CASCADE forgets which clusters averaged this voice.
    impact = capture_speaker_impact(db, [int(speaker.id)], removed=True, reindex=True)

    db.delete(speaker)
    db.commit()

    raise_if_incomplete(reconcile_speaker_impact(db, impact))
